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
#
# NOTE on certbot's storage directories: this appliance runs certbot as an
# UNPRIVILEGED service account (acme-appliance), not root. certbot's
# normal defaults (/etc/letsencrypt, /var/lib/letsencrypt,
# /var/log/letsencrypt) are root-owned system directories that a
# non-root account cannot create or write to -- so instead we point
# certbot at appliance-owned directories via --config-dir/--work-dir/
# --logs-dir.
#
# NOTE on --manual-public-ip-logging-ok: this flag was REMOVED entirely
# in certbot 3.0.0 (it had been a deprecated no-op for a while before
# that) -- see https://github.com/certbot/certbot/issues/9988. EPEL9's
# certbot package is currently 3.1.0+, so on a stock Rocky/RHEL 9
# install, passing this flag causes a hard
# "certbot: error: unrecognized arguments: --manual-public-ip-logging-ok"
# failure. Some older/EL8 or manually-installed certbot versions (<3.0)
# still REQUIRE this flag to avoid an interactive confirmation prompt
# when running non-interactively. Rather than hardcoding one behavior
# and breaking the other, this script detects the installed certbot's
# major version at runtime and only includes the flag on versions that
# still need it.

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
  log "(then re-run this script or trigger a renewal from the web UI)"
  exit 1
fi

# Determine whether this certbot install still needs (and accepts)
# --manual-public-ip-logging-ok. certbot --version prints e.g.
# "certbot 3.1.0"; older releases (e.g. "certbot 1.32.0" on EL8) print
# the same format. If parsing fails for any reason, default to OMITTING
# the flag -- that matches all currently-shipping certbot versions
# (3.0+), so it's the safer default going forward as older versions age
# out of use.
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
    log "This usually means the service account doesn't own this directory,"
    log "or (if triggered from the web UI) the acme-webui.service systemd"
    log "unit's ReadWritePaths doesn't include it."
    rm -f /tmp/acme-renew-mkdir-err.$$
    exit 1
  fi
  rm -f /tmp/acme-renew-mkdir-err.$$
done

EMAIL=$(python3 -c "import yaml,sys; print(yaml.safe_load(open('$CONFIG'))['acme']['email'])")
SERVER=$(python3 -c "import yaml,sys; print(yaml.safe_load(open('$CONFIG'))['acme']['server'])")

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
