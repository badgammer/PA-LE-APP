#!/usr/bin/env bash
# Sourced by install.sh -- profile-specific steps for "msp-panos".
# Deliberately does NOT install requirements-iis.txt (pywinrm) at all --
# on this profile, deploy_providers/__init__.py's ACME_APPLIANCE_PROFILE
# filtering already makes iis unavailable regardless, but skipping the
# dependency entirely means there is no WinRM client library, and
# therefore no WinRM credential-handling code path, present on an MSP
# fleet host in the first place -- defense in depth, not just a
# feature flag.
#
# Also deliberately does NOT install the System Updates sudoers rule,
# create a shared service account, generate a single TLS cert, or open
# firewalld for a fixed port -- none of those single-instance concepts
# apply here (see webui/app.py's SYSTEM_UPDATES_ENABLED and the identity
# model in systemd/msp/acme-webui@.service).

run_profile_install() {
    local appliance_dir="$1"

    echo "==> [msp-panos] Skipping IIS/WinRM dependency (not used by this profile)."

    echo "==> [msp-panos] Installing templated (multi-tenant) systemd units..."
    install -d -m 0755 /etc/systemd/system
    install -m 0644 "$appliance_dir/systemd/msp/acme-webui@.service" /etc/systemd/system/
    install -m 0644 "$appliance_dir/systemd/msp/acme-renew@.service" /etc/systemd/system/
    install -m 0644 "$appliance_dir/systemd/msp/acme-renew@.timer" /etc/systemd/system/
    systemctl daemon-reload
    # Deliberately nothing is enabled/started here -- an MSP host with
    # zero customers provisioned yet should have zero running instances.
    # Run bin/msp-provision-customer.sh <slug> to bring up the first one.

    echo "==> [msp-panos] Preparing shared parent directories (per-customer subdirectories are created and owned by bin/msp-provision-customer.sh)..."
    install -d -m 0755 /etc/acme-appliance/customers
    install -d -m 0755 /var/log/acme-appliance/customers
    install -d -m 0755 /var/lib/acme-appliance/customers
    install -d -m 0755 /run/acme-appliance

    _install_msp_console "$appliance_dir"

    if [[ -d /etc/nginx/conf.d ]]; then
        echo "==> [msp-panos] nginx detected -- reverse-proxy template available at:"
        echo "    $appliance_dir/deploy/nginx/acme-appliance-msp.conf.template"
    else
        echo "==> [msp-panos] nginx not detected on this host -- install it separately"
        echo "    before provisioning customers; each customer instance binds a unix"
        echo "    socket only, it has no directly-reachable TCP listener by design."
    fi

    echo ""
    echo "==================================================================="
    echo " Done. No customers are provisioned yet."
    echo ""
    echo " Manage the fleet from the MSP Console dashboard:"
    echo "     https://$(hostname -I 2>/dev/null | awk '{print $1}'):9443/"
    echo " First visit will prompt you to create the initial owner account."
    echo ""
    echo " Or from the CLI:"
    echo "     sudo $appliance_dir/bin/msp-provision-customer.sh <customer-slug>"
    echo "     sudo $appliance_dir/bin/msp-fleet-status.sh"
    echo ""
    echo " Note: the System Updates feature (OS package checks/updates,"
    echo " reboot) is NOT available on this profile -- it operates on the"
    echo " shared host and has no safe per-tenant model. Manage OS updates"
    echo " for this host directly (dnf update -y) outside of any customer's"
    echo " web UI or the MSP Console."
    echo "==================================================================="
}

_install_msp_console() {
    local appliance_dir="$1"
    local console_dir="/etc/acme-appliance/msp-console"
    local service_user="acme-msp-console"

    echo "==> [msp-panos] Setting up the MSP Console dashboard..."

    echo "  Creating dedicated service account '$service_user'..."
    id -u "$service_user" &>/dev/null || useradd --system --home "$appliance_dir/msp_console" --shell /sbin/nologin "$service_user"

    echo "  Creating console config/log directories with correct ownership..."
    install -d -m 0700 -o "$service_user" -g "$service_user" "$console_dir" "$console_dir/tls"
    install -d -m 0755 /var/log/acme-appliance
    touch /var/log/acme-appliance/msp-console.log
    chown "$service_user:$service_user" /var/log/acme-appliance/msp-console.log
    chmod 600 /var/log/acme-appliance/msp-console.log

    if [[ ! -f "$console_dir/tls/console.crt" ]]; then
        echo "  Generating self-signed TLS certificate for the console (if not already present)..."
        local cn
        cn="$(hostname -f 2>/dev/null || hostname)"
        openssl req -x509 -nodes -days 825 -newkey rsa:2048 \
            -keyout "$console_dir/tls/console.key" -out "$console_dir/tls/console.crt" \
            -subj "/CN=${cn}" -addext "subjectAltName=DNS:${cn}"
        chown "$service_user:$service_user" "$console_dir/tls/console.key" "$console_dir/tls/console.crt"
        chmod 600 "$console_dir/tls/console.key"
        chmod 644 "$console_dir/tls/console.crt"
    fi

    echo "  Ensuring appliance code ownership allows the console to read its own files..."
    chown -R "$service_user:$service_user" "$appliance_dir/msp_console"

    echo "  Installing the sudoers rule for the MSP Console's privileged fleet actions..."
    local sudoers_src="$appliance_dir/iso-build/sudoers.d/acme-msp-console"
    local sudoers_dst="/etc/sudoers.d/acme-msp-console"
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
        echo "    WARNING: $sudoers_src not found -- MSP Console will not be able to perform any privileged fleet action."
    fi

    echo "  Installing and starting the MSP Console systemd unit..."
    install -m 0644 "$appliance_dir/systemd/msp-console/acme-msp-console.service" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now acme-msp-console.service

    if systemctl is-active --quiet firewalld; then
        echo "  Opening firewalld port 9443/tcp for the MSP Console..."
        firewall-cmd --permanent --add-port=9443/tcp
        firewall-cmd --reload
    fi
}
