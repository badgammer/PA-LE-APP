"""Base interface that every DNS provider plugin must implement."""
from abc import ABC, abstractmethod


class DnsProviderError(Exception):
    """Raised when a DNS provider plugin cannot complete a request."""


class BaseDnsProvider(ABC):
    def __init__(self, settings: dict):
        self.settings = settings or {}

    @abstractmethod
    def add_txt_record(self, fqdn: str, value: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def remove_txt_record(self, fqdn: str, value: str) -> None:
        raise NotImplementedError

    def propagation_seconds(self) -> int:
        return int(self.settings.get("propagation_seconds", 30))

    def test_connection(self) -> str:
        raise NotImplementedError("No automated test available for this provider type.")
