#!/usr/bin/env bash
# Sourced by install.sh -- profile-specific steps for "msp-panos".
#
# CLEAN REDESIGN (see CHANGES.md for the full history): this profile
# used to run one dedicated Linux account + templated systemd instance
# PER CUSTOMER (acmecust-<slug>, acme-webui@<slug>.service), with
# customers logging into their OWN web UI instance directly. That model
# was built on a misunderstanding of the actual requirement -- customers
# were never meant to log in themselves at all; only MSP staff log in,
# via the MSP Console's own owner/staff permission model. Under this
# redesign there is exactly ONE running process for the entire fleet
# (the MSP Console itself, under the single "acme-msp-console" service
# account), and a "customer" is purely a DATA namespace: its own
# physically-separate appliance.yaml and certbot --config-dir (so
# Let's Encrypt rate-limit accounting stays correctly isolated per
# customer), all owned by that one shared account.
#
# This means, compared to the earlier per-customer-instance release,
# this profile no longer needs: templated systemd units, a dedicated
# Linux account per customer, nginx reverse-proxy routing per customer,
# or a sudoers rule granting the console any privileged escalation at
# all -- there is no useradd/userdel/per-customer-systemctl happening
# anywhere anymore, so there is nothing for the console to need root
# access to. See systemd/msp-console/acme-msp-console.service's own
# comments for the resulting (genuine, not just cosmetic) sandboxing
# improvement this allows.
#
# Deliberately does NOT install requirements-iis.txt (pywinrm) at all --
# on this profile, deploy_providers/__init__.py's ACME_APPLIANCE_PROFILE
# filtering already makes iis unavailable regardless, but skipping the
# dependency entirely means there is no WinRM client library, and
# therefore no WinRM credential-handling code path, present on an MSP
# fleet host in the first place -- defense in depth, not just a
# feature flag.
#
# Also deliberately does NOT install the System Updates sudoers rule --
# that feature operates on the shared HOST (dnf update -y, reboot) and
# has no safe per-tenant model, so it stays disabled at the application
# layer under this profile regardless (see webui/app.py's
# SYSTEM_UPDATES_ENABLED).

run_profile_install() {
    local appliance_dir="$1"

    echo "==> [msp-panos] Skipping IIS/WinRM dependency (not used by this profile)."

    echo "==> [msp-panos] Preparing shared parent directories (per-customer subdirectories are created and owned by bin/msp-provision-customer.sh)..."
    install -d -m 0755 /etc/acme-appliance/customers
    install -d -m 0755 /etc/acme-appliance/offboarded
    install -d -m 0755 /var/log/acme-appliance/customers
    install -d -m 0755 /var/lib/acme-appliance/customers
    install -d -m 0755 /run/acme-appliance

    _install_msp_console "$appliance_dir"

    echo ""
    echo "==================================================================="
    echo " Done. No customers are provisioned yet."
    echo ""
    echo " Manage the fleet from the MSP Console dashboard:"
    echo "     https://$(hostname -I 2>/dev/null | awk '{print $1}'):9443/"
    echo " First visit will prompt you to create the initial owner account."
    echo ""
    echo " Onboard your first customer from the console, or from the CLI:"
    echo "     sudo $appliance_dir/bin/msp-provision-customer.sh <customer-slug>"
    echo ""
    echo " Note: the System Updates feature (OS package checks/updates,"
    echo " reboot) is NOT available on this profile -- it operates on the"
    echo " shared host and has no safe per-tenant model. Manage OS updates"
    echo " for this host directly (dnf update -y) outside of the MSP Console."
    echo "==================================================================="
}

_install_msp_console() {
    local appliance_dir="$1"
    local console_dir="/etc/acme-appliance/msp-console"
    local service_user="acme-msp-console"

    echo "==> [msp-panos] Setting up the MSP Console..."

    echo "  Creating dedicated service account '$service_user'..."
    # This is now the ONLY Linux account this entire profile ever
    # creates -- there is no longer a per-customer account, so this
    # useradd call happens exactly ONCE, at install time, not
    # repeatedly at runtime every time a customer is provisioned. That
    # distinction matters: a one-time, install-time useradd is not
    # subject to the same runtime lock-contention/stale-lock concerns
    # that repeated, on-demand account creation was (see CHANGES.md's
    # earlier entries for that whole saga) -- there is simply no
    # equivalent risk left once account creation isn't happening as
    # part of routine customer onboarding anymore.
    id -u "$service_user" &>/dev/null || useradd --system --home "$appliance_dir/msp_console" --shell /sbin/nologin "$service_user"

    echo "  Creating console config/log directories with correct ownership..."
    install -d -m 0700 -o "$service_user" -g "$service_user" "$console_dir" "$console_dir/tls"
    install -d -m 0755 /var/log/acme-appliance
    touch /var/log/acme-appliance/msp-console.log
    chown "$service_user:$service_user" /var/log/acme-appliance/msp-console.log
    chmod 600 /var/log/acme-appliance/msp-console.log

    echo "  Ensuring this account owns the shared customer/fleet directories..."
    chown "$service_user:$service_user" /etc/acme-appliance/customers /etc/acme-appliance/offboarded \
        /var/log/acme-appliance/customers /var/lib/acme-appliance/customers /run/acme-appliance

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

    echo "  Installing the MSP Console systemd units..."
    install -d -m 0755 /etc/systemd/system
    install -m 0644 "$appliance_dir/systemd/msp-console/acme-msp-console.service" /etc/systemd/system/
    install -m 0644 "$appliance_dir/systemd/msp-console/acme-msp-renewal.service" /etc/systemd/system/
    install -m 0644 "$appliance_dir/systemd/msp-console/acme-msp-renewal.timer" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now acme-msp-console.service
    systemctl enable --now acme-msp-renewal.timer

    if systemctl is-active --quiet firewalld; then
        echo "  Opening firewalld port 9443/tcp for the MSP Console..."
        firewall-cmd --permanent --add-port=9443/tcp
        firewall-cmd --reload
    fi
}
