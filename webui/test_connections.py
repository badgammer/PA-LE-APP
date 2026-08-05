"""
Wraps dns_providers.get_provider(...).test_connection() and
panos.PanosClient(...) helper calls for the web UI's "Test Connection"
buttons and the SSL/TLS profile picker, normalizing results into
(ok: bool, message-or-data) tuples.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dns_providers import get_provider, DnsProviderError  # noqa: E402
from panos import PanosClient, PanosError  # noqa: E402


def _make_client(fw_settings: dict) -> PanosClient:
    return PanosClient(
        hostname=fw_settings["hostname"],
        api_key=fw_settings.get("api_key") or None,
        username=fw_settings.get("username") or None,
        password=fw_settings.get("password") or None,
        verify_tls=fw_settings.get("verify_tls", False),
        timeout=15,
    )


def test_dns_provider(provider_type: str, settings: dict):
    try:
        provider = get_provider(provider_type, settings)
        message = provider.test_connection()
        return True, message
    except NotImplementedError as exc:
        return None, str(exc)
    except DnsProviderError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"Unexpected error: {exc}"


def test_panos_firewall(fw_settings: dict):
    """
    Note: this calls PanosClient.system_info(), which is an "Operational
    Requests" XML API call. If the admin role assigned to this firewall's
    API account only has "Configuration"/"Import"/"Commit" enabled (the
    minimum for actual cert deployment) but not "Operational Requests",
    this test will fail even though real renewals/deployments would
    still succeed -- PanosClient surfaces a hint about this in the error
    message when it looks like a permissions problem.
    """
    try:
        client = _make_client(fw_settings)
        info = client.system_info()
        return True, (
            f"Connected to {info.get('hostname', fw_settings['hostname'])} "
            f"({info.get('model', 'unknown model')}, "
            f"PAN-OS {info.get('sw-version', 'unknown')})."
        )
    except PanosError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"Unexpected error: {exc}"


def list_ssl_profiles(fw_settings: dict, vsys: str = None):
    """
    Returns (True, [profile_names...]) on success, or (False, error_message)
    on failure. Used by the domain form's "Fetch profiles" button.
    """
    try:
        client = _make_client(fw_settings)
        profiles = client.list_ssl_tls_profiles(vsys=vsys)
        return True, profiles
    except PanosError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"Unexpected error: {exc}"
