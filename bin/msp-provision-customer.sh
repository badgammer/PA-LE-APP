#!/usr/bin/env bash
#
# Provisions a new customer instance for the msp-panos install profile:
# creates a dedicated system account for this customer (mirroring how
# bootstrap-appliance.sh / lib/profile-single-instance.sh creates the
# single "acme-appliance" account for the single-instance profile),
# creates and chowns this customer's config/cert/log/runtime directory
# tree, seeds a starter appliance.yaml, then enables + starts this
# customer's systemd instances (web UI + daily renewal timer) and prints
# the nginx upstream snippet needed to route to it.
#
# Usage:
#   msp-provision-customer.sh <customer-slug>
#
# <customer-slug> must be systemd-instance-safe: lowercase letters,
# digits, and hyphens only (it becomes the %i in every acme-*@<slug>
# systemd unit, a directory name under /etc/acme-appliance/customers/,
# and part of this customer's dedicated "acmecust-<slug>" system account
# name -- keep it reasonably short, Linux usernames are capped at 32
# characters and "acmecust-" already uses 9 of them).

set -euo pipefail

# --------------------------------------------------------------- locking
# useradd/userdel serialize on a shared, OS-wide /etc/passwd (etc.) lock
# file -- if this script's own useradd call collides with ANYTHING else
# on the box also mutating account databases at that exact moment
# (another invocation of this same script from a double-click on the MSP
# Console's "Provision customer" button, msp-deprovision-customer.sh's
# userdel running concurrently for a different customer, an unrelated
# admin session, config management, etc.), useradd fails immediately
# with "cannot lock /etc/passwd; try again later" -- a real, previously
# reported error, not hypothetical.
#
# Two independent defenses, both needed:
#   1. This script (and msp-deprovision-customer.sh) take a shared,
#      appliance-owned flock BEFORE touching any account database, so
#      this appliance's OWN provision/deprovision calls always run one
#      at a time and never collide with each other, regardless of
#      whether the caller was careful about that (the MSP Console's
#      "Provision customer" button has no client-side double-submit
#      guard as of this writing, so this flock is the real backstop).
#   2. _retry_account_cmd() below additionally retries the useradd
#      call itself with a short exponential backoff, since useradd's
#      own error message ("try again later") is quite literally asking
#      for a retry -- this also covers contention from something
#      OUTSIDE this appliance's own flock (e.g. a human running useradd
#      by hand on the box at the same moment).
ACCOUNT_LOCK_FILE="/run/acme-appliance/msp-account-ops.lock"
mkdir -p "$(dirname "$ACCOUNT_LOCK_FILE")"
exec 9>"$ACCOUNT_LOCK_FILE"
if ! flock -w 30 9; then
    echo "ERROR: could not acquire the account-operations lock within 30s -- another" >&2
    echo "       provision/deprovision is apparently stuck. Check 'ps aux | grep useradd'" >&2
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
# _stale_lock_files() therefore checks, using ONLY /proc (no fuser/lsof
# dependency -- neither is guaranteed present on a minimal install,
# matching this codebase's existing dnf-utils/policycoreutils
# philosophy), whether any of these lock files exist AND are genuinely
# not held open by any live process. Only if BOTH are true does
# _retry_account_cmd remove the stale file itself before retrying --
# this is exactly as safe as a human manually verifying "is anything
# actually using this?" before deleting it by hand, just automated.
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
        rm -f "$lockfile" && cleared=true
    done
    $cleared
}

