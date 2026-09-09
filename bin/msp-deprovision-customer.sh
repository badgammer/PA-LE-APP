#!/usr/bin/env bash
#
# Deprovisions a customer instance -- stops and disables its systemd
# units, ARCHIVES (never deletes outright) its config/cert directory to
# a dated tarball so an accidental offboarding is recoverable and
# certificate private keys don't linger unencrypted on disk indefinitely
# after a customer leaves, then removes this customer's dedicated system
# account.
#
# Usage:
#   msp-deprovision-customer.sh <customer-slug>            # interactive confirm
#   msp-deprovision-customer.sh <customer-slug> --yes       # skip confirmation
#
# --yes is REQUIRED when this script is invoked non-interactively (no
# TTY) -- notably, this is how the MSP Console dashboard calls it (see
# msp_console/actions.py deprovision_customer()) via a narrowly-scoped
# sudoers rule. The dashboard performs its OWN confirmation step in the
# web UI (a confirmation checkbox/dialog) before ever invoking this, so
# skipping the shell prompt here does not skip confirmation entirely --
# it just moves where that confirmation happens for the non-interactive
# caller.

set -euo pipefail

SLUG=""
SKIP_CONFIRM=false
for arg in "$@"; do
    case "$arg" in
        --yes) SKIP_CONFIRM=true ;;
        *) SLUG="$arg" ;;
    esac
done
if [[ -z "$SLUG" ]]; then
    echo "Usage: msp-deprovision-customer.sh <customer-slug> [--yes]" >&2
    exit 1
fi

# Same appliance-wide account-operations lock bin/msp-provision-customer.sh
# uses (and the SAME lock file), so a provision for one customer and a
# deprovision for another can never both call useradd/userdel at the same
# moment and collide on the shared, OS-wide /etc/passwd lock -- see the
# detailed comment in msp-provision-customer.sh for the full reasoning.
ACCOUNT_LOCK_FILE="/run/acme-appliance/msp-account-ops.lock"
mkdir -p "$(dirname "$ACCOUNT_LOCK_FILE")"
exec 9>"$ACCOUNT_LOCK_FILE"
if ! flock -w 30 9; then
    echo "ERROR: could not acquire the account-operations lock within 30s -- another" >&2
    echo "       provision/deprovision is apparently stuck. Check 'ps aux | grep userdel'" >&2
    echo "       and /run/acme-appliance/msp-account-ops.lock before retrying." >&2
    exit 1
fi

# /etc/passwd (etc.) locking in shadow-utils is NOT an in-kernel
# advisory lock tied to a live process -- it is a plain lockFILE
# (/etc/.pwd.lock, /etc/.grp.lock, /etc/.shadow.lock, /etc/.gshadow.lock,
# /etc/.subid.lock, /etc/.subgid.lock), created with O_CREAT|O_EXCL
# ("atomically create, fail if it already exists"). This means a
# process that dies WITHOUT running its own cleanup (killed by OOM, a
# hard VM reset mid-operation, etc.) leaves that lock file behind
# PERMANENTLY -- a plain reboot only clears processes/memory, it does
# NOT delete files under /etc, so the stale file continues blocking
# every future useradd/userdel call indefinitely, with the exact same
# "cannot lock /etc/passwd; try again later" error every time. Plain
# retries (below) can NEVER recover from this on their own -- there is
# nothing running that will ever finish and release a lock that isn't
# actually held by anyone anymore.
#
# _clear_stale_account_locks_if_safe() therefore checks, using ONLY
# /proc (no fuser/lsof dependency -- neither is guaranteed present on a
# minimal install, matching this codebase's existing dnf-utils/
# policycoreutils philosophy), whether any of these lock files exist
# AND are genuinely not held open by any live process. Only if BOTH are
# true does _retry_account_cmd remove the stale file itself before
# retrying -- this is exactly as safe as a human manually verifying "is
# anything actually using this?" before deleting it by hand, just
# automated.
_ACCOUNT_LOCK_CANDIDATES=(
    /etc/.pwd.lock /etc/.grp.lock /etc/.shadow.lock /etc/.gshadow.lock
    /etc/.subid.lock /etc/.subgid.lock
)

