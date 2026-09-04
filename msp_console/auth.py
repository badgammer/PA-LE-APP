"""
MSP Console admin authentication + authorization.

Deliberately mirrors webui/auth.py's proven pattern (bcrypt-hashed
passwords, optional TOTP MFA, in-memory failed-attempt lockout) --
same dependencies, same security properties, same YAML-backed storage
approach -- but for a DIFFERENT, separate set of accounts: MSP staff who
manage the fleet, not any one customer's own admin login. These two
account stores never overlap and never share credentials; an MSP staff
account grants no access whatsoever to a customer's own web UI login,
and vice versa.

### Authorization model

Every admin record has:
    is_owner          bool  -- owners have implicit read+write on EVERY
                               customer, can manage other admin accounts,
                               and can provision/deprovision customers.
                               customer_grants is ignored for owners.
    customer_grants   dict  -- {customer_slug: "read"|"write"}, consulted
                               ONLY for non-owners. A customer slug that
                               does not appear here is completely
                               invisible to that admin -- not just
                               hidden UI, every route enforces this (see
                               require_customer_access() below), so a
                               non-owner literally cannot discover
                               whether a customer they lack access to
                               even exists.

"write" on a customer implies "read" on that same customer. There is no
concept of write-without-read.
"""
import fcntl
import os
import time

import bcrypt
import pyotp
import yaml

ADMINS_PATH = os.environ.get("ACME_MSP_CONSOLE_ADMINS", "/etc/acme-appliance/msp-console/admins.yaml")
LOCK_PATH = ADMINS_PATH + ".lock"

_FAILED_ATTEMPTS = {}
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 300

VALID_LEVELS = ("read", "write")


def _ensure_parent_dir():
    os.makedirs(os.path.dirname(ADMINS_PATH), exist_ok=True)


def load_admins() -> dict:
    _ensure_parent_dir()
    if not os.path.exists(ADMINS_PATH):
        return {"admins": {}}
    with open(ADMINS_PATH, "r") as f:
        return yaml.safe_load(f) or {"admins": {}}


def save_admins(data: dict) -> None:
    _ensure_parent_dir()
    with open(LOCK_PATH, "w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            tmp_path = ADMINS_PATH + ".tmp"
            with open(tmp_path, "w") as f:
                yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, ADMINS_PATH)
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)


def any_admins_exist() -> bool:
    return bool(load_admins().get("admins"))


def create_admin(username: str, password: str, is_owner: bool,
                  customer_grants: dict = None, totp_enabled: bool = False) -> str:
    data = load_admins()
    pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    totp_secret = pyotp.random_base32() if totp_enabled else ""
    data.setdefault("admins", {})[username] = {
        "password_hash": pw_hash,
        "totp_secret": totp_secret,
        "totp_enabled": totp_enabled,
        "is_owner": bool(is_owner),
        "customer_grants": dict(customer_grants or {}) if not is_owner else {},
    }
    save_admins(data)
    return totp_secret


def update_admin_grants(username: str, is_owner: bool, customer_grants: dict) -> None:
    data = load_admins()
    admin = data["admins"][username]
    admin["is_owner"] = bool(is_owner)
    admin["customer_grants"] = dict(customer_grants) if not is_owner else {}
    save_admins(data)


def set_password(username: str, new_password: str) -> None:
    data = load_admins()
    admin = data["admins"][username]
    admin["password_hash"] = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    save_admins(data)


def set_totp(username: str, enabled: bool) -> str:
    data = load_admins()
    admin = data["admins"][username]
    if enabled and not admin.get("totp_secret"):
        admin["totp_secret"] = pyotp.random_base32()
    admin["totp_enabled"] = enabled
    save_admins(data)
    return admin.get("totp_secret", "")


def delete_admin(username: str) -> None:
    data = load_admins()
    data.get("admins", {}).pop(username, None)
    save_admins(data)


def count_owners(data: dict = None) -> int:
    data = data or load_admins()
    return sum(1 for a in data.get("admins", {}).values() if a.get("is_owner"))


def get_admin(username: str) -> dict:
    return load_admins().get("admins", {}).get(username, {})


# --------------------------------------------------------------- lockout

def _is_locked_out(username: str) -> bool:
    entry = _FAILED_ATTEMPTS.get(username)
    if not entry:
        return False
    count, locked_at = entry
    if count >= MAX_ATTEMPTS and (time.time() - locked_at) < LOCKOUT_SECONDS:
        return True
    if count >= MAX_ATTEMPTS and (time.time() - locked_at) >= LOCKOUT_SECONDS:
        _FAILED_ATTEMPTS.pop(username, None)
    return False


def _record_failure(username: str) -> None:
    count, _ = _FAILED_ATTEMPTS.get(username, (0, time.time()))
    _FAILED_ATTEMPTS[username] = (count + 1, time.time())


def _clear_failures(username: str) -> None:
    _FAILED_ATTEMPTS.pop(username, None)


def verify_password(username: str, password: str) -> bool:
    if _is_locked_out(username):
        return False
    admin = get_admin(username)
    if not admin:
        _record_failure(username)
        return False
    ok = bcrypt.checkpw(password.encode("utf-8"), admin["password_hash"].encode("utf-8"))
    if ok:
        _clear_failures(username)
    else:
        _record_failure(username)
    return ok


def requires_totp(username: str) -> bool:
    admin = get_admin(username)
    return bool(admin.get("totp_enabled") and admin.get("totp_secret"))


def verify_totp(username: str, code: str) -> bool:
    admin = get_admin(username)
    secret = admin.get("totp_secret")
    if not secret:
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def totp_provisioning_uri(username: str, secret: str, issuer: str = "ACME MSP Console") -> str:
    return pyotp.totp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


# ----------------------------------------------------------- permissions

def is_owner(username: str) -> bool:
    return bool(get_admin(username).get("is_owner"))


def customer_grant_level(username: str, slug: str) -> str:
    """
    Returns "write", "read", or "" (no access) for this admin on this
    specific customer slug. Owners always get "write" regardless of
    customer_grants (which is ignored/cleared for owner accounts).
    """
    admin = get_admin(username)
    if admin.get("is_owner"):
        return "write"
    return admin.get("customer_grants", {}).get(slug, "")


def can_read_customer(username: str, slug: str) -> bool:
    return customer_grant_level(username, slug) in ("read", "write")


def can_write_customer(username: str, slug: str) -> bool:
    return customer_grant_level(username, slug) == "write"


def accessible_customer_slugs(username: str, all_slugs: list) -> list:
    """
    Filters a list of every customer slug on the fleet down to just the
    ones this admin is allowed to even see -- owners see everything;
    non-owners see only slugs present in their own customer_grants.
    """
    if is_owner(username):
        return list(all_slugs)
    admin = get_admin(username)
    grants = admin.get("customer_grants", {})
    return [s for s in all_slugs if s in grants]
