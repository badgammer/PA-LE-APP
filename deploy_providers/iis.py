"""
IIS (Windows Internet Information Services) deploy provider -- pushes a
Let's Encrypt certificate to a remote Windows/IIS server over WinRM and
binds it to one of that server's IIS site HTTPS bindings.

Requires the `pywinrm` package (`pip install pywinrm`) in the appliance's
venv, and WinRM listening on the target Windows host -- typically
HTTPS/5986 with NTLM or Kerberos auth. One-time setup on the WINDOWS side
(not this appliance):
    winrm quickconfig -transport:https
    winrm set winrm/config/service/auth '@{Basic="false";Kerberos="true"}'
    New-NetFirewallRule -DisplayName "WinRM HTTPS" -Direction Inbound -LocalPort 5986 -Protocol TCP -Action Allow
See the README for the full walkthrough, including the self-signed WinRM
listener certificate case (set verify_tls=false on this instance, exactly
like PAN-OS's verify_tls setting handles self-signed mgmt certs).

### What actually happens on each deploy
1. certbot only ever gives us PEM files (fullchain.pem + privkey.pem).
   IIS/Windows needs a PKCS#12 (.pfx) bundle instead, so this module
   shells out to the LOCAL `openssl` binary -- already a hard dependency
   of this appliance (see bin/generate-selfsigned-cert.sh) -- to build
   one, protected by a randomly-generated, single-use passphrase that is
   never written to disk and never logged, only held in-memory for this
   one deploy. This mirrors how the System Updates feature's step-up
   sudo password is handled (see webui/system_updates.py) -- a
   deliberate, consistent pattern across this appliance for any secret
   that only needs to exist for the duration of one operation.
2. The .pfx bytes are base64-encoded and written to a temp file on the
   REMOTE Windows host via a small PowerShell script run over WinRM,
   then imported into the Windows local machine certificate store with
   Import-PfxCertificate. This returns a certificate object, whose
   FriendlyName we immediately set to our own cert_name (the
   "<prefix>-<timestamp>" name this appliance already uses everywhere
   else) so cleanup_old_certificates below can find/skip it by name,
   the same way PanosClient.cleanup_old_certificates matches on the
   PAN-OS certificate object's own name.
3. The temp .pfx file on the Windows host is deleted immediately after
   import (whether it succeeded or not) -- the private key only ever
   needs to touch disk for the moment Import-PfxCertificate reads it.
4. The target IIS site's HTTPS binding (matched by IP:port, and by
   hostname too if SNI is in use) is pointed at the new certificate via
   `netsh http` -- the same underlying mechanism IIS Manager itself uses
   for SSL bindings. If no matching binding exists yet, one is created;
   if one already exists it is deleted and re-added, since netsh has no
   "update sslcert in place" operation.
5. (Optional) old certificates sharing this appliance's naming prefix
   are removed from the store, skipping the one just imported -- see
   cleanup_old_certificates below.
"""
import base64
import logging
import os
import secrets
import subprocess
import tempfile

from .base import BaseDeployProvider, DeployProviderError

try:
    import winrm
except ImportError:  # pragma: no cover
    winrm = None

log = logging.getLogger("deploy_providers.iis")

DEFAULT_STORE = "Cert:\\LocalMachine\\My"

# Fixed, appliance-owned GUID used as the appid for every sslcert binding
# this appliance creates via netsh -- lets an administrator identify
# "who owns this binding" with `netsh http show sslcert` without it being
# tied to any particular application's real AppID.
_APPLIANCE_APPID = "{6f2f8f2a-1e39-4d7a-9b3e-8b1c9c2f7a10}"


