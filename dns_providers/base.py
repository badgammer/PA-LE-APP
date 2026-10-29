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


def candidate_zones(fqdn: str, min_labels: int = 2):
    """
    Yield possible zone / registered-domain names for a challenge FQDN,
    longest first, skipping the leading "_acme-challenge" label.

        _acme-challenge.www.example.co.uk ->
            www.example.co.uk, example.co.uk, co.uk

    Providers whose API needs the exact zone (GoDaddy, Namecheap, RFC 2136)
    probe these in order and use the first one the provider recognises, which
    handles multi-label public suffixes such as co.uk without a suffix list.
    """
    labels = fqdn.rstrip(".").lower().split(".")
    for i in range(1, len(labels) - min_labels + 1):
        yield ".".join(labels[i:])


def relative_name(fqdn: str, zone: str) -> str:
    """'_acme-challenge.www.example.com' in 'example.com' -> '_acme-challenge.www'."""
    fqdn = fqdn.rstrip(".").lower()
    zone = zone.rstrip(".").lower()
    if fqdn == zone:
        return "@"
    if not fqdn.endswith("." + zone):
        raise DnsProviderError(f"{fqdn} is not inside zone {zone}")
    return fqdn[: -(len(zone) + 1)]
