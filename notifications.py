#!/usr/bin/env python3
"""
Email alerting for the ACME appliance (shared by BOTH install profiles).

Two delivery methods, chosen on the Notifications page:

  smtp    Plain SMTP: STARTTLS (587), implicit TLS (465) or none (25, for an
          internal relay), optional username/password.

  m365    Microsoft 365 / Exchange Online over OAuth 2.0 using Microsoft
          Graph "sendMail" with the app-only client-credentials flow
          (tenant id + client id + client secret). No user sign-in, no
          redirect URI, no token refresh to babysit. This is the method to
          use for Microsoft 365 because Exchange Online is retiring Basic
          auth for SMTP AUTH.

          NOTE: the Mail.Send *application* permission allows sending as
          ANY mailbox in the tenant unless it is scoped. Scope the app to
          the single alert mailbox (Exchange Online RBAC for Applications).
          The Notifications page explains how.

  inherit (MSP customers only) use the MSP-wide sender configured once by an
          owner in the MSP Console (see FLEET_PATH), so a Graph secret is not
          copied into every customer namespace.

Where settings live: a top-level `notifications:` block in the appliance's own
appliance.yaml (per customer under msp-panos). Secrets are stored there the
same way every other provider secret already is (file mode 0600).

Events (each can be switched on/off):
  cert_expiring    daily expiry scan found certificates close to expiry
  dns_failure      a DNS-01 challenge record could not be added/removed
  renewal_failed   a renewal run failed                    (CLI/hook)
  deploy_failed    deploying a certificate to a target failed   (CLI/hook)
  renewal_success  a certificate was issued/renewed        (CLI/hook, off by default)
  deploy_success   a certificate was deployed              (CLI/hook, off by default)

Entry points
  notify_event(...)   fire-and-forget; NEVER raises (alerting must not be able
                      to break a renewal). Rate-limited per event+domain.
  check_expiry(...)   scan a live/ directory and send ONE digest email.
  bin/acme-notify.py  CLI wrapper: test | event | check-expiry
"""
import argparse
import base64
import contextlib
import fcntl
import json
import logging
import os
import re
import smtplib
import socket
import ssl
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from urllib.parse import quote

import requests
import yaml

log = logging.getLogger("notifications")

DEFAULT_CONFIG_PATH = os.environ.get("ACME_APPLIANCE_CONFIG", "/etc/acme-appliance/appliance.yaml")
DEFAULT_LIVE_DIR = os.environ.get("ACME_APPLIANCE_LE_LIVE_DIR", "/etc/acme-appliance/letsencrypt/live")
CUSTOMERS_DIR = os.environ.get("ACME_APPLIANCE_CUSTOMERS_DIR", "/etc/acme-appliance/customers")
FLEET_PATH = os.environ.get("ACME_APPLIANCE_FLEET_NOTIFY", "/etc/acme-appliance/msp-console/notifications.yaml")

TIMEOUT = 20
STATE_KEEP_DAYS = 60

# name -> label, default-on, minimum hours between repeats of the same alert, and
# "wired": True once something in the appliance actually fires it. An event with wired=False
# is hidden on the Notifications page (and its saved value left alone) so the page never
# offers a switch that does nothing. All six are wired today:
#   cert_expiring ........ bin/acme-notify.py check-expiry (daily timer)
#   dns_failure .......... dns_dispatcher.py
#   renewal_failed ....... bin/acme-renew.sh (certbot failed for a domain)
#   renewal_success ...... deploy_certificate.py (certbot's --deploy-hook: runs only on issuance/renewal)
#   deploy_failed/success  deploy_certificate.py (also covers the web UI / CLI "Redeploy")
EVENTS = {
    "cert_expiring": {"label": "Certificate expiring soon (daily scan)", "default": True, "min_hours": 0, "wired": True},
    "dns_failure": {"label": "DNS challenge record could not be added or removed", "default": True, "min_hours": 6, "wired": True},
    "renewal_failed": {"label": "Certificate renewal failed", "default": True, "min_hours": 6, "wired": True},
    "deploy_failed": {"label": "Deploying a certificate to a target failed", "default": True, "min_hours": 6, "wired": True},
    "renewal_success": {"label": "Certificate issued / renewed successfully", "default": False, "min_hours": 0, "wired": True},
    "deploy_success": {"label": "Certificate deployed successfully", "default": False, "min_hours": 0, "wired": True},
}


