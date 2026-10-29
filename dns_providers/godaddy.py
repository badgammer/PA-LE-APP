"""
GoDaddy DNS provider plugin (Domains API v1, DNS records endpoints).

Works only for domains whose nameservers are GoDaddy's (the DNS records
API edits GoDaddy's own zone). If a customer's domain is registered at
GoDaddy but delegated elsewhere, use the DNS host's provider instead.

Credentials: create an API key at https://developer.godaddy.com/keys.
  - Key + Secret pair      -> sends "Authorization: sso-key KEY:SECRET"
  - Personal access token  -> put the token in "API key" and leave the
                              secret blank; sends "Authorization: Bearer ..."
GoDaddy's first key is an OTE (test) key; create a Production key for real
use (the "Use OTE (test) environment" box only exists for testing).

Expected `settings` keys:
    api_key           key, or personal access token if api_secret is blank
    api_secret        optional (secret)
    domain            optional registered domain (e.g. "example.com");
                      auto-detected from the challenge name when blank
    use_ote           optional bool, use https://api.ote-godaddy.com

TXT handling note: GoDaddy's DELETE on /records/TXT/<name> removes EVERY
TXT value at that name, which would break a wildcard + apex certificate
(two tokens at the same name). So add/remove here read the current values,
then PUT back the merged / filtered list, and only DELETE when nothing
is left.
"""
import requests

from .base import BaseDnsProvider, DnsProviderError, candidate_zones, relative_name

PROD = "https://api.godaddy.com"
OTE = "https://api.ote-godaddy.com"
MIN_TTL = 600  # GoDaddy's minimum for the v1 DNS records API


class GoDaddyProvider(BaseDnsProvider):
    def _base(self) -> str:
        return OTE if self.settings.get("use_ote") else PROD

    def _headers(self) -> dict:
        key = self.settings.get("api_key", "")
        secret = self.settings.get("api_secret", "")
        if not key:
            raise DnsProviderError("GoDaddy api_key is not set")
        auth = f"sso-key {key}:{secret}" if secret else f"Bearer {key}"
        return {"Authorization": auth, "Content-Type": "application/json", "Accept": "application/json"}

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        try:
            return requests.request(method, self._base() + path, headers=self._headers(), timeout=20, **kwargs)
        except requests.RequestException as exc:
            raise DnsProviderError(f"Could not reach GoDaddy API: {exc}") from exc

    @staticmethod
    def _err(r: requests.Response) -> str:
        try:
            body = r.json()
            return f"HTTP {r.status_code}: {body.get('code', '')} {body.get('message', '')}".strip()
        except ValueError:
            return f"HTTP {r.status_code}: {r.text[:200]}"

    def _find_domain(self, fqdn: str) -> str:
        explicit = (self.settings.get("domain") or "").strip().strip(".").lower()
        if explicit:
            return explicit
        problems = []
        for cand in candidate_zones(fqdn):
            r = self._request("GET", f"/v1/domains/{cand}")
            if r.status_code == 200:
                return cand
            if r.status_code == 401:
                raise DnsProviderError(f"GoDaddy rejected the credentials ({self._err(r)})")
            problems.append(f"{cand}: {self._err(r)}")
        raise DnsProviderError(
            f"No domain in this GoDaddy account matches {fqdn}. Tried: {'; '.join(problems)}. "
            "Check the key belongs to the account that owns the domain (and that it is a "
            "Production key, not an OTE key), or set the Domain field explicitly."
        )

    def _get_txt(self, domain: str, name: str) -> list:
        r = self._request("GET", f"/v1/domains/{domain}/records/TXT/{name}")
        if r.status_code == 404:
            return []
        if not r.ok:
            raise DnsProviderError(f"GoDaddy read of TXT {name}.{domain} failed ({self._err(r)})")
        return r.json() or []

    def _put_txt(self, domain: str, name: str, values: list) -> None:
        body = [{"data": v["data"], "ttl": max(int(v.get("ttl", MIN_TTL)), MIN_TTL)} for v in values]
        r = self._request("PUT", f"/v1/domains/{domain}/records/TXT/{name}", json=body)
        if not r.ok:
            raise DnsProviderError(f"GoDaddy update of TXT {name}.{domain} failed ({self._err(r)})")

    def add_txt_record(self, fqdn: str, value: str) -> None:
        domain = self._find_domain(fqdn)
        name = relative_name(fqdn, domain)
        existing = self._get_txt(domain, name)
        if any(rec.get("data") == value for rec in existing):
            return
        self._put_txt(domain, name, existing + [{"data": value, "ttl": MIN_TTL}])

    def remove_txt_record(self, fqdn: str, value: str) -> None:
        domain = self._find_domain(fqdn)
        name = relative_name(fqdn, domain)
        existing = self._get_txt(domain, name)
        remaining = [rec for rec in existing if rec.get("data") != value]
        if len(remaining) == len(existing):
            return  # nothing of ours to remove
        if remaining:
            self._put_txt(domain, name, remaining)
            return
        r = self._request("DELETE", f"/v1/domains/{domain}/records/TXT/{name}")
        if not r.ok and r.status_code != 404:
            raise DnsProviderError(f"GoDaddy delete of TXT {name}.{domain} failed ({self._err(r)})")

    def test_connection(self) -> str:
        domain = (self.settings.get("domain") or "").strip().strip(".")
        path = f"/v1/domains/{domain}" if domain else "/v1/domains?limit=1"
        r = self._request("GET", path)
        if r.status_code in (401, 403):
            raise DnsProviderError(f"GoDaddy rejected the credentials ({self._err(r)})")
        if not r.ok:
            raise DnsProviderError(f"GoDaddy API test failed ({self._err(r)})")
        env = "OTE (test)" if self.settings.get("use_ote") else "production"
        return f"GoDaddy API credentials are valid ({env} environment)."
