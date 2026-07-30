#!/usr/bin/env bash
#
# Main entry point: issues/renews a Let's Encrypt certificate for every
# domain in appliance.yaml (or just one) using DNS-01 validation (via
# dns_dispatcher.py) and deploys the result to Palo Alto via
# deploy_to_panos.py.
#
# Each domains[] entry can optionally list `additional_names` -- extra
# SANs included on the same certificate (e.g. a wildcard entry
# "*.example.com" with additional_names: ["example.com"] to also cover
# the bare apex domain on the same cert). All names for an entry are
# passed to certbot as separate -d flags in one invocation.
#
# Usage:
#   acme-renew.sh                     # process every configured domain entry
#   acme-renew.sh <domain>            # process only the entry containing
#                                      # <domain> (matches primary name or
#                                      # any additional_names entry)
#   acme-renew.sh <domain> --force    # same, ignoring certbot's 30-day
#                                      # renewal window

set -euo pipefail

APPLIANCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${ACME_APPLIANCE_CONFIG:-/etc/acme-appliance/appliance.yaml}"
LOG="${ACME_APPLIANCE_LOG:-/var/log/acme-appliance.log}"

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

EMAIL=$(python3 -c "import yaml,sys; print(yaml.safe_load(open('$CONFIG'))['acme']['email'])")
SERVER=$(python3 -c "import yaml,sys; print(yaml.safe_load(open('$CONFIG'))['acme']['server'])")

# Emits one line per certificate to issue:
#   "<safe-cert-name>\t<primary-name>\t<name1 name2 ...>"
# safe-cert-name is what gets passed to certbot's --cert-name (see
# cert_naming.py -- wildcard entries get a "wildcard." prefix instead of
# "*." so the on-disk lineage directory name has no special characters).
# The third field is every name (primary + any additional_names) that
# should be passed as -d flags for that cert.
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
    log "ERROR: '$ONLY_DOMAIN' does not match any domains[] entry (checked primary name and additional_names) in $CONFIG"
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

  # Build the -d flag array from the space-separated name list so certs
  # with additional_names (SANs) get every name on one certbot invocation.
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
      --manual --manual-public-ip-logging-ok \
      --manual-auth-hook "$APPLIANCE_DIR/dns_dispatcher.py add" \
      --manual-cleanup-hook "$APPLIANCE_DIR/dns_dispatcher.py remove" \
      --deploy-hook "$APPLIANCE_DIR/deploy_to_panos.py" \
      --cert-name "$CERT_NAME" \
      $FORCE_FLAG \
      "${DOMAIN_ARGS[@]}"; then
    log "OK: $ENTRY_NAME"
  else
    log "FAILED: $ENTRY_NAME (see certbot log at /var/log/letsencrypt/letsencrypt.log)"
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
