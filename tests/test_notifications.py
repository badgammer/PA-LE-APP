"""
Tests for notifications.py (email alerting) and the config_store regression fix.

No external network: SMTP talks to a tiny in-process server (plain, STARTTLS,
implicit TLS, AUTH PLAIN), Microsoft Graph is faked, and certificate expiry
uses real certificates generated with the openssl CLI.

    python -m pytest tests/test_notifications.py
"""
import base64
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from email import message_from_bytes

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notifications as n  # noqa: E402


# ------------------------------------------------------------------ form / validation

class Form(dict):
    """Like Flask's request.form for our purposes."""


def test_parse_recipients():
    ok, bad = n.parse_recipients("a@x.com, B@y.org;c@z.net\nA@X.com  not-an-email, d@@e.com")
    assert ok == ["a@x.com", "B@y.org", "c@z.net"]          # de-duplicated case-insensitively
    assert bad == ["not-an-email", "d@@e.com"]
    assert n.parse_recipients(["a@x.com", "a@x.com"]) == (["a@x.com"], [])
    assert n.parse_recipients("") == ([], [])


def test_settings_from_form_keeps_secrets_and_checkboxes():
    existing = n.default_settings()
    existing["smtp"].update(host="old", password="s3cret")
    existing["m365"]["client_secret"] = "graph-secret"
    form = Form(enabled="on", transport="m365", from_address=" alerts@contoso.com ", recipients="a@x.com, b@y.com",
                m365_tenant_id="t", m365_client_id="c", m365_client_secret="",  # blank -> keep stored
                smtp_host="mail.x.com", smtp_port="99999", smtp_security="bogus", smtp_password="",
                expiry_warn_days="0", event_cert_expiring="on", event_deploy_failed="on")
    s = n.settings_from_form(form, existing)
    assert s["enabled"] is True and s["transport"] == "m365"
    assert s["from_address"] == "alerts@contoso.com"
    assert s["recipients"] == ["a@x.com", "b@y.com"]
    assert s["m365"]["client_secret"] == "graph-secret"          # preserved
    assert s["smtp"]["password"] == "s3cret"                     # preserved
    assert s["smtp"]["port"] == 65535 and s["smtp"]["security"] == "starttls"   # clamped / defaulted
    assert s["expiry_warn_days"] == 1                            # clamped to >= 1
    assert s["events"]["cert_expiring"]
    assert not s["events"]["dns_failure"]                        # shown on the page + unchecked -> off
    # every event is wired now, so all of them come from the form: unchecked -> off
    assert s["events"]["deploy_failed"] is True and s["events"]["renewal_success"] is False   # ticked / unticked
    assert set(n.ui_events()) == set(n.EVENTS) and len(n.EVENTS) == 6
    # a NEW secret replaces the stored one
    s2 = n.settings_from_form(Form(m365_client_secret="new", transport="m365"), existing)
    assert s2["m365"]["client_secret"] == "new"
    # 'inherit' only when allowed
    assert n.settings_from_form(Form(transport="inherit"), existing)["transport"] == "smtp"
    assert n.settings_from_form(Form(transport="inherit"), existing, allow_inherit=True)["transport"] == "inherit"


def test_hidden_events_keep_their_saved_value(monkeypatch):
    # An event with wired=False is not shown on the page, so saving the page must not flip it.
    monkeypatch.setitem(n.EVENTS, "deploy_success", {**n.EVENTS["deploy_success"], "wired": False})
    existing = n.default_settings()
    existing["events"]["deploy_success"] = True
    s = n.settings_from_form(Form(event_cert_expiring="on"), existing)
    assert s["events"]["deploy_success"] is True
    assert "deploy_success" not in n.ui_events()


def test_validate():
    s = n.default_settings()
    assert n.validate(s) == []                                   # disabled + empty is fine
    s["enabled"] = True
    errs = n.validate(s)
    assert any("recipient" in e for e in errs) and any("SMTP server" in e for e in errs)
    s.update(recipients=["a@x.com", "oops"], from_address="alerts@x.com")
    s["smtp"]["host"] = "mail"
    assert n.validate(s) == ["Not valid email addresses: oops"]
    s["recipients"] = ["a@x.com"]
    s["smtp"]["username"] = "u"
    assert any("password" in e for e in n.validate(s))
    s["transport"] = "m365"
    assert len([e for e in n.validate(s) if "required" in e]) == 3
    s["transport"] = "inherit"
    assert n.validate(s) == []                                   # checked at send time instead


