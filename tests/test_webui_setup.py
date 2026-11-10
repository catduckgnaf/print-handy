"""Web UI access after the first-run gate reversal (was #708).

An install with no credential and no saved choice serves the full Web
UI openly and is *unclaimed* (print_handy.webui.access): the walkthrough
is offered from the first request, and a password exists only once one
is deliberately set — through the walkthrough's Login step, which
writes through the same access writers the retired /setup page used.
Every install that already has a credential or a saved choice keeps
its exact behavior, and the precedence is unchanged: a saved password
is primary, the environment token stays a co-credential, and a saved
"open" never overrides the token.

Offline: temp settings and state folder, the server on an ephemeral port.
"""
import http.client
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from print_handy import settings
from print_handy.webui import access, pages, server, walkthrough

TOKEN = "env-token-for-setup-tests-123"
PASSWORD = "correct horse battery"
USERNAME = "catduck"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}
HTML = {"Accept": "text/html"}


FAKE_SNAPSHOT = {
    "version": "0.test", "host": "testhost", "ok": True,
    "printer": {"model": "flashforge.ad5x", "name": "Flashforge AD5X",
                "vendor": "flashforge", "connection": "moonraker"},
    "bot": {"platform": "discord", "mode": "standalone", "ok": True,
            "reason": "connected", "heartbeat": {"connected": True}},
    "listener": {"required": True, "ok": True, "reason": "alive"},
    "spoolman": {"bundled": True, "url": "http://127.0.0.1:8000", "ok": True,
                 "detail": "HTTP 200"},
    "services": {},
    "doctor": [{"label": "settings", "status": "ok", "detail": "fine"}],
}


def load(tmp, extra=""):
    local = Path(tmp) / "local.yaml"
    local.write_text(f"paths:\n  hermes_home: {tmp}/hermes\n"
                     "webui:\n  enabled: true\n  host: 0.0.0.0\n  port: 8765\n" + extra)
    return settings.load(local=local, environ={})


