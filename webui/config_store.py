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
    "deploy_providers": {},
    "domains": [],
}


def _ensure_parent_dirs():
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)


def _migrate_legacy_config(cfg: dict) -> bool:
    """
    One-time, in-memory migration of pre-multi-target appliance.yaml
    files (from before the "single pane of glass" deploy_providers/
    generalization) into the current schema:
      - the old top-level panos_firewalls[] map becomes deploy_providers[]
        (each entry tagged type: "panos", with its old settings kept as-is).
      - each domain's old panos_targets[] list becomes deploy_targets[],
        with each {"firewall": ..., "ssl_tls_profile"/"globalprotect_portal": ...,
        "vsys": ...} row rewritten to {"target": ..., "cert_field_type": ...,
        "cert_field_value": ..., "vsys": ...}.
    Returns True if anything was migrated, in which case the caller
    should persist the result via save_config() so this only ever runs
    once per appliance (subsequent loads will find deploy_providers/
    deploy_targets already in place and skip migration entirely).
    """
    migrated = False

    if "panos_firewalls" in cfg:
        cfg.setdefault("deploy_providers", {})
        for name, settings in cfg.pop("panos_firewalls").items():
            cfg["deploy_providers"].setdefault(name, {"type": "panos", "settings": settings})
        migrated = True

    for d in cfg.get("domains", []):
        if "panos_targets" in d:
            new_targets = []
            for t in d.pop("panos_targets"):
                entry = {"target": t["firewall"]}
                if t.get("globalprotect_portal"):
                    entry["cert_field_type"] = "globalprotect_portal"
                    entry["cert_field_value"] = t["globalprotect_portal"]
                else:
                    entry["cert_field_type"] = "ssl_tls_profile"
                    entry["cert_field_value"] = t.get("ssl_tls_profile", "")
                if t.get("vsys"):
                    entry["vsys"] = t["vsys"]
                new_targets.append(entry)
            d["deploy_targets"] = new_targets
            migrated = True

    return migrated


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
    if _migrate_legacy_config(cfg):
        save_config(cfg)
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
    """
    Returns the list of domain entry names that reference this DNS
    provider instance -- either as the entry's own top-level
    dns_provider, OR as a per-name override on one of its
    additional_names (SANs). Checking both is required before allowing
    deletion of a provider instance, since a per-name override alone
    would otherwise be silently orphaned.
    """
    used = []
    for d in cfg["domains"]:
        if d.get("dns_provider") == instance_name:
            used.append(d["name"])
            continue
        for item in d.get("additional_names", []):
            if isinstance(item, dict) and item.get("dns_provider") == instance_name:
                used.append(d["name"])
                break
    return used


def upsert_deploy_provider(cfg: dict, name: str, provider_type: str, settings: dict) -> None:
    cfg["deploy_providers"][name] = {"type": provider_type, "settings": settings}


def delete_deploy_provider(cfg: dict, name: str) -> None:
    cfg["deploy_providers"].pop(name, None)


def deploy_provider_in_use(cfg: dict, name: str) -> list:
    used = []
    for d in cfg["domains"]:
        for t in d.get("deploy_targets", []):
            if t.get("target") == name:
                used.append(d["name"])
                break
    return used
