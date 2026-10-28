"""
PAN-OS deploy provider -- adapts the existing panos.client.PanosClient
(completely unchanged; see panos/client.py) to the generic
BaseDeployProvider interface, so Palo Alto firewalls become just ONE of
potentially several deploy target TYPES the appliance supports, on equal
footing with iis.py and any future target types.

All of the PAN-OS-specific behavior (category=keypair cert+key import,
full-object type=edit for Panorama-managed profiles, commit polling,
old-cert cleanup by name prefix, etc.) still lives in panos/client.py --
this file is intentionally a thin adapter, not a reimplementation.
"""
from panos import PanosClient, PanosError  # noqa: E402  (the original, unmodified PAN-OS XML API client)

from .base import BaseDeployProvider, DeployProviderError


class PanosDeployProvider(BaseDeployProvider):
    def _client(self) -> PanosClient:
        return PanosClient(
            hostname=self.settings["hostname"],
            api_key=self.settings.get("api_key") or None,
            username=self.settings.get("username") or None,
            password=self.settings.get("password") or None,
            verify_tls=self.settings.get("verify_tls", False),
        )

    def deploy_certificate(self, cert_name, cert_path, key_path, target_entry, domain_name=None):
        client = self._client()
        try:
            client.import_certificate(cert_name, cert_path, key_path)

            if target_entry.get("cert_field_type") == "globalprotect_portal":
                client.set_globalprotect_portal_certificate(
                    target_entry["cert_field_value"], cert_name
                )
            else:
                client.set_ssl_tls_profile_certificate(
                    target_entry["cert_field_value"], cert_name,
                    vsys=target_entry.get("vsys") or None,
                )

            client.commit(description=f"ACME appliance: deploy {domain_name or cert_name}")
        except PanosError as exc:
            raise DeployProviderError(str(exc)) from exc

    def cleanup_old_certificates(self, prefix, keep_name):
        if not self.settings.get("cleanup_old_certs"):
            return
        try:
            self._client().cleanup_old_certificates(prefix=prefix, keep_name=keep_name)
        except PanosError as exc:
            raise DeployProviderError(f"Cleanup of old certificates failed: {exc}") from exc

    def test_connection(self) -> str:
        try:
            info = self._client().system_info()
        except PanosError as exc:
            raise DeployProviderError(str(exc)) from exc
        return (
            f"Connected to {info.get('hostname', self.settings.get('hostname', ''))} "
            f"({info.get('model', 'unknown model')}, PAN-OS {info.get('sw-version', 'unknown')})."
        )

    def list_options(self, vsys: str = None, **kwargs) -> list:
        try:
            return self._client().list_ssl_tls_profiles(vsys=vsys)
        except PanosError as exc:
            raise DeployProviderError(str(exc)) from exc