def ui_events() -> dict:
    """Events shown on the Notifications page (only those something actually fires)."""
    return {k: v for k, v in EVENTS.items() if v["wired"]}


_EVENT_SUBJECT = {
    "cert_expiring": "Certificate expiring soon",
    "dns_failure": "DNS challenge failed",
    "renewal_failed": "Renewal FAILED",
    "deploy_failed": "Deployment FAILED",
    "renewal_success": "Certificate renewed",
    "deploy_success": "Certificate deployed",
    "test": "Test message",
}

EMAIL_RE = re.compile(r"^[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[^@\s,;<>\"']+$")


class NotificationError(Exception):
    """Raised when a message cannot be sent or settings are unusable."""


# --------------------------------------------------------------------------- defaults / form handling

def default_settings() -> dict:
    return {
        "enabled": False,
        "transport": "smtp",
        "from_address": "",
        "from_name": "ACME Appliance",
        "recipients": [],
        "subject_prefix": "[ACME]",
        "expiry_warn_days": 14,
        "events": {k: v["default"] for k, v in EVENTS.items()},
        "smtp": {"host": "", "port": 587, "security": "starttls", "username": "", "password": "", "verify_tls": True},
        "m365": {"tenant_id": "", "client_id": "", "client_secret": ""},
    }


def get_config(cfg: dict) -> dict:
    """The notifications block of a loaded appliance.yaml, merged over defaults."""
    return _merge(default_settings(), (cfg or {}).get("notifications") or {})


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def parse_recipients(text):
    """'a@x.com, b@y.com;c@z.com' (or a list) -> (valid list, invalid list). De-duplicated, order kept."""
    if isinstance(text, (list, tuple)):
        parts = [str(p) for p in text]
    else:
        parts = re.split(r"[,;\s]+", text or "")
    valid, invalid, seen = [], [], set()
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if not EMAIL_RE.match(p):
            invalid.append(p)
        elif p.lower() not in seen:
            seen.add(p.lower())
            valid.append(p)
    return valid, invalid


def _to_int(value, default, lo, hi):
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def settings_from_form(form, existing: dict, allow_inherit: bool = False) -> dict:
    """
    Build a notifications settings dict from a submitted form (anything with
    .get(), like Flask's request.form). A blank secret field keeps the
    stored value -- secrets are never sent back to the browser.
    """
    base = _merge(default_settings(), existing or {})
    s = _merge(default_settings(), {})  # fresh copy, then fill from the form + base
    s["enabled"] = form.get("enabled") == "on"

    transport = (form.get("transport") or base["transport"]).strip()
    valid_transports = ("smtp", "m365", "inherit") if allow_inherit else ("smtp", "m365")
    s["transport"] = transport if transport in valid_transports else "smtp"

    s["from_address"] = (form.get("from_address") or "").strip()
    s["from_name"] = (form.get("from_name") or "").strip()
    s["subject_prefix"] = (form.get("subject_prefix") or "").strip()
    valid, invalid = parse_recipients(form.get("recipients") or "")
    # Keep invalid tokens visible so validate() can report them instead of silently dropping.
    s["recipients"] = valid + invalid
    s["expiry_warn_days"] = _to_int(form.get("expiry_warn_days"), base["expiry_warn_days"], 1, 60)
    # Only events shown on the page come from the form; hidden (un-wired) ones keep their saved value.
    s["events"] = {k: (form.get(f"event_{k}") == "on") if EVENTS[k]["wired"] else bool(base["events"].get(k, EVENTS[k]["default"]))
                   for k in EVENTS}

    smtp_old, m365_old = base["smtp"], base["m365"]
    s["smtp"] = {
        "host": (form.get("smtp_host") or "").strip(),
        "port": _to_int(form.get("smtp_port"), smtp_old["port"], 1, 65535),
        "security": (form.get("smtp_security") or smtp_old["security"]).strip(),
        "username": (form.get("smtp_username") or "").strip(),
        "password": form.get("smtp_password") or smtp_old.get("password", ""),
        "verify_tls": form.get("smtp_verify_tls") == "on",
    }
    if s["smtp"]["security"] not in ("starttls", "ssl", "none"):
        s["smtp"]["security"] = "starttls"
    s["m365"] = {
        "tenant_id": (form.get("m365_tenant_id") or "").strip(),
        "client_id": (form.get("m365_client_id") or "").strip(),
        "client_secret": form.get("m365_client_secret") or m365_old.get("client_secret", ""),
    }
    return s