class IisDeployProvider(BaseDeployProvider):
    def __init__(self, settings: dict):
        super().__init__(settings)
        if winrm is None:
            raise DeployProviderError(
                "pywinrm is not installed. Run 'pip install pywinrm' inside the "
                "appliance's venv (/opt/acme-appliance/venv/bin/pip) to enable the "
                "iis deploy provider."
            )

    # ------------------------------------------------------------ session
    def _session(self):
        hostname = self.settings["hostname"]
        port = int(self.settings.get("winrm_port", 5986))
        transport = self.settings.get("transport", "ntlm")
        scheme = "http" if port == 5985 else "https"
        endpoint = f"{scheme}://{hostname}:{port}/wsman"
        return winrm.Session(
            endpoint,
            auth=(self.settings.get("username", ""), self.settings.get("password", "")),
            transport=transport,
            server_cert_validation=("validate" if self.settings.get("verify_tls", False) else "ignore"),
            operation_timeout_sec=int(self.settings.get("timeout", 60)),
            read_timeout_sec=int(self.settings.get("timeout", 60)) + 10,
        )

    def _run_ps(self, session, script: str) -> str:
        result = session.run_ps(script)
        if result.status_code != 0:
            raise DeployProviderError(
                f"Remote PowerShell command on {self.settings.get('hostname', '')} failed "
                f"(exit {result.status_code}): {result.std_err.decode(errors='replace').strip()}"
            )
        return result.std_out.decode(errors="replace").strip()

    # -------------------------------------------------------------- pfx
    def _build_pfx(self, cert_path: str, key_path: str):
        """
        Returns (base64_pfx_bytes: str, passphrase: str). Uses the LOCAL
        openssl binary to bundle certbot's fullchain.pem + privkey.pem
        into the single PKCS#12 file Import-PfxCertificate expects.
        """
        passphrase = secrets.token_urlsafe(24)
        fd, pfx_path = tempfile.mkstemp(suffix=".pfx")
        os.close(fd)
        try:
            subprocess.run(
                [
                    "openssl", "pkcs12", "-export",
                    "-out", pfx_path,
                    "-inkey", key_path,
                    "-in", cert_path,
                    "-passout", f"pass:{passphrase}",
                ],
                check=True, capture_output=True, timeout=30,
            )
            with open(pfx_path, "rb") as f:
                return base64.b64encode(f.read()).decode(), passphrase
        except subprocess.CalledProcessError as exc:
            raise DeployProviderError(
                f"Could not build a .pfx bundle via openssl: {exc.stderr.decode(errors='replace')}"
            ) from exc
        finally:
            try:
                os.remove(pfx_path)
            except OSError:
                pass

    # ---------------------------------------------------------- deploy
    def deploy_certificate(self, cert_name, cert_path, key_path, target_entry, domain_name=None):
        b64_pfx, passphrase = self._build_pfx(cert_path, key_path)
        store = self.settings.get("cert_store_location", DEFAULT_STORE)
        site_name = target_entry.get("site_name", "")
        binding_ip = target_entry.get("binding_ip") or "*"
        binding_port = target_entry.get("binding_port") or 443
        hostname_sni = target_entry.get("hostname") or ""

        if not site_name:
            raise DeployProviderError("iis deploy target entry is missing 'site_name'.")

        session = self._session()

        # Steps 1-2: write the .pfx to a remote temp file, import it,
        # tag it with our own cert_name as FriendlyName (so cleanup can
        # find it later), then delete the temp file no matter what.
        import_script = f"""
$ErrorActionPreference = 'Stop'
$tmp = [System.IO.Path]::GetTempFileName() + '.pfx'
try {{
    [IO.File]::WriteAllBytes($tmp, [Convert]::FromBase64String('{b64_pfx}'))
    $securePw = ConvertTo-SecureString -String '{passphrase}' -AsPlainText -Force
    $cert = Import-PfxCertificate -FilePath $tmp -CertStoreLocation '{store}' -Password $securePw -Exportable
    $cert.FriendlyName = '{cert_name}'
    Write-Output $cert.Thumbprint
}} finally {{
    Remove-Item -Path $tmp -ErrorAction SilentlyContinue
}}
""".strip()
        thumbprint = self._run_ps(session, import_script).strip().upper()
        if not thumbprint:
            raise DeployProviderError("Import-PfxCertificate did not return a certificate thumbprint.")
        log.info(
            "Imported certificate %s to %s (%s) as thumbprint %s",
            cert_name, self.settings.get("hostname", ""), store, thumbprint,
        )

        # Step 4: point the IIS binding at the new thumbprint via netsh
        # http -- delete any existing sslcert entry at this exact
        # ip:port(+hostname) first (netsh has no in-place update), then
        # add the new one. A missing "delete" target is expected and
        # harmless on first-ever deploy for a binding, so errors from
        # the delete step are suppressed.
        ipport = f"{binding_ip}:{binding_port}" if binding_ip != "*" else f"0.0.0.0:{binding_port}"
        if hostname_sni:
            selector = f"hostnameport={hostname_sni}:{binding_port}"
        else:
            selector = f"ipport={ipport}"

        binding_script = f"""
$ErrorActionPreference = 'SilentlyContinue'
netsh http delete sslcert {selector} | Out-Null
$ErrorActionPreference = 'Stop'
netsh http add sslcert {selector} certhash={thumbprint} appid='{_APPLIANCE_APPID}' certstorename=MY
""".strip()
        self._run_ps(session, binding_script)
        log.info(
            "Bound certificate %s (thumbprint %s) to IIS site '%s' (%s)%s",
            cert_name, thumbprint, site_name, ipport,
            f", SNI host={hostname_sni}" if hostname_sni else "",
        )

    # --------------------------------------------------------- cleanup
    def cleanup_old_certificates(self, prefix, keep_name):
        if not self.settings.get("cleanup_old_certs"):
            return
        store = self.settings.get("cert_store_location", DEFAULT_STORE)
        session = self._session()
        script = f"""
$ErrorActionPreference = 'Stop'
Get-ChildItem -Path '{store}' |
    Where-Object {{ $_.FriendlyName -like '{prefix}*' -and $_.FriendlyName -ne '{keep_name}' }} |
    Remove-Item -Force
""".strip()
        try:
            self._run_ps(session, script)
        except DeployProviderError as exc:
            # Mirrors PanosClient.cleanup_old_certificates: a cleanup
            # failure is logged, not fatal to the deploy that already
            # succeeded.
            log.warning("Could not clean up stale certificates on %s: %s", self.settings.get("hostname", ""), exc)

    # ------------------------------------------------------------- test
    def test_connection(self) -> str:
        session = self._session()
        out = self._run_ps(
            session,
            "$env:COMPUTERNAME; (Get-CimInstance Win32_OperatingSystem).Caption",
        )
        lines = out.splitlines()
        computer = lines[0].strip() if lines else self.settings.get("hostname", "")
        os_caption = lines[1].strip() if len(lines) > 1 else "unknown OS"
        return f"Connected to {computer} ({os_caption}) via WinRM."

    # ------------------------------------------------------ list_options
    def list_options(self, **kwargs) -> list:
        session = self._session()
        out = self._run_ps(
            session,
            "Import-Module WebAdministration -ErrorAction Stop; "
            "(Get-Website | Select-Object -ExpandProperty Name)",
        )
        return [line.strip() for line in out.splitlines() if line.strip()]
