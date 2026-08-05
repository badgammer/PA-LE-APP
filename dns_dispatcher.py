#!/usr/bin/env python3
"""certbot manual auth-hook / cleanup-hook dispatcher."""
import logging
import os
import sys
import time

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dns_providers import get_provider, DnsProviderError  # noqa: E402

CONFIG_PATH = os.environ.get("ACME_APPLIANCE_CONFIG", "/etc/acme-appliance/appliance.yaml")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s dns_dispatcher %(levelname)s %(message)s",
    filename=os.environ.get("ACME_APPLIANCE_LOG", "/var/log/acme-appliance.log"),
)
log = logging.getLogger("dns_dispatcher")


def load_config():
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)


def find_domain_config(cfg, domain: str):
    for entry in cfg["domains"]:
        if entry["name"] == domain or domain.endswith(entry["name"]):
            return entry
        if domain in entry.get("additional_names", []):
            return entry
    raise SystemExit(f"No domains[] entry in appliance.yaml matches '{domain}'")


def acme_challenge_fqdn(domain: str) -> str:
    base = domain[2:] if domain.startswith("*.") else domain
    return f"_acme-challenge.{base}"


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("add", "remove"):
        raise SystemExit("Usage: dns_dispatcher.py {add|remove}")

    action = sys.argv[1]
    domain = os.environ.get("CERTBOT_DOMAIN")
    validation = os.environ.get("CERTBOT_VALIDATION")
    if not domain or not validation:
        raise SystemExit("CERTBOT_DOMAIN / CERTBOT_VALIDATION not set - run via certbot hooks")

    fqdn = acme_challenge_fqdn(domain)

    cfg = load_config()
    domain_cfg = find_domain_config(cfg, domain)

    instance_name = domain_cfg["dns_provider"]
    try:
        instance_cfg = cfg["dns_providers"][instance_name]
    except KeyError:
        raise SystemExit(
            f"domains[] entry for '{domain}' references dns_provider "
            f"'{instance_name}', which has no matching entry under dns_providers[]"
        )
    provider_type = instance_cfg["type"]
    provider_settings = instance_cfg.get("settings", {})

    try:
        provider = get_provider(provider_type, provider_settings)
        if action == "add":
            log.info("Adding TXT %s via provider instance=%s (type=%s)", fqdn, instance_name, provider_type)
            provider.add_txt_record(fqdn, validation)
            wait = provider.propagation_seconds()
            log.info("Sleeping %ss for DNS propagation", wait)
            time.sleep(wait)
        else:
            log.info("Removing TXT %s via provider instance=%s (type=%s)", fqdn, instance_name, provider_type)
            provider.remove_txt_record(fqdn, validation)
    except DnsProviderError as exc:
        log.error("DNS provider error: %s", exc)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
