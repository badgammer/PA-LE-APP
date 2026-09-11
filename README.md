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
**MSP Console** profile for running this as a shared fleet host managed
by your own staff on behalf of many customers (customers themselves
never log in -- see [Running the msp-panos profile](#running-the-msp-panos-profile)).

## Install profiles

Rather than maintaining this as two forked codebases, `install.sh` at
the repo root asks (or is told via `--profile=`) which of two profiles
to configure -- the application code, plugin registries, and templates
are 100% identical either way; only what gets *enabled* differs:

| | `single-instance` | `msp-panos` |
|---|---|---|
| Tenancy | one tenant (your own org), one web UI | N customer *namespaces*, managed by MSP staff from ONE shared MSP Console process |
| Who logs in | your own admin account(s) | your MSP staff only, via the MSP Console's own owner/staff accounts -- customers never log in themselves |
| Deploy target types available | PAN-OS + Windows/IIS (WinRM) | PAN-OS only -- `pywinrm` is never even installed, and `deploy_providers/__init__.py` never imports the `iis` type at all |
| System Updates feature (OS package checks/updates, host reboot) | Enabled | **Disabled entirely** -- it operates on the shared host and has no safe per-tenant equivalent |
| Identity model | one `acme-appliance` service account | one `acme-msp-console` service account -- the SAME account owns every customer's data; there is no separate account or process per customer |
| Process model | one `acme-webui.service` | one `acme-msp-console.service` (the web UI) + one `acme-msp-renewal.timer` (daily renewal loop across every customer) |
| Config path | fixed `/etc/acme-appliance/appliance.yaml` | one per customer namespace, under `/etc/acme-appliance/customers/<slug>/appliance.yaml` |
| Let's Encrypt rate-limit isolation | one certbot `--config-dir` for the whole appliance | each customer namespace still gets its OWN certbot `--config-dir` -- rate-limit accounting is isolated per customer even though there's only one process |

```bash
sudo ./install.sh                          # interactive prompt
sudo ./install.sh --profile=single-instance
sudo ./install.sh --profile=msp-panos
```

`install.sh` refuses to switch an already-provisioned host from one
profile to the other -- the directory/config models differ enough that
a clean re-provision (fresh OS install) is the only supported path
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

### 10. git is not included in a Rocky Linux 9 minimal install
`iso-build/bootstrap-appliance.sh` now installs `git` alongside the
other critical packages (epel-release, python3, certbot, openssl) in the
same dnf transaction, since a fresh Rocky Linux 9 "minimal" ISO install
does not ship it and this repo's own quick-start instructs cloning with
`git` before that script is even reachable.

### 11. MSP Console has its own account store, completely separate from any customer's data
The MSP Console (`msp_console/`, msp-panos profile only) runs as a
dedicated `acme-msp-console` system account. Its admin database
(`/etc/acme-appliance/msp-console/admins.yaml`) is completely separate
from any customer's own domain/provider configuration -- an MSP Console
login grants access only to whichever customers that specific admin has
been explicitly granted, never anything else by default. Per-customer
access for non-owner (staff) accounts is enforced as a 404, not a 403,
on every route for a customer outside that admin's grant table -- so a
staff account cannot even confirm a customer they lack access to
exists.

### 12. No `sudo`, no per-customer Linux accounts, no `ProtectSystem=strict`/`ReadWritePaths=` complexity
An earlier iteration of the msp-panos profile ran one dedicated Linux
account and one systemd instance PER CUSTOMER, which required the MSP
Console to escalate via `sudo` into a narrow sudoers rule for every
customer lifecycle action (`useradd`, `userdel`, `systemctl enable`,
etc.). That design hit a real, reproducible class of bugs: systemd's
`ProtectSystem=strict` bind-mounts the filesystem read-only at the
KERNEL mount layer, a restriction `sudo` cannot bypass (it only changes
UID/capabilities, not the mount namespace) -- so every provisioning
attempt failed identically with `useradd: cannot lock /etc/passwd; try
again later.` on every host, VM, and CPU architecture tested, since the
cause was the unit file's `ReadWritePaths=`, not any particular disk or
hardware.

**This was fixed by removing the need for it entirely, not by widening
`ReadWritePaths=`.** Under the current design, a "customer" is purely a
DATA namespace served by the ONE MSP Console process -- there is no
per-customer Linux account or systemd unit to create at runtime, so
there is no `useradd`/`userdel` call anywhere in this profile anymore,
and therefore no `sudo` call in `msp_console/actions.py` either. The
`acme-msp-console.service` unit can therefore use BOTH
`ProtectSystem=strict` AND `NoNewPrivileges=true` (the earlier design
could not use the latter, since `sudo` is a setuid-root binary that
`NoNewPrivileges=true` would have silently blocked), and
`ReadWritePaths=` only ever needs to list the exact directories this
one account's own data lives under -- never `/etc` as a whole. This is
a genuine hardening improvement, not just a simplification.

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

`git` is required to clone this repo but is **not included in a stock
Rocky Linux 9 "minimal" install** -- install it first, before cloning:

```bash
sudo dnf update -y
ip a #Write this down to get into the webui
sudo dnf install -y git
git clone https://github.com/badgammer/PA-LE-APP /tmp/acme-appliance-src
cd /tmp/acme-appliance-src
```

`iso-build/bootstrap-appliance.sh` handles the rest of OS-level prep
(packages, copying this repo's code to `/opt/acme-appliance`, creating
the Python venv) and then hands off to `install.sh` for the
profile-specific setup covered above -- by default it will prompt you
interactively for a profile, or you can pass one straight through:

```bash
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

Once a host is installed with `--profile=msp-panos`, **only your MSP
staff log in** -- there is one shared web UI, the **MSP Console**, at
`https://<host>:9443/`. Customers never get their own login or their
own URL; every customer is managed as a data namespace from within this
one console by whichever staff have been granted access to them. First
visit prompts you to create the initial owner account.

From the console, an owner (or a staff member with write access to a
given customer) can:

- Add and remove customers (equivalent to
  `bin/msp-provision-customer.sh` / `bin/msp-deprovision-customer.sh`,
  without needing shell access)
- Manage that customer's own DNS providers, deploy targets, and domains
  -- the same forms and workflow the single-instance profile's web UI
  has always had, just reached via `/customers/<slug>/...` instead of
  the top level
- View fleet-wide health (domain counts, soonest cert expiry) and each
  customer's recent log activity
- Trigger a renewal check for an entire customer, or for just one of
  their domains, from that domain's own Renew/Redeploy buttons
- Add other MSP staff accounts, each with their own per-customer
  read/write grants -- a staff account only ever sees the customers
  explicitly granted to it; anything else is completely invisible to
  them (a 404, not a hidden button)

Owners have full read/write access to every customer and can manage
other admin accounts; staff accounts are scoped per-customer via an
explicit grant table (read, read+write, or no access at all). The MSP
Console runs as its own unprivileged system account
(`acme-msp-console`), with its own separate admin database -- logging
into it grants no MORE access to any customer's data than that admin's
own grants specify.

**There is exactly one running process for the entire fleet** -- unlike
an earlier design, a "customer" here is purely a data namespace (its
own `appliance.yaml`, its own certbot `--config-dir` for isolated Let's
Encrypt rate-limit accounting), not a separate Linux account or systemd
unit. This means there is no `useradd`/`userdel`/per-customer
`systemctl` call anywhere in this profile, and therefore no `sudo` call
in `msp_console/actions.py` either -- every action the console performs
runs directly as its own account, which already owns every customer's
files.

The `bin/msp-*.sh` CLI scripts remain fully functional and are what the
console itself calls under the hood for customer lifecycle
(provision/deprovision) and renewal -- use them directly for
scripting/automation/cron use where a web session isn't appropriate:

```bash
# Onboard a new customer -- creates its directory tree and starter
# config. No Linux account, systemd unit, or nginx routing involved.
sudo /opt/acme-appliance/bin/msp-provision-customer.sh customer-a

# Renew every domain for every customer (what acme-msp-renewal.timer
# runs daily) -- or just one customer:
sudo /opt/acme-appliance/bin/msp-renew-all-customers.sh
sudo /opt/acme-appliance/bin/msp-renew-all-customers.sh customer-a

# Offboard a customer -- archives (never deletes outright) its
# config/certs to a dated tarball, removes its directory tree.
sudo /opt/acme-appliance/bin/msp-deprovision-customer.sh customer-a
```

Updating the shared code is a single `git pull` + restart, since every
customer's data is served by this one process:

```bash
cd /opt/acme-appliance && git pull
sudo systemctl restart acme-msp-console.service
```

## Adding a new DNS provider

1. Create `dns_providers/<name>.py` implementing `add_txt_record` / `remove_txt_record` from `base.py`.
2. Register it in `dns_providers/__init__.py`'s `PROVIDER_TYPES` and `PROVIDER_FIELDS`.
3. Restart the web UI: `systemctl restart acme-webui.service` (single-instance)
   or `systemctl restart acme-msp-console.service` (msp-panos -- restarts
   the one shared process, picking up the change for every customer at once).

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
