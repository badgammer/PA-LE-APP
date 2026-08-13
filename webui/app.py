#!/usr/bin/env python3
"""
Simple web frontend for configuring the ACME / Palo Alto appliance:
DNS provider instances, target firewalls, and the domains that tie them
together -- plus a log viewer, on-demand renewal triggers, a "redeploy
existing certificate" action, an SSL/TLS profile picker, certificate
export, ACME account settings, and OS update checking/applying.
"""

import io
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import zipfile
from datetime import datetime

from flask import (
    Flask, render_template, request, redirect, url_for, session, flash, abort,
    jsonify, send_file
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth  # noqa: E402
import config_store as store  # noqa: E402
import test_connections  # noqa: E402
import system_updates  # noqa: E402
from dns_providers import PROVIDER_FIELDS  # noqa: E402
from deploy_providers import INSTANCE_FIELDS, TARGET_FIELDS  # noqa: E402
from cert_naming import safe_cert_name  # noqa: E402

APPLIANCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.environ.get("ACME_APPLIANCE_LOG", "/var/log/acme-appliance.log")
RENEW_LOCK_DIR = os.environ.get("ACME_APPLIANCE_RENEW_LOCK_DIR", "/var/run/acme-appliance")
LETSENCRYPT_LIVE_DIR = os.environ.get(
    "ACME_APPLIANCE_LE_LIVE_DIR", "/etc/acme-appliance/letsencrypt/live"
)
SECRET_KEY_PATH = os.environ.get(
    "ACME_APPLIANCE_SECRET_KEY_FILE", "/etc/acme-appliance/webui_secret_key"
)

PLACEHOLDER_EMAIL_DOMAINS = {
    "example.com", "example.org", "example.net", "example.edu",
    "test.com", "localhost", "invalid",
}

PRODUCTION_ACME_SERVER = "https://acme-v02.api.letsencrypt.org/directory"
STAGING_ACME_SERVER = "https://acme-staging-v02.api.letsencrypt.org/directory"

app = Flask(__name__)


def _load_or_create_secret_key() -> bytes:
    os.makedirs(os.path.dirname(SECRET_KEY_PATH), exist_ok=True)
    if os.path.exists(SECRET_KEY_PATH):
        with open(SECRET_KEY_PATH, "rb") as f:
            return f.read()
    key = secrets.token_bytes(32)
    with open(SECRET_KEY_PATH, "wb") as f:
        f.write(key)
    os.chmod(SECRET_KEY_PATH, 0o600)
    return key


app.secret_key = _load_or_create_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("ACME_APPLIANCE_UI_TLS", "1") == "1",
)


def current_user():
    return session.get("username")


def login_required(view):
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    wrapped.__name__ = view.__name__
    return wrapped


def csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


def check_csrf():
    token = request.form.get("csrf_token", "")
    if not token or not secrets.compare_digest(token, session.get("csrf_token", "")):
        abort(400, "Invalid or missing CSRF token. Please retry.")


@app.context_processor
def inject_globals():
    return {"csrf_token": csrf_token, "current_user": current_user()}


def mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "****"
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


def _appliance_log(message: str) -> None:
    try:
        with open(LOG_PATH, "a") as f:
            stamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            f.write(f"{stamp} webui: {message}\n")
    except OSError:
        pass


