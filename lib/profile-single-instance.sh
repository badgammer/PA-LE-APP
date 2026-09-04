#!/usr/bin/env bash
# Sourced by install.sh -- profile-specific steps for "single-instance".
# This absorbs everything the original (pre-multi-target-installer)
# bootstrap-appliance.sh used to do after installing OS packages and
# copying source: service account, config seeding, TLS cert, sudoers,
# firewalld, static systemd units, enable/start. Nothing here is new
# behavior -- it is the appliance's original, single-tenant setup,
# simply relocated so install.sh can offer it as one of two profiles.

run_profile_install() {
    local appliance_dir="$1"
    local service_user="acme-appliance"
    local config_dir="/etc/acme-appliance"
    local run_dir="/var/run/acme-appliance"
    local le_config_dir="$config_dir/letsencrypt"
    local le_work_dir="/var/lib/acme-appliance/letsencrypt"
    local le_logs_dir="/var/log/acme-appliance/letsencrypt"

    echo "==> [single-instance] Installing IIS/WinRM dependency (pywinrm)..."
    "$appliance_dir/venv/bin/pip" install --quiet -r "$appliance_dir/requirements-iis.txt"

    echo "==> [single-instance] Creating service account '$service_user' (if needed)..."
    id -u "$service_user" &>/dev/null || useradd --system --home "$appliance_dir" --shell /sbin/nologin "$service_user"
    chown -R "$service_user:$service_user" "$appliance_dir"

    echo "==> [single-instance] Creating config/log/runtime directories with correct ownership..."
    mkdir -p "$config_dir" "$config_dir/backups" "$config_dir/webui-tls" "$run_dir"
    touch /var/log/acme-appliance.log
    chown -R "$service_user:$service_user" "$config_dir" /var/log/acme-appliance.log "$run_dir"
    chmod 700 "$config_dir"

    echo "==> [single-instance] Creating certbot's own config/work/logs directories (appliance-owned)..."
    mkdir -p "$le_config_dir" "$le_work_dir" "$le_logs_dir"
    chown -R "$service_user:$service_user" "$le_config_dir" "$le_work_dir" "$le_logs_dir"

    if [[ ! -f "$config_dir/appliance.yaml" ]]; then
        echo "==> [single-instance] No appliance.yaml found -- installing the example template."
        cp "$appliance_dir/config/appliance.yaml.example" "$config_dir/appliance.yaml"
        chown "$service_user:$service_user" "$config_dir/appliance.yaml"
        chmod 600 "$config_dir/appliance.yaml"
    fi

    echo "==> [single-instance] Generating self-signed TLS certificate for the web UI (if not already present)..."
    sudo -u "$service_user" "$appliance_dir/bin/generate-selfsigned-cert.sh" "$(hostname -f 2>/dev/null || hostname)"

    echo "==> [single-instance] Installing static systemd units..."
    cp "$appliance_dir"/systemd/*.service "$appliance_dir"/systemd/*.timer /etc/systemd/system/
    systemctl daemon-reload

    echo "==> [single-instance] Installing the sudoers rule for the System Updates feature..."
    local sudoers_src="$appliance_dir/iso-build/sudoers.d/acme-appliance-updates"
    local sudoers_dst="/etc/sudoers.d/acme-appliance-updates"
    if [[ -f "$sudoers_src" ]]; then
        install -m 0440 -o root -g root "$sudoers_src" "$sudoers_dst"
        if command -v visudo >/dev/null 2>&1; then
            if ! visudo -c -f "$sudoers_dst" >/dev/null; then
                echo "ERROR: the installed sudoers file at $sudoers_dst failed validation -- removing it." >&2
                rm -f "$sudoers_dst"
            else
                echo "    sudoers rule installed and validated OK."
            fi
        fi
    else
        echo "    WARNING: $sudoers_src not found -- System Updates feature will not work."
    fi

    echo "==> [single-instance] Opening firewalld port 8443/tcp for the web UI (if firewalld is active)..."
    if systemctl is-active --quiet firewalld; then
        firewall-cmd --permanent --add-port=8443/tcp
        firewall-cmd --reload
    else
        echo "    firewalld is not active -- skipping (open port 8443 manually if you enable a firewall later)."
    fi

    echo "==> [single-instance] Enabling and starting services..."
    systemctl enable --now acme-webui.service
    systemctl enable --now acme-renew.timer

    echo ""
    echo "==================================================================="
    echo " Done. Web UI should now be reachable at:"
    echo "   https://$(hostname -I 2>/dev/null | awk '{print $1}'):8443/"
    echo ""
    echo " First visit will prompt you to create the admin account."
    echo " Visit Settings to set a real acme.email before your first renewal."
    echo " Both PAN-OS and Windows/IIS deploy targets are available in this profile."
    echo "==================================================================="
    echo ""
    echo "Check status with:"
    echo "  systemctl status acme-webui.service acme-renew.timer"
    echo "  journalctl -u acme-webui.service -n 50 --no-pager"
}
