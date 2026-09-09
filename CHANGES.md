# Change Tree -- Multi-Target Deploy Provider Rebuild

Baseline: `PA-LE-APP-Beta_Release.zip` (the uploaded beta with cross-zone
SAN mapping). This pass generalizes the appliance from a PAN-OS-only
tool into a "single pane of glass" that can deploy the same certificate
to any mix of target types, and adds the first new target type: IIS
over WinRM.

## + New files

```
deploy_providers/__init__.py      Registry (PROVIDER_TYPES, INSTANCE_FIELDS, TARGET_FIELDS, get_provider())
deploy_providers/base.py          BaseDeployProvider interface + DeployProviderError
deploy_providers/panos.py         Thin adapter: wraps the UNCHANGED panos/client.py PanosClient
deploy_providers/iis.py           NEW: WinRM-based IIS deploy provider (pfx build, import, netsh bind, cleanup)
deploy_certificate.py             NEW generic deploy dispatcher (mixed-type, multi-target per domain)
webui/templates/deploy_providers.html      List page for deploy target instances (any type)
webui/templates/deploy_provider_form.html  Add/Edit form, type selector + dynamic per-type fields
```

## - Removed files

```
deploy_to_panos.py                 -> replaced by deploy_certificate.py
webui/templates/firewalls.html     -> replaced by deploy_providers.html
webui/templates/firewall_form.html -> replaced by deploy_provider_form.html
```

## ~ Modified files

```
webui/config_store.py
  - DEFAULT_CONFIG: panos_firewalls{} -> deploy_providers{}
  - NEW _migrate_legacy_config(): auto-converts old panos_firewalls[]/
    panos_targets[] into deploy_providers[]/deploy_targets[] on first
    load of an old appliance.yaml, then persists it (one-time, no
    manual steps for existing installs).
  - upsert_firewall/delete_firewall/firewall_in_use ->
    upsert_deploy_provider/delete_deploy_provider/deploy_provider_in_use
    (now scans deploy_targets[] instead of panos_targets[])

webui/app.py
  - New imports: json, deploy_providers.{INSTANCE_FIELDS,TARGET_FIELDS}
  - dashboard(): firewall_count -> deploy_provider_count
  - domains_list(): added _target_summary()/_deploy_targets_display()
    helpers to render any target type's fields generically
  - domain_new/domain_edit: pass deploy_providers + TARGET_FIELDS schema
    to the template instead of panos_firewalls
  - _domain_from_form(): rewritten to parse generic target_instance[]/
    target_config[] (JSON) pairs instead of fixed target_firewall[]/
    target_cert_type[]/target_cert_value[]/target_vsys[] arrays, with
    server-side required-field validation per the selected type's schema
  - /firewalls/<name>/ssl-profiles -> /deploy-providers/<name>/options
    (generic; dispatches to whichever type's list_options())
  - _settings_from_form(): generalized to accept a fields_schema param
    so it's shared between dns_providers and deploy_providers forms
  - Entire firewall CRUD route block replaced with deploy-provider CRUD
    routes (type selector on add, exactly like the DNS provider form)

webui/test_connections.py
  - test_panos_firewall/list_ssl_profiles -> test_deploy_provider/
    list_target_options, both now generic over deploy_providers.get_provider()

webui/templates/base.html       Nav: "Firewalls" -> "Deploy Targets"; rebranded title/header
webui/templates/login.html      Rebranded subtitle (no longer PAN-OS-only)
webui/templates/dashboard.html  Stat card: firewall count -> deploy target instance count
webui/templates/domains.html    "Firewall targets" column -> generic "Deploy targets" (type badge + summary)
webui/templates/domain_form.html
  - "Firewall targets" section replaced with a fully generic, JS-driven
    "Deploy targets" section: each row picks ANY configured instance
    (any type), and its type-specific sub-fields render dynamically
    from deploy_providers.TARGET_FIELDS (client-side), then get
    serialized into a JSON blob (target_config[]) right before submit.
    "Fetch options" buttons wired to the new generic options endpoint.
webui/static/style.css          Added .deploy-target-row / .target-type-fields (flex-based, replaces the old fixed-column grid so rows can hold a variable number of fields per type)

bin/acme-renew.sh          --deploy-hook path: deploy_to_panos.py -> deploy_certificate.py
bin/redeploy-cert.sh       Script path + comments generalized to "deploy target" language
iso-build/bootstrap-appliance.sh   chmod +x path updated to deploy_certificate.py
requirements.txt           Added pywinrm (only required if you configure an iis deploy target)
config/appliance.yaml.example      Rewritten: deploy_providers[]/deploy_targets[] schema, new iis01-prod
                                    example, and a "portal.example.com" example deploying to BOTH
                                    a PAN-OS firewall and an IIS server at once
README.md                  Major rewrite: architecture overview (two symmetric plugin systems),
                            multi-target deploy section, IIS/WinRM setup walkthrough, migration
                            notes, "Adding a new deploy target type" guide
iso-build/README.md         Updated gotchas list for generic deploy target language
```

## Design notes worth knowing

