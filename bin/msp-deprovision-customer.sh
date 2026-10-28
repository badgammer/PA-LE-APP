#!/usr/bin/env bash
#
# Deprovisions a customer NAMESPACE -- archives its config/certs/keys to
# a dated tarball (nothing is deleted outright, so an accidental
# offboarding is recoverable) and removes its directory tree.
#
# See bin/msp-provision-customer.sh's header comment for the full
# "clean redesign" rationale -- there is no separate Linux account or
# systemd instance per customer anymore, so unlike an earlier release of
# this appliance, this script does NOT call userdel or systemctl at all.
#
# Usage:
#   msp-deprovision-customer.sh <customer-slug>            # interactive confirm
#   msp-deprovision-customer.sh <customer-slug> --yes       # skip confirmation
#
# --yes is REQUIRED when this script is invoked non-interactively (no
# TTY) -- notably, this is how the MSP Console calls it (see
# msp_console/actions.py deprovision_customer()). The console performs
# its own confirmation step in the web UI (a "type the customer slug to
# confirm" field) before ever invoking this, so skipping the shell
# prompt here does not skip confirmation entirely -- it just moves where
# that confirmation happens for the non-interactive caller.

set -euo pipefail

SLUG=""
SKIP_CONFIRM=false
for arg in "$@"; do
    case "$arg" in
        --yes) SKIP_CONFIRM=true ;;
        *) SLUG="$arg" ;;
    esac
done
if [[ -z "$SLUG" ]]; then
    echo "Usage: msp-deprovision-customer.sh <customer-slug> [--yes]" >&2
    exit 1
fi

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

# Same lightweight, retry-free flock bin/msp-provision-customer.sh uses
# -- see that script's comment for why this profile no longer needs the
# stale-lock-detection/retry machinery an earlier release required (that
# was entirely about useradd/userdel's shared, external /etc/passwd
# lock file, which no longer applies since there is no per-customer
# Linux account to create or remove here anymore).
PROVISION_LOCK_FILE="/run/acme-appliance/msp-provision-ops.lock"
mkdir -p "$(dirname "$PROVISION_LOCK_FILE")"
exec 9>"$PROVISION_LOCK_FILE"
if ! flock -w 30 9; then
    echo "ERROR: could not acquire the provisioning lock within 30s -- another" >&2
    echo "       provision/deprovision is apparently stuck." >&2
    exit 1
fi

if ! $SKIP_CONFIRM; then
    read -r -p "This will archive '$SLUG''s config/certs and remove its directory tree. Continue? [y/N] " confirm
    [[ "$confirm" == "y" || "$confirm" == "Y" ]] || { echo "Aborted."; exit 1; }
fi

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

echo ""
echo "'$SLUG' deprovisioned. Remaining manual steps:"
echo "  If any PAN-OS firewalls or DNS provider accounts were dedicated to this"
echo "  customer (not shared), rotate/revoke those credentials independently --"
echo "  this script only touches this appliance's own config, not any"
echo "  third-party credential lifecycle."
