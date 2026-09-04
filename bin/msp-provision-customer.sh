#!/usr/bin/env bash
#
# Provisions a new customer instance for the msp-panos install profile:
# creates a dedicated system account for this customer (mirroring how
# bootstrap-appliance.sh / lib/profile-single-instance.sh creates the
# single "acme-appliance" account for the single-instance profile),
# creates and chowns this customer's config/cert/log/runtime directory
# tree, seeds a starter appliance.yaml, generates its own webui TLS...
# actually TLS is terminated by nginx for this profile, not per-instance
# -- see deploy/nginx/acme-appliance-msp.conf.template -- then enables +
# starts this customer's systemd instances (web UI + daily renewal
# timer) and prints the nginx upstream snippet needed to route to it.
#
# Usage:
#   msp-provision-customer.sh <customer-slug>
#
# <customer-slug> must be systemd-instance-safe: lowercase letters,
# digits, and hyphens only (it becomes the %i in every acme-*@<slug>
# systemd unit, a directory name under /etc/acme-appliance/customers/,
# and part of this customer's dedicated "acmecust-<slug>" system account
# name -- keep it reasonably short, Linux usernames are capped at 32
# characters and "acmecust-" already uses 9 of them).

set -euo pipefail

SLUG="${1:?Usage: msp-provision-customer.sh <customer-slug>}"
if [[ ! "$SLUG" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]]; then
    echo "ERROR: '$SLUG' is not a valid instance slug (lowercase letters/digits/hyphens only, no leading/trailing hyphen)." >&2
    exit 1
fi
if [[ ${#SLUG} -gt 23 ]]; then
    echo "ERROR: '$SLUG' is too long -- keep customer slugs to 23 characters or fewer" >&2
    echo "       (the 'acmecust-' account name prefix plus your slug must fit within" >&2
    echo "       Linux's 32-character username limit)." >&2
    exit 1
fi

APPLIANCE_DIR="/opt/acme-appliance"
SERVICE_USER="acmecust-$SLUG"
CUSTOMER_ETC_DIR="/etc/acme-appliance/customers/$SLUG"
CUSTOMER_VAR_DIR="/var/lib/acme-appliance/customers/$SLUG"
CUSTOMER_LOG_DIR="/var/log/acme-appliance/customers/$SLUG"
CUSTOMER_LOG_FILE="/var/log/acme-appliance/customers/$SLUG.log"
CUSTOMER_RUN_DIR="/run/acme-appliance/$SLUG"

if [[ ! -f /etc/acme-appliance/profile ]] || [[ "$(cat /etc/acme-appliance/profile)" != "msp-panos" ]]; then
    echo "ERROR: this host was not installed with the msp-panos profile (see /etc/acme-appliance/profile)." >&2
    echo "       Run install.sh --profile=msp-panos first." >&2
    exit 1
fi

if [[ -d "$CUSTOMER_ETC_DIR" ]]; then
    echo "ERROR: $CUSTOMER_ETC_DIR already exists -- refusing to overwrite an existing customer." >&2
    exit 1
fi

echo "Provisioning customer instance '$SLUG'..."

echo "  Creating dedicated system account '$SERVICE_USER'..."
id -u "$SERVICE_USER" &>/dev/null || useradd --system --home "$CUSTOMER_ETC_DIR" --shell /sbin/nologin "$SERVICE_USER"

echo "  Creating and chowning directory tree..."
install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$CUSTOMER_ETC_DIR" "$CUSTOMER_ETC_DIR/backups" "$CUSTOMER_ETC_DIR/letsencrypt"
install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$CUSTOMER_VAR_DIR" "$CUSTOMER_VAR_DIR/letsencrypt"
install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$CUSTOMER_LOG_DIR"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$CUSTOMER_RUN_DIR"
touch "$CUSTOMER_LOG_FILE"
chown "$SERVICE_USER:$SERVICE_USER" "$CUSTOMER_LOG_FILE"
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
chown "$SERVICE_USER:$SERVICE_USER" "$CUSTOMER_ETC_DIR/appliance.yaml"
chmod 600 "$CUSTOMER_ETC_DIR/appliance.yaml"

echo "  Enabling systemd instances..."
systemctl daemon-reload
systemctl enable --now "acme-webui@${SLUG}.service"
systemctl enable --now "acme-renew@${SLUG}.timer"

echo ""
echo "Customer '$SLUG' provisioned. Next steps:"
echo "  1. Add this nginx server block (or the equivalent Caddy block) and reload:"
echo ""
echo "     server {"
echo "         listen 443 ssl http2;"
echo "         server_name ${SLUG}.appliance.yourmsp.com;"
echo "         # ... your ssl_certificate / ssl_certificate_key directives ..."
echo "         location / {"
echo "             proxy_pass http://unix:/run/acme-appliance/${SLUG}/webui.sock;"
echo "             proxy_set_header Host \$host;"
echo "             proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;"
echo "             proxy_set_header X-Forwarded-Proto \$scheme;"
echo "         }"
echo "     }"
echo ""
echo "     (See $APPLIANCE_DIR/deploy/nginx/acme-appliance-msp.conf.template"
echo "     for the full canonical version of this block.)"
echo ""
echo "  2. Browse to https://${SLUG}.appliance.yourmsp.com/setup to create the admin account."
echo "  3. Add DNS provider(s), PAN-OS deploy target(s), and domain(s) through the web UI."
echo "     (Windows/IIS is not offered as a deploy target type on this profile, and the"
echo "     System page is not shown -- both are single-instance-profile-only features.)"
