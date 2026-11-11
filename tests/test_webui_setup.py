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
import json
import os
import re
import socket
import stat
import sys
import tempfile
import threading
import time
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

    def sign_in(self, port, password=PASSWORD):
        code, headers, _ = self.request(port, "POST", "/login", headers=FORM,
                                        body=urlencode({"username": USERNAME,
                                                        "token": password}))
        self.assertEqual(code, 303)
        return headers["set-cookie"].split(";", 1)[0]

    def test_changing_the_password_ends_every_other_live_session(self):
        httpd, port, data = self.start()
        access.save_password(data, PASSWORD, PASSWORD, username=USERNAME)
        intruder = self.sign_in(port)
        owner = self.sign_in(port)
        for cookie in (intruder, owner):
            self.assertEqual(self.request(port, "GET", "/api/status",
                                          headers={"Cookie": cookie})[0], 200)
        walkthrough.save_state(data, {"step": 3})
        _, _, page = self.request(port, "GET", "/walkthrough",
                                  headers={"Cookie": owner})
        csrf = dict(re.findall(r'name="([^"]+)" value="([^"]*)"', page))[pages.CSRF_FIELD]
        new_password = "a different long passphrase"
        body = urlencode({"step": "login", "action": "continue", pages.CSRF_FIELD: csrf,
                          "username": USERNAME, "password": new_password,
                          "password_again": new_password, "current_password": PASSWORD})
        code, headers, _ = self.request(port, "POST", "/walkthrough", body=body,
                                        headers={**FORM, "Cookie": owner})
        self.assertEqual(code, 303)
        fresh = headers["set-cookie"].split(";", 1)[0]
        self.assertEqual(self.request(port, "GET", "/api/status",
                                      headers={"Cookie": intruder})[0], 401)
        self.assertEqual(self.request(port, "GET", "/api/status",
                                      headers={"Cookie": fresh})[0], 200)

    def test_two_simultaneous_first_claims_cannot_both_succeed(self):
        httpd, port, data = self.start()
        walkthrough.save_state(data, {"step": 3})
        token = httpd.form_token
        real_save = access.save_password
        barrier = threading.Barrier(2, timeout=5)

        def slow_save(*args, **kwargs):
            time.sleep(0.3)  # widen the window between the check and the write
            return real_save(*args, **kwargs)
        results = {}

        def claim(name):
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            results[name] = self.login_step(port, token, username=name, password=PASSWORD,
                                            password_again=PASSWORD)[0]
        with mock.patch.object(access, "save_password", slow_save):
            threads = [threading.Thread(target=claim, args=(name,))
                       for name in ("first-owner", "second-owner")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
        self.assertEqual(sorted(results.values()), [303, 403], results)
        winner = next(name for name, code in results.items() if code == 303)
        self.assertEqual(access.resolve(data, "0.0.0.0", {}).username, winner)

    def test_removing_a_claim_on_a_loopback_bind_turns_the_host_check_back_on(self):
        tmp = tempfile.mkdtemp()
        data = load(tmp)
        access.save_password(data, PASSWORD, PASSWORD, username=USERNAME)
        httpd = server.make_server(data, environ={}, host="127.0.0.1", port=0,
                                   snapshot_func=lambda: FAKE_SNAPSHOT)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        port = httpd.server_address[1]
        evil = {"Host": f"rebind.attacker.example:{port}", server.TOKEN_HEADER: PASSWORD}
        # Claimed: the password is the guard, any Host name is answered.
        self.assertEqual(self.request(port, "GET", "/api/status", headers=evil)[0], 200)
        access.access_path(data).unlink()  # the claim is removed
        self.assertEqual(self.request(port, "GET", "/api/status", headers=evil)[0], 403)
        self.assertEqual(self.request(port, "GET", "/api/status",
                                      headers={"Host": f"127.0.0.1:{port}"})[0], 200)

    def test_every_response_carries_the_security_headers(self):
        _httpd, port, _data = self.start()
        for method, route in (("GET", "/"), ("GET", "/api/status"), ("GET", "/healthz"),
                              ("GET", "/nope"), ("POST", "/nope")):
            _code, headers, _ = self.request(port, method, route,
                                             body="x=1" if method == "POST" else None,
                                             headers=FORM if method == "POST" else None)
            self.assertEqual(headers.get("x-content-type-options"), "nosniff", route)
            self.assertEqual(headers.get("referrer-policy"), "same-origin", route)
            self.assertEqual(headers.get("x-frame-options"), "DENY", route)
            self.assertIn("frame-ancestors 'none'",
                          headers.get("content-security-policy", ""), route)

    def test_a_500_never_echoes_the_exception(self):
        httpd, port, _data = self.start()

        def broken():
            raise RuntimeError("secret detail /home/someone/token=abc")
        httpd.snapshot_func = broken
        # /api/status reads through the SnapshotCache bound to the builder
        # at startup (api_snapshot_func), so break that seam too: replacing
        # snapshot_func alone no longer reaches the route.
        httpd.api_snapshot_func = broken
        with self.assertLogs("print_handy.webui.server", "ERROR") as logs:
            code, _, body = self.request(port, "GET", "/api/status")
        self.assertEqual(code, 500)
        self.assertNotIn("secret detail", body)
        self.assertNotIn("RuntimeError", body)
        self.assertIn("secret detail", "\n".join(logs.output))

    def test_a_500_names_a_request_id_that_the_log_line_carries(self):
        # #1243: one id per failed request, in the answer and in the log
        # line with the traceback, so a reported error can be found.
        httpd, port, _data = self.start()

        def broken(*_args, **_kwargs):
            raise RuntimeError("secret detail /home/someone/token=abc")
        httpd.snapshot_func = broken
        httpd.api_snapshot_func = broken
        ids = []
        with self.assertLogs("print_handy.webui.server", "ERROR") as logs:
            for _ in range(2):
                code, headers, body = self.request(port, "GET", "/api/status")
                self.assertEqual(code, 500)
                self.assertTrue(headers["content-type"].startswith("application/json"))
                payload = json.loads(body)
                self.assertEqual(payload["reason"], server.INTERNAL_ERROR_REASON)
                self.assertRegex(payload["request_id"], r"^[0-9a-f]{12}$")
                self.assertNotIn("secret detail", body)
                ids.append(payload["request_id"])
            # A browser page gets the themed refusal with the id on it.
            with mock.patch.object(server._Handler, "_chat_page", broken):
                code, _, page = self.request(port, "GET", "/chat", headers=HTML)
            self.assertEqual(code, 500)
            self.assertNotIn("secret detail", page)
            ids.append(re.search(r"Request id: ([0-9a-f]{12})", page).group(1))
            # A broken POST answers the same way.
            with mock.patch.object(server._Handler, "_debug_post", broken):
                code, _, body = self.request(port, "POST", "/debug", body="", headers=FORM)
            self.assertEqual(code, 500)
            ids.append(json.loads(body)["request_id"])
        self.assertEqual(len(set(ids)), len(ids))  # one id per failed request
        self.assertEqual(len(logs.records), len(ids))
        for rid, record in zip(ids, logs.records):
            line = record.getMessage()
            self.assertIn(f"request id {rid}", line)
            self.assertIsNotNone(record.exc_info)  # the traceback is logged

    def test_handlers_have_a_socket_timeout_and_a_thread_cap(self):
        httpd, port, _data = self.start()
        self.assertEqual(server._Handler.timeout, server.REQUEST_TIMEOUT_S)
        self.assertIsInstance(httpd, server._BoundedThreadingHTTPServer)
        # A connection that sends nothing holds one slot, not the server.
        idle = socket.create_connection(("127.0.0.1", port))
        self.addCleanup(idle.close)
        self.assertEqual(self.request(port, "GET", "/api/status")[0], 200)

    def test_every_page_links_to_the_source(self):
        # #1158: the link names the source tree at the running version,
        # so a network user gets the corresponding source, not just the
        # repository's current main.
        from print_handy import __version__
        from print_handy.webui import shell
        _httpd, port, _data = self.start()
        for route in ("/", "/config", "/walkthrough"):
            _code, _headers, body = self.request(port, "GET", route)
            self.assertIn(
                f'<a href="{shell.SOURCE_URL}/tree/{__version__}" rel="noopener">'
                "Source for this version</a>", body)
        bare = shell.page("upload", "<p>x</p>", chrome=False)
        self.assertIn(shell.SOURCE_URL, bare)

    def test_html_pages_are_never_cached(self):
        _httpd, port, _data = self.start()
        for route in ("/", "/config", "/walkthrough", "/upload"):
            _code, headers, _ = self.request(port, "GET", route)
            self.assertEqual(headers.get("cache-control"), "no-store", route)

    def test_every_open_reason_shows_a_banner_naming_it(self):
        httpd, port, data = self.start("  require_auth: false\n")
        access.save_password(data, PASSWORD, PASSWORD, username=USERNAME)
        httpd.access = access.resolve(data, "0.0.0.0", {})
        self.assertEqual(httpd.access.open_reason, access.OPEN_AUTH_OFF)
        self.assertTrue(httpd.access.credential_ignored)
        code, _, body = self.request(port, "GET", "/")
        self.assertEqual(code, 200)
        self.assertIn("This Web UI is open: login is turned off by", body)
        self.assertIn("require_auth = false", body)
        self.assertIn("A saved login or access token exists but is ignored", body)
        # The owner's saved leave-it-open choice: open, and the page says so.
        httpd2, port2, data2 = self.start()
        access.save_open(data2, True)
        httpd2.access = access.resolve(data2, "0.0.0.0", {})
        code, _, body = self.request(port2, "GET", "/")
        self.assertIn("This Web UI is open: a login was removed", body)
        self.assertNotIn("No login is set", body)

    def test_a_password_saved_under_require_auth_false_turns_auth_back_on(self):
        httpd, port, data = self.start("  require_auth: false\n")
        walkthrough.save_state(data, {"step": 3})
        code, _, body = self.request(port, "GET", "/walkthrough")
        self.assertIn("Saving a password here turns that setting back on", body)
        code, headers, _ = self.login_step(
            port, httpd.form_token, username=USERNAME, password=PASSWORD,
            password_again=PASSWORD)
        self.assertEqual(code, 303)
        reloaded = settings.load(local=Path(httpd.webui_environ["PRINT_HANDY_CONFIG"]),
                                 environ=httpd.webui_environ)
        self.assertIs(reloaded["webui"]["require_auth"], True)
        # Enforced now, not only after a restart.
        self.assertEqual(httpd.access.mode, access.PASSWORD)
        self.assertEqual(self.request(port, "GET", "/api/status")[0], 401)
        cookie = headers["set-cookie"].split(";", 1)[0]
        self.assertEqual(self.request(port, "GET", "/api/status",
                                      headers={"Cookie": cookie})[0], 200)

    def test_a_password_is_refused_when_require_auth_false_cannot_be_undone(self):
        httpd, port, data = self.start(
            "  require_auth: false\n",
            environ={"PRINT_HANDY__WEBUI__REQUIRE_AUTH": "false"})
        walkthrough.save_state(data, {"step": 3})
        code, _, body = self.login_step(
            port, httpd.form_token, username=USERNAME, password=PASSWORD,
            password_again=PASSWORD)
        self.assertEqual(code, 200)
        self.assertIn("The password was not saved", body)
        self.assertFalse(access.access_path(data).exists())

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
