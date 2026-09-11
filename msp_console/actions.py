"""
Fleet-wide customer lifecycle and renewal actions for the MSP Console.

CLEAN REDESIGN NOTE (see CHANGES.md for the full history): an earlier
release of this profile ran one dedicated Linux account + systemd
instance PER CUSTOMER, which meant every action here had to escalate
via `sudo` into a narrowly-scoped sudoers rule (see the removed
iso-build/sudoers.d/acme-msp-console) to perform useradd/userdel/
systemctl operations against that separate identity. Under this
redesign there is exactly ONE process for the entire fleet, running as
this SAME account (acme-msp-console) that every customer's directories
are already owned by -- so there is NOTHING here that needs root, and
this module makes ZERO `sudo` calls. Every function still:
  1. Re-validates its slug argument with the exact same regex/length
     rule as bin/msp-provision-customer.sh (defense in depth -- callers
     in app.py are also expected to have already checked permissions
     and slug validity via msp_console.auth / msp_console.fleet before
     ever reaching here, but this module never trusts that alone).
  2. Invokes subprocess with an explicit argv LIST, never shell=True --
     so even if a slug somehow contained shell metacharacters, there is
     no shell present to interpret them.
"""
import logging
import os
import shlex
import subprocess

from fleet import is_valid_slug, renew_lock_path, renewal_in_progress, redeploy_lock_path, redeploy_in_progress

log = logging.getLogger("msp_console.actions")

APPLIANCE_DIR = os.environ.get("ACME_APPLIANCE_DIR", "/opt/acme-appliance")


class ActionError(Exception):
    """Raised when an action could not be completed. Message is always
    safe to show to the requesting admin (never includes secrets)."""


def _require_valid_slug(slug: str) -> None:
    if not is_valid_slug(slug):
        raise ActionError(f"'{slug}' is not a valid customer slug.")


def _run(argv: list, timeout: int = 60) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ActionError(f"Command timed out after {timeout}s: {' '.join(argv)}") from exc
    except FileNotFoundError as exc:
        raise ActionError(f"Command not found: {exc}") from exc
    if result.returncode != 0:
        raise ActionError(
            (result.stderr or result.stdout or f"command exited {result.returncode}").strip()
        )
    return result.stdout


def provision_customer(slug: str) -> str:
    """
    Delegates directly to bin/msp-provision-customer.sh, unchanged --
    that script already does its own slug validation, directory
    creation, and config seeding. This is deliberately synchronous/
    blocking (provisioning is a handful of fast filesystem operations --
    mkdir, cp -- typically well under a second, with no account or
    systemd unit creation involved at all anymore), so the admin gets an
    immediate success/failure result rather than needing to poll.
    """
    _require_valid_slug(slug)
    script = os.path.join(APPLIANCE_DIR, "bin", "msp-provision-customer.sh")
    return _run([script, slug], timeout=60)


def deprovision_customer(slug: str) -> str:
    """
    Delegates to bin/msp-deprovision-customer.sh with --yes, which
    SKIPS that script's interactive confirmation prompt -- required
    since a call from a web request has no TTY to prompt on at all; the
    confirmation step instead happens in the web UI itself (a "type the
    customer slug to confirm" field before this function is ever
    called).
    """
    _require_valid_slug(slug)
    script = os.path.join(APPLIANCE_DIR, "bin", "msp-deprovision-customer.sh")
    return _run([script, slug, "--yes"], timeout=60)


def _customer_env(slug: str) -> dict:
    """
    The exact same set of ACME_APPLIANCE_* environment variables
    bin/msp-renew-all-customers.sh sets before invoking bin/acme-renew.sh
    for a given customer -- factored out here so per-DOMAIN actions
    (trigger_renew_domain, trigger_redeploy_domain) can invoke
    bin/acme-renew.sh / bin/redeploy-cert.sh directly with the same
    per-customer isolation (own appliance.yaml, own certbot
    --config-dir/--work-dir/--logs-dir, own log file), without going
    through the "loop over every customer" wrapper script at all.
    """
    return {
        "ACME_APPLIANCE_CONFIG": f"/etc/acme-appliance/customers/{slug}/appliance.yaml",
        "ACME_APPLIANCE_LOG": f"/var/log/acme-appliance/customers/{slug}.log",
        "ACME_APPLIANCE_LE_CONFIG_DIR": f"/etc/acme-appliance/customers/{slug}/letsencrypt",
        "ACME_APPLIANCE_LE_WORK_DIR": f"/var/lib/acme-appliance/customers/{slug}/letsencrypt",
        "ACME_APPLIANCE_LE_LOGS_DIR": f"/var/log/acme-appliance/customers/{slug}/letsencrypt",
    }


