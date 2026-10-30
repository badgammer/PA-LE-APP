"""
Integration tests for the places alerts are actually fired from:

  * bin/acme-renew.sh        -- renewal_failed (runs the REAL script with a fake `certbot`)
  * deploy_certificate.py    -- renewal_success / deploy_success / deploy_failed
  * bin/redeploy-cert.sh     -- REAL script; a redeploy must not claim a certificate was renewed
  * the certbot hook commands use the appliance venv's Python

Mail goes to the same in-process SMTP server used by test_notifications.py.

    python -m pytest tests/test_alert_wiring.py
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
import time

import pytest
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
sys.path.insert(0, HERE)

import notifications as n  # noqa: E402
from test_notifications import FakeSMTP  # noqa: E402,F401

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")


def _copy_app(dest, with_tests=False):
    """
    Copy the app to `dest` with line endings normalised to LF. A Windows working copy checks the
    shell scripts out with CRLF (git converts them back on Linux), and bash cannot run those, so the
    tests always run against an LF copy rather than the working tree itself.
    """
    ignore = shutil.ignore_patterns(".git", ".vs", "__pycache__", "venv", "*.pyc", *(() if with_tests else ("tests",)))
    shutil.copytree(APP, dest, ignore=ignore)
    for root, _, files in os.walk(dest):
        for f in files:
            if f.endswith((".sh", ".py")):
                p = os.path.join(root, f)
                data = open(p, "rb").read()
                if b"\r\n" in data:
                    open(p, "wb").write(data.replace(b"\r\n", b"\n"))
    return str(dest)


@pytest.fixture(scope="session")
def app(tmp_path_factory):
    return _copy_app(tmp_path_factory.mktemp("app") / "acme-appliance")


@pytest.fixture
def smtpd(tmp_path):
    srv = FakeSMTP(tmp_path)
    yield srv
    srv.close()


def _wait_for(srv, count, timeout=5):
    end = time.time() + timeout
    while time.time() < end and len(srv.messages) < count:
        time.sleep(0.05)
    return srv.messages


def _subjects(srv):
    return [m[1]["Subject"] for m in srv.messages]


def _bodies(srv):
    return [m[1].get_payload() for m in srv.messages]


def make_config(root, srv, *, customer=None, events=None, domains=None, deploy_providers=None, email="ops@corp.test"):
    root.mkdir(parents=True, exist_ok=True)
    cfg_dir = root if customer is None else root / "customers" / customer
    cfg_dir.mkdir(parents=True, exist_ok=True)
    notif = n.default_settings()
    notif.update(enabled=True, transport="smtp", from_address="acme@appliance.test", recipients=["noc@x.com"])
    notif["smtp"].update(host="127.0.0.1", port=srv.port, security="none")
    notif["events"].update(events or {})
    cfg = {
        "acme": {"email": email, "server": "https://acme.invalid/dir"},
        "dns_providers": {"dns1": {"type": "generic_webhook", "settings": {"add_url": "x", "remove_url": "y"}}},
        "deploy_providers": deploy_providers or {},
        "domains": domains if domains is not None else [
            {"name": "vpn.example.com", "dns_provider": "dns1", "cert_name_prefix": "gp", "deploy_targets": []}],
        "notifications": notif,
    }
    path = cfg_dir / "appliance.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return str(path)


def fake_certbot(bindir, *, exit_code, output, args_file):
    """A `certbot` stand-in that records its arguments and prints something recognisable."""
    bindir.mkdir(exist_ok=True)
    p = bindir / "certbot"
    p.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        if [ "$1" = "--version" ]; then echo "certbot 3.0.0"; exit 0; fi
        printf '%s\\n' "$@" > {args_file}
        echo "{output}"
        exit {exit_code}
    """))
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


