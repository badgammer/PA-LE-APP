# ACME / Let's Encrypt Appliance for Palo Alto GlobalProtect

A lightweight Linux appliance that issues and renews Let's Encrypt (ACME)
certificates via DNS-01 validation, using a **pluggable DNS provider
architecture**, and automatically deploys the renewed certificate to one
or more Palo Alto firewalls' GlobalProtect portal (or gateway) SSL/TLS
Service Profile. Includes a **web UI** for configuring everything, a
**live SSL/TLS profile picker**, **certificate export**, and
**kickstart/Packer/ISO tooling** to produce a prebuilt appliance image.

## Why this design

- **Lightweight**: no database, no heavy JS framework -- certbot handles
  the ACME protocol, and the web UI is server-rendered Flask + plain
  HTML/CSS.
- **DNS-provider agnostic**: DNS-01 record creation is abstracted behind
  `dns_providers/base.py`. Cloudflare, Azure DNS, and Route53 are included
  out of the box; a `generic_webhook` provider covers anything else
  without writing Python. You can configure multiple *named instances* of
  the same type (e.g. two Cloudflare accounts, two Azure subscriptions).
- **No inbound NAT/firewall changes required**: DNS-01 instead of HTTP-01
  means the appliance never needs an inbound port opened through the
  Palo Alto, and it supports wildcard certificates.
- **Per-domain isolation**: one certbot invocation per certificate, so a
  problem with one DNS zone or one firewall does not block renewal of the
  others. This also enables renewing a single domain on demand.
- **Runs entirely under an unprivileged service account**: certbot is
  pointed at appliance-owned directories instead of the usual root-owned
  `/etc/letsencrypt`, so nothing here needs root after initial setup.

## Architecture

```
        ┌────────────────────────┐        ┌───────────────────────────┐
        │   Web UI (Flask)       │  edits  │   /etc/acme-appliance/    │
        │   webui/app.py         │ ──────► │   appliance.yaml          │
        │   (gunicorn + HTTPS)   │         │   users.yaml (local auth) │
        └────────────────────────┘         └─────────────┬─────────────┘
                                                           │ read by
                                                           ▼
 systemd timer   ┌─────────────────────────┐
  (03:30 UTC)  → │   bin/acme-renew.sh      │  (all domains, or a single
   or Web UI     └───────────┬─────────────┘   domain from the web UI)
     button                  │
                             ▼
                 certbot certonly --manual --preferred-challenges dns
                        │                    │
             auth-hook  │                    │  cleanup-hook
                        ▼                    ▼
              dns_dispatcher.py add   dns_dispatcher.py remove
                        │
                        ▼
          dns_providers/{cloudflare,azure,route53,generic_webhook}.py
                        │
                        ▼
              (creates/removes _acme-challenge TXT record)
                        │
                        ▼
        Let's Encrypt validates → certbot issues fullchain.pem/privkey.pem
                        │
                        ▼  --deploy-hook (only on successful issuance)
              deploy_to_panos.py
                        │
                        ▼
        panos/client.py: import cert → set SSL/TLS profile
                         (or GlobalProtect portal cert) → commit
                         → cleanup old certs with same prefix
```

## Repository layout

```
acme-appliance/
├── bin/
│   ├── acme-renew.sh                # renewal entry point (all domains,
│   │                                # or a single domain via arg + --force)
│   └── generate-selfsigned-cert.sh  # TLS cert for the web UI
├── cert_naming.py                   # shared "safe cert name" helper (wildcard-aware)
├── iso-build/                       # prebuilt-image tooling (see iso-build/README.md)
├── dns_dispatcher.py                # certbot auth-hook / cleanup-hook
├── deploy_to_panos.py                # certbot deploy-hook
├── dns_providers/
│   ├── base.py                      # interface every provider implements
│   ├── cloudflare.py
│   ├── azure.py                     # matches your existing dns_azure_* fields
│   ├── route53.py
│   └── generic_webhook.py           # for any provider without a dedicated plugin
├── panos/
│   └── client.py                    # PAN-OS XML API: import/set-profile/commit/list-profiles
├── webui/                           # web frontend for configuring everything
│   ├── app.py                       # Flask routes
│   ├── auth.py                      # local users, bcrypt + optional TOTP MFA
│   ├── config_store.py              # safe atomic read/write of appliance.yaml
│   ├── test_connections.py          # "Test Connection" + SSL profile fetch logic
│   ├── templates/                   # server-rendered HTML (Jinja2)
│   └── static/style.css             # no CDN dependencies -- works fully offline
├── config/appliance.yaml.example    # copy to /etc/acme-appliance/appliance.yaml
├── systemd/
│   ├── acme-renew.service / .timer  # daily renewal
│   └── acme-webui.service           # the web UI, via gunicorn
└── requirements.txt
```

## Getting a running appliance

