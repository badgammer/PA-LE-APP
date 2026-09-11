#!/usr/bin/env python3
"""
MSP Console -- a fleet-wide dashboard for the msp-panos install profile.

Runs as its OWN Flask app, under its OWN unprivileged system account
(acme-msp-console), with its OWN admin account store (msp_console/auth.py,
backed by /etc/acme-appliance/msp-console/admins.yaml) -- completely
separate from any customer's own web UI login. Logging into the MSP
Console grants NO access to any customer's DNS providers, deploy
targets, or domain configuration; it only grants the specific
fleet-management capabilities described in msp_console/auth.py's
docstring (view status, provision/deprovision, trigger renewals, restart
a hung instance, tail logs -- gated by each admin's own read/write
grants per customer).

This app is ONLY ever installed/enabled under the msp-panos profile --
see lib/profile-msp-panos.sh.
"""
import io
import os
import secrets
import sys
import zipfile

from flask import (
    Flask, render_template, request, redirect, url_for, session, flash, abort, jsonify,
    send_file
)

# IMPORTANT: insertion order matters here. Each sys.path.insert(0, ...)
# pushes to the FRONT of the search path, so whichever path is inserted
# LAST ends up with the HIGHEST priority. webui/ is inserted FIRST
# (lowest priority) and this file's own directory LAST (highest
# priority) specifically so that "import auth" (and "import fleet",
# "import actions") resolve to THIS package's own auth.py/fleet.py/
# actions.py -- NOT webui/auth.py, which is a same-named but
# completely different module (single-instance profile's user store,
# with any_users_exist()/create_user() instead of this package's
# any_admins_exist()/create_admin()/is_owner()/customer_grant_level()).
# Getting this order backwards previously caused "import auth" to
# silently resolve to webui/auth.py instead, crashing every request
# with AttributeError: module 'auth' has no attribute 'any_admins_exist'
# on the very first call to auth.any_admins_exist() in login()/setup()
# below -- confirmed via the exact traceback from a live deployment.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webui"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import auth  # noqa: E402
import fleet  # noqa: E402
import actions  # noqa: E402
import config_store as store  # noqa: E402
import test_connections  # noqa: E402
import domain_logic  # noqa: E402
from dns_providers import PROVIDER_FIELDS  # noqa: E402
from deploy_providers import INSTANCE_FIELDS, TARGET_FIELDS  # noqa: E402
from cert_naming import safe_cert_name  # noqa: E402

PLACEHOLDER_EMAIL_DOMAINS = {
    "example.com", "example.org", "example.net", "example.edu",
    "test.com", "localhost", "invalid",
}
PRODUCTION_ACME_SERVER = "https://acme-v02.api.letsencrypt.org/directory"
STAGING_ACME_SERVER = "https://acme-staging-v02.api.letsencrypt.org/directory"

SECRET_KEY_PATH = os.environ.get(
    "ACME_MSP_CONSOLE_SECRET_KEY_FILE", "/etc/acme-appliance/msp-console/secret_key"
)
LOG_PATH = os.environ.get("ACME_MSP_CONSOLE_LOG", "/var/log/acme-appliance/msp-console.log")

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
    SESSION_COOKIE_SECURE=os.environ.get("ACME_MSP_CONSOLE_UI_TLS", "1") == "1",
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


def owner_required(view):
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        if not auth.is_owner(current_user()):
            abort(403, "This action requires an owner-level MSP Console account.")
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
    username = current_user()
    return {
        "csrf_token": csrf_token,
        "current_user": username,
        "current_user_is_owner": auth.is_owner(username) if username else False,
    }


def _console_log(message: str) -> None:
    """
    Dedicated audit log for the console itself -- every provisioning,
    deprovisioning, renewal-trigger, restart, and admin-account change
    is recorded here with WHO performed it, separate from any customer's
    own per-customer log. This matters more here than in the
    single-tenant web UI because multiple distinct people now have
    write access to actions that affect customers other than
    themselves.
    """
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        from datetime import datetime
        with open(LOG_PATH, "a") as f:
            stamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            f.write(f"{stamp} msp-console: {message}\n")
    except OSError:
        pass


def require_customer_read(slug: str):
    """
    Aborts with 404 (NOT 403) if the current admin cannot read this
    customer -- a 403 would confirm the customer slug exists at all,
    which is itself information a non-owner with zero grant on that
    slug should not be able to learn. This mirrors the same
    "fail-invisible rather than fail-explicit" philosophy already used
    for the single-instance profile's System Updates gating.
    """
    if not auth.can_read_customer(current_user(), slug):
        abort(404)


