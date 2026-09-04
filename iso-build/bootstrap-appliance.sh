#!/usr/bin/env bash
#
# One-shot OS-level provisioning script: turns a freshly-installed,
# internet-connected Rocky Linux 9 (minimal) box into a box that HAS the
# appliance code and its prerequisites installed at /opt/acme-appliance --
# but does NOT itself decide which install profile (single-instance vs.
# msp-panos) to run. That decision, and everything profile-specific
# (systemd units, service account(s)/DynamicUser, config seeding, TLS
# cert, sudoers, firewalld) is handled by install.sh, which this script
# calls at the very end.
#
# It is idempotent -- safe to re-run if a step fails partway through.
#
# Usage (as root):
#   ./bootstrap-appliance.sh [/path/to/acme-appliance-source] [-- <install.sh args>]
#
# Examples:
#   ./bootstrap-appliance.sh
#     -> copies source, installs OS packages, then runs install.sh with
#        an interactive profile prompt.
#   ./bootstrap-appliance.sh /tmp/acme-appliance-src -- --profile=msp-panos --non-interactive
#     -> same, but installs (in the msp-panos profile) with no prompts.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Must be run as root (use sudo)." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Split args at "--": everything before is the optional source path,
# everything after is forwarded verbatim to install.sh.
SRC_DIR=""
INSTALL_ARGS=()
SEEN_DASHDASH=false
for arg in "$@"; do
  if [ "$arg" = "--" ]; then
    SEEN_DASHDASH=true
    continue
  fi
  if $SEEN_DASHDASH; then
    INSTALL_ARGS+=("$arg")
  elif [ -z "$SRC_DIR" ]; then
    SRC_DIR="$arg"
  fi
done
SRC_DIR="${SRC_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"

INSTALL_DIR="/opt/acme-appliance"

log() { echo "[bootstrap] $*"; }

if [ ! -f "$SRC_DIR/webui/app.py" ]; then
  echo "ERROR: '$SRC_DIR' does not look like an acme-appliance source tree (webui/app.py not found)." >&2
  echo "Pass the path to the appliance source as the first argument." >&2
  exit 1
fi

log "Installing critical OS packages (git, epel-release, python3, certbot, openssl)..."
# IMPORTANT: this dnf install list contains ONLY packages this appliance
# genuinely cannot function without, under EITHER install profile. Every
# package in a single dnf transaction must resolve successfully for ANY
# of them to install -- one broken/unavailable package aborts the WHOLE
# transaction, silently preventing everything else in the same command
# (including certbot) from installing too. This has bitten this script
# twice before:
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
# "git" is included here (not treated as optional) because a fresh
# Rocky Linux 9 "minimal" install does NOT ship it, and this repo's own
# README/quick-start instructs cloning this repository with git BEFORE
# this script ever runs -- so by the time bootstrap-appliance.sh itself
# executes, git already needed to exist. It's listed here anyway so that
# re-running this script (or invoking it from a context where git
# wasn't installed some other way first, e.g. a from-source copy that
# didn't use git clone) doesn't silently assume it's present.
# Going forward: only add a package to THIS list if the appliance is
# genuinely non-functional without it. Anything merely convenient or
# defensive belongs in the "optional packages" step further down, each
# installed in its OWN transaction so a failure there can never block
# the packages the appliance actually needs to run.
dnf install -y epel-release
dnf install -y git python3 python3-pip certbot openssl

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
# UI's System Updates page (single-instance profile only) to detect
# whether a reboot is required after applying updates. Not installing
# this just means that one detail is reported as "unknown" instead of
# yes/no -- everything else still works. Harmless to install even under
# the msp-panos profile, where System Updates is disabled entirely.
if ! dnf install -y dnf-utils; then
  log "  WARNING: could not install dnf-utils. The System Updates page's"
  log "  'reboot required?' detection will show 'unknown' instead of"
  log "  yes/no, but updates can still be checked/applied normally."
  log "  (Not applicable at all if you go on to install the msp-panos profile.)"
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
  log "  comment in this script / README for why). If a web UI instance"
  log "  later fails to bind to its port under SELinux enforcing mode"
  log "  (check 'journalctl -t setroubleshoot' or 'ausearch -m avc -ts"
  log "  recent' for AVC denials), install this package manually and run:"
  log "    semanage port -a -t http_port_t -p tcp <port>"
  log "  (use -m instead of -a if that port is already assigned a"
  log "  different type on your system)."
fi

log "Copying appliance source to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
find "$SRC_DIR" -mindepth 1 -maxdepth 1 \
  ! -name venv ! -name .git ! -name iso-build \
  -exec cp -r {} "$INSTALL_DIR"/ \;
# iso-build/ is deliberately excluded from the copy above (it is only
# ever needed on a BUILD machine, not the running appliance) EXCEPT for
# install.sh, lib/, systemd/, bin/, and deploy/ at the repo root (all
# copied by the find above already, since they live outside iso-build/),
# PLUS three things that live INSIDE iso-build/ but are still needed on
# the running appliance and must be copied back in explicitly:
#   - iso-build/'s own install.sh/lib/ re-copy below (install.sh runs
#     FROM $INSTALL_DIR from this point on)
#   - iso-build/sudoers.d/ -- BOTH install profile scripts
#     (lib/profile-single-instance.sh for acme-appliance-updates, and
#     lib/profile-msp-panos.sh for acme-msp-console) read their sudoers
#     rule source file from $INSTALL_DIR/iso-build/sudoers.d/<name> at
#     install time. Forgetting this copy means that lookup silently
#     fails (a plain `[[ -f "$sudoers_src" ]]` check, not a hard error)
#     and BOTH profiles' privileged-action sudoers rule never gets
#     installed at all -- the affected service account
#     (acme-appliance or acme-msp-console) ends up with ZERO sudo
#     grants, and every privileged action it tries then fails
#     identically with "sudo: a password is required". This is easy to
#     miss in installer scrollback (it only prints a WARNING, doesn't
#     abort) and only actually surfaces later when someone clicks a
#     privileged action (Apply updates/Reboot on single-instance;
#     restart web UI/tail log/provision/deprovision on msp-panos) in
#     whichever UI is affected.
cp -r "$SRC_DIR/install.sh" "$INSTALL_DIR/install.sh" 2>/dev/null || true
cp -r "$SRC_DIR/lib" "$INSTALL_DIR/lib" 2>/dev/null || true
mkdir -p "$INSTALL_DIR/iso-build"
cp -r "$SRC_DIR/iso-build/sudoers.d" "$INSTALL_DIR/iso-build/" 2>/dev/null || true
chmod +x "$INSTALL_DIR/install.sh" "$INSTALL_DIR"/lib/*.sh 2>/dev/null || true

log "Creating Python virtual environment (dependency installation happens in install.sh, since which requirements file(s) get installed depends on the chosen profile)..."
if [ ! -d "$INSTALL_DIR/venv" ]; then
  python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip --quiet

log "Setting execute permissions on scripts..."
chmod +x "$INSTALL_DIR"/bin/*.sh \
         "$INSTALL_DIR"/dns_dispatcher.py \
         "$INSTALL_DIR"/deploy_certificate.py

log ""
log "OS-level setup complete. Handing off to install.sh for profile-specific setup..."
log ""
exec "$INSTALL_DIR/install.sh" "${INSTALL_ARGS[@]}"
