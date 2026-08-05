#!/usr/bin/env bash
#
# Runs as root via systemd (see systemd/acme-appliance-check-updates.service).
# Checks for available OS package updates and writes the results to
# status files the (unprivileged) web UI can read afterward.
#
# This deliberately does NOT install anything -- see apply-system-update.sh
# for the actual "dnf update -y" step, which is a separate, explicitly
# triggered action.

set -uo pipefail  # NOT -e: we want to still write status files even if dnf errors

STATUS_DIR="${ACME_APPLIANCE_RUN_DIR:-/var/run/acme-appliance}"
LOG="${ACME_APPLIANCE_LOG:-/var/log/acme-appliance.log}"
mkdir -p "$STATUS_DIR"

OUT_FILE="$STATUS_DIR/available-updates.txt"
EXITCODE_FILE="$STATUS_DIR/available-updates-exitcode.txt"
CHECKED_AT_FILE="$STATUS_DIR/available-updates-checked-at.txt"

log() { echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') system-update-check: $*" | tee -a "$LOG"; }

log "Checking for available package updates (dnf check-update)..."

# dnf check-update exit codes: 0 = no updates available, 100 = updates
# available, 1 = an error occurred. We want the exit code, not to abort
# on 100 (which "set -e" would otherwise treat as a failure).
dnf check-update > "$OUT_FILE.tmp" 2>&1
EXIT_CODE=$?
mv "$OUT_FILE.tmp" "$OUT_FILE"
chmod 644 "$OUT_FILE"

echo "$EXIT_CODE" > "$EXITCODE_FILE"
chmod 644 "$EXITCODE_FILE"
date -u +'%Y-%m-%dT%H:%M:%SZ' > "$CHECKED_AT_FILE"
chmod 644 "$CHECKED_AT_FILE"

case "$EXIT_CODE" in
  0)   log "No updates available." ;;
  100) log "Updates are available (see $OUT_FILE for the list)." ;;
  *)   log "dnf check-update exited with code $EXIT_CODE (see $OUT_FILE for details)." ;;
esac
