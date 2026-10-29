"""
Namecheap DNS provider plugin (XML API: getList / getHosts / setHosts).

Namecheap's API has NO "add one record" call. setHosts REPLACES the whole
host-record set for a domain, and "all host records that are not included
in the call will be deleted". So every add/remove here is:

    getHosts (read everything) -> change our one TXT -> setHosts (write all)

under a per-domain file lock so two certificates renewing at once can't
overwrite each other. Existing records are sent back unchanged (including
MX preference and TTL).

Requirements on the Namecheap side (all three are mandatory):
  1. API access enabled for the account (Profile > Tools > API Access).
  2. THIS APPLIANCE'S PUBLIC IPv4 whitelisted there (max 20 IPv4 addresses;
     IPv6 is not supported). Put the same address in "Client IP" below.
  3. The domain must use Namecheap BasicDNS/PremiumDNS (its default
     nameservers); getHosts reports IsUsingOurDNS=false otherwise.

Caveat: domains using Namecheap email forwarding or Private Email rely on
MX/forwarding settings that setHosts can disturb. For those, delegate
_acme-challenge with a CNAME to another provider (cname_alias type).

Expected `settings` keys:
    api_user, api_key (secret), username (blank = api_user),
    client_ip, domain (optional), use_sandbox (optional bool)
"""
import contextlib
import fcntl
import os
import re
import tempfile
import xml.etree.ElementTree as ET

import requests

from .base import BaseDnsProvider, DnsProviderError, candidate_zones, relative_name

PROD = "https://api.namecheap.com/xml.response"
SANDBOX = "https://api.sandbox.namecheap.com/xml.response"
CHALLENGE_TTL = 60


class NamecheapProvider(BaseDnsProvider):
    def _endpoint(self) -> str:
        return SANDBOX if self.settings.get("use_sandbox") else PROD

    def _call(self, command: str, params: dict = None, post: bool = False) -> ET.Element:
        for needed in ("api_user", "api_key", "client_ip"):
            if not self.settings.get(needed):
                raise DnsProviderError(f"Namecheap setting '{needed}' is not set")
        data = {
            "ApiUser": self.settings["api_user"],
            "ApiKey": self.settings["api_key"],
            "UserName": self.settings.get("username") or self.settings["api_user"],
            "ClientIp": self.settings["client_ip"],
            "Command": command,
        }
        data.update(params or {})
        try:
            if post:
                r = requests.post(self._endpoint(), data=data, timeout=30)
            else:
                r = requests.get(self._endpoint(), params=data, timeout=30)
        except requests.RequestException as exc:
            raise DnsProviderError(f"Could not reach Namecheap API: {exc}") from exc
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError as exc:
            raise DnsProviderError(f"Namecheap returned a non-XML response (HTTP {r.status_code})") from exc
        for el in root.iter():  # drop the XML namespace so find() paths are readable
            el.tag = el.tag.split("}", 1)[-1]
        if root.get("Status") != "OK":
            errs = [f"{e.get('Number', '?')}: {(e.text or '').strip()}" for e in root.iter("Error")]
            hint = ""
            if any(re.search(r"\bIP\b", e, re.IGNORECASE) for e in errs):
                hint = f" -- is {self.settings['client_ip']} whitelisted in Namecheap (Profile > Tools > API Access)?"
            raise DnsProviderError(f"Namecheap {command} failed: {'; '.join(errs) or r.text[:200]}{hint}")
        return root

    @staticmethod
    def _split(domain: str):
        sld, _, tld = domain.partition(".")
        return sld, tld

    def _find_domain(self, fqdn: str) -> str:
        explicit = (self.settings.get("domain") or "").strip().strip(".").lower()
        if explicit:
            return explicit
        for cand in candidate_zones(fqdn):
            root = self._call("namecheap.domains.getList", {"SearchTerm": cand, "PageSize": "100"})
            for d in root.iter("Domain"):
                if (d.get("Name") or "").lower() == cand:
                    return cand
        raise DnsProviderError(
            f"No domain in this Namecheap account matches {fqdn}. Check the API user owns it, "
            "or set the Domain field explicitly."
        )

    def _get_hosts(self, domain: str):
        sld, tld = self._split(domain)
        root = self._call("namecheap.domains.dns.getHosts", {"SLD": sld, "TLD": tld})
        result = root.find(".//DomainDNSGetHostsResult")
        if result is None:
            raise DnsProviderError(f"Namecheap getHosts for {domain} returned no result block")
        if (result.get("IsUsingOurDNS") or "").lower() != "true":
            raise DnsProviderError(
                f"{domain} is not using Namecheap's own DNS servers, so its records can't be edited "
                "through the Namecheap API. Use the provider that actually hosts its DNS."
            )
        hosts = [
            {
                "Name": h.get("Name", ""),
                "Type": h.get("Type", ""),
                "Address": h.get("Address", ""),
                "MXPref": h.get("MXPref", "10"),
                "TTL": h.get("TTL", "1800"),
            }
            for h in result.iter("Host")
        ]
        return hosts, result.get("EmailType")

    def _set_hosts(self, domain: str, hosts: list, email_type=None) -> None:
        sld, tld = self._split(domain)
        params = {"SLD": sld, "TLD": tld}
        for i, h in enumerate(hosts, 1):
            params[f"HostName{i}"] = h["Name"]
            params[f"RecordType{i}"] = h["Type"]
            params[f"Address{i}"] = h["Address"]
            params[f"TTL{i}"] = h["TTL"]
            if h["Type"].upper().startswith("MX"):
                params[f"MXPref{i}"] = h["MXPref"]
        if email_type:
            params["EmailType"] = email_type
        root = self._call("namecheap.domains.dns.setHosts", params, post=True)
        res = root.find(".//DomainDNSSetHostsResult")
        if res is not None and (res.get("IsSuccess") or "").lower() == "false":
            raise DnsProviderError(f"Namecheap setHosts for {domain} reported IsSuccess=false")

    @contextlib.contextmanager
    def _lock(self, domain: str):
        # setHosts is read-modify-write across the whole zone; serialise it.
        path = os.path.join(tempfile.gettempdir(), "acme-namecheap-" + re.sub(r"[^a-z0-9.-]", "_", domain) + ".lock")
        with open(path, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def add_txt_record(self, fqdn: str, value: str) -> None:
        domain = self._find_domain(fqdn)
        name = relative_name(fqdn, domain)
        with self._lock(domain):
            hosts, email_type = self._get_hosts(domain)
            if any(h["Type"].upper() == "TXT" and h["Name"].lower() == name and h["Address"] == value for h in hosts):
                return
            hosts.append({"Name": name, "Type": "TXT", "Address": value, "MXPref": "10", "TTL": str(CHALLENGE_TTL)})
            self._set_hosts(domain, hosts, email_type)

    def remove_txt_record(self, fqdn: str, value: str) -> None:
        domain = self._find_domain(fqdn)
        name = relative_name(fqdn, domain)
        with self._lock(domain):
            hosts, email_type = self._get_hosts(domain)
            kept = [h for h in hosts
                    if not (h["Type"].upper() == "TXT" and h["Name"].lower() == name and h["Address"] == value)]
            if len(kept) == len(hosts):
                return
            self._set_hosts(domain, kept, email_type)

    def test_connection(self) -> str:
        root = self._call("namecheap.domains.getList", {"PageSize": "10"})
        count = len(list(root.iter("Domain")))
        env = "sandbox" if self.settings.get("use_sandbox") else "production"
        return (f"Namecheap API credentials and IP whitelisting are valid ({env}); "
                f"{count} domain(s) visible on the first page.")
