"""
"Check for updates" / "Apply updates" / "Reboot" support for the web UI.

Security model (read this before changing anything here):

  1. The web UI process itself (acme-webui.service) runs as an
     unprivileged service account and NEVER executes dnf, or any other
     system-mutating command, directly. It only ever asks systemd (via
     `systemctl start <unit>`) to start one of a small number of
     pre-defined, root-owned, single-purpose systemd units that do the
     actual work (see systemd/acme-appliance-check-updates.service,
     acme-appliance-updates.service, acme-appliance-reboot.service).
  2. The unprivileged account is allowed to start ONLY those specific
     units via a narrowly-scoped sudoers rule (see
     iso-build/sudoers.d/acme-appliance-updates) -- nothing else.
  3. Before the web UI will even attempt step 1 for "Apply updates" or
     "Reboot" (NOT for the read-only "Check for updates"), it requires
     the requester to separately authenticate with a real Linux
     sudo-capable ("wheel" group by default) account's username and
     password via PAM. This is a step-up authentication check in
     addition to the normal web UI login -- knowing the web UI password
     alone is not sufficient to apply updates or reboot the box.
  4. That password is used only in-memory, for the single PAM
     authentication call, and is never logged, written to disk, or
     otherwise persisted.
  5. Failed step-up auth attempts are rate-limited independently of (and
     BEFORE reaching) the real PAM/system authentication stack, both to
     protect real Linux accounts from being locked out or brute-forced
     through this web form, and to reduce load on the system auth stack.
"""

import fcntl
import grp
import logging
import os
import shlex
import subprocess
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s system_updates %(levelname)s %(message)s",
    filename=os.environ.get("ACME_APPLIANCE_LOG", "/var/log/acme-appliance.log"),
)
log = logging.getLogger("system_updates")

RUN_DIR = os.environ.get("ACME_APPLIANCE_RUN_DIR", "/var/run/acme-appliance")
SUDO_GROUP = os.environ.get("ACME_APPLIANCE_SUDO_GROUP", "wheel")

CHECK_UNIT = "acme-appliance-check-updates.service"
UPDATE_UNIT = "acme-appliance-updates.service"
REBOOT_UNIT = "acme-appliance-reboot.service"

_FAILED_STEPUP_ATTEMPTS = {}
MAX_STEPUP_ATTEMPTS = 5
STEPUP_LOCKOUT_SECONDS = 300


# ------------------------------------------------------- PAM step-up auth

class StepUpAuthError(Exception):
    """Raised when the Linux sudo-user step-up check fails, with a
    message safe to show to the end user (never includes the password)."""


def _is_locked_out(username: str) -> bool:
    entry = _FAILED_STEPUP_ATTEMPTS.get(username)
    if not entry:
        return False
    count, locked_at = entry
    if count >= MAX_STEPUP_ATTEMPTS and (time.time() - locked_at) < STEPUP_LOCKOUT_SECONDS:
        return True
    if count >= MAX_STEPUP_ATTEMPTS and (time.time() - locked_at) >= STEPUP_LOCKOUT_SECONDS:
        _FAILED_STEPUP_ATTEMPTS.pop(username, None)
    return False


def _record_stepup_failure(username: str) -> None:
    count, _ = _FAILED_STEPUP_ATTEMPTS.get(username, (0, time.time()))
    _FAILED_STEPUP_ATTEMPTS[username] = (count + 1, time.time())


def _clear_stepup_failures(username: str) -> None:
    _FAILED_STEPUP_ATTEMPTS.pop(username, None)


def _is_in_sudo_group(username: str) -> bool:
    """
    Checks membership in the configured sudo-capable group (default
    "wheel", the standard Rocky/RHEL sudo group). Checks both
    supplementary group membership and the account's primary group.

    Known limitation: this does NOT parse /etc/sudoers or sudoers.d for
    users granted sudo rights individually (outside of group
    membership) -- if your organization grants sudo via direct sudoers
    entries rather than group membership, set ACME_APPLIANCE_SUDO_GROUP
    to a group all such admins are actually members of, or adjust this
    function.
    """
    import pwd
    try:
        group = grp.getgrnam(SUDO_GROUP)
    except KeyError:
        log.error("Configured sudo group '%s' does not exist on this system", SUDO_GROUP)
        return False
    if username in group.gr_mem:
        return True
    try:
        user_info = pwd.getpwnam(username)
        if user_info.pw_gid == group.gr_gid:
            return True
    except KeyError:
        return False
    return False


