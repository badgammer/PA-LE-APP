"""
Wraps dns_providers.get_provider(...).test_connection() and
deploy_providers.get_provider(...).test_connection()/list_options() for
the web UI's "Test Connection" and "Fetch options" buttons, normalizing
results into (ok: bool-or-None, message-or-data) tuples.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dns_providers import get_provider as get_dns_provider, DnsProviderError  # noqa: E402
from deploy_providers import get_provider as get_deploy_provider, DeployProviderError  # noqa: E402


def test_dns_provider(provider_type: str, settings: dict):
    try:
        provider = get_dns_provider(provider_type, settings)
        message = provider.test_connection()
        return True, message
    except NotImplementedError as exc:
        return None, str(exc)
    except DnsProviderError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"Unexpected error: {exc}"


def test_deploy_provider(provider_type: str, settings: dict):
    """
    Note (PAN-OS specifically): this calls PanosClient.system_info(), an
    "Operational Requests" XML API call. If the admin role assigned to a
    panos-type instance's API account only has Configuration/Import/Commit
    enabled (the minimum for actual cert deployment) but not Operational
    Requests, this test will fail even though real deploys would still
    succeed -- PanosClient surfaces a hint about this in the error message
    when it looks like a permissions problem.
    """
    try:
        provider = get_deploy_provider(provider_type, settings)
        message = provider.test_connection()
        return True, message
    except NotImplementedError as exc:
        return None, str(exc)
    except DeployProviderError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"Unexpected error: {exc}"


def list_target_options(provider_type: str, settings: dict, **kwargs):
    """
    Used by the Domain form's "Fetch options" button (e.g. PAN-OS SSL/TLS
    Service Profile names, or IIS site names). Returns (True, [options...])
    on success, or (False, error_message) on failure/unsupported.
    """
    try:
        provider = get_deploy_provider(provider_type, settings)
        return True, provider.list_options(**kwargs)
    except NotImplementedError as exc:
        return False, str(exc)
    except DeployProviderError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"Unexpected error: {exc}"
