"""
Base interface that every certificate DEPLOY TARGET plugin must implement.

This mirrors the dns_providers/ plugin architecture (see dns_providers/base.py)
one level further down the pipeline: dns_providers/ answer "how do we prove
we own this name to Let's Encrypt", while deploy_providers/ answer "where
does the issued certificate actually go once we have it". Each deploy
target TYPE (e.g. "panos" for Palo Alto firewalls, "iis" for Windows/IIS
servers) is a class implementing this interface; each named INSTANCE of a
type (e.g. "hq-firewall", "iis01-prod") lives under the top-level
deploy_providers[] map in appliance.yaml, exactly the way dns_providers[]
instances work.
"""
from abc import ABC, abstractmethod


class DeployProviderError(Exception):
    """Raised when a deploy target plugin cannot complete a request."""


class BaseDeployProvider(ABC):
    def __init__(self, settings: dict):
        self.settings = settings or {}

    @abstractmethod
    def deploy_certificate(self, cert_name: str, cert_path: str, key_path: str,
                            target_entry: dict, domain_name: str = None) -> None:
        """
        Deploys the certificate at cert_path/key_path (PEM files, as
        produced by certbot) to this specific target INSTANCE, using
        whatever type-specific fields are present in target_entry -- the
        domain's deploy_targets[] row that pointed at this instance (e.g.
        cert_field_type/cert_field_value/vsys for panos, or
        site_name/binding_ip/binding_port/hostname for iis).

        Must raise DeployProviderError (not any provider-internal
        exception type) on failure, so deploy_certificate.py can treat
        every deploy target type identically.
        """
        raise NotImplementedError

    def cleanup_old_certificates(self, prefix: str, keep_name: str) -> None:
        """
        Optional: remove old certificates sharing `prefix` (but not
        `keep_name`) from this target instance, if the instance's
        settings enable cleanup_old_certs. Default is a no-op for
        provider types that don't support/need this -- callers should
        always call this unconditionally after a successful deploy and
        let each provider type decide internally whether to act.
        """

    def test_connection(self) -> str:
        raise NotImplementedError("No automated test available for this deploy target type.")

    def list_options(self, **kwargs) -> list:
        """
        Optional: returns a list of selectable strings (e.g. PAN-OS
        SSL/TLS Service Profile names, or IIS site names) for the web
        UI's "Fetch options" button on the Domain form. Default is
        unsupported -- callers should treat NotImplementedError as
        "this type doesn't offer fetch-assist, just type the value in".
        """
        raise NotImplementedError("This deploy target type does not support option fetching.")
