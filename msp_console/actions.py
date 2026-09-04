"""
Privileged fleet actions -- the ONLY place in msp_console/ that ever
shells out to sudo. Every function here:
  1. Re-validates its slug argument with the exact same regex/length
     rule as bin/msp-provision-customer.sh (defense in depth -- callers
     in app.py are also expected to have already checked permissions
     and slug validity via msp_console.auth / msp_console.fleet before
     ever reaching here, but this module never trusts that alone).
  2. Invokes subprocess with an explicit argv LIST, never shell=True --
     so even if a slug somehow contained shell metacharacters, there is
     no shell present to interpret them.
  3. Matches, argument-for-argument and in the same order, one of the
     narrowly-scoped commands whitelisted in
     iso-build/sudoers.d/acme-msp-console -- if you change a command
     here, the sudoers file MUST be updated to match exactly, or sudo
     will simply refuse the request (fails closed, not open).

This is the same privilege-separation shape webui/system_updates.py
already uses (unprivileged process -> narrow sudoers rule -> pre-defined
command) -- extended here to take one validated parameter (the customer
slug) per call, which the ORIGINAL system_updates.py units never needed
since they always operate on "the whole host" with zero parameters.
"""
import logging
import os
import subprocess

from fleet import is_valid_slug

log = logging.getLogger("msp_console.actions")

APPLIANCE_DIR = os.environ.get("ACME_APPLIANCE_DIR", "/opt/acme-appliance")


class ActionError(Exception):
    """Raised when a privileged action could not be completed. Message is
    always safe to show to the requesting admin (never includes secrets)."""


def _require_valid_slug(slug: str) -> None:
    if not is_valid_slug(slug):
        raise ActionError(f"'{slug}' is not a valid customer slug.")


def _run_sudo(argv: list, timeout: int = 60) -> str:
    try:
        result = subprocess.run(
            ["sudo", "-n"] + argv, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ActionError(f"Command timed out after {timeout}s: {' '.join(argv)}") from exc
    except FileNotFoundError as exc:
        raise ActionError(f"sudo is not available: {exc}") from exc
    if result.returncode != 0:
        raise ActionError(
            (result.stderr or result.stdout or f"command exited {result.returncode}").strip()
        )
    return result.stdout


def provision_customer(slug: str) -> str:
    """
    Delegates directly to bin/msp-provision-customer.sh, unchanged --
    that script already does its own slug validation, directory/account
    creation, config seeding, and systemd enable+start. This is
    deliberately synchronous/blocking (provisioning is a handful of fast
    operations -- useradd, mkdir, cp, systemctl enable --now -- typically
    well under a second), so the admin gets an immediate success/failure
    result rather than needing to poll.
    """
    _require_valid_slug(slug)
    script = os.path.join(APPLIANCE_DIR, "bin", "msp-provision-customer.sh")
    return _run_sudo([script, slug], timeout=60)


def deprovision_customer(slug: str) -> str:
    """
    Delegates to bin/msp-deprovision-customer.sh with --yes, which
    SKIPS that script's interactive confirmation prompt -- required here
    since a sudo call from a web request has no TTY to prompt on at all;
    the confirmation step instead happens in the web UI itself (a
    confirmation checkbox/dialog before this function is ever called).
    """
    _require_valid_slug(slug)
    script = os.path.join(APPLIANCE_DIR, "bin", "msp-deprovision-customer.sh")
    return _run_sudo([script, slug, "--yes"], timeout=60)


def trigger_renew(slug: str) -> str:
    """
    Fire-and-forget: starts this customer's renewal check unit WITHOUT
    blocking on it (certbot + DNS propagation waits can take minutes,
    far longer than is reasonable to hold a web request/gunicorn worker
    open for). Use fleet.renew_timer_state()/customer_status() and the
    customer's own log (see tail_log()) to observe progress afterward.
    """
    _require_valid_slug(slug)
    return _run_sudo(
        ["systemctl", "start", "--no-block", f"acme-renew@{slug}.service"], timeout=15
    )


def restart_webui(slug: str) -> str:
    """Restarts a hung/misbehaving customer's web UI instance."""
    _require_valid_slug(slug)
    return _run_sudo(["systemctl", "restart", f"acme-webui@{slug}.service"], timeout=30)


def tail_log(slug: str, lines: int = 200) -> str:
    """
    Reads the tail of one customer's log file AS ROOT via a small
    dedicated helper script (bin/msp-tail-log.sh) -- needed because that
    log file is owned 0600 by the customer's OWN dedicated system
    account (acmecust-<slug>), which the console's unprivileged account
    has no read access to by design (the same isolation that keeps one
    customer's secrets safe from another also, correctly, keeps them
    safe from the console's own service account -- root-via-sudo is the
    one identity allowed to cross that boundary, exactly like every
    other privileged action here).
    """
    _require_valid_slug(slug)
    lines = max(1, min(int(lines), 2000))
    script = os.path.join(APPLIANCE_DIR, "bin", "msp-tail-log.sh")
    return _run_sudo([script, slug, str(lines)], timeout=15)
