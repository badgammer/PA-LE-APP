# Integrating the MSP Console into your existing repo

This addon is almost entirely **new files** -- copy them into your repo
at the paths shown below, then make the small number of precise edits
listed at the end. No changes are needed to `webui/`, `dns_providers/`,
`deploy_providers/`, `panos/`, or any single-instance-profile file at
all -- the MSP Console is only ever installed under the `msp-panos`
profile.

## 1. Copy these new files into your repo

```
msp_console/app.py
msp_console/auth.py
msp_console/fleet.py
msp_console/actions.py
msp_console/static/style.css
msp_console/templates/base.html
msp_console/templates/login.html
msp_console/templates/login_verify.html
msp_console/templates/setup.html
msp_console/templates/dashboard.html
msp_console/templates/customers.html
msp_console/templates/customer_new.html
msp_console/templates/customer_detail.html
msp_console/templates/admins.html
msp_console/templates/admin_form.html
msp_console/templates/account.html
systemd/msp-console/acme-msp-console.service
iso-build/sudoers.d/acme-msp-console
bin/msp-tail-log.sh
```

No new Python dependencies are required -- Flask, bcrypt, pyotp, and
PyYAML are all already in `requirements-core.txt`, installed under
BOTH profiles already. `bin/msp-tail-log.sh` also needs no separate
`chmod +x` step -- your existing `iso-build/bootstrap-appliance.sh`
already runs `chmod +x "$INSTALL_DIR"/bin/*.sh` as a wildcard over
everything in `bin/`, which picks up this new script automatically.

Note: `msp_console/` does NOT need an `__init__.py` -- it deliberately
mirrors `webui/`'s existing pattern of flat sibling imports (`import
auth`, `import fleet`) via `sys.path.insert`, run directly by gunicorn
from within that directory (`WorkingDirectory=.../msp_console`,
`app:app`), rather than being imported as a Python package from
elsewhere. This was verified directly while testing this addon.

## 2. Replace one existing file

```
bin/msp-deprovision-customer.sh   -- REPLACE with the version in this addon
```

The only functional change: it now accepts a `--yes` flag that skips
the interactive confirmation prompt. This is required because the MSP
Console calls this script via `sudo` from a web request, which has no
TTY to prompt on at all -- the console performs its own confirmation
step (a "type the customer slug to confirm" field) in the web UI
*before* ever invoking this script. Calling the script by hand from a
real terminal without `--yes` behaves exactly as before (interactive
prompt).

## 3. Extend `lib/profile-msp-panos.sh`

Replace your existing `lib/profile-msp-panos.sh` with the version in
this addon. It contains everything your current file already does
(templated systemd units, shared parent directories, nginx template
detection) PLUS a new `_install_msp_console()` step that:
- creates a dedicated `acme-msp-console` system account (never root,
  never shared with any customer's own account)
- creates `/etc/acme-appliance/msp-console/` (0700, owned by that
  account) for its admin database and secret key
- generates a self-signed TLS certificate for the console (separate
  from any customer's own cert)
- installs and validates the new sudoers rule
- installs, enables, and starts `acme-msp-console.service`
- opens firewalld port 9443/tcp if firewalld is active

If you'd rather patch your existing file by hand instead of replacing
it wholesale, the only required additions are:
1. A call to `_install_msp_console "$appliance_dir"` somewhere inside
   `run_profile_install()`, after the templated-units step.
2. The `_install_msp_console()` function itself (copy verbatim from
   this addon's version of the file).
3. Update the final "Done." banner to mention
   `https://<host>:9443/` for the MSP Console (optional, cosmetic).

## 4. Documentation updates (optional but recommended)

Add a section to your main `README.md` under "Running the msp-panos
profile" describing the dashboard as the primary way to manage the
fleet, with the CLI scripts (`bin/msp-*.sh`) as the underlying
mechanism / scriptable alternative. Suggested addition:

```markdown
### MSP Console dashboard

Once a host is installed with `--profile=msp-panos`, the primary way to
manage the fleet is the MSP Console at `https://<host>:9443/` -- first
visit prompts you to create the initial owner account. From there you
can:
- Add and remove customers (equivalent to `bin/msp-provision-customer.sh`
  / `bin/msp-deprovision-customer.sh`, without needing shell access)
- View fleet-wide health (cert expiry, service state) and each
  customer's recent log activity
- Trigger a renewal check or restart a hung customer's web UI
- Add other MSP staff accounts, each with their own per-customer
  read/write grants -- a staff account only ever sees the customers
  explicitly granted to it; anything else is completely invisible to
  them, not just hidden behind disabled buttons.

Owners have full read/write access to every customer and can manage
other admin accounts; staff accounts are scoped per-customer via an
explicit grant table (read, read+write, or no access at all). The
`bin/msp-*.sh` CLI scripts remain fully functional and are what the
console itself calls under the hood via a narrowly-scoped sudoers rule
(see `iso-build/sudoers.d/acme-msp-console`) -- use them directly for
scripting/automation/cron use where a web session isn't appropriate.
```

## Testing performed on this addon (see chat for full detail)

- All Python compiles (`python3 -m py_compile`); all bash passes
  `bash -n`; the new systemd unit parses as valid INI; the sudoers file
  passes real `visudo -c -f` syntax validation.
- `msp_console/auth.py`'s full permission model was exercised directly:
  owner bypass, per-customer read/write staff grants, unlisted-customer
  invisibility, write-implies-read, grant updates, account deletion,
  and password lockout -- all verified correct.
- `msp_console/fleet.py` was tested against a synthetic customer
  directory tree with real (self-signed) certificates of varying
  expiry, correctly flagging a near-expiry cert, a healthy customer,
  and a not-yet-issued customer.
- `msp_console/actions.py`'s exact subprocess argv for every privileged
  action was captured and cross-checked argument-for-argument against
  `iso-build/sudoers.d/acme-msp-console`'s rules -- all five match
  exactly. Invalid-slug rejection was verified to happen BEFORE any
  subprocess call is ever constructed, and sudo failures were verified
  to surface as a catchable `ActionError` with a safe message.
- Every template was rendered with realistic mock data in BOTH
  permission states (owner vs. read-only staff vs. write-capable staff)
  -- confirmed the "Admins" nav link, the "Add customer" button, the
  fleet-wide total count, the renewal/restart action buttons, and the
  entire "danger zone" deprovision section are all correctly hidden for
  every case where the current admin lacks the required grant, not
  merely styled differently.
- Caught and fixed one real bug during testing: `actions.py` originally
  used a relative import (`from .fleet import ...`) inconsistent with
  how `app.py` imports its sibling modules (flat imports via
  `sys.path.insert`, matching `webui/app.py`'s own established style
  for a gunicorn-run, non-packaged Flask app) -- fixed to a flat import.
- Caught and fixed one real design bug before it ever became code:
  `acme-msp-console.service` initially set `NoNewPrivileges=true`, which
  would have silently broken EVERY privileged action the console
  performs (sudo is a setuid-root binary; `NoNewPrivileges=true` blocks
  setuid escalation entirely) -- corrected with the same reasoning
  already documented on the single-instance profile's
  `acme-webui.service` for its own, differently-scoped sudo use.
- Not tested (no root available in this environment): actually
  installing the sudoers file and confirming `sudo -l` grants exactly
  the intended commands to the `acme-msp-console` account on a real
  host. `visudo -c` confirms the file parses correctly, but a live
  `sudo -l -U acme-msp-console` check on your actual target host is
  worth doing once before relying on this in production.
