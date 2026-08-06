"""
Shared helpers for:
  - turning a domain name into a filesystem-safe certbot --cert-name /
    lineage directory name (safe_cert_name)
  - resolving the flat list of all names on a domains[] entry, and which
    DNS provider instance should be used for each one, supporting a
    per-name provider OVERRIDE within additional_names (SANs)

additional_names[] schema
--------------------------
Each item in a domains[].additional_names list may be EITHER:
  - a plain string, e.g. "example.com"
      -> uses the entry's own top-level dns_provider (unchanged,
         fully backward-compatible with existing configs)
  - a dict, e.g. {"name": "portal.otherdomain.com", "dns_provider": "azure-otherdomain"}
      -> uses "dns_provider" for JUST this one name instead of the
         entry's default. This is what makes it possible to issue a
         single SAN certificate that spans multiple DNS zones/accounts,
         as long as each zone's DNS provider is configured somewhere in
         dns_providers[].

Example:
    domains:
      - name: vpn.howardscams.com
        dns_provider: azure-howardscams        # default for this entry
        additional_names:
          - apex.howardscams.com               # plain string -> uses azure-howardscams
          - name: portal.otherdomain.com        # dict -> overrides to a different provider
            dns_provider: azure-otherdomain
        cert_name_prefix: gp-portal-cert
        panos_targets: [...]
"""


def safe_cert_name(domain_name: str) -> str:
    if domain_name.startswith("*."):
        return "wildcard." + domain_name[2:]
    return domain_name


def normalize_additional_names(additional_names, default_provider):
    """
    Given a domains[].additional_names list (which may mix plain strings
    and {"name": ..., "dns_provider": ...} dicts) and the entry's default
    dns_provider, returns a normalized list of (name, dns_provider) tuples
    -- one per additional name, in original order.

    A dict entry with no "dns_provider" key (or an empty/falsy one) also
    falls back to default_provider, same as a plain string would.
    """
    normalized = []
    for item in additional_names or []:
        if isinstance(item, dict):
            name = item["name"]
            provider = item.get("dns_provider") or default_provider
        else:
            name = item
            provider = default_provider
        normalized.append((name, provider))
    return normalized


def all_names_with_providers(domain_cfg):
    """
    Returns a list of (name, dns_provider) tuples covering EVERY name on
    a domains[] entry -- the primary name first (always using the
    entry's own top-level dns_provider), followed by each additional
    name with its resolved (possibly overridden) provider.
    """
    result = [(domain_cfg["name"], domain_cfg["dns_provider"])]
    result.extend(normalize_additional_names(
        domain_cfg.get("additional_names"), domain_cfg["dns_provider"]
    ))
    return result


def all_names(domain_cfg):
    """Returns just the flat list of name strings (primary + additional), e.g. for certbot -d args."""
    return [name for name, _provider in all_names_with_providers(domain_cfg)]


def resolve_dns_provider_for_name(domain_cfg, target_name: str):
    """
    Given a domains[] entry and one specific FQDN being challenged
    (either the primary name or one of additional_names), returns the
    dns_provider instance name that should be used for it -- respecting
    a per-name override in additional_names if one is set, and falling
    back to the entry's default dns_provider otherwise.
    """
    for name, provider in all_names_with_providers(domain_cfg):
        if name == target_name:
            return provider
    # Shouldn't normally be reached -- callers only invoke this after
    # already matching target_name to this entry -- but fall back to the
    # entry's default provider rather than raising, to fail safe.
    return domain_cfg["dns_provider"]


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        raise SystemExit("Usage: cert_naming.py <domain-name>")
    print(safe_cert_name(sys.argv[1]))