def validate(s: dict, sender_only: bool = False) -> list:
    """List of human-readable problems; empty means OK to save. Disabled configs may be incomplete."""
    errors = []
    if not sender_only:
        valid, invalid = parse_recipients(s.get("recipients") or [])
        if invalid:
            errors.append("Not valid email addresses: " + ", ".join(invalid))
        if s.get("enabled") and not valid:
            errors.append("Add at least one recipient before enabling notifications.")
    if not s.get("enabled") and not sender_only:
        return errors
    t = s.get("transport")
    if t == "smtp":
        smtp = s.get("smtp", {})
        if not smtp.get("host"):
            errors.append("SMTP server is required.")
        if not EMAIL_RE.match(s.get("from_address") or ""):
            errors.append("A valid From address is required.")
        if smtp.get("username") and not smtp.get("password"):
            errors.append("An SMTP username was given but no password.")
    elif t == "m365":
        m = s.get("m365", {})
        for key, label in (("tenant_id", "Tenant ID"), ("client_id", "Application (client) ID"),
                           ("client_secret", "Client secret")):
            if not m.get(key):
                errors.append(f"{label} is required for Microsoft 365.")
        if not EMAIL_RE.match(s.get("from_address") or ""):
            errors.append("The sender mailbox (From address) must be a valid address for Microsoft 365.")
    elif t != "inherit":
        errors.append(f"Unknown delivery method '{t}'.")
    return errors


# --------------------------------------------------------------------------- transports

def resolve_transport(s: dict, fleet_path: str = None) -> dict:
    """Return the settings that will actually be used to send (applies 'inherit')."""
    if s.get("transport") != "inherit":
        return s
    fleet_path = fleet_path or FLEET_PATH
    if not os.path.exists(fleet_path):
        raise NotificationError(
            "This customer is set to use the MSP-wide sender, but none has been configured yet. "
            "An owner must set it up under Notifications in the MSP Console.")
    try:
        with open(fleet_path, "r") as f:
            fleet = _merge(default_settings(), yaml.safe_load(f) or {})
    except (OSError, yaml.YAMLError) as exc:
        raise NotificationError(f"MSP-wide sender settings could not be read ({fleet_path}): {exc}") from exc
    problems = validate({**fleet, "enabled": True}, sender_only=True)
    if fleet.get("transport") == "inherit" or problems:
        raise NotificationError(
            "The MSP-wide sender is not configured yet. An owner must set it up under "
            "Notifications in the MSP Console. " + "; ".join(problems))
    merged = dict(s)
    for key in ("transport", "from_address", "from_name", "smtp", "m365"):
        merged[key] = fleet[key]
    return merged


def _clean_header(text: str) -> str:
    return re.sub(r"[\r\n]+", " ", text or "").strip()


def _build_message(s: dict, subject: str, body: str, recipients: list) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = _clean_header(subject)
    msg["From"] = formataddr((_clean_header(s.get("from_name") or ""), s["from_address"]))
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    return msg