def _safe_lock_name(domain: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", domain)


def _lock_path(domain=None) -> str:
    os.makedirs(RENEW_LOCK_DIR, exist_ok=True)
    name = "renew-ALL.lock" if domain is None else f"renew-{_safe_lock_name(domain)}.lock"
    return os.path.join(RENEW_LOCK_DIR, name)


def _redeploy_lock_path(domain: str) -> str:
    os.makedirs(RENEW_LOCK_DIR, exist_ok=True)
    return os.path.join(RENEW_LOCK_DIR, f"redeploy-{_safe_lock_name(domain)}.lock")


def _active_lock_labels() -> list:
    if not os.path.isdir(RENEW_LOCK_DIR):
        return []
    labels = []
    for fname in sorted(os.listdir(RENEW_LOCK_DIR)):
        if fname == "renew-ALL.lock":
            labels.append("All domains (renew)")
        elif fname.startswith("renew-") and fname.endswith(".lock"):
            labels.append(fname[len("renew-"):-len(".lock")] + " (renew)")
        elif fname.startswith("redeploy-") and fname.endswith(".lock"):
            labels.append(fname[len("redeploy-"):-len(".lock")] + " (redeploy)")
    return labels


def _renewal_in_progress(domain=None) -> bool:
    if os.path.exists(_lock_path(None)):
        return True
    if domain is not None and os.path.exists(_lock_path(domain)):
        return True
    return False


def _redeploy_in_progress(domain: str) -> bool:
    return os.path.exists(_redeploy_lock_path(domain))


def _domain_busy(domain: str) -> bool:
    return _renewal_in_progress(domain) or _redeploy_in_progress(domain)


def _start_background(lock_path: str, script_name: str, args: list) -> None:
    script = os.path.join(APPLIANCE_DIR, "bin", script_name)
    quoted_cmd = " ".join(shlex.quote(a) for a in [script] + args)
    with open(lock_path, "w") as f:
        f.write(str(os.getpid()))
    subprocess.Popen(
        ["/bin/bash", "-c", f"{quoted_cmd}; rm -f {shlex.quote(lock_path)}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if auth.any_users_exist():
        return redirect(url_for("login"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        enable_totp = request.form.get("enable_totp") == "on"
        if not username or not password:
            flash("Username and password are required.", "error")
        elif password != confirm:
            flash("Passwords do not match.", "error")
        elif len(password) < 10:
            flash("Password must be at least 10 characters.", "error")
        else:
            totp_secret = auth.create_user(username, password, enable_totp)
            if enable_totp:
                uri = auth.totp_provisioning_uri(username, totp_secret)
                flash(
                    "Admin account created. Add this secret to your authenticator "
                    f"app before logging in: {totp_secret}  ({uri})",
                    "success",
                )
            else:
                flash("Admin account created. You can now log in.", "success")
            return redirect(url_for("login"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not auth.any_users_exist():
        return redirect(url_for("setup"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if auth.verify_password(username, password):
            if auth.requires_totp(username):
                session["pending_totp_user"] = username
                return redirect(url_for("login_verify"))
            session["username"] = username
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@app.route("/login/verify", methods=["GET", "POST"])
def login_verify():
    username = session.get("pending_totp_user")
    if not username:
        return redirect(url_for("login"))
    if request.method == "POST":
        code = request.form.get("code", "").strip()
        if auth.verify_totp(username, code):
            session.pop("pending_totp_user", None)
            session["username"] = username
            return redirect(url_for("dashboard"))
        flash("Invalid authentication code.", "error")
    return render_template("login_verify.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    cfg = store.load_config()
    last_lines = _tail_log(20)
    return render_template(
        "dashboard.html",
        cfg=cfg,
        domain_count=len(cfg["domains"]),
        provider_count=len(cfg["dns_providers"]),
        deploy_provider_count=len(cfg["deploy_providers"]),
        last_lines=last_lines,
        active_renewals=_active_lock_labels(),
    )


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    cfg = store.load_config()
    if request.method == "POST":
        check_csrf()
        email = request.form.get("acme_email", "").strip()
        server_choice = request.form.get("acme_server_choice", "production")
        custom_server = request.form.get("acme_server_custom", "").strip()

        error = None
        if not email or "@" not in email or email.startswith("@") or email.endswith("@"):
            error = "Enter a valid email address."
        else:
            domain_part = email.rsplit("@", 1)[-1].lower()
            if domain_part in PLACEHOLDER_EMAIL_DOMAINS:
                error = (
                    f"'{domain_part}' is a reserved/placeholder domain -- Let's Encrypt "
                    "will reject account registration with this address. Use a real, "
                    "monitored email address (this is where certificate expiration "
                    "warnings will be sent)."
                )

        if server_choice == "production":
            server = PRODUCTION_ACME_SERVER
        elif server_choice == "staging":
            server = STAGING_ACME_SERVER
        else:
            server = custom_server
            if not error and not server.startswith("https://"):
                error = "Custom ACME server URL must start with https://"

        if error:
            flash(error, "error")
        else:
            cfg["acme"]["email"] = email
            cfg["acme"]["server"] = server
            store.save_config(cfg)
            flash("ACME settings updated.", "success")
            return redirect(url_for("settings_page"))

    current_server = cfg["acme"].get("server", "")
    if current_server == PRODUCTION_ACME_SERVER:
        server_choice = "production"
    elif current_server == STAGING_ACME_SERVER:
        server_choice = "staging"
    else:
        server_choice = "custom"

    return render_template(
        "settings.html", cfg=cfg, server_choice=server_choice,
        production_server=PRODUCTION_ACME_SERVER, staging_server=STAGING_ACME_SERVER,
    )


@app.route("/renew-now", methods=["POST"])
@login_required
def renew_now():
    check_csrf()
    if _active_lock_labels():
        flash(
            "One or more renewals or redeploys are currently running; "
            "wait for them to finish before renewing all domains.", "error",
        )
        return redirect(url_for("logs"))
    try:
        _start_background(_lock_path(None), "acme-renew.sh", [])
        flash(
            "Renewal started for all domains in the background. "
            "Refresh the log below to follow progress.", "success",
        )
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not start renewal: {exc}", "error")
    return redirect(url_for("logs"))


@app.route("/domains/<path:name>/renew", methods=["POST"])
@login_required
def domain_renew(name):
    check_csrf()
    cfg = store.load_config()
    if not store.get_domain(cfg, name):
        abort(404)
    if _domain_busy(name):
        flash(f"A renewal or redeploy is already in progress for '{name}' (or for all domains).", "error")
        return redirect(url_for("logs"))
    force = request.form.get("force") == "on"
    try:
        args = [name] + (["--force"] if force else [])
        _start_background(_lock_path(name), "acme-renew.sh", args)
        suffix = " (forcing renewal outside the normal 30-day window)" if force else ""
        flash(
            f"Renewal started for '{name}' in the background{suffix}. "
            "Refresh the log below to follow progress.", "success",
        )
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not start renewal for '{name}': {exc}", "error")
    return redirect(url_for("logs"))


@app.route("/domains/<path:name>/redeploy", methods=["POST"])
@login_required
def domain_redeploy(name):
    check_csrf()
    cfg = store.load_config()
    if not store.get_domain(cfg, name):
        abort(404)
    if not _cert_lineage_dir(name):
        flash(
            f"No certificate has been issued yet for '{name}' -- nothing to redeploy. "
            "Use 'Renew now' first.", "error",
        )
        return redirect(url_for("domains_list"))
    if _domain_busy(name):
        flash(f"A renewal or redeploy is already in progress for '{name}' (or for all domains).", "error")
        return redirect(url_for("logs"))
    try:
        _start_background(_redeploy_lock_path(name), "redeploy-cert.sh", [name])
        flash(
            f"Redeploy started for '{name}' in the background -- re-importing the existing "
            "certificate and committing it to the configured firewall target(s). No new "
            "ACME issuance is performed. Refresh the log below to follow progress.", "success",
        )
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not start redeploy for '{name}': {exc}", "error")
    return redirect(url_for("logs"))


def _tail_log(n: int):
    if not os.path.exists(LOG_PATH):
        return []
    with open(LOG_PATH, "r", errors="replace") as f:
        lines = f.readlines()
    return lines[-n:]


@app.route("/logs")
@login_required
def logs():
    n = int(request.args.get("n", 200))
    return render_template("logs.html", lines=_tail_log(n), n=n,
                            active_renewals=_active_lock_labels())


@app.route("/domains")
@login_required
def domains_list():
    cfg = store.load_config()
    rows = []
    for d in cfg["domains"]:
        rows.append({
            "entry": d,
            "additional_names_display": _additional_names_display(d),
            "deploy_targets_display": _deploy_targets_display(d, cfg["deploy_providers"]),
            "cert_expiry": _cert_expiry(d["name"]),
            "renewal_in_progress": _renewal_in_progress(d["name"]),
            "redeploy_in_progress": _redeploy_in_progress(d["name"]),
            "has_cert": _cert_lineage_dir(d["name"]) is not None,
        })
    return render_template("domains.html", rows=rows, any_renewal_in_progress=bool(_active_lock_labels()))


def _target_summary(target_entry: dict, provider_type: str) -> str:
    """
    Human-readable one-liner for a single deploy_targets[] row, used on
    the Domains list page -- each deploy target TYPE has its own
    type-specific fields (see deploy_providers.TARGET_FIELDS), so this
    knows how to summarize the ones this appliance ships with and falls
    back to a generic dump of the entry for anything else.
    """
    if provider_type == "panos":
        kind = "GlobalProtect portal" if target_entry.get("cert_field_type") == "globalprotect_portal" else "SSL/TLS profile"
        vsys = f" (vsys={target_entry['vsys']})" if target_entry.get("vsys") else ""
        return f"{kind}: {target_entry.get('cert_field_value', '')}{vsys}"
    if provider_type == "iis":
        ip = target_entry.get("binding_ip") or "*"
        port = target_entry.get("binding_port") or 443
        host = f", SNI={target_entry['hostname']}" if target_entry.get("hostname") else ""
        return f"IIS site '{target_entry.get('site_name', '')}' ({ip}:{port}{host})"
    return ", ".join(f"{k}={v}" for k, v in target_entry.items() if k != "target")


def _deploy_targets_display(entry: dict, deploy_providers: dict) -> list:
    result = []
    for t in entry.get("deploy_targets", []) or []:
        instance = deploy_providers.get(t.get("target"), {})
        result.append({
            "target": t.get("target"),
            "type": instance.get("type", "unknown"),
            "summary": _target_summary(t, instance.get("type", "")),
        })
    return result


def _additional_names_display(entry: dict) -> list:
    """
    Returns [{"name": ..., "override": provider_name_or_empty}] for
    display on the Domains list page -- override is non-empty only when
    that specific name uses a DIFFERENT dns_provider than the entry's
    own default (i.e. a genuine per-name override), so the UI can show
    a small badge only where it's actually meaningful.
    """
    default_provider = entry.get("dns_provider")
    result = []
    for item in entry.get("additional_names", []) or []:
        if isinstance(item, dict):
            name = item["name"]
            override = item.get("dns_provider") or ""
            if override == default_provider:
                override = ""
        else:
            name = item
            override = ""
        result.append({"name": name, "override": override})
    return result


def _cert_lineage_dir(domain_name: str):
    for candidate in (safe_cert_name(domain_name), domain_name):
        path = os.path.join(LETSENCRYPT_LIVE_DIR, candidate)
        if os.path.isdir(path):
            return path
    return None


def _cert_expiry(domain_name: str):
    lineage_dir = _cert_lineage_dir(domain_name)
    if not lineage_dir:
        return None
    cert_path = os.path.join(lineage_dir, "cert.pem")
    if not os.path.exists(cert_path):
        return None
    try:
        out = subprocess.check_output(
            ["openssl", "x509", "-enddate", "-noout", "-in", cert_path],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode()
        return out.strip().replace("notAfter=", "")
    except Exception:  # noqa: BLE001
        return None


@app.route("/domains/<path:name>/download")
@login_required
def domain_download(name):
    cfg = store.load_config()
    if not store.get_domain(cfg, name):
        abort(404)
    lineage_dir = _cert_lineage_dir(name)
    if not lineage_dir:
        flash(f"No certificate has been issued yet for '{name}'.", "error")
        return redirect(url_for("domains_list"))

    buf = io.BytesIO()
    included = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in ("fullchain.pem", "cert.pem", "chain.pem", "privkey.pem"):
            fpath = os.path.join(lineage_dir, fname)
            if os.path.exists(fpath):
                zf.write(fpath, arcname=fname)
                included.append(fname)
    buf.seek(0)

    _appliance_log(
        f"user '{current_user()}' downloaded certificate files for '{name}' "
        f"({', '.join(included)}) from {request.remote_addr}"
    )

    zip_name = f"{safe_cert_name(name)}-certs.zip"
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=zip_name)


@app.route("/domains/new", methods=["GET", "POST"])
@login_required
def domain_new():
    cfg = store.load_config()
    if request.method == "POST":
        check_csrf()
        name, entry, error = _domain_from_form(cfg)
        if error:
            flash(error, "error")
        elif store.get_domain(cfg, name):
            flash(f"A domain entry for '{name}' already exists.", "error")
        else:
            store.upsert_domain(cfg, name, entry)
            store.save_config(cfg)
            flash(f"Added domain {name}.", "success")
            return redirect(url_for("domains_list"))
    return render_template(
        "domain_form.html", mode="new", entry=None, has_cert=False, redeploy_in_progress=False,
        additional_names_for_form=[], deploy_targets_for_form=[],
        providers=cfg["dns_providers"], deploy_providers=cfg["deploy_providers"],
        target_fields_schema=TARGET_FIELDS,
    )


@app.route("/domains/<path:name>/edit", methods=["GET", "POST"])
@login_required
def domain_edit(name):
    cfg = store.load_config()
    entry = store.get_domain(cfg, name)
    if not entry:
        abort(404)
    if request.method == "POST":
        check_csrf()
        new_name, new_entry, error = _domain_from_form(cfg)
        if error:
            flash(error, "error")
        else:
            store.delete_domain(cfg, name)
            store.upsert_domain(cfg, new_name, new_entry)
            store.save_config(cfg)
            flash(f"Updated domain {new_name}.", "success")
            return redirect(url_for("domains_list"))
    return render_template(
        "domain_form.html", mode="edit", entry=entry,
        has_cert=_cert_lineage_dir(name) is not None,
        redeploy_in_progress=_redeploy_in_progress(name),
        additional_names_for_form=_additional_names_for_form(entry),
        deploy_targets_for_form=entry.get("deploy_targets", []),
        providers=cfg["dns_providers"], deploy_providers=cfg["deploy_providers"],
        target_fields_schema=TARGET_FIELDS,
    )


def _additional_names_for_form(entry: dict) -> list:
    """
    Returns [{"name": ..., "provider_override": provider_or_empty}] for
    populating the Edit Domain form's repeatable "Additional names"
    rows. Unlike _additional_names_display (used on the Domains list),
    this deliberately keeps provider_override EMPTY (not resolved to the
    entry's default) whenever the stored entry didn't specify one, so
    the form's dropdown correctly pre-selects "(same as primary)"
    rather than appearing to explicitly re-select the default provider.
    """
    result = []
    for item in entry.get("additional_names", []) or []:
        if isinstance(item, dict):
            result.append({"name": item["name"], "provider_override": item.get("dns_provider") or ""})
        else:
            result.append({"name": item, "provider_override": ""})
    return result


@app.route("/domains/<path:name>/delete", methods=["POST"])
@login_required
def domain_delete(name):
    check_csrf()
    cfg = store.load_config()
    store.delete_domain(cfg, name)
    store.save_config(cfg)
    flash(f"Deleted domain {name}.", "success")
    return redirect(url_for("domains_list"))


def _domain_from_form(cfg):
    name = request.form.get("name", "").strip()
    dns_provider = request.form.get("dns_provider", "").strip()
    cert_name_prefix = request.form.get("cert_name_prefix", "").strip() or "gp-portal-cert"

    if not name:
        return None, None, "Domain name is required."
    if dns_provider not in cfg["dns_providers"]:
        return None, None, "Select a valid primary DNS provider."

    # Additional names (SANs): each row is a name + an optional per-name
    # DNS provider override. An override is only stored (as a {"name":
    # ..., "dns_provider": ...} dict) when it differs from the entry's
    # own primary dns_provider -- otherwise the name is stored as a
    # plain string, keeping the YAML clean and fully backward-compatible
    # with configs written before this feature existed.
    additional_names = []
    an_names = request.form.getlist("additional_name[]")
    an_providers = request.form.getlist("additional_name_provider[]")
    for an_name, an_provider in zip(an_names, an_providers):
        an_name = an_name.strip()
        if not an_name:
            continue
        if an_provider and an_provider != dns_provider:
            if an_provider not in cfg["dns_providers"]:
                return None, None, f"Unknown DNS provider override '{an_provider}' for additional name '{an_name}'."
            additional_names.append({"name": an_name, "dns_provider": an_provider})
        else:
            additional_names.append(an_name)

    # Deploy targets: each row is a reference to a named deploy_providers[]
    # instance (any type -- PAN-OS firewall, IIS server, etc.) plus a JSON
    # blob of that type's own fields (e.g. ssl_tls_profile/vsys for panos,
    # site_name/binding_ip/binding_port/hostname for iis). The JSON blob is
    # assembled client-side by domain_form.html's JS right before submit,
    # from whichever type-specific subform is currently rendered for that
    # row -- this lets ONE generic form support any number of deploy
    # target types without a fixed set of parallel array fields per type.
    deploy_targets = []
    target_instances = request.form.getlist("target_instance[]")
    target_configs = request.form.getlist("target_config[]")
    for instance_name, config_json in zip(target_instances, target_configs):
        if not instance_name:
            continue
        if instance_name not in cfg["deploy_providers"]:
            return None, None, f"Unknown deploy target '{instance_name}' in target list."
        try:
            fields = json.loads(config_json) if config_json else {}
            if not isinstance(fields, dict):
                raise ValueError("not an object")
        except (TypeError, ValueError):
            return None, None, f"Invalid target configuration submitted for '{instance_name}'."
        provider_type = cfg["deploy_providers"][instance_name]["type"]
        schema = TARGET_FIELDS.get(provider_type, {"fields": []})
        for field in schema["fields"]:
            if field.get("required") and not fields.get(field["name"]):
                return None, None, (
                    f"'{field['label']}' is required for deploy target '{instance_name}' "
                    f"({provider_type})."
                )
        target = {"target": instance_name}
        target.update(fields)
        deploy_targets.append(target)

    if not deploy_targets:
        return None, None, "At least one deploy target is required."

    entry = {
        "name": name,
        "dns_provider": dns_provider,
        "cert_name_prefix": cert_name_prefix,
        "deploy_targets": deploy_targets,
    }
    if additional_names:
        entry["additional_names"] = additional_names
    return name, entry, None


@app.route("/deploy-providers/<name>/options")
@login_required
def deploy_provider_options(name):
    cfg = store.load_config()
    instance = cfg["deploy_providers"].get(name)
    if instance is None:
        return jsonify({"ok": False, "error": f"Unknown deploy target '{name}'"}), 404
    kwargs = {}
    if request.args.get("vsys"):
        kwargs["vsys"] = request.args.get("vsys")
    ok, result = test_connections.list_target_options(instance["type"], instance.get("settings", {}), **kwargs)
    if ok:
        return jsonify({"ok": True, "options": result})
    return jsonify({"ok": False, "error": result})


@app.route("/dns-providers")
@login_required
def dns_providers_list():
    cfg = store.load_config()
    provider_labels = {k: v["label"] for k, v in PROVIDER_FIELDS.items()}
    return render_template(
        "dns_providers.html", providers=cfg["dns_providers"],
        provider_labels=provider_labels, mask=mask,
    )


@app.route("/dns-providers/new", methods=["GET", "POST"])
@login_required
def dns_provider_new():
    cfg = store.load_config()
    selected_type = request.values.get("type", next(iter(PROVIDER_FIELDS)))
    if request.method == "POST":
        check_csrf()
        instance_name = request.form.get("instance_name", "").strip()
        provider_type = request.form.get("type", "").strip()
        if not instance_name or provider_type not in PROVIDER_FIELDS:
            flash("A unique name and a valid provider type are required.", "error")
        elif instance_name in cfg["dns_providers"]:
            flash(f"A DNS provider named '{instance_name}' already exists.", "error")
        else:
            settings = _settings_from_form(PROVIDER_FIELDS, provider_type, existing_settings={})
            store.upsert_dns_provider(cfg, instance_name, provider_type, settings)
            store.save_config(cfg)
            flash(f"Added DNS provider '{instance_name}'.", "success")
            return redirect(url_for("dns_providers_list"))
        selected_type = provider_type or selected_type
    return render_template(
        "dns_provider_form.html", mode="new", instance_name="",
        provider_fields=PROVIDER_FIELDS, selected_type=selected_type,
        existing_settings={},
    )


@app.route("/dns-providers/<instance_name>/edit", methods=["GET", "POST"])
@login_required
def dns_provider_edit(instance_name):
    cfg = store.load_config()
    instance = cfg["dns_providers"].get(instance_name)
    if not instance:
        abort(404)
    provider_type = instance["type"]
    if request.method == "POST":
        check_csrf()
        settings = _settings_from_form(PROVIDER_FIELDS, provider_type, existing_settings=instance.get("settings", {}))
        store.upsert_dns_provider(cfg, instance_name, provider_type, settings)
        store.save_config(cfg)
        flash(f"Updated DNS provider '{instance_name}'.", "success")
        return redirect(url_for("dns_providers_list"))
    return render_template(
        "dns_provider_form.html", mode="edit", instance_name=instance_name,
        provider_fields=PROVIDER_FIELDS, selected_type=provider_type,
        existing_settings=instance.get("settings", {}),
    )


@app.route("/dns-providers/<instance_name>/delete", methods=["POST"])
@login_required
def dns_provider_delete(instance_name):
    check_csrf()
    cfg = store.load_config()
    used_by = store.dns_provider_in_use(cfg, instance_name)
    if used_by:
        flash(f"Cannot delete '{instance_name}' -- still used by domain(s): {', '.join(used_by)}.", "error")
    else:
        store.delete_dns_provider(cfg, instance_name)
        store.save_config(cfg)
        flash(f"Deleted DNS provider '{instance_name}'.", "success")
    return redirect(url_for("dns_providers_list"))


@app.route("/dns-providers/<instance_name>/test", methods=["POST"])
@login_required
def dns_provider_test(instance_name):
    check_csrf()
    cfg = store.load_config()
    instance = cfg["dns_providers"].get(instance_name)
    if not instance:
        abort(404)
    ok, message = test_connections.test_dns_provider(instance["type"], instance.get("settings", {}))
    if ok is True:
        flash(f"'{instance_name}': {message}", "success")
    elif ok is False:
        flash(f"'{instance_name}' test failed: {message}", "error")
    else:
        flash(f"'{instance_name}': {message}", "error")
    return redirect(url_for("dns_providers_list"))


def _settings_from_form(fields_schema: dict, provider_type: str, existing_settings: dict) -> dict:
    """
    Generic "read an instance's connection settings out of request.form"
    helper -- used for BOTH dns_providers (fields_schema=PROVIDER_FIELDS)
    and deploy_providers (fields_schema=INSTANCE_FIELDS) instance forms,
    since both follow the exact same {label, name, type, secret, default,
    required} field-schema shape.
    """
    settings = dict(existing_settings)
    for field in fields_schema[provider_type]["fields"]:
        fname = field["name"]
        if field.get("type") == "checkbox":
            settings[fname] = request.form.get(fname) == "on"
            continue
        value = request.form.get(fname, "")
        if field.get("secret") and not value:
            continue
        if value == "" and "default" in field:
            settings[fname] = field["default"]
        elif field.get("type") == "number":
            settings[fname] = int(value) if value else field.get("default", 0)
        else:
            settings[fname] = value
    return settings


@app.route("/deploy-providers")
@login_required
def deploy_providers_list():
    cfg = store.load_config()
    provider_labels = {k: v["label"] for k, v in INSTANCE_FIELDS.items()}
    return render_template(
        "deploy_providers.html", providers=cfg["deploy_providers"],
        provider_labels=provider_labels, mask=mask,
    )


@app.route("/deploy-providers/new", methods=["GET", "POST"])
@login_required
def deploy_provider_new():
    cfg = store.load_config()
    selected_type = request.values.get("type", next(iter(INSTANCE_FIELDS)))
    if request.method == "POST":
        check_csrf()
        instance_name = request.form.get("instance_name", "").strip()
        provider_type = request.form.get("type", "").strip()
        if not instance_name or provider_type not in INSTANCE_FIELDS:
            flash("A unique name and a valid deploy target type are required.", "error")
        elif instance_name in cfg["deploy_providers"]:
            flash(f"A deploy target named '{instance_name}' already exists.", "error")
        else:
            settings = _settings_from_form(INSTANCE_FIELDS, provider_type, existing_settings={})
            store.upsert_deploy_provider(cfg, instance_name, provider_type, settings)
            store.save_config(cfg)
            flash(f"Added deploy target '{instance_name}'.", "success")
            return redirect(url_for("deploy_providers_list"))
        selected_type = provider_type or selected_type
    return render_template(
        "deploy_provider_form.html", mode="new", instance_name="",
        provider_fields=INSTANCE_FIELDS, selected_type=selected_type,
        existing_settings={},
    )


@app.route("/deploy-providers/<instance_name>/edit", methods=["GET", "POST"])
@login_required
def deploy_provider_edit(instance_name):
    cfg = store.load_config()
    instance = cfg["deploy_providers"].get(instance_name)
    if not instance:
        abort(404)
    provider_type = instance["type"]
    if request.method == "POST":
        check_csrf()
        settings = _settings_from_form(INSTANCE_FIELDS, provider_type, existing_settings=instance.get("settings", {}))
        store.upsert_deploy_provider(cfg, instance_name, provider_type, settings)
        store.save_config(cfg)
        flash(f"Updated deploy target '{instance_name}'.", "success")
        return redirect(url_for("deploy_providers_list"))
    return render_template(
        "deploy_provider_form.html", mode="edit", instance_name=instance_name,
        provider_fields=INSTANCE_FIELDS, selected_type=provider_type,
        existing_settings=instance.get("settings", {}),
    )


@app.route("/deploy-providers/<instance_name>/delete", methods=["POST"])
@login_required
def deploy_provider_delete(instance_name):
    check_csrf()
    cfg = store.load_config()
    used_by = store.deploy_provider_in_use(cfg, instance_name)
    if used_by:
        flash(f"Cannot delete '{instance_name}' -- still used by domain(s): {', '.join(used_by)}.", "error")
    else:
        store.delete_deploy_provider(cfg, instance_name)
        store.save_config(cfg)
        flash(f"Deleted deploy target '{instance_name}'.", "success")
    return redirect(url_for("deploy_providers_list"))


@app.route("/deploy-providers/<instance_name>/test", methods=["POST"])
@login_required
def deploy_provider_test(instance_name):
    check_csrf()
    cfg = store.load_config()
    instance = cfg["deploy_providers"].get(instance_name)
    if not instance:
        abort(404)
    ok, message = test_connections.test_deploy_provider(instance["type"], instance.get("settings", {}))
    if ok is True:
        flash(f"'{instance_name}': {message}", "success")
    elif ok is False:
        flash(f"'{instance_name}' test failed: {message}", "error")
    else:
        flash(f"'{instance_name}': {message}", "error")
    return redirect(url_for("deploy_providers_list"))


@app.route("/system")
@login_required
def system_page():
    return render_template(
        "system.html",
        check_status=system_updates.get_check_status(),
        apply_status=system_updates.get_last_apply_status(),
        check_in_progress=system_updates.is_check_in_progress(),
        update_in_progress=system_updates.is_update_in_progress(),
        sudo_group=system_updates.SUDO_GROUP,
    )


@app.route("/system/check", methods=["POST"])
@login_required
def system_check():
    check_csrf()
    ok, message = system_updates.trigger_check()
    flash(message, "success" if ok else "error")
    return redirect(url_for("system_page"))


@app.route("/system/apply", methods=["POST"])
@login_required
def system_apply():
    check_csrf()
    username = request.form.get("sudo_username", "")
    password = request.form.get("sudo_password", "")
    try:
        ok, message = system_updates.trigger_apply_update(username, password)
        flash(message, "success" if ok else "error")
    except system_updates.StepUpAuthError as exc:
        flash(str(exc), "error")
    finally:
        password = None  # noqa: F841
    return redirect(url_for("system_page"))


@app.route("/system/reboot", methods=["POST"])
@login_required
def system_reboot():
    check_csrf()
    username = request.form.get("sudo_username", "")
    password = request.form.get("sudo_password", "")
    try:
        ok, message = system_updates.trigger_reboot(username, password)
        flash(message, "success" if ok else "error")
    except system_updates.StepUpAuthError as exc:
        flash(str(exc), "error")
    finally:
        password = None  # noqa: F841
    return redirect(url_for("system_page"))


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    username = current_user()
    users = auth.load_users()["users"]
    user = users.get(username, {})
    if request.method == "POST":
        check_csrf()
        action = request.form.get("action")
        if action == "change_password":
            current_pw = request.form.get("current_password", "")
            new_pw = request.form.get("new_password", "")
            confirm = request.form.get("confirm_password", "")
            if not auth.verify_password(username, current_pw):
                flash("Current password is incorrect.", "error")
            elif new_pw != confirm:
                flash("New passwords do not match.", "error")
            elif len(new_pw) < 10:
                flash("New password must be at least 10 characters.", "error")
            else:
                auth.set_password(username, new_pw)
                flash("Password updated.", "success")
        elif action == "enable_totp":
            secret = auth.set_totp(username, True)
            uri = auth.totp_provisioning_uri(username, secret)
            flash(f"MFA enabled. Add this secret to your authenticator app: {secret}  ({uri})", "success")
        elif action == "disable_totp":
            auth.set_totp(username, False)
            flash("MFA disabled.", "success")
        return redirect(url_for("account"))
    return render_template("account.html", username=username, totp_enabled=user.get("totp_enabled", False))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8443, debug=True)
