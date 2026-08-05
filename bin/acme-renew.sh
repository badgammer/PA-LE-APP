#!/usr/bin/env bash
#
# Main entry point: issues/renews a Let's Encrypt certificate for every
# domain in appliance.yaml (or just one) using DNS-01 validation (via
# dns_dispatcher.py) and deploys the result to Palo Alto via
# deploy_to_panos.py.
#
# Usage:
#   acme-renew.sh                     # process every configured domain entry
#   acme-renew.sh <domain>            # process only the entry containing <domain>
#   acme-renew.sh <domain> --force    # same, ignoring certbot's 30-day renewal window

set -euo pipefail

APPLIANCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${ACME_APPLIANCE_CONFIG:-/etc/acme-appliance/appliance.yaml}"
LOG="${ACME_APPLIANCE_LOG:-/var/log/acme-appliance.log}"

LE_CONFIG_DIR="${ACME_APPLIANCE_LE_CONFIG_DIR:-/etc/acme-appliance/letsencrypt}"
LE_WORK_DIR="${ACME_APPLIANCE_LE_WORK_DIR:-/var/lib/acme-appliance/letsencrypt}"
LE_LOGS_DIR="${ACME_APPLIANCE_LE_LOGS_DIR:-/var/log/acme-appliance/letsencrypt}"

ONLY_DOMAIN=""
FORCE_FLAG=""
for arg in "$@"; do
  case "$arg" in
    --force) FORCE_FLAG="--force-renewal" ;;
    *) ONLY_DOMAIN="$arg" ;;
  esac
done

log() { echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') acme-renew.sh: $*" | tee -a "$LOG"; }

if [ ! -f "$CONFIG" ]; then
  log "ERROR: config not found at $CONFIG"
  exit 1
fi

if ! command -v certbot >/dev/null 2>&1; then
  log "ERROR: certbot is not installed or not on PATH."
  log "Install it with: sudo dnf install -y epel-release certbot"
  exit 1
fi

# certbot 3.0+ removed --manual-public-ip-logging-ok; older versions still
# require it. Detect at runtime so this works regardless of installed version.
IP_LOGGING_FLAG=""
CERTBOT_VERSION_RAW="$(certbot --version 2>&1 || true)"
CERTBOT_MAJOR="$(echo "$CERTBOT_VERSION_RAW" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 | cut -d. -f1)"
if [[ "$CERTBOT_MAJOR" =~ ^[0-9]+$ ]] && [ "$CERTBOT_MAJOR" -lt 3 ]; then
  IP_LOGGING_FLAG="--manual-public-ip-logging-ok"
  log "Detected certbot major version $CERTBOT_MAJOR (< 3) -- including --manual-public-ip-logging-ok"
else
  log "Detected certbot version '${CERTBOT_VERSION_RAW:-unknown}' -- --manual-public-ip-logging-ok not needed/accepted (removed in certbot 3.0+), omitting it"
fi

for dir in "$LE_CONFIG_DIR" "$LE_WORK_DIR" "$LE_LOGS_DIR"; do
  if ! mkdir -p "$dir" 2>/tmp/acme-renew-mkdir-err.$$; then
    log "ERROR: could not create '$dir': $(cat /tmp/acme-renew-mkdir-err.$$ 2>/dev/null)"
    rm -f /tmp/acme-renew-mkdir-err.$$
    exit 1
  fi
  rm -f /tmp/acme-renew-mkdir-err.$$
done

EMAIL=$(python3 -c "import yaml,sys; print(yaml.safe_load(open('$CONFIG'))['acme']['email'])")
SERVER=$(python3 -c "import yaml,sys; print(yaml.safe_load(open('$CONFIG'))['acme']['server'])")

if [ -z "$EMAIL" ] || [[ "$EMAIL" == *"@example.com" ]] || [[ "$EMAIL" == *"@example.org" ]] || [[ "$EMAIL" == *"@example.net" ]]; then
  log "ERROR: acme.email in $CONFIG is not set to a real address (currently: '$EMAIL')."
  log "Let's Encrypt rejects placeholder domains like example.com/.org/.net during"
  log "account registration. Set a real, monitored email via the web UI's Settings"
  log "page, or edit $CONFIG directly."
  exit 1
fi

ALL_ENTRIES=$(python3 -c "
import sys
sys.path.insert(0, '$APPLIANCE_DIR')
import yaml
from cert_naming import safe_cert_name
cfg = yaml.safe_load(open('$CONFIG'))
for d in cfg.get('domains', []):
    names = [d['name']] + list(d.get('additional_names', []))
    print(safe_cert_name(d['name']) + '\t' + d['name'] + '\t' + ' '.join(names))
")

if [ -n "$ONLY_DOMAIN" ]; then
  RESOLVED=$(python3 -c "
import sys
sys.path.insert(0, '$APPLIANCE_DIR')
import yaml
from cert_naming import safe_cert_name
cfg = yaml.safe_load(open('$CONFIG'))
target = '$ONLY_DOMAIN'
for d in cfg.get('domains', []):
    names = [d['name']] + list(d.get('additional_names', []))
    if target == d['name'] or target in names:
        print(safe_cert_name(d['name']) + '\t' + d['name'] + '\t' + ' '.join(names))
        break
")
  if [ -z "$RESOLVED" ]; then
    log "ERROR: '$ONLY_DOMAIN' does not match any domains[] entry in $CONFIG"
    exit 1
  fi
  ENTRIES="$RESOLVED"
  log "Single-domain run requested for $ONLY_DOMAIN${FORCE_FLAG:+ (forced)}"
else
  ENTRIES="$ALL_ENTRIES"
fi

if [ -z "$ENTRIES" ]; then
  log "No domains configured in $CONFIG - nothing to do"
  exit 0
fi

FAILURES=0
while IFS=$'\t' read -r CERT_NAME ENTRY_NAME NAME_LIST; do
  [ -z "$ENTRY_NAME" ] && continue

  DOMAIN_ARGS=()
  for NAME in $NAME_LIST; do
    DOMAIN_ARGS+=(-d "$NAME")
  done

  log "Processing $ENTRY_NAME (cert-name: $CERT_NAME, names: $NAME_LIST)"
  if ACME_APPLIANCE_CONFIG="$CONFIG" certbot certonly \
      --non-interactive --agree-tos \
      --email "$EMAIL" \
      --server "$SERVER" \
      --preferred-challenges dns \
      --manual $IP_LOGGING_FLAG \
      --manual-auth-hook "$APPLIANCE_DIR/dns_dispatcher.py add" \
      --manual-cleanup-hook "$APPLIANCE_DIR/dns_dispatcher.py remove" \
      --deploy-hook "$APPLIANCE_DIR/deploy_to_panos.py" \
      --config-dir "$LE_CONFIG_DIR" \
      --work-dir "$LE_WORK_DIR" \
      --logs-dir "$LE_LOGS_DIR" \
      --cert-name "$CERT_NAME" \
      $FORCE_FLAG \
      "${DOMAIN_ARGS[@]}" 2>&1 | tee -a "$LOG"; then
    log "OK: $ENTRY_NAME"
  else
    log "FAILED: $ENTRY_NAME (certbot output above; full log also at $LE_LOGS_DIR/letsencrypt.log)"
    FAILURES=$((FAILURES + 1))
  fi
done <<< "$ENTRIES"

if [ "$FAILURES" -gt 0 ]; then
  log "$FAILURES certificate(s) failed this run"
  exit 1
fi

if [ -n "$ONLY_DOMAIN" ]; then
  log "$ONLY_DOMAIN processed successfully"
else
  log "All domains processed successfully"
fi
