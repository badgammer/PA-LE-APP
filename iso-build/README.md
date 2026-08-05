# Building a prebuilt appliance image

Three options here, in order of least to most "hands-off." All three end
up running the exact same `bootstrap-appliance.sh`.

## Option 1: Bootstrap script only

```bash
sudo dnf update -y
ip a #Write this down to get into the webui
sudo dnf install git -y
git clone <your-repo-url> /tmp/acme-appliance-src
cd /tmp/acme-appliance-src
sudo ./iso-build/bootstrap-appliance.sh
```

## Option 2: Unattended install ISO

```bash
sudo dnf install -y xorriso syslinux genisoimage
sudo ./iso-build/build-iso.sh /path/to/Rocky-9-x86_64-minimal.iso acme-appliance.iso
```

## Option 3: Packer image build

```bash
cd iso-build
packer init appliance.pkr.hcl
packer build -var 'iso_url=/path/to/Rocky-9-x86_64-minimal.iso' -var 'iso_checksum=sha256:<checksum>' appliance.pkr.hcl
```

## After first boot

1. Browse to `https://<appliance-ip>:8443/` and create the admin account.
2. Visit **Settings** and set `acme.email` to a REAL, monitored email
   address.
3. Add DNS provider(s), firewall(s), and domain(s).
4. Visit **System** to check for updates.

## Known gotchas already fixed in this codebase

- **python3-venv**: not a separate package on Rocky/RHEL.
- **certbot version**: auto-detected at renewal time.
- **DNS zone case-sensitivity**: Azure/Route53 providers compare zone
  names case-insensitively.
- **PAN-OS certificate + private key import**: uses `category=keypair`
  with a single combined cert+key PEM file.
- **Redeploy without re-issuing**: the Domains page has a "Redeploy to
  firewall" button that re-runs just the PAN-OS import/attach/commit
  steps for an already-issued certificate -- no new ACME issuance, no
  Let's Encrypt rate-limit usage. Useful while iterating on firewall-side
  configuration (SSL/TLS profile names, targets) without burning through
  Let's Encrypt's weekly duplicate-certificate limit.
- **Redeploy without re-issuing**: the Domains page has a "Redeploy to
  firewall" button that re-runs just the PAN-OS import/attach/commit
  steps for an already-issued certificate -- no new ACME issuance, no
  Let's Encrypt rate-limit usage. Useful while iterating on firewall-side
  configuration (SSL/TLS profile names, targets) without burning through
  Let's Encrypt's weekly duplicate-certificate limit.
