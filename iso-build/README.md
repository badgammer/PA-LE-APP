# Building a prebuilt appliance image

Three options: bootstrap script, unattended ISO, or Packer image build.
All end up running the exact same `bootstrap-appliance.sh`.

```bash
sudo dnf update -y
ip a #Write this down to get into the webui
sudo dnf install git -y
git clone https://github.com/badgammer/PA-LE-APP /tmp/acme-appliance-src
cd /tmp/acme-appliance-src
sudo bash ./iso-build/bootstrap-appliance.sh
```

## After first boot

1. Browse to `https://<appliance-ip>:8443/` and create the admin account.
2. Visit **Settings** and set `acme.email` to a REAL, monitored email address.
3. Add DNS provider(s), firewall(s), and domain(s).
4. Visit **System** to check for updates.

## Known gotchas already fixed in this codebase

- **python3-venv / policycoreutils-python-utils**: critical vs optional
  packages installed in separate dnf transactions.
- **certbot version**: auto-detected at renewal time.
- **DNS zone case-sensitivity**: Azure/Route53 providers compare zone
  names case-insensitively.
- **PAN-OS certificate + private key import**: uses `category=keypair`
  with a single combined cert+key PEM file.
- **Redeploy without re-issuing**: the Domains page has a "Redeploy to
  firewall" button.
- **Deploy failure reporting**: deploy_to_panos.py correctly exits
  non-zero if ANY firewall target fails.
- **Panorama-managed firewalls**: SSL/TLS profile / GP portal updates
  use a full-object type=edit (like the GUI does), not a partial
  type=set, so no manual CLI override is needed.
- **Cross-zone SAN certificates**: a domain's `additional_names` can
  each specify their own `dns_provider`, so a single certificate can
  cover names spread across multiple DNS zones/accounts. See the main
  README and `config/appliance.yaml.example` for details.
