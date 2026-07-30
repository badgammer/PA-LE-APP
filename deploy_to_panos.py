#!/usr/bin/env python3
"""
certbot --deploy-hook script. Runs only after certbot successfully issues
or renews a certificate. Reads the standard certbot deploy-hook env vars
(RENEWED_LINEAGE, RENEWED_DOMAINS), looks up which Palo Alto firewall(s)
should receive this certificate from appliance.yaml, and:

    1. Imports the new cert+key into each target firewall (unique name
       per run so we never clobber a cert that's still referenced).
    2. Points the configured SSL/TLS Service Profile (or GlobalProtect
       portal certificate field) at the new cert.
    3. Commits.
    4. Optionally deletes older certs sharing the same prefix so the
       firewall's certificate store doesn't grow unbounded.

Wired up automatically by bin/acme-renew.sh - you should not need to run
this by hand.
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
    filename="/var/log/acme-appliance.log",
)
log = logging.getLogger("deploy_to_panos")


def load_config():
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)


def find_domain_config(cfg, domain: str):
    """
    Find the domains[] entry that owns `domain`, matching on either the
    entry's primary `name` or any of its `additional_names` (SAN certs --
    e.g. a wildcard "*.example.com" entry that also lists "example.com").
    """
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
            "RENEWED_LINEAGE / RENEWED_DOMAINS not set - run via certbot --deploy-hook"
        )

    cfg = load_config()
    cert_path = os.path.join(lineage, "fullchain.pem")
    key_path = os.path.join(lineage, "privkey.pem")
    stamp = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")

    # A SAN certificate (e.g. wildcard + apex) reports multiple names in
    # RENEWED_DOMAINS, all belonging to the same domains[] entry -- track
    # which entries we've already deployed this run so we don't import/
    # commit the same certificate to the same firewall more than once.
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

                client.commit(description=f"ACME appliance: renew {domain_cfg['name']}")

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
                # Continue on to remaining targets/firewalls rather than
                # aborting the whole run over one failed device.
                continue

    if not matched_any:
        log.warning(
            "No domains[] entry matched renewed domains: %s", renewed_domains
        )


if __name__ == "__main__":
    main()
