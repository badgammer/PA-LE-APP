#!/usr/bin/env bash
#
# Fleet-wide status view across every provisioned customer instance --
# the thing you lose the moment you split from "one appliance, one
# dashboard" into "N independent appliance instances." Deliberately reads
# certificate expiry directly off disk (openssl x509) and each customer's
# own systemd unit state, rather than calling into any customer's web UI
# -- so this has zero dependency on webui/app.py internals and keeps
# working even if a given customer's Flask process is down.
#
# Usage:
#   msp-fleet-status.sh                 # summary table, all customers
#   msp-fleet-status.sh --errors-only    # only customers with a problem
#   msp-fleet-status.sh <customer-slug>  # detail view for one customer

set -euo pipefail

CUSTOMERS_DIR="/etc/acme-appliance/customers"
ERRORS_ONLY=false
FILTER_SLUG=""

for arg in "$@"; do
    case "$arg" in
        --errors-only) ERRORS_ONLY=true ;;
        *) FILTER_SLUG="$arg" ;;
    esac
done

printf "%-24s %-10s %-10s %-22s %s\n" "CUSTOMER" "WEBUI" "RENEW" "SOONEST CERT EXPIRY" "NOTE"
printf "%-24s %-10s %-10s %-22s %s\n" "------------------------" "----------" "----------" "----------------------" "----"

for dir in "$CUSTOMERS_DIR"/*/; do
    [[ -d "$dir" ]] || continue
    slug="$(basename "$dir")"
    [[ -n "$FILTER_SLUG" && "$slug" != "$FILTER_SLUG" ]] && continue

    webui_state="down"
    systemctl is-active --quiet "acme-webui@${slug}.service" && webui_state="up"

    renew_state="disabled"
    systemctl is-enabled --quiet "acme-renew@${slug}.timer" 2>/dev/null && renew_state="enabled"

    soonest_expiry=""
    soonest_epoch=999999999999
    note=""
    # Matches ACME_APPLIANCE_LE_LIVE_DIR as set in
    # systemd/msp/acme-webui@.service: /etc/acme-appliance/customers/<slug>/letsencrypt/live
    live_dir="$dir/letsencrypt/live"
    if [[ -d "$live_dir" ]]; then
        for cert_dir in "$live_dir"/*/; do
            [[ -f "$cert_dir/cert.pem" ]] || continue
            end_date="$(openssl x509 -enddate -noout -in "$cert_dir/cert.pem" 2>/dev/null | sed 's/notAfter=//')"
            [[ -z "$end_date" ]] && continue
            epoch="$(date -d "$end_date" +%s 2>/dev/null || echo 999999999999)"
            if (( epoch < soonest_epoch )); then
                soonest_epoch="$epoch"
                soonest_expiry="$end_date"
            fi
        done
    fi

    now_epoch="$(date +%s)"
    if [[ -z "$soonest_expiry" ]]; then
        note="no certs issued yet"
    elif (( soonest_epoch - now_epoch < 7*86400 )); then
        note="EXPIRES < 7 DAYS"
    elif (( soonest_epoch < now_epoch )); then
        note="EXPIRED"
    fi
    [[ "$webui_state" == "down" ]] && note="WEBUI DOWN; $note"
    [[ "$renew_state" == "disabled" ]] && note="RENEW TIMER DISABLED; $note"

    if $ERRORS_ONLY && [[ -z "$note" ]]; then
        continue
    fi

    printf "%-24s %-10s %-10s %-22s %s\n" "$slug" "$webui_state" "$renew_state" "${soonest_expiry:-N/A}" "$note"
done