- **Symmetry with `dns_providers/`**: `deploy_providers/` intentionally
  mirrors the existing DNS provider plugin architecture 1:1 (type vs.
  named instance, a registry module, per-type field schemas for the web
  UI). Adding a third target type later (e.g. HAProxy, F5, nginx) is a
  self-contained plugin file plus two registry dict entries -- no other
  file needs to change.
- **`panos/client.py` is completely untouched.** `deploy_providers/panos.py`
  is a thin adapter around it, so none of the existing PAN-OS behavior
  (category=keypair import, full-object type=edit for Panorama, commit
  polling, name-prefix cleanup) changed at all.
- **Zero-touch migration**: existing `appliance.yaml` files with the old
  `panos_firewalls[]`/`panos_targets[]` shape are automatically upgraded
  in place the first time `config_store.load_config()` runs after this
  update -- verified with a round-trip test (old-shape YAML in, new-shape
  YAML out, values preserved exactly).
- **Tested**: all Python files compile; all Jinja templates render
  (both empty and populated states, including mixed PAN-OS+IIS rows);
  the JS emitted by `domain_form.html` passes `node --check`; and
  `deploy_certificate.py`'s dispatch logic was exercised end-to-end
  against mocked providers covering success, per-target failure
  isolation, and mixed target types on one domain.
- **Not yet done / next steps**: `deploy_providers/iis.py` is written
  and unit-testable in isolation but has not been run against a real
  Windows/IIS host in this environment (no Windows target available
  here) -- validate the WinRM/netsh commands against a lab IIS box
  before relying on it in production, the same way you'd want to smoke
  test any new panos_firewalls instance's "Test connection" button
  first.

---

## Change Tree -- Unified Installer + Generalized Naming + git Prerequisite

Baseline: the previous multi-target release (PAN-OS + IIS, before any
install-profile work). This pass adds a single-installer, dual-profile
packaging layer on top of the existing multi-target appliance, removes
customer-identifying naming from docs/examples, and fixes a genuine
first-boot gap: a stock Rocky Linux 9 "minimal" install does not include
`git`, which every quick-start path in this repo assumes is present.

### + New files

```
install.sh                              Repo-root entry point -- prompts or takes --profile=
lib/profile-single-instance.sh          Absorbs bootstrap-appliance.sh's former single-tenant
                                           setup steps (service account, config seed, TLS cert,
                                           sudoers, firewalld, static systemd units, enable/start)
lib/profile-msp-panos.sh                Multi-tenant setup: templated systemd units, shared
                                           parent directories, nginx template staging -- explicitly
                                           skips sudoers/TLS-cert/single-service-account/firewalld,
                                           none of which have a safe multi-tenant equivalent
systemd/msp/acme-webui@.service         Templated web UI unit, one per customer instance
systemd/msp/acme-renew@.service         Templated renewal unit, one per customer instance
systemd/msp/acme-renew@.timer           Templated daily renewal timer, randomized within 1hr
                                           so many customer instances don't fire simultaneously
bin/msp-provision-customer.sh           Onboards a customer: dedicated system account
                                           (acmecust-<slug>), directory tree, starter config,
                                           enable+start; prints the nginx server block to add
bin/msp-deprovision-customer.sh         Offboards a customer: stop/disable units, archive
                                           (never delete outright) config+certs to a dated
                                           tarball, remove the system account
bin/msp-fleet-status.sh                 Cross-customer health view (cert expiry + systemd unit
                                           state), read directly off disk/systemd -- independent
                                           of whether any customer's web UI process is up
requirements-core.txt                   Split out of requirements.txt -- installed under BOTH
                                           profiles
requirements-iis.txt                    Split out of requirements.txt -- pywinrm, installed
                                           ONLY under single-instance (msp-panos never installs
                                           this dependency at all, not merely hides the iis type)
deploy/nginx/acme-appliance-msp.conf.template
                                         Per-customer nginx vhost, routed to that customer's
                                           unix socket (msp-panos has no direct TCP listener
                                           per customer by design)
```

### - Removed files

```
deploy_to_panos.py     Stale leftover from before the deploy_providers/ generalization --
                          already superseded by deploy_certificate.py; deleted.
requirements.txt        Replaced by requirements-core.txt + requirements-iis.txt (see above).
```

### ~ Modified files

