"""Plugin registry / factory for DNS providers."""
from .base import BaseDnsProvider, DnsProviderError
from .cloudflare import CloudflareProvider
from .azure import AzureDnsProvider
from .route53 import Route53Provider
from .generic_webhook import GenericWebhookProvider

PROVIDER_TYPES = {
    "cloudflare": CloudflareProvider,
    "azure": AzureDnsProvider,
    "route53": Route53Provider,
    "generic_webhook": GenericWebhookProvider,
}

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
}


def get_provider(provider_type: str, settings: dict) -> BaseDnsProvider:
    try:
        cls = PROVIDER_TYPES[provider_type]
    except KeyError as exc:
        raise DnsProviderError(f"Unknown DNS provider type '{provider_type}'. Available: {', '.join(PROVIDER_TYPES)}") from exc
    return cls(settings)
