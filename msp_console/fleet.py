"""
Fleet enumeration and status reads for the MSP Console dashboard.

CLEAN REDESIGN NOTE (see CHANGES.md for the full history): an earlier
release of this profile checked a per-customer systemd unit's state
(acme-webui@<slug>.service) to report "is this customer's web UI up".
Under this redesign there IS no per-customer process or systemd unit
anymore -- every customer is purely a data namespace served by the ONE
MSP Console process -- so that check no longer applies (or means
anything): if the MSP Console itself is reachable at all, EVERY
customer's data is reachable through it. What actually varies per
customer now is: whether it has an issued certificate and how soon it
expires, whether a renewal is currently running in the background for
it, and how many domains/providers are configured -- all read directly
off disk, with zero dependency on any other process being up.
"""
import os
import re
import subprocess

CUSTOMERS_DIR = os.environ.get("ACME_MSP_CONSOLE_CUSTOMERS_DIR", "/etc/acme-appliance/customers")
RUN_DIR = os.environ.get("ACME_MSP_CONSOLE_RUN_DIR", "/run/acme-appliance")


def _safe_lock_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)

# Slugs are used as directory names, log file names, and lock file name
# fragments -- validated once here, then re-validated (defense in depth)
# by actions.py before ever being used to build a subprocess argv or
# filesystem path.
_SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def is_valid_slug(slug: str) -> bool:
    return bool(slug) and len(slug) <= 40 and bool(_SLUG_RE.match(slug))


def list_customer_slugs() -> list:
    if not os.path.isdir(CUSTOMERS_DIR):
        return []
    return sorted(
        name for name in os.listdir(CUSTOMERS_DIR)
        if os.path.isdir(os.path.join(CUSTOMERS_DIR, name)) and is_valid_slug(name)
    )


def renew_lock_path(slug: str, domain: str = None) -> str:
    """
    domain=None means "renew every domain for this customer" (the lock
    the daily timer / dashboard-level "renew all" action uses); a
    specific domain means "renew just this one domain for this
    customer" (the lock a single domain's own "Renew now" button uses).
    These are DELIBERATELY separate lock files -- renewing one domain
    should not be blocked by (or block) a customer-wide renewal run
    unless they happen to target the exact same domain.
    """
    if domain is None:
        return os.path.join(RUN_DIR, f"renew-{slug}-ALL.lock")
    return os.path.join(RUN_DIR, f"renew-{slug}-{_safe_lock_name(domain)}.lock")


def redeploy_lock_path(slug: str, domain: str) -> str:
    return os.path.join(RUN_DIR, f"redeploy-{slug}-{_safe_lock_name(domain)}.lock")


def renewal_in_progress(slug: str, domain: str = None) -> bool:
    """
    True if EITHER a customer-wide renewal is running for this
    customer, OR (when `domain` is given) that specific domain's own
    renewal is running -- mirroring the single-instance profile's
    _renewal_in_progress() semantics (see webui/app.py), where a
    "renew everything" run in progress also counts as "busy" for the
    purposes of any single domain's own renew/redeploy buttons.
    """
    if os.path.exists(renew_lock_path(slug, domain=None)):
        return True
    if domain is not None and os.path.exists(renew_lock_path(slug, domain)):
        return True
    return False


def redeploy_in_progress(slug: str, domain: str) -> bool:
    return os.path.exists(redeploy_lock_path(slug, domain))


def domain_busy(slug: str, domain: str) -> bool:
    return renewal_in_progress(slug, domain) or redeploy_in_progress(slug, domain)


def domain_count(slug: str) -> int:
    """
    Reads just the domain count out of a customer's appliance.yaml
    without going through the full config_store machinery (which would
    also perform a legacy-config migration write) -- this is a
    read-only, best-effort status check, so it deliberately tolerates a
    missing or malformed file by returning 0 rather than raising.
    """
    path = os.path.join(CUSTOMERS_DIR, slug, "appliance.yaml")
    if not os.path.isfile(path):
        return 0
    try:
        import yaml
        with open(path, "r") as f:
            cfg = yaml.safe_load(f) or {}
        return len(cfg.get("domains", []) or [])
    except (OSError, yaml.YAMLError):
        return 0


def _soonest_cert_expiry(slug: str):
    """
    Returns (expiry_string_or_None, epoch_or_None) for the
    soonest-expiring certificate under this customer's own certbot
    --config-dir live/ directory, reading cert.pem files directly via
    the local openssl binary.
    """
    live_dir = os.path.join(CUSTOMERS_DIR, slug, "letsencrypt", "live")
    if not os.path.isdir(live_dir):
        return None, None
    soonest_str, soonest_epoch = None, None
    for entry in sorted(os.listdir(live_dir)):
        cert_path = os.path.join(live_dir, entry, "cert.pem")
        if not os.path.isfile(cert_path):
            continue
        try:
            out = subprocess.run(
                ["openssl", "x509", "-enddate", "-noout", "-in", cert_path],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            end_str = out.replace("notAfter=", "").strip()
            epoch_out = subprocess.run(
                ["date", "-d", end_str, "+%s"], capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if not epoch_out.isdigit():
                continue
            epoch = int(epoch_out)
        except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
            continue
        if soonest_epoch is None or epoch < soonest_epoch:
            soonest_epoch, soonest_str = epoch, end_str
    return soonest_str, soonest_epoch


def customer_status(slug: str) -> dict:
    """
    Returns a single dashboard-ready status dict for one customer slug.
    Deliberately does not raise if the slug directory is missing/odd --
    returns a best-effort dict with a note explaining what's wrong,
    since this is read by dashboard code that should degrade gracefully
    rather than 500 on one bad entry blocking the whole fleet view.
    """
    import time

    expiry_str, expiry_epoch = _soonest_cert_expiry(slug)
    renewing = renewal_in_progress(slug)
    domains = domain_count(slug)

    notes = []
    if expiry_epoch is None:
        notes.append("no certs issued yet")
    else:
        now = int(time.time())
        if expiry_epoch < now:
            notes.append("EXPIRED")
        elif expiry_epoch - now < 7 * 86400:
            notes.append("expires < 7 days")
    if renewing:
        notes.append("renewal in progress")

    return {
        "slug": slug,
        "domain_count": domains,
        "cert_expiry": expiry_str,
        "renewing": renewing,
        "note": "; ".join(notes),
        "healthy": not notes or notes == ["renewal in progress"],
    }


def fleet_status(slugs: list = None) -> list:
    slugs = slugs if slugs is not None else list_customer_slugs()
    return [customer_status(s) for s in slugs]