_retry_account_cmd() {
    local attempt=1
    local max_attempts=5
    local delay=1
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

SLUG="${1:?Usage: msp-provision-customer.sh <customer-slug>}"
if [[ ! "$SLUG" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]]; then
    echo "ERROR: '$SLUG' is not a valid instance slug (lowercase letters/digits/hyphens only, no leading/trailing hyphen)." >&2
    exit 1
fi
if [[ ${#SLUG} -gt 23 ]]; then
    echo "ERROR: '$SLUG' is too long -- keep customer slugs to 23 characters or fewer" >&2
    echo "       (the 'acmecust-' account name prefix plus your slug must fit within" >&2
    echo "       Linux's 32-character username limit)." >&2
    exit 1
fi

APPLIANCE_DIR="/opt/acme-appliance"
SERVICE_USER="acmecust-$SLUG"
CUSTOMER_ETC_DIR="/etc/acme-appliance/customers/$SLUG"
CUSTOMER_VAR_DIR="/var/lib/acme-appliance/customers/$SLUG"
CUSTOMER_LOG_DIR="/var/log/acme-appliance/customers/$SLUG"
CUSTOMER_LOG_FILE="/var/log/acme-appliance/customers/$SLUG.log"
# Matches systemd/msp/acme-webui@.service's --access-logfile path EXACTLY
# (/var/log/acme-appliance/customers/%i-access.log). gunicorn runs as
# this customer's OWN dedicated account (acmecust-<slug>), and the
# PARENT directory (/var/log/acme-appliance/customers/) is shared and
# owned root:root mode 0755 -- so that account can write to an EXISTING
# file there (needs only write permission on the file itself), but
# cannot CREATE a brand-new one (that requires write permission on the
# directory, which only root has). Without pre-creating and chowning
# this file here -- exactly the same way CUSTOMER_LOG_FILE below already
# is -- gunicorn's first attempt to open it for its own access log fails
# with PermissionError, and the whole acme-webui@<slug>.service unit
# exits immediately with status=1/FAILURE on every single start,
# including every subsequent "Restart" (systemd will even hit its
# start-limit and refuse to keep retrying if this goes unnoticed for a
# few seconds). This was a real, reproduced bug -- not hypothetical.
CUSTOMER_ACCESS_LOG_FILE="/var/log/acme-appliance/customers/$SLUG-access.log"
CUSTOMER_RUN_DIR="/run/acme-appliance/$SLUG"

if [[ ! -f /etc/acme-appliance/profile ]] || [[ "$(cat /etc/acme-appliance/profile)" != "msp-panos" ]]; then
    echo "ERROR: this host was not installed with the msp-panos profile (see /etc/acme-appliance/profile)." >&2
    echo "       Run install.sh --profile=msp-panos first." >&2
    exit 1
fi

if [[ -d "$CUSTOMER_ETC_DIR" ]]; then
    echo "ERROR: $CUSTOMER_ETC_DIR already exists -- refusing to overwrite an existing customer." >&2
    exit 1
fi

echo "Provisioning customer instance '$SLUG'..."

echo "  Creating dedicated system account '$SERVICE_USER'..."
id -u "$SERVICE_USER" &>/dev/null || _retry_account_cmd useradd --system --home "$CUSTOMER_ETC_DIR" --shell /sbin/nologin "$SERVICE_USER"

echo "  Creating and chowning directory tree..."
install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$CUSTOMER_ETC_DIR" "$CUSTOMER_ETC_DIR/backups" "$CUSTOMER_ETC_DIR/letsencrypt"
install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$CUSTOMER_VAR_DIR" "$CUSTOMER_VAR_DIR/letsencrypt"
install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$CUSTOMER_LOG_DIR"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$CUSTOMER_RUN_DIR"
touch "$CUSTOMER_LOG_FILE"
chown "$SERVICE_USER:$SERVICE_USER" "$CUSTOMER_LOG_FILE"
chmod 600 "$CUSTOMER_LOG_FILE"
touch "$CUSTOMER_ACCESS_LOG_FILE"
chown "$SERVICE_USER:$SERVICE_USER" "$CUSTOMER_ACCESS_LOG_FILE"
chmod 600 "$CUSTOMER_ACCESS_LOG_FILE"

echo "  Seeding starter appliance.yaml..."
if [[ -f "$APPLIANCE_DIR/config/appliance.yaml.example" ]]; then
    cp "$APPLIANCE_DIR/config/appliance.yaml.example" "$CUSTOMER_ETC_DIR/appliance.yaml"
    # Trim the example down to an empty starting point -- nothing further
    # to strip re: deploy providers here specifically, since with
    # ACME_APPLIANCE_PROFILE=msp-panos, deploy_providers/__init__.py
    # never registers the iis type in the first place regardless of what
    # the example file happens to contain.
    python3 - "$CUSTOMER_ETC_DIR/appliance.yaml" <<'PYEOF'
import sys, yaml
path = sys.argv[1]
cfg = yaml.safe_load(open(path)) or {}
cfg["dns_providers"] = {}
cfg["deploy_providers"] = {}
cfg["domains"] = []
cfg.setdefault("acme", {})["email"] = ""
yaml.safe_dump(cfg, open(path, "w"), default_flow_style=False, sort_keys=False)
PYEOF
else
    cat > "$CUSTOMER_ETC_DIR/appliance.yaml" <<'YAMLEOF'
acme:
  email: ""
  server: "https://acme-v02.api.letsencrypt.org/directory"
dns_providers: {}
deploy_providers: {}
domains: []
YAMLEOF
fi
chown "$SERVICE_USER:$SERVICE_USER" "$CUSTOMER_ETC_DIR/appliance.yaml"
chmod 600 "$CUSTOMER_ETC_DIR/appliance.yaml"

echo "  Enabling systemd instances..."
systemctl daemon-reload
systemctl enable --now "acme-webui@${SLUG}.service"
systemctl enable --now "acme-renew@${SLUG}.timer"

echo ""
echo "Customer '$SLUG' provisioned. Next steps:"
echo "  1. Add this nginx server block (or the equivalent Caddy block) and reload:"
echo ""
echo "     server {"
echo "         listen 443 ssl http2;"
echo "         server_name ${SLUG}.appliance.yourmsp.com;"
echo "         # ... your ssl_certificate / ssl_certificate_key directives ..."
echo "         location / {"
echo "             proxy_pass http://unix:/run/acme-appliance/${SLUG}/webui.sock;"
echo "             proxy_set_header Host \$host;"
echo "             proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;"
echo "             proxy_set_header X-Forwarded-Proto \$scheme;"
echo "         }"
echo "     }"
echo ""
echo "     (See $APPLIANCE_DIR/deploy/nginx/acme-appliance-msp.conf.template"
echo "     for the full canonical version of this block.)"
echo ""
echo "  2. Browse to https://${SLUG}.appliance.yourmsp.com/setup to create the admin account."
echo "  3. Add DNS provider(s), PAN-OS deploy target(s), and domain(s) through the web UI."
echo "     (Windows/IIS is not offered as a deploy target type on this profile, and the"
echo "     System page is not shown -- both are single-instance-profile-only features.)"