# ------------------------------------------------------------------ a tiny SMTP server

def _make_cert(tmp_path, name="smtp", days=30, san=None):
    key, crt = str(tmp_path / f"{name}.key"), str(tmp_path / f"{name}.crt")
    cmd = ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
           "-keyout", key, "-out", crt, "-days", str(days), "-subj", f"/CN={name}"]
    if san:
        cmd += ["-addext", "subjectAltName=" + ",".join(f"DNS:{d}" for d in san)]
    subprocess.run(cmd, check=True, capture_output=True)
    return key, crt


class FakeSMTP:
    def __init__(self, tmp_path, implicit_tls=False, user="u", password="p"):
        self.messages, self.logins, self.implicit_tls = [], [], implicit_tls
        self.user, self.password = user, password
        key, crt = _make_cert(tmp_path)
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(crt, key)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.stop = True
        self.sock.close()

    def _accept(self):
        while not self.stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            if self.implicit_tls:
                conn = self.ctx.wrap_socket(conn, server_side=True)
            f = conn.makefile("rwb", buffering=0)

            def say(line):
                f.write((line + "\r\n").encode())

            say("220 fake ESMTP")
            mail_from, rcpts, authed = None, [], False
            while True:
                line = f.readline().decode(errors="replace").strip()
                if not line:
                    return
                cmd = line.split(" ", 1)[0].upper()
                if cmd in ("EHLO", "HELO"):
                    f.write(b"250-fake\r\n250-AUTH PLAIN\r\n" + (b"250-STARTTLS\r\n" if not isinstance(conn, ssl.SSLSocket) else b"")
                            + b"250 8BITMIME\r\n")
                elif cmd == "STARTTLS":
                    say("220 go ahead")
                    conn = self.ctx.wrap_socket(conn, server_side=True)
                    f = conn.makefile("rwb", buffering=0)
                elif cmd == "AUTH":
                    parts = line.split()
                    _, user, pw = base64.b64decode(parts[2]).decode().split("\0")
                    if (user, pw) == (self.user, self.password):
                        authed = True
                        self.logins.append(user)
                        say("235 ok")
                    else:
                        say("535 5.7.8 bad credentials")
                elif cmd == "MAIL":
                    mail_from = line
                    say("250 ok")
                elif cmd == "RCPT":
                    rcpts.append(line.split(":", 1)[1].strip(" <>"))
                    say("250 ok")
                elif cmd == "DATA":
                    say("354 end with .")
                    data = b""
                    while True:
                        ln = f.readline()
                        if ln in (b".\r\n", b""):
                            break
                        data += ln[1:] if ln.startswith(b"..") else ln
                    self.messages.append((rcpts, message_from_bytes(data), authed))
                    rcpts = []
                    say("250 queued")
                elif cmd == "QUIT":
                    say("221 bye")
                    return
                else:
                    say("250 ok")
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def _smtp_settings(port, **smtp):
    s = n.default_settings()
    s.update(enabled=True, transport="smtp", from_address="alerts@appliance.test", from_name="ACME",
             recipients=["ops@x.com", "oncall@x.com"], subject_prefix="[ACME]")
    s["smtp"].update(host="127.0.0.1", port=port, security="none", username="", password="", verify_tls=False)
    s["smtp"].update(smtp)
    return s


@pytest.fixture
def smtpd(tmp_path):
    srv = FakeSMTP(tmp_path)
    yield srv
    srv.close()


def test_smtp_plain_send_and_headers(smtpd):
    s = _smtp_settings(smtpd.port)
    n.send_message(s, "Hello\r\nBcc: evil@x.com", "body text")      # header injection attempt
    (rcpts, msg, authed), = smtpd.messages
    assert rcpts == ["ops@x.com", "oncall@x.com"] and not authed
    assert msg["Subject"] == "Hello Bcc: evil@x.com" and msg["Bcc"] is None
    assert "ACME" in msg["From"] and "alerts@appliance.test" in msg["From"]
    assert msg.get_payload().strip() == "body text"


