"""
Plugin registry / factory for certificate DEPLOY TARGETS.

Mirrors dns_providers/__init__.py's registry pattern one level further
down the pipeline: dns_providers[] answer "how do we prove domain
ownership to Let's Encrypt"; deploy_providers[] answer "where does the
issued certificate actually go". Each entry in PROVIDER_TYPES is a TYPE
of deploy target (e.g. "panos" for Palo Alto firewalls, "iis" for
Windows/IIS servers via WinRM); each named INSTANCE of a type (e.g.
"hq-firewall", "iis01-prod") lives under the top-level deploy_providers[]
map in appliance.yaml.

Every domain's deploy_targets[] entry references one of these named
instances by name (the "target" key) plus whatever TARGET_FIELDS that
type needs to know WHERE on that instance to put the certificate (e.g.
which SSL/TLS profile on a firewall, or which IIS site + binding on a
Windows server) -- this is the same "instance vs. per-use fields" split
already used for dns_providers (a provider instance's connection
settings vs. a domain's per-name dns_provider reference).
"""
from .base import BaseDeployProvider, DeployProviderError
from .panos import PanosDeployProvider
from .iis import IisDeployProvider

PROVIDER_TYPES = {
    "panos": PanosDeployProvider,
    "iis": IisDeployProvider,
}

# Connection-level settings for each named INSTANCE, shown on the
# "Add/Edit deploy target" form -- parallels dns_providers.PROVIDER_FIELDS.
INSTANCE_FIELDS = {
    "panos": {
        "label": "Palo Alto Firewall (PAN-OS)",
        "fields": [
            {"name": "hostname", "label": "Management hostname or IP", "required": True},
            {"name": "api_key", "label": "API key", "secret": True},
            {"name": "username", "label": "Username (alternative to API key)"},
            {"name": "password", "label": "Password", "secret": True},
            {"name": "verify_tls", "label": "Verify TLS certificate", "type": "checkbox", "default": False},
            {"name": "cleanup_old_certs", "label": "Automatically delete old certs sharing the same prefix",
             "type": "checkbox", "default": True},
        ],
    },
    "iis": {
        "label": "Windows / IIS Server (WinRM)",
        "fields": [
            {"name": "hostname", "label": "WinRM hostname or IP", "required": True},
            {"name": "winrm_port", "label": "WinRM port", "type": "number", "default": 5986},
            {"name": "transport", "label": "WinRM auth transport", "type": "select",
             "options": ["ntlm", "kerberos", "credssp", "basic"], "default": "ntlm"},
            {"name": "username", "label": "Username (DOMAIN\\\\user or user@domain)", "required": True},
            {"name": "password", "label": "Password", "secret": True, "required": True},
            {"name": "verify_tls", "label": "Verify WinRM TLS certificate", "type": "checkbox", "default": False},
            {"name": "cert_store_location", "label": "Certificate store path", "default": "Cert:\\LocalMachine\\My"},
            {"name": "cleanup_old_certs", "label": "Automatically delete old certs sharing the same prefix",
             "type": "checkbox", "default": True},
        ],
    },
}

# Per-domain deploy_targets[] entry fields for each type -- rendered on
# the Domain form once a target instance of that type is picked in a
# row. Fields flagged "fetchable" get a "Fetch options" button wired up
# to GET /deploy-providers/<instance>/options (see webui/app.py).
TARGET_FIELDS = {
    "panos": {
        "fields": [
            {"name": "cert_field_type", "label": "Target type", "type": "select",
             "options": [["ssl_tls_profile", "SSL/TLS Service Profile"],
                         ["globalprotect_portal", "GlobalProtect portal cert field"]],
             "default": "ssl_tls_profile"},
            {"name": "cert_field_value", "label": "Profile / portal name", "required": True, "fetchable": True},
            {"name": "vsys", "label": "vsys (optional)"},
        ],
    },
    "iis": {
        "fields": [
            {"name": "site_name", "label": "IIS site name", "required": True, "fetchable": True},
            {"name": "binding_ip", "label": "Binding IP (optional, default: all)", "default": "*"},
            {"name": "binding_port", "label": "Binding port", "type": "number", "default": 443},
            {"name": "hostname", "label": "Hostname / SNI (optional)"},
        ],
    },
}


def get_provider(provider_type: str, settings: dict) -> BaseDeployProvider:
    try:
        cls = PROVIDER_TYPES[provider_type]
    except KeyError as exc:
        raise DeployProviderError(
            f"Unknown deploy provider type '{provider_type}'. Available: {', '.join(PROVIDER_TYPES)}"
        ) from exc
    return cls(settings)
