# ACME / Let's Encrypt Appliance for Palo Alto GlobalProtect

A lightweight Linux appliance that issues and renews Let's Encrypt (ACME)
certificates via DNS-01 validation, using a **pluggable DNS provider
architecture**, and automatically deploys the renewed certificate to one
or more Palo Alto firewalls' GlobalProtect portal (or gateway) SSL/TLS
Service Profile. Includes a **web UI** for configuring everything, a
**live SSL/TLS profile picker**, **certificate export**, **OS update
checking/applying**, and **kickstart/Packer/ISO tooling** to produce a
prebuilt appliance image.

## Why this design

- **Lightweight**: no database, no heavy JS framework -- certbot handles
  the ACME protocol, and the web UI is server-rendered Flask + plain
  HTML/CSS.
- **DNS-provider agnostic**: DNS-01 record creation is abstracted behind
  `dns_providers/base.py`. Cloudflare, Azure DNS, and Route53 are included
  out of the box; a `generic_webhook` provider covers anything else
  without writing Python.
- **No inbound NAT/firewall changes required**: DNS-01 instead of HTTP-01
  means the appliance never needs an inbound port opened through the
  Palo Alto, and it supports wildcard certificates.
- **Runs entirely under an unprivileged service account**: certbot is
  pointed at appliance-owned directories instead of root-owned system
  paths. The one exception is the System Updates feature, which is
  deliberately isolated into small, purpose-built, root-owned systemd
  units rather than granting the always-on web process broad privileges
  (see below).

## Repository layout

```
acme-appliance/
├── bin/
│   ├── acme-renew.sh                  # renewal entry point
│   ├── generate-selfsigned-cert.sh    # TLS cert for the web UI
│   ├── check-system-updates.sh        # runs as root via systemd; dnf check-update
│   └── apply-system-update.sh         # runs as root via systemd; dnf update -y
├── cert_naming.py                     # shared "safe cert name" helper
├── iso-build/                         # prebuilt-image tooling (see iso-build/README.md)
│   └── sudoers.d/acme-appliance-updates  # the ONLY sudo rule this appliance needs
├── dns_dispatcher.py                   # certbot auth-hook / cleanup-hook
├── deploy_to_panos.py                  # certbot deploy-hook
├── dns_providers/                      # cloudflare, azure, route53, generic_webhook
├── panos/client.py                     # PAN-OS XML API client
├── webui/
│   ├── app.py                          # Flask routes
│   ├── auth.py                         # local users, bcrypt + optional TOTP MFA
│   ├── system_updates.py               # System Updates feature: PAM step-up auth +
│   │                                    # trigger/status logic for check/apply/reboot
│   ├── config_store.py                 # safe atomic read/write of appliance.yaml
│   ├── test_connections.py             # "Test Connection" + SSL profile fetch logic
│   ├── templates/                      # server-rendered HTML (Jinja2)
│   └── static/style.css
├── config/appliance.yaml.example
├── systemd/
│   ├── acme-renew.service / .timer     # daily renewal (fully sandboxed)
│   ├── acme-webui.service              # the web UI (see below for its sandboxing)
│   ├── acme-appliance-check-updates.service  # root, on-demand, read-only dnf check
│   ├── acme-appliance-updates.service        # root, on-demand, dnf update -y
│   └── acme-appliance-reboot.service         # root, on-demand, reboot
└── requirements.txt
```

## System Updates feature: how it works and why

The web UI's **System** page lets you check for and apply OS package
updates (`dnf update`), and reboot if required afterward -- without
SSHing in. Getting this right required a specific architecture, because
the web UI process itself deliberately runs as an unprivileged,
sandboxed service account and should not be able to run arbitrary root
commands just because a browser button was clicked.

**How a privileged action actually happens:**
1. The always-on `acme-webui.service` process never runs `dnf` directly.
   It only ever asks systemd to start one of three small, purpose-built,
   root-owned, on-demand systemd units:
   `acme-appliance-check-updates.service` (read-only `dnf check-update`),
   `acme-appliance-updates.service` (`dnf update -y`), and
   `acme-appliance-reboot.service` (`reboot`).
2. The unprivileged `acme-appliance` account is allowed to start **only**
   those three units, via a narrowly-scoped sudoers rule
   (`iso-build/sudoers.d/acme-appliance-updates`) -- nothing else. It
   cannot run `dnf` directly, and cannot run `sudo` for any other command.
