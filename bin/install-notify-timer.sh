#!/usr/bin/env bash
#
# Installs (or removes) the daily certificate-expiry email check as a systemd timer, for whichever
# install profile this host was set up with. Idempotent -- safe to re-run after an update.
#
#   sudo /opt/acme-appliance/bin/install-notify-timer.sh            # install + enable + start
#   sudo /opt/acme-appliance/bin/install-notify-timer.sh --remove   # disable + delete the units
#
# single-instance : runs as the acme-appliance account against /etc/acme-appliance/appliance.yaml
# msp-panos       : runs as the acme-msp-console account and checks EVERY customer namespace
#
# The check does nothing (and sends nothing) until email alerts are enabled on the Notifications page.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: run as root (sudo)." >&2
    exit 1
fi

APPLIANCE_DIR="${APPLIANCE_DIR:-/opt/acme-appliance}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
SERVICE="acme-notify-check.service"
TIMER="acme-notify-check.timer"

if [[ "${1:-}" == "--remove" ]]; then
    systemctl disable --now "$TIMER" 2>/dev/null || true
    rm -f "$UNIT_DIR/$SERVICE" "$UNIT_DIR/$TIMER"
    systemctl daemon-reload
    echo "Removed $TIMER and $SERVICE."
    exit 0
fi

PROFILE="single-instance"
[[ -f /etc/acme-appliance/profile ]] && PROFILE="$(cat /etc/acme-appliance/profile)"

case "$PROFILE" in
    msp-panos)
        RUN_USER="acme-msp-console"
        EXTRA_ARGS=" --all-customers"
        EXTRA_ENV="Environment=ACME_APPLIANCE_LOG=/var/log/acme-appliance/msp-console.log"
        ;;
    *)
        RUN_USER="acme-appliance"
        EXTRA_ARGS=""
        EXTRA_ENV=""
        ;;
esac

if ! id "$RUN_USER" >/dev/null 2>&1; then
    echo "ERROR: service account '$RUN_USER' does not exist. Has install.sh been run for the '$PROFILE' profile?" >&2
    exit 1
fi
if [[ ! -x "$APPLIANCE_DIR/venv/bin/python" || ! -f "$APPLIANCE_DIR/bin/acme-notify.py" ]]; then
    echo "ERROR: $APPLIANCE_DIR/venv/bin/python or $APPLIANCE_DIR/bin/acme-notify.py not found." >&2
    echo "       Deploy the updated code to $APPLIANCE_DIR first (git pull) and re-run." >&2
    exit 1
fi

cat > "$UNIT_DIR/$SERVICE" <<UNIT
[Unit]
Description=ACME appliance - certificate expiry email alert check ($PROFILE)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
Environment=ACME_APPLIANCE_PROFILE=$PROFILE
${EXTRA_ENV}
ExecStart=$APPLIANCE_DIR/venv/bin/python $APPLIANCE_DIR/bin/acme-notify.py check-expiry${EXTRA_ARGS}
NoNewPrivileges=true
PrivateTmp=true
UNIT

cat > "$UNIT_DIR/$TIMER" <<UNIT
[Unit]
Description=Daily certificate expiry email alert check

[Timer]
OnCalendar=*-*-* 07:30:00
RandomizedDelaySec=20min
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now "$TIMER"
echo "Installed and started $TIMER (profile: $PROFILE, runs as $RUN_USER)."
echo "Test it any time without waiting:  sudo -u $RUN_USER $APPLIANCE_DIR/venv/bin/python $APPLIANCE_DIR/bin/acme-notify.py check-expiry${EXTRA_ARGS} --dry-run"
