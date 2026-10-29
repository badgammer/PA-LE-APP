"""
RFC 2136 dynamic DNS update provider (TSIG-signed), for BIND, PowerDNS,
Knot, Infoblox and any other server that accepts TSIG-authenticated
dynamic updates. Uses dnspython.

NOT supported: Windows DNS "Secure only" dynamic updates. Those use
GSS-TSIG (Kerberos), which dnspython can't speak. A Windows DNS zone
would have to allow "Nonsecure and secure" updates, which is not
recommended; use a CNAME delegation (cname_alias type) to another
provider instead.

Server side, e.g. BIND:
    key "acme-key" { algorithm hmac-sha256; secret "<base64>"; };
    zone "example.com" { ... update-policy {
        grant acme-key name _acme-challenge.example.com. txt; }; };
(Scoping the key to _acme-challenge TXT records only is strongly advised.)

Expected `settings` keys:
    server           IP or hostname of the DNS server accepting updates
    port             default 53 (TCP is always used for updates)
    key_name         TSIG key name, e.g. "acme-key"
    key_secret       TSIG secret (base64)
    key_algorithm    default "hmac-sha256"
    zone             optional zone to update (e.g. "example.com"). When
                     blank the zone is found by asking `server` for SOA
                     records, which needs a normal, non-split answer.
"""
import socket

from .base import BaseDnsProvider, DnsProviderError, candidate_zones

try:
    import dns.exception
    import dns.inet
    import dns.message
    import dns.name
    import dns.query
    import dns.rcode
    import dns.rdatatype
    import dns.tsigkeyring
    import dns.update
except ImportError:  # pragma: no cover
    dns = None

TTL = 120
QUERY_TIMEOUT = 10


class Rfc2136Provider(BaseDnsProvider):
    def __init__(self, settings: dict):
        super().__init__(settings)
        if dns is None:
            raise DnsProviderError(
                "dnspython is not installed. Run 'pip install dnspython' in the appliance "
                "virtualenv to enable the rfc2136 DNS provider."
            )

    # -- helpers ---------------------------------------------------------

    def _server_ip(self) -> str:
        server = (self.settings.get("server") or "").strip()
        if not server:
            raise DnsProviderError("rfc2136 'server' is not set")
        if dns.inet.is_address(server):
            return server
        try:
            return socket.getaddrinfo(server, None)[0][4][0]
        except OSError as exc:
            raise DnsProviderError(f"Cannot resolve DNS server '{server}': {exc}") from exc

    def _port(self) -> int:
        return int(self.settings.get("port") or 53)

    def _keyring(self):
        name, secret = self.settings.get("key_name"), self.settings.get("key_secret")
        if not name or not secret:
            raise DnsProviderError("rfc2136 key_name and key_secret must both be set")
        try:
            return dns.tsigkeyring.from_text({name: secret}), dns.name.from_text(name)
        except Exception as exc:  # noqa: BLE001 - bad base64 etc.
            raise DnsProviderError(f"Invalid TSIG key: {exc}") from exc

    def _find_zone(self, fqdn: str) -> str:
        explicit = (self.settings.get("zone") or "").strip().strip(".").lower()
        if explicit:
            return explicit
        ip, port = self._server_ip(), self._port()
        for cand in candidate_zones(fqdn):
            try:
                resp = dns.query.tcp(dns.message.make_query(cand, "SOA"), ip, port=port, timeout=QUERY_TIMEOUT)
            except (dns.exception.DNSException, OSError, EOFError) as exc:
                raise DnsProviderError(f"SOA query to {ip}:{port} failed: {exc}") from exc
            target = dns.name.from_text(cand)
            for rrset in resp.answer:
                if rrset.rdtype == dns.rdatatype.SOA and rrset.name == target:
                    return cand
        raise DnsProviderError(
            f"{self.settings.get('server')} did not return an SOA for any parent zone of {fqdn}. "
            "Set the Zone field explicitly."
        )

    def _send(self, zone: str, build) -> None:
        keyring, keyname = self._keyring()
        algorithm = dns.name.from_text(self.settings.get("key_algorithm") or "hmac-sha256")
        update = dns.update.Update(zone, keyring=keyring, keyname=keyname, keyalgorithm=algorithm)
        build(update)
        ip, port = self._server_ip(), self._port()
        try:
            resp = dns.query.tcp(update, ip, port=port, timeout=QUERY_TIMEOUT)
        except EOFError as exc:
            raise DnsProviderError(
                f"{ip}:{port} closed the connection without answering the update -- usually a wrong "
                "TSIG key name/secret/algorithm, or updates from this host are not allowed"
            ) from exc
        except (dns.exception.DNSException, OSError) as exc:
            raise DnsProviderError(f"Dynamic update to {ip}:{port} failed: {exc}") from exc
        rcode = resp.rcode()
        if rcode != dns.rcode.NOERROR:
            hint = ""
            if rcode == dns.rcode.REFUSED:
                hint = " (server refused: check the TSIG key is allowed to update this zone/name)"
            elif rcode == dns.rcode.NOTAUTH:
                hint = f" (server is not authoritative for '{zone}', or the TSIG key/algorithm is wrong)"
            raise DnsProviderError(f"Dynamic update to zone '{zone}' returned {dns.rcode.to_text(rcode)}{hint}")

    # -- interface -------------------------------------------------------

    def add_txt_record(self, fqdn: str, value: str) -> None:
        zone = self._find_zone(fqdn)
        self._send(zone, lambda u: u.add(dns.name.from_text(fqdn), TTL, "TXT", f'"{value}"'))

    def remove_txt_record(self, fqdn: str, value: str) -> None:
        zone = self._find_zone(fqdn)
        # delete(name, rdtype, rdata) removes only this exact TXT value, so a
        # second token at the same name (wildcard + apex) is left alone.
        self._send(zone, lambda u: u.delete(dns.name.from_text(fqdn), "TXT", f'"{value}"'))

    def test_connection(self) -> str:
        ip, port = self._server_ip(), self._port()
        zone = (self.settings.get("zone") or "").strip().strip(".")
        if not zone:
            try:
                socket.create_connection((ip, port), timeout=QUERY_TIMEOUT).close()
            except OSError as exc:
                raise DnsProviderError(f"Cannot open TCP connection to {ip}:{port}: {exc}") from exc
            return (f"TCP connection to {ip}:{port} works. Set the Zone field to also verify the "
                    "TSIG key with a real test update.")
        probe = dns.name.from_text(f"_acme-appliance-test.{zone}")
        self._send(zone, lambda u: u.add(probe, 30, "TXT", '"acme-appliance-connectivity-test"'))
        self._send(zone, lambda u: u.delete(probe, "TXT", '"acme-appliance-connectivity-test"'))
        return f"Signed dynamic update to zone '{zone}' on {ip}:{port} succeeded (test record added and removed)."