```bash
# NOTE: do NOT add "python3-venv" to this command -- Rocky/RHEL doesn't
# ship it as a separate package (the venv module is already inside
# python3), and adding a nonexistent package name here makes the WHOLE
# dnf install fail, silently skipping certbot too.
sudo dnf install -y epel-release python3 python3-pip certbot openssl
sudo useradd --system --home /opt/acme-appliance --shell /sbin/nologin acme-appliance
sudo mkdir -p /opt/acme-appliance /etc/acme-appliance
sudo cp -r acme-appliance/* /opt/acme-appliance/
sudo python3 -m venv /opt/acme-appliance/venv
sudo /opt/acme-appliance/venv/bin/pip install -r /opt/acme-appliance/requirements.txt
sudo touch /var/log/acme-appliance.log
sudo chown acme-appliance:acme-appliance /var/log/acme-appliance.log
sudo chmod +x /opt/acme-appliance/bin/*.sh /opt/acme-appliance/dns_dispatcher.py /opt/acme-appliance/deploy_to_panos.py

# certbot's own directories -- appliance-owned, NOT the usual root-owned
# /etc/letsencrypt (see "Certbot's storage directories" below for why).
sudo mkdir -p /etc/acme-appliance/letsencrypt /var/lib/acme-appliance/letsencrypt /var/log/acme-appliance/letsencrypt
sudo chown -R acme-appliance:acme-appliance /etc/acme-appliance/letsencrypt /var/lib/acme-appliance/letsencrypt /var/log/acme-appliance/letsencrypt

sudo /opt/acme-appliance/bin/generate-selfsigned-cert.sh acme-appliance.yourdomain.local
sudo cp /opt/acme-appliance/systemd/acme-webui.service /etc/systemd/system/
sudo cp /opt/acme-appliance/systemd/acme-renew.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now acme-webui.service
sudo systemctl enable --now acme-renew.timer
```

Or, for a one-command bootstrap on a fresh box (does all of the above,
including the certbot directory setup, automatically):
```bash
git clone <your-repo-url> /tmp/acme-appliance-src
cd /tmp/acme-appliance-src
sudo ./iso-build/bootstrap-appliance.sh
```

## Certbot's storage directories (read this if renewals fail silently)

This appliance runs certbot as an **unprivileged service account**
(`acme-appliance`), not root. certbot's normal default directories
(`/etc/letsencrypt`, `/var/lib/letsencrypt`, `/var/log/letsencrypt`) are
root-owned system paths a non-root account can't create or write to --
so `bin/acme-renew.sh` instead points certbot at appliance-owned
directories via `--config-dir`/`--work-dir`/`--logs-dir`:

| Purpose             | Default path                              | Override env var                  |
|----------------------|--------------------------------------------|------------------------------------|
| certbot config/live  | `/etc/acme-appliance/letsencrypt`           | `ACME_APPLIANCE_LE_CONFIG_DIR`     |
| certbot working dir  | `/var/lib/acme-appliance/letsencrypt`       | `ACME_APPLIANCE_LE_WORK_DIR`       |
| certbot's own log     | `/var/log/acme-appliance/letsencrypt`       | `ACME_APPLIANCE_LE_LOGS_DIR`       |

So issued certificates live at
`/etc/acme-appliance/letsencrypt/live/<cert-name>/`, **not** the usual
`/etc/letsencrypt/live/...`, and certbot's own log is at
`/var/log/acme-appliance/letsencrypt/letsencrypt.log`, not
`/var/log/letsencrypt/letsencrypt.log`. `bootstrap-appliance.sh` creates
and `chown`s all three directories for you; if you set things up
manually, create them yourself (see the manual steps above) -- if they
don't exist or aren't owned by the service account, certbot fails before
it can even write its own log file, which looks exactly like "renewal
failed and there's no log anywhere," a genuinely confusing symptom.

