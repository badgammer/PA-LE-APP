"""
CNAME-delegation ("alias") DNS provider -- for DNS hosts with no API.

Used for Network Solutions (no public DNS-record API that ACME clients can
use) and for any other DNS host where the customer can add a CNAME in a web
console but we can't automate TXT records. It also protects customers'
production DNS credentials: the appliance never gets any access to the
customer's own zone.

How it works
------------
One-time, done by hand at the customer's DNS host (e.g. in Network
Solutions' Advanced DNS Manager):

    _acme-challenge.customer.org   CNAME   customer-org.<target_zone>

where <target_zone> is a zone YOU host on a provider that does have an API
(Azure DNS, Cloudflare, Route53...). When Let's Encrypt looks up the
challenge TXT it follows the CNAME, so this provider simply writes the TXT
into <target_zone> through the other provider instance named in
`target_provider`. Renewals then run with no further manual DNS work.

The CNAME target is derived deterministically from the name being validated
(dots become dashes, 63-char label limit honoured), so a wildcard and apex
certificate share one CNAME, and each additional SAN needs its own. Before
touching anything, add_txt_record checks the CNAME really exists and points
at the expected target, and fails with the exact record to create if not.

Expected `settings` keys:
    target_provider   instance name of another dns_providers[] entry
    target_zone       zone served by that provider where TXTs are written
    verify_cname      bool, default True
"""
import hashlib

from .base import BaseDnsProvider, DnsProviderError

try:
    import dns.exception
    import dns.resolver
except ImportError:  # pragma: no cover
    dns = None

PREFIX = "_acme-challenge."
ALIAS_TYPES = ("cname_alias", "network_solutions")


def target_fqdn(fqdn: str, target_zone: str) -> str:
    """Where the TXT for `fqdn` really lives, e.g. customer-org.acme.example.net"""
    fqdn = fqdn.rstrip(".").lower()
    base = fqdn[len(PREFIX):] if fqdn.startswith(PREFIX) else fqdn
    label = base.replace(".", "-")
    if len(label) > 63:  # DNS label limit: keep it unique but short
        label = label[:46] + "-" + hashlib.sha1(base.encode()).hexdigest()[:16]
    return f"{label}.{target_zone.strip('.').lower()}"


class CnameAliasProvider(BaseDnsProvider):
    NEEDS_INSTANCES = True  # get_provider() passes every dns_providers[] instance

    def __init__(self, settings: dict, instances: dict = None):
        super().__init__(settings)
        self.instances = instances or {}

    def _zone(self) -> str:
        zone = (self.settings.get("target_zone") or "").strip().strip(".")
        if not zone:
            raise DnsProviderError("cname_alias 'target_zone' is not set")
        return zone

    def _inner(self) -> BaseDnsProvider:
        from . import PROVIDER_TYPES  # late import: registry imports this module
        name = (self.settings.get("target_provider") or "").strip()
        if not name:
            raise DnsProviderError("cname_alias 'target_provider' is not set")
        inst = self.instances.get(name)
        if not inst:
            raise DnsProviderError(f"target_provider '{name}' is not a configured DNS provider instance")
        if inst["type"] in ALIAS_TYPES:
            raise DnsProviderError("target_provider cannot itself be a CNAME-alias provider")
        cls = PROVIDER_TYPES.get(inst["type"])
        if cls is None:
            raise DnsProviderError(f"target_provider '{name}' has unknown type '{inst['type']}'")
        return cls(inst.get("settings", {}))

    def _check_cname(self, fqdn: str, expected: str) -> None:
        if self.settings.get("verify_cname") is False:
            return
        if dns is None:
            raise DnsProviderError("dnspython is required to verify the CNAME; pip install dnspython")
        try:
            answer = dns.resolver.Resolver().resolve(fqdn, "CNAME", lifetime=10)
            actual = str(answer[0].target).rstrip(".").lower()
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            raise DnsProviderError(
                f"Missing CNAME. At the customer's DNS host create:  {fqdn}  CNAME  {expected}"
            ) from None
        except dns.exception.DNSException as exc:
            raise DnsProviderError(f"CNAME lookup for {fqdn} failed: {exc}") from exc
        if actual != expected:
            raise DnsProviderError(f"{fqdn} is a CNAME to '{actual}' but must point to '{expected}'")

    def add_txt_record(self, fqdn: str, value: str) -> None:
        target = target_fqdn(fqdn, self._zone())
        self._check_cname(fqdn.rstrip(".").lower(), target)
        self._inner().add_txt_record(target, value)

    def remove_txt_record(self, fqdn: str, value: str) -> None:
        self._inner().remove_txt_record(target_fqdn(fqdn, self._zone()), value)

    def propagation_seconds(self) -> int:
        own = int(self.settings.get("propagation_seconds", 30))
        try:
            return max(own, self._inner().propagation_seconds())
        except DnsProviderError:
            return own

    def test_connection(self) -> str:
        zone = self._zone()
        inner = self._inner()  # raises with a clear message if the target is wrong
        try:
            inner_msg = inner.test_connection()
        except NotImplementedError:
            inner_msg = "target provider has no automated test"
        example = target_fqdn("_acme-challenge.example.com", zone)
        return (f"Target provider OK ({inner_msg}). Required CNAME pattern at the customer's DNS host: "
                f"_acme-challenge.<name>  ->  <name with dots as dashes>.{zone}  "
                f"(e.g. _acme-challenge.example.com -> {example}).")
