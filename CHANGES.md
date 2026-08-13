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