def test_smtp_starttls_and_login(smtpd):
    s = _smtp_settings(smtpd.port, security="starttls", username="u", password="p")
    assert "authenticated" in n.test_connection(s)
    n.send_message(s, "s", "b")
    assert smtpd.messages[-1][2] is True and smtpd.logins


def test_smtp_bad_password_is_clear(smtpd):
    s = _smtp_settings(smtpd.port, security="starttls", username="u", password="WRONG")
    with pytest.raises(n.NotificationError, match="login rejected"):
        n.test_connection(s)


def test_smtp_tls_verification_is_enforced(smtpd):
    s = _smtp_settings(smtpd.port, security="starttls", verify_tls=True)   # self-signed cert must be rejected
    with pytest.raises(n.NotificationError, match="Could not connect"):
        n.test_connection(s)


def test_smtp_implicit_tls(tmp_path):
    srv = FakeSMTP(tmp_path, implicit_tls=True)
    try:
        n.send_message(_smtp_settings(srv.port, security="ssl"), "s", "b")
        assert len(srv.messages) == 1
    finally:
        srv.close()


def test_smtp_unreachable_is_clear():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(n.NotificationError, match="Could not connect to SMTP server"):
        n.test_connection(_smtp_settings(port))


# ------------------------------------------------------------------ Microsoft 365 / Graph

def _jwt(roles):
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'none'})}.{b64({'roles': roles})}.sig"


class FakeGraph:
    def __init__(self, roles=("Mail.Send",), token_status=200, send_status=202, send_error=None):
        self.roles, self.token_status, self.send_status, self.send_error = roles, token_status, send_status, send_error
        self.calls = []

    def post(self, url, data=None, json=None, headers=None, timeout=None):
        self.calls.append((url, data, json, headers))
        r = type("R", (), {})()
        if "login.microsoftonline.com" in url:
            r.status_code, r.ok = self.token_status, self.token_status == 200
            body = ({"access_token": _jwt(list(self.roles))} if r.ok
                    else {"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret.\r\nTrace"})
        else:
            r.status_code, r.ok = self.send_status, self.send_status < 400
            body = self.send_error or {}
        r.json = lambda: body
        r.text = str(body)
        return r


def _m365_settings():
    s = n.default_settings()
    s.update(enabled=True, transport="m365", from_address="acme-alerts@contoso.com", recipients=["ops@x.com"])
    s["m365"].update(tenant_id="tenant-guid", client_id="client-guid", client_secret="shh")
    return s


def test_graph_send_request_shape(monkeypatch):
    g = FakeGraph()
    monkeypatch.setattr(n.requests, "post", g.post)
    n.send_message(_m365_settings(), "Subj", "Body", ["ops@x.com", "b@y.com"])
    (turl, tdata, _, _), (surl, _, payload, headers) = g.calls
    assert turl == "https://login.microsoftonline.com/tenant-guid/oauth2/v2.0/token"
    assert tdata["grant_type"] == "client_credentials" and tdata["scope"] == "https://graph.microsoft.com/.default"
    assert surl == "https://graph.microsoft.com/v1.0/users/acme-alerts@contoso.com/sendMail"
    assert headers["Authorization"].startswith("Bearer ")
    assert payload["saveToSentItems"] is False
    msg = payload["message"]
    assert msg["subject"] == "Subj" and msg["body"] == {"contentType": "Text", "content": "Body"}
    assert [r["emailAddress"]["address"] for r in msg["toRecipients"]] == ["ops@x.com", "b@y.com"]


def test_graph_bad_secret_message(monkeypatch):
    monkeypatch.setattr(n.requests, "post", FakeGraph(token_status=401).post)
    with pytest.raises(n.NotificationError, match="Invalid client secret"):
        n.test_connection(_m365_settings())


