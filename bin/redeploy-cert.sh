#!/usr/bin/env bash
#
# Re-runs ONLY the PAN-OS import/attach/commit steps for a certificate
# that has ALREADY been issued and is sitting on disk -- no new ACME
# issuance, no DNS-01 challenge, no Let's Encrypt rate-limit usage at
# all. Useful when the certificate itself is fine but the firewall-side
# deployment failed or needs to be redone (e.g. after fixing an SSL/TLS
# profile name, adding a firewall target, or working around the
# category=certificate vs category=keypair PAN-OS import bug that this
# appliance previously had).
#
# This is intentionally a thin wrapper: it locates the existing certbot
# lineage directory for the given domain and hands it to
# deploy_to_panos.py via the exact same RENEWED_LINEAGE / RENEWED_DOMAINS
# environment variables that certbot's own --deploy-hook mechanism uses --
# deploy_to_panos.py has no idea (and does not need to know) whether it
# was invoked by certbot after a real renewal or by this script against
# an already-issued certificate.
#
# Usage:
#   redeploy-cert.sh <domain-entry-name>
#
# <domain-entry-name> must match a domains[].name value EXACTLY (the
# web UI always passes this correctly since it reads the name directly
# from appliance.yaml -- if running by hand, use the same name shown on
# the Domains page, including a leading "*." for wildcard entries).

set -euo pipefail

APPLIANCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${ACME_APPLIANCE_CONFIG:-/etc/acme-appliance/appliance.yaml}"
LOG="${ACME_APPLIANCE_LOG:-/var/log/acme-appliance.log}"
LE_CONFIG_DIR="${ACME_APPLIANCE_LE_CONFIG_DIR:-/etc/acme-appliance/letsencrypt}"

log() { echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') redeploy-cert.sh: $*" | tee -a "$LOG"; }

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
  echo "Usage: redeploy-cert.sh <domain-entry-name>" >&2
  exit 1
fi

if [ ! -f "$CONFIG" ]; then
  log "ERROR: config not found at $CONFIG"
  exit 1
fi

# Confirm this is actually a configured domains[] entry (matches primary
# name only -- redeploy targets one specific certificate/lineage, so we
# deliberately do NOT also match on additional_names here the way
# acme-renew.sh's single-domain mode does; the web UI always passes the
# entry's primary name).
KNOWN=$(python3 -c "
import yaml
cfg = yaml.safe_load(open('$CONFIG'))
target = '$DOMAIN'
print('yes' if any(d['name'] == target for d in cfg.get('domains', [])) else 'no')
")
if [ "$KNOWN" != "yes" ]; then
  log "ERROR: '$DOMAIN' does not match any domains[] entry's name in $CONFIG"
  exit 1
fi

SAFE_NAME=$(python3 -c "
import sys
sys.path.insert(0, '$APPLIANCE_DIR')
from cert_naming import safe_cert_name
print(safe_cert_name('$DOMAIN'))
")

LINEAGE_DIR="$LE_CONFIG_DIR/live/$SAFE_NAME"

if [ ! -f "$LINEAGE_DIR/fullchain.pem" ] || [ ! -f "$LINEAGE_DIR/privkey.pem" ]; then
  log "ERROR: no certificate found at $LINEAGE_DIR for '$DOMAIN' -- nothing to redeploy."
  log "Issue a certificate first (use 'Renew now' on the Domains page)."
  exit 1
fi

log "Redeploying existing certificate for '$DOMAIN' from $LINEAGE_DIR"
log "(no new ACME issuance -- this does not use any Let's Encrypt rate-limit headroom)"

if RENEWED_LINEAGE="$LINEAGE_DIR" RENEWED_DOMAINS="$DOMAIN" ACME_APPLIANCE_CONFIG="$CONFIG" \
    python3 "$APPLIANCE_DIR/deploy_to_panos.py" 2>&1 | tee -a "$LOG"; then
  log "OK: redeployed '$DOMAIN' to its configured firewall target(s)"
else
  log "FAILED: redeploy of '$DOMAIN' (see output above; full deploy log entries are also in $LOG)"
  exit 1
fi
