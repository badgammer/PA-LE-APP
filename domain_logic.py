"""
Shared, Flask-app-independent business logic for turning appliance.yaml
domain/DNS-provider/deploy-provider data into web-UI-ready structures,
and for parsing web forms back into appliance.yaml entries.

Extracted verbatim (same logic, same behavior) from webui/app.py so it
can be reused by BOTH:
  - webui/app.py (single-instance profile) -- operating against its one
    fixed appliance.yaml, exactly as before this extraction.
  - msp_console/ (msp-panos profile) -- operating against MANY different
    customers' appliance.yaml files from within the same process.

Every function here is a pure function: it takes whatever config dict
and/or form data it needs as explicit parameters, and never reaches into
a module-global Flask `request` or a fixed file path. This is what makes
it safe to share between two different Flask apps (different modules,
different `app` objects, potentially different customers' data in the
same process) without either one being able to accidentally see or
mutate the other's state.
"""
import json
import os
import subprocess

from cert_naming import safe_cert_name


def target_summary(target_entry: dict, provider_type: str) -> str:
    """
    Human-readable one-liner for a single deploy_targets[] row, used on
    the Domains list page -- each deploy target TYPE has its own
    type-specific fields (see deploy_providers.TARGET_FIELDS), so this
    knows how to summarize the ones this appliance ships with and falls
    back to a generic dump of the entry for anything else.
    """
    if provider_type == "panos":
        kind = "GlobalProtect portal" if target_entry.get("cert_field_type") == "globalprotect_portal" else "SSL/TLS profile"
        vsys = f" (vsys={target_entry['vsys']})" if target_entry.get("vsys") else ""
        return f"{kind}: {target_entry.get('cert_field_value', '')}{vsys}"
    if provider_type == "iis":
        ip = target_entry.get("binding_ip") or "*"
        port = target_entry.get("binding_port") or 443
        host = f", SNI={target_entry['hostname']}" if target_entry.get("hostname") else ""
        return f"IIS site '{target_entry.get('site_name', '')}' ({ip}:{port}{host})"
    return ", ".join(f"{k}={v}" for k, v in target_entry.items() if k != "target")


def deploy_targets_display(entry: dict, deploy_providers: dict) -> list:
    result = []
    for t in entry.get("deploy_targets", []) or []:
        instance = deploy_providers.get(t.get("target"), {})
        result.append({
            "target": t.get("target"),
            "type": instance.get("type", "unknown"),
            "summary": target_summary(t, instance.get("type", "")),
        })
    return result


def additional_names_display(entry: dict) -> list:
    """
    Returns [{"name": ..., "override": provider_name_or_empty}] for
    display on the Domains list page -- override is non-empty only when
    that specific name uses a DIFFERENT dns_provider than the entry's
    own default (i.e. a genuine per-name override), so the UI can show
    a small badge only where it's actually meaningful.
    """
    default_provider = entry.get("dns_provider")
    result = []
    for item in entry.get("additional_names", []) or []:
        if isinstance(item, dict):
            name = item["name"]
            override = item.get("dns_provider") or ""
            if override == default_provider:
                override = ""
        else:
            name = item
            override = ""
        result.append({"name": name, "override": override})
    return result


def additional_names_for_form(entry: dict) -> list:
    """
    Returns [{"name": ..., "provider_override": provider_or_empty}] for
    populating the Edit Domain form's repeatable "Additional names"
    rows. Unlike additional_names_display (used on the Domains list),
    this deliberately keeps provider_override EMPTY (not resolved to the
    entry's default) whenever the stored entry didn't specify one, so
    the form's dropdown correctly pre-selects "(same as primary)"
    rather than appearing to explicitly re-select the default provider.
    """
    result = []
    for item in entry.get("additional_names", []) or []:
        if isinstance(item, dict):
            result.append({"name": item["name"], "provider_override": item.get("dns_provider") or ""})
        else:
            result.append({"name": item, "provider_override": ""})
    return result


def cert_lineage_dir(domain_name: str, live_dir: str):
    for candidate in (safe_cert_name(domain_name), domain_name):
        path = os.path.join(live_dir, candidate)
        if os.path.isdir(path):
            return path
    return None