def _run_background_with_lock(lock_path: str, argv: list, env_overrides: dict) -> None:
    """
    Fire-and-forget: writes `lock_path` immediately (so
    renewal_in_progress()/redeploy_in_progress() reflect "busy" right
    away), then spawns `argv` as a detached background process that
    removes the lock file itself once it exits, regardless of exit
    code. This does NOT block the calling web request -- certbot + DNS
    propagation waits can take minutes, far longer than is reasonable
    to hold a web request/gunicorn worker open for.

    Unlike an earlier release of this profile (which started a
    per-customer SYSTEMD UNIT via sudo, since each customer had its own
    unit to start), this now runs as a plain background subprocess of
    the console's OWN account -- there is no separate systemd unit or
    account to start something on behalf of anymore, so this uses the
    exact same Popen-with-a-lock-file pattern webui/app.py's
    single-instance profile already uses for its own "Renew now"/
    "Redeploy" buttons, just parameterized per customer (and,
    optionally, per domain).
    """
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    env = dict(os.environ)
    env.update(env_overrides)
    quoted_cmd = " ".join(shlex.quote(a) for a in argv)
    with open(lock_path, "w") as f:
        f.write(str(os.getpid()))
    try:
        subprocess.Popen(
            ["/bin/bash", "-c", f"{quoted_cmd}; rm -f {shlex.quote(lock_path)}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, env=env,
        )
    except Exception:
        # Starting the background process itself failed (e.g. bash not
        # found) -- clean up the lock file we just wrote so a retry
        # isn't blocked by "already in progress" forever.
        try:
            os.remove(lock_path)
        except OSError:
            pass
        raise


def trigger_renew_customer(slug: str) -> str:
    """
    Starts a renewal check for EVERY domain belonging to this ONE
    customer, in the background (via
    bin/msp-renew-all-customers.sh <slug>, which -- despite the
    script's name -- accepts an optional single slug to process only
    that customer, see that script's own docstring).
    """
    _require_valid_slug(slug)
    if renewal_in_progress(slug):
        raise ActionError(f"A renewal is already in progress for '{slug}'.")
    script = os.path.join(APPLIANCE_DIR, "bin", "msp-renew-all-customers.sh")
    _run_background_with_lock(renew_lock_path(slug), [script, slug], {})
    return f"Renewal started for all of '{slug}''s domains in the background."


def trigger_renew_domain(slug: str, domain: str, force: bool = False) -> str:
    """
    Starts a renewal check for just ONE domain belonging to one
    customer, via bin/acme-renew.sh directly (unchanged) with that
    customer's own environment variables applied -- exactly mirroring
    webui/app.py's single-instance domain_renew() route, just scoped to
    one customer's own config/certbot directories instead of the fixed,
    single set of paths that route uses.
    """
    _require_valid_slug(slug)
    if renewal_in_progress(slug, domain) or redeploy_in_progress(slug, domain):
        raise ActionError(f"A renewal or redeploy is already in progress for '{domain}' (or for all of '{slug}''s domains).")
    script = os.path.join(APPLIANCE_DIR, "bin", "acme-renew.sh")
    argv = [script, domain] + (["--force"] if force else [])
    _run_background_with_lock(renew_lock_path(slug, domain), argv, _customer_env(slug))
    suffix = " (forcing renewal outside the normal 30-day window)" if force else ""
    return f"Renewal started for '{domain}' in the background{suffix}."


def trigger_redeploy_domain(slug: str, domain: str) -> str:
    """
    Re-imports an ALREADY-issued certificate to its configured deploy
    target(s) without requesting a new one from Let's Encrypt at all --
    via bin/redeploy-cert.sh directly (unchanged), scoped to this one
    customer's own config/certbot directories. Mirrors webui/app.py's
    single-instance domain_redeploy() route.
    """
    _require_valid_slug(slug)
    if renewal_in_progress(slug, domain) or redeploy_in_progress(slug, domain):
        raise ActionError(f"A renewal or redeploy is already in progress for '{domain}' (or for all of '{slug}''s domains).")
    script = os.path.join(APPLIANCE_DIR, "bin", "redeploy-cert.sh")
    _run_background_with_lock(redeploy_lock_path(slug, domain), [script, domain], _customer_env(slug))
    return f"Redeploy started for '{domain}' in the background."


def tail_log(slug: str, lines: int = 200) -> str:
    """
    Reads the tail of one customer's own log file directly -- no
    privilege escalation needed, since that log file is owned by this
    SAME account (unlike an earlier release of this profile, where each
    customer's log was owned by a separate dedicated account and had to
    be read via a root-privileged helper script over sudo).
    """
    _require_valid_slug(slug)
    lines = max(1, min(int(lines), 2000))
    log_path = f"/var/log/acme-appliance/customers/{slug}.log"
    if not os.path.isfile(log_path):
        return "(no log file yet for this customer)"
    try:
        with open(log_path, "r", errors="replace") as f:
            all_lines = f.readlines()
        return "".join(all_lines[-lines:])
    except OSError as exc:
        raise ActionError(f"Could not read log file: {exc}") from exc
