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
  `azure-hoffman-net`, `hq-firewall`, `iis01-prod`) that lives under the
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
  - name: vpn.howardscams.com
    dns_provider: azure-howardscams          # default for this entry
    additional_names:
      - apex.howardscams.com                  # plain string -> uses azure-howardscams (default)
      - name: portal.otherdomain.com          # override -> uses a different provider
        dns_provider: azure-otherdomain
    cert_name_prefix: gp-portal-cert
    deploy_targets: [...]
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
    dns_provider: azure-hoffman-net
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

1. On the appliance: `pip install pywinrm` inside the venv (or add it to
   `requirements.txt` before running `bootstrap-appliance.sh`, which is
   already done in this release).
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

```bash
sudo dnf update -y
ip a #Write this down to get into the webui
sudo dnf install git -y
git clone https://github.com/badgammer/PA-LE-APP/tree/Extended-Deployment-Targets /tmp/acme-appliance-src
cd /tmp/acme-appliance-src
sudo bash ./iso-build/bootstrap-appliance.sh
```

See `iso-build/README.md` for manual setup, unattended ISO, and Packer
image build options.

## Adding a new DNS provider

1. Create `dns_providers/<name>.py` implementing `add_txt_record` / `remove_txt_record` from `base.py`.
2. Register it in `dns_providers/__init__.py`'s `PROVIDER_TYPES` and `PROVIDER_FIELDS`.
3. Restart `acme-webui.service`.

## Adding a new deploy target type

1. Create `deploy_providers/<name>.py` with a class implementing
   `deploy_certificate()` (required) and optionally `cleanup_old_certificates()`,
   `test_connection()`, `list_options()` from `deploy_providers/base.py`'s
   `BaseDeployProvider`.
2. Register the class in `deploy_providers/__init__.py`'s `PROVIDER_TYPES`,
   its connection-level settings in `INSTANCE_FIELDS` (shown on the "Add
   Deploy Target" form), and its per-domain fields in `TARGET_FIELDS`
   (shown on the Domain form once an instance of that type is selected).
3. Restart `acme-webui.service`. No changes are needed anywhere else --
   `deploy_certificate.py`, the Domain form's dynamic field rendering,
   and the "Fetch options" wiring are all fully generic against whatever
   is registered in `deploy_providers/`.
