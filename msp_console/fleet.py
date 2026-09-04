"""
Fleet enumeration and status reads for the MSP Console dashboard.

Deliberately reads everything directly off disk (customer directories
under CUSTOMERS_DIR) and via systemctl is-active/is-enabled queries --
NEVER by calling into any customer's own web UI -- so this keeps working
correctly even if a given customer's Flask process is completely down,
and has zero dependency on that process's internals. This mirrors
exactly what bin/msp-fleet-status.sh already does for the CLI view; this
module is the shared logic the CLI script and the dashboard should both
eventually consolidate onto (the CLI script remains for scripting/cron
use where a web session isn't available or appropriate).
"""
import os
import re
import subprocess

CUSTOMERS_DIR = os.environ.get("ACME_MSP_CONSOLE_CUSTOMERS_DIR", "/etc/acme-appliance/customers")

# Same slug validation used by bin/msp-provision-customer.sh -- enforced
# here TOO (defense in depth) before any slug is used to build a
# subprocess argv or filesystem path, even though every caller is also
# expected to have validated it already.
_SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def is_valid_slug(slug: str) -> bool:
    return bool(slug) and len(slug) <= 23 and bool(_SLUG_RE.match(slug))


def list_customer_slugs() -> list:
    if not os.path.isdir(CUSTOMERS_DIR):
        return []
    return sorted(
        name for name in os.listdir(CUSTOMERS_DIR)
        if os.path.isdir(os.path.join(CUSTOMERS_DIR, name)) and is_valid_slug(name)
    )


def _systemctl_query(args: list, timeout: int = 10) -> str:
    try:
        result = subprocess.run(
            ["systemctl"] + args, capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def webui_state(slug: str) -> str:
    out = _systemctl_query(["is-active", f"acme-webui@{slug}.service"])
    return "up" if out == "active" else "down"


def renew_timer_state(slug: str) -> str:
    out = _systemctl_query(["is-enabled", f"acme-renew@{slug}.timer"])
    return "enabled" if out == "enabled" else "disabled"


def _soonest_cert_expiry(slug: str):
    """
    Returns (expiry_string_or_None, epoch_or_None) for the
    soonest-expiring certificate under this customer's live/ directory,
    reading cert.pem files directly via the local openssl binary --
    exactly the same approach bin/msp-fleet-status.sh uses, so results
    are consistent between the CLI and dashboard views.
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
            epoch = subprocess.run(
                ["date", "-d", end_str, "+%s"], capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if not epoch.isdigit():
                continue
            epoch = int(epoch)
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

    webui = webui_state(slug)
    renew = renew_timer_state(slug)
    expiry_str, expiry_epoch = _soonest_cert_expiry(slug)

    notes = []
    if webui == "down":
        notes.append("web UI down")
    if renew == "disabled":
        notes.append("renewal timer disabled")
    if expiry_epoch is None:
        notes.append("no certs issued yet")
    else:
        now = int(time.time())
        if expiry_epoch < now:
            notes.append("EXPIRED")
        elif expiry_epoch - now < 7 * 86400:
            notes.append("expires < 7 days")

    return {
        "slug": slug,
        "webui_state": webui,
        "renew_state": renew,
        "cert_expiry": expiry_str,
        "note": "; ".join(notes),
        "healthy": not notes,
    }


def fleet_status(slugs: list = None) -> list:
    slugs = slugs if slugs is not None else list_customer_slugs()
    return [customer_status(s) for s in slugs]
