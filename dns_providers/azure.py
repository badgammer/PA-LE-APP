"""
Azure DNS provider plugin (service principal / client-credentials auth).

Expected `settings` keys (mirrors the certbot-dns-azure ini fields you are
already using, so migrating existing zones is just copy/paste):
    tenant_id
    sp_client_id
    sp_client_secret
    subscription_id
    resource_group
    zone               e.g. "example.com"
    environment        optional, default "AzurePublicCloud"
"""

import requests

from .base import BaseDnsProvider, DnsProviderError

MGMT = "https://management.azure.com"
API_VERSION = "2018-05-01"


class AzureDnsProvider(BaseDnsProvider):
    def __init__(self, settings: dict):
        super().__init__(settings)
        self._token = None

    def _get_token(self) -> str:
        if self._token:
            return self._token
        url = f"https://login.microsoftonline.com/{self.settings['tenant_id']}/oauth2/v2.0/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": self.settings["sp_client_id"],
            "client_secret": self.settings["sp_client_secret"],
            "scope": "https://management.azure.com/.default",
        }
        r = requests.post(url, data=data, timeout=15)
        if not r.ok:
            raise DnsProviderError(f"Azure token request failed: {r.text}")
        self._token = r.json()["access_token"]
        return self._token

    def _record_url(self, fqdn: str) -> str:
        zone = self.settings["zone"]
        if not fqdn.endswith(zone):
            raise DnsProviderError(f"{fqdn} is not under configured zone {zone}")
        relative_name = fqdn[: -(len(zone) + 1)] or "@"
        sub = self.settings["subscription_id"]
        rg = self.settings["resource_group"]
        return (
            f"{MGMT}/subscriptions/{sub}/resourceGroups/{rg}/providers/"
            f"Microsoft.Network/dnsZones/{zone}/TXT/{relative_name}"
            f"?api-version={API_VERSION}"
        )

    def _get_existing_txt_entries(self, fqdn: str) -> list:
        """
        Return the current list of TXTRecords entries (each like
        {"value": ["some-string"]}) at fqdn, or [] if the record doesn't
        exist yet. Needed so add/remove can merge instead of clobber --
        important when a wildcard cert (e.g. "*.example.com") and its
        apex ("example.com") are requested together, since both ACME
        challenges land on the same "_acme-challenge.example.com" name
        and each needs its own distinct TXT value present at the same time.
        """
        headers = {"Authorization": f"Bearer {self._get_token()}"}
        r = requests.get(self._record_url(fqdn), headers=headers, timeout=15)
        if r.status_code == 404:
            return []
        if not r.ok:
            raise DnsProviderError(f"Azure lookup of existing TXT record failed: {r.text}")
        return r.json().get("properties", {}).get("TXTRecords", [])

    def add_txt_record(self, fqdn: str, value: str) -> None:
        existing = self._get_existing_txt_entries(fqdn)
        if any(entry.get("value") == [value] for entry in existing):
            return  # already present -- nothing to do (idempotent)
        merged = existing + [{"value": [value]}]
        headers = {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
        }
        body = {"properties": {"TTL": 120, "TXTRecords": merged}}
        r = requests.put(self._record_url(fqdn), headers=headers, json=body, timeout=15)
        if not r.ok:
            raise DnsProviderError(f"Azure add_txt_record failed: {r.text}")

    def remove_txt_record(self, fqdn: str, value: str) -> None:
        existing = self._get_existing_txt_entries(fqdn)
        remaining = [entry for entry in existing if entry.get("value") != [value]]
        headers = {"Authorization": f"Bearer {self._get_token()}"}
        if not remaining:
            # No other challenge values left at this name -- delete the
            # record entirely (matches prior behavior for the common
            # single-domain case).
            r = requests.delete(self._record_url(fqdn), headers=headers, timeout=15)
            if r.status_code not in (200, 204, 404):
                raise DnsProviderError(f"Azure remove_txt_record failed: {r.text}")
            return
        # Other challenge value(s) still need to exist (e.g. the sibling
        # apex/wildcard challenge) -- rewrite with just this value removed.
        headers["Content-Type"] = "application/json"
        body = {"properties": {"TTL": 120, "TXTRecords": remaining}}
        r = requests.put(self._record_url(fqdn), headers=headers, json=body, timeout=15)
        if not r.ok:
            raise DnsProviderError(f"Azure remove_txt_record (partial) failed: {r.text}")

    def test_connection(self) -> str:
        required = ["tenant_id", "sp_client_id", "sp_client_secret",
                    "subscription_id", "resource_group", "zone"]
        missing = [k for k in required if not self.settings.get(k)]
        if missing:
            raise DnsProviderError(f"Missing required settings: {', '.join(missing)}")
        token = self._get_token()
        zone = self.settings["zone"]
        sub = self.settings["subscription_id"]
        rg = self.settings["resource_group"]
        url = (
            f"{MGMT}/subscriptions/{sub}/resourceGroups/{rg}/providers/"
            f"Microsoft.Network/dnsZones/{zone}?api-version={API_VERSION}"
        )
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
        if not r.ok:
            raise DnsProviderError(f"Could not read zone {zone}: {r.text}")
        return f"Authenticated to Azure and confirmed access to zone '{zone}'."