**If you're triggering renewals from the web UI**, both
`systemd/acme-renew.service` (the daily timer) *and*
`systemd/acme-webui.service` (since the "Renew now" buttons spawn
`acme-renew.sh` as a child of the running web UI process, which inherits
that unit's sandboxing) need `ReadWritePaths` covering
`/var/lib/acme-appliance` -- both unit files already do this; if you
ever hand-edit them, keep that in sync or web-UI-triggered renewals will
fail while the daily-timer path keeps working, which is a very confusing
thing to debug.

Also: `bin/acme-renew.sh` now checks that `certbot` is actually on `PATH`
before doing anything else, and pipes certbot's own console output
directly into `/var/log/acme-appliance.log` (in addition to certbot's own
log file), so a failure is visible from the web UI's Logs page
immediately rather than requiring you to go find a separate file.

## Using the web UI

Browse to `https://<appliance-ip>:8443/` -- the first visit prompts you to
create an admin account (with optional TOTP MFA). From there:

1. **DNS Providers** -- one entry per DNS account/zone. Each has a "Test
   connection" button.
2. **Firewalls** -- one entry per Palo Alto firewall, with API key (or
   username/password) and a "Test connection" button. **Required PAN-OS
   admin role permissions** (Device > Admin Roles > &lt;role&gt; > XML
   API tab): `Configuration`, `Import`, and `Commit` are needed to deploy
   certificates; `Operational Requests` is *additionally* needed for
   "Test connection" specifically (it calls a `show system info`
   operational command) -- a role missing only `Operational Requests`
   can still deploy certificates successfully even though "Test
   connection" fails, which is expected, not a sign of a deeper problem.
3. **Domains** -- pick the DNS provider and one or more firewall targets
   per certificate.

### Assigning SSL/TLS profiles per domain

Each domain entry's firewall target row has its own **SSL/TLS Service
Profile** (or GlobalProtect portal cert field), so different domains --
or even different targets on the *same* domain across multiple firewalls
-- can each point at a different, specific profile. Click **Fetch
profiles** next to the profile field to pull the actual list of SSL/TLS
Service Profiles already configured on the selected firewall (via a live
PAN-OS API call) and pick from real names via autocomplete.

### Wildcard certificates and combining names on one certificate

Enter `*.example.com` as the domain name for a wildcard cert. To also
cover the bare apex domain (`example.com`) on the *same* certificate,
add `example.com` under **Additional names (SANs)** on the domain form.

### Exporting/downloading a certificate

Once a certificate has been issued, a **Download cert** button appears on
its row in the Domains page, bundling `fullchain.pem`, `cert.pem`,
`chain.pem`, and `privkey.pem` into a zip. Every download is recorded in
`/var/log/acme-appliance.log` (who, when, from which IP) since it
includes the private key.

The dashboard has a **Run renewal now** button (all domains) and a live
log viewer. Each row on the **Domains** page also has its own **Renew
now** button; the domain's edit page additionally offers **Force renew
now**. Single-domain runs for *different* domains can happen
concurrently; re-running the same domain, or running "all domains" while
any single-domain renewal is in flight, is blocked until the in-progress
run finishes.

Secret fields are always masked in the UI and are only overwritten if you
type a new value.

### Getting a Palo Alto API key

```bash
curl -sk "https://<firewall-mgmt-ip>/api/?type=keygen&user=<admin-user>&password=<password>"
```
Prefer a dedicated API-only admin account. See the permissions note above
-- Configuration + Import + Commit + Operational Requests, all four.

### First test run (staging environment, no rate limits)

Set `acme.server` to the Let's Encrypt **staging** endpoint first (edit
directly in `appliance.yaml`):
```yaml
acme:
  server: "https://acme-staging-v02.api.letsencrypt.org/directory"
```
Then click **Run renewal now** in the web UI, or run manually:
```bash
sudo -u acme-appliance ACME_APPLIANCE_CONFIG=/etc/acme-appliance/appliance.yaml \
    /opt/acme-appliance/bin/acme-renew.sh vpn.example.com --force
tail -f /var/log/acme-appliance.log
```

## Wildcard / SAN certificate details

- A domain entry's `name` can be `*.example.com`. `additional_names: []`
  lists any extra names (typically the bare apex) issued as SANs on the
  same certificate.
- Both a wildcard and its apex resolve to the **same**
  `_acme-challenge.example.com` DNS record. The Azure and Route53
  providers correctly **merge** multiple simultaneous TXT challenge
  values at that name rather than overwriting one with the other.
- On disk, `acme-renew.sh` passes an explicit `--cert-name` to certbot so
  the lineage directory never contains a literal `*` character --
  `*.example.com` becomes `wildcard.example.com` (see `cert_naming.py`).

## Adding a new DNS provider

1. Create `dns_providers/<name>.py` implementing `add_txt_record` /
   `remove_txt_record` (and optionally `test_connection`) from `base.py`.
2. Register it in `dns_providers/__init__.py`'s `PROVIDER_TYPES` and add a
   `PROVIDER_FIELDS` entry for the web UI's form.
3. Restart `acme-webui.service`.

## Web UI security notes

- Local accounts only (bcrypt-hashed passwords), with optional TOTP MFA.
- CSRF tokens required on all state-changing requests.
- Failed logins rate-limited (5 attempts, 5-minute lockout); the service
  runs a single gunicorn worker so this stays consistent.
- HTTPS via a self-signed cert by default; swap in your internal CA's
  cert/key in `systemd/acme-webui.service`.
- Certificate/private-key downloads are logged with user, timestamp, and
  source IP.

## Operational notes

- **Logs**: `/var/log/acme-appliance.log` (includes certbot's own console
  output now, see above), viewable from the web UI's Logs page.
  certbot's dedicated log: `/var/log/acme-appliance/letsencrypt/letsencrypt.log`.
- **Certificate naming**: each renewal creates a new certificate object
  named `<cert_name_prefix>-<UTC timestamp>` on the firewall. "Cleanup
  old certs" removes older certs with the same prefix after a successful
  commit.
- **Renewal locking**: lock files live under `/var/run/acme-appliance/`.
- **Panorama-managed firewalls**: `panos/client.py` targets a
  standalone-managed firewall's local config; a Panorama variant would
  need a different xpath base.
- **Config backups**: every web UI save keeps a timestamped backup under
  `/etc/acme-appliance/backups/` (last 30 kept).

## Known follow-ups / roadmap

- Add a Panorama (template/device-group) variant of `panos/client.py`.
- Add Slack/Teams/email notification on renewal failure.
- Optional "certificate health" widget querying each firewall's actual
  active cert expiry directly.
- A proper image-update mechanism for the prebuilt-image options.
