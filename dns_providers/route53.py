"""
AWS Route53 DNS provider plugin.

Uses boto3 if it is available on the appliance (recommended -- handles
SigV4 signing, retries and credential discovery for you). Falls back to a
clear error telling the operator to `pip install boto3` if it's missing,
rather than failing with a confusing traceback.

Expected `settings` keys:
    hosted_zone_id      e.g. "Z1234567890ABC"
    aws_access_key_id       optional if using instance/role credentials
    aws_secret_access_key   optional if using instance/role credentials
    region                  optional, default "us-east-1"
"""

from .base import BaseDnsProvider, DnsProviderError

try:
    import boto3
except ImportError:  # pragma: no cover
    boto3 = None


class Route53Provider(BaseDnsProvider):
    def __init__(self, settings: dict):
        super().__init__(settings)
        if boto3 is None:
            raise DnsProviderError(
                "boto3 is not installed. Run 'pip install boto3' on the "
                "appliance to enable the route53 DNS provider."
            )
        kwargs = {}
        if settings.get("aws_access_key_id"):
            kwargs["aws_access_key_id"] = settings["aws_access_key_id"]
            kwargs["aws_secret_access_key"] = settings["aws_secret_access_key"]
        kwargs["region_name"] = settings.get("region", "us-east-1")
        self.client = boto3.client("route53", **kwargs)

    def _existing_values(self, fqdn: str) -> list:
        """
        Return the current list of TXT record values at fqdn (each a
        quoted string like '"abc123"'), or [] if no TXT record set
        exists there yet. Needed so add/remove can merge rather than
        clobber -- a wildcard cert (e.g. "*.example.com") requested
        together with its apex ("example.com") both need a distinct TXT
        value present at "_acme-challenge.example.com" simultaneously.
        """
        resp = self.client.list_resource_record_sets(
            HostedZoneId=self.settings["hosted_zone_id"],
            StartRecordName=fqdn, StartRecordType="TXT", MaxItems="1",
        )
        for rrset in resp.get("ResourceRecordSets", []):
            # Route53 names come back with a trailing dot.
            if rrset["Name"].rstrip(".") == fqdn.rstrip(".") and rrset["Type"] == "TXT":
                return [r["Value"] for r in rrset.get("ResourceRecords", [])]
        return []

    def _upsert(self, fqdn: str, values: list) -> None:
        self.client.change_resource_record_sets(
            HostedZoneId=self.settings["hosted_zone_id"],
            ChangeBatch={"Changes": [{
                "Action": "UPSERT",
                "ResourceRecordSet": {
                    "Name": fqdn, "Type": "TXT", "TTL": 60,
                    "ResourceRecords": [{"Value": v} for v in values],
                },
            }]},
        )

    def _delete(self, fqdn: str, values: list) -> None:
        try:
            self.client.change_resource_record_sets(
                HostedZoneId=self.settings["hosted_zone_id"],
                ChangeBatch={"Changes": [{
                    "Action": "DELETE",
                    "ResourceRecordSet": {
                        "Name": fqdn, "Type": "TXT", "TTL": 60,
                        "ResourceRecords": [{"Value": v} for v in values],
                    },
                }]},
            )
        except Exception as exc:  # noqa: BLE001
            if "not found" in str(exc).lower():
                return
            raise DnsProviderError(f"Route53 DELETE failed: {exc}") from exc

    def add_txt_record(self, fqdn: str, value: str) -> None:
        quoted = f'"{value}"'
        try:
            existing = self._existing_values(fqdn)
            if quoted in existing:
                return  # already present -- nothing to do (idempotent)
            self._upsert(fqdn, existing + [quoted])
        except Exception as exc:  # noqa: BLE001
            raise DnsProviderError(f"Route53 add_txt_record failed: {exc}") from exc

    def remove_txt_record(self, fqdn: str, value: str) -> None:
        quoted = f'"{value}"'
        try:
            existing = self._existing_values(fqdn)
            remaining = [v for v in existing if v != quoted]
            if not remaining:
                self._delete(fqdn, existing or [quoted])
            elif remaining != existing:
                self._upsert(fqdn, remaining)
        except Exception as exc:  # noqa: BLE001
            if "not found" in str(exc).lower():
                return
            raise DnsProviderError(f"Route53 remove_txt_record failed: {exc}") from exc

    def test_connection(self) -> str:
        if not self.settings.get("hosted_zone_id"):
            raise DnsProviderError("hosted_zone_id is not set")
        try:
            resp = self.client.get_hosted_zone(Id=self.settings["hosted_zone_id"])
        except Exception as exc:  # noqa: BLE001
            raise DnsProviderError(f"Could not read hosted zone: {exc}") from exc
        name = resp["HostedZone"]["Name"]
        return f"Authenticated to AWS and confirmed access to hosted zone '{name}'."