def run_renew(tmp_path, cfg_path, app, extra_env=None):
    env = dict(os.environ)
    env.update({
        "ACME_APPLIANCE_CONFIG": cfg_path,
        "ACME_APPLIANCE_LOG": str(tmp_path / "renew.log"),
        "ACME_APPLIANCE_LE_CONFIG_DIR": str(tmp_path / "le-config"),
        "ACME_APPLIANCE_LE_WORK_DIR": str(tmp_path / "le-work"),
        "ACME_APPLIANCE_LE_LOGS_DIR": str(tmp_path / "le-logs"),
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "TMPDIR": str(tmp_path),
    })
    env.update(extra_env or {})
    return subprocess.run([BASH, os.path.join(app, "bin", "acme-renew.sh")], env=env,
                          capture_output=True, text=True, timeout=60)


# --------------------------------------------------------------------- acme-renew.sh

def test_renew_failure_sends_alert_with_certbot_output(tmp_path, smtpd, app):
    cfg = make_config(tmp_path, smtpd, customer="acme-corp")
    fake_certbot(tmp_path / "bin", exit_code=1, output="Challenge failed for domain vpn.example.com",
                 args_file=tmp_path / "certbot-args.txt")
    r = run_renew(tmp_path, cfg, app)
    assert r.returncode == 1, r.stdout + r.stderr          # the script still fails exactly as before
    _wait_for(smtpd, 1)
    assert _subjects(smtpd) == ["[ACME] acme-corp: Renewal FAILED - vpn.example.com"]
    body = _bodies(smtpd)[0]
    assert "Challenge failed for domain vpn.example.com" in body and "Domain: vpn.example.com" in body


def test_renew_success_sends_nothing(tmp_path, smtpd, app):
    cfg = make_config(tmp_path, smtpd)
    fake_certbot(tmp_path / "bin", exit_code=0, output="Certificate not yet due for renewal",
                 args_file=tmp_path / "certbot-args.txt")
    r = run_renew(tmp_path, cfg, app)
    assert r.returncode == 0, r.stdout + r.stderr
    time.sleep(0.3)
    assert smtpd.messages == []      # "not due" must not email every night


def test_renew_failure_with_alerts_off_or_broken_still_just_fails(tmp_path, smtpd, app):
    cfg = make_config(tmp_path, smtpd, events={"renewal_failed": False})
    fake_certbot(tmp_path / "bin", exit_code=1, output="boom", args_file=tmp_path / "certbot-args.txt")
    assert run_renew(tmp_path, cfg, app).returncode == 1
    time.sleep(0.3)
    assert smtpd.messages == []
    smtpd.close()                                            # mail server gone: must not change the outcome
    cfg = make_config(tmp_path, smtpd)
    r = run_renew(tmp_path, cfg, app)
    assert r.returncode == 1


