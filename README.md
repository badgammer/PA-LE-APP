# ACME / Let's Encrypt Appliance for Palo Alto GlobalProtect

A lightweight Linux appliance that issues and renews Let's Encrypt (ACME)
certificates via DNS-01 validation, using a pluggable DNS provider
architecture, and automatically deploys the renewed certificate to one
or more Palo Alto firewalls' GlobalProtect portal (or gateway) SSL/TLS
Service Profile.

## Known gotchas already fixed here

### 1. ACME account email must be real
Set via the web UI's **Settings** page.

### 2. certbot version compatibility
`bin/acme-renew.sh` auto-detects certbot's major version.

### 3. DNS zone name case-sensitivity
`dns_providers/azure.py` / `route53.py` compare zone names case-insensitively.

### 4. PAN-OS certificate + private key import
`panos/client.py` uses `category=keypair` with a single combined PEM file.

### 5. Silent deploy failures
`deploy_to_panos.py` correctly exits non-zero if any firewall target fails.

### 6. Panorama-managed firewalls: partial edits rejected, full edits succeed
When updating an SSL/TLS Service Profile's certificate (or a
GlobalProtect portal's certificate) on a Panorama-managed firewall,
PAN-OS's API previously used a `type=set` call targeting just the one
child field being changed (e.g. `.../SSL-Howards/certificate`). If that
object is defined in a Panorama-pushed template, PAN-OS rejects this
partial edit with `"set failed, may need to override template object
first"` -- there's no local copy of the parent object to merge a leaf
value into.

Confirmed via the firewall's own config audit log that the GUI's Edit
dialog succeeds at the exact same task using a `type=edit` action
against the *entire* object's xpath (not a child field), submitting the
complete object back with just the one field changed. PAN-OS creates
the necessary local override as part of that atomic full-object edit.

Fix: `panos/client.py`'s `set_ssl_tls_profile_certificate()` and
`set_globalprotect_portal_certificate()` now do the same thing the GUI
does -- fetch the complete current object (or start with a minimal one
if it doesn't exist yet), update just the `<certificate>` field on that
in-memory copy, and push the *entire* object back via `type=edit`. This
means Panorama-managed profiles now update successfully through the API
with no manual one-time CLI override step required. Verified this
preserves all of an existing profile's other settings (protocol
versions, trust certs, etc.) unchanged, replaces an existing certificate
value in place without duplicating it, and still creates a fresh minimal
object correctly if nothing exists at that xpath yet.

### 7. Critical vs optional OS packages during setup
`iso-build/bootstrap-appliance.sh` installs only genuinely-required
packages in one dnf transaction; convenience packages are installed
separately so one bad dependency can't block the whole setup.

## Redeploying an already-issued certificate

Every domain on the Domains page has a **Redeploy to firewall** button
once a certificate has been issued for it -- re-runs only the PAN-OS
import/attach/commit steps, no new ACME issuance, no Let's Encrypt
rate-limit usage.

## Getting a running appliance

```bash
sudo dnf update -y
ip a #Write this down to get into the webui
sudo dnf install git -y
git clone https://github.com/badgammer/PA-LE-APP /tmp/acme-appliance-src
cd /tmp/acme-appliance-src
sudo bash ./iso-build/bootstrap-appliance.sh
```

See `iso-build/README.md` for manual setup, unattended ISO, and Packer
image build options.

## Adding a new DNS provider

1. Create `dns_providers/<name>.py` implementing `add_txt_record` / `remove_txt_record` from `base.py`.
2. Register it in `dns_providers/__init__.py`.
3. Restart `acme-webui.service`.
