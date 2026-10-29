"""Plugin registry / factory for DNS providers."""
from .base import BaseDnsProvider, DnsProviderError
from .cloudflare import CloudflareProvider
from .azure import AzureDnsProvider
from .route53 import Route53Provider
from .generic_webhook import GenericWebhookProvider
from .godaddy import GoDaddyProvider
from .namecheap import NamecheapProvider
from .rfc2136 import Rfc2136Provider
from .cname_alias import CnameAliasProvider, ALIAS_TYPES

PROVIDER_TYPES = {
    "cloudflare": CloudflareProvider,
    "azure": AzureDnsProvider,
    "route53": Route53Provider,
    "generic_webhook": GenericWebhookProvider,
    "godaddy": GoDaddyProvider,
    "namecheap": NamecheapProvider,
    "rfc2136": Rfc2136Provider,
    # Network Solutions has no DNS-record API ACME clients can use, so it is
    # served by the same CNAME-delegation class as any other API-less host.
    "network_solutions": CnameAliasProvider,
    "cname_alias": CnameAliasProvider,
}

_ALIAS_FIELDS = [
    {"name": "target_provider", "label": "Target DNS provider instance (name of another provider configured here)",
     "required": True,
     "help": "The provider that hosts the zone below, e.g. an Azure DNS or Cloudflare instance you control."},
    {"name": "target_zone", "label": "Target zone where challenge records are written (e.g. acme.example.net)",
     "required": True,
     "help": "Must be a zone (or inside a zone) served by the target provider."},
    {"name": "verify_cname", "label": "Verify the CNAME exists before validating", "type": "checkbox", "default": True},
    {"name": "propagation_seconds", "label": "Propagation wait (seconds)", "type": "number", "default": 30},
]
_ALIAS_NOTE = (
    "This host's DNS is not edited automatically. Once, in its DNS console, create "
    "_acme-challenge.<name> as a CNAME to <name with dots replaced by dashes>.<target zone> "
    "(a wildcard and its apex share one CNAME). Let's Encrypt follows the CNAME, and this appliance "
    "writes the TXT records into the target zone through the target provider. The Test button prints "
    "the exact pattern for your target zone."
)