```
deploy_providers/__init__.py
  - Added ACME_APPLIANCE_PROFILE-based filtering: _ALL_PROVIDER_TYPES /
    _ALL_INSTANCE_FIELDS / _ALL_TARGET_FIELDS hold every type this
    codebase knows about; the public PROVIDER_TYPES / INSTANCE_FIELDS /
    TARGET_FIELDS are narrowed to just {"panos"} when
    ACME_APPLIANCE_PROFILE=msp-panos, or left at "everything available"
    otherwise (including unset/unrecognized values, so pre-existing
    installs are unaffected by default).
  - get_provider() now raises a specific, actionable DeployProviderError
    when a provider_type exists in this codebase but is disabled under
    the active profile (vs. the generic "unknown type" error for a
    genuinely unregistered type).
  - iis.py's import is now wrapped in a try/except ImportError, matching
    the pattern iis.py itself already uses internally for `winrm` --
    this module now degrades gracefully rather than raising at import
    time if pywinrm truly isn't installed (relevant on an msp-panos host,
    which never installs it).

webui/app.py
  - Added INSTALLED_PROFILE / SYSTEM_UPDATES_ENABLED module-level
    constants, read from ACME_APPLIANCE_PROFILE.
  - inject_globals() now also exposes system_updates_enabled to every
    template.
  - New _require_system_updates_enabled() helper, called at the top of
    all four /system* routes -- returns 404 outright under msp-panos
    rather than letting a request reach system_updates.py at all. This
    matters because the System Updates feature is fundamentally
    single-host (dnf update -y, reboot, via a sudoers rule tied to ONE
    fixed service account) with no safe per-tenant equivalent -- an MSP
    customer's tenant admin must never be able to reboot the shared host
    out from under every other customer's instance.

webui/templates/base.html
  - The "System" nav link is now wrapped in
    {% if system_updates_enabled %} -- invisible under msp-panos,
    exactly mirroring the route-level gate above (belt and suspenders:
    even if someone typed the URL directly, the route itself still 404s).

iso-build/bootstrap-appliance.sh
  - Trimmed down to ONLY universal, profile-independent steps: OS
    package installation, copying the appliance source (plus
    install.sh/lib/) to /opt/acme-appliance, creating the Python venv
    (dependency installation itself moved to install.sh, since WHICH
    requirements file(s) get installed is profile-specific).
  - NEW: "git" added to the critical-package dnf install list alongside
    epel-release/python3/certbot/openssl -- a stock Rocky Linux 9
    "minimal" install does not ship git, and this repo's own quick-start
    instructs cloning with git before this script is ever reachable, so
    this is defensive for any path that reaches this script without git
    already having been installed some other way first.
  - Everything else it used to do (service account creation, systemd
    unit installation, config seeding, TLS cert generation, sudoers,
    firewalld) moved into lib/profile-single-instance.sh, unchanged in
    behavior -- this script now execs install.sh as its last step,
    forwarding any arguments given after a "--" separator.

iso-build/ks.cfg
  - The %post section's bootstrap-appliance.sh invocation now passes
    "-- --profile=single-instance --non-interactive" explicitly. This is
    NOT optional: %post runs completely unattended with no TTY, and
    install.sh's interactive profile prompt (read -rp) would otherwise
    hang the kickstart forever. Preserves this kickstart's historical
    behavior (it has always produced a single-instance appliance).

README.md
  - NEW: "Getting a running appliance" now explicitly calls out that
    `git` must be installed first (`sudo dnf install -y git`) since a
    stock Rocky Linux 9 minimal install doesn't include it -- added
    right before the `git clone` step, and as gotcha #10.
  - New "Install profiles" section with the full single-instance vs.
    msp-panos comparison table.
  - "Getting a running appliance" rewritten to show both the
    interactive and non-interactive (--profile=...) bootstrap paths.
  - New "Running the msp-panos profile" section documenting the
    onboard/fleet-status/offboard workflow and fleet-wide code updates.
  - New gotcha #9 documenting why System Updates is fully disabled
    (both routes and the sudoers rule) under msp-panos, not merely
    hidden from the nav bar.
  - "Adding a new DNS/deploy provider type" sections updated: the
    restart command now differs by profile, and the deploy-provider
    section notes how to make a new type profile-restricted if it
    doesn't have a safe multi-tenant story (matching how System Updates
    itself is excluded, even though that one isn't a deploy provider).
  - Customer-identifying example domain names removed (see "Generalized
    naming" below) -- all other content unchanged.

iso-build/README.md
  - Explains the bootstrap-appliance.sh -> install.sh handoff and how
    ks.cfg/appliance.pkr.hcl relate to profile selection.
  - "After first boot" now has separate walkthroughs per profile.
  - Known-gotchas list extended with the git-prerequisite, profile-split,
    and System Updates disablement entries.
```

### Generalized naming (customer-identifying strings removed)

