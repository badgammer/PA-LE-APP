#!/usr/bin/env bash
#
# Runs as root via systemd (see systemd/acme-appliance-updates.service).
# Applies all available OS package updates via "dnf update -y", logs
# progress to the shared appliance log, and records whether a reboot is
# required afterward so the web UI can surface that.
#
# This is only ever started via a narrowly-scoped sudoers rule that lets
# the unprivileged acme-appliance service account run exactly
# "systemctl start acme-appliance-updates.service" -- see
# iso-build/sudoers.d/acme-appliance-updates. The web UI additionally
# requires the person triggering this to authenticate with a real Linux
# sudo-capable account via PAM before it will even issue that systemctl
# call (see webui/system_updates.py) -- this script itself has no
# awareness of who requested it; it just does the update and records
# the outcome.

set -uo pipefail  # NOT -e: we want to still record status/cleanup even if dnf fails

RUN_DIR="${ACME_APPLIANCE_RUN_DIR:-/var/run/acme-appliance}"
LOG="${ACME_APPLIANCE_LOG:-/var/log/acme-appliance.log}"
LOCK_FILE="$RUN_DIR/system-update.lock"
STATUS_FILE="$RUN_DIR/last-update-status.txt"

mkdir -p "$RUN_DIR"
log() { echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') system-update: $*" | tee -a "$LOG"; }

echo $$ > "$LOCK_FILE"
chmod 644 "$LOCK_FILE"
cleanup() { rm -f "$LOCK_FILE"; }
trap cleanup EXIT

log "Starting dnf update -y"
dnf update -y 2>&1 | tee -a "$LOG"
DNF_EXIT=${PIPESTATUS[0]}

if [ "$DNF_EXIT" -eq 0 ]; then
  log "dnf update -y completed successfully"
else
  log "dnf update -y FAILED with exit code $DNF_EXIT"
fi

REBOOT_REQUIRED="unknown"
if command -v needs-restarting >/dev/null 2>&1; then
  # needs-restarting -r: exit 0 = reboot NOT required, exit 1 = reboot required.
  if needs-restarting -r >/dev/null 2>&1; then
    REBOOT_REQUIRED="no"
  else
    REBOOT_REQUIRED="yes"
  fi
else
  log "needs-restarting not found (install dnf-utils/yum-utils for reboot-required detection)"
fi

{
  echo "timestamp=$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
  echo "exit_code=$DNF_EXIT"
  echo "reboot_required=$REBOOT_REQUIRED"
} > "$STATUS_FILE"
chmod 644 "$STATUS_FILE"

log "Reboot required: $REBOOT_REQUIRED"
exit "$DNF_EXIT"
