"""Generic REST/webhook DNS provider."""
import requests
from .base import BaseDnsProvider, DnsProviderError


class GenericWebhookProvider(BaseDnsProvider):
    def _headers(self) -> dict:
        headers = dict(self.settings.get("headers", {}))
        if self.settings.get("auth_header"):
            headers.setdefault("Authorization", self.settings["auth_header"])
        return headers

    def _call(self, url: str, fqdn: str, value: str) -> None:
        method = self.settings.get("method", "POST")
        headers = self._headers()
        verify = self.settings.get("verify_tls", True)
        r = requests.request(method, url, json={"fqdn": fqdn, "value": value}, headers=headers, verify=verify, timeout=20)
        if not r.ok:
            raise DnsProviderError(f"Webhook call to {url} failed: {r.status_code} {r.text}")

    def add_txt_record(self, fqdn: str, value: str) -> None:
        self._call(self.settings["add_url"], fqdn, value)

    def remove_txt_record(self, fqdn: str, value: str) -> None:
        self._call(self.settings["remove_url"], fqdn, value)

    def test_connection(self) -> str:
        add_url = self.settings.get("add_url")
        remove_url = self.settings.get("remove_url")
        if not add_url or not remove_url:
            raise DnsProviderError("add_url and remove_url must both be set")
        headers = self._headers()
        verify = self.settings.get("verify_tls", True)
        try:
            r = requests.head(add_url, headers=headers, verify=verify, timeout=10)
        except requests.RequestException as exc:
            raise DnsProviderError(f"Could not reach {add_url}: {exc}") from exc
        return f"Reached {add_url} (HTTP {r.status_code}). This only checks reachability -- use a real renewal to confirm add/remove logic."