def cert_expiry(domain_name: str, live_dir: str):
    lineage_dir = cert_lineage_dir(domain_name, live_dir)
    if not lineage_dir:
        return None
    cert_path = os.path.join(lineage_dir, "cert.pem")
    if not os.path.exists(cert_path):
        return None
    try:
        out = subprocess.check_output(
            ["openssl", "x509", "-enddate", "-noout", "-in", cert_path],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode()
        return out.strip().replace("notAfter=", "")
    except Exception:  # noqa: BLE001
        return None


def domain_from_form(cfg: dict, form, target_fields_schema: dict):
    """
    Parses a submitted Domain form into (name, entry, error) -- error is
    None on success. `form` must support .get(key, default) and
    .getlist(key) exactly like Flask's request.form (an
    ImmutableMultiDict) -- both webui/app.py and msp_console pass their
    own request.form here directly. `target_fields_schema` is
    deploy_providers.TARGET_FIELDS (identical for both profiles, just
    threaded through explicitly rather than imported as a module
    global, to keep this function fully self-contained).
    """
    name = form.get("name", "").strip()
    dns_provider = form.get("dns_provider", "").strip()
    cert_name_prefix = form.get("cert_name_prefix", "").strip() or "gp-portal-cert"

    if not name:
        return None, None, "Domain name is required."
    if dns_provider not in cfg["dns_providers"]:
        return None, None, "Select a valid primary DNS provider."

    # Additional names (SANs): each row is a name + an optional per-name
    # DNS provider override. An override is only stored (as a {"name":
    # ..., "dns_provider": ...} dict) when it differs from the entry's
    # own primary dns_provider -- otherwise the name is stored as a
    # plain string, keeping the YAML clean and fully backward-compatible
    # with configs written before this feature existed.
    additional_names = []
    an_names = form.getlist("additional_name[]")
    an_providers = form.getlist("additional_name_provider[]")
    for an_name, an_provider in zip(an_names, an_providers):
        an_name = an_name.strip()
        if not an_name:
            continue
        if an_provider and an_provider != dns_provider:
            if an_provider not in cfg["dns_providers"]:
                return None, None, f"Unknown DNS provider override '{an_provider}' for additional name '{an_name}'."
            additional_names.append({"name": an_name, "dns_provider": an_provider})
        else:
            additional_names.append(an_name)

    # Deploy targets: each row is a reference to a named deploy_providers[]
    # instance (any type -- PAN-OS firewall, IIS server, etc.) plus a JSON
    # blob of that type's own fields (e.g. ssl_tls_profile/vsys for panos,
    # site_name/binding_ip/binding_port/hostname for iis). The JSON blob is
    # assembled client-side by domain_form.html's JS right before submit,
    # from whichever type-specific subform is currently rendered for that
    # row -- this lets ONE generic form support any number of deploy
    # target types without a fixed set of parallel array fields per type.
    deploy_targets = []
    target_instances = form.getlist("target_instance[]")
    target_configs = form.getlist("target_config[]")
    for instance_name, config_json in zip(target_instances, target_configs):
        if not instance_name:
            continue
        if instance_name not in cfg["deploy_providers"]:
            return None, None, f"Unknown deploy target '{instance_name}' in target list."
        try:
            fields = json.loads(config_json) if config_json else {}
            if not isinstance(fields, dict):
                raise ValueError("not an object")
        except (TypeError, ValueError):
            return None, None, f"Invalid target configuration submitted for '{instance_name}'."
        provider_type = cfg["deploy_providers"][instance_name]["type"]
        schema = target_fields_schema.get(provider_type, {"fields": []})
        for field in schema["fields"]:
            if field.get("required") and not fields.get(field["name"]):
                return None, None, (
                    f"'{field['label']}' is required for deploy target '{instance_name}' "
                    f"({provider_type})."
                )
        target = {"target": instance_name}
        target.update(fields)
        deploy_targets.append(target)

    if not deploy_targets:
        return None, None, "At least one deploy target is required."

    entry = {
        "name": name,
        "dns_provider": dns_provider,
        "cert_name_prefix": cert_name_prefix,
        "deploy_targets": deploy_targets,
    }
    if additional_names:
        entry["additional_names"] = additional_names
    return name, entry, None


def settings_from_form(fields_schema: dict, provider_type: str, existing_settings: dict, form) -> dict:
    """
    Generic "read an instance's connection settings out of a submitted
    form" helper -- used for BOTH dns_providers (fields_schema=
    PROVIDER_FIELDS) and deploy_providers (fields_schema=INSTANCE_FIELDS)
    instance forms, since both follow the exact same {label, name, type,
    secret, default, required} field-schema shape. `form` must support
    .get(key, default) exactly like Flask's request.form.
    """
    settings = dict(existing_settings)
    for field in fields_schema[provider_type]["fields"]:
        fname = field["name"]
        if field.get("type") == "checkbox":
            settings[fname] = form.get(fname) == "on"
            continue
        value = form.get(fname, "")
        if field.get("secret") and not value:
            continue
        if value == "" and "default" in field:
            settings[fname] = field["default"]
        elif field.get("type") == "number":
            settings[fname] = int(value) if value else field.get("default", 0)
        else:
            settings[fname] = value
    return settings
