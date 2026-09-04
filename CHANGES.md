# Change Tree -- Multi-Target Deploy Provider Rebuild

Baseline: the previous beta release archive (uploaded beta with cross-zone
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

## Change Tree -- Unified Installer: One Codebase, Two Install Profiles

Baseline: this same repo (Alpha release), as uploaded. Adds a
single-installer, dual-profile packaging layer on top of the existing
multi-target appliance -- `install.sh` at the repo root now chooses
between `single-instance` (today's existing single-tenant setup,
unchanged) and a new `msp-panos` profile (multi-tenant: one systemd
instance per customer, PAN-OS deploy targets only), from the SAME
checked-out code, with no forked repository to maintain.

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
                          already superseded by deploy_certificate.py and listed as removed
                          in this file's own prior entry, but was still physically present in
                          the uploaded Alpha release; deleted now.
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
    package installation (unchanged from before), copying the appliance
    source (plus install.sh/lib/) to /opt/acme-appliance, creating the
    Python venv (dependency installation itself moved to install.sh,
    since WHICH requirements file(s) get installed is profile-specific).
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

iso-build/README.md
  - Explains the bootstrap-appliance.sh -> install.sh handoff and how
    ks.cfg/appliance.pkr.hcl relate to profile selection.
  - "After first boot" now has separate walkthroughs per profile.
  - Known-gotchas list extended with the profile-split and System
    Updates disablement entries (short pointers back to the main README).
```

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
  DynamicUser.** An earlier draft of this design used `DynamicUser=true`
  for per-customer isolation, but `DynamicUser` combined with raw
  `ReadWritePaths=` on arbitrary `/etc` paths does NOT get automatic
  ownership management from systemd the way `StateDirectory=`/
  `ConfigurationDirectory=`/etc. do -- getting that combination genuinely
  correct requires directory creation to happen at systemd's hands, not
  a provisioning script's, which conflicts with wanting to pre-seed a
  starter `appliance.yaml` before first start. Rather than depend on
  subtle, systemd-version-sensitive behavior that couldn't be fully
  exercised in this environment (no real systemd here), `msp-panos`
  instead gives each customer instance its own plain `useradd --system`
  account (`acmecust-<slug>`), mirroring EXACTLY how the existing
  single-instance profile already establishes identity for its one
  `acme-appliance` account. This is more code-reviewable, matches a
  pattern already trusted elsewhere in this codebase, and sidesteps the
  ambiguity entirely.
- **System Updates required a real architectural decision, not just a
  config flag.** Reading `webui/system_updates.py` and
  `iso-build/sudoers.d/acme-appliance-updates` closely revealed that
  feature is inseparable from the single fixed `acme-appliance` account
  (the sudoers rule names that account explicitly) and requires
  `NoNewPrivileges` to stay unset for PAM/setuid access -- there is no
  safe way to offer "reboot the host" or "apply OS updates" per-tenant
  on a shared MSP fleet host. It is now excluded at both the
  application layer (404 on every route) and the OS-privilege layer
  (`lib/profile-msp-panos.sh` never installs that sudoers rule at all).
  A useful side effect: because msp-panos instances never need PAM/sudo
  access, their systemd units can set `NoNewPrivileges=true` -- strictly
  tighter sandboxing than the single-instance web UI unit is able to use.
- **Tested against the real uploaded codebase, not a reconstruction.**
  Every script here was syntax-checked (`bash -n`) and every Python file
  compiled (`python3 -m py_compile`) against the actual Alpha release
  files. `install.sh` was dry-run end-to-end for BOTH profiles with
  `systemctl`/`useradd`/`pip`/etc. stubbed and all paths redirected into
  a scratch root, confirming the full step sequence (venv setup, service
  account creation, directory creation, config seeding, TLS cert
  generation call, systemd unit installation, sudoers install, or the
  msp-panos equivalents) executes correctly and in the right order.
  `bin/msp-provision-customer.sh` was exercised for successful
  provisioning, invalid-slug rejection, overly-long-slug rejection,
  genuine duplicate-customer refusal (verified with the directory
  actually pre-existing, not just short-circuited by an earlier stub
  failure), and the msp-panos-only profile guard. `install.sh`'s own
  profile-switch guard (refusing to reprovision an existing host under a
  different profile) was verified directly. `webui/app.py`'s
  SYSTEM_UPDATES_ENABLED gating and `base.html`'s conditional nav link
  were verified by rendering the REAL templates (not stubs) with Jinja2
  in both states. The handful of failures surfaced during dry-run
  testing were all genuine sandbox limitations (no real root/systemd/
  useradd available here) rather than script logic bugs, and are called
  out explicitly rather than glossed over.
- **Known gap, called out rather than hidden:** `deploy_providers/iis.py`
  still has not been exercised against a real Windows/IIS host in any
  session so far (unchanged from the prior entry in this file) --
  validate against a lab IIS box before relying on it in production,
  regardless of which install profile you use it under.

