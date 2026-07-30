"""Cloudflare DNS provider plugin (API token based)."""
import requests
from .base import BaseDnsProvider, DnsProviderError

API = "https://api.cloudflare.com/client/v4"


class CloudflareProvider(BaseDnsProvider):
    def _headers(self):
        return {"Authorization": f"Bearer {self.settings['api_token']}", "Content-Type": "application/json"}

    def _zone_id(self, fqdn: str) -> str:
        labels = fqdn.split(".")
        for i in range(len(labels) - 1):
            candidate = ".".join(labels[i:])
            r = requests.get(f"{API}/zones", headers=self._headers(), params={"name": candidate}, timeout=15)
            r.raise_for_status()
            result = r.json().get("result", [])
            if result:
                return result[0]["id"]
        raise DnsProviderError(f"No Cloudflare zone found for {fqdn}")

    def add_txt_record(self, fqdn: str, value: str) -> None:
        zone_id = self._zone_id(fqdn)
        payload = {"type": "TXT", "name": fqdn, "content": value, "ttl": 120}
        r = requests.post(f"{API}/zones/{zone_id}/dns_records", headers=self._headers(), json=payload, timeout=15)
        if not r.ok or not r.json().get("success"):
            raise DnsProviderError(f"Cloudflare add_txt_record failed: {r.text}")

    def remove_txt_record(self, fqdn: str, value: str) -> None:
        zone_id = self._zone_id(fqdn)
        r = requests.get(f"{API}/zones/{zone_id}/dns_records", headers=self._headers(),
                          params={"type": "TXT", "name": fqdn, "content": value}, timeout=15)
        r.raise_for_status()
        for record in r.json().get("result", []):
            requests.delete(f"{API}/zones/{zone_id}/dns_records/{record['id']}", headers=self._headers(), timeout=15)

    def test_connection(self) -> str:
        if not self.settings.get("api_token"):
            raise DnsProviderError("api_token is not set")
        r = requests.get(f"{API}/user/tokens/verify", headers=self._headers(), timeout=15)
        if not r.ok or not r.json().get("success"):
            raise DnsProviderError(f"Token verification failed: {r.text}")
        return "Cloudflare API token is valid."