@pytest.mark.parametrize("status,code,expect", [
    (403, "ErrorAccessDenied", "not allowed to send as this mailbox"),
    (404, "ErrorInvalidUser", "was not found"),
    (400, "BadRequest", "BadRequest"),
])
def test_graph_send_errors(monkeypatch, status, code, expect):
    g = FakeGraph(send_status=status, send_error={"error": {"code": code, "message": "nope"}})
    monkeypatch.setattr(n.requests, "post", g.post)
    with pytest.raises(n.NotificationError, match=expect):
        n.send_message(_m365_settings(), "s", "b", ["ops@x.com"])


def test_graph_role_check_is_advisory_not_blocking(monkeypatch):
    # RBAC-for-Applications setups have NO Mail.Send role in the token; that must not block sending.
    monkeypatch.setattr(n.requests, "post", FakeGraph(roles=()).post)
    msg = n.test_connection(_m365_settings())
    assert "RBAC for Applications" in msg
    n.send_message(_m365_settings(), "s", "b", ["ops@x.com"])            # still sends
    monkeypatch.setattr(n.requests, "post", FakeGraph(roles=("Mail.Send",)).post)
    assert "RBAC" not in n.test_connection(_m365_settings())


# ------------------------------------------------------------------ MSP-wide sender (inherit)

def test_inherit_uses_fleet_sender(monkeypatch, tmp_path):
    fleet = tmp_path / "fleet.yaml"
    cust = n.default_settings()
    cust.update(enabled=True, transport="inherit", recipients=["cust@x.com"])
    with pytest.raises(n.NotificationError, match="none has been configured yet"):
        n.send_message(cust, "s", "b", fleet_path=str(fleet))           # no fleet file yet
    assert n.fleet_sender_summary(str(fleet)) is None
    fs = _m365_settings()
    n.save_fleet_settings(fs, str(fleet))
    assert oct(os.stat(fleet).st_mode & 0o777) == "0o600"
    assert "recipients" not in yaml.safe_load(fleet.read_text())         # sender only
    assert n.fleet_sender_summary(str(fleet)) == "Microsoft 365 (OAuth) as acme-alerts@contoso.com"
    g = FakeGraph()
    monkeypatch.setattr(n.requests, "post", g.post)
    n.send_message(cust, "s", "b", fleet_path=str(fleet))
    assert g.calls[-1][2]["message"]["toRecipients"][0]["emailAddress"]["address"] == "cust@x.com"
    assert "acme-alerts@contoso.com" in g.calls[-1][0]


# ------------------------------------------------------------------ events, rate limiting, never-raise

def _write_cfg(tmp_path, notif=None, domains=None):
    cfg = {"acme": {"email": "x@y.z"}, "dns_providers": {}, "deploy_providers": {}, "domains": domains or []}
    if notif is not None:
        cfg["notifications"] = notif
    p = tmp_path / "appliance.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return str(p)


@pytest.fixture
def sent(monkeypatch):
    box = []
    monkeypatch.setattr(n, "send_message", lambda s, subj, body, to, fleet_path=None: box.append((subj, body, list(to))))
    return box


def test_notify_event_disabled_or_missing_config_never_raises(tmp_path, sent):
    assert n.notify_event("dns_failure", "a.com", "x", config_path=str(tmp_path / "missing.yaml")) is False
    off = _write_cfg(tmp_path)                                           # no notifications block at all
    assert n.notify_event("dns_failure", "a.com", config_path=off) is False
    assert n.notify_event("not-an-event", config_path=off) is False
    assert sent == []


def test_notify_event_sends_and_rate_limits(tmp_path, sent):
    s = n.default_settings()
    s.update(enabled=True, recipients=["ops@x.com"], subject_prefix="[ACME]")
    s["smtp"]["host"], s["from_address"] = "h", "a@b.co"
    cfgp = _write_cfg(tmp_path, s)
    assert n.notify_event("dns_failure", "a.example.com", "TXT add failed", config_path=cfgp, label="acme-corp")
    subj, body, to = sent[0]
    assert subj == "[ACME] acme-corp: DNS challenge failed - a.example.com" and to == ["ops@x.com"]
    assert "TXT add failed" in body and "Domain: a.example.com" in body
    assert n.notify_event("dns_failure", "a.example.com", config_path=cfgp) is False          # within 6h -> suppressed
    assert n.notify_event("dns_failure", "b.example.com", config_path=cfgp) is True           # different domain is separate
    assert n.notify_event("renewal_success", "a.example.com", config_path=cfgp) is False      # event off by default
    # age the state so the limit has passed
    sp = tmp_path / "notification-state.json"
    st = json.loads(sp.read_text())
    st["dns_failure:a.example.com"] -= 7 * 3600
    sp.write_text(json.dumps(st))
    assert n.notify_event("dns_failure", "a.example.com", config_path=cfgp) is True