def test_renew_hooks_use_the_venv_python_when_present(tmp_path, smtpd):
    cfg = make_config(tmp_path, smtpd)
    args = tmp_path / "certbot-args.txt"
    fake_certbot(tmp_path / "bin", exit_code=0, output="ok", args_file=args)
    # Build a minimal copy of the app dir with a fake venv so the script picks it up.
    app = tmp_path / "app"
    _copy_app(app)
    (app / "venv" / "bin").mkdir(parents=True)
    # A wrapper rather than a symlink: a symlinked interpreter looks for pyvenv.cfg next to the link
    # and would lose the test environment's site-packages.
    fake_python = app / "venv" / "bin" / "python"
    fake_python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    fake_python.chmod(0o755)
    env = dict(os.environ, ACME_APPLIANCE_CONFIG=cfg, ACME_APPLIANCE_LOG=str(tmp_path / "r.log"),
               ACME_APPLIANCE_LE_CONFIG_DIR=str(tmp_path / "a"), ACME_APPLIANCE_LE_WORK_DIR=str(tmp_path / "b"),
               ACME_APPLIANCE_LE_LOGS_DIR=str(tmp_path / "c"), TMPDIR=str(tmp_path),
               PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    r = subprocess.run([BASH, str(app / "bin" / "acme-renew.sh")], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    argv = args.read_text().splitlines()
    venv_python = str(app / "venv" / "bin" / "python")
    hooks = {argv[i]: argv[i + 1] for i, a in enumerate(argv) if a in
             ("--manual-auth-hook", "--manual-cleanup-hook", "--deploy-hook")}
    assert hooks["--manual-auth-hook"] == f"{venv_python} {app}/dns_dispatcher.py add"
    assert hooks["--manual-cleanup-hook"] == f"{venv_python} {app}/dns_dispatcher.py remove"
    assert hooks["--deploy-hook"] == f"{venv_python} {app}/deploy_certificate.py"


def test_renew_hooks_fall_back_to_python3_without_a_venv(tmp_path, smtpd, app):
    cfg = make_config(tmp_path, smtpd)
    args = tmp_path / "certbot-args.txt"
    fake_certbot(tmp_path / "bin", exit_code=0, output="ok", args_file=args)
    assert run_renew(tmp_path, cfg, app).returncode == 0         # the shared copy has no venv/ directory
    argv = args.read_text().splitlines()
    assert f"python3 {app}/deploy_certificate.py" in argv


# ------------------------------------------------------------- deploy_certificate.py

DRIVER = textwrap.dedent("""
    import os, sys
    sys.path.insert(0, {app!r})
    import deploy_providers

    class FakeProvider:
        def __init__(self, settings): self.s = settings
        def deploy_certificate(self, cert_name, cert_path, key_path, target_entry, domain_name=None):
            mode = self.s.get("mode")
            if mode == "fail":  raise deploy_providers.DeployProviderError("commit rejected by firewall")
            if mode == "crash": raise RuntimeError("winrm exploded")
        def cleanup_old_certificates(self, prefix, keep_name): pass

    deploy_providers.get_provider = lambda t, s: FakeProvider(s)
    import deploy_certificate
    sys.exit(deploy_certificate.main())
""")


def run_deploy(tmp_path, cfg_path, app, *, redeploy=False, domains="vpn.example.com"):
    lineage = tmp_path / "live" / "vpn.example.com"
    lineage.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, ACME_APPLIANCE_CONFIG=cfg_path, ACME_APPLIANCE_LOG=str(tmp_path / "deploy.log"),
               RENEWED_LINEAGE=str(lineage), RENEWED_DOMAINS=domains)
    env.pop("ACME_APPLIANCE_REDEPLOY", None)
    if redeploy:
        env["ACME_APPLIANCE_REDEPLOY"] = "1"
    return subprocess.run([sys.executable, "-c", DRIVER.format(app=app)], env=env,
                          capture_output=True, text=True, timeout=60)


def _deploy_domain(*targets):
    return [{"name": "vpn.example.com", "dns_provider": "dns1", "cert_name_prefix": "gp",
             "deploy_targets": [{"target": t} for t in targets]}]


PROVIDERS = {
    "fw-ok": {"type": "panos", "settings": {"mode": "ok"}},
    "fw-bad": {"type": "panos", "settings": {"mode": "fail"}},
    "iis-crash": {"type": "panos", "settings": {"mode": "crash"}},
}


def test_deploy_success_and_renewal_success_alerts(tmp_path, smtpd, app):
    cfg = make_config(tmp_path, smtpd, deploy_providers=PROVIDERS, domains=_deploy_domain("fw-ok"),
                      events={"renewal_success": True, "deploy_success": True})
    r = run_deploy(tmp_path, cfg, app)
    assert r.returncode == 0, r.stdout + r.stderr
    _wait_for(smtpd, 2)
    assert sorted(_subjects(smtpd)) == ["[ACME] Certificate deployed - vpn.example.com",
                                        "[ACME] Certificate renewed - vpn.example.com"]
    assert any("Deployed to: fw-ok" in b for b in _bodies(smtpd))


def test_success_alerts_are_off_by_default(tmp_path, smtpd, app):
    cfg = make_config(tmp_path, smtpd, deploy_providers=PROVIDERS, domains=_deploy_domain("fw-ok"))
    assert run_deploy(tmp_path, cfg, app).returncode == 0
    time.sleep(0.3)
    assert smtpd.messages == []


def test_failed_target_alerts_and_other_targets_still_deploy(tmp_path, smtpd, app):
    cfg = make_config(tmp_path, smtpd, deploy_providers=PROVIDERS,
                      domains=_deploy_domain("fw-bad", "fw-ok"), events={"deploy_success": True})
    r = run_deploy(tmp_path, cfg, app)
    assert r.returncode == 1                                 # unchanged contract: non-zero if any target failed
    _wait_for(smtpd, 1)
    assert _subjects(smtpd) == ["[ACME] Deployment FAILED - vpn.example.com"]   # failure only, not a success mail
    body = _bodies(smtpd)[0]
    assert "- fw-bad: commit rejected by firewall" in body and "Deployed OK to: fw-ok" in body


def test_unexpected_provider_exception_is_isolated_and_reported(tmp_path, smtpd, app):
    cfg = make_config(tmp_path, smtpd, deploy_providers=PROVIDERS, domains=_deploy_domain("iis-crash", "fw-ok"))
    r = run_deploy(tmp_path, cfg, app)
    assert r.returncode == 1 and "Traceback" not in r.stderr   # no crash: handled like any failed target
    _wait_for(smtpd, 1)
    body = _bodies(smtpd)[0]
    assert "- iis-crash: RuntimeError: winrm exploded" in body and "Deployed OK to: fw-ok" in body


def test_unknown_target_instance_is_reported(tmp_path, smtpd, app):
    cfg = make_config(tmp_path, smtpd, deploy_providers=PROVIDERS, domains=_deploy_domain("nope"))
    assert run_deploy(tmp_path, cfg, app).returncode == 1
    _wait_for(smtpd, 1)
    assert "- nope: unknown deploy target instance" in _bodies(smtpd)[0]


def test_redeploy_is_not_reported_as_a_renewal(tmp_path, smtpd, app):
    cfg = make_config(tmp_path, smtpd, deploy_providers=PROVIDERS, domains=_deploy_domain("fw-ok"),
                      events={"renewal_success": True, "deploy_success": True})
    assert run_deploy(tmp_path, cfg, app, redeploy=True).returncode == 0
    _wait_for(smtpd, 1)
    time.sleep(0.3)
    assert _subjects(smtpd) == ["[ACME] Certificate deployed - vpn.example.com"]


def test_deploy_still_works_if_the_mail_server_is_down(tmp_path, smtpd, app):
    cfg = make_config(tmp_path, smtpd, deploy_providers=PROVIDERS, domains=_deploy_domain("fw-bad"))
    smtpd.close()
    r = run_deploy(tmp_path, cfg, app)
    assert r.returncode == 1 and "Traceback" not in r.stderr


# --------------------------------------------------------------- redeploy-cert.sh

def test_redeploy_script_sets_the_redeploy_flag(tmp_path, smtpd, app):
    # Real script + real deploy_certificate.py. The target instance does not exist, so the deploy
    # fails after the point where a "renewed" mail WOULD have been sent for a normal run.
    cfg = make_config(tmp_path, smtpd, domains=_deploy_domain("missing"), events={"renewal_success": True})
    lineage = tmp_path / "le-config" / "live" / "vpn.example.com"
    lineage.mkdir(parents=True)
    (lineage / "fullchain.pem").write_text("x")
    (lineage / "privkey.pem").write_text("x")
    env = dict(os.environ, ACME_APPLIANCE_CONFIG=cfg, ACME_APPLIANCE_LOG=str(tmp_path / "re.log"),
               ACME_APPLIANCE_LE_CONFIG_DIR=str(tmp_path / "le-config"))
    r = subprocess.run([BASH, os.path.join(app, "bin", "redeploy-cert.sh"), "vpn.example.com"],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1, r.stdout + r.stderr
    _wait_for(smtpd, 1)
    time.sleep(0.3)
    assert _subjects(smtpd) == ["[ACME] Deployment FAILED - vpn.example.com"]    # and no "Certificate renewed"
