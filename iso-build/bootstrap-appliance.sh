#!/usr/bin/env bash
#
# One-shot provisioning script: turns a freshly-installed, internet-
# connected Rocky Linux 9 (minimal) box into a fully running ACME/
# GlobalProtect appliance -- no ISO building required.
#
# It is idempotent -- safe to re-run if a step fails partway through.
#
# Usage (as root):
#   ./bootstrap-appliance.sh [/path/to/acme-appliance-source]

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Must be run as root (use sudo)." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="${1:-$(cd "$SCRIPT_DIR/.." && pwd)}"
INSTALL_DIR="/opt/acme-appliance"
CONFIG_DIR="/etc/acme-appliance"
SERVICE_USER="acme-appliance"
RUN_DIR="/var/run/acme-appliance"

LE_CONFIG_DIR="$CONFIG_DIR/letsencrypt"
LE_WORK_DIR="/var/lib/acme-appliance/letsencrypt"
LE_LOGS_DIR="/var/log/acme-appliance/letsencrypt"

log() { echo "[bootstrap] $*"; }

if [ ! -f "$SRC_DIR/webui/app.py" ]; then
  echo "ERROR: '$SRC_DIR' does not look like an acme-appliance source tree (webui/app.py not found)." >&2
  echo "Pass the path to the appliance source as the first argument." >&2
  exit 1
fi

log "Installing critical OS packages (epel-release, python3, certbot, openssl)..."
# IMPORTANT: this dnf install list contains ONLY packages this appliance
# genuinely cannot function without. Every package in a single dnf
# transaction must resolve successfully for ANY of them to install --
# one broken/unavailable package aborts the WHOLE transaction, silently
# preventing everything else in the same command (including certbot)
# from installing too. This has bitten this script twice before:
#   - "python3-venv" isn't a real package on Rocky/RHEL (venv ships
#     inside base python3) -- listing it here previously broke this
#     exact transaction.
#   - "policycoreutils-python-utils" has been observed to have an
#     unsatisfiable dependency on some systems (e.g. a repo/mirror
#     metadata mismatch reporting "nothing provides policycoreutils =
#     X.Y-Z" for the exact version python3-policycoreutils requires) --
#     this ALSO previously broke this exact transaction and blocked
#     certbot from installing, even though this appliance doesn't
#     actually require that package (see the note below).
# Going forward: only add a package to THIS list if the appliance is
# genuinely non-functional without it. Anything merely convenient or
# defensive belongs in the "optional packages" step further down, each
# installed in its OWN transaction so a failure there can never block
# the packages the appliance actually needs to run.
dnf install -y epel-release
dnf install -y python3 python3-pip certbot openssl

log "Verifying python3's built-in venv module is usable..."
if ! python3 -c "import venv" 2>/dev/null; then
  echo "ERROR: python3's built-in 'venv' module is not available. This is" >&2
  echo "unexpected on Rocky/RHEL -- check your python3 installation." >&2
  exit 1
fi

log "Verifying certbot installed correctly..."
if ! command -v certbot >/dev/null 2>&1; then
  echo "ERROR: certbot did not install correctly (not found on PATH)." >&2
  echo "Try running: dnf install -y epel-release certbot" >&2
  echo "and re-run this script." >&2
  exit 1
fi
log "  $(certbot --version 2>&1)"

log "Installing optional OS packages (each in its own transaction -- a failure here is logged as a warning and does NOT abort setup)..."

# dnf-utils (yum-utils) provides "needs-restarting", used only by the web
# UI's System Updates page to detect whether a reboot is required after
# applying updates. Not installing this just means that one detail is
# reported as "unknown" instead of yes/no -- everything else still works.
if ! dnf install -y dnf-utils; then
  log "  WARNING: could not install dnf-utils. The System Updates page's"
  log "  'reboot required?' detection will show 'unknown' instead of"
  log "  yes/no, but updates can still be checked/applied normally."
fi

# policycoreutils-python-utils provides 'semanage', which would only ever
# be needed here if SELinux (in enforcing mode) blocks the web UI from
# binding to its port. In practice this should not happen: port 8443 is
# already in SELinux's default http_port_t port list on RHEL/Rocky, and
# a plain systemd-launched binary like our gunicorn process normally
# runs under the very permissive unconfined_service_t domain, which does
# not require any port-specific policy changes to bind to a port that's
# already assigned an appropriate type. This appliance does not call
# semanage/restorecon anywhere -- this package is purely a "just in
# case" convenience for manual troubleshooting, so a failure to install
# it is always safe to ignore.
if ! dnf install -y policycoreutils-python-utils; then
  log "  WARNING: could not install policycoreutils-python-utils (this is"
  log "  OPTIONAL and not required for the appliance to run -- see the"
  log "  comment in this script / README for why). If the web UI later"
  log "  fails to bind to port 8443 under SELinux enforcing mode (check"
  log "  'journalctl -t setroubleshoot' or 'ausearch -m avc -ts recent'"
  log "  for AVC denials), install this package manually and run:"
  log "    semanage port -a -t http_port_t -p tcp 8443"
  log "  (use -m instead of -a if that port is already assigned a"
  log "  different type on your system)."