def test_failed_send_is_not_recorded_and_not_raised(tmp_path, monkeypatch):
    s = n.default_settings()
    s.update(enabled=True, recipients=["ops@x.com"])
    cfgp = _write_cfg(tmp_path, s)
    monkeypatch.setattr(n, "send_message", lambda *a, **k: (_ for _ in ()).throw(n.NotificationError("boom")))
    assert n.notify_event("deploy_failed", "a.com", config_path=cfgp) is False
    box = []
    monkeypatch.setattr(n, "send_message", lambda s_, subj, body, to, fleet_path=None: box.append(subj))
    assert n.notify_event("deploy_failed", "a.com", config_path=cfgp) is True     # retry NOT blocked by the failure
    assert box


def test_label_derivation(monkeypatch):
    monkeypatch.delenv("ACME_APPLIANCE_CUSTOMER", raising=False)
    assert n.label_for("/etc/acme-appliance/customers/acme-corp/appliance.yaml") == "acme-corp"
    assert n.label_for("/etc/acme-appliance/appliance.yaml") == ""
    monkeypatch.setenv("ACME_APPLIANCE_CUSTOMER", "from-env")
    assert n.label_for("/etc/acme-appliance/appliance.yaml") == "from-env"
    assert n.label_for("/x", "explicit") == "explicit"


# ------------------------------------------------------------------ certificate expiry scan

def _lineage(live, name, days, san):
    d = live / name
    d.mkdir(parents=True)
    key, crt = _make_cert(d, name="cert", days=days, san=san)
    os.rename(crt, d / "cert.pem")
    os.remove(key)


@pytest.fixture
def certs(tmp_path):
    live = tmp_path / "live"
    _lineage(live, "vpn.example.com", 5, ["vpn.example.com"])
    _lineage(live, "wild.example.org", 6, ["*.example.org", "example.org"])
    _lineage(live, "healthy.example.net", 60, ["healthy.example.net"])
    _lineage(live, "deleted.example.io", 2, ["deleted.example.io"])       # lineage for a domain no longer configured
    return live


DOMAINS = [{"name": "vpn.example.com"}, {"name": "*.example.org", "additional_names": ["example.org"]},
           {"name": "healthy.example.net"}]


def _notif_cfg(tmp_path, **over):
    s = n.default_settings()
    s.update(enabled=True, recipients=["ops@x.com"], expiry_warn_days=14)
    s.update(over)
    return _write_cfg(tmp_path, s, DOMAINS)


def test_scan_filters_to_configured_names(certs):
    names = n.configured_names({"domains": DOMAINS})
    assert names == {"vpn.example.com", "example.org", "healthy.example.net"}   # wildcard prefix normalised
    found = {c["lineage"]: c for c in n.scan_certificates(str(certs), names)}
    assert set(found) == {"vpn.example.com", "wild.example.org", "healthy.example.net"}   # stale lineage ignored
    assert 4.5 < found["vpn.example.com"]["days_left"] < 5.1
    assert "*.example.org" in found["wild.example.org"]["names"]
    assert len(n.scan_certificates(str(certs), None)) == 4                       # no filter -> everything


