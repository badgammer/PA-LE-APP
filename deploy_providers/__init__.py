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

### Install profiles (single-instance vs. msp-panos)

This one codebase supports TWO install profiles from a single installer
(see install.sh at the repo root) rather than being maintained as two
forked codebases:

    single-instance  - one tenant, every registered deploy provider type
                        available (PAN-OS + IIS today).
    msp-panos        - multi-tenant (one systemd instance per customer,
                        see systemd/msp/*.service), PAN-OS only. IIS is
                        deliberately never even imported in this profile
                        -- not just hidden in the UI -- so pywinrm isn't
                        a dependency and there is no WinRM credential
                        surface at all on an MSP fleet host that only
                        ever needed PAN-OS.

The active profile is read from the ACME_APPLIANCE_PROFILE environment
variable, set once per systemd unit at install time (see
lib/profile-*.sh) -- exactly the same pattern already used for
ACME_APPLIANCE_CONFIG, ACME_APPLIANCE_LOG, etc. elsewhere in this
appliance. Unset/unrecognized values default to "single-instance" (full
functionality) so existing installs that predate this feature keep
working exactly as before with no config changes required.
"""
import os

from .base import BaseDeployProvider, DeployProviderError
from .panos import PanosDeployProvider

try:
    from .iis import IisDeployProvider
except ImportError:  # pragma: no cover -- pywinrm not installed
    IisDeployProvider = None

# Every deploy provider type this codebase knows how to build, regardless
# of profile. Profile filtering (see _enabled_type_names() below) narrows
# this down to what's actually usable/offered for the active install.
_ALL_PROVIDER_TYPES = {"panos": PanosDeployProvider}
if IisDeployProvider is not None:
    _ALL_PROVIDER_TYPES["iis"] = IisDeployProvider

# Connection-level settings for each named INSTANCE, shown on the
# "Add/Edit deploy target" form -- parallels dns_providers.PROVIDER_FIELDS.
_ALL_INSTANCE_FIELDS = {
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
_ALL_TARGET_FIELDS = {
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


_VALID_PROFILES = {"single-instance", "msp-panos"}


def _active_profile() -> str:
    profile = os.environ.get("ACME_APPLIANCE_PROFILE", "single-instance").strip()
    return profile if profile in _VALID_PROFILES else "single-instance"


def _enabled_type_names() -> set:
    if _active_profile() == "msp-panos":
        return {"panos"}
    return set(_ALL_PROVIDER_TYPES.keys())  # single-instance (or unrecognized/default): everything available


def _filtered(all_dict: dict) -> dict:
    enabled = _enabled_type_names()
    return {k: v for k, v in all_dict.items() if k in enabled}


# Public, profile-filtered views -- everything that already imported
# PROVIDER_TYPES / INSTANCE_FIELDS / TARGET_FIELDS from this module
# (webui/app.py, webui/test_connections.py, domain_form.html's Jinja
# context, etc.) keeps working completely unchanged; they just now only
# ever see the type(s) this install's profile actually enables.
PROVIDER_TYPES = _filtered(_ALL_PROVIDER_TYPES)
INSTANCE_FIELDS = _filtered(_ALL_INSTANCE_FIELDS)
TARGET_FIELDS = _filtered(_ALL_TARGET_FIELDS)


def get_provider(provider_type: str, settings: dict) -> BaseDeployProvider:
    try:
        cls = PROVIDER_TYPES[provider_type]
    except KeyError as exc:
        if provider_type in _ALL_PROVIDER_TYPES and provider_type not in PROVIDER_TYPES:
            raise DeployProviderError(
                f"Deploy provider type '{provider_type}' exists but is disabled under this "
                f"install's profile ({_active_profile()}). This appliance.yaml may have been "
                f"copied from a different install profile, or IisDeployProvider's dependency "
                f"(pywinrm) may not be installed. Available here: {', '.join(PROVIDER_TYPES)}"
            ) from exc
        raise DeployProviderError(
            f"Unknown deploy provider type '{provider_type}'. Available: {', '.join(PROVIDER_TYPES)}"
        ) from exc
    return cls(settings)
