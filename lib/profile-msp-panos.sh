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
    echo " Onboard the first one with:"
    echo "     sudo $appliance_dir/bin/msp-provision-customer.sh <customer-slug>"
    echo " Check fleet health at any time with:"
    echo "     sudo $appliance_dir/bin/msp-fleet-status.sh"
    echo ""
    echo " Note: the System Updates feature (OS package checks/updates,"
    echo " reboot) is NOT available on this profile -- it operates on the"
    echo " shared host and has no safe per-tenant model. Manage OS updates"
    echo " for this host directly (dnf update -y) outside of any customer's"
    echo " web UI."
    echo "==================================================================="
}
