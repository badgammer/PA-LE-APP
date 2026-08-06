#!/usr/bin/env python3
"""
certbot --deploy-hook script. Runs only after certbot successfully issues
or renews a certificate.

This is also reused directly (NOT just via certbot) by
bin/redeploy-cert.sh for the web UI's "Redeploy to firewall" button.

EXIT CODE: exits 1 if ANY firewall target for ANY domain failed to
deploy, and 0 only if every target for every matched domain succeeded
(and at least one domain matched at all).
"""
import datetime
import logging
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from panos import PanosClient, PanosError  # noqa: E402

CONFIG_PATH = os.environ.get("ACME_APPLIANCE_CONFIG", "/etc/acme-appliance/appliance.yaml")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s deploy_to_panos %(levelname)s %(message)s",
    filename=os.environ.get("ACME_APPLIANCE_LOG", "/var/log/acme-appliance.log"),
)
log = logging.getLogger("deploy_to_panos")


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
    panos_targets) a renewed name belongs to.
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

        targets = domain_cfg["panos_targets"]
        if not targets:
            log.warning("Domain entry '%s' has no panos_targets configured -- nothing to deploy to.", domain_cfg["name"])
            continue

        for target in targets:
            fw_cfg = cfg["panos_firewalls"][target["firewall"]]
            client = PanosClient(
                hostname=fw_cfg["hostname"],
                api_key=fw_cfg.get("api_key"),
                username=fw_cfg.get("username"),
                password=fw_cfg.get("password"),
                verify_tls=fw_cfg.get("verify_tls", False),
            )
            try:
                client.import_certificate(cert_name, cert_path, key_path)

                if target.get("ssl_tls_profile"):
                    client.set_ssl_tls_profile_certificate(
                        target["ssl_tls_profile"], cert_name, vsys=target.get("vsys")
                    )
                if target.get("globalprotect_portal"):
                    client.set_globalprotect_portal_certificate(
                        target["globalprotect_portal"], cert_name
                    )

                client.commit(description=f"ACME appliance: deploy {domain_cfg['name']}")

                if fw_cfg.get("cleanup_old_certs"):
                    client.cleanup_old_certificates(
                        prefix=domain_cfg["cert_name_prefix"], keep_name=cert_name
                    )

                log.info(
                    "Successfully deployed %s to firewall %s", cert_name, target["firewall"]
                )
            except PanosError as exc:
                any_failures = True
                log.error(
                    "Failed deploying %s to firewall %s: %s",
                    cert_name, target["firewall"], exc,
                )
                continue

    if not matched_any:
        log.warning(
            "No domains[] entry matched renewed domains: %s", renewed_domains
        )
        return 1

    if any_failures:
        log.error("One or more firewall targets failed to deploy -- see errors above.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
