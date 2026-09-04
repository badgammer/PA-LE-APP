#!/usr/bin/env bash
#
# Tails ONE specific customer's own log file and prints it to stdout.
# Exists ONLY so the MSP Console (which runs as its own unprivileged
# system account, acme-msp-console) can show a customer's recent log
# activity in the dashboard -- that log file is owned 0600 by the
# customer's OWN dedicated system account (acmecust-<slug>), which the
# console has no read access to by design. Running this one narrow,
# read-only operation as root (via the sudoers rule in
# iso-build/sudoers.d/acme-msp-console) is the one sanctioned way to
# cross that boundary, exactly like every other privileged action the
# console performs.
#
# Usage:
#   msp-tail-log.sh <customer-slug> [lines]

set -euo pipefail

SLUG="${1:?Usage: msp-tail-log.sh <customer-slug> [lines]}"
LINES="${2:-200}"

if [[ ! "$SLUG" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || [[ ${#SLUG} -gt 23 ]]; then
    echo "ERROR: '$SLUG' is not a valid customer slug." >&2
    exit 1
fi
if ! [[ "$LINES" =~ ^[0-9]+$ ]] || [[ "$LINES" -lt 1 ]] || [[ "$LINES" -gt 2000 ]]; then
    echo "ERROR: lines must be a number between 1 and 2000." >&2
    exit 1
fi

LOG_FILE="/var/log/acme-appliance/customers/$SLUG.log"

if [[ ! -f "$LOG_FILE" ]]; then
    echo "(no log file yet for '$SLUG')"
    exit 0
fi

tail -n "$LINES" "$LOG_FILE"
