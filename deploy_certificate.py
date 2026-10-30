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


def _notify(event: str, domain: str, detail: str = "") -> None:
    """
    Email alert (see docs/NOTIFICATIONS.md). Lazily imported and fully guarded: a problem in the
    alert code must never change what this script does or how it exits.
    """
    try:
        import notifications
        notifications.notify_event(event, domain, detail, config_path=CONFIG_PATH)
    except Exception:  # noqa: BLE001
        log.exception("Could not send '%s' notification for %s", event, domain)


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
    # bin/redeploy-cert.sh sets this: re-deploying an existing certificate is not a renewal.
    is_redeploy = os.environ.get("ACME_APPLIANCE_REDEPLOY") == "1"

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
        if not is_redeploy:
            _notify("renewal_success", domain_cfg["name"],
                    f"A certificate was issued or renewed for: {renewed_domains}")

        targets = domain_cfg.get("deploy_targets", [])
        if not targets:
            log.warning(
                "Domain entry '%s' has no deploy_targets configured -- nothing to deploy to.",
                domain_cfg["name"],
            )
            continue

        entry_failures = []   # (target instance, reason) for the alert email
        deployed_to = []
        for target_entry in targets:
            instance_name = target_entry.get("target")
            instance_cfg = cfg.get("deploy_providers", {}).get(instance_name)
            if instance_cfg is None:
                any_failures = True
                entry_failures.append((instance_name, "unknown deploy target instance"))
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
                deployed_to.append(instance_name)
            except DeployProviderError as exc:
                any_failures = True
                entry_failures.append((instance_name, str(exc)))
                log.error(
                    "Failed deploying %s to deploy target '%s': %s",
                    cert_name, instance_name, exc,
                )
                continue
            except Exception as exc:  # noqa: BLE001
                # A provider raised something that isn't a DeployProviderError (e.g. a library
                # error). Treat it exactly like a failed target so the remaining targets still
                # get the certificate and the failure is reported instead of killing the run.
                any_failures = True
                entry_failures.append((instance_name, f"{type(exc).__name__}: {exc}"))
                log.exception(
                    "Unexpected error deploying %s to deploy target '%s'", cert_name, instance_name,
                )
                continue
        if entry_failures:
            detail = "\n".join(f"- {name}: {reason}" for name, reason in entry_failures)
            if deployed_to:
                detail += "\n\nDeployed OK to: " + ", ".join(deployed_to)
            _notify("deploy_failed", domain_cfg["name"], detail)
        elif deployed_to:
            _notify("deploy_success", domain_cfg["name"], "Deployed to: " + ", ".join(deployed_to))

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