def _smtp_connect(smtp: dict):
    host, port = smtp["host"], int(smtp.get("port") or 587)
    ctx = ssl.create_default_context()
    if not smtp.get("verify_tls", True):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    security = smtp.get("security", "starttls")
    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=ctx)
            server.ehlo()
        else:
            server = smtplib.SMTP(host, port, timeout=TIMEOUT)
            server.ehlo()
            if security == "starttls":
                server.starttls(context=ctx)
                server.ehlo()
        if smtp.get("username"):
            server.login(smtp["username"], smtp.get("password", ""))
        return server
    except smtplib.SMTPAuthenticationError as exc:
        raise NotificationError(f"SMTP login rejected ({exc.smtp_code}). Check the username/password "
                                "(and that this account may use SMTP AUTH).") from exc
    except (smtplib.SMTPException, ssl.SSLError, OSError) as exc:
        raise NotificationError(f"Could not connect to SMTP server {host}:{port} ({security}): {exc}") from exc


def _send_smtp(s: dict, subject, body, recipients) -> None:
    server = _smtp_connect(s["smtp"])
    try:
        server.send_message(_build_message(s, subject, body, recipients))
    except smtplib.SMTPRecipientsRefused as exc:
        raise NotificationError(f"SMTP server refused the recipient(s): {exc}") from exc
    except smtplib.SMTPException as exc:
        raise NotificationError(f"SMTP send failed: {exc}") from exc
    finally:
        with contextlib.suppress(Exception):
            server.quit()


