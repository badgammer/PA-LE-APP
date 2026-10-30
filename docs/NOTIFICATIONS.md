# Email alerting (Notifications)

Email alerts for certificate problems, for both install profiles. Configured on a **Notifications** page; nothing is sent
until you turn it on there.

## What you get

| Alert | When it fires | Default | Fired by |
|---|---|---|---|
| Certificate expiring soon | A certificate is within N days of expiry (default 14), or already expired. One digest email listing every affected certificate; repeats every 3 days, daily once 3 days or less remain. | on | Daily scan (systemd timer) |
| DNS challenge failed | A DNS-01 TXT record could not be added or removed. At most one email per domain per 6 hours. | on | `dns_dispatcher.py` |
| Renewal failed | certbot failed for a domain. The email includes the tail of certbot's output. At most one per domain per 6 hours. | on | `bin/acme-renew.sh` |
| Deployment failed | One or more deploy targets rejected the certificate (or raised an unexpected error). Lists each failing target and the ones that did succeed. At most one per domain per 6 hours. | on | `deploy_certificate.py` |
| Certificate issued / renewed | certbot issued or renewed a certificate. Not sent for "not due yet" runs and not sent for a Redeploy. | off | `deploy_certificate.py` (certbot's deploy hook) |
| Certificate deployed | Every deploy target for a domain succeeded (also for a Redeploy). | off | `deploy_certificate.py` |

The expiry scan reads the certificates on disk and does not depend on the renewal job having run, so it is the backstop that
also catches a renewal job that has silently stopped. Only certificates that match a domain currently in the config are reported,
so a leftover lineage from a deleted domain does not alert forever.

One failed DNS challenge normally produces two emails (a "DNS challenge failed" with the provider's exact error, then a "Renewal
failed" when certbot gives up). Untick whichever you find redundant on the Notifications page.

## Choosing a delivery method

| Method | Use it when |
|---|---|
| **Microsoft 365 (OAuth 2.0 via Microsoft Graph)** | The sender is a Microsoft 365 mailbox. No user sign-in, no redirect URI, no token refresh: it uses an app registration with a client secret (client-credentials flow). |
| **SMTP** | Any other mail server or an internal relay. STARTTLS (587), SSL/TLS (465) or none (25), optional login. |
| **Use the MSP-wide sender** (msp-panos customers only) | You want one set of mail credentials for the whole fleet. An owner configures it once under **Notifications** in the MSP Console. |

Why Microsoft Graph instead of SMTP for Microsoft 365: Exchange Online is retiring Basic authentication for SMTP AUTH.
As of Microsoft's January 2026 update it is disabled by default for existing tenants at the end of December 2026, with a final
removal date to be announced in the second half of 2027. Graph with OAuth is unaffected. Google Workspace/Gmail OAuth is
deliberately not offered: it needs an interactive consent flow with refresh tokens, which is not worth the fragility for an
appliance. Use an SMTP relay or app password there.

### Microsoft 365 setup (about 10 minutes)

1. Entra admin center > **App registrations > New registration** (single tenant, no redirect URI).
2. Copy the **Directory (tenant) ID** and **Application (client) ID** into the Notifications page.
3. **Certificates & secrets > New client secret**. Paste the secret **Value** (not the Secret ID). Note its expiry date: when it
   expires, alerts stop until you paste a new one.
4. Choose the sender mailbox, for example `acme-alerts@yourdomain.com`. A shared mailbox works and needs no license. Enter it as
   "Send as".
5. Authorize the app to send **as that one mailbox**.

   The Microsoft Graph `Mail.Send` *application* permission lets an app send as **any** mailbox in the tenant, so do not
   grant it tenant-wide unless this is a lab. The recommended route is Exchange Online **RBAC for Applications**:
   `New-ServicePrincipal` for the app, a `New-ManagementScope` that matches only the alert mailbox, then
   `New-ManagementRoleAssignment` with the *Application Mail.Send* role, and verify with `Test-ServicePrincipalAuthorization`.
   Do not also leave an unscoped Mail.Send grant in Entra ID: the two add together. (Older "Application Access Policies" still work
   but Microsoft is no longer investing in them.) See Microsoft's "RBAC for Applications in Exchange Online" article for full syntax.

   *Quick lab alternative:* Entra **API permissions > Add a permission > Microsoft Graph > Application permissions > Mail.Send**,
   then **Grant admin consent**.
6. On the page click **Test connection** (checks the app can sign in), then **Send test email** (proves the mailbox can send).

Notes: the test buttons use what is currently typed in the form, so you can test before saving. After a successful *Test
connection* the page may say no tenant-wide Mail.Send grant was found; that is expected for the scoped setup.

## The daily expiry scan

`install.sh` (and therefore `bootstrap-appliance.sh`) installs `acme-notify-check.timer` on new installs, for either profile. It runs
daily at 07:30 plus up to 20 minutes of random delay. On a single-instance host it runs as `acme-appliance`; under msp-panos it runs
as `acme-msp-console` and checks **every** customer namespace. It does nothing until alerts are enabled on the Notifications page.

For a host that was installed **before** this update, install the timer once after deploying the new code (git pull):

```bash
sudo bash /opt/acme-appliance/bin/install-notify-timer.sh            # install + enable + start (safe to re-run)
sudo bash /opt/acme-appliance/bin/install-notify-timer.sh --remove   # to undo
```

Try the scan without sending anything:

```bash
sudo -u acme-appliance /opt/acme-appliance/venv/bin/python /opt/acme-appliance/bin/acme-notify.py check-expiry --dry-run
```

## MSP console behaviour

* **Notifications** (owners only, top level): the MSP-wide sender (Microsoft 365 or SMTP). Stored in
  `/etc/acme-appliance/msp-console/notifications.yaml` (mode 0600).
* **Customer > Notifications** (needs write access to that customer): recipients, which alerts, expiry threshold, and the delivery
  method (own sender, or the MSP-wide one). Subjects are prefixed with the customer slug, e.g.
  `[ACME] acme-corp: 2 certificate(s) expiring soon (4d left)`.
* A customer with read-only access can view the page but has no save/test buttons.

## Command line

```bash
acme-notify.py test            [--config PATH]
acme-notify.py event NAME      --domain D --detail "text" [--config PATH] [--label customer]
acme-notify.py check-expiry    [--config PATH --live-dir DIR | --all-customers] [--dry-run]
```

`event` always exits 0 so an alert problem can never fail a renewal or deploy script. In Python the equivalent is
`notifications.notify_event(event, domain, detail, config_path=...)`, which also never raises.

## How the alerts are fired

`bin/acme-renew.sh` calls `bin/acme-notify.py event renewal_failed ...` when certbot fails for a domain. `deploy_certificate.py`
(certbot's `--deploy-hook`, and also what the Redeploy button runs) sends the renewed / deployed / deploy-failed alerts, and
`bin/redeploy-cert.sh` sets `ACME_APPLIANCE_REDEPLOY=1` so a redeploy is never reported as a renewal. All of these are best effort:
a mail problem is written to the appliance log and can never fail or slow a renewal, a deploy or a DNS challenge.

The certbot hooks now run under the appliance's virtualenv Python (`/opt/acme-appliance/venv/bin/python`) when it exists. They used to
run under the system `python3`, which does not have `dnspython` (rfc2136, CNAME-alias), `boto3` (Route 53) or `pywinrm` (IIS).

## Where settings are stored

A `notifications:` block in the appliance's own `appliance.yaml` (per customer under msp-panos), in the same file and with the same
0600 permissions as the DNS and deploy credentials, so it is included in the existing automatic config backups. De-duplication state
is `notification-state.json` beside it. Secrets are never sent back to the browser: leave a secret box blank to keep the stored value.

## Troubleshooting

| Message | Meaning |
|---|---|
| `Microsoft sign-in rejected the app credentials: AADSTS7000215 ...` | Wrong or expired client secret, or you pasted the Secret ID instead of the Value. |
| `Microsoft Graph sendMail failed (HTTP 403)` | The app may not send as that mailbox: scope/consent missing, or the mailbox is outside the RBAC scope. |
| `... (HTTP 404) ... mailbox ... was not found` | The "Send as" address is not a real mailbox. |
| `SMTP login rejected` | Bad username/password, or that account may not use SMTP AUTH. |
| `Could not connect to SMTP server ... certificate verify failed` | Untrusted certificate: fix the certificate, or untick "Verify the server's TLS certificate" for an internal relay. |
| Nothing arrives, no error | Check recipients, spam folder, and `/var/log/acme-appliance.log` (single) or `/var/log/acme-appliance/msp-console.log` (MSP) for `acme-notify` lines. |