def test_check_expiry_digest_dedupe_and_escalation(tmp_path, certs, sent):
    cfgp = _notif_cfg(tmp_path)
    res = n.check_expiry(cfgp, str(certs), label="acme-corp")
    assert res["sent"] and res["scanned"] == 3
    assert [e["lineage"] for e in res["expiring"]] == ["vpn.example.com", "wild.example.org"]   # soonest first
    assert len(sent) == 1                                                        # ONE digest, not one per cert
    subj, body, _ = sent[0]
    assert subj.startswith("[ACME] acme-corp: 2 certificate(s) expiring soon (4d left)") or "d left" in subj
    assert "vpn.example.com" in body and "*.example.org (+1 more)" in body
    assert "deleted.example.io" not in body and "healthy.example.net" not in body

    res = n.check_expiry(cfgp, str(certs))                                       # immediately again
    assert not res["sent"] and res["skipped"] == "already alerted recently" and len(sent) == 1

    later = datetime.now(timezone.utc) + timedelta(days=3.5)                     # past the 72h repeat interval
    res = n.check_expiry(cfgp, str(certs), now=later)
    assert res["sent"] and len(sent) == 2
    # expired certs are reported as EXPIRED, with the 24h repeat once <= 3 days remain
    far = datetime.now(timezone.utc) + timedelta(days=30)
    res = n.check_expiry(cfgp, str(certs), now=far)
    assert res["sent"] and "EXPIRED" in sent[-1][1] and "EXPIRED" in sent[-1][0]


def test_check_expiry_respects_settings(tmp_path, certs, sent):
    off = _notif_cfg(tmp_path, enabled=False)
    res = n.check_expiry(off, str(certs))
    assert not res["sent"] and res["expiring"] and "disabled" in res["skipped"] and sent == []
    cfgp = _notif_cfg(tmp_path, expiry_warn_days=3)                              # nothing within 3 days
    assert n.check_expiry(cfgp, str(certs))["expiring"] == []
    dry = _notif_cfg(tmp_path)
    res = n.check_expiry(dry, str(certs), send=False)
    assert not res["sent"] and res["expiring"] and sent == []
    assert n.check_expiry(dry, str(tmp_path / "no-live-dir"))["scanned"] == 0     # missing dir is fine


def test_customer_paths(tmp_path):
    for slug in ("b-corp", "a-corp"):
        (tmp_path / slug).mkdir()
        (tmp_path / slug / "appliance.yaml").write_text("{}")
    (tmp_path / "stray").mkdir()
    got = list(n.customer_config_paths(str(tmp_path)))
    assert [g[0] for g in got] == ["a-corp", "b-corp"]
    assert got[0][2] == str(tmp_path / "a-corp" / "letsencrypt" / "live")


# ------------------------------------------------------------------ CLI

def test_cli_event_never_fails_the_caller(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ACME_APPLIANCE_LOG", str(tmp_path / "x.log"))
    rc = n.main(["event", "renewal_failed", "--domain", "a.com", "--detail", "boom",
                 "--config", str(tmp_path / "does-not-exist.yaml")])
    assert rc == 0 and "not sent" in capsys.readouterr().out


def test_cli_test_command_reports_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ACME_APPLIANCE_LOG", str(tmp_path / "x.log"))
    cfgp = _write_cfg(tmp_path, {"enabled": True, "recipients": []})
    assert n.main(["test", "--config", cfgp]) == 1
    assert "FAILED" in capsys.readouterr().err


def test_cli_check_expiry_dry_run(tmp_path, certs, capsys, monkeypatch):
    monkeypatch.setenv("ACME_APPLIANCE_LOG", str(tmp_path / "x.log"))
    cfgp = _notif_cfg(tmp_path)
    assert n.main(["check-expiry", "--config", cfgp, "--live-dir", str(certs), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "expiring 2" in out and "vpn.example.com" in out


# ------------------------------------------------------------------ regression: config_store

def test_config_store_in_use_helpers():
    from webui import config_store
    cfg = {"domains": [{"name": "a.com", "deploy_targets": [{"target": "fw1"}]}],
           "dns_providers": {"alias": {"type": "network_solutions", "settings": {"target_provider": "azure1"}}}}
    # Regression: this raised NameError('instance_name') after a bulk string replace hit the wrong function.
    assert config_store.deploy_provider_in_use(cfg, "fw1") == ["a.com"]
    assert config_store.deploy_provider_in_use(cfg, "unused") == []
    assert config_store.deploy_provider_in_use(cfg, "azure1") == []       # a DNS-alias target is not a deploy target
    assert config_store.dns_provider_in_use(cfg, "azure1") == ["DNS provider alias 'alias'"]