def verify_step_up_credentials(username: str, password: str) -> None:
    """
    Raises StepUpAuthError with a user-safe message if the credentials
    are invalid, the account is not sudo-capable, or the rate limit has
    been hit. Returns None (no exception) on success.
    """
    username = (username or "").strip()
    if not username or not password:
        raise StepUpAuthError("Username and password are required.")

    if _is_locked_out(username):
        raise StepUpAuthError(
            "Too many failed attempts for this account. Try again in a few minutes."
        )

    try:
        import pam
    except ImportError:
        log.error("python-pam is not installed -- cannot perform step-up authentication")
        raise StepUpAuthError(
            "Step-up authentication is not available: the 'python-pam' package is not "
            "installed on this appliance. Run 'pip install python-pam' in the appliance's "
            "venv and restart acme-webui.service."
        )

    authenticated = pam.pam().authenticate(username, password, service="login")
    # Drop our local reference to the password as soon as we're done with
    # it. This does not guarantee the underlying memory is scrubbed
    # (Python strings are immutable and may have been copied internally
    # by the pam binding), but avoids holding an unnecessary extra
    # reference alive for the rest of the request.
    password = None  # noqa: F841

    if not authenticated:
        _record_stepup_failure(username)
        log.warning("Step-up authentication FAILED for user '%s'", username)
        raise StepUpAuthError("Invalid username or password.")

    if not _is_in_sudo_group(username):
        _record_stepup_failure(username)
        log.warning(
            "Step-up authentication succeeded for '%s' but the account is not in "
            "the '%s' group -- refusing to proceed", username, SUDO_GROUP,
        )
        raise StepUpAuthError(
            f"'{username}' authenticated successfully but is not a member of the "
            f"'{SUDO_GROUP}' sudo group, so this action was refused."
        )

    _clear_stepup_failures(username)
    log.info("Step-up authentication succeeded for sudo-capable user '%s'", username)


# ------------------------------------------------------------ run helpers

def _run(cmd: list, timeout: int = 20):
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout}s: {' '.join(shlex.quote(c) for c in cmd)}"
    except FileNotFoundError as exc:
        return -1, "", str(exc)


def _read_file(path: str) -> str:
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except OSError:
        return ""


# ------------------------------------------------------------ status reads

def get_check_status() -> dict:
    """
    Reads the results of the last "Check for updates" run (written by
    bin/check-system-updates.sh via acme-appliance-check-updates.service).
    """
    checked_at = _read_file(os.path.join(RUN_DIR, "available-updates-checked-at.txt"))
    exit_code_raw = _read_file(os.path.join(RUN_DIR, "available-updates-exitcode.txt"))
    raw_output = _read_file(os.path.join(RUN_DIR, "available-updates.txt"))

    exit_code = int(exit_code_raw) if exit_code_raw.isdigit() else None
    packages = []
    if exit_code == 100:
        # dnf check-update output: lines of "<name>.<arch>  <version>  <repo>",
        # with a blank line separating the header/obsoletes section if present.
        for line in raw_output.splitlines():
            parts = line.split()
            if len(parts) == 3 and "." in parts[0]:
                packages.append({"package": parts[0], "version": parts[1], "repo": parts[2]})

    return {
        "checked_at": checked_at or None,
        "exit_code": exit_code,
        "updates_available": exit_code == 100,
        "packages": packages,
        "raw_output": raw_output,
        "check_error": exit_code not in (0, 100) if exit_code is not None else False,
    }


def get_last_apply_status() -> dict:
    """Reads the results of the last "Apply updates" run."""
    raw = _read_file(os.path.join(RUN_DIR, "last-update-status.txt"))
    status = {}
    for line in raw.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            status[k.strip()] = v.strip()
    return status


def _lock_path(name: str) -> str:
    os.makedirs(RUN_DIR, exist_ok=True)
    return os.path.join(RUN_DIR, f"{name}.lock")


def is_check_in_progress() -> bool:
    code, out, _ = _run(["systemctl", "is-active", CHECK_UNIT])
    return out.strip() == "active"


def is_update_in_progress() -> bool:
    if os.path.exists(_lock_path("system-update")):
        return True
    code, out, _ = _run(["systemctl", "is-active", UPDATE_UNIT])
    return out.strip() == "active"


# --------------------------------------------------------------- triggers

def trigger_check() -> tuple:
    """Starts the read-only update-check unit. No step-up auth required."""
    if is_check_in_progress():
        return False, "A check is already in progress."
    code, out, err = _run(["sudo", "-n", "systemctl", "start", CHECK_UNIT])
    if code != 0:
        log.error("Failed to start %s: %s", CHECK_UNIT, err or out)
        return False, f"Could not start the update check: {err or out or 'unknown error'}"
    return True, "Update check started."


def trigger_apply_update(username: str, password: str) -> tuple:
    """
    Starts the OS-update unit. Requires a valid, sudo-capable Linux
    account (verified via PAM) -- raises StepUpAuthError if that check
    fails, which the caller should catch and surface to the user.
    """
    verify_step_up_credentials(username, password)

    if is_update_in_progress():
        return False, "An update is already in progress."
    code, out, err = _run(["sudo", "-n", "systemctl", "start", UPDATE_UNIT])
    if code != 0:
        log.error("Failed to start %s: %s", UPDATE_UNIT, err or out)
        return False, f"Could not start the update: {err or out or 'unknown error'}"
    log.info("OS update started by web UI (authenticated as Linux user '%s')", username)
    return True, "Update started in the background. Follow progress on the Logs page."


def trigger_reboot(username: str, password: str) -> tuple:
    """Starts the reboot unit. Requires the same step-up auth as apply_update."""
    verify_step_up_credentials(username, password)

    code, out, err = _run(["sudo", "-n", "systemctl", "start", REBOOT_UNIT])
    if code != 0:
        log.error("Failed to start %s: %s", REBOOT_UNIT, err or out)
        return False, f"Could not start the reboot: {err or out or 'unknown error'}"
    log.info("Reboot triggered by web UI (authenticated as Linux user '%s')", username)
    return True, "Reboot initiated. The web UI will be unreachable until the host comes back up."
