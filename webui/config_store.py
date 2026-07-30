"""Safe, atomic read/write access to appliance.yaml for the web UI."""
import copy
import datetime
import fcntl
import os
import shutil
from typing import Optional

import yaml

CONFIG_PATH = os.environ.get("ACME_APPLIANCE_CONFIG", "/etc/acme-appliance/appliance.yaml")
BACKUP_DIR = os.path.join(os.path.dirname(CONFIG_PATH), "backups")
LOCK_PATH = CONFIG_PATH + ".lock"

DEFAULT_CONFIG = {
    "acme": {"email": "", "server": "https://acme-v02.api.letsencrypt.org/directory"},
    "dns_providers": {},
    "panos_firewalls": {},
    "domains": [],
}


def _ensure_parent_dirs():
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)


def load_config() -> dict:
    _ensure_parent_dirs()
    if not os.path.exists(CONFIG_PATH):
        return copy.deepcopy(DEFAULT_CONFIG)
    with open(CONFIG_PATH, "r") as f:
        cfg = yaml.safe_load(f) or {}
    for key, default in DEFAULT_CONFIG.items():
        cfg.setdefault(key, copy.deepcopy(default))
    cfg["acme"].setdefault("email", "")
    cfg["acme"].setdefault("server", DEFAULT_CONFIG["acme"]["server"])
    return cfg


def save_config(cfg: dict) -> None:
    _ensure_parent_dirs()
    with open(LOCK_PATH, "w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            if os.path.exists(CONFIG_PATH):
                stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
                shutil.copy2(CONFIG_PATH, os.path.join(BACKUP_DIR, f"appliance-{stamp}.yaml"))
                _prune_old_backups()
            tmp_path = CONFIG_PATH + ".tmp"
            with open(tmp_path, "w") as f:
                yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, CONFIG_PATH)
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)


def _prune_old_backups(keep: int = 30) -> None:
    backups = sorted((os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR)), key=os.path.getmtime)
    for path in backups[:-keep]:
        try:
            os.remove(path)
        except OSError:
            pass


def get_domain(cfg: dict, name: str) -> Optional[dict]:
    for d in cfg["domains"]:
        if d["name"] == name:
            return d
    return None


def upsert_domain(cfg: dict, name: str, new_entry: dict) -> None:
    for i, d in enumerate(cfg["domains"]):
        if d["name"] == name:
            cfg["domains"][i] = new_entry
            return
    cfg["domains"].append(new_entry)


def delete_domain(cfg: dict, name: str) -> None:
    cfg["domains"] = [d for d in cfg["domains"] if d["name"] != name]


def upsert_dns_provider(cfg: dict, instance_name: str, provider_type: str, settings: dict) -> None:
    cfg["dns_providers"][instance_name] = {"type": provider_type, "settings": settings}


def delete_dns_provider(cfg: dict, instance_name: str) -> None:
    cfg["dns_providers"].pop(instance_name, None)


def dns_provider_in_use(cfg: dict, instance_name: str) -> list:
    return [d["name"] for d in cfg["domains"] if d.get("dns_provider") == instance_name]


def upsert_firewall(cfg: dict, name: str, settings: dict) -> None:
    cfg["panos_firewalls"][name] = settings


def delete_firewall(cfg: dict, name: str) -> None:
    cfg["panos_firewalls"].pop(name, None)


def firewall_in_use(cfg: dict, name: str) -> list:
    used = []
    for d in cfg["domains"]:
        for t in d.get("panos_targets", []):
            if t.get("firewall") == name:
                used.append(d["name"])
                break
    return used
