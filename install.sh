#!/usr/bin/env bash
#
# Single entry point for configuring this appliance in EITHER supported
# profile from the SAME checked-out codebase -- no forked repo, no
# separate branch to keep in sync. Run this AFTER
# iso-build/bootstrap-appliance.sh (which installs OS packages and
# copies the appliance source to /opt/acme-appliance) -- or let
# bootstrap-appliance.sh call this automatically, which it does by
# default.
#
# The profile choice controls:
#   - which deploy provider types are importable (see
#     deploy_providers/__init__.py's ACME_APPLIANCE_PROFILE handling)
#   - whether pywinrm gets installed at all
#   - whether the System Updates feature (OS updates, reboot) is enabled
#     at all (see webui/app.py's SYSTEM_UPDATES_ENABLED) -- it is
#     unconditionally disabled under msp-panos, since it operates on the
#     shared HOST via a sudoers rule tied to a single fixed service
#     account, which has no safe per-tenant equivalent
#   - which systemd units get installed: static single-tenant units
#     (single-instance) vs. the MSP Console's own units (msp-panos) --
#     under msp-panos there is only ONE process for the entire fleet
#     (see systemd/msp-console/), never one unit/account per customer;
#     customers are purely DATA namespaces under that one account
#   - whether the MSP Console and its helper scripts (bin/msp-*.sh) get
#     staged and started
#
# Usage:
#   sudo ./install.sh                          # interactive prompt
#   sudo ./install.sh --profile=single-instance
#   sudo ./install.sh --profile=msp-panos
#   sudo ./install.sh --profile=msp-panos --non-interactive
#
# This script is idempotent -- safe to re-run (e.g. after a code update)
# for the SAME profile a host was already installed with. Switching an
# existing host from one profile to the other is NOT supported by this
# script (each profile's identity/directory model differs enough that a
# clean re-provision is the only supported path) -- refuses to proceed
# if /etc/acme-appliance/profile already names a DIFFERENT profile.

set -euo pipefail

APPLIANCE_DIR="${APPLIANCE_DIR:-/opt/acme-appliance}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILE=""
NONINTERACTIVE=false

usage() {
    cat <<'EOF'
Usage: install.sh [--profile=single-instance|msp-panos] [--non-interactive]

Profiles:
  single-instance   One tenant. Both PAN-OS and Windows/IIS (WinRM) deploy
                    target types are available. The System Updates
                    feature (OS package checks/updates, reboot) is
                    enabled. Static systemd units (acme-webui.service,
                    acme-renew.timer), running as the shared
                    "acme-appliance" service account -- exactly the
                    existing single-tenant setup this appliance has
                    always used.

  msp-panos         Multi-tenant MSP mode. ONE process (the MSP Console)
                    for the entire fleet, running as a single dedicated
                    "acme-msp-console" service account -- each customer
                    is a DATA namespace (its own appliance.yaml, own
                    certbot --config-dir for correctly isolated Let's
                    Encrypt rate-limit accounting), not a separate
                    account or systemd unit. Customers never log in
                    themselves; only MSP staff log into the console,
                    which has its own owner/staff permission model
                    scoping which customers each admin can read/write.
                    PAN-OS deploy targets ONLY -- pywinrm is never
                    installed and the iis deploy provider type is never
                    even importable on this profile. The System Updates
                    feature is disabled entirely (it operates on the
                    shared host, which has no safe per-tenant model).
                    See systemd/msp-console/*.service and
                    bin/msp-*.sh.
EOF
}

for arg in "$@"; do
    case "$arg" in
        --profile=*) PROFILE="${arg#*=}" ;;
        --non-interactive) NONINTERACTIVE=true ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $arg" >&2; usage; exit 1 ;;
    esac
done

if [[ -z "$PROFILE" ]]; then
    if $NONINTERACTIVE; then
        echo "ERROR: --non-interactive requires --profile=... to be specified." >&2
        exit 1
    fi
    echo "Which installation profile should this host run?"
    echo ""
    echo "  1) single-instance  -- one tenant, PAN-OS + Windows/IIS deploy targets"
    echo "  2) msp-panos        -- multi-tenant (one instance per customer), PAN-OS only"
    echo ""
    read -rp "Choose [1/2]: " choice
    case "$choice" in
        1) PROFILE="single-instance" ;;
        2) PROFILE="msp-panos" ;;
        *) echo "Invalid choice." >&2; exit 1 ;;
    esac
fi

case "$PROFILE" in
    single-instance|msp-panos) ;;
    *) echo "ERROR: unknown profile '$PROFILE' (expected single-instance or msp-panos)" >&2; exit 1 ;;
esac

if [[ ! -d "$APPLIANCE_DIR" ]]; then
    echo "ERROR: $APPLIANCE_DIR does not exist. Run iso-build/bootstrap-appliance.sh" \
         "(or otherwise deploy this repo's code) to $APPLIANCE_DIR before running install.sh." >&2
    exit 1
fi

PROFILE_MARKER="/etc/acme-appliance/profile"
if [[ -f "$PROFILE_MARKER" ]]; then
    EXISTING_PROFILE="$(cat "$PROFILE_MARKER")"
    if [[ "$EXISTING_PROFILE" != "$PROFILE" ]]; then
        echo "ERROR: this host was already installed with profile '$EXISTING_PROFILE'" >&2
        echo "       (see $PROFILE_MARKER). Switching an existing host between profiles" >&2
        echo "       is not supported by this script -- each profile's identity and" >&2
        echo "       directory model differs enough that a clean re-provision (fresh OS" >&2
        echo "       install, then bootstrap-appliance.sh + install.sh with the new" >&2
        echo "       profile) is the only supported path." >&2
        exit 1
    fi
fi

echo "==> Installing with profile: $PROFILE"

echo "==> Setting up core Python dependencies (shared by both profiles)..."
if [[ ! -d "$APPLIANCE_DIR/venv" ]]; then
    python3 -m venv "$APPLIANCE_DIR/venv"
fi
"$APPLIANCE_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APPLIANCE_DIR/venv/bin/pip" install --quiet -r "$APPLIANCE_DIR/requirements-core.txt"

install -d -m 0755 /etc/acme-appliance
echo "$PROFILE" > "$PROFILE_MARKER"
chmod 0644 "$PROFILE_MARKER"

# shellcheck source=/dev/null
source "$SCRIPT_DIR/lib/profile-${PROFILE}.sh"
run_profile_install "$APPLIANCE_DIR"

echo ""
echo "==> Done. Profile '$PROFILE' installed."