Two real-sounding customer/company domain names that had crept into
documentation examples and code comments were replaced with the
industry-standard "obviously fictional" placeholders (matching the
convention Microsoft's own docs use), consistently across every file
that referenced them:

```
hoffman.net / azure-hoffman-net       -> contoso.com / azure-contoso
howardscams.com / azure-howardscams   -> fabrikam.com / azure-fabrikam
HowardsCams.com (mixed-case variant)  -> Fabrikam.com
```

Affected files: `README.md`, `config/appliance.yaml.example`,
`cert_naming.py` (module docstring example), `dns_providers/azure.py`
(a code comment illustrating a mixed-case zone-name bug fix), and
`webui/templates/dns_provider_form.html` (a placeholder instance-name
example). No functional code changed -- these were all documentation,
comments, or example/placeholder values, never real configuration.

The GitHub clone URL (`https://github.com/badgammer/PA-LE-APP`) was
deliberately left as-is per explicit instruction -- it is not a
customer-identifying string and is out of scope for this generalization
pass.

### Design notes worth knowing

- **Not a fork.** Every plugin (`dns_providers/`, `deploy_providers/`),
  every template, and the entire `webui/` application are byte-for-byte
  identical across both profiles. The profile choice changes exactly
  three things: which deploy provider types `deploy_providers/__init__.py`
  exposes, whether the System Updates routes in `webui/app.py` respond
  at all, and which systemd units + identity model `install.sh` sets up.
  A bug fix anywhere else in the codebase benefits both profiles
  automatically with zero porting effort.
- **Per-customer identity uses plain system accounts, not systemd
  DynamicUser.** `DynamicUser` combined with raw `ReadWritePaths=` on
  arbitrary `/etc` paths does not get automatic ownership management
  from systemd the way `StateDirectory=`/`ConfigurationDirectory=`
  do -- getting that combination genuinely correct requires directory
  creation to happen at systemd's hands, not a provisioning script's,
  which conflicts with wanting to pre-seed a starter `appliance.yaml`
  before first start. `msp-panos` instead gives each customer instance
  its own plain `useradd --system` account (`acmecust-<slug>`),
  mirroring EXACTLY how the existing single-instance profile already
  establishes identity for its one `acme-appliance` account.
- **System Updates required a real architectural decision, not just a
  config flag.** `webui/system_updates.py` and
  `iso-build/sudoers.d/acme-appliance-updates` are inseparable from the
  single fixed `acme-appliance` account (the sudoers rule names that
  account explicitly) and require `NoNewPrivileges` to stay unset for
  PAM/setuid access -- there is no safe way to offer "reboot the host"
  or "apply OS updates" per-tenant on a shared MSP fleet host. It is now
  excluded at both the application layer (404 on every route) and the
  OS-privilege layer (`lib/profile-msp-panos.sh` never installs that
  sudoers rule at all). A useful side effect: because msp-panos
  instances never need PAM/sudo access, their systemd units can set
  `NoNewPrivileges=true` -- strictly tighter sandboxing than the
  single-instance web UI unit is able to use.
- **The git fix closes a real first-boot gap, not a hypothetical one.**
  Every quick-start path in this repo (main README, iso-build README)
  starts with `git clone`, but a stock Rocky Linux 9 "minimal" ISO
  install genuinely does not include git -- confirmed by inspecting
  what `bootstrap-appliance.sh`'s existing critical-package dnf
  transaction did and did not include. Fixed in two places: the
  quick-start commands now explicitly run `sudo dnf install -y git`
  before the `git clone` step, AND `bootstrap-appliance.sh` itself now
  installs `git` as one of its critical packages (defensive, in case
  this script is ever reached via a path that didn't `git clone` first
  -- e.g. a source tree copied some other way).
- **Tested against the actual uploaded codebase, not a reconstruction.**
  This pass was applied directly to a freshly re-uploaded copy of the
  appliance source (not carried over from a prior session's in-memory
  state, since this environment resets between turns) -- every script
  was syntax-checked (`bash -n`), every Python file compiled
  (`python3 -m py_compile`), the YAML example was re-validated, and
  every Jinja template was re-rendered (including `base.html`'s new
  conditional nav link in both enabled/disabled states) against the
  real files in this checkout.
- **Known gap, still called out rather than hidden:** `deploy_providers/iis.py`
  has still not been exercised against a real Windows/IIS host in any
  session so far -- validate against a lab IIS box before relying on it
  in production, regardless of which install profile you use it under.

---

## Change Tree -- MSP Console Fleet Dashboard Integrated

Baseline: this same repo (the unified-installer/dual-profile release),
merged with the previously-built `msp-console-addon` package. Adds a
fleet-management web dashboard for the `msp-panos` profile, so onboarding
a customer, checking fleet health, and delegating scoped access to other
MSP staff no longer requires shell access to the host.

### + New files

```
msp_console/app.py                Fleet dashboard Flask app -- own admin store,
                                     own session, completely separate from any
                                     customer's own web UI login
msp_console/auth.py                Owner/staff permission model: owners get
                                     implicit read+write on every customer;
                                     staff get an explicit per-customer
                                     {slug: "read"|"write"} grant table, with
                                     unlisted customers fully invisible (404,
                                     not 403) rather than merely hidden
msp_console/fleet.py               Customer enumeration + status reads (cert
                                     expiry, systemd unit state) straight off
                                     disk/systemctl -- zero dependency on any
                                     customer's own Flask process being up
msp_console/actions.py             The ONLY module that ever shells out to
                                     sudo -- provision/deprovision a customer,
                                     trigger a renewal, restart a customer's
                                     web UI, tail a customer's log, each
                                     re-validating its slug argument and using
                                     an explicit argv list (never a shell)
msp_console/templates/*.html       Dashboard, customer list/detail, admin
                                     list/add/edit, account settings, login/
                                     setup/MFA -- styled consistently with the
                                     existing webui/ templates
msp_console/static/style.css       Dashboard styling
systemd/msp-console/acme-msp-console.service
                                    Runs as its own unprivileged
                                     acme-msp-console system account, binds
                                     0.0.0.0:9443 directly (unlike per-customer
                                     instances, which bind unix sockets behind
                                     nginx). NoNewPrivileges is deliberately
                                     left unset (not true) -- it still needs
                                     sudo for privileged actions, exactly like
                                     the single-instance acme-webui.service
                                     needs the same exemption for its own,
                                     differently-scoped sudo use
iso-build/sudoers.d/acme-msp-console
                                    Five exact-match sudoers rules (customer
                                     provision/deprovision, renewal trigger,
                                     web UI restart, log tail) -- argument
                                     order/flags cross-checked line-for-line
                                     against actions.py's actual subprocess
                                     calls
bin/msp-tail-log.sh                Root-privileged helper (via sudo) that
                                     reads ONE customer's own log file --
                                     needed because that file is correctly
                                     owned 0600 by the customer's OWN
                                     dedicated account, which the console's
                                     account has no read access to by design
docs/MSP-CONSOLE-INTEGRATION.md    Integration notes (superseded by this
                                     CHANGES.md entry now that the merge is
                                     complete, kept for historical reference)
```

### ~ Modified files

```
bin/msp-deprovision-customer.sh
  - Added a --yes flag that skips the interactive confirmation prompt.
    Required because the MSP Console calls this script via sudo from a
    web request, which has no TTY to prompt on at all -- the console
    performs its own confirmation step (a "type the customer slug to
    confirm" field) in the web UI before ever invoking this script.
    Calling it by hand from a real terminal without --yes is completely
    unchanged (interactive prompt, as before).

lib/profile-msp-panos.sh
  - Added a call to a new _install_msp_console() function, invoked
    right after the existing templated-systemd-units step.
  - _install_msp_console() creates the acme-msp-console service
    account, the /etc/acme-appliance/msp-console/ config directory
    (0700, owned by that account), a self-signed TLS certificate for
    the console (separate from any customer's own cert), installs +
    validates the sudoers rule, installs/enables/starts the systemd
    unit, and opens firewalld port 9443/tcp if firewalld is active.
  - The final "Done." banner now points to the MSP Console URL as the
    primary next step, with the existing bin/msp-*.sh CLI commands
    listed as the scriptable alternative underneath.

README.md
  - New paragraph at the top of "Running the msp-panos profile"
    introducing the MSP Console as the primary fleet-management
    interface, with the CLI scripts repositioned as the underlying
    mechanism / scripting alternative.
  - New gotcha #11 documenting the console's separate account store,
    separate sudoers rule, and the 404-not-403 invisibility model for
    staff accounts without a grant on a given customer.

iso-build/README.md
  - "After first boot" > msp-panos profile: now leads with browsing to
    the MSP Console (port 9443) to create the initial owner account,
    before the nginx-per-customer setup steps.
  - New gotcha entry pointing back to the main README's MSP Console
    section.
```

### Design notes worth knowing

- **Verified against the REAL uploaded files, not a reconstruction.**
  Both the appliance codebase and the msp-console-addon package were
  re-uploaded as actual zip archives this pass (not the garbled
  plain-text office365 preview of them) -- extracted with `unzip`,
  diffed file-for-file against what the integration doc claimed would
  change before applying any edit, and every test below was run against
  the literal merged file tree, not a mental model of it.
- **The one-line "replace" and "extend" instructions in
  MSP-CONSOLE-INTEGRATION.md were verified, not just trusted.**
  `diff`'d both versions of `bin/msp-deprovision-customer.sh` and both
  versions of `lib/profile-msp-panos.sh` before applying either --
  confirmed each addon version is a strict superset of the appliance's
  existing version (only additive changes: the `--yes` flag, and the
  `_install_msp_console` call + function), never a divergent rewrite.
- **Full install.sh dry run for BOTH profiles against the actual merged
  tree**, with `systemctl`/`useradd`/`sudo`/`firewall-cmd`/`pip` stubbed
  and a thin `install`/`chown` shim (to route around this sandbox
  having no real `useradd` to create the accounts those commands would
  otherwise legitimately need to exist) -- confirmed msp-panos now
  additionally creates the console's service account, generates a
  REAL, valid self-signed TLS certificate (verified with
  `openssl x509 -noout -subject -dates`), installs and validates
  (`visudo -c`) the new sudoers file, and installs/starts the new
  systemd unit -- and confirmed single-instance is entirely unaffected
  (byte-identical install flow, since the MSP Console is wired in
  exclusively through the msp-panos-only profile script).
- **Cross-checked every privileged action's exact argv against the
  sudoers file**, using the actual merged `actions.py` (not a copy) --
  all five commands (provision, deprovision, renew, restart, tail-log)
  match their corresponding sudoers rule argument-for-argument.
- **Both template sets (webui/ and msp_console/) render independently
  with no collisions** -- confirmed the two apps' `base.html` files are
  genuinely distinct (separate Flask apps, separate template
  directories, separate ports), and every template in both directories
  parses and renders with realistic mock context.
- **Known gaps carried forward, not newly introduced:**
  `deploy_providers/iis.py` still hasn't been exercised against a real
  Windows/IIS host, and the MSP Console's sudoers rule still hasn't
  been validated with a live `sudo -l -U acme-msp-console` on a real
  host with a real root user -- both call for the same kind of one-time
  validation on an actual target box before production use.

---

## Change Tree -- Two Live-Reported Bug Fixes (MSP Console Deployment)

Baseline: this same repo, as re-uploaded after real-world use of the
MSP Console on a live msp-panos host surfaced two distinct, previously
untested failure modes. Both were reproduced in isolation before being
fixed, and both fixes were re-verified end-to-end against the actual
files in this upload (not a reconstruction).

### ~ Modified files

```
iso-build/bootstrap-appliance.sh
  - Added a copy step for iso-build/sudoers.d/ into the installed
    /opt/acme-appliance/iso-build/ tree, alongside the existing
    install.sh/lib/ re-copy exceptions to the iso-build/ exclusion.
  - BUG: both lib/profile-single-instance.sh (for
    iso-build/sudoers.d/acme-appliance-updates) and
    lib/profile-msp-panos.sh (for
    iso-build/sudoers.d/acme-msp-console) read their sudoers rule
    source file from $INSTALL_DIR/iso-build/sudoers.d/<name> at
    install time -- but bootstrap-appliance.sh never copied that
    directory into the installed tree, only install.sh and lib/ were
    special-cased back in after the broader iso-build/ exclusion. The
    `[[ -f "$sudoers_src" ]]` check in both profile scripts silently
    failed (prints a WARNING, does not abort), leaving the affected
    service account (acme-appliance or acme-msp-console) with ZERO
    sudo grants. Every privileged action that account tried afterward
    failed identically with "sudo: a password is required" -- this
    surfaced in production as the MSP Console's "Restart web UI" and
    log-tail actions both failing with that exact message.
  - REPRODUCED: confirmed in isolation (a fake source tree missing this
    copy step reliably failed to produce iso-build/sudoers.d/* in the
    installed tree) before fixing, and re-verified end-to-end afterward
    by running the actual (now-fixed) bootstrap-appliance.sh through to
    install.sh --profile=msp-panos and confirming both sudoers source
    files land correctly and get installed + visudo-validated.

bin/msp-provision-customer.sh
  - Added CUSTOMER_ACCESS_LOG_FILE
    (/var/log/acme-appliance/customers/<slug>-access.log), pre-created
    and chowned to the customer's dedicated account exactly the same
    way CUSTOMER_LOG_FILE already was.
  - BUG: systemd/msp/acme-webui@.service's gunicorn ExecStart uses
    --access-logfile pointing at that exact path, but nothing ever
    pre-created it. The customer's account (acmecust-<slug>) can write
    to an EXISTING file in the shared, root-owned
    /var/log/acme-appliance/customers/ directory, but cannot CREATE a
    brand-new one there (that requires write permission on the
    directory itself, which only root has under its 0755 mode) --
    gunicorn's very first attempt to open its own access log for
    writing therefore failed with a PermissionError, and the entire
    acme-webui@<slug>.service unit exited immediately on every single
    start attempt, including every subsequent "Restart" click.
  - REPRODUCED: confirmed the exact POSIX permission mechanics with a
    directory-permission test before fixing, and re-verified end-to-end
    afterward by actually running the real (now-fixed)
    msp-provision-customer.sh script and confirming both the main log
    file and the access log file are pre-created, chowned, and
    permissioned identically.
```

### Design notes worth knowing

- **Both bugs were live production reports, not hypotheticals.** The
  first surfaced as literal "sudo: a password is required" errors
  visible in the MSP Console's own UI (log tail and restart-web-UI
  actions); the second surfaced as a customer's web UI staying down
  even after an explicit restart, with the underlying
  `journalctl -u acme-webui@<slug>.service` output showing gunicorn's
  own error: `Error: '/var/log/acme-appliance/customers/<slug>-access.log'
  isn't writable [PermissionError(13, 'Permission denied')]`.