3. For "Apply updates" and "Reboot" (**not** for the read-only "Check for
   updates"), the web UI additionally requires **step-up authentication**:
   the person clicking the button must separately enter the username and
   password of a real Linux account that is a member of the appliance's
   sudo-capable group (`wheel` by default), verified via PAM
   (`webui/system_updates.py`). Knowing the web UI's own login is **not**
   sufficient to apply updates or reboot -- this matches what you asked
   for ("require the use of the Rocky Linux sudo user credentials").
4. That password is used only in-memory for the single PAM authentication
   call and is never logged, written to disk, or echoed back in any
   response.
5. Failed step-up attempts are rate-limited (5 attempts / 5-minute
   lockout) **before** ever reaching the real PAM stack, both to protect
   real Linux accounts from being brute-forced through this web form and
   to reduce load on the system's authentication stack.

**A necessary trade-off:** `acme-webui.service` has `NoNewPrivileges`
**removed** (every other systemd unit in this appliance keeps it). This
is required because both of the mechanisms above -- PAM authentication
(which internally invokes the setuid-root `unix_chkpwd` helper) and
`sudo`/`systemctl` escalation -- involve executing a setuid binary to
gain privilege, which `NoNewPrivileges` unconditionally blocks regardless
of how narrow the actual permission granted is. The trade-off is
deliberately narrow: this service can *only* escalate via the one
specific sudoers rule (5 exact command patterns) and PAM (identity
verification only) -- it still cannot execute arbitrary commands as root.
See the comments in `systemd/acme-webui.service` for the full reasoning.

**Setup requirements** (all handled automatically by
`iso-build/bootstrap-appliance.sh`, listed here for manual installs):
```bash
# Package providing "needs-restarting" (reboot-required detection):
sudo dnf install -y dnf-utils

# The sudoers rule (always validate before/after installing):
sudo install -m 0440 -o root -g root \
  iso-build/sudoers.d/acme-appliance-updates \
  /etc/sudoers.d/acme-appliance-updates
sudo visudo -c -f /etc/sudoers.d/acme-appliance-updates

# The three new systemd units (not enabled -- only started on demand):
sudo cp systemd/acme-appliance-check-updates.service \
        systemd/acme-appliance-updates.service \
        systemd/acme-appliance-reboot.service \
        /etc/systemd/system/
sudo systemctl daemon-reload

# python-pam (in the appliance's venv):
/opt/acme-appliance/venv/bin/pip install python-pam
```

If your organization grants sudo via a group other than `wheel`, set
`ACME_APPLIANCE_SUDO_GROUP` in `systemd/acme-webui.service`'s
`Environment=` lines to match. Note this checks group membership only --
it does not parse `/etc/sudoers`/`sudoers.d` for individually-granted
sudo rights outside of group membership.

## Getting a running appliance

```bash
sudo dnf install -y epel-release python3 python3-pip certbot openssl dnf-utils
sudo useradd --system --home /opt/acme-appliance --shell /sbin/nologin acme-appliance
sudo mkdir -p /opt/acme-appliance /etc/acme-appliance
sudo cp -r acme-appliance/* /opt/acme-appliance/
sudo python3 -m venv /opt/acme-appliance/venv
sudo /opt/acme-appliance/venv/bin/pip install -r /opt/acme-appliance/requirements.txt
sudo touch /var/log/acme-appliance.log
sudo chown acme-appliance:acme-appliance /var/log/acme-appliance.log
sudo chmod +x /opt/acme-appliance/bin/*.sh /opt/acme-appliance/dns_dispatcher.py /opt/acme-appliance/deploy_to_panos.py

sudo mkdir -p /etc/acme-appliance/letsencrypt /var/lib/acme-appliance/letsencrypt /var/log/acme-appliance/letsencrypt
sudo chown -R acme-appliance:acme-appliance /etc/acme-appliance/letsencrypt /var/lib/acme-appliance/letsencrypt /var/log/acme-appliance/letsencrypt

sudo /opt/acme-appliance/bin/generate-selfsigned-cert.sh acme-appliance.yourdomain.local
sudo cp /opt/acme-appliance/systemd/*.service /opt/acme-appliance/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload

sudo install -m 0440 -o root -g root /opt/acme-appliance/iso-build/sudoers.d/acme-appliance-updates /etc/sudoers.d/acme-appliance-updates
sudo visudo -c -f /etc/sudoers.d/acme-appliance-updates

sudo systemctl enable --now acme-webui.service
sudo systemctl enable --now acme-renew.timer
```

Or, for a one-command bootstrap on a fresh box (does all of the above
automatically):
```bash
git clone <your-repo-url> /tmp/acme-appliance-src
cd /tmp/acme-appliance-src
sudo ./iso-build/bootstrap-appliance.sh
```

## Certbot's storage directories (read this if renewals fail silently)

certbot runs as the unprivileged `acme-appliance` account, pointed at
appliance-owned directories instead of the usual root-owned
`/etc/letsencrypt`:

| Purpose             | Default path                              | Override env var                  |
|----------------------|--------------------------------------------|------------------------------------|
| certbot config/live  | `/etc/acme-appliance/letsencrypt`           | `ACME_APPLIANCE_LE_CONFIG_DIR`     |
| certbot working dir  | `/var/lib/acme-appliance/letsencrypt`       | `ACME_APPLIANCE_LE_WORK_DIR`       |
| certbot's own log     | `/var/log/acme-appliance/letsencrypt`       | `ACME_APPLIANCE_LE_LOGS_DIR`       |

Both `acme-renew.service` and `acme-webui.service` (since "Renew now"
buttons spawn `acme-renew.sh` as a child of the running web UI process)
need `ReadWritePaths` covering `/var/lib/acme-appliance` -- both unit
files already do this.

## Using the web UI

Browse to `https://<appliance-ip>:8443/` -- the first visit prompts you to
create an admin account (with optional TOTP MFA). From there:

1. **DNS Providers** -- one entry per DNS account/zone, each with a "Test
   connection" button.
2. **Firewalls** -- one entry per Palo Alto firewall. **Required PAN-OS
   admin role permissions**: `Configuration`, `Import`, `Commit` for
   deployment; `Operational Requests` additionally for "Test connection".
3. **Domains** -- pick the DNS provider and one or more firewall targets,
   each with its own SSL/TLS Service Profile (use **Fetch profiles** to
   pull real profile names from the firewall via autocomplete). Supports
   wildcards (`*.example.com`) and combining names via **Additional
   names (SANs)**. Once issued, a **Download cert** button exports
   fullchain/cert/chain/privkey as a zip (logged for audit, since it
   includes the private key).
4. **System** -- check for and apply OS updates, and reboot if required
   (see "System Updates feature" above for the full security model).

The dashboard/Domains pages have **Run renewal now** / **Renew now** /
**Force renew now** buttons; single-domain runs for different domains
can run concurrently, but re-running the same domain or "all domains"
while a single-domain run is active is blocked until it finishes.

## Adding a new DNS provider

1. Create `dns_providers/<name>.py` implementing `add_txt_record` /
   `remove_txt_record` (and optionally `test_connection`) from `base.py`.
2. Register it in `dns_providers/__init__.py`'s `PROVIDER_TYPES` and add a
   `PROVIDER_FIELDS` entry for the web UI's form.
3. Restart `acme-webui.service`.

## Wildcard / SAN certificate details

- A domain entry's `name` can be `*.example.com`; `additional_names: []`
  lists extra names issued as SANs on the same certificate.
- Both a wildcard and its apex resolve to the same
  `_acme-challenge.example.com` record. The Azure and Route53 providers
  correctly **merge** simultaneous TXT challenge values there.
- `acme-renew.sh` passes an explicit `--cert-name` so the lineage
  directory never contains a literal `*` -- `*.example.com` becomes
  `wildcard.example.com` (see `cert_naming.py`).

## Security notes (web UI + System Updates)

- Local web UI accounts (bcrypt + optional TOTP MFA); CSRF required on
  all state-changing requests; failed logins rate-limited.
- Certificate/private-key downloads are logged with user, timestamp, IP.
- System Updates step-up auth: see the dedicated section above. Summary:
  PAM identity verification + sudo-group membership check, independently
  rate-limited, password never persisted, actual privilege escalation
  isolated to 3 narrowly-scoped systemd units + one sudoers rule.
- HTTPS via a self-signed cert by default; swap in your internal CA's
  cert/key in `systemd/acme-webui.service`.

## Operational notes

- **Logs**: `/var/log/acme-appliance.log` (includes certbot's console
  output and System Updates progress), viewable from the web UI's Logs
  page. certbot's dedicated log:
  `/var/log/acme-appliance/letsencrypt/letsencrypt.log`.
- **Renewal locking**: `/var/run/acme-appliance/*.lock`.
- **System Updates status files**: `/var/run/acme-appliance/available-updates*.txt`,
  `last-update-status.txt`, `system-update.lock`.
- **Config backups**: `/etc/acme-appliance/backups/` (last 30 kept).

## Known follow-ups / roadmap

- Add a Panorama (template/device-group) variant of `panos/client.py`.
- Add Slack/Teams/email notification on renewal failure or update failure.
- Optional "certificate health" widget querying each firewall's actual
  active cert expiry directly.
- A proper image-update mechanism for the prebuilt-image options.
