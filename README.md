# ACME / Let's Encrypt Certificate Appliance -- Multi-Target Edition

A lightweight Linux appliance that issues and renews Let's Encrypt (ACME)
certificates via DNS-01 validation, using a pluggable DNS provider
architecture, and automatically deploys the renewed certificate to
**any number and mix of deploy targets** -- Palo Alto firewalls'
GlobalProtect portal (or gateway) SSL/TLS Service Profile, Windows/IIS
servers over WinRM, and potentially more target types in future. This is
the "single pane of glass" release: one certificate can be deployed to a
firewall AND an IIS server (AND anything else that gets a plugin) in the
same run.

This one codebase also supports **two install profiles** chosen at
install time (see [Install profiles](#install-profiles) below) -- a
single-tenant profile with every feature enabled, and a multi-tenant
"MSP" profile (one systemd instance per customer, PAN-OS deploy targets
only) for running this as a shared fleet host across multiple customers.

## Install profiles

Rather than maintaining this as two forked codebases, `install.sh` at
the repo root asks (or is told via `--profile=`) which of two profiles
to configure -- the application code, plugin registries, and templates
are 100% identical either way; only what gets *enabled* differs:

| | `single-instance` | `msp-panos` |
|---|---|---|
| Tenancy | one tenant (your own org) | N customers, one systemd instance each, sharing one host and one code install |
| Deploy target types available | PAN-OS + Windows/IIS (WinRM) | PAN-OS only -- `pywinrm` is never even installed, and `deploy_providers/__init__.py` never imports the `iis` type at all |
| System Updates feature (OS package checks/updates, host reboot) | Enabled | **Disabled entirely** -- it operates on the shared host via a sudoers rule tied to one fixed service account, which has no safe per-tenant equivalent |
| Identity model | one shared `acme-appliance` service account | one dedicated system account per customer (`acmecust-<slug>`) |
| systemd units | static (`acme-webui.service`, `acme-renew.timer`) | templated (`acme-webui@.service`, `acme-renew@.service`, `acme-renew@.timer`) |
| Web UI reachability | gunicorn binds `0.0.0.0:8443` directly, self-signed TLS | gunicorn binds a per-customer unix socket; nginx (not included, see `deploy/nginx/`) terminates TLS and routes per customer |
| Config path | fixed `/etc/acme-appliance/appliance.yaml` | one per customer, under `/etc/acme-appliance/customers/<slug>/` |

```bash
sudo ./install.sh                          # interactive prompt
sudo ./install.sh --profile=single-instance
sudo ./install.sh --profile=msp-panos
```

`install.sh` refuses to switch an already-provisioned host from one
profile to the other -- the identity and directory models differ enough
that a clean re-provision (fresh OS install) is the only supported path
between them. See [Getting a running appliance](#getting-a-running-appliance)
for how `iso-build/bootstrap-appliance.sh` and `install.sh` fit together,
and [Running the msp-panos profile](#running-the-msp-panos-profile) for
onboarding/offboarding customers on that profile.

## Architecture: two symmetric plugin systems

This appliance is built around two parallel, independently-pluggable
systems that mirror each other:

| | Answers | Package | Registry |
|---|---|---|---|
| **DNS providers** | "How do we prove we own this name to Let's Encrypt?" | `dns_providers/` | `dns_providers[]` in appliance.yaml |
| **Deploy providers** | "Where does the issued certificate actually go?" | `deploy_providers/` | `deploy_providers[]` in appliance.yaml |

Both follow the same **type vs. instance** split:
- A **type** (e.g. `azure`, `cloudflare` for DNS; `panos`, `iis` for
  deploy) is a Python class implementing a small interface.
- An **instance** is a named, configured occurrence of a type (e.g.
  `azure-contoso`, `hq-firewall`, `iis01-prod`) that lives under the
  matching top-level map in `appliance.yaml`.
- A domain's `dns_provider` / `deploy_targets[].target` fields reference
  instances by name -- never types directly.

This means adding support for a brand-new kind of firewall, load
balancer, or web server is a self-contained plugin addition (see
[Adding a new deploy target type](#adding-a-new-deploy-target-type)
below) that never touches certbot, DNS validation, or any *other*
deploy target's code.

## Cross-zone SAN certificates (per-name DNS provider override)

Normally, every name on a `domains[]` entry -- the primary name and every
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
  - name: vpn.fabrikam.com
    dns_provider: azure-fabrikam          # default for this entry
    additional_names:
      - apex.fabrikam.com                  # plain string -> uses azure-fabrikam (default)
      - name: portal.otherdomain.com          # override -> uses a different provider
        dns_provider: azure-otherdomain
    cert_name_prefix: gp-portal-cert
    deploy_targets: [...]
```

Both `azure-fabrikam` and `azure-otherdomain` must exist under
`dns_providers[]` (each configured for its own zone/account) -- the
appliance resolves the correct provider independently for each name
during the DNS-01 challenge. This is fully backward-compatible: existing
configs with plain-string `additional_names` lists are unaffected.

In the web UI, each "Additional name" row on the Domain form has its own
DNS provider dropdown (defaulting to "(same as primary)"); attempting to
delete a DNS provider that's still referenced -- either as an entry's
primary provider OR as a per-name override -- is blocked with a clear
error listing which domain(s) depend on it.

## Multi-target certificate deployment

Each `domains[]` entry's `deploy_targets[]` list can reference **any
mix** of deploy target instances, regardless of type:

```yaml
deploy_providers:
  hq-firewall:
    type: panos
    settings: { hostname: 10.10.10.1, api_key: "...", cleanup_old_certs: true }
  iis01-prod:
    type: iis
    settings: { hostname: iis01.corp.example.com, username: "CORP\\svc-acme", password: "...", cleanup_old_certs: true }

domains:
  - name: portal.example.com
    dns_provider: azure-contoso
    cert_name_prefix: portal-cert
    deploy_targets:
      - target: hq-firewall
        cert_field_type: ssl_tls_profile
        cert_field_value: SSL-Portal-Profile
      - target: iis01-prod
        site_name: "Default Web Site"
        binding_port: 443
        hostname: portal.example.com   # SNI, optional
```

Each deploy target TYPE defines its own per-domain fields (see
`deploy_providers.TARGET_FIELDS`) -- PAN-OS wants an SSL/TLS profile (or
GlobalProtect portal) name and optional vsys; IIS wants a site name and
binding IP/port/hostname. The Domain form in the web UI renders the
right fields automatically once you pick a target instance in a row, and
offers a "Fetch options" button wherever the target type supports
listing existing values (SSL/TLS profiles from PAN-OS, site names from
IIS) instead of typing them by hand.

### Setting up the `iis` deploy target type (WinRM)

1. `pywinrm` is installed automatically under the `single-instance`
   install profile (see `requirements-iis.txt` and `install.sh`) -- no
   manual step needed there. It is deliberately never installed under
   the `msp-panos` profile, where the `iis` deploy target type is
   unavailable entirely (see the "Install profiles" section above). If
   you need it outside of `install.sh` for some reason, it's just
   `pip install pywinrm` inside the venv.
2. On the target Windows/IIS host, enable WinRM over HTTPS and open the
   firewall:
   ```powershell
   winrm quickconfig -transport:https
   winrm set winrm/config/service/auth '@{Basic="false";Kerberos="true"}'
   New-NetFirewallRule -DisplayName "WinRM HTTPS" -Direction Inbound -LocalPort 5986 -Protocol TCP -Action Allow
   ```
3. Create a **Deploy Target** in the web UI of type "Windows / IIS
   Server (WinRM)", pointing at that host with a domain account that can
   manage the local certificate store and IIS bindings (local
   Administrators, or a delegated group with the equivalent rights).
4. Leave "Verify WinRM TLS certificate" unchecked if the host's WinRM
   listener uses a self-signed certificate (the default) -- this mirrors
   how PAN-OS instances handle self-signed management certificates.

### What the `iis` provider actually does on each deploy

See the detailed docstring in `deploy_providers/iis.py`, in short: it
bundles certbot's `fullchain.pem` + `privkey.pem` into a one-time `.pfx`
via local `openssl`, ships it to the Windows host over WinRM,
`Import-PfxCertificate`s it into `Cert:\LocalMachine\My`, tags it with
this appliance's own cert name as its `FriendlyName` (so cleanup can
find it later), and points the target IIS site's HTTPS binding at the
new certificate's thumbprint via `netsh http` -- the same mechanism IIS
Manager itself uses.

## Known gotchas already fixed here

### 1. ACME account email must be real
Set via the web UI's **Settings** page.

### 2. certbot version compatibility
`bin/acme-renew.sh` auto-detects certbot's major version.

### 3. DNS zone name case-sensitivity
`dns_providers/azure.py` / `route53.py` compare zone names case-insensitively.

### 4. PAN-OS certificate + private key import
`deploy_providers/panos.py` (via `panos/client.py`) uses `category=keypair`
with a single combined PEM file.

### 5. Silent deploy failures
`deploy_certificate.py` correctly exits non-zero if any deploy target,
of any type, fails.

### 6. Panorama-managed firewalls
`panos/client.py` uses a full-object `type=edit` (matching the GUI)
instead of a partial `type=set`, so no manual CLI override is needed.

### 7. Critical vs optional OS packages during setup
`iso-build/bootstrap-appliance.sh` installs only genuinely-required
packages in one dnf transaction; convenience packages are installed
separately so one bad dependency can't block the whole setup.

### 8. IIS certificate FriendlyName tagging
`deploy_providers/iis.py` sets each imported certificate's `FriendlyName`
to this appliance's own cert name (not the Windows-default subject-based
name), so `cleanup_old_certificates` can reliably find/skip the right
one later -- exactly like PAN-OS certificate objects are matched by name.

### 9. System Updates feature is single-host, so it's fully disabled under msp-panos
The System Updates page triggers `dnf update -y` / a host reboot via a
sudoers rule tied to the single fixed `acme-appliance` service account
and a relaxed `NoNewPrivileges` setting needed for PAM step-up auth --
neither has a safe per-customer equivalent on a shared MSP fleet host
(a customer's tenant admin cannot be allowed to reboot the whole host
out from under every other customer's instance). `webui/app.py` gates
every `/system*` route behind `SYSTEM_UPDATES_ENABLED` (false whenever
`ACME_APPLIANCE_PROFILE=msp-panos`), and `lib/profile-msp-panos.sh`
never installs the sudoers rule in the first place -- so the feature is
unavailable at both the application and OS-privilege layers, not just
hidden from the nav bar.

## Migrating an existing (pre-multi-target) appliance.yaml

If you're upgrading from a version of this appliance that only supported
PAN-OS (top-level `panos_firewalls[]` and per-domain `panos_targets[]`),
nothing manual is required: `webui/config_store.py` automatically
migrates the old shape into `deploy_providers[]` (tagged `type: panos`)
and `deploy_targets[]` the first time the config is loaded, and persists
the migrated file back to disk. Existing PAN-OS deployments keep working
unchanged -- the migration only renames/reshapes keys, it never changes
firewall connection settings or SSL/TLS profile assignments.

## Redeploying an already-issued certificate

Every domain on the Domains page has a **Redeploy** button once a
certificate has been issued for it -- re-runs only the
import/attach/commit (or WinRM import/bind) steps for **every**
configured deploy target, regardless of type, with no new ACME issuance
and no Let's Encrypt rate-limit usage.

## Getting a running appliance

`iso-build/bootstrap-appliance.sh` handles OS-level prep only (packages,
copying this repo's code to `/opt/acme-appliance`, creating the Python
venv) and then hands off to `install.sh` for the profile-specific setup
covered above -- by default it will prompt you interactively for a
profile, or you can pass one straight through:

```bash
sudo dnf update -y
ip a #Write this down to get into the webui
sudo dnf install git -y
git clone https://github.com/badgammer/PA-LE-APP /tmp/acme-appliance-src
cd /tmp/acme-appliance-src

# Interactive profile prompt:
sudo bash ./iso-build/bootstrap-appliance.sh

# Or non-interactive, e.g. for automation:
sudo bash ./iso-build/bootstrap-appliance.sh . -- --profile=single-instance --non-interactive
sudo bash ./iso-build/bootstrap-appliance.sh . -- --profile=msp-panos --non-interactive
```

If the appliance code is already deployed to `/opt/acme-appliance` (e.g.
after a `git pull` to update an existing install), you can run
`/opt/acme-appliance/install.sh` directly without re-running bootstrap.

See `iso-build/README.md` for manual setup, unattended ISO, and Packer
image build options.

## Running the msp-panos profile

Once a host is installed with `--profile=msp-panos`, onboard and manage
customer instances with the helper scripts staged into `bin/`:

```bash
# Onboard a new customer -- creates its dedicated system account,
# directory tree, and starter config; enables + starts its systemd
# instances; prints the nginx server block to add.
sudo /opt/acme-appliance/bin/msp-provision-customer.sh customer-a

# Fleet-wide health at a glance (cert expiry, web UI/renewal-timer
# state) -- reads everything directly off disk/systemd, independent of
# whether any customer's web UI process happens to be up.
sudo /opt/acme-appliance/bin/msp-fleet-status.sh
sudo /opt/acme-appliance/bin/msp-fleet-status.sh --errors-only

# Offboard a customer -- stops/disables its units, archives (never
# deletes outright) its config/certs to a dated tarball, removes its
# system account.
sudo /opt/acme-appliance/bin/msp-deprovision-customer.sh customer-a
```

Each customer instance binds a **unix socket only**
(`/run/acme-appliance/<slug>/webui.sock`) -- there is no directly
reachable TCP listener per customer. Route to it with nginx (or Caddy)
using `deploy/nginx/acme-appliance-msp.conf.template` as a starting
point; `msp-provision-customer.sh` prints a filled-in copy of this
template for each new customer.

Updating the shared code across the whole fleet is a single `git pull` +
restart, since every customer instance shares one code install:

```bash
cd /opt/acme-appliance && git pull
sudo systemctl restart 'acme-webui@*.service'
```

## Adding a new DNS provider

1. Create `dns_providers/<name>.py` implementing `add_txt_record` / `remove_txt_record` from `base.py`.
2. Register it in `dns_providers/__init__.py`'s `PROVIDER_TYPES` and `PROVIDER_FIELDS`.
3. Restart the web UI: `systemctl restart acme-webui.service` (single-instance)
   or `systemctl restart 'acme-webui@*.service'` (msp-panos, restarts every
   customer instance at once since they all share this one code install).

## Adding a new deploy target type

1. Create `deploy_providers/<name>.py` with a class implementing
   `deploy_certificate()` (required) and optionally `cleanup_old_certificates()`,
   `test_connection()`, `list_options()` from `deploy_providers/base.py`'s
   `BaseDeployProvider`.
2. Register the class in `deploy_providers/__init__.py`'s `PROVIDER_TYPES`,
   its connection-level settings in `INSTANCE_FIELDS` (shown on the "Add
   Deploy Target" form), and its per-domain fields in `TARGET_FIELDS`
   (shown on the Domain form once an instance of that type is selected).
3. Restart the web UI (see the DNS provider section above for the exact
   command per profile). No changes are needed anywhere else --
   `deploy_certificate.py`, the Domain form's dynamic field rendering,
   and the "Fetch options" wiring are all fully generic against whatever
   is registered in `deploy_providers/`. If your new type should be
   available under the msp-panos profile too, no extra step is needed --
   profile filtering in `deploy_providers/__init__.py` is driven purely
   by `ACME_APPLIANCE_PROFILE`, not a separate registration list; if it
   should be single-instance-only (e.g. it has no safe multi-tenant
   story, the way System Updates doesn't), add it to that module's
   `_enabled_type_names()` exclusion for `msp-panos` explicitly.
