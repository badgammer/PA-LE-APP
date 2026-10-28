#!/usr/bin/env python3
"""
certbot --deploy-hook script. Runs only after certbot successfully issues
or renews a certificate.

This REPLACES the original deploy_to_panos.py now that the appliance is a
"single pane of glass" supporting multiple kinds of deploy targets (PAN-OS
firewalls, Windows/IIS servers via WinRM, and potentially more in future --
see deploy_providers/). Each domains[] entry's deploy_targets[] list can
reference targets of ANY type, mixed freely -- e.g. the same certificate
deployed to both a firewall AND an IIS server in the same run.

This is also reused directly (NOT just via certbot) by
bin/redeploy-cert.sh for the web UI's "Redeploy" button.

EXIT CODE: exits 1 if ANY deploy target for ANY domain failed to
deploy, and 0 only if every target for every matched domain succeeded
(and at least one domain matched at all).
"""
import datetime
import logging
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deploy_providers import get_provider, DeployProviderError  # noqa: E402

CONFIG_PATH = os.environ.get("ACME_APPLIANCE_CONFIG", "/etc/acme-appliance/appliance.yaml")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s deploy_certificate %(levelname)s %(message)s",
    filename=os.environ.get("ACME_APPLIANCE_LOG", "/var/log/acme-appliance.log"),
)
log = logging.getLogger("deploy_certificate")


def load_config():
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)


def _entry_contains_name(entry: dict, domain: str) -> bool:
    """
    True if `domain` appears in this entry's additional_names list --
    as a plain string OR as the "name" of a {"name": ..., "dns_provider":
    ...} per-name provider override dict. This deployment step doesn't
    care WHICH dns_provider was used to validate a name (that's only
    relevant during the DNS-01 challenge, handled by dns_dispatcher.py)
    -- it just needs to find which domains[] entry (and therefore which
    deploy_targets) a renewed name belongs to.
    """
    for item in entry.get("additional_names", []):
        item_name = item["name"] if isinstance(item, dict) else item
        if item_name == domain:
            return True
    return False


def find_domain_config(cfg, domain: str):
    for entry in cfg["domains"]:
        if entry["name"] == domain:
            return entry
        if _entry_contains_name(entry, domain):
            return entry
    return None


def main() -> int:
    lineage = os.environ.get("RENEWED_LINEAGE")
    renewed_domains = os.environ.get("RENEWED_DOMAINS", "")
    if not lineage or not renewed_domains:
        raise SystemExit(
            "RENEWED_LINEAGE / RENEWED_DOMAINS not set - run via certbot --deploy-hook "
            "or bin/redeploy-cert.sh"
        )

    cfg = load_config()
    cert_path = os.path.join(lineage, "fullchain.pem")
    key_path = os.path.join(lineage, "privkey.pem")
    stamp = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")

    processed_entry_names = set()
    matched_any = False
    any_failures = False

    for domain in renewed_domains.split():
        domain_cfg = find_domain_config(cfg, domain)
        if not domain_cfg:
            continue
        matched_any = True
        if domain_cfg["name"] in processed_entry_names:
            continue
        processed_entry_names.add(domain_cfg["name"])

        cert_name = f"{domain_cfg['cert_name_prefix']}-{stamp}"
        log.info("Deploying %s (domain entry=%s) as cert_name=%s",
                  cert_path, domain_cfg["name"], cert_name)

        targets = domain_cfg.get("deploy_targets", [])
        if not targets:
            log.warning(
                "Domain entry '%s' has no deploy_targets configured -- nothing to deploy to.",
                domain_cfg["name"],
            )
            continue

        for target_entry in targets:
            instance_name = target_entry.get("target")
            instance_cfg = cfg.get("deploy_providers", {}).get(instance_name)
            if instance_cfg is None:
                any_failures = True
                log.error(
                    "Domain entry '%s' references unknown deploy target instance '%s'",
                    domain_cfg["name"], instance_name,
                )
                continue

            try:
                provider = get_provider(instance_cfg["type"], instance_cfg.get("settings", {}))
                provider.deploy_certificate(
                    cert_name, cert_path, key_path, target_entry, domain_name=domain_cfg["name"]
                )
                provider.cleanup_old_certificates(
                    prefix=domain_cfg["cert_name_prefix"], keep_name=cert_name
                )
                log.info(
                    "Successfully deployed %s to deploy target '%s' (type=%s)",
                    cert_name, instance_name, instance_cfg["type"],
                )
            except DeployProviderError as exc:
                any_failures = True
                log.error(
                    "Failed deploying %s to deploy target '%s': %s",
                    cert_name, instance_name, exc,
                )
                continue

    if not matched_any:
        log.warning(
            "No domains[] entry matched renewed domains: %s", renewed_domains
        )
        return 1

    if any_failures:
        log.error("One or more deploy targets failed to deploy -- see errors above.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
