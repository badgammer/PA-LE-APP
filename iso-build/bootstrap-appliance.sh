#!/usr/bin/env bash
#
# One-shot provisioning script: turns a freshly-installed, internet-
# connected Rocky Linux 9 (minimal) box into a fully running ACME/
# GlobalProtect appliance -- no ISO building required.
#
# This is the script that both:
#   - the kickstart file (ks.cfg) calls from its %post section when you
#     build a fully unattended install ISO (see build-iso.sh), and
#   - you can run directly, by hand, against any existing Rocky 9 box
#     (VM, bare metal, whatever) to get the same end result without ever
#     touching an ISO.
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

log() { echo "[bootstrap] $*"; }

if [ ! -f "$SRC_DIR/webui/app.py" ]; then
  echo "ERROR: '$SRC_DIR' does not look like an acme-appliance source tree (webui/app.py not found)." >&2
  echo "Pass the path to the appliance source as the first argument." >&2
  exit 1
fi

log "Installing OS packages (epel-release, python3, certbot, openssl)..."
dnf install -y epel-release
dnf install -y python3 python3-pip python3-venv certbot openssl policycoreutils-python-utils

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
mkdir -p "$CONFIG_DIR" "$CONFIG_DIR/backups" "$CONFIG_DIR/webui-tls" /var/run/acme-appliance
touch /var/log/acme-appliance.log
chown -R "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR" /var/log/acme-appliance.log /var/run/acme-appliance
chmod 700 "$CONFIG_DIR"

if [ ! -f "$CONFIG_DIR/appliance.yaml" ]; then
  log "No appliance.yaml found -- installing the example template (edit or use the web UI to fill it in)."
  cp "$INSTALL_DIR/config/appliance.yaml.example" "$CONFIG_DIR/appliance.yaml"
  chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR/appliance.yaml"
  chmod 600 "$CONFIG_DIR/appliance.yaml"
fi

log "Generating self-signed TLS certificate for the web UI (if not already present)..."
sudo -u "$SERVICE_USER" "$INSTALL_DIR/bin/generate-selfsigned-cert.sh" "$(hostname -f 2>/dev/null || hostname)"

log "Installing systemd units..."
cp "$INSTALL_DIR"/systemd/acme-renew.service /etc/systemd/system/
cp "$INSTALL_DIR"/systemd/acme-renew.timer /etc/systemd/system/
cp "$INSTALL_DIR"/systemd/acme-webui.service /etc/systemd/system/
systemctl daemon-reload

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
log " Edit $CONFIG_DIR/appliance.yaml (or use the web UI) to configure"
log " your DNS providers, Palo Alto firewalls, and domains."
log "==================================================================="
log ""
log "Check status with:"
log "  systemctl status acme-webui.service acme-renew.timer"
log "  journalctl -u acme-webui.service -n 50 --no-pager"
