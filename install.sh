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
# DEFAULT BEHAVIOR: with no flags at all, this installs the STANDALONE
# (single-instance) appliance -- no prompt, nothing to choose. The MSP
# (multi-tenant) build is only installed when you add the --msp modifier.
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
#   sudo ./install.sh                  # STANDALONE (single-instance) -- the default
#   sudo ./install.sh --msp            # MSP (multi-tenant, msp-panos) build
#
# Long forms (still accepted, e.g. for existing automation):
#   sudo ./install.sh --profile=single-instance
#   sudo ./install.sh --profile=msp-panos
#
# --non-interactive is still accepted but is now a no-op: this script
# never prompts anymore.
#
# This script is idempotent -- safe to re-run (e.g. after a code update).
# Re-running with NO profile flag on a host that was already installed
# reuses that host's existing profile (read from
# /etc/acme-appliance/profile), so updating an MSP host does not require
# remembering to pass --msp again. Switching an existing host from one
# profile to the other is NOT supported by this script (each profile's
# identity/directory model differs enough that a clean re-provision is
# the only supported path) -- it refuses to proceed if a flag explicitly
# names a DIFFERENT profile than /etc/acme-appliance/profile.

set -euo pipefail

APPLIANCE_DIR="${APPLIANCE_DIR:-/opt/acme-appliance}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILE=""
NONINTERACTIVE=false   # retained for compatibility only; nothing prompts anymore

usage() {
    cat <<'EOF'
Usage: install.sh [--msp] [--non-interactive]

With no flags, installs the STANDALONE (single-instance) appliance.

  (no flag)         STANDALONE / single-instance (default). One tenant.
                    Both PAN-OS and Windows/IIS (WinRM) deploy target
                    types are available. The System Updates feature
                    (OS package checks/updates, reboot) is enabled.
                    Static systemd units (acme-webui.service,
                    acme-renew.timer), running as the shared
                    "acme-appliance" service account.

  --msp             MSP / msp-panos build. Multi-tenant MSP mode. ONE
                    process (the MSP Console) for the entire fleet,
                    running as a single dedicated "acme-msp-console"
                    service account -- each customer is a DATA
                    namespace (its own appliance.yaml, own certbot
                    --config-dir for correctly isolated Let's Encrypt
                    rate-limit accounting), not a separate account or
                    systemd unit. Customers never log in themselves;
                    only MSP staff log into the console, which has its
                    own owner/staff permission model scoping which
                    customers each admin can read/write.
                    PAN-OS deploy targets ONLY -- pywinrm is never
                    installed and the iis deploy provider type is never
                    even importable on this profile. The System Updates
                    feature is disabled entirely (it operates on the
                    shared host, which has no safe per-tenant model).
                    See systemd/msp-console/*.service and
                    bin/msp-*.sh.

Other options:
  --profile=single-instance|msp-panos
                    Long form of the above (kept for existing automation).
  --non-interactive Accepted for compatibility; has no effect (this
                    script never prompts).
  -h, --help        Show this help.

Re-running on a host that is already installed (with no profile flag)
reuses that host's existing profile.
EOF
}

set_profile() {
    # Records the requested profile, rejecting contradictory flags such as
    # "--msp --profile=single-instance" rather than silently letting the
    # last one win.
    if [[ -n "$PROFILE" && "$PROFILE" != "$1" ]]; then
        echo "ERROR: conflicting profile flags ('$PROFILE' and '$1')." >&2
        usage >&2
        exit 1
    fi
    PROFILE="$1"
}

for arg in "$@"; do
    case "$arg" in
        --msp) set_profile "msp-panos" ;;
        --profile=*) set_profile "${arg#*=}" ;;
        --non-interactive) NONINTERACTIVE=true ;;  # no-op: kept for compatibility
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $arg" >&2; usage >&2; exit 1 ;;
    esac
done

PROFILE_MARKER="/etc/acme-appliance/profile"

# No explicit flag: reuse an already-installed host's profile if there is
# one (so re-running install.sh to update an MSP host does not silently try
# to flip it to the standalone default and trip the mismatch guard below);
# otherwise default to the STANDALONE build.
if [[ -z "$PROFILE" ]]; then
    if [[ -f "$PROFILE_MARKER" ]]; then
        PROFILE="$(cat "$PROFILE_MARKER")"
        echo "==> No profile flag given; reusing this host's existing profile '$PROFILE' (from $PROFILE_MARKER)."
    else
        PROFILE="single-instance"
    fi
fi

case "$PROFILE" in
    single-instance|msp-panos) ;;
    *) echo "ERROR: unknown profile '$PROFILE' (expected single-instance or msp-panos; or just use --msp)" >&2; exit 1 ;;
esac

if [[ ! -d "$APPLIANCE_DIR" ]]; then
    echo "ERROR: $APPLIANCE_DIR does not exist. Run iso-build/bootstrap-appliance.sh" \
         "(or otherwise deploy this repo's code) to $APPLIANCE_DIR before running install.sh." >&2
    exit 1
fi

if [[ -f "$PROFILE_MARKER" ]]; then
    EXISTING_PROFILE="$(cat "$PROFILE_MARKER")"
    if [[ "$EXISTING_PROFILE" != "$PROFILE" ]]; then
        echo "ERROR: this host was already installed with profile '$EXISTING_PROFILE'" >&2
        echo "       (see $PROFILE_MARKER). Switching an existing host between profiles" >&2
        echo "       is not supported by this script -- each profile's identity and" >&2
        echo "       directory model differs enough that a clean re-provision (fresh OS" >&2
        echo "       install, then bootstrap-appliance.sh [--msp]) is the only supported" >&2
        echo "       path." >&2
        exit 1
    fi
fi

if [[ "$PROFILE" == "msp-panos" ]]; then
    echo "==> Installing the MSP build (profile: msp-panos)"
else
    echo "==> Installing the STANDALONE build (profile: single-instance)"
fi

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
