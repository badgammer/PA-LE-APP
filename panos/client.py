"""
Thin PAN-OS XML/REST API client used to deploy an ACME-issued certificate
to one or more Palo Alto firewalls and point the GlobalProtect portal (and
optionally the gateway) SSL/TLS Service Profile at it.

Required PAN-OS admin role permissions for the account used here (Device
> Admin Roles > <role> > XML API tab):
    - Configuration        (get/set profile, list/delete certificates)
    - Import               (upload the certificate + key)
    - Commit                (commit the change)
    - Operational Requests  (system_info / the web UI's "Test Connection")
All four should be enabled; a role with only some of them will produce
confusing partial failures (e.g. "Test Connection" fails while deploys
still work, or vice versa).
"""

import logging
import time

import requests

requests.packages.urllib3.disable_warnings()

log = logging.getLogger("panos_client")


class PanosError(Exception):
    pass


class PanosClient:
    def __init__(self, hostname: str, api_key: str = None,
                 username: str = None, password: str = None,
                 verify_tls: bool = False, timeout: int = 30):
        self.base = f"https://{hostname}/api/"
        self.verify_tls = verify_tls
        self.timeout = timeout
        self.api_key = api_key or self._keygen(username, password)

    def _keygen(self, username: str, password: str) -> str:
        if not username or not password:
            raise PanosError("No api_key provided and no username/password to generate one.")
        r = requests.get(
            self.base, params={"type": "keygen", "user": username, "password": password},
            verify=self.verify_tls, timeout=self.timeout,
        )
        r.raise_for_status()
        import xml.etree.ElementTree as ET
        root = ET.fromstring(r.text)
        key = root.findtext(".//key")
        if not key:
            raise PanosError(f"keygen did not return a key: {r.text}")
        return key

    def _get(self, params: dict):
        params = dict(params)
        params["key"] = self.api_key
        r = requests.get(self.base, params=params, verify=self.verify_tls, timeout=self.timeout)
        r.raise_for_status()
        return r.text

    def _post_files(self, params: dict, files: dict):
        params = dict(params)
        params["key"] = self.api_key
        r = requests.post(self.base, params=params, files=files,
                           verify=self.verify_tls, timeout=self.timeout)
        r.raise_for_status()
        return r.text

    @staticmethod
    def _check_success(xml_text: str, context: str):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml_text)
        if root.attrib.get("status") != "success":
            msg = xml_text
            lowered = xml_text.lower()
            if "not authorized" in lowered or "permission" in lowered or "unauthorized" in lowered:
                msg += (
                    "\nHint: this usually means the admin role assigned to this API "
                    "account is missing a required XML API permission. Check Device > "
                    "Admin Roles > <role> > XML API tab and make sure Configuration, "
                    "Import, Commit, and Operational Requests are all enabled."
                )
            elif "override" in lowered and "template" in lowered:
                msg += (
                    "\nHint: this firewall is Panorama-managed and the object being "
                    "modified (e.g. the SSL/TLS Service Profile) is defined in a "
                    "Panorama-pushed template, which blocks direct local edits until "
                    "it's overridden. Either (a) SSH to the firewall and run "
                    "'configure' / 'override network ssl-tls-service-profile "
                    "<profile-name>' / 'set ...' / 'commit' once to create a local "
                    "override this appliance can then update on future renewals, or "
                    "(b) manage this certificate assignment from Panorama itself "
                    "instead of the local firewall API."
                )
            raise PanosError(f"{context} failed: {msg}")
        return root

    def system_info(self) -> dict:
        text = self._get({"type": "op", "cmd": "<show><system><info></info></system></show>"})
        root = self._check_success(text, "system_info")
        info = root.find(".//system")
        if info is None:
            raise PanosError(f"Unexpected response to system info request: {text}")
        return {
            "hostname": info.findtext("hostname", ""),
            "model": info.findtext("model", ""),
            "sw-version": info.findtext("sw-version", ""),
        }

    def import_certificate(self, cert_name: str, cert_path: str, key_path: str,
                            passphrase: str = None) -> None:
        """
        Imports a certificate AND its private key together onto the
        firewall as a PAN-OS "keypair" object (category=keypair), which
        requires a SINGLE PEM file containing both the certificate and
        the private key concatenated together in one multipart "file"
        field. category=certificate (the old, broken approach here)
        imports ONLY the public certificate with no private key at all.

        passphrase: PAN-OS's keypair import API requires this parameter
        to be present (non-empty) on every call, but it's only actually
        used to decrypt the private key if that key's PEM content
        indicates it's encrypted. certbot's default keys are not
        encrypted, so an inert placeholder is sent when none is given.
        """
        with open(cert_path, "rb") as cert_f:
            cert_bytes = cert_f.read()
        with open(key_path, "rb") as key_f:
            key_bytes = key_f.read()

        combined = cert_bytes
        if not combined.endswith(b"\n"):
            combined += b"\n"
        combined += key_bytes

        params = {
            "type": "import", "category": "keypair",
            "certificate-name": cert_name, "format": "pem",
            "passphrase": passphrase or "unused-key-is-not-encrypted",
        }
        files = {"file": (f"{cert_name}.pem", combined)}
        text = self._post_files(params, files)
        self._check_success(text, f"import_certificate({cert_name})")
        log.info("Imported certificate+key %s", cert_name)

    def set_ssl_tls_profile_certificate(self, profile_name: str, cert_name: str,
                                         vsys: str = None) -> None:
        if vsys:
            base_xpath = (
                f"/config/devices/entry/vsys/entry[@name='{vsys}']"
                f"/ssl-tls-service-profile/entry[@name='{profile_name}']"
            )
        else:
            base_xpath = f"/config/shared/ssl-tls-service-profile/entry[@name='{profile_name}']"
        params = {
            "type": "config", "action": "set",
            "xpath": f"{base_xpath}/certificate",
            "element": f"<certificate>{cert_name}</certificate>",
        }
        text = self._get(params)
        self._check_success(text, f"set_ssl_tls_profile_certificate({profile_name})")
        log.info("SSL/TLS profile %s now references %s", profile_name, cert_name)

    def set_globalprotect_portal_certificate(self, portal_name: str, cert_name: str) -> None:
        xpath = (
            "/config/devices/entry/network/global-protect/portal/"
            f"entry[@name='{portal_name}']/certificate"
        )
        params = {
            "type": "config", "action": "set", "xpath": xpath,
            "element": f"<certificate>{cert_name}</certificate>",
        }
        text = self._get(params)
        self._check_success(text, f"set_globalprotect_portal_certificate({portal_name})")

    def delete_certificate(self, cert_name: str) -> None:
        xpath = f"/config/shared/certificate/entry[@name='{cert_name}']"
        params = {"type": "config", "action": "delete", "xpath": xpath}
        text = self._get(params)
        self._check_success(text, f"delete_certificate({cert_name})")

    def list_certificates(self):
        import xml.etree.ElementTree as ET
        text = self._get({"type": "config", "action": "get", "xpath": "/config/shared/certificate"})
        root = ET.fromstring(text)
        return [e.attrib["name"] for e in root.findall(".//certificate/entry")]

    def list_ssl_tls_profiles(self, vsys: str = None):
        import xml.etree.ElementTree as ET
        names = []

        def _fetch(xpath):
            try:
                text = self._get({"type": "config", "action": "get", "xpath": xpath})
                root = ET.fromstring(text)
                return [e.attrib["name"] for e in root.findall(".//ssl-tls-service-profile/entry")]
            except PanosError:
                return []

        names.extend(_fetch("/config/shared/ssl-tls-service-profile"))
        if vsys:
            names.extend(_fetch(
                f"/config/devices/entry/vsys/entry[@name='{vsys}']/ssl-tls-service-profile"
            ))
        else:
            names.extend(_fetch(
                "/config/devices/entry/vsys/entry[@name='vsys1']/ssl-tls-service-profile"
            ))
        seen = set()
        unique = []
        for n in names:
            if n not in seen:
                seen.add(n)
                unique.append(n)
        return unique

    def cleanup_old_certificates(self, prefix: str, keep_name: str) -> None:
        for name in self.list_certificates():
            if name.startswith(prefix) and name != keep_name:
                try:
                    self.delete_certificate(name)
                    log.info("Cleaned up stale certificate %s", name)
                except PanosError as exc:
                    log.warning("Could not delete stale cert %s: %s", name, exc)

    def commit(self, description: str = "ACME appliance certificate update",
                poll_interval: int = 5, poll_timeout: int = 300) -> None:
        import xml.etree.ElementTree as ET
        text = self._get({
            "type": "commit",
            "cmd": f"<commit><description>{description}</description></commit>",
        })
        root = self._check_success(text, "commit")
        job_id = root.findtext(".//job")
        if not job_id:
            log.info("Commit returned no job id (nothing to commit?)")
            return
        deadline = time.time() + poll_timeout
        while time.time() < deadline:
            status_text = self._get({"type": "op", "cmd": f"<show><jobs><id>{job_id}</id></jobs></show>"})
            status_root = ET.fromstring(status_text)
            job_status = status_root.findtext(".//job/status")
            if job_status == "FIN":
                result = status_root.findtext(".//job/result")
                if result != "OK":
                    raise PanosError(f"Commit job {job_id} finished with result={result}")
                log.info("Commit job %s finished OK", job_id)
                return
            time.sleep(poll_interval)
        raise PanosError(f"Commit job {job_id} did not finish within {poll_timeout}s")
