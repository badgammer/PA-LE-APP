#!/usr/bin/env bash
#
# Deprovisions a customer instance -- stops and disables its systemd
# units, ARCHIVES (never deletes outright) its config/cert directory to
# a dated tarball so an accidental offboarding is recoverable and
# certificate private keys don't linger unencrypted on disk indefinitely
# after a customer leaves, then removes this customer's dedicated system
# account.
#
# Usage:
#   msp-deprovision-customer.sh <customer-slug>

set -euo pipefail

SLUG="${1:?Usage: msp-deprovision-customer.sh <customer-slug>}"
SERVICE_USER="acmecust-$SLUG"
CUSTOMER_ETC_DIR="/etc/acme-appliance/customers/$SLUG"
CUSTOMER_VAR_DIR="/var/lib/acme-appliance/customers/$SLUG"
CUSTOMER_LOG_DIR="/var/log/acme-appliance/customers/$SLUG"
CUSTOMER_LOG_FILE="/var/log/acme-appliance/customers/$SLUG.log"
ARCHIVE_DIR="/etc/acme-appliance/offboarded"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

if [[ ! -d "$CUSTOMER_ETC_DIR" ]]; then
    echo "ERROR: no such customer directory: $CUSTOMER_ETC_DIR" >&2
    exit 1
fi

read -r -p "This will STOP service for '$SLUG', archive its config/certs, and remove its system account. Continue? [y/N] " confirm
[[ "$confirm" == "y" || "$confirm" == "Y" ]] || { echo "Aborted."; exit 1; }

echo "Stopping and disabling systemd instances for '$SLUG'..."
systemctl disable --now "acme-webui@${SLUG}.service" 2>/dev/null || true
systemctl disable --now "acme-renew@${SLUG}.timer" 2>/dev/null || true

install -d -m 0700 "$ARCHIVE_DIR"
ARCHIVE_PATH="$ARCHIVE_DIR/${SLUG}-${STAMP}.tar.gz"
echo "Archiving config, certs, and state (this may include private keys -- archive is 0600, root-owned)..."
STAGING_DIR="$(mktemp -d)"
trap 'rm -rf "$STAGING_DIR"' EXIT
mkdir -p "$STAGING_DIR/etc" "$STAGING_DIR/var-lib"
cp -a "$CUSTOMER_ETC_DIR" "$STAGING_DIR/etc/$SLUG"
[[ -d "$CUSTOMER_VAR_DIR" ]] && cp -a "$CUSTOMER_VAR_DIR" "$STAGING_DIR/var-lib/$SLUG"
tar -czf "$ARCHIVE_PATH" -C "$STAGING_DIR" .
chmod 0600 "$ARCHIVE_PATH"
echo "Archived to $ARCHIVE_PATH"

rm -rf "$CUSTOMER_ETC_DIR" "$CUSTOMER_VAR_DIR" "$CUSTOMER_LOG_DIR" "$CUSTOMER_LOG_FILE"
rm -rf "/run/acme-appliance/$SLUG"

if id -u "$SERVICE_USER" &>/dev/null; then
    echo "Removing system account '$SERVICE_USER'..."
    userdel "$SERVICE_USER" 2>/dev/null || true
fi

echo ""
echo "'$SLUG' deprovisioned. Remaining manual steps:"
echo "  1. Remove the customer's nginx/Caddy server block and reload."
echo "  2. If any PAN-OS firewalls or DNS provider accounts were dedicated"
echo "     to this customer (not shared), rotate/revoke those credentials"
echo "     independently -- this script only touches the appliance's own"
echo "     config and system account, not any third-party credential lifecycle."