class ResolveOrderTests(unittest.TestCase):
    """The precedence rules, first match wins; only the last rule changed
    (nothing set: open and unclaimed instead of a setup block)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = load(self.tmp)

    def test_lan_bind_with_nothing_chosen_is_open_and_unclaimed(self):
        resolved = access.resolve(self.data, "0.0.0.0", {})
        self.assertEqual(resolved.mode, access.OPEN)
        self.assertTrue(resolved.unclaimed)
        self.assertFalse(resolved.required)
        self.assertFalse(resolved.setup_pending)

    def test_loopback_with_nothing_chosen_is_open_and_unclaimed(self):
        resolved = access.resolve(self.data, "127.0.0.1", {})
        self.assertEqual(resolved.mode, access.OPEN)
        self.assertTrue(resolved.unclaimed)

    def test_a_saved_open_choice_is_open_and_not_unclaimed(self):
        # The saved "leave it open" record is the deliberate exit from the
        # unclaimed state: it dismisses the claim nudge.
        access.save_open(self.data, True)
        resolved = access.resolve(self.data, "0.0.0.0", {})
        self.assertEqual(resolved.mode, access.OPEN)
        self.assertFalse(resolved.unclaimed)

    def test_env_token_wins_over_a_saved_choice(self):
        access.save_open(self.data, True)
        resolved = access.resolve(self.data, "0.0.0.0", {"WEBUI_ACCESS_TOKEN": TOKEN})
        self.assertEqual(resolved.mode, access.TOKEN)
        self.assertFalse(resolved.unclaimed)
        self.assertTrue(resolved.matches(TOKEN))
        self.assertFalse(resolved.matches(TOKEN + "x"))

    def test_username_and_password_must_both_match(self):
        access.save_password(self.data, PASSWORD, PASSWORD, username=USERNAME)
        saved = access.access_path(self.data).read_text()
        self.assertIn(USERNAME, saved)
        self.assertNotIn(PASSWORD, saved)
        resolved = access.resolve(self.data, "0.0.0.0", {})
        self.assertEqual(resolved.username, USERNAME)
        self.assertFalse(resolved.unclaimed)
        self.assertTrue(resolved.matches_login(USERNAME, PASSWORD))
        self.assertTrue(resolved.matches_login(USERNAME.upper(), PASSWORD))
        self.assertFalse(resolved.matches_login("someone", PASSWORD))
        self.assertFalse(resolved.matches_login(USERNAME, PASSWORD + "x"))

    def test_a_bad_username_is_refused(self):
        for bad in ("", "   ", "with space", "a:b", "x" * 65):
            with self.subTest(username=bad), self.assertRaises(access.AccessError):
                access.save_password(self.data, PASSWORD, PASSWORD, username=bad)

    def test_a_password_saved_before_usernames_accepts_any_name(self):
        access.save_password(self.data, PASSWORD, PASSWORD)
        resolved = access.resolve(self.data, "0.0.0.0", {})
        self.assertTrue(resolved.matches_login("", PASSWORD))
        self.assertTrue(resolved.matches_login("anything", PASSWORD))

    def test_a_saved_login_wins_and_the_env_token_still_works(self):
        access.save_password(self.data, PASSWORD, PASSWORD, username=USERNAME)
        resolved = access.resolve(self.data, "0.0.0.0", {"WEBUI_ACCESS_TOKEN": TOKEN})
        self.assertEqual(resolved.mode, access.PASSWORD)
        self.assertTrue(resolved.matches_login(USERNAME, PASSWORD))
        self.assertTrue(resolved.matches(PASSWORD))
        self.assertTrue(resolved.matches(TOKEN))  # Home Assistant's header
        self.assertTrue(resolved.matches_login("", TOKEN))  # forgotten password
        self.assertFalse(resolved.matches(TOKEN + "x"))

    def test_require_auth_false_ignores_a_saved_login(self):
        access.save_password(self.data, PASSWORD, PASSWORD, username=USERNAME)
        data = load(self.tmp, "  require_auth: false\n")
        resolved = access.resolve(data, "0.0.0.0", {})
        self.assertEqual(resolved.mode, access.OPEN)
        self.assertFalse(resolved.unclaimed)

    def test_require_auth_false_is_open_and_ignores_a_token_as_before(self):
        data = load(self.tmp, "  require_auth: false\n")
        resolved = access.resolve(data, "0.0.0.0", {"WEBUI_ACCESS_TOKEN": TOKEN})
        self.assertEqual(resolved.mode, access.OPEN)
        self.assertFalse(resolved.required)

    def test_saved_password_is_required_and_hashed(self):
        access.save_password(self.data, PASSWORD, PASSWORD)
        path = access.access_path(self.data)
        self.assertNotIn(PASSWORD, path.read_text())
        if sys.platform != "win32":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        resolved = access.resolve(self.data, "0.0.0.0", {})
        self.assertEqual(resolved.mode, access.PASSWORD)
        self.assertTrue(resolved.matches(PASSWORD))
        self.assertTrue(resolved.matches(PASSWORD))  # the cached fast path agrees
        self.assertFalse(resolved.matches("wrong password"))
        self.assertFalse(resolved.matches(""))

    def test_saved_open_choice_is_open(self):
        access.save_open(self.data, True)
        self.assertEqual(access.resolve(self.data, "0.0.0.0", {}).mode, access.OPEN)

    def test_refused_choices(self):
        with self.assertRaises(access.AccessError):
            access.save_password(self.data, "short", "short")
        with self.assertRaises(access.AccessError):
            access.save_password(self.data, PASSWORD, PASSWORD + "!")
        with self.assertRaises(access.AccessError):
            access.save_open(self.data, False)
        self.assertFalse(access.access_path(self.data).exists())

    def test_a_damaged_saved_file_fails_closed_instead_of_reopening(self):
        """A file that exists but cannot be used is BROKEN, never
        "unclaimed": the owner chose a login, and nobody can tell what
        the damaged file held. Only a missing file is unclaimed."""
        path = access.access_path(self.data)
        path.parent.mkdir(parents=True, exist_ok=True)
        damaged = {"not json": "{not json", "not a dict": "[1, 2]",
                   "missing fields": '{"mode": "password", "salt": "AAAA"}',
                   "bad base64": '{"mode": "password", "salt": "!!", "hash": "!!", '
                                 '"iterations": 5}',
                   "unknown mode": '{"mode": "sideways"}', "empty": ""}
        for label, text in damaged.items():
            with self.subTest(label):
                path.write_text(text)
                for environ in ({}, {"WEBUI_ACCESS_TOKEN": TOKEN}):
                    resolved = access.resolve(self.data, "0.0.0.0", environ)
                    self.assertEqual(resolved.mode, access.BROKEN)
                    self.assertTrue(resolved.required)
                    self.assertFalse(resolved.unclaimed)
                    self.assertFalse(resolved.matches(TOKEN))
                    self.assertIn(access.ACCESS_FILE, resolved.problem)

    def test_an_unreadable_saved_file_fails_closed_and_says_why(self):
        path = access.access_path(self.data)
        access.save_password(self.data, PASSWORD, PASSWORD, username=USERNAME)
        real = Path.read_text

        def denied(self_path, *args, **kwargs):
            if self_path == path:
                raise PermissionError(13, "Permission denied")
            return real(self_path, *args, **kwargs)
        with mock.patch.object(Path, "read_text", denied):
            resolved = access.resolve(self.data, "0.0.0.0", {})
        self.assertEqual(resolved.mode, access.BROKEN)
        self.assertIn("permission denied", resolved.problem)
        path.unlink()
        resolved = access.resolve(self.data, "0.0.0.0", {})
        self.assertEqual((resolved.mode, resolved.unclaimed), (access.OPEN, True))


class UnclaimedFlowTests(unittest.TestCase):
    def start(self, extra="", environ=None):
        tmp = tempfile.mkdtemp()
        data = load(tmp, extra)
        environ = {"PRINT_HANDY_CONFIG": str(Path(tmp) / "local.yaml"), **(environ or {})}
        httpd = server.make_server(data, environ=environ, host="0.0.0.0", port=0,
                                   snapshot_func=lambda: FAKE_SNAPSHOT)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        return httpd, httpd.server_address[1], data

    def request(self, port, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return (response.status, {k.lower(): v for k, v in response.getheaders()},
                    response.read().decode())
        finally:
            conn.close()

    def login_step(self, port, csrf, **fields):
        body = urlencode({"step": "login", "action": "continue",
                          pages.CSRF_FIELD: csrf, **fields})
        return self.request(port, "POST", "/walkthrough", body=body, headers=FORM)

    def test_unclaimed_mode_serves_the_full_web_ui(self):
        httpd, port, data = self.start()
        self.assertTrue(httpd.access.unclaimed)
        # A browser landing on / is offered the walkthrough, not a gate.
        code, headers, _ = self.request(port, "GET", "/", headers=HTML)
        self.assertEqual((code, headers["location"]), (303, "/walkthrough"))
        # API and config routes serve normally; nothing answers 503.
        self.assertEqual(self.request(port, "GET", "/api/status")[0], 200)
        self.assertEqual(self.request(port, "GET", "/config")[0], 200)
        code, _, body = self.request(port, "GET", "/")
        self.assertEqual(code, 200)
        self.assertIn("No login is set", body)
        self.assertIn(self.request(port, "GET", "/healthz")[0], (200, 503))
        # The retired setup page is gone: GET redirects home, POST refuses.
        code, headers, _ = self.request(port, "GET", "/setup")
        self.assertEqual((code, headers["location"]), (303, "/"))
        code, _, _ = self.request(port, "POST", "/setup", body="x=1", headers=FORM)
        self.assertEqual(code, 405)
        # Browsing never creates a claim by itself.
        self.assertFalse(access.access_path(data).exists())

    def test_claiming_a_password_in_the_walkthrough_then_requires_it(self):
        httpd, port, data = self.start()
        walkthrough.save_state(data, {"step": 3})
        self.assertEqual(self.request(port, "GET", "/walkthrough")[0], 200)
        code, headers, _ = self.login_step(
            port, httpd.form_token, username=USERNAME, password=PASSWORD,
            password_again=PASSWORD)
        self.assertEqual((code, headers["location"]), (303, "/walkthrough"))
        cookie = headers["set-cookie"].split(";", 1)[0]
        self.assertEqual(httpd.access.mode, access.PASSWORD)
        self.assertFalse(httpd.access.unclaimed)
        self.assertEqual(self.request(port, "GET", "/api/status", headers={"Cookie": cookie})[0], 200)
        self.assertEqual(self.request(port, "GET", "/api/status")[0], 401)
        self.assertEqual(self.request(port, "GET", "/api/status",
                                      headers={server.TOKEN_HEADER: PASSWORD})[0], 200)
        code, _, body = self.request(port, "GET", "/login")
        self.assertIn("password chosen during setup", body)

    def test_login_form_asks_for_username_and_password(self):
        httpd, port, data = self.start()
        access.save_password(data, PASSWORD, PASSWORD, username=USERNAME)
        httpd.access = access.resolve(data, "0.0.0.0", {})
        _, _, body = self.request(port, "GET", "/login")
        self.assertIn('autocomplete="username"', body)
        self.assertIn('autocomplete="current-password"', body)
        for user, secret, expected in ((USERNAME, PASSWORD, 303), ("nobody", PASSWORD, 401),
                                       (USERNAME, "wrong password", 401)):
            with self.subTest(user=user, secret=secret):
                code, _, body = self.request(
                    port, "POST", "/login", headers=FORM,
                    body=urlencode({"username": user, "token": secret}))
                self.assertEqual(code, expected)
                self.assertNotIn(secret, body)

    def test_leaving_it_open_in_the_walkthrough_dismisses_the_nudge(self):
        httpd, port, data = self.start()
        walkthrough.save_state(data, {"step": 3})
        code, _, _ = self.login_step(port, httpd.form_token, confirm_open="yes")
        self.assertEqual(code, 303)
        self.assertEqual(httpd.access.mode, access.OPEN)
        self.assertFalse(httpd.access.unclaimed)
        code, _, body = self.request(port, "GET", "/")
        self.assertEqual(code, 200)
        self.assertNotIn("No login is set", body)

    def test_a_rebinding_host_is_refused_while_open_on_the_lan(self):
        # With no password, a site that rebinds its own name to this LAN
        # address is "same origin", so it could read page tokens and post
        # for the operator; the guard still refuses it while unclaimed.
        httpd, port, _ = self.start()
        evil = {"Host": f"rebind.attacker.example:{port}"}
        self.assertEqual(self.request(port, "GET", "/", headers=evil)[0], 403)
        # A cross-site post by IP is refused on its Origin.
        self.assertEqual(self.request(port, "POST", "/walkthrough", body="x=1", headers={
            **FORM, "Origin": "http://rebind.attacker.example"})[0], 403)
        self.assertTrue(httpd.access.unclaimed)
        for host in ("192.168.1.20", "tower", "unraid.local", "nas.home.arpa", "[fd00::5]"):
            self.assertEqual(self.request(port, "GET", "/api/status",
                                          headers={"Host": f"{host}:{port}"})[0], 200, host)
        self.assertNotEqual(self.request(port, "GET", "/healthz", headers=evil)[0], 403)

    def test_a_password_install_answers_to_any_host_name(self):
        # Behind a reverse proxy name the password, not the Host, is the guard.
        httpd, port, data = self.start()
        access.save_password(data, PASSWORD, PASSWORD, username=USERNAME)
        httpd.access = access.resolve(data, "0.0.0.0", {})
        self.assertEqual(self.request(port, "GET", "/api/status", headers={
            "Host": "print.example.com", server.TOKEN_HEADER: PASSWORD})[0], 200)

    def test_private_hostname(self):
        for name in ("192.168.1.2", "fd00::1", "tower", "unraid.local", "nas.lan", "x.home.arpa",
                     "box.internal", "tower."):
            self.assertTrue(server.private_hostname(name), name)
        for name in ("", "attacker.example", "evil.com", "local.evil.com", "lan.attacker.io"):
            self.assertFalse(server.private_hostname(name), name)

    def test_existing_token_install_is_never_unclaimed(self):
        httpd, port, _ = self.start(environ={"WEBUI_ACCESS_TOKEN": TOKEN})
        self.assertFalse(httpd.access.unclaimed)
        self.assertEqual(self.request(port, "GET", "/api/status")[0], 401)
        self.assertEqual(self.request(port, "GET", "/api/status",
                                      headers={server.TOKEN_HEADER: TOKEN})[0], 200)
        code, headers, _ = self.request(port, "GET", "/setup", headers={server.TOKEN_HEADER: TOKEN})
        self.assertEqual((code, headers.get("location")), (303, "/"))

    def test_a_claimed_install_with_a_damaged_login_file_refuses_service(self):
        httpd, port, data = self.start()
        access.save_password(data, PASSWORD, PASSWORD, username=USERNAME)
        path = access.access_path(data)
        path.write_text('{"mode": "password"')  # cut short
        with self.assertLogs("print_handy.webui.server", "ERROR") as logs:
            for method, route in (("GET", "/"), ("GET", "/api/status"), ("GET", "/login"),
                                  ("GET", "/walkthrough"), ("POST", "/walkthrough")):
                code, headers, body = self.request(port, method, route,
                                                   body="x=1" if method == "POST" else None,
                                                   headers=FORM if method == "POST" else HTML)
                self.assertEqual(code, 503, route)
                self.assertIn(access.ACCESS_FILE, body)
                self.assertIn("not valid JSON", body)
                self.assertTrue(headers["content-type"].startswith("text/plain"))
            # The header token does not open it either.
            self.assertEqual(self.request(port, "GET", "/api/status",
                                          headers={server.TOKEN_HEADER: PASSWORD})[0], 503)
        self.assertEqual(len(logs.records), 1)  # logged once, not per request
        # /healthz still answers its own JSON, so the container stays observable.
        _code, headers, body = self.request(port, "GET", "/healthz")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertNotIn(access.ACCESS_FILE, body)
        # Deleting the file is the documented reset: open and unclaimed again.
        path.unlink()
        self.assertEqual(self.request(port, "GET", "/api/status")[0], 200)
        self.assertTrue(httpd.access.unclaimed)

    def test_a_fixed_login_file_applies_without_a_restart(self):
        httpd, port, data = self.start()
        access.save_password(data, PASSWORD, PASSWORD, username=USERNAME)
        path = access.access_path(data)
        good = path.read_bytes()
        stamp = path.stat().st_mtime_ns
        path.write_text("garbage")
        self.assertEqual(self.request(port, "GET", "/api/status")[0], 503)
        # Restore the content with the old mtime, as a chmod/chown fix
        # would leave it: the server must still notice.
        path.write_bytes(good)
        os.utime(path, ns=(stamp, stamp))
        self.assertEqual(self.request(port, "GET", "/api/status")[0], 401)
        self.assertEqual(self.request(port, "GET", "/api/status",
                                      headers={server.TOKEN_HEADER: PASSWORD})[0], 200)

    def test_bearer_is_not_accepted(self):
        # X-WebUI-Token is the one header (Home Assistant sends it).
        _, port, _ = self.start(environ={"WEBUI_ACCESS_TOKEN": TOKEN})
        self.assertEqual(self.request(port, "GET", "/api/status",
                                      headers={"Authorization": f"Bearer {TOKEN}"})[0], 401)


if __name__ == "__main__":
    unittest.main()