fi

log "Creating service account '$SERVICE_USER' (if needed)..."
id -u "$SERVICE_USER" &>/dev/null || useradd --system --home "$INSTALL_DIR" --shell /sbin/nologin "$SERVICE_USER"

log "Copying appliance source to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
find "$SRC_DIR" -mindepth 1 -maxdepth 1 \
  ! -name venv ! -name .git ! -name iso-build \
  -exec cp -r {} "$INSTALL_DIR"/ \;

log "Creating Python virtual environment and installing dependencies..."
if [ ! -d "$INSTALL_DIR/venv" ]; then
  python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip --quiet
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" --quiet

log "Setting permissions..."
chmod +x "$INSTALL_DIR"/bin/*.sh \
         "$INSTALL_DIR"/dns_dispatcher.py \
         "$INSTALL_DIR"/deploy_to_panos.py
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

log "Creating config/log/runtime directories with correct ownership..."
mkdir -p "$CONFIG_DIR" "$CONFIG_DIR/backups" "$CONFIG_DIR/webui-tls" "$RUN_DIR"
touch /var/log/acme-appliance.log
chown -R "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR" /var/log/acme-appliance.log "$RUN_DIR"
chmod 700 "$CONFIG_DIR"

log "Creating certbot's own config/work/logs directories (appliance-owned)..."
mkdir -p "$LE_CONFIG_DIR" "$LE_WORK_DIR" "$LE_LOGS_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$LE_CONFIG_DIR" "$LE_WORK_DIR" "$LE_LOGS_DIR"

if [ ! -f "$CONFIG_DIR/appliance.yaml" ]; then
  log "No appliance.yaml found -- installing the example template."
  cp "$INSTALL_DIR/config/appliance.yaml.example" "$CONFIG_DIR/appliance.yaml"
  chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR/appliance.yaml"
  chmod 600 "$CONFIG_DIR/appliance.yaml"
fi

log "Generating self-signed TLS certificate for the web UI (if not already present)..."
sudo -u "$SERVICE_USER" "$INSTALL_DIR/bin/generate-selfsigned-cert.sh" "$(hostname -f 2>/dev/null || hostname)"

log "Installing systemd units..."
cp "$INSTALL_DIR"/systemd/*.service "$INSTALL_DIR"/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload

log "Installing the sudoers rule for the System Updates feature..."
SUDOERS_SRC="$INSTALL_DIR/iso-build/sudoers.d/acme-appliance-updates"
SUDOERS_DST="/etc/sudoers.d/acme-appliance-updates"
if [ -f "$SUDOERS_SRC" ]; then
  install -m 0440 -o root -g root "$SUDOERS_SRC" "$SUDOERS_DST"
  if command -v visudo >/dev/null 2>&1; then
    if ! visudo -c -f "$SUDOERS_DST" >/dev/null; then
      echo "ERROR: the installed sudoers file at $SUDOERS_DST failed validation -- removing it." >&2
      rm -f "$SUDOERS_DST"
    else
      log "  sudoers rule installed and validated OK."
    fi
  fi
else
  log "  WARNING: $SUDOERS_SRC not found -- System Updates feature will not work."
fi

log "Opening firewalld port 8443/tcp for the web UI (if firewalld is active)..."
if systemctl is-active --quiet firewalld; then
  firewall-cmd --permanent --add-port=8443/tcp
  firewall-cmd --reload
else
  log "firewalld is not active -- skipping (open port 8443 manually if you enable a firewall later)."
fi

log "Enabling and starting services..."
systemctl enable --now acme-webui.service
systemctl enable --now acme-renew.timer

log ""
log "==================================================================="
log " Done. Web UI should now be reachable at:"
log "   https://$(hostname -I 2>/dev/null | awk '{print $1}'):8443/"
log ""
log " First visit will prompt you to create the admin account."
log " Visit Settings to set a real acme.email before your first renewal."
log "==================================================================="
log ""
log "Check status with:"
log "  systemctl status acme-webui.service acme-renew.timer"
log "  journalctl -u acme-webui.service -n 50 --no-pager"
