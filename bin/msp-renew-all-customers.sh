#!/usr/bin/env bash
#
# Daily entry point for the msp-panos profile: loops over every
# provisioned customer namespace and runs bin/acme-renew.sh -- itself
# COMPLETELY UNMODIFIED -- once per customer, pointed at that customer's
# own appliance.yaml, certbot --config-dir/--work-dir/--logs-dir, and log
# file via environment variables.
#
# This REPLACES the earlier release's N separate systemd timer instances
# (acme-renew@<slug>.timer, one per customer) with ONE timer
# (systemd/msp-console/acme-msp-renewal.timer) that runs this ONE
# script. The certbot rate-limit isolation that model provided is fully
# preserved here: each customer still gets its OWN --config-dir (and
# therefore its own Let's Encrypt account registration and its own
# per-domain issuance history), it's just achieved by looping this
# single script over each customer's directory rather than by giving
# each customer its own systemd unit and Linux account.
#
# Usage:
#   msp-renew-all-customers.sh                 # process every customer
#   msp-renew-all-customers.sh <customer-slug>  # process just one customer
#
# A failure processing one customer does NOT stop this script from
# continuing on to the next customer -- every customer gets a renewal
# attempt regardless of whether an earlier one failed, and this script's
# own exit code reflects whether ANY customer failed (non-zero) or every
# customer that was actually processed succeeded (zero).

set -uo pipefail  # NOT -e: a failure for one customer must not abort the loop

APPLIANCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CUSTOMERS_DIR="/etc/acme-appliance/customers"
MSP_LOG="/var/log/acme-appliance/msp-console.log"

FILTER_SLUG="${1:-}"

log() { echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') msp-renew-all-customers: $*" | tee -a "$MSP_LOG"; }

if [[ ! -d "$CUSTOMERS_DIR" ]]; then
    log "No customers directory found at $CUSTOMERS_DIR -- nothing to do."
    exit 0
fi

FAILURES=0
PROCESSED=0

for dir in "$CUSTOMERS_DIR"/*/; do
    [[ -d "$dir" ]] || continue
    slug="$(basename "$dir")"
    if [[ -n "$FILTER_SLUG" && "$slug" != "$FILTER_SLUG" ]]; then
        continue
    fi
    if [[ ! -f "$dir/appliance.yaml" ]]; then
        log "Skipping '$slug' -- no appliance.yaml found (incompletely provisioned?)."
        continue
    fi

    log "Processing customer '$slug'..."
    PROCESSED=$((PROCESSED + 1))

    if ACME_APPLIANCE_CONFIG="/etc/acme-appliance/customers/$slug/appliance.yaml" \
       ACME_APPLIANCE_LOG="/var/log/acme-appliance/customers/$slug.log" \
       ACME_APPLIANCE_LE_CONFIG_DIR="/etc/acme-appliance/customers/$slug/letsencrypt" \
       ACME_APPLIANCE_LE_WORK_DIR="/var/lib/acme-appliance/customers/$slug/letsencrypt" \
       ACME_APPLIANCE_LE_LOGS_DIR="/var/log/acme-appliance/customers/$slug/letsencrypt" \
       "$APPLIANCE_DIR/bin/acme-renew.sh"; then
        log "OK: customer '$slug' processed successfully (see /var/log/acme-appliance/customers/$slug.log for per-domain detail)."
    else
        FAILURES=$((FAILURES + 1))
        log "FAILED: customer '$slug' had one or more renewal failures (see /var/log/acme-appliance/customers/$slug.log for detail)."
    fi
done

if [[ -n "$FILTER_SLUG" && "$PROCESSED" -eq 0 ]]; then
    log "ERROR: '$FILTER_SLUG' does not match any provisioned customer under $CUSTOMERS_DIR"
    exit 1
fi

if [[ "$FAILURES" -gt 0 ]]; then
    log "$FAILURES of $PROCESSED customer(s) had renewal failures this run."
    exit 1
fi

log "All $PROCESSED customer(s) processed successfully."
