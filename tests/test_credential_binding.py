"""#1192: a stored service key goes only to the host it was bound to.

Every client is pointed at a capturing HTTP listener on a loopback port and
checked for the Authorization header it sends. The keys are throwaway test
strings, never real ones.
"""
import http.client
import http.server
import json
import logging
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

import pytest

from print_handy import ai_routes, credential_binding, notify, settings
from print_handy.bot import agent, matrix_rest
from print_handy.core import design
from print_handy.webui import config as config_api
from print_handy.webui import pages as pages_module
from print_handy.webui import server

KEY = "test-stored-key-0000-not-real"
FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}


class Listener:
    """A loopback HTTP server that answers every request like a chat endpoint
    and records each request's Authorization header (None when absent)."""

    def __init__(self):
        seen = self.seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                seen.append(self.headers.get("Authorization"))
                body = json.dumps({"choices": [{"message": {"content": "ok"}}],
                                   "user_id": "@bot:test"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _answer

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path="/v1/chat/completions"):
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def listener():
    live = Listener()
    yield live
    live.close()


# ------------------------------------------------------------- the binding record

def test_first_use_binds_and_same_host_keeps_the_key():
    assert credential_binding.key_for("notify.ntfy_url", "https://ntfy.example/a", KEY) == KEY
    assert credential_binding.bound_host("notify.ntfy_url") == "ntfy.example"
    # A path or port change on the same host behaves as before.
    assert credential_binding.key_for("notify.ntfy_url", "https://NTFY.example:8443/b", KEY) == KEY


def test_new_host_gets_no_key_and_the_log_never_names_it(caplog):
    credential_binding.key_for("notify.ntfy_url", "https://ntfy.example/a", KEY)
    with caplog.at_level(logging.WARNING, logger="print_handy.credential_binding"):
        assert credential_binding.key_for("notify.ntfy_url", "https://other.example/a", KEY) == ""
        assert credential_binding.key_for("notify.ntfy_url", "https://other.example/b", KEY) == ""
    assert len(caplog.records) == 1  # logged once per setting and host
    assert "other.example" in caplog.text and "ntfy.example" in caplog.text
    assert KEY not in caplog.text


def test_https_binding_never_downgrades_to_plain_http(caplog):
    credential_binding.key_for("notify.ntfy_url", "https://ntfy.example/a", KEY)
    with caplog.at_level(logging.WARNING, logger="print_handy.credential_binding"):
        assert credential_binding.key_for("notify.ntfy_url", "http://ntfy.example/a", KEY) == ""
        assert credential_binding.key_for("notify.ntfy_url", "http://ntfy.example/b", KEY) == ""
    assert len(caplog.records) == 1
    assert "http://ntfy.example" in caplog.text and KEY not in caplog.text
    assert credential_binding.key_for("notify.ntfy_url", "https://ntfy.example:8443/a", KEY) == KEY
    credential_binding.rebind("notify.ntfy_url", "http://ntfy.example/a")
    assert credential_binding.key_for("notify.ntfy_url", "http://ntfy.example/a", KEY) == KEY


def test_http_binding_tightens_to_https_on_use():
    credential_binding.key_for("notify.ntfy_url", "http://ntfy.example/a", KEY)
    assert credential_binding.key_for("notify.ntfy_url", "https://ntfy.example/a", KEY) == KEY
    assert credential_binding.key_for("notify.ntfy_url", "http://ntfy.example/a", KEY) == ""


def test_pending_rebinds_refuses_a_scheme_downgrade():
    env = {"NTFY_ACCESS_TOKEN": KEY}
    old = {"notify.ntfy_url": "https://ntfy.example/a"}
    [move] = credential_binding.pending_rebinds(
        {"notify.ntfy_url": "http://ntfy.example/a"}, old, {}, env)
    assert (move.old_host, move.new_host) == ("https://ntfy.example", "http://ntfy.example")
    assert credential_binding.pending_rebinds(
        {"notify.ntfy_url": "https://ntfy.example/b"}, old, {}, env) == []


def test_rebind_sends_the_key_to_the_new_host():
    credential_binding.key_for("notify.ntfy_url", "https://ntfy.example/a", KEY)
    credential_binding.rebind("notify.ntfy_url", "https://other.example/a")
    assert credential_binding.key_for("notify.ntfy_url", "https://other.example/a", KEY) == KEY
    assert credential_binding.key_for("notify.ntfy_url", "https://ntfy.example/a", KEY) == ""


def test_keyless_use_binds_nothing():
    assert credential_binding.key_for("notify.ntfy_url", "https://ntfy.example/a", "") == ""
    assert credential_binding.bound_host("notify.ntfy_url") is None


def test_record_never_holds_a_key():
    credential_binding.key_for("llm.chat_url", "https://llm.example/v1", KEY)
    assert KEY not in credential_binding.record_path().read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_record_file_is_owner_only():
    credential_binding.key_for("llm.chat_url", "https://llm.example/v1", KEY)
    mode = stat.S_IMODE(os.stat(credential_binding.record_path()).st_mode)
    assert mode == 0o600


def test_host_fields_compare_by_host():
    assert credential_binding.host_of("192.168.1.50") == "192.168.1.50"
    assert credential_binding.host_of("http://Printer.Local.:7125/x") == "printer.local"
    assert credential_binding.host_of("") == ""


def test_pending_rebinds_rules():
    old = {"notify.ntfy_url": "https://ntfy.example/a"}
    env = {"NTFY_ACCESS_TOKEN": KEY}
    pend = credential_binding.pending_rebinds
    # Unbound with a key present: the current host counts as bound (first save after upgrade).
    [move] = pend({"notify.ntfy_url": "https://evil.example/a"}, old, {}, env)
    assert (move.old_host, move.new_host) == ("ntfy.example", "evil.example")
    assert KEY not in move.refusal
    assert pend({"notify.ntfy_url": "https://ntfy.example/b"}, old, {}, env) == []  # same host
    assert pend({"notify.ntfy_url": ""}, old, {}, env) == []  # cleared
    assert pend({"notify.ntfy_url": "https://evil.example/a"}, {}, {}, env) == []  # first address


def test_keyless_service_save_is_unchanged(monkeypatch):
    monkeypatch.delenv("NTFY_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr("print_handy.hermes._config.profile_env_value", lambda name, **kw: "")
    old = {"notify.ntfy_url": "https://ntfy.example/a"}
    assert credential_binding.pending_rebinds(
        {"notify.ntfy_url": "https://evil.example/a"}, old, {}, {}) == []


def test_design_inherits_the_chat_endpoint_binding():
    credential_binding.key_for("llm.chat_url", "https://llm.example/v1", KEY)
    old = {"llm.chat_url": "https://llm.example/v1", "design.chat_url": ""}
    [move] = credential_binding.pending_rebinds(
        {"design.chat_url": "https://evil.example/v1"}, old, {}, {})
    assert move.old_host == "llm.example"


# ---------------------------------------------------------------- each client

def _bind_elsewhere(setting):
    credential_binding.rebind(setting, "https://bound.example/")


def _rebind_here(setting, live):
    credential_binding.rebind(setting, live.url())


def test_ntfy_client_uses_the_binding(listener, monkeypatch):
    monkeypatch.setenv("NTFY_ACCESS_TOKEN", KEY)
    cfg = {"ntfy_url": listener.url("/topic")}
    _bind_elsewhere("notify.ntfy_url")
    assert notify._emit_ntfy(cfg, "complete", "done", http_opener=None)
    _rebind_here("notify.ntfy_url", listener)
    assert notify._emit_ntfy(cfg, "complete", "done", http_opener=None)
    assert listener.seen == [None, f"Bearer {KEY}"]


def test_mqtt_password_uses_the_binding(monkeypatch):
    monkeypatch.setenv("MQTT_PASSWORD", KEY)
    sent = []

    class Sock:
        def sendall(self, data):
            sent.append(data)

        def recv(self, n):
            return b"\x20\x02\x00\x00"

        def close(self):
            pass

    cfg = {"mqtt_host": "127.0.0.1", "mqtt_port": 1883, "mqtt_username": "u"}
    _bind_elsewhere("notify.mqtt_host")
    notify._emit_mqtt(cfg, "complete", "done", connector=lambda h, p: Sock())
    credential_binding.rebind("notify.mqtt_host", "127.0.0.1")
    notify._emit_mqtt(cfg, "complete", "done", connector=lambda h, p: Sock())
    assert KEY.encode() not in sent[0] and KEY.encode() in sent[1]


def test_ai_routes_uses_the_binding(listener, monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", KEY)
    cfg = {"llm": {"chat_url": listener.url(), "model": "m", "api_key_env": "TEST_LLM_KEY"}}
    _bind_elsewhere("llm.chat_url")
    ai_routes.call("chat", [{"role": "user", "content": "hi"}], cfg=cfg)
    _rebind_here("llm.chat_url", listener)
    ai_routes.call("chat", [{"role": "user", "content": "hi"}], cfg=cfg)
    assert listener.seen == [None, f"Bearer {KEY}"]


def test_ai_routes_tier_url_has_its_own_binding(listener, monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", KEY)
    cfg = {"ai": {"chat": {"chat_url": listener.url(), "model": "m",
                           "api_key_env": "TEST_LLM_KEY"}}}
    _bind_elsewhere("ai.chat.chat_url")
    ai_routes.call("chat", [{"role": "user", "content": "hi"}], cfg=cfg)
    assert listener.seen == [None]


def test_design_client_uses_the_binding(listener, monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", KEY)
    cfg = {"design": {"chat_url": listener.url(), "model": "m", "api_key_env": "TEST_LLM_KEY"}}
    _bind_elsewhere("design.chat_url")
    design.http_complete(cfg, [{"role": "user", "content": "hi"}])
    _rebind_here("design.chat_url", listener)
    design.http_complete(cfg, [{"role": "user", "content": "hi"}])
    assert listener.seen == [None, f"Bearer {KEY}"]


def test_design_inheriting_the_chat_url_uses_its_binding(listener, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", KEY)
    cfg = {"llm": {"chat_url": listener.url(), "model": "m"}}
    _bind_elsewhere("llm.chat_url")
    design.http_complete(cfg, [{"role": "user", "content": "hi"}])
    assert listener.seen == [None]


def test_agent_client_uses_the_binding(listener, monkeypatch):
    monkeypatch.setenv("TEST_AGENT_KEY", KEY)
    cfg = {"agent": {"chat_url": listener.url(), "api_key_env": "TEST_AGENT_KEY"}}
    _bind_elsewhere("agent.chat_url")
    agent.ask("hi", "s1", cfg=cfg)
    _rebind_here("agent.chat_url", listener)
    agent.ask("hi", "s1", cfg=cfg)
    assert listener.seen == [None, f"Bearer {KEY}"]


def test_matrix_client_uses_the_binding(listener):
    homeserver = listener.url("")
    _bind_elsewhere("matrix.homeserver_url")
    matrix_rest.whoami(homeserver, KEY)
    _rebind_here("matrix.homeserver_url", listener)
    matrix_rest.whoami(homeserver, KEY)
    assert listener.seen == [None, f"Bearer {KEY}"]


def test_vision_text_call_uses_the_binding(listener, monkeypatch):
    from print_handy.hermes import moonraker_event_listener as mel
    from print_handy.webui import spools
    monkeypatch.delenv("PRINT_VISION_URL", raising=False)
    monkeypatch.setattr(mel, "_vision_chat_url", listener.url)
    monkeypatch.setattr(mel, "_profile_env_value", lambda name: KEY)
    call = spools._default_ai_call()
    _bind_elsewhere("services.vision_chat_url")
    call(prompt="p", text="t")
    _rebind_here("services.vision_chat_url", listener)
    call(prompt="p", text="t")
    assert listener.seen == [None, f"Bearer {KEY}"]


def test_purchase_image_call_reaches_the_endpoint_with_its_key(listener, monkeypatch):
    # The image branch of the Web UI purchase-intake call hands visual_extract
    # an api_key that visual_watch calls; it must be a callable, not the key itself.
    from print_handy.hermes import moonraker_event_listener as mel
    from print_handy.webui import spools
    monkeypatch.delenv("PRINT_VISION_URL", raising=False)
    monkeypatch.setattr(mel, "_vision_chat_url", listener.url)
    monkeypatch.setattr(mel, "_profile_env_value", lambda name: KEY)
    assert spools._default_ai_call()(prompt="p", image=b"img") == "ok"
    assert listener.seen == [f"Bearer {KEY}"]


def test_vision_image_route_uses_the_binding(listener, monkeypatch):
    from print_handy.hermes import visual_watch
    monkeypatch.delenv("PRINT_VISION_URL", raising=False)
    _bind_elsewhere("services.vision_chat_url")
    visual_watch._vision_answer(b"img", "p", chat_url=listener.url, model="m",
                                api_key=lambda: KEY, context="")
    assert listener.seen == [None]


# --------------------------------------------------------------- the Web UI save

class WebUISaveTests(unittest.TestCase):
    """The settings save refuses a host change for a bound key unless confirmed."""

    def start(self, tmp, environ):
        local = Path(tmp) / "local.yaml"
        local.write_text(f"paths:\n  hermes_home: {tmp}/hermes\n"
                         "webui:\n  enabled: true\n  port: 8765\n"
                         "notify:\n  ntfy_url: https://ntfy.example/topic\n")
        environ = {"PRINT_HANDY_CONFIG": str(local), **environ}
        data = settings.load(local=local, environ=environ)
        httpd = server.make_server(data, environ=environ, port=0,
                                   snapshot_func=lambda: {"ok": True})
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        return httpd.server_address[1], local, environ

    def request(self, port, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read().decode()
        finally:
            conn.close()

    def csrf(self, port):
        import re
        body = self.request(port, "GET", "/config")[1]
        return re.search(r'name="_csrf" value="([^"]*)"', body).group(1)

    def preview(self, port, environ, url):
        import re
        form = []
        for item in config_api.read_fields(environ=environ):
            value = item.value
            if item.env_overridden:
                continue
            if item.field.kind == "toggle":
                if value:
                    form.append((item.id, "true"))
            elif isinstance(value, (list, tuple)):
                form += [(item.id, str(part)) for part in value]
            elif isinstance(value, float) and value == int(value):
                form.append((item.id, str(int(value))))
            else:
                form.append((item.id, "" if value is None else str(value)))
        form = [(k, v) for k, v in form if k != "notify.ntfy_url"] + [("notify.ntfy_url", url)]
        form.append(("_csrf", self.csrf(port)))
        code, body = self.request(port, "POST", "/config/preview", urlencode(form), FORM_HEADERS)
        self.assertEqual(code, 200, body)
        return body, re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', body)

    def test_host_change_is_refused_then_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            port, local, environ = self.start(tmp, {"NTFY_ACCESS_TOKEN": KEY})
            before = local.read_bytes()
            body, hidden = self.preview(port, environ, "https://evil.example/topic")
            self.assertIn("Send my ntfy key to evil.example", body)
            self.assertNotIn(KEY, body)
            code, body = self.request(port, "POST", "/config", urlencode(hidden), FORM_HEADERS)
            self.assertEqual(code, 400)
            self.assertIn("bound to ntfy.example", body)
            self.assertNotIn(KEY, body)
            self.assertEqual(local.read_bytes(), before)
            self.assertIsNone(credential_binding.bound_host("notify.ntfy_url"))
            confirmed = hidden + [(pages_module.REBIND_FIELD, "notify.ntfy_url")]
            code, body = self.request(port, "POST", "/config", urlencode(confirmed), FORM_HEADERS)
            self.assertEqual(code, 200, body)
            self.assertEqual(settings.load(local=local, environ={})["notify"]["ntfy_url"],
                             "https://evil.example/topic")
            self.assertEqual(credential_binding.bound_host("notify.ntfy_url"), "evil.example")

    def test_https_to_http_on_the_same_host_needs_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            port, local, environ = self.start(tmp, {"NTFY_ACCESS_TOKEN": KEY})
            before = local.read_bytes()
            body, hidden = self.preview(port, environ, "http://ntfy.example/topic")
            self.assertIn("Send my ntfy key to http://ntfy.example", body)
            code, body = self.request(port, "POST", "/config", urlencode(hidden), FORM_HEADERS)
            self.assertEqual(code, 400)
            self.assertNotIn(KEY, body)
            self.assertEqual(local.read_bytes(), before)

    def test_same_host_path_change_needs_no_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            port, local, environ = self.start(tmp, {"NTFY_ACCESS_TOKEN": KEY})
            body, hidden = self.preview(port, environ, "https://ntfy.example/other")
            self.assertNotIn("Send my ntfy key", body)
            code, body = self.request(port, "POST", "/config", urlencode(hidden), FORM_HEADERS)
            self.assertEqual(code, 200, body)

    def test_write_api_refuses_without_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "local.yaml"
            local.write_text(f"paths:\n  hermes_home: {tmp}/hermes\n"
                             "notify:\n  ntfy_url: https://ntfy.example/topic\n")
            credential_binding.rebind("notify.ntfy_url", "https://ntfy.example/")
            environ = {"PRINT_HANDY_CONFIG": str(local)}
            with self.assertRaisesRegex(config_api.WebUIConfigError, "bound to ntfy.example"):
                config_api.write_changes({"notify.ntfy_url": "https://evil.example/t"},
                                         environ=environ)
            config_api.write_changes({"notify.ntfy_url": "https://evil.example/t"},
                                     environ=environ, rebind={"notify.ntfy_url"})
            self.assertEqual(credential_binding.bound_host("notify.ntfy_url"), "evil.example")