def _graph_token(m: dict) -> str:
    url = f"https://login.microsoftonline.com/{quote(m['tenant_id'].strip(), safe='')}/oauth2/v2.0/token"
    try:
        r = requests.post(url, data={
            "grant_type": "client_credentials",
            "client_id": m["client_id"].strip(),
            "client_secret": m["client_secret"],
            "scope": "https://graph.microsoft.com/.default",
        }, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise NotificationError(f"Could not reach Microsoft sign-in: {exc}") from exc
    if not r.ok:
        try:
            err = r.json()
            detail = (err.get("error_description") or err.get("error") or r.text).splitlines()[0]
        except ValueError:
            detail = r.text[:200]
        raise NotificationError(f"Microsoft sign-in rejected the app credentials: {detail}")
    token = r.json().get("access_token")
    if not token:
        raise NotificationError("Microsoft sign-in returned no access token.")
    return token


def _token_roles(token: str):
    """App roles in the (unverified) access token, or None if it can't be read. Used only for friendlier errors."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("roles") or []
    except Exception:  # noqa: BLE001
        return None


def _m365_check_token(m: dict):
    """Sign in. Returns (token, roles-or-None). The roles list is advisory only: with Exchange RBAC for
    Applications (the scoped, recommended setup) the Mail.Send grant lives in Exchange, NOT in the token."""
    token = _graph_token(m)
    return token, _token_roles(token)


def _send_m365(s: dict, subject, body, recipients) -> None:
    token, _roles = _m365_check_token(s["m365"])
    sender = s["from_address"].strip()
    payload = {
        "message": {
            "subject": _clean_header(subject),
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": a}} for a in recipients],
        },
        "saveToSentItems": False,
    }
    url = f"https://graph.microsoft.com/v1.0/users/{quote(sender, safe='@')}/sendMail"
    try:
        r = requests.post(url, json=payload, timeout=TIMEOUT,
                          headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    except requests.RequestException as exc:
        raise NotificationError(f"Could not reach Microsoft Graph: {exc}") from exc
    if r.status_code == 202:
        return
    try:
        err = r.json().get("error", {})
        detail = f"{err.get('code', '')}: {err.get('message', '')}".strip(": ")
    except ValueError:
        detail = r.text[:200]
    hint = ""
    if r.status_code == 403:
        hint = (" -- the app is not allowed to send as this mailbox. Either grant Mail.Send (admin consent) or, "
                "if you use Exchange RBAC for Applications, make sure this mailbox is inside the scope.")
    elif r.status_code == 404:
        hint = f" -- mailbox '{sender}' was not found (check the sender address)."
    raise NotificationError(f"Microsoft Graph sendMail failed (HTTP {r.status_code}): {detail}{hint}")


def send_message(settings: dict, subject: str, body: str, recipients: list = None, fleet_path: str = None) -> None:
    """Send one message using the effective transport. Raises NotificationError."""
    eff = resolve_transport(settings, fleet_path)
    to, invalid = parse_recipients(recipients if recipients is not None else settings.get("recipients", []))
    if invalid:
        raise NotificationError("Invalid recipient address(es): " + ", ".join(invalid))
    if not to:
        raise NotificationError("No recipients configured.")
    problems = validate({**eff, "enabled": True}, sender_only=True)
    if problems:
        raise NotificationError("; ".join(problems))
    if eff["transport"] == "m365":
        _send_m365(eff, subject, body, to)
    else:
        _send_smtp(eff, subject, body, to)


def test_connection(settings: dict, fleet_path: str = None) -> str:
    """Check credentials/connectivity WITHOUT sending a message. Returns a message; raises NotificationError."""
    eff = resolve_transport(settings, fleet_path)
    problems = validate({**eff, "enabled": True}, sender_only=True)
    if problems:
        raise NotificationError("; ".join(problems))
    if eff["transport"] == "m365":
        _token, roles = _m365_check_token(eff["m365"])
        msg = "Microsoft 365 sign-in works. "
        if roles is not None and "Mail.Send" not in roles:
            msg += ("No tenant-wide Mail.Send grant was found in Entra ID. That is expected if you scoped the app "
                    "with Exchange RBAC for Applications; otherwise sending will fail. ")
        return msg + "Use 'Send test email' to confirm the sender mailbox can actually send."
    smtp = eff["smtp"]
    server = _smtp_connect(smtp)
    with contextlib.suppress(Exception):
        server.quit()
    auth = "authenticated" if smtp.get("username") else "no login"
    return f"Connected to {smtp['host']}:{smtp['port']} ({smtp['security']}, {auth})."


def _footer(label: str = "") -> str:
    where = f"{label} on " if label else ""
    return f"\n--\nSent by the ACME appliance ({where}{socket.gethostname()}) at {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}."


def send_test_email(settings: dict, label: str = "", fleet_path: str = None) -> str:
    to, _ = parse_recipients(settings.get("recipients", []))
    if not to:
        raise NotificationError("Add at least one recipient first.")
    prefix = settings.get("subject_prefix", "")
    subject = f"{prefix} {label + ': ' if label else ''}{_EVENT_SUBJECT['test']}".strip()
    body = ("This is a test message from the ACME certificate appliance. If you can read this, email "
            "alerting is working." + _footer(label))
    send_message(settings, subject, body, to, fleet_path)
    return "Test email sent to " + ", ".join(to) + "."


# --------------------------------------------------------------------------- MSP-wide sender (msp-panos)

def load_fleet_settings(path: str = None) -> dict:
    path = path or FLEET_PATH
    try:
        with open(path, "r") as f:
            return _merge(default_settings(), yaml.safe_load(f) or {})
    except (OSError, yaml.YAMLError):
        return default_settings()


def save_fleet_settings(settings: dict, path: str = None) -> None:
    """Atomic 0600 write of the MSP-wide sender settings (only transport/from/smtp/m365 are used)."""
    path = path or FLEET_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    keep = {k: settings[k] for k in ("transport", "from_address", "from_name", "smtp", "m365")}
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        yaml.safe_dump(keep, f, default_flow_style=False, sort_keys=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def fleet_sender_summary(path: str = None):
    """Short description of the MSP-wide sender, or None if it is not (fully) configured."""
    fleet = load_fleet_settings(path)
    if fleet.get("transport") == "inherit" or validate({**fleet, "enabled": True}, sender_only=True):
        return None
    if fleet["transport"] == "m365":
        return f"Microsoft 365 (OAuth) as {fleet['from_address']}"
    return f"SMTP via {fleet['smtp']['host']}:{fleet['smtp']['port']} as {fleet['from_address']}"


# --------------------------------------------------------------------------- reading config / state (CLI + hooks)

def _read_config(config_path: str) -> dict:
    try:
        with open(config_path, "r") as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise NotificationError(f"Cannot read {config_path}: {exc}") from exc


def label_for(config_path: str, label: str = None) -> str:
    if label:
        return label
    env = os.environ.get("ACME_APPLIANCE_CUSTOMER", "").strip()
    if env:
        return env
    m = re.search(r"/customers/([^/]+)/", os.path.abspath(config_path or "") + "/")
    return m.group(1) if m else ""


def _state_path(config_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(config_path)), "notification-state.json")


@contextlib.contextmanager
def _state(config_path: str):
    """Locked read-modify-write of the dedupe state. Yields a dict; best effort if the dir isn't writable."""
    path = _state_path(config_path)
    data, lock_f = {}, None
    try:
        lock_f = open(path + ".lock", "w")
        fcntl.flock(lock_f, fcntl.LOCK_EX)
    except OSError:
        lock_f = None
    try:
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        yield data
        cutoff = time.time() - STATE_KEEP_DAYS * 86400
        pruned = {k: v for k, v in data.items() if isinstance(v, (int, float)) and v >= cutoff}
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(pruned, f)
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("Could not save notification state (%s): %s -- repeat alerts are not rate-limited", path, exc)
    finally:
        if lock_f:
            with contextlib.suppress(Exception):
                fcntl.flock(lock_f, fcntl.LOCK_UN)
                lock_f.close()


def _due(state: dict, key: str, min_hours: float, now: float) -> bool:
    last = state.get(key)
    return not (isinstance(last, (int, float)) and now - last < min_hours * 3600)


def notify_event(event: str, domain: str = "", detail: str = "", *, config_path: str = None,
                 label: str = None, fleet_path: str = None) -> bool:
    """
    Send an alert for `event` if notifications and that event are enabled and
    it is not a repeat within the event's rate limit. NEVER raises: any
    problem is logged and False returned, so calling this from a renewal /
    deploy / DNS hook can't make that hook fail.
    """
    try:
        if event not in EVENTS:
            log.warning("notify_event: unknown event '%s'", event)
            return False
        config_path = config_path or DEFAULT_CONFIG_PATH
        s = get_config(_read_config(config_path))
        if not s["enabled"] or not s["events"].get(event):
            return False
        lbl = label_for(config_path, label)
        now = time.time()
        key = f"{event}:{domain}"
        with _state(config_path) as state:
            if not _due(state, key, EVENTS[event]["min_hours"], now):
                log.info("Notification '%s' for %s suppressed (rate limit)", event, domain or "-")
                return False
            subject = f"{s['subject_prefix']} {lbl + ': ' if lbl else ''}{_EVENT_SUBJECT[event]}" \
                      f"{' - ' + domain if domain else ''}".strip()
            body = f"{_EVENT_SUBJECT[event]}" + (f"\nDomain: {domain}" if domain else "") + \
                   (f"\n\nDetails:\n{detail.strip()}" if detail and detail.strip() else "") + _footer(lbl)
            send_message(s, subject, body, s["recipients"], fleet_path)
            state[key] = now
        log.info("Notification '%s' sent for %s", event, domain or "-")
        return True
    except Exception as exc:  # noqa: BLE001 - must never break the caller
        log.error("Notification '%s' for %s failed: %s", event, domain or "-", exc)
        return False


# --------------------------------------------------------------------------- certificate expiry scan

def _norm_name(n: str) -> str:
    n = (n or "").strip().lower().rstrip(".")
    return n[2:] if n.startswith("*.") else n


def configured_names(cfg: dict) -> set:
    names = set()
    for d in (cfg or {}).get("domains", []) or []:
        names.add(_norm_name(d.get("name", "")))
        for item in d.get("additional_names", []) or []:
            names.add(_norm_name(item["name"] if isinstance(item, dict) else item))
    names.discard("")
    return names


def _parse_cert(path: str):
    """(not_after UTC datetime, [dns names]) for a PEM certificate, using the openssl CLI."""
    try:
        out = subprocess.run(["openssl", "x509", "-in", path, "-noout", "-enddate", "-ext", "subjectAltName"],
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise NotificationError(f"openssl failed on {path}: {exc}") from exc
    if out.returncode != 0:
        raise NotificationError(f"openssl could not read {path}: {out.stderr.strip()[:160]}")
    m = re.search(r"notAfter=(.+)", out.stdout)
    if not m:
        raise NotificationError(f"no expiry date found in {path}")
    not_after = datetime.strptime(m.group(1).replace("GMT", "").strip(), "%b %d %H:%M:%S %Y").replace(tzinfo=timezone.utc)
    return not_after, re.findall(r"DNS:([^,\s]+)", out.stdout)


def scan_certificates(live_dir: str, only_names: set = None, now: datetime = None) -> list:
    """
    Read every certbot lineage under live_dir/. If only_names is given, keep
    only certs covering at least one of those names (so a lineage left over
    from a deleted domain does not alert forever).
    """
    now = now or datetime.now(timezone.utc)
    certs = []
    if not os.path.isdir(live_dir):
        return certs
    for lineage in sorted(os.listdir(live_dir)):
        pem = os.path.join(live_dir, lineage, "cert.pem")
        if not os.path.isfile(pem):
            continue
        try:
            not_after, names = _parse_cert(pem)
        except NotificationError as exc:
            log.warning("scan: %s", exc)
            continue
        if only_names is not None and not ({_norm_name(n) for n in names} & only_names):
            continue
        certs.append({"lineage": lineage, "names": names or [lineage], "not_after": not_after,
                      "days_left": (not_after - now).total_seconds() / 86400.0})
    return certs


def check_expiry(config_path: str = None, live_dir: str = None, *, label: str = None,
                 fleet_path: str = None, now: datetime = None, send: bool = True) -> dict:
    """
    Daily check. Collects every managed certificate within `expiry_warn_days`
    (or already expired) and sends ONE digest email. Re-alerts every 3 days,
    and daily once 3 days or less remain. Returns a summary dict.
    """
    config_path = config_path or DEFAULT_CONFIG_PATH
    live_dir = live_dir or DEFAULT_LIVE_DIR
    now = now or datetime.now(timezone.utc)
    cfg = _read_config(config_path)
    s = get_config(cfg)
    summary = {"config": config_path, "scanned": 0, "expiring": [], "sent": False, "skipped": ""}
    certs = scan_certificates(live_dir, configured_names(cfg), now)
    summary["scanned"] = len(certs)
    warn = s["expiry_warn_days"]
    expiring = [c for c in certs if c["days_left"] <= warn]
    summary["expiring"] = [{"lineage": c["lineage"], "days_left": round(c["days_left"], 1)} for c in expiring]
    if not expiring:
        return summary
    if not s["enabled"] or not s["events"].get("cert_expiring"):
        summary["skipped"] = "expiry notifications are disabled"
        return summary
    lbl = label_for(config_path, label)
    ts = now.timestamp()
    with _state(config_path) as state:
        due = []
        for c in expiring:
            key = f"cert_expiring:{c['lineage']}:{c['not_after']:%Y%m%d}"
            interval_h = 24 if c["days_left"] <= 3 else 72
            if _due(state, key, interval_h, ts):
                due.append((key, c))
        if not due:
            summary["skipped"] = "already alerted recently"
            return summary
        due.sort(key=lambda kc: kc[1]["days_left"])
        lines = []
        for _, c in due:
            when = "EXPIRED" if c["days_left"] < 0 else f"{int(c['days_left'])} day(s) left"
            lines.append(f"  {c['names'][0]}{' (+%d more)' % (len(c['names']) - 1) if len(c['names']) > 1 else ''}"
                         f" - {when} (expires {c['not_after']:%Y-%m-%d})")
        soonest = due[0][1]
        head = "EXPIRED" if soonest["days_left"] < 0 else f"{int(soonest['days_left'])}d left"
        subject = f"{s['subject_prefix']} {lbl + ': ' if lbl else ''}{len(due)} certificate(s) expiring soon ({head})".strip()
        body = ("These certificates are close to expiry. Renewal normally happens at 30 days, so something "
                "is stopping it. Check the appliance log and run a renewal.\n\n" + "\n".join(lines) + _footer(lbl))
        if send:
            send_message(s, subject, body, s["recipients"], fleet_path)
            for key, _ in due:
                state[key] = ts
            summary["sent"] = True
        summary["expiring"] = [{"lineage": c["lineage"], "days_left": round(c["days_left"], 1)} for _, c in due]
    return summary


def customer_config_paths(customers_dir: str = None):
    """Yield (slug, appliance.yaml path, live dir) for every msp-panos customer namespace."""
    customers_dir = customers_dir or CUSTOMERS_DIR
    if not os.path.isdir(customers_dir):
        return
    for slug in sorted(os.listdir(customers_dir)):
        cfg = os.path.join(customers_dir, slug, "appliance.yaml")
        if os.path.isfile(cfg):
            yield slug, cfg, os.path.join(customers_dir, slug, "letsencrypt", "live")


# --------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="acme-notify", description="Email alerting for the ACME appliance.")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("test", help="Send a test email using the saved settings")
    t.add_argument("--config", default=DEFAULT_CONFIG_PATH)

    e = sub.add_parser("event", help="Send an alert for an event (used by renew/deploy hooks; never fails the caller)")
    e.add_argument("event", choices=sorted(EVENTS))
    e.add_argument("--domain", default="")
    e.add_argument("--detail", default="")
    e.add_argument("--detail-file", help="Read details from this file (last 4000 chars)")
    e.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    e.add_argument("--label", default=None)

    c = sub.add_parser("check-expiry", help="Scan certificates and email a digest if any are close to expiry")
    c.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    c.add_argument("--live-dir", default=DEFAULT_LIVE_DIR)
    c.add_argument("--all-customers", action="store_true", help="msp-panos: check every customer namespace")
    c.add_argument("--dry-run", action="store_true", help="Report only; send nothing")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s acme-notify %(levelname)s %(message)s")
    log_file = os.environ.get("ACME_APPLIANCE_LOG", "/var/log/acme-appliance.log")
    try:
        fh = logging.FileHandler(log_file)
        fh.setFormatter(logging.Formatter("%(asctime)s acme-notify %(levelname)s %(message)s"))
        logging.getLogger().addHandler(fh)
    except OSError:
        pass

    if args.cmd == "test":
        try:
            s = get_config(_read_config(args.config))
            print(send_test_email(s, label_for(args.config)))
            return 0
        except NotificationError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            return 1

    if args.cmd == "event":
        detail = args.detail
        if args.detail_file:
            try:
                with open(args.detail_file, "r", errors="replace") as f:
                    detail = f.read()[-4000:]
            except OSError as exc:
                detail = f"(could not read {args.detail_file}: {exc})"
        sent = notify_event(args.event, args.domain, detail, config_path=args.config, label=args.label)
        print("sent" if sent else "not sent (disabled, rate-limited, or failed -- see log)")
        return 0  # never fail a renewal because an alert could not be sent

    # check-expiry
    targets = []
    if args.all_customers:
        targets = [(slug, cfg, live) for slug, cfg, live in customer_config_paths()]
    else:
        targets = [(None, args.config, args.live_dir)]
    rc = 0
    for slug, cfg, live in targets:
        try:
            res = check_expiry(cfg, live, label=slug, send=not args.dry_run)
            log.info("check-expiry %s: scanned=%d expiring=%d sent=%s %s", slug or cfg, res["scanned"],
                     len(res["expiring"]), res["sent"], res["skipped"])
            if args.dry_run or res["expiring"]:
                print(f"{slug or cfg}: scanned {res['scanned']}, expiring {len(res['expiring'])}"
                      f"{' (' + res['skipped'] + ')' if res['skipped'] else ''}"
                      + "".join(f"\n  {x['lineage']}: {x['days_left']} days" for x in res["expiring"]))
        except NotificationError as exc:
            log.error("check-expiry %s failed: %s", slug or cfg, exc)
            print(f"{slug or cfg}: FAILED: {exc}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