- **Neither bug was caught by any of the prior testing passes** because
  every earlier dry run either stubbed out `useradd`/`chown` entirely
  (masking the real ownership semantics that Fix #2 depends on) or
  never exercised the FULL bootstrap-appliance.sh -> install.sh chain
  in one continuous run against the literal source tree about to be
  uploaded (which is what Fix #1's gap actually required to surface).
  This pass's re-verification specifically closes both of those gaps:
  Fix #1 was checked with a full, continuous
  bootstrap-appliance.sh -> install.sh --profile=msp-panos run against
  a real copy of the exact uploaded source tree, and Fix #2 was checked
  by literally executing the real, now-patched
  bin/msp-provision-customer.sh script rather than reasoning about it.
- **Immediate remediation for hosts already affected by either bug**
  (i.e. provisioned/installed before this fix) is NOT automatic --
  these are install-time/provision-time fixes, so any customer already
  provisioned with the old script, or any host already bootstrapped
  with the old install flow, needs the missing file(s) created by hand
  once (re-installing the sudoers file from this corrected repo, and/or
  creating+chowning the missing `<slug>-access.log` file for each
  already-provisioned customer). Re-running the corrected scripts going
  forward prevents the issue for any NEW customer or NEW host from this
  point on.

---

## Change Tree -- useradd/passwd Lock Contention Fix (Live-Reported)

Baseline: this same repo, after both prior fixes (sudoers copy, access
log pre-creation) were confirmed present. Despite those fixes, customer
provisioning still failed with `useradd: cannot lock /etc/passwd; try
again later.` -- a genuinely different, previously untested failure
mode: useradd/userdel serialize on a shared, OS-wide account-database
lock, and NOTHING in this codebase previously protected against two
near-simultaneous account mutations colliding on it (no server-side
lock around provisioning, no client-side guard against a double-click
on "Provision customer", and no retry around useradd itself even
though its own error message is literally asking for one).

### ~ Modified files

```
bin/msp-provision-customer.sh
  - Added an appliance-wide flock (/run/acme-appliance/msp-account-ops.lock,
    30s wait) taken BEFORE any account-database mutation, so this
    appliance's own provision/deprovision calls always run one at a
    time and can never collide with each other regardless of what
    triggered them concurrently (double-click, two admins, a retried
    web request, etc.).
  - Added _retry_account_cmd(), a short exponential-backoff retry
    wrapper (5 attempts, 1/2/4/8s delays) around the useradd call
    specifically -- covers contention from anything OUTSIDE this
    appliance's own flock (e.g. a human running useradd by hand on the
    box at the same moment), since useradd's own "try again later"
    error message is quite literally advising exactly this.

bin/msp-deprovision-customer.sh
  - Added the SAME flock, using the SAME lock file as
    msp-provision-customer.sh, so a provision for one customer and a
    deprovision for another can never race on the shared account
    database either.
  - Wrapped the userdel call in the same _retry_account_cmd() helper.
    Preserved the original call's "non-fatal on final failure" behavior
    (`|| true`) but deliberately did NOT keep the original
    `2>/dev/null` redirect, since that would have also silently
    swallowed the retry helper's own progress messages on stderr.

msp_console/templates/customer_new.html
  - Added a disable-on-submit script: the "Provision customer" button
    disables itself (and its label changes to "Provisioning...") the
    instant the form is submitted, preventing a double-click or an
    impatient re-click during a slow request from ever sending a
    second, overlapping provisioning request in the first place. This
    is now the FIRST line of defense; the flock in
    msp-provision-customer.sh is the backstop that still holds even if
    this client-side guard is bypassed (e.g. two different browser
    tabs/sessions).

msp_console/templates/customer_detail.html
  - Consolidated the deprovision form's inline onsubmit confirm() and a
    new disable-on-submit guard into one confirmDeprovision(form, slug)
    function, specifically so that canceling the confirmation dialog
    leaves the button clickable again rather than getting stuck
    disabled (a real edge case that a naive "always disable on submit"
    implementation would have introduced).
```

### Design notes worth knowing

- **This was a live production report, reproduced exactly before being
  fixed.** A fake `useradd` was scripted to fail TWICE with the
  identical error text from the reported screenshot
  ("useradd: cannot lock /etc/passwd; try again later.") before
  succeeding on a third call, then the REAL, unmodified
  bin/msp-provision-customer.sh (pre-fix) was run against it end-to-end
  to confirm the script aborted the whole provisioning attempt on the
  very first useradd failure -- exactly matching the reported behavior.
  The same fake useradd was then run again against the PATCHED script
  and confirmed to succeed after two automatic retries, completing
  provisioning fully (directory tree, appliance.yaml, systemd units all
  created) rather than aborting.
- **The flock's serialization was verified with genuinely concurrent
  processes, not just reasoned about.** Two real, separate `bash`
  processes were launched a few milliseconds apart against the actual
  patched script (one provisioning "customer-a", one "customer-b"),
  with a fake useradd that logs precise start/end timestamps and holds
  the "account database" for 0.5s per call. The captured timeline
  confirmed customer-a's useradd call fully completed (START through
  END) before customer-b's useradd call ever started -- i.e. the two
  concurrent provisioning attempts were correctly serialized rather
  than allowed to race.
- **Why a flock AND a retry, not just one or the other:** the flock
  alone only protects this appliance's OWN provision/deprovision calls
  against each other -- it cannot prevent contention from a completely
  external process (a human running `useradd` by hand, unrelated
  config management, etc.) that isn't participating in this
  appliance's locking convention at all. The retry-with-backoff is the
  layer that handles that case, and is cheap/safe to have even though
  the flock alone eliminates the most likely self-inflicted cause
  (there being no client or server-side guard against this appliance's
  own concurrent requests up to this point).
- **The UI disable-on-submit guards are a genuine belt-and-suspenders
  addition, not a replacement for the shell-level fix.** Even with
  perfect client-side guarding, two different browser sessions/tabs
  (e.g. two different MSP staff both provisioning at the same moment)
  could still trigger genuinely concurrent server-side requests -- the
  flock in the shell scripts is what actually guarantees correctness;
  the UI changes only reduce how often that flock's 30-second wait is
  ever actually exercised in practice.
- **Known gaps carried forward, not newly introduced:**
  `deploy_providers/iis.py` still hasn't been exercised against a real
  Windows/IIS host, and no sudoers rule in this repo has yet been
  validated with a live `sudo -l -U <account>` on a real target host --
  both still call for one-time validation on an actual box before
  production use, same as previously noted.





---

## Change Tree -- Stale Account-Lock File Self-Healing (Live-Reported)

Baseline: this same repo, after the flock+retry fix for useradd/passwd
lock contention. A customer still reproducibly failed to provision with
the identical error, EVEN AFTER a full VM reboot intended to "clear
processes" -- proving the retry-with-backoff fix alone was insufficient
for a related but distinct failure mode: a genuinely STALE lock FILE,
not transient contention from a live process.

### ~ Modified files

```
bin/msp-provision-customer.sh
bin/msp-deprovision-customer.sh
  - Both scripts' _retry_account_cmd() now calls a new
    _clear_stale_account_locks_if_safe() between retry attempts, which
    checks (using only /proc -- no fuser/lsof dependency, matching this
    codebase's existing philosophy of not assuming optional packages
    are present) whether any of shadow-utils' lock files
    (/etc/.pwd.lock, /etc/.grp.lock, /etc/.shadow.lock,
    /etc/.gshadow.lock, /etc/.subid.lock, /etc/.subgid.lock) exist AND
    are genuinely not held open by any live process. Only if BOTH are
    true does it remove the stale file before the next retry --
    exactly as safe as a human manually confirming the same thing with
    fuser/lsof/ps before deleting it by hand.
  - BUG: shadow-utils' account-database locking is NOT an in-kernel
    advisory lock (flock/fcntl) tied to a live process -- it is a
    plain lockFILE created with O_CREAT|O_EXCL ("atomically create,
    fail if it already exists"). A process that dies abnormally
    (OOM-killed, a hard VM reset mid-operation, etc.) WITHOUT running
    its own cleanup leaves that lock file behind PERMANENTLY. Critically,
    a plain host reboot does NOT fix this -- rebooting only clears
    processes and memory, it does not delete files under /etc -- so the
    stale file continues blocking every future useradd/userdel call
    indefinitely, with the exact same "cannot lock /etc/passwd; try
    again later" error every single time. The previous fix's
    retry-with-backoff was only ever designed to wait out a live,
    finishing process; it could never recover from a lock that nothing
    is actually holding anymore, which is exactly what a live customer
    report demonstrated -- the same error, still occurring after a full
    VM reboot specifically intended to clear stuck processes.
  - REPRODUCED PRECISELY before fixing: confirmed directly on the
    testing host that a real, pre-existing /etc/.pwd.lock file (dated
    from days earlier, 0 bytes, untouched) causes a fresh
    open(O_CREAT|O_EXCL) attempt against that exact path to fail with
    "File exists" -- the identical underlying mechanism behind
    useradd's error message -- and separately confirmed (via a
    dependency-free /proc-based open-file check) that this specific
    file is NOT held open by any running process, proving it is a
    genuinely orphaned artifact, not active contention.
  - RE-VERIFIED end-to-end afterward: simulated the exact reported
    failure (a stale lock file present from before the script even
    starts, with a fake useradd that fails identically every time the
    file exists) against the real, newly-patched script -- confirmed it
    now detects the stale file on the very first failed attempt,
    removes it safely, and succeeds on the very next retry (after 1s,
    not exhausting all 5 attempts), completing the full provisioning
    flow end-to-end.
```

### Design notes worth knowing

- **This is a genuinely distinct root cause from the prior lock-related
  fix, not a failure of it.** The earlier flock (serializing this
  appliance's own concurrent provision/deprovision calls) and
  retry-with-backoff (for contention from something outside this
  appliance) were both real, correct fixes for real, different
  problems -- but neither one, by design, could ever detect or recover
  from a lock file that has been orphaned by a process that no longer
  exists. This fix adds exactly that missing capability, without
  removing or weakening either of the previous two.
- **Automated removal was scoped as narrowly as it safely could be.**
  The check is not "the file exists, so delete it" -- it is "the file
  exists AND no live process anywhere on the system currently has it
  open," verified by walking every process's actual open file
  descriptors in /proc. This is the same verification a careful
  administrator would perform by hand before manually deleting a
  suspected-stale lock file; the only thing automated here is
  performing that same check reliably every time instead of requiring
  a human to SSH in and do it.
- **Deliberately dependency-free.** `fuser`/`lsof` are the more common
  tools for this kind of check but are NOT guaranteed present on a
  minimal Rocky/RHEL install (this codebase already documents this
  exact caveat for `dnf-utils` and `policycoreutils-python-utils` in
  `iso-build/bootstrap-appliance.sh`) -- so this uses only `/proc`,
  which is always present on any Linux system, with zero additional
  package requirements.
- **A host reboot is not a substitute for this fix, and users should
  not rely on "just reboot" going forward** -- this was the direct,
  concrete lesson from the report that prompted this fix: rebooting
  clears live processes but has no effect whatsoever on stale files
  already sitting on persistent storage under /etc.
