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

### 6. Panorama-managed firewalls
`panos/client.py` surfaces a specific hint for PAN-OS's "may need to
override template object first" error.

### 7. Critical vs optional OS packages during setup (important)
`iso-build/bootstrap-appliance.sh` installs only genuinely-required
packages (`python3`, `python3-pip`, `certbot`, `openssl`) in one `dnf`
transaction. Everything else that's merely convenient
(`dnf-utils`, `policycoreutils-python-utils`) is installed separately,
each in its own transaction, with a warning logged instead of the whole
setup aborting if one fails.

This matters because a single `dnf install <pkg1> <pkg2> ...` command
is all-or-nothing -- if even one package has an unresolvable dependency
(e.g. a repo/mirror metadata inconsistency), the ENTIRE transaction
fails, silently blocking every other package in that same command,
including certbot. This has happened twice: once with `python3-venv`
(not a real package on Rocky/RHEL at all), and once with
`policycoreutils-python-utils` (an unresolvable dependency on some
systems/mirrors). Neither package is actually required for this
appliance to run -- see `iso-build/README.md` for the full explanation,
including why SELinux port-labeling (`semanage`, which
`policycoreutils-python-utils` provides) normally isn't needed here at
all.

**A note on OS version:** this appliance was built and tested against
Rocky/RHEL 9. If you're deploying to Rocky/RHEL 10 (or another EL10
derivative), package names and default behavior should mostly carry
over, but this hasn't been extensively validated on EL10 -- if you hit
anything else EL10-specific, please flag it.

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

See `iso-build/README.md` for manual setup, unattended ISO, Packer image
build options, and the critical-vs-optional package installation design.

## Adding a new DNS provider

1. Create `dns_providers/<name>.py` implementing `add_txt_record` / `remove_txt_record` from `base.py`.
2. Register it in `dns_providers/__init__.py`.
3. Restart `acme-webui.service`.
