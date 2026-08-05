# ACME / Let's Encrypt Appliance for Palo Alto GlobalProtect

A lightweight Linux appliance that issues and renews Let's Encrypt (ACME)
certificates via DNS-01 validation, using a **pluggable DNS provider
architecture**, and automatically deploys the renewed certificate to one
or more Palo Alto firewalls' GlobalProtect portal (or gateway) SSL/TLS
Service Profile. Includes a web UI for configuring everything, ACME
account settings, certificate export, a "redeploy without re-issuing"
action, OS update checking/applying, and kickstart/Packer/ISO tooling.

## Redeploying an already-issued certificate

Every domain on the Domains page (and its Edit page) has a **Redeploy to
firewall** button once a certificate has been issued for it. This re-runs
ONLY the PAN-OS import/attach/commit steps against the certificate
already sitting on disk -- no new ACME issuance, no DNS-01 challenge, and
no Let's Encrypt rate-limit usage at all (Let's Encrypt limits you to 5
duplicate certificates per exact domain set per week; "Force renew now"
counts against that limit every time, "Redeploy to firewall" never does).

Use this when the certificate itself is fine but the firewall-side
deployment needs fixing or redoing -- e.g. after correcting an SSL/TLS
Service Profile name, adding/removing a firewall target, or after a
previous deploy attempt failed partway through (such as the
`category=certificate` vs `category=keypair` PAN-OS import bug this
appliance used to have, which resulted in certificates missing their
private key on the firewall with no commit ever attempted).

Implementation: `bin/redeploy-cert.sh <domain>` locates the existing
certbot lineage for that domain and feeds it to the exact same
`deploy_to_panos.py` that certbot's own `--deploy-hook` uses -- that
script has no idea (and doesn't need to know) whether it was invoked by
certbot after a real renewal or by this script against an
already-issued certificate. Renewal and redeploy jobs for the same
domain share a "busy" check (in `webui/app.py`), so they can't run
concurrently and race each other on the same firewall commit.

## Known gotchas already fixed here

### 1. ACME account email must be real
Set via the web UI's **Settings** page. Placeholder domains
(`@example.com` etc.) are rejected up front with a clear message.

### 2. certbot version compatibility
`bin/acme-renew.sh` auto-detects the installed certbot's major version
and includes/omits `--manual-public-ip-logging-ok` accordingly.

### 3. DNS zone name case-sensitivity
`dns_providers/azure.py` / `route53.py` compare zone names
case-insensitively (DNS names are case-insensitive per RFC 1035/4343).

### 4. PAN-OS certificate + private key import
`panos/client.py` uses `category=keypair` with a single combined PEM
file, not the old broken `category=certificate` + separate "keyfile"
approach (which silently dropped the private key entirely).

## Web UI pages

Dashboard, Domains (add/edit, SSL/TLS profile picker, wildcard+SAN
support, cert download, per-domain renew/force-renew/redeploy), DNS
Providers, Firewalls, Settings (ACME email/server), System (OS update
check/apply/reboot with step-up Linux sudo-group auth), Logs.

## Getting a running appliance

```bash
sudo dnf update -y
ip a #Write this down to get into the webui
sudo dnf install git -y
git clone <your-repo-url> /tmp/acme-appliance-src
cd /tmp/acme-appliance-src
sudo ./iso-build/bootstrap-appliance.sh
```

See `iso-build/README.md` for manual setup, unattended ISO, and Packer
image build options.

## Adding a new DNS provider

1. Create `dns_providers/<name>.py` implementing `add_txt_record` /
   `remove_txt_record` from `base.py`.
2. Register it in `dns_providers/__init__.py`.
3. Restart `acme-webui.service`.

## Known follow-ups / roadmap

- Add a Panorama (template/device-group) variant of `panos/client.py`.
- Add Slack/Teams/email notification on renewal or update failure.
- A proper image-update mechanism for the prebuilt-image options.
