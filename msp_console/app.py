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
import os
import secrets
import sys

from flask import (
    Flask, render_template, request, redirect, url_for, session, flash, abort, jsonify
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth  # noqa: E402
import fleet  # noqa: E402
import actions  # noqa: E402

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
                    f"Customer '{slug}' provisioned. Add the nginx server block shown "
                    "in the CLI output (or see deploy/nginx/acme-appliance-msp.conf.template) "
                    "to make it reachable, then have them visit /setup on their instance.",
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
    log_lines = None
    log_error = None
    try:
        log_lines = actions.tail_log(slug, lines=100)
    except actions.ActionError as exc:
        log_error = str(exc)
    return render_template(
        "customer_detail.html", status=status, grant=grant,
        log_lines=log_lines, log_error=log_error,
    )


@app.route("/customers/<slug>/renew", methods=["POST"])
@login_required
def customer_renew(slug):
    require_customer_write(slug)
    check_csrf()
    try:
        actions.trigger_renew(slug)
        _console_log(f"'{current_user()}' triggered renewal for '{slug}'")
        flash(f"Renewal check started for '{slug}' in the background.", "success")
    except actions.ActionError as exc:
        flash(f"Could not start renewal for '{slug}': {exc}", "error")
    return redirect(url_for("customer_detail", slug=slug))


@app.route("/customers/<slug>/restart", methods=["POST"])
@login_required
def customer_restart(slug):
    require_customer_write(slug)
    check_csrf()
    try:
        actions.restart_webui(slug)
        _console_log(f"'{current_user()}' restarted web UI for '{slug}'")
        flash(f"Web UI restarted for '{slug}'.", "success")
    except actions.ActionError as exc:
        flash(f"Could not restart web UI for '{slug}': {exc}", "error")
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
