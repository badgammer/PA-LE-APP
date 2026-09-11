#!/usr/bin/env bash
#
# Provisions a new customer NAMESPACE for the msp-panos install profile.
#
# IMPORTANT -- this is the "clean redesign" version of this script. The
# msp-panos profile no longer runs one systemd instance + one dedicated
# Linux account PER CUSTOMER -- there is exactly ONE running process
# (the MSP Console, msp_console/app.py, under the single
# "acme-msp-console" service account) for the entire fleet. A "customer"
# is now purely a DATA namespace: its own physically-separate
# appliance.yaml, its own certbot --config-dir (so Let's Encrypt
# rate-limit accounting stays correctly isolated per customer -- see
# the main README), and its own log file -- all owned by that one
# shared service account, since there is no longer a separate account
# per customer to own them instead.
#
# This is a deliberate architectural change from an earlier release of
# this appliance (which DID create a dedicated acmecust-<slug> Linux
# account and a templated acme-webui@<slug>.service instance per
# customer) -- that model was built on a misunderstanding of the
# requirement: customers were never meant to log into their OWN web UI
# instance at all. Only MSP staff log in, via the MSP Console's own
# owner/staff permission model (msp_console/auth.py, unchanged) -- see
# CHANGES.md for the full rationale. One consequence worth knowing: this
# script no longer touches useradd/systemctl/nginx at all, which also
# means the entire useradd/etc-passwd-lock-contention class of bugs
# extensively documented earlier in CHANGES.md no longer applies to
# this profile -- there is no dynamic Linux account creation happening
# here anymore.
#
# Usage:
#   msp-provision-customer.sh <customer-slug>
#
# <customer-slug> must be filesystem/systemd-instance-safe: lowercase
# letters, digits, and hyphens only (it becomes the directory name under
# /etc/acme-appliance/customers/, /var/lib/acme-appliance/customers/,
# and /var/log/acme-appliance/customers/, and is embedded in certbot
# renewal lock file names -- keep it reasonably short and stable, it is
# not easily renamed later without also renaming the customer's existing
# certbot lineage names).

set -euo pipefail

SLUG="${1:?Usage: msp-provision-customer.sh <customer-slug>}"
if [[ ! "$SLUG" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]]; then
    echo "ERROR: '$SLUG' is not a valid instance slug (lowercase letters/digits/hyphens only, no leading/trailing hyphen)." >&2
    exit 1
fi
if [[ ${#SLUG} -gt 40 ]]; then
    echo "ERROR: '$SLUG' is too long -- keep customer slugs to 40 characters or fewer." >&2
    exit 1
fi

APPLIANCE_DIR="/opt/acme-appliance"
CUSTOMER_ETC_DIR="/etc/acme-appliance/customers/$SLUG"
CUSTOMER_VAR_DIR="/var/lib/acme-appliance/customers/$SLUG"
CUSTOMER_LOG_DIR="/var/log/acme-appliance/customers/$SLUG"
CUSTOMER_LOG_FILE="/var/log/acme-appliance/customers/$SLUG.log"

if [[ ! -f /etc/acme-appliance/profile ]] || [[ "$(cat /etc/acme-appliance/profile)" != "msp-panos" ]]; then
    echo "ERROR: this host was not installed with the msp-panos profile (see /etc/acme-appliance/profile)." >&2
    echo "       Run install.sh --profile=msp-panos first." >&2
    exit 1
fi

if [[ -d "$CUSTOMER_ETC_DIR" ]]; then
    echo "ERROR: $CUSTOMER_ETC_DIR already exists -- refusing to overwrite an existing customer." >&2
    exit 1
fi

# A lightweight flock, kept as defense-in-depth against two simultaneous
# "provision customer <same-slug>" requests racing between the
# already-exists check above and the mkdir below (e.g. a double-click on
# the MSP Console's "Provision customer" button reaching this script
# twice in close succession). Unlike the earlier per-account-creation
# lock this profile used to need, this is a PLAIN filesystem lock with
# no retry/stale-lock-detection machinery required -- there is no
# equivalent here to useradd's shared, external /etc/passwd lock file
# that could be left behind by an abnormally-killed process; flock's own
# kernel-level lock is released automatically the instant this process
# exits, by any means, including a crash.
PROVISION_LOCK_FILE="/run/acme-appliance/msp-provision-ops.lock"
mkdir -p "$(dirname "$PROVISION_LOCK_FILE")"
exec 9>"$PROVISION_LOCK_FILE"
if ! flock -w 30 9; then
    echo "ERROR: could not acquire the provisioning lock within 30s -- another" >&2
    echo "       provision/deprovision is apparently stuck." >&2
    exit 1
fi

echo "Provisioning customer namespace '$SLUG'..."

echo "  Creating directory tree..."
install -d -m 0700 "$CUSTOMER_ETC_DIR" "$CUSTOMER_ETC_DIR/backups" "$CUSTOMER_ETC_DIR/letsencrypt"
install -d -m 0700 "$CUSTOMER_VAR_DIR" "$CUSTOMER_VAR_DIR/letsencrypt"
install -d -m 0700 "$CUSTOMER_LOG_DIR"
touch "$CUSTOMER_LOG_FILE"
chmod 600 "$CUSTOMER_LOG_FILE"

echo "  Seeding starter appliance.yaml..."
if [[ -f "$APPLIANCE_DIR/config/appliance.yaml.example" ]]; then
    cp "$APPLIANCE_DIR/config/appliance.yaml.example" "$CUSTOMER_ETC_DIR/appliance.yaml"
    # Trim the example down to an empty starting point -- nothing further
    # to strip re: deploy providers here specifically, since with
    # ACME_APPLIANCE_PROFILE=msp-panos, deploy_providers/__init__.py
    # never registers the iis type in the first place regardless of what
    # the example file happens to contain.
    python3 - "$CUSTOMER_ETC_DIR/appliance.yaml" <<'PYEOF'
import sys, yaml
path = sys.argv[1]
cfg = yaml.safe_load(open(path)) or {}
cfg["dns_providers"] = {}
cfg["deploy_providers"] = {}
cfg["domains"] = []
cfg.setdefault("acme", {})["email"] = ""
yaml.safe_dump(cfg, open(path, "w"), default_flow_style=False, sort_keys=False)
PYEOF
else
    cat > "$CUSTOMER_ETC_DIR/appliance.yaml" <<'YAMLEOF'
acme:
  email: ""
  server: "https://acme-v02.api.letsencrypt.org/directory"
dns_providers: {}
deploy_providers: {}
domains: []
YAMLEOF
fi
chmod 600 "$CUSTOMER_ETC_DIR/appliance.yaml"

echo ""
echo "Customer '$SLUG' provisioned. Next steps:"
echo "  1. In the MSP Console, open Customers > $SLUG and set a real ACME account"
echo "     email under that customer's Settings before its first renewal."
echo "  2. Add DNS provider(s), PAN-OS deploy target(s), and domain(s) for this"
echo "     customer through the MSP Console -- no separate login or nginx routing"
echo "     is needed; MSP staff manage every customer from the same console,"
echo "     scoped by their own owner/staff read-write grants."