PROVIDER_FIELDS = {
    "cloudflare": {
        "label": "Cloudflare",
        "fields": [
            {"name": "api_token", "label": "API Token", "secret": True, "required": True},
            {"name": "propagation_seconds", "label": "Propagation wait (seconds)", "type": "number", "default": 20},
        ],
    },
    "azure": {
        "label": "Azure DNS",
        "fields": [
            {"name": "tenant_id", "label": "Tenant ID", "required": True},
            {"name": "sp_client_id", "label": "Service Principal Client ID", "required": True},
            {"name": "sp_client_secret", "label": "Service Principal Client Secret", "secret": True, "required": True},
            {"name": "subscription_id", "label": "Subscription ID", "required": True},
            {"name": "resource_group", "label": "Resource Group", "required": True},
            {"name": "zone", "label": "DNS Zone (e.g. example.com)", "required": True},
            {"name": "propagation_seconds", "label": "Propagation wait (seconds)", "type": "number", "default": 45},
        ],
    },
    "route53": {
        "label": "AWS Route53",
        "fields": [
            {"name": "hosted_zone_id", "label": "Hosted Zone ID", "required": True},
            {"name": "aws_access_key_id", "label": "AWS Access Key ID (blank = use role)"},
            {"name": "aws_secret_access_key", "label": "AWS Secret Access Key", "secret": True},
            {"name": "region", "label": "Region", "default": "us-east-1"},
            {"name": "propagation_seconds", "label": "Propagation wait (seconds)", "type": "number", "default": 30},
        ],
    },
    "generic_webhook": {
        "label": "Generic Webhook / Internal API",
        "fields": [
            {"name": "add_url", "label": "Add-record URL", "required": True},
            {"name": "remove_url", "label": "Remove-record URL", "required": True},
            {"name": "method", "label": "HTTP method", "default": "POST"},
            {"name": "auth_header", "label": "Authorization header value (optional)", "secret": True},
            {"name": "verify_tls", "label": "Verify TLS certificate", "type": "checkbox", "default": True},
            {"name": "propagation_seconds", "label": "Propagation wait (seconds)", "type": "number", "default": 30},
        ],
    },
    "godaddy": {
        "label": "GoDaddy",
        "note": "Only for domains using GoDaddy's own nameservers. Create a Production API key at "
                "developer.godaddy.com/keys.",
        "fields": [
            {"name": "api_key", "label": "API key (or personal access token)", "secret": True, "required": True},
            {"name": "api_secret", "label": "API secret (leave blank when using a personal access token)", "secret": True},
            {"name": "domain", "label": "Registered domain (optional, auto-detected when blank)"},
            {"name": "use_ote", "label": "Use OTE (test) environment", "type": "checkbox", "default": False},
            {"name": "propagation_seconds", "label": "Propagation wait (seconds)", "type": "number", "default": 60},
        ],
    },
    "namecheap": {
        "label": "Namecheap",
        "note": "Requires API access enabled on the account AND this appliance's public IPv4 whitelisted "
                "in Namecheap (max 20, IPv4 only). Domain must use Namecheap's own DNS. Every change "
                "rewrites the domain's full record set (read, modify, write back).",
        "fields": [
            {"name": "api_user", "label": "API user", "required": True},
            {"name": "api_key", "label": "API key", "secret": True, "required": True},
            {"name": "username", "label": "Username (blank = same as API user)"},
            {"name": "client_ip", "label": "Client IP (this appliance's whitelisted public IPv4)", "required": True},
            {"name": "domain", "label": "Registered domain (optional, auto-detected when blank)"},
            {"name": "use_sandbox", "label": "Use Namecheap sandbox", "type": "checkbox", "default": False},
            {"name": "propagation_seconds", "label": "Propagation wait (seconds)", "type": "number", "default": 90},
        ],
    },
    "rfc2136": {
        "label": "RFC 2136 Dynamic DNS (BIND, PowerDNS, Knot, Infoblox)",
        "note": "TSIG-signed dynamic updates over TCP. Windows DNS secure-only zones (GSS-TSIG) are "
                "not supported.",
        "fields": [
            {"name": "server", "label": "DNS server (IP or hostname)", "required": True},
            {"name": "port", "label": "Port", "type": "number", "default": 53},
            {"name": "key_name", "label": "TSIG key name", "required": True},
            {"name": "key_secret", "label": "TSIG key secret (base64)", "secret": True, "required": True},
            {"name": "key_algorithm", "label": "TSIG algorithm", "default": "hmac-sha256"},
            {"name": "zone", "label": "Zone (optional, e.g. example.com; found via SOA when blank)"},
            {"name": "propagation_seconds", "label": "Propagation wait (seconds)", "type": "number", "default": 10},
        ],
    },
    "network_solutions": {
        "label": "Network Solutions (via CNAME delegation)",
        "note": _ALIAS_NOTE,
        "fields": _ALIAS_FIELDS,
    },
    "cname_alias": {
        "label": "Any DNS host without an API (via CNAME delegation)",
        "note": _ALIAS_NOTE,
        "fields": _ALIAS_FIELDS,
    },
}


def get_provider(provider_type: str, settings: dict, instances: dict = None) -> BaseDnsProvider:
    try:
        cls = PROVIDER_TYPES[provider_type]
    except KeyError as exc:
        raise DnsProviderError(f"Unknown DNS provider type '{provider_type}'. Available: {', '.join(PROVIDER_TYPES)}") from exc
    if getattr(cls, "NEEDS_INSTANCES", False):
        # CNAME-alias types delegate to ANOTHER configured dns_providers[] instance.
        return cls(settings, instances=instances)
    return cls(settings)