def require_customer_write(slug: str):
    if not auth.can_write_customer(current_user(), slug):
        abort(404)


def mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "****"
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


def _customer_config_path(slug: str) -> str:
    return os.path.join(fleet.CUSTOMERS_DIR, slug, "appliance.yaml")


def _customer_live_dir(slug: str) -> str:
    return os.path.join(fleet.CUSTOMERS_DIR, slug, "letsencrypt", "live")


def _load_customer_config(slug: str) -> dict:
    return store.load_config(config_path=_customer_config_path(slug))


def _save_customer_config(slug: str, cfg: dict) -> None:
    store.save_config(cfg, config_path=_customer_config_path(slug))


# ------------------------------------------------------------------ auth

@app.route("/setup", methods=["GET", "POST"])
def setup():
    if auth.any_admins_exist():
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
            # The FIRST account created here is always an owner -- there
            # is no bootstrap path that creates a non-owner first (that
            # would leave nobody able to grant anyone else access).
            totp_secret = auth.create_admin(username, password, is_owner=True, totp_enabled=enable_totp)
            if enable_totp:
                uri = auth.totp_provisioning_uri(username, totp_secret)
                flash(
                    "Owner account created. Add this secret to your authenticator "
                    f"app before logging in: {totp_secret}  ({uri})",
                    "success",
                )
            else:
                flash("Owner account created. You can now log in.", "success")
            _console_log(f"initial owner account '{username}' created")
            return redirect(url_for("login"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not auth.any_admins_exist():
        return redirect(url_for("setup"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if auth.verify_password(username, password):
            if auth.requires_totp(username):
                session["pending_totp_user"] = username
                return redirect(url_for("login_verify"))
            session["username"] = username
            _console_log(f"'{username}' logged in")
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
            _console_log(f"'{username}' logged in (MFA)")
            return redirect(url_for("dashboard"))
        flash("Invalid authentication code.", "error")
    return render_template("login_verify.html")


@app.route("/logout")
def logout():
    if current_user():
        _console_log(f"'{current_user()}' logged out")
    session.clear()
    return redirect(url_for("login"))


# ------------------------------------------------------------- dashboard

@app.route("/")
@login_required
def dashboard():
    all_slugs = fleet.list_customer_slugs()
    my_slugs = auth.accessible_customer_slugs(current_user(), all_slugs)
    statuses = fleet.fleet_status(my_slugs)
    unhealthy = [s for s in statuses if not s["healthy"]]
    return render_template(
        "dashboard.html",
        total_accessible=len(my_slugs),
        total_fleet=len(all_slugs),
        unhealthy=unhealthy,
        recent=statuses[:10],
    )


# ------------------------------------------------------------- customers

@app.route("/customers")
@login_required
def customers_list():
    all_slugs = fleet.list_customer_slugs()
    my_slugs = auth.accessible_customer_slugs(current_user(), all_slugs)
    rows = fleet.fleet_status(my_slugs)
    for row in rows:
        row["grant"] = auth.customer_grant_level(current_user(), row["slug"])
    return render_template("customers.html", rows=rows)


@app.route("/customers/new", methods=["GET", "POST"])
@owner_required
def customer_new():
    if request.method == "POST":
        check_csrf()
        slug = request.form.get("slug", "").strip().lower()
        if not fleet.is_valid_slug(slug):
            flash(
                "Invalid slug -- use lowercase letters, digits, and hyphens only "
                "(no leading/trailing hyphen), 23 characters or fewer.", "error",
            )
        elif slug in fleet.list_customer_slugs():
            flash(f"A customer named '{slug}' already exists.", "error")
        else:
            try:
                actions.provision_customer(slug)
                _console_log(f"'{current_user()}' provisioned customer '{slug}'")
                flash(
                    f"Customer '{slug}' provisioned. Add DNS provider(s), deploy target(s), "
                    "and domain(s) for this customer from its detail page below.",
                    "success",
                )
                return redirect(url_for("customer_detail", slug=slug))
            except actions.ActionError as exc:
                flash(f"Could not provision '{slug}': {exc}", "error")
    return render_template("customer_new.html")


@app.route("/customers/<slug>")
@login_required
def customer_detail(slug):
    require_customer_read(slug)
    status = fleet.customer_status(slug)
    grant = auth.customer_grant_level(current_user(), slug)
    cfg = _load_customer_config(slug)
    log_lines = None
    log_error = None
    try:
        log_lines = actions.tail_log(slug, lines=100)
    except actions.ActionError as exc:
        log_error = str(exc)
    return render_template(
        "customer_detail.html", status=status, grant=grant, cfg=cfg,
        log_lines=log_lines, log_error=log_error,
    )


@app.route("/customers/<slug>/renew", methods=["POST"])
@login_required
def customer_renew(slug):
    require_customer_write(slug)
    check_csrf()
    try:
        msg = actions.trigger_renew_customer(slug)
        _console_log(f"'{current_user()}' triggered renewal for all of '{slug}''s domains")
        flash(msg, "success")
    except actions.ActionError as exc:
        flash(f"Could not start renewal for '{slug}': {exc}", "error")
    return redirect(url_for("customer_detail", slug=slug))


@app.route("/customers/<slug>/deprovision", methods=["POST"])
@login_required
def customer_deprovision(slug):
    require_customer_write(slug)
    check_csrf()
    if request.form.get("confirm_slug", "").strip() != slug:
        flash("Confirmation text did not match the customer slug -- nothing was done.", "error")
        return redirect(url_for("customer_detail", slug=slug))
    try:
        actions.deprovision_customer(slug)
        _console_log(f"'{current_user()}' deprovisioned customer '{slug}'")
        flash(f"Customer '{slug}' deprovisioned. Config/certs archived on the host.", "success")
        return redirect(url_for("customers_list"))
    except actions.ActionError as exc:
        flash(f"Could not deprovision '{slug}': {exc}", "error")
        return redirect(url_for("customer_detail", slug=slug))


@app.route("/customers/<slug>/log-fragment")
@login_required
def customer_log_fragment(slug):
    """Small JSON endpoint used by the detail page's manual-refresh log tail."""
    require_customer_read(slug)
    try:
        return jsonify({"ok": True, "log": actions.tail_log(slug, lines=100)})
    except actions.ActionError as exc:
        return jsonify({"ok": False, "error": str(exc)})


# ---------------------------------------------------------- customer settings

@app.route("/customers/<slug>/settings", methods=["GET", "POST"])
@login_required
def customer_settings(slug):
    require_customer_read(slug)
    cfg = _load_customer_config(slug)
    if request.method == "POST":
        require_customer_write(slug)
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
            _save_customer_config(slug, cfg)
            _console_log(f"'{current_user()}' updated ACME settings for '{slug}'")
            flash("ACME settings updated.", "success")
            return redirect(url_for("customer_settings", slug=slug))

    current_server = cfg["acme"].get("server", "")
    if current_server == PRODUCTION_ACME_SERVER:
        server_choice = "production"
    elif current_server == STAGING_ACME_SERVER:
        server_choice = "staging"
    else:
        server_choice = "custom"

    grant = auth.customer_grant_level(current_user(), slug)
    return render_template(
        "customer_settings.html", slug=slug, cfg=cfg, grant=grant, server_choice=server_choice,
        production_server=PRODUCTION_ACME_SERVER, staging_server=STAGING_ACME_SERVER,
    )


# --------------------------------------------------------- customer DNS providers

@app.route("/customers/<slug>/dns-providers")
@login_required
def customer_dns_providers_list(slug):
    require_customer_read(slug)
    cfg = _load_customer_config(slug)
    grant = auth.customer_grant_level(current_user(), slug)
    provider_labels = {k: v["label"] for k, v in PROVIDER_FIELDS.items()}
    return render_template(
        "customer_dns_providers.html", slug=slug, grant=grant,
        providers=cfg["dns_providers"], provider_labels=provider_labels, mask=mask,
    )


@app.route("/customers/<slug>/dns-providers/new", methods=["GET", "POST"])
@login_required
def customer_dns_provider_new(slug):
    require_customer_write(slug)
    cfg = _load_customer_config(slug)
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
            settings = domain_logic.settings_from_form(PROVIDER_FIELDS, provider_type, existing_settings={}, form=request.form)
            store.upsert_dns_provider(cfg, instance_name, provider_type, settings)
            _save_customer_config(slug, cfg)
            _console_log(f"'{current_user()}' added DNS provider '{instance_name}' for '{slug}'")
            flash(f"Added DNS provider '{instance_name}'.", "success")
            return redirect(url_for("customer_dns_providers_list", slug=slug))
        selected_type = provider_type or selected_type
    return render_template(
        "customer_dns_provider_form.html", slug=slug, mode="new", instance_name="",
        provider_fields=PROVIDER_FIELDS, selected_type=selected_type, existing_settings={},
    )


@app.route("/customers/<slug>/dns-providers/<instance_name>/edit", methods=["GET", "POST"])
@login_required
def customer_dns_provider_edit(slug, instance_name):
    require_customer_write(slug)
    cfg = _load_customer_config(slug)
    instance = cfg["dns_providers"].get(instance_name)
    if not instance:
        abort(404)
    provider_type = instance["type"]
    if request.method == "POST":
        check_csrf()
        settings = domain_logic.settings_from_form(
            PROVIDER_FIELDS, provider_type, existing_settings=instance.get("settings", {}), form=request.form
        )
        store.upsert_dns_provider(cfg, instance_name, provider_type, settings)
        _save_customer_config(slug, cfg)
        _console_log(f"'{current_user()}' updated DNS provider '{instance_name}' for '{slug}'")
        flash(f"Updated DNS provider '{instance_name}'.", "success")
        return redirect(url_for("customer_dns_providers_list", slug=slug))
    return render_template(
        "customer_dns_provider_form.html", slug=slug, mode="edit", instance_name=instance_name,
        provider_fields=PROVIDER_FIELDS, selected_type=provider_type,
        existing_settings=instance.get("settings", {}),
    )


@app.route("/customers/<slug>/dns-providers/<instance_name>/delete", methods=["POST"])
@login_required
def customer_dns_provider_delete(slug, instance_name):
    require_customer_write(slug)
    check_csrf()
    cfg = _load_customer_config(slug)
    used_by = store.dns_provider_in_use(cfg, instance_name)
    if used_by:
        flash(f"Cannot delete '{instance_name}' -- still used by domain(s): {', '.join(used_by)}.", "error")
    else:
        store.delete_dns_provider(cfg, instance_name)
        _save_customer_config(slug, cfg)
        _console_log(f"'{current_user()}' deleted DNS provider '{instance_name}' for '{slug}'")
        flash(f"Deleted DNS provider '{instance_name}'.", "success")
    return redirect(url_for("customer_dns_providers_list", slug=slug))


@app.route("/customers/<slug>/dns-providers/<instance_name>/test", methods=["POST"])
@login_required
def customer_dns_provider_test(slug, instance_name):
    require_customer_write(slug)
    check_csrf()
    cfg = _load_customer_config(slug)
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
    return redirect(url_for("customer_dns_providers_list", slug=slug))


# ------------------------------------------------------ customer deploy providers

@app.route("/customers/<slug>/deploy-providers")
@login_required
def customer_deploy_providers_list(slug):
    require_customer_read(slug)
    cfg = _load_customer_config(slug)
    grant = auth.customer_grant_level(current_user(), slug)
    provider_labels = {k: v["label"] for k, v in INSTANCE_FIELDS.items()}
    return render_template(
        "customer_deploy_providers.html", slug=slug, grant=grant,
        providers=cfg["deploy_providers"], provider_labels=provider_labels, mask=mask,
    )


@app.route("/customers/<slug>/deploy-providers/new", methods=["GET", "POST"])
@login_required
def customer_deploy_provider_new(slug):
    require_customer_write(slug)
    cfg = _load_customer_config(slug)
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
            settings = domain_logic.settings_from_form(INSTANCE_FIELDS, provider_type, existing_settings={}, form=request.form)
            store.upsert_deploy_provider(cfg, instance_name, provider_type, settings)
            _save_customer_config(slug, cfg)
            _console_log(f"'{current_user()}' added deploy target '{instance_name}' for '{slug}'")
            flash(f"Added deploy target '{instance_name}'.", "success")
            return redirect(url_for("customer_deploy_providers_list", slug=slug))
        selected_type = provider_type or selected_type
    return render_template(
        "customer_deploy_provider_form.html", slug=slug, mode="new", instance_name="",
        provider_fields=INSTANCE_FIELDS, selected_type=selected_type, existing_settings={},
    )


@app.route("/customers/<slug>/deploy-providers/<instance_name>/edit", methods=["GET", "POST"])
@login_required
def customer_deploy_provider_edit(slug, instance_name):
    require_customer_write(slug)
    cfg = _load_customer_config(slug)
    instance = cfg["deploy_providers"].get(instance_name)
    if not instance:
        abort(404)
    provider_type = instance["type"]
    if request.method == "POST":
        check_csrf()
        settings = domain_logic.settings_from_form(
            INSTANCE_FIELDS, provider_type, existing_settings=instance.get("settings", {}), form=request.form
        )
        store.upsert_deploy_provider(cfg, instance_name, provider_type, settings)
        _save_customer_config(slug, cfg)
        _console_log(f"'{current_user()}' updated deploy target '{instance_name}' for '{slug}'")
        flash(f"Updated deploy target '{instance_name}'.", "success")
        return redirect(url_for("customer_deploy_providers_list", slug=slug))
    return render_template(
        "customer_deploy_provider_form.html", slug=slug, mode="edit", instance_name=instance_name,
        provider_fields=INSTANCE_FIELDS, selected_type=provider_type,
        existing_settings=instance.get("settings", {}),
    )


@app.route("/customers/<slug>/deploy-providers/<instance_name>/delete", methods=["POST"])
@login_required
def customer_deploy_provider_delete(slug, instance_name):
    require_customer_write(slug)
    check_csrf()
    cfg = _load_customer_config(slug)
    used_by = store.deploy_provider_in_use(cfg, instance_name)
    if used_by:
        flash(f"Cannot delete '{instance_name}' -- still used by domain(s): {', '.join(used_by)}.", "error")
    else:
        store.delete_deploy_provider(cfg, instance_name)
        _save_customer_config(slug, cfg)
        _console_log(f"'{current_user()}' deleted deploy target '{instance_name}' for '{slug}'")
        flash(f"Deleted deploy target '{instance_name}'.", "success")
    return redirect(url_for("customer_deploy_providers_list", slug=slug))


@app.route("/customers/<slug>/deploy-providers/<instance_name>/test", methods=["POST"])
@login_required
def customer_deploy_provider_test(slug, instance_name):
    require_customer_write(slug)
    check_csrf()
    cfg = _load_customer_config(slug)
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
    return redirect(url_for("customer_deploy_providers_list", slug=slug))


@app.route("/customers/<slug>/deploy-providers/<instance_name>/options")
@login_required
def customer_deploy_provider_options(slug, instance_name):
    require_customer_read(slug)
    cfg = _load_customer_config(slug)
    instance = cfg["deploy_providers"].get(instance_name)
    if instance is None:
        return jsonify({"ok": False, "error": f"Unknown deploy target '{instance_name}'"}), 404
    kwargs = {}
    if request.args.get("vsys"):
        kwargs["vsys"] = request.args.get("vsys")
    ok, result = test_connections.list_target_options(instance["type"], instance.get("settings", {}), **kwargs)
    if ok:
        return jsonify({"ok": True, "options": result})
    return jsonify({"ok": False, "error": result})


# ------------------------------------------------------------- customer domains

@app.route("/customers/<slug>/domains")
@login_required
def customer_domains_list(slug):
    require_customer_read(slug)
    cfg = _load_customer_config(slug)
    grant = auth.customer_grant_level(current_user(), slug)
    live_dir = _customer_live_dir(slug)
    rows = []
    for d in cfg["domains"]:
        rows.append({
            "entry": d,
            "additional_names_display": domain_logic.additional_names_display(d),
            "deploy_targets_display": domain_logic.deploy_targets_display(d, cfg["deploy_providers"]),
            "cert_expiry": domain_logic.cert_expiry(d["name"], live_dir),
            "renewal_in_progress": fleet.renewal_in_progress(slug, d["name"]),
            "redeploy_in_progress": fleet.redeploy_in_progress(slug, d["name"]),
            "has_cert": domain_logic.cert_lineage_dir(d["name"], live_dir) is not None,
        })
    return render_template(
        "customer_domains.html", slug=slug, grant=grant, rows=rows,
        any_renewal_in_progress=fleet.renewal_in_progress(slug),
    )


@app.route("/customers/<slug>/domains/new", methods=["GET", "POST"])
@login_required
def customer_domain_new(slug):
    require_customer_write(slug)
    cfg = _load_customer_config(slug)
    if request.method == "POST":
        check_csrf()
        name, entry, error = domain_logic.domain_from_form(cfg, request.form, TARGET_FIELDS)
        if error:
            flash(error, "error")
        elif store.get_domain(cfg, name):
            flash(f"A domain entry for '{name}' already exists.", "error")
        else:
            store.upsert_domain(cfg, name, entry)
            _save_customer_config(slug, cfg)
            _console_log(f"'{current_user()}' added domain '{name}' for '{slug}'")
            flash(f"Added domain {name}.", "success")
            return redirect(url_for("customer_domains_list", slug=slug))
    return render_template(
        "customer_domain_form.html", slug=slug, mode="new", entry=None, has_cert=False,
        redeploy_in_progress=False, additional_names_for_form=[], deploy_targets_for_form=[],
        providers=cfg["dns_providers"], deploy_providers=cfg["deploy_providers"],
        target_fields_schema=TARGET_FIELDS,
    )


@app.route("/customers/<slug>/domains/<path:name>/edit", methods=["GET", "POST"])
@login_required
def customer_domain_edit(slug, name):
    require_customer_write(slug)
    cfg = _load_customer_config(slug)
    entry = store.get_domain(cfg, name)
    if not entry:
        abort(404)
    if request.method == "POST":
        check_csrf()
        new_name, new_entry, error = domain_logic.domain_from_form(cfg, request.form, TARGET_FIELDS)
        if error:
            flash(error, "error")
        else:
            store.delete_domain(cfg, name)
            store.upsert_domain(cfg, new_name, new_entry)
            _save_customer_config(slug, cfg)
            _console_log(f"'{current_user()}' updated domain '{new_name}' for '{slug}'")
            flash(f"Updated domain {new_name}.", "success")
            return redirect(url_for("customer_domains_list", slug=slug))
    live_dir = _customer_live_dir(slug)
    return render_template(
        "customer_domain_form.html", slug=slug, mode="edit", entry=entry,
        has_cert=domain_logic.cert_lineage_dir(name, live_dir) is not None,
        redeploy_in_progress=fleet.redeploy_in_progress(slug, name),
        additional_names_for_form=domain_logic.additional_names_for_form(entry),
        deploy_targets_for_form=entry.get("deploy_targets", []),
        providers=cfg["dns_providers"], deploy_providers=cfg["deploy_providers"],
        target_fields_schema=TARGET_FIELDS,
    )


@app.route("/customers/<slug>/domains/<path:name>/delete", methods=["POST"])
@login_required
def customer_domain_delete(slug, name):
    require_customer_write(slug)
    check_csrf()
    cfg = _load_customer_config(slug)
    store.delete_domain(cfg, name)
    _save_customer_config(slug, cfg)
    _console_log(f"'{current_user()}' deleted domain '{name}' for '{slug}'")
    flash(f"Deleted domain {name}.", "success")
    return redirect(url_for("customer_domains_list", slug=slug))


@app.route("/customers/<slug>/domains/<path:name>/renew", methods=["POST"])
@login_required
def customer_domain_renew(slug, name):
    require_customer_write(slug)
    check_csrf()
    cfg = _load_customer_config(slug)
    if not store.get_domain(cfg, name):
        abort(404)
    force = request.form.get("force") == "on"
    try:
        msg = actions.trigger_renew_domain(slug, name, force=force)
        _console_log(f"'{current_user()}' triggered renewal for '{name}' ('{slug}'){' [forced]' if force else ''}")
        flash(msg, "success")
    except actions.ActionError as exc:
        flash(f"Could not start renewal for '{name}': {exc}", "error")
    return redirect(url_for("customer_domains_list", slug=slug))


@app.route("/customers/<slug>/domains/<path:name>/redeploy", methods=["POST"])
@login_required
def customer_domain_redeploy(slug, name):
    require_customer_write(slug)
    check_csrf()
    cfg = _load_customer_config(slug)
    if not store.get_domain(cfg, name):
        abort(404)
    live_dir = _customer_live_dir(slug)
    if not domain_logic.cert_lineage_dir(name, live_dir):
        flash(f"No certificate has been issued yet for '{name}' -- nothing to redeploy. Use 'Renew now' first.", "error")
        return redirect(url_for("customer_domains_list", slug=slug))
    try:
        msg = actions.trigger_redeploy_domain(slug, name)
        _console_log(f"'{current_user()}' triggered redeploy for '{name}' ('{slug}')")
        flash(msg, "success")
    except actions.ActionError as exc:
        flash(f"Could not start redeploy for '{name}': {exc}", "error")
    return redirect(url_for("customer_domains_list", slug=slug))


@app.route("/customers/<slug>/domains/<path:name>/download")
@login_required
def customer_domain_download(slug, name):
    require_customer_read(slug)
    cfg = _load_customer_config(slug)
    if not store.get_domain(cfg, name):
        abort(404)
    live_dir = _customer_live_dir(slug)
    lineage_dir = domain_logic.cert_lineage_dir(name, live_dir)
    if not lineage_dir:
        flash(f"No certificate has been issued yet for '{name}'.", "error")
        return redirect(url_for("customer_domains_list", slug=slug))

    buf = io.BytesIO()
    included = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in ("fullchain.pem", "cert.pem", "chain.pem", "privkey.pem"):
            fpath = os.path.join(lineage_dir, fname)
            if os.path.exists(fpath):
                zf.write(fpath, arcname=fname)
                included.append(fname)
    buf.seek(0)

    _console_log(
        f"'{current_user()}' downloaded certificate files for '{name}' ('{slug}') "
        f"({', '.join(included)})"
    )

    zip_name = f"{safe_cert_name(name)}-certs.zip"
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=zip_name)


# ---------------------------------------------------------------- admins

@app.route("/admins")
@owner_required
def admins_list():
    data = auth.load_admins()
    return render_template("admins.html", admins=data.get("admins", {}))


@app.route("/admins/new", methods=["GET", "POST"])
@owner_required
def admin_new():
    all_slugs = fleet.list_customer_slugs()
    if request.method == "POST":
        check_csrf()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        is_owner_flag = request.form.get("is_owner") == "on"
        grants = _grants_from_form(all_slugs)
        if not username or not password:
            flash("Username and password are required.", "error")
        elif password != confirm:
            flash("Passwords do not match.", "error")
        elif len(password) < 10:
            flash("Password must be at least 10 characters.", "error")
        elif auth.get_admin(username):
            flash(f"An admin named '{username}' already exists.", "error")
        else:
            auth.create_admin(username, password, is_owner=is_owner_flag, customer_grants=grants)
            _console_log(
                f"'{current_user()}' created admin '{username}' "
                f"({'owner' if is_owner_flag else f'{len(grants)} customer grant(s)'})"
            )
            flash(f"Admin '{username}' created.", "success")
            return redirect(url_for("admins_list"))
    return render_template("admin_form.html", mode="new", username="", admin={}, all_slugs=all_slugs)


@app.route("/admins/<username>/edit", methods=["GET", "POST"])
@owner_required
def admin_edit(username):
    admin = auth.get_admin(username)
    if not admin:
        abort(404)
    all_slugs = fleet.list_customer_slugs()
    if request.method == "POST":
        check_csrf()
        is_owner_flag = request.form.get("is_owner") == "on"
        if admin.get("is_owner") and not is_owner_flag and auth.count_owners() <= 1:
            flash("Cannot remove owner status from the last remaining owner account.", "error")
        else:
            grants = _grants_from_form(all_slugs)
            auth.update_admin_grants(username, is_owner_flag, grants)
            _console_log(f"'{current_user()}' updated grants for admin '{username}'")
            flash(f"Updated '{username}'.", "success")
            return redirect(url_for("admins_list"))
    return render_template("admin_form.html", mode="edit", username=username, admin=admin, all_slugs=all_slugs)


@app.route("/admins/<username>/delete", methods=["POST"])
@owner_required
def admin_delete(username):
    check_csrf()
    if username == current_user():
        flash("You cannot delete your own account while logged in as it.", "error")
        return redirect(url_for("admins_list"))
    admin = auth.get_admin(username)
    if admin.get("is_owner") and auth.count_owners() <= 1:
        flash("Cannot delete the last remaining owner account.", "error")
        return redirect(url_for("admins_list"))
    auth.delete_admin(username)
    _console_log(f"'{current_user()}' deleted admin '{username}'")
    flash(f"Deleted admin '{username}'.", "success")
    return redirect(url_for("admins_list"))


def _grants_from_form(all_slugs: list) -> dict:
    grants = {}
    for slug in all_slugs:
        level = request.form.get(f"grant__{slug}", "")
        if level in auth.VALID_LEVELS:
            grants[slug] = level
    return grants


# --------------------------------------------------------------- account

@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    username = current_user()
    admin = auth.get_admin(username)
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
    return render_template(
        "account.html", username=username,
        is_owner=admin.get("is_owner", False),
        totp_enabled=admin.get("totp_enabled", False),
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=9443, debug=True)
