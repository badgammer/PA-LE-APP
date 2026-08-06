# ACME / Let's Encrypt Appliance for Palo Alto GlobalProtect

A lightweight Linux appliance that issues and renews Let's Encrypt (ACME)
certificates via DNS-01 validation, using a pluggable DNS provider
architecture, and automatically deploys the renewed certificate to one
or more Palo Alto firewalls' GlobalProtect portal (or gateway) SSL/TLS
Service Profile.

## Cross-zone SAN certificates (per-name DNS provider override)

Normally, every name on a domains[] entry -- the primary name and every
`additional_names` entry -- uses that entry's single top-level
`dns_provider` for its DNS-01 challenge. This works fine as long as
every name lives in the same DNS zone/account (e.g. a wildcard + its
apex domain).

If you need a SAN certificate covering names in **different** DNS
zones or accounts (e.g. your own domain plus a partner/customer's
domain, or the same zone split across two cloud subscriptions), each
`additional_names` entry can instead be an object specifying its own
`dns_provider`, overriding the entry's default for just that one name:

```yaml
domains:
  - name: vpn.howardscams.com
    dns_provider: azure-howardscams          # default for this entry
    additional_names:
      - apex.howardscams.com                  # plain string -> uses azure-howardscams (default)
      - name: portal.otherdomain.com          # override -> uses a different provider
        dns_provider: azure-otherdomain
    cert_name_prefix: gp-portal-cert
    panos_targets: [...]
```

Both `azure-howardscams` and `azure-otherdomain` must exist under
`dns_providers[]` (each configured for its own zone/account) -- the
appliance resolves the correct provider independently for each name
during the DNS-01 challenge. This is fully backward-compatible: existing
configs with plain-string `additional_names` lists are unaffected.

In the web UI, each "Additional name" row on the Domain form has its own
DNS provider dropdown (defaulting to "(same as primary)"); attempting to
delete a DNS provider that's still referenced -- either as an entry's
primary provider OR as a per-name override -- is blocked with a clear
error listing which domain(s) depend on it.

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

### 6. Panorama-managed firewalls
`panos/client.py` uses a full-object `type=edit` (matching the GUI)
instead of a partial `type=set`, so no manual CLI override is needed.

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
