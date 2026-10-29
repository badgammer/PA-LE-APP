"""
Tests for the GoDaddy, Namecheap, RFC 2136 and CNAME-alias DNS providers.

No network access needed: GoDaddy/Namecheap talk to in-memory fakes, and
RFC 2136 talks to a tiny TSIG-validating DNS server on 127.0.0.1.

    python -m pytest tests/test_new_dns_providers.py
"""
import os
import socketserver
import struct
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dns.flags
import dns.message
import dns.opcode
import dns.rcode
import dns.rdatatype
import dns.rrset
import dns.tsigkeyring

from dns_providers import PROVIDER_FIELDS, PROVIDER_TYPES, DnsProviderError, get_provider
from dns_providers.base import candidate_zones, relative_name
from dns_providers import cname_alias
from dns_providers import godaddy as godaddy_mod
from dns_providers import namecheap as namecheap_mod

FQDN = "_acme-challenge.www.example.co.uk"


def test_registry_is_consistent():
    assert set(PROVIDER_TYPES) == set(PROVIDER_FIELDS)
    for t in ("godaddy", "namecheap", "rfc2136", "network_solutions", "cname_alias"):
        assert t in PROVIDER_TYPES


def test_candidate_zones_and_relative_name():
    assert list(candidate_zones(FQDN)) == ["www.example.co.uk", "example.co.uk", "co.uk"]
    assert relative_name(FQDN, "example.co.uk") == "_acme-challenge.www"
    assert relative_name("_acme-challenge.example.com", "example.com") == "_acme-challenge"
    with pytest.raises(DnsProviderError):
        relative_name("a.other.com", "example.com")


# ---------------------------------------------------------------- GoDaddy
class FakeGoDaddy:
    """Minimal stand-in for requests.request against GoDaddy's v1 DNS API."""

    def __init__(self, domains=("example.co.uk",)):
        self.domains = set(domains)
        self.txt = {}  # (domain, name) -> list of {"data","ttl"}
        self.calls = []

    def __call__(self, method, url, headers=None, timeout=None, json=None, **kw):
        path = url.split("godaddy.com", 1)[1]
        self.calls.append((method, path))
        parts = path.strip("/").split("/")  # v1 domains <d> [records TXT <name>]
        resp = type("R", (), {})()
        resp.status_code, resp.ok, resp.text = 200, True, ""
        resp.json = lambda: {}

        def fin(code, body=None):
            resp.status_code, resp.ok = code, code < 400
            resp.json = lambda: body if body is not None else {}
            return resp

        domain = parts[2]
        if domain not in self.domains:
            return fin(404, {"code": "UNKNOWN_DOMAIN", "message": "nope"})
        if len(parts) == 3:
            return fin(200, {"domain": domain})
        name = parts[5]
        key = (domain, name)
        if method == "GET":
            return fin(200, list(self.txt.get(key, [])))
        if method == "PUT":
            self.txt[key] = [dict(r) for r in json]
            return fin(200)
        if method == "DELETE":
            self.txt.pop(key, None)
            return fin(204)
        raise AssertionError(method)


@pytest.fixture
def gd(monkeypatch):
    fake = FakeGoDaddy()
    monkeypatch.setattr(godaddy_mod.requests, "request", fake)
    return fake


def test_godaddy_add_remove_keeps_other_tokens(gd):
    p = get_provider("godaddy", {"api_key": "k", "api_secret": "s"})
    p.add_txt_record(FQDN, "tokenA")   # e.g. wildcard
    p.add_txt_record(FQDN, "tokenB")   # e.g. apex, same name
    p.add_txt_record(FQDN, "tokenB")   # idempotent
    stored = gd.txt[("example.co.uk", "_acme-challenge.www")]
    assert [r["data"] for r in stored] == ["tokenA", "tokenB"]
    assert all(r["ttl"] >= 600 for r in stored)
    p.remove_txt_record(FQDN, "tokenA")
    assert [r["data"] for r in gd.txt[("example.co.uk", "_acme-challenge.www")]] == ["tokenB"]
    p.remove_txt_record(FQDN, "tokenB")
    assert ("example.co.uk", "_acme-challenge.www") not in gd.txt
    p.remove_txt_record(FQDN, "tokenB")  # removing something absent is a no-op