_file_is_open_by_any_process() {
    local target pid fd link
    target="$(readlink -f "$1" 2>/dev/null)" || return 1
    [[ -z "$target" ]] && return 1
    for pid in /proc/[0-9]*; do
        [[ -d "$pid/fd" ]] || continue
        for fd in "$pid"/fd/*; do
            link="$(readlink -f "$fd" 2>/dev/null)"
            [[ "$link" == "$target" ]] && return 0
        done
    done
    return 1
}

# Distinct from the stale-lock-FILE case above: this checks whether
# /etc itself is even WRITABLE at all right now. If it isn't (e.g. the
# root filesystem was remounted read-only by the kernel after detecting
# an inconsistency -- ext4's default errors=remount-ro behavior, most
# commonly triggered by a VM being hard-reset/power-cycled mid-write --
# see the README/CHANGES.md for the full explanation), then NEITHER
# userdel NOR our own attempt to remove a stale lock file can ever
# succeed, no matter how many times or how long we retry. Detecting
# this UP FRONT and failing immediately with a clear, correct diagnosis
# is far better than silently burning through all 5 retries (15+
# seconds) only to end with a generic "giving up" message that never
# mentions the actual, real cause -- which is an OS/filesystem-level
# problem entirely outside this script's or this appliance's control,
# not something any amount of application-level retrying can fix.
_etc_is_writable() {
    local testfile
    testfile="/etc/.acme-appliance-writetest.$$"
    if ( : > "$testfile" ) 2>/dev/null; then
        rm -f "$testfile"
        return 0
    fi
    return 1
}

_clear_stale_account_locks_if_safe() {
    local cleared=false
    local lockfile
    for lockfile in "${_ACCOUNT_LOCK_CANDIDATES[@]}"; do
        [[ -e "$lockfile" ]] || continue
        if _file_is_open_by_any_process "$lockfile"; then
            echo "  NOTE: $lockfile exists and IS currently open by a live process --" >&2
            echo "  leaving it alone (this looks like genuine, active contention, not a stale lock)." >&2
            continue
        fi
        echo "  $lockfile exists but is NOT held open by any running process -- this is a" >&2
        echo "  stale lock file (most likely left behind by an account-tool invocation that" >&2
        echo "  was killed abnormally at some point -- note that rebooting the HOST does NOT" >&2
        echo "  clean these up, since they are plain files under /etc, not process-held" >&2
        echo "  kernel locks). Removing it automatically, exactly as a human would after" >&2
        echo "  manually confirming the same thing with 'fuser'/'lsof'/ps." >&2
        if ! rm -f "$lockfile" 2>/tmp/.acme-lockrm-err.$$; then
            if grep -qi "read-only file system" "/tmp/.acme-lockrm-err.$$" 2>/dev/null; then
                rm -f "/tmp/.acme-lockrm-err.$$" 2>/dev/null
                echo "" >&2
                echo "############################################################" >&2
                echo "FATAL: could not remove $lockfile -- the filesystem itself is" >&2
                echo "READ-ONLY. This is an OS-level problem, not an application bug:" >&2
                echo "no amount of retrying will ever fix this. The most common cause" >&2
                echo "is the kernel automatically remounting the root filesystem" >&2
                echo "read-only after detecting an inconsistency (ext4's default" >&2
                echo "errors=remount-ro behavior) -- frequently triggered by a VM" >&2
                echo "being hard-reset or power-cycled while something was mid-write." >&2
                echo "" >&2
                echo "On the appliance host, diagnose and fix this DIRECTLY (not" >&2
                echo "through this script or the MSP Console) before retrying:" >&2
                echo "  mount | grep ' / '" >&2
                echo "  dmesg | grep -iE 'ext4|xfs|error|remount|read-only' | head -40" >&2
                echo "  sudo mount -o remount,rw /" >&2
                echo "If the remount above fails or reverts immediately, the" >&2
                echo "filesystem likely has real corruption from an earlier abrupt" >&2
                echo "shutdown and needs an OFFLINE fsck (boot to rescue mode; you" >&2
                echo "generally cannot fsck a mounted root filesystem) before this" >&2
                echo "host can provision or deprovision ANY customer again." >&2
                echo "############################################################" >&2
                exit 1
            fi
            rm -f "/tmp/.acme-lockrm-err.$$" 2>/dev/null
        else
            cleared=true
        fi
    done
    $cleared
}

_retry_account_cmd() {
    local attempt=1
    local max_attempts=5
    local delay=1
    if ! _etc_is_writable; then
        echo "" >&2
        echo "############################################################" >&2
        echo "FATAL: /etc is not writable on this host RIGHT NOW -- userdel" >&2
        echo "cannot possibly succeed, and retrying will not help. This" >&2
        echo "usually means the root filesystem has been remounted" >&2
        echo "read-only by the kernel (see dmesg for the actual cause)." >&2
        echo "Fix this at the OS level first:" >&2
        echo "  mount | grep ' / '" >&2
        echo "  dmesg | grep -iE 'ext4|xfs|error|remount|read-only' | head -40" >&2
        echo "  sudo mount -o remount,rw /" >&2
        echo "############################################################" >&2
        return 1
    fi
    while true; do
        if "$@"; then
            return 0
        fi
        local rc=$?
        if [[ $attempt -ge $max_attempts ]]; then
            echo "ERROR: '$*' still failing after $max_attempts attempts -- giving up." >&2
            echo "ERROR: if this is STILL 'cannot lock /etc/passwd', check by hand:" >&2
            echo "  ps aux | grep -E 'useradd|userdel|passwd|chpasswd|vipw|vigr|usermod|groupadd|groupdel'" >&2
            echo "  ls -la /etc/.pwd.lock /etc/.grp.lock /etc/.shadow.lock /etc/.gshadow.lock" >&2
            echo "  mount | grep ' / '   # confirm the filesystem is actually writable" >&2
            return $rc
        fi
        echo "  (attempt $attempt/$max_attempts) '$*' failed -- likely transient /etc/passwd" >&2
        echo "  lock contention from something outside this appliance; checking for a stale" >&2
        echo "  lock file before retrying..." >&2
        _clear_stale_account_locks_if_safe || true
        echo "  retrying in ${delay}s..." >&2
        sleep "$delay"
        attempt=$((attempt + 1))
        delay=$((delay * 2))
    done
}

SERVICE_USER="acmecust-$SLUG"
CUSTOMER_ETC_DIR="/etc/acme-appliance/customers/$SLUG"
CUSTOMER_VAR_DIR="/var/lib/acme-appliance/customers/$SLUG"
CUSTOMER_LOG_DIR="/var/log/acme-appliance/customers/$SLUG"
CUSTOMER_LOG_FILE="/var/log/acme-appliance/customers/$SLUG.log"
ARCHIVE_DIR="/etc/acme-appliance/offboarded"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

if [[ ! -d "$CUSTOMER_ETC_DIR" ]]; then
    echo "ERROR: no such customer directory: $CUSTOMER_ETC_DIR" >&2
    exit 1
fi

if ! $SKIP_CONFIRM; then
    read -r -p "This will STOP service for '$SLUG', archive its config/certs, and remove its system account. Continue? [y/N] " confirm
    [[ "$confirm" == "y" || "$confirm" == "Y" ]] || { echo "Aborted."; exit 1; }
fi

echo "Stopping and disabling systemd instances for '$SLUG'..."
systemctl disable --now "acme-webui@${SLUG}.service" 2>/dev/null || true
systemctl disable --now "acme-renew@${SLUG}.timer" 2>/dev/null || true

install -d -m 0700 "$ARCHIVE_DIR"
ARCHIVE_PATH="$ARCHIVE_DIR/${SLUG}-${STAMP}.tar.gz"
echo "Archiving config, certs, and state (this may include private keys -- archive is 0600, root-owned)..."
STAGING_DIR="$(mktemp -d)"
trap 'rm -rf "$STAGING_DIR"' EXIT
mkdir -p "$STAGING_DIR/etc" "$STAGING_DIR/var-lib"
cp -a "$CUSTOMER_ETC_DIR" "$STAGING_DIR/etc/$SLUG"
[[ -d "$CUSTOMER_VAR_DIR" ]] && cp -a "$CUSTOMER_VAR_DIR" "$STAGING_DIR/var-lib/$SLUG"
tar -czf "$ARCHIVE_PATH" -C "$STAGING_DIR" .
chmod 0600 "$ARCHIVE_PATH"
echo "Archived to $ARCHIVE_PATH"

rm -rf "$CUSTOMER_ETC_DIR" "$CUSTOMER_VAR_DIR" "$CUSTOMER_LOG_DIR" "$CUSTOMER_LOG_FILE"
rm -rf "/run/acme-appliance/$SLUG"

if id -u "$SERVICE_USER" &>/dev/null; then
    echo "Removing system account '$SERVICE_USER'..."
    # (not "2>/dev/null" here, unlike the original -- that would also
    # silently swallow _retry_account_cmd's own progress messages on
    # stderr; "|| true" alone still preserves this call's original
    # best-effort/non-fatal behavior on final failure)
    _retry_account_cmd userdel "$SERVICE_USER" || true
fi

echo ""
echo "'$SLUG' deprovisioned. Remaining manual steps:"
echo "  1. Remove the customer's nginx/Caddy server block and reload."
echo "  2. If any PAN-OS firewalls or DNS provider accounts were dedicated"
echo "     to this customer (not shared), rotate/revoke those credentials"
echo "     independently -- this script only touches the appliance's own"
echo "     config and system account, not any third-party credential lifecycle."
