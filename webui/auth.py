"""Minimal local authentication for the appliance web UI: bcrypt-hashed passwords plus optional TOTP MFA."""
import fcntl
import os
import time

import bcrypt
import pyotp
import yaml

USERS_PATH = os.environ.get("ACME_APPLIANCE_USERS", "/etc/acme-appliance/users.yaml")
LOCK_PATH = USERS_PATH + ".lock"

_FAILED_ATTEMPTS = {}
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 300


def _ensure_parent_dir():
    os.makedirs(os.path.dirname(USERS_PATH), exist_ok=True)


def load_users() -> dict:
    _ensure_parent_dir()
    if not os.path.exists(USERS_PATH):
        return {"users": {}}
    with open(USERS_PATH, "r") as f:
        return yaml.safe_load(f) or {"users": {}}


def save_users(data: dict) -> None:
    _ensure_parent_dir()
    with open(LOCK_PATH, "w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            tmp_path = USERS_PATH + ".tmp"
            with open(tmp_path, "w") as f:
                yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, USERS_PATH)
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)


def any_users_exist() -> bool:
    return bool(load_users().get("users"))


def create_user(username: str, password: str, totp_enabled: bool = False) -> str:
    data = load_users()
    pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    totp_secret = pyotp.random_base32() if totp_enabled else ""
    data.setdefault("users", {})[username] = {
        "password_hash": pw_hash, "totp_secret": totp_secret, "totp_enabled": totp_enabled,
    }
    save_users(data)
    return totp_secret


def set_password(username: str, new_password: str) -> None:
    data = load_users()
    user = data["users"][username]
    user["password_hash"] = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    save_users(data)


def set_totp(username: str, enabled: bool) -> str:
    data = load_users()
    user = data["users"][username]
    if enabled and not user.get("totp_secret"):
        user["totp_secret"] = pyotp.random_base32()
    user["totp_enabled"] = enabled
    save_users(data)
    return user.get("totp_secret", "")


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
    data = load_users()
    user = data.get("users", {}).get(username)
    if not user:
        _record_failure(username)
        return False
    ok = bcrypt.checkpw(password.encode("utf-8"), user["password_hash"].encode("utf-8"))
    if ok:
        _clear_failures(username)
    else:
        _record_failure(username)
    return ok


def requires_totp(username: str) -> bool:
    data = load_users()
    user = data.get("users", {}).get(username, {})
    return bool(user.get("totp_enabled") and user.get("totp_secret"))


def verify_totp(username: str, code: str) -> bool:
    data = load_users()
    user = data.get("users", {}).get(username, {})
    secret = user.get("totp_secret")
    if not secret:
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def totp_provisioning_uri(username: str, secret: str, issuer: str = "ACME Appliance") -> str:
    return pyotp.totp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)
