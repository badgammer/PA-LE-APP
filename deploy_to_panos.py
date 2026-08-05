#!/usr/bin/env python3
"""
certbot --deploy-hook script. Runs only after certbot successfully issues
or renews a certificate.

Reads RENEWED_LINEAGE (the certbot lineage directory containing
fullchain.pem/privkey.pem) and RENEWED_DOMAINS (space-separated domain
names) from the environment, finds the matching domains[] entry for
each, and imports+deploys the certificate to every configured PAN-OS
target.

This is also reused directly (NOT just via certbot) by
bin/redeploy-cert.sh for the web UI's "Redeploy to firewall" button --
that script sets these same two env vars to point at an ALREADY-ISSUED
lineage on disk, letting you re-run just the PAN-OS import/attach/commit
steps without a new ACME issuance (and without consuming Let's Encrypt
rate-limit headroom). Nothing in this file needs to know or care which
of the two callers invoked it.
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


def find_domain_config(cfg, domain: str):
    for entry in cfg["domains"]:
        if entry["name"] == domain:
            return entry
        if domain in entry.get("additional_names", []):
            return entry
    return None


def main():
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

        for target in domain_cfg["panos_targets"]:
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
                log.error(
                    "Failed deploying %s to firewall %s: %s",
                    cert_name, target["firewall"], exc,
                )
                continue

    if not matched_any:
        log.warning(
            "No domains[] entry matched renewed domains: %s", renewed_domains
        )


if __name__ == "__main__":
    main()