def test_godaddy_auth_header_modes(monkeypatch):
    seen = {}

    def fake(method, url, headers=None, **kw):
        seen["auth"] = headers["Authorization"]
        r = type("R", (), {"status_code": 200, "ok": True, "text": "", "json": lambda self: []})()
        return r

    monkeypatch.setattr(godaddy_mod.requests, "request", fake)
    get_provider("godaddy", {"api_key": "K", "api_secret": "S"}).test_connection()
    assert seen["auth"] == "sso-key K:S"
    get_provider("godaddy", {"api_key": "PAT"}).test_connection()
    assert seen["auth"] == "Bearer PAT"


def test_godaddy_unknown_domain_is_clear_error(gd):
    gd.domains.clear()
    with pytest.raises(DnsProviderError, match="No domain in this GoDaddy account"):
        get_provider("godaddy", {"api_key": "k", "api_secret": "s"}).add_txt_record(FQDN, "x")


# --------------------------------------------------------------- Namecheap
NS = 'xmlns="http://api.namecheap.com/xml.response"'


class FakeNamecheap:
    def __init__(self, using_our_dns=True):
        self.using_our_dns = using_our_dns
        self.hosts = [
            {"Name": "@", "Type": "A", "Address": "1.2.3.4", "MXPref": "10", "TTL": "1800"},
            {"Name": "@", "Type": "MX", "Address": "mail.example.co.uk.", "MXPref": "5", "TTL": "3600"},
            {"Name": "www", "Type": "CNAME", "Address": "example.co.uk.", "MXPref": "10", "TTL": "1800"},
        ]
        self.set_calls = 0

    def _resp(self, body, status="OK"):
        xml = f'<?xml version="1.0"?><ApiResponse {NS} Status="{status}">{body}</ApiResponse>'
        return type("R", (), {"content": xml.encode(), "status_code": 200, "text": xml})()

    def handle(self, params):
        cmd = params["Command"]
        if cmd == "namecheap.domains.getList":
            term = params.get("SearchTerm", "")
            doms = "".join(f'<Domain Name="{d}"/>' for d in ["example.co.uk"] if term in d)
            return self._resp(f"<Errors/><CommandResponse><DomainGetListResult>{doms}</DomainGetListResult></CommandResponse>")
        if cmd == "namecheap.domains.dns.getHosts":
            assert (params["SLD"], params["TLD"]) == ("example", "co.uk")
            hs = "".join(
                f'<Host Name="{h["Name"]}" Type="{h["Type"]}" Address="{h["Address"]}" MXPref="{h["MXPref"]}" TTL="{h["TTL"]}"/>'
                for h in self.hosts)
            return self._resp(
                f'<Errors/><CommandResponse><DomainDNSGetHostsResult Domain="example.co.uk" '
                f'IsUsingOurDNS="{str(self.using_our_dns).lower()}">{hs}</DomainDNSGetHostsResult></CommandResponse>')
        if cmd == "namecheap.domains.dns.setHosts":
            self.set_calls += 1
            new, i = [], 1
            while f"HostName{i}" in params:
                new.append({"Name": params[f"HostName{i}"], "Type": params[f"RecordType{i}"],
                            "Address": params[f"Address{i}"], "MXPref": params.get(f"MXPref{i}", "10"),
                            "TTL": params[f"TTL{i}"]})
                i += 1
            self.hosts = new
            return self._resp('<Errors/><CommandResponse><DomainDNSSetHostsResult Domain="example.co.uk" IsSuccess="true"/></CommandResponse>')
        return self._resp('<Errors><Error Number="1">bad command</Error></Errors>', status="ERROR")


