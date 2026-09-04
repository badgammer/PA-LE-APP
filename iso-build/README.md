# Building a prebuilt appliance image

Three options: bootstrap script, unattended ISO, or Packer image build.
All end up running the exact same `bootstrap-appliance.sh`, which itself
hands off to `install.sh` (repo root) for the install PROFILE decision --
see the main README's "Install profiles" section for the full
single-instance vs. msp-panos comparison. `bootstrap-appliance.sh` only
ever handles OS-level prep (packages, copying code, the Python venv);
everything profile-specific (systemd units, service account(s), config
seeding, TLS cert, sudoers, firewalld) lives in `install.sh` and
`lib/profile-*.sh`.

`git` is required to clone this repo but is **not included in a stock
Rocky Linux 9 "minimal" install** -- install it first, before cloning
(`bootstrap-appliance.sh` also installs it itself as one of its critical
packages, but you still need it up front just to get the repo onto the
box in the first place):

```bash
sudo dnf update -y
ip a #Write this down to get into the webui
sudo dnf install -y git
git clone https://github.com/badgammer/PA-LE-APP /tmp/acme-appliance-src
cd /tmp/acme-appliance-src

# Prompts interactively for a profile:
sudo bash ./iso-build/bootstrap-appliance.sh

# Or specify one up front (useful for kickstart/Packer/unattended builds):
sudo bash ./iso-build/bootstrap-appliance.sh . -- --profile=single-instance --non-interactive
```

The unattended kickstart (`ks.cfg`) and Packer template (`appliance.pkr.hcl`)
in this directory build the **single-instance** profile by default (the
kickstart's `%post` section calls `bootstrap-appliance.sh` with
`--profile=single-instance --non-interactive` explicitly -- %post runs
completely unattended with no TTY, so a profile MUST be passed rather
than relying on the interactive prompt, which would otherwise hang the
build forever). Change that to `--profile=msp-panos` in `ks.cfg` if
you're imaging an MSP fleet host instead. See the main README for what
each profile changes.

## After first boot

**single-instance profile:**
1. Browse to `https://<appliance-ip>:8443/` and create the admin account.
2. Visit **Settings** and set `acme.email` to a REAL, monitored email address.
3. Add DNS provider(s), deploy target(s) (PAN-OS firewall, IIS server, etc.), and domain(s).
4. Visit **System** to check for updates.

**msp-panos profile:**
1. Browse to `https://<appliance-ip>:9443/` -- the **MSP Console**
   dashboard -- and create the initial owner account. This is the
   primary way to onboard/offboard customers and monitor the fleet; see
   the main README's "Running the msp-panos profile" section for the
   full walkthrough, including how to add other MSP staff accounts with
   scoped per-customer read/write access.
2. Install nginx if it isn't already present (each CUSTOMER instance
   binds a unix socket only -- there is no direct TCP listener to browse
   to until nginx is routing to it; the MSP Console itself, unlike
   customer instances, does listen directly on 9443/tcp).
3. Onboard your first customer from the MSP Console, or from the CLI:
   `sudo bin/msp-provision-customer.sh <slug>`.
4. Add the printed nginx server block, reload nginx, then browse to that
   customer's URL and create its admin account.
5. Repeat per customer. Use the MSP Console dashboard or
   `bin/msp-fleet-status.sh` for a cross-customer
   health view at any time. There is no System page on this profile --
   see the main README's gotcha #9 for why.

## Known gotchas already fixed in this codebase

- **git is not included in a Rocky Linux 9 minimal install**: installed
  explicitly, both in the quick-start command above and as one of
  `bootstrap-appliance.sh`'s critical packages.
- **python3-venv / policycoreutils-python-utils**: critical vs optional
  packages installed in separate dnf transactions.
- **certbot version**: auto-detected at renewal time.
- **DNS zone case-sensitivity**: Azure/Route53 providers compare zone
  names case-insensitively.
- **PAN-OS certificate + private key import**: uses `category=keypair`
  with a single combined cert+key PEM file.
- **Redeploy without re-issuing**: the Domains page has a "Redeploy"
  button that re-runs deployment to every configured target, any type.
- **Deploy failure reporting**: deploy_certificate.py correctly exits
  non-zero if ANY deploy target, of any type, fails.
- **Panorama-managed firewalls**: SSL/TLS profile / GP portal updates
  use a full-object type=edit (like the GUI does), not a partial
  type=set, so no manual CLI override is needed.
- **Multi-target deploys**: a single certificate can be deployed to a
  mix of PAN-OS firewalls AND Windows/IIS servers (via WinRM) in the
  same run -- see `deploy_providers/` and the main README.
- **Cross-zone SAN certificates**: a domain's `additional_names` can
  each specify their own `dns_provider`, so a single certificate can
  cover names spread across multiple DNS zones/accounts. See the main
  README and `config/appliance.yaml.example` for details.
- **One codebase, two install profiles**: `install.sh` (not a forked
  repo) chooses between `single-instance` (this page's default flow)
  and `msp-panos` (multi-tenant, one systemd instance per customer,
  PAN-OS only, System Updates disabled) -- see the main README's
  "Install profiles" section.
- **System Updates is single-host by design**: it's fully unavailable
  under the msp-panos profile (both the web routes and the sudoers rule
  that would back them), not just hidden from the nav bar -- see the
  main README's gotcha #9.
- **MSP Console fleet dashboard**: msp-panos hosts get a separate
  fleet-management web UI at `https://<host>:9443/` (own admin
  database, own unprivileged service account, own narrowly-scoped
  sudoers rule) -- see the main README's "Running the msp-panos
  profile" section for the owner/staff permission model.
