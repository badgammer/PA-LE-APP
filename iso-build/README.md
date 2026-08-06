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

## Critical vs optional packages (important)

`bootstrap-appliance.sh` deliberately installs packages in two separate
groups:

- **Critical** (`python3`, `python3-pip`, `certbot`, `openssl`) -- one
  `dnf install` transaction. The appliance genuinely cannot run without
  these.
- **Optional** (`dnf-utils`, `policycoreutils-python-utils`) -- each
  installed in its OWN separate `dnf install` call, with a warning
  logged (not a hard failure) if it can't install.

This split exists because **a single `dnf install` command is
all-or-nothing** -- if even one package in the list has an unresolvable
dependency, the ENTIRE transaction fails, silently blocking every other
package in that same command too. This has bitten this script twice:

1. `python3-venv` isn't a real package on Rocky/RHEL (the `venv` module
   ships inside base `python3`) -- listing it here previously aborted
   the whole transaction, including certbot.
2. `policycoreutils-python-utils` has been observed to have an
   unresolvable dependency on some systems (a repo/mirror metadata
   mismatch reporting `"nothing provides policycoreutils = X.Y-Z"` for
   the exact version its own `python3-policycoreutils` dependency
   needs) -- this ALSO previously aborted the whole transaction and
   blocked certbot from installing, even though the appliance doesn't
   actually need this package (see below).

**Why `policycoreutils-python-utils` (which provides `semanage`) isn't
actually required:** it exists purely for SELinux port-labeling
troubleshooting. In practice this should never be needed here:

- Port 8443 is already included in SELinux's default `http_port_t` port
  list on RHEL/Rocky.
- A plain systemd-launched binary with no custom SELinux type (like our
  gunicorn process) normally runs under the very permissive
  `unconfined_service_t` domain, which doesn't require port-specific
  policy changes to bind to an already-typed port.

If the web UI ever *does* fail to bind under SELinux enforcing mode
(check `journalctl -t setroubleshoot` or `ausearch -m avc -ts recent`
for AVC denials), install `policycoreutils-python-utils` manually and
run `semanage port -a -t http_port_t -p tcp 8443` (use `-m` instead of
`-a` if that port is already assigned a different type).

## After first boot

1. Browse to `https://<appliance-ip>:8443/` and create the admin account.
2. Visit **Settings** and set `acme.email` to a REAL, monitored email address.
3. Add DNS provider(s), firewall(s), and domain(s).
4. Visit **System** to check for updates.

## Known gotchas already fixed in this codebase

- **python3-venv**: not a separate package on Rocky/RHEL.
- **policycoreutils-python-utils / dnf-utils**: installed as best-effort
  extras, never bundled with critical packages (see above).
- **certbot version**: auto-detected at renewal time.
- **DNS zone case-sensitivity**: Azure/Route53 providers compare zone
  names case-insensitively.
- **PAN-OS certificate + private key import**: uses `category=keypair`
  with a single combined cert+key PEM file.
- **Redeploy without re-issuing**: the Domains page has a "Redeploy to
  firewall" button.
- **Deploy failure reporting**: deploy_to_panos.py correctly exits
  non-zero if ANY firewall target fails.
- **Panorama-managed firewalls**: a specific hint is shown for the
  "may need to override template object first" error.