@pytest.fixture
def nc(monkeypatch, tmp_path):
    fake = FakeNamecheap()
    monkeypatch.setattr(namecheap_mod.requests, "get", lambda url, params=None, **kw: fake.handle(params))
    monkeypatch.setattr(namecheap_mod.requests, "post", lambda url, data=None, **kw: fake.handle(data))
    monkeypatch.setattr(namecheap_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    return fake


NC_SETTINGS = {"api_user": "u", "api_key": "k", "client_ip": "203.0.113.9"}


def test_namecheap_preserves_existing_records(nc):
    before = [dict(h) for h in nc.hosts]
    p = get_provider("namecheap", NC_SETTINGS)
    p.add_txt_record(FQDN, "tok1")
    p.add_txt_record(FQDN, "tok2")
    p.add_txt_record(FQDN, "tok2")  # idempotent -> no extra setHosts
    assert nc.set_calls == 2
    assert nc.hosts[:3] == before      # untouched, incl. MX pref 5
    txts = [h for h in nc.hosts if h["Type"] == "TXT"]
    assert [(h["Name"], h["Address"]) for h in txts] == [("_acme-challenge.www", "tok1"), ("_acme-challenge.www", "tok2")]
    p.remove_txt_record(FQDN, "tok1")
    p.remove_txt_record(FQDN, "tok2")
    assert nc.hosts == before


def test_namecheap_refuses_foreign_dns(nc):
    nc.using_our_dns = False
    with pytest.raises(DnsProviderError, match="not using Namecheap's own DNS"):
        get_provider("namecheap", NC_SETTINGS).add_txt_record(FQDN, "x")
    assert nc.set_calls == 0


def test_namecheap_requires_settings():
    with pytest.raises(DnsProviderError, match="client_ip"):
        get_provider("namecheap", {"api_user": "u", "api_key": "k"}).test_connection()


# ----------------------------------------------------------------- RFC 2136
KEY_NAME = "acme-key"
KEY_SECRET = "c2VjcmV0c2VjcmV0c2VjcmV0c2VjcmV0MTIzNDU2Nzg="  # base64


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        srv = self.server
        raw = self.request.recv(2)
        if len(raw) < 2:
            return
        (n,) = struct.unpack("!H", raw)
        data = b""
        while len(data) < n:
            data += self.request.recv(n - len(data))
        try:
            msg = dns.message.from_wire(data, keyring=srv.keyring)
        except Exception:  # bad / missing TSIG: drop the connection
            return
        resp = dns.message.make_response(msg)
        if msg.opcode() == dns.opcode.UPDATE:
            zone = msg.zone[0].name.to_text().rstrip(".")
            if zone != srv.zone:
                resp.set_rcode(dns.rcode.NOTAUTH)
            else:
                for rrset in msg.update:
                    name = rrset.name.to_text().rstrip(".")
                    vals = {r.to_text() for r in rrset}
                    if rrset.deleting == dns.rdataclass.NONE:  # "delete this specific rdata"
                        srv.txt.get(name, set()).difference_update(vals)
                    elif rrset.rdtype == dns.rdatatype.TXT:
                        srv.txt.setdefault(name, set()).update(vals)
        else:  # SOA query
            q = msg.question[0]
            if q.rdtype == dns.rdatatype.SOA and q.name.to_text().rstrip(".") == srv.zone:
                resp.answer.append(dns.rrset.from_text(
                    q.name, 60, "IN", "SOA", "ns.example. admin.example. 1 3600 600 86400 60"))
        wire = resp.to_wire()
        self.request.sendall(struct.pack("!H", len(wire)) + wire)


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@pytest.fixture
def dnsd():
    srv = _Server(("127.0.0.1", 0), _Handler)
    srv.keyring = dns.tsigkeyring.from_text({KEY_NAME: KEY_SECRET})
    srv.zone = "example.com"
    srv.txt = {}
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _rfc(srv, **over):
    s = {"server": "127.0.0.1", "port": srv.server_address[1], "key_name": KEY_NAME, "key_secret": KEY_SECRET}
    s.update(over)
    return get_provider("rfc2136", s)


def test_rfc2136_add_remove_with_zone_autodetect(dnsd):
    p = _rfc(dnsd)  # no zone given -> found via SOA
    name = "_acme-challenge.www.example.com"
    p.add_txt_record(name, "tokA")
    p.add_txt_record(name, "tokB")
    assert dnsd.txt[name] == {'"tokA"', '"tokB"'}
    p.remove_txt_record(name, "tokA")
    assert dnsd.txt[name] == {'"tokB"'}


def test_rfc2136_explicit_zone_and_test_connection(dnsd):
    p = _rfc(dnsd, zone="example.com")
    assert "succeeded" in p.test_connection()
    assert not any(dnsd.txt.values())  # probe record cleaned up


def test_rfc2136_wrong_key_fails_cleanly(dnsd):
    bad = _rfc(dnsd, zone="example.com", key_secret="d3Jvbmd3cm9uZ3dyb25nd3Jvbmd3cm9uZ3dyb25nd3Jvbmc=")
    with pytest.raises(DnsProviderError):
        bad.add_txt_record("_acme-challenge.example.com", "x")


def test_rfc2136_wrong_zone_is_notauth(dnsd):
    p = _rfc(dnsd, zone="other.org")
    with pytest.raises(DnsProviderError, match="NOTAUTH"):
        p.add_txt_record("_acme-challenge.other.org", "x")


# -------------------------------------------------- CNAME alias / Network Sol.
class RecordingProvider:
    def __init__(self):
        self.added, self.removed = [], []

    def add_txt_record(self, fqdn, value):
        self.added.append((fqdn, value))

    def remove_txt_record(self, fqdn, value):
        self.removed.append((fqdn, value))

    def propagation_seconds(self):
        return 45

    def test_connection(self):
        return "inner ok"


def _alias(monkeypatch, rec, ptype="network_solutions", **over):
    instances = {"azure-acme": {"type": "cloudflare", "settings": {"api_token": "t"}}}
    monkeypatch.setitem(PROVIDER_TYPES, "cloudflare", lambda settings: rec)
    s = {"target_provider": "azure-acme", "target_zone": "acme.hbs.example", "verify_cname": False}
    s.update(over)
    return get_provider(ptype, s, instances=instances)


def test_target_fqdn_derivation():
    assert cname_alias.target_fqdn("_acme-challenge.customer.org", "acme.hbs.example") == "customer-org.acme.hbs.example"
    long = "_acme-challenge." + ".".join(["abcdefghij"] * 8) + ".org"
    t = cname_alias.target_fqdn(long, "z.example")
    assert len(t.split(".")[0]) <= 63 and t.endswith(".z.example")
    assert t == cname_alias.target_fqdn(long, "z.example")  # deterministic


def test_alias_writes_into_target_zone_via_inner_provider(monkeypatch):
    rec = RecordingProvider()
    p = _alias(monkeypatch, rec)
    p.add_txt_record("_acme-challenge.customer.org", "tok")
    p.remove_txt_record("_acme-challenge.customer.org", "tok")
    assert rec.added == [("customer-org.acme.hbs.example", "tok")]
    assert rec.removed == [("customer-org.acme.hbs.example", "tok")]
    assert p.propagation_seconds() == 45
    assert "inner ok" in p.test_connection() and "CNAME" in p.test_connection()


def test_alias_cname_verification(monkeypatch):
    rec = RecordingProvider()
    p = _alias(monkeypatch, rec, verify_cname=True)
    # don't read the sandbox's /etc/resolv.conf; resolve() is faked below anyway
    monkeypatch.setattr(cname_alias.dns.resolver.Resolver, "__init__", lambda self, *a, **k: None)

    def resolver_ok(self, name, rdtype, **kw):
        return [type("A", (), {"target": "customer-org.acme.hbs.example."})()]

    monkeypatch.setattr(cname_alias.dns.resolver.Resolver, "resolve", resolver_ok)
    p.add_txt_record("_acme-challenge.customer.org", "tok")
    assert rec.added

    def resolver_missing(self, name, rdtype, **kw):
        raise cname_alias.dns.resolver.NoAnswer()

    monkeypatch.setattr(cname_alias.dns.resolver.Resolver, "resolve", resolver_missing)
    rec.added.clear()
    with pytest.raises(DnsProviderError, match="Missing CNAME.*CNAME  customer-org.acme.hbs.example"):
        p.add_txt_record("_acme-challenge.customer.org", "tok2")
    assert rec.added == []  # nothing written when CNAME is absent

    def resolver_wrong(self, name, rdtype, **kw):
        return [type("A", (), {"target": "elsewhere.example."})()]

    monkeypatch.setattr(cname_alias.dns.resolver.Resolver, "resolve", resolver_wrong)
    with pytest.raises(DnsProviderError, match="must point to"):
        p.add_txt_record("_acme-challenge.customer.org", "tok3")


def test_alias_rejects_missing_or_nested_target(monkeypatch):
    with pytest.raises(DnsProviderError, match="not a configured"):
        get_provider("cname_alias", {"target_provider": "nope", "target_zone": "z.example", "verify_cname": False},
                     instances={}).add_txt_record("_acme-challenge.a.org", "x")
    nested = {"x": {"type": "network_solutions", "settings": {}}}
    with pytest.raises(DnsProviderError, match="cannot itself be"):
        get_provider("cname_alias", {"target_provider": "x", "target_zone": "z.example", "verify_cname": False},
                     instances=nested).add_txt_record("_acme-challenge.a.org", "x")


def test_config_store_protects_alias_targets():
    from webui import config_store
    cfg = {"domains": [], "dns_providers": {
        "azure-acme": {"type": "azure", "settings": {}},
        "ns-alias": {"type": "network_solutions", "settings": {"target_provider": "azure-acme"}}}}
    assert config_store.dns_provider_in_use(cfg, "azure-acme") == ["DNS provider alias 'ns-alias'"]
    assert config_store.dns_provider_in_use(cfg, "ns-alias") == []
