"""Web UI browser login (#286 follow-up): cookie sessions for browsers.

With an access token configured, API clients keep using the
X-WebUI-Token header, but a plain browser cannot send it — so
POST /login trades the token for an in-memory session cookie that
authorises GETs. Config POSTs still need the header or the _csrf form
token: the cookie alone never writes. Everything runs offline against
a temp settings file, with the server on an ephemeral loopback port.
"""
import http.client
import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from print_handy import settings
from print_handy.webui import config as config_api
from print_handy.webui import server

TOKEN = "test-webui-token-12345"
WRONG = "wrong-token-attempt-999"
OTHER_SECRET = "test-discord-token-67890"
FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}

FAKE_SNAPSHOT = {
    "version": "0.test", "host": "testhost", "ok": True,
    "printer": {"model": "flashforge.ad5x", "name": "Flashforge AD5X",
                "vendor": "flashforge", "connection": "moonraker"},
    "bot": {"platform": "discord", "mode": "standalone", "ok": True,
            "reason": "connected", "heartbeat": {"connected": True}},
    "listener": {"required": True, "ok": True, "reason": "alive"},
    "spoolman": {"bundled": True, "url": "http://127.0.0.1:8000", "ok": True,
                 "detail": "HTTP 200"},
    "services": {"spoolman_url": "http://127.0.0.1:8000"},
    "doctor": [{"label": "settings", "status": "ok", "detail": "fine"}],
}


def hidden_inputs(body):
    return re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', body)


def session_cookie_pair(set_cookie):
    """The "name=value" pair a browser would send back from Set-Cookie."""
    return set_cookie.split(";", 1)[0].strip()


class LoginTests(unittest.TestCase):
    def start(self, tmp, extra="", environ=None):
        local = Path(tmp) / "local.yaml"
        local.write_text(f"paths:\n  hermes_home: {tmp}/hermes\n"
                         "webui:\n  enabled: true\n  port: 8765\n" + extra)
        environ = {"PRINT_HANDY_CONFIG": str(local), **(environ or {})}
        data = settings.load(local=local, environ=environ)
        httpd = server.make_server(data, environ=environ, port=0,
                                   snapshot_func=lambda: FAKE_SNAPSHOT)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        return httpd, httpd.server_address[1], local, environ

    def request(self, port, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return (response.status,
                    {name.lower(): value for name, value in response.getheaders()},
                    response.read().decode())
        finally:
            conn.close()

    def login(self, port, token=TOKEN):
        return self.request(port, "POST", "/login",
                            body=urlencode({"token": token}),
                            headers=FORM_HEADERS)

    def logged_in_cookie(self, port):
        code, headers, _body = self.login(port)
        self.assertEqual(code, 303)
        return session_cookie_pair(headers["set-cookie"])

    def test_repeated_wrong_logins_are_locked_out_even_for_the_right_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port, _local, _environ = self.start(
                tmp, environ={"WEBUI_ACCESS_TOKEN": TOKEN})
            for _ in range(5):
                code, _h, _b = self.login(port, token=WRONG)
                self.assertEqual(code, 401)
            code, headers, _b = self.login(port, token=WRONG)
            self.assertEqual(code, 429)
            self.assertGreater(int(headers["retry-after"]), 0)
            code, _h, _b = self.login(port)  # correct token, still locked
            self.assertEqual(code, 429)
            httpd.login_throttle._locked.clear()
            code, _h, _b = self.login(port)
            self.assertEqual(code, 303)

    # -- the login flow ------------------------------------------------------

    def test_browser_navigation_redirects_to_login(self):
        """#440: an unauthenticated browser (Accept: text/html) following
        the Unraid WebUI link gets the login page, not raw JSON 401."""
        with tempfile.TemporaryDirectory() as tmp:
            _httpd, port, _local, _environ = self.start(
                tmp, environ={"WEBUI_ACCESS_TOKEN": TOKEN})
            browser = {"Accept": "text/html,application/xhtml+xml"}
            for path in ("/", "/config", "/designs", "/layers"):
                with self.subTest(path=path):
                    code, headers, _body = self.request(port, "GET", path, headers=browser)
                    self.assertEqual(code, 303)
                    self.assertEqual(headers["location"], "/login")
            # API paths keep the JSON 401 contract, whatever Accept says.
            code, _headers, body = self.request(port, "GET", "/api/status", headers=browser)
            self.assertEqual(code, 401)
            self.assertIn("missing or wrong X-WebUI-Token", body)
            # A non-HTML client on a page path also keeps the JSON 401.
            code, _headers, body = self.request(
                port, "GET", "/", headers={"Accept": "application/json"})
            self.assertEqual(code, 401)
            self.assertIn("missing or wrong X-WebUI-Token", body)
            # With the header token, a browser navigation is let in: the
            # first one is offered the walkthrough once (#875), the next
            # serves the page.
            code, headers, _body = self.request(
                port, "GET", "/", headers={**browser, server.TOKEN_HEADER: TOKEN})
            self.assertEqual((code, headers["location"]), (303, "/walkthrough"))
            code, _headers, _body = self.request(
                port, "GET", "/", headers={**browser, server.TOKEN_HEADER: TOKEN})
            self.assertEqual(code, 200)

    def test_login_page_flow_and_cookie_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            _httpd, port, _local, _environ = self.start(
                tmp, environ={"WEBUI_ACCESS_TOKEN": TOKEN})
            # Unauthenticated as before.
            code, _headers, _body = self.request(port, "GET", "/")
            self.assertEqual(code, 401)
            # The login page is the entry point: reachable without auth,
            # and it never contains the token value.
            code, _headers, body = self.request(port, "GET", "/login")
            self.assertEqual(code, 200)
            self.assertIn('action="/login"', body)
            self.assertIn('name="token"', body)
            self.assertNotIn(TOKEN, body)
            # A wrong token: 401, a generic error, no cookie, and the
            # submitted value is not reflected back.
            code, headers, body = self.login(port, token=WRONG)
            self.assertEqual(code, 401)
            self.assertNotIn("set-cookie", headers)
            self.assertIn("not accepted", body)
            self.assertNotIn(WRONG, body)
            self.assertNotIn(TOKEN, body)
            # The right token: 303 to /, with an HttpOnly session cookie.
            code, headers, _body = self.login(port)
            self.assertEqual(code, 303)
            self.assertEqual(headers["location"], "/")
            set_cookie = headers["set-cookie"]
            self.assertIn(f"{server.SESSION_COOKIE}=", set_cookie)
            self.assertIn("HttpOnly", set_cookie)
            self.assertIn("SameSite=Strict", set_cookie)
            self.assertIn("Path=/", set_cookie)
            self.assertNotIn(TOKEN, set_cookie)
            cookie = session_cookie_pair(set_cookie)
            # The cookie authorises the GET surfaces.
            for path in ("/", "/config", "/api/status"):
                code, _headers, _body = self.request(
                    port, "GET", path, headers={"Cookie": cookie})
                self.assertEqual(code, 200, path)
            # Header auth keeps working alongside it.
            code, _headers, _body = self.request(
                port, "GET", "/", headers={server.TOKEN_HEADER: TOKEN})
            self.assertEqual(code, 200)

    def test_unknown_and_expired_cookies_are_unauthenticated(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port, _local, _environ = self.start(
                tmp, environ={"WEBUI_ACCESS_TOKEN": TOKEN})
            for bad in ("webui_session=garbage!!", "webui_session=",
                        f"webui_session={'x' * 43}", "other=1"):
                code, _headers, _body = self.request(
                    port, "GET", "/", headers={"Cookie": bad})
                self.assertEqual(code, 401, bad)
            cookie = self.logged_in_cookie(port)
            code, _headers, _body = self.request(
                port, "GET", "/", headers={"Cookie": cookie})
            self.assertEqual(code, 200)
            # Expire the session server-side: the same cookie dies.
            session_id = cookie.split("=", 1)[1]
            httpd.sessions[session_id] = time.time() - 1
            code, _headers, _body = self.request(
                port, "GET", "/", headers={"Cookie": cookie})
            self.assertEqual(code, 401)

    def test_logout_invalidates_the_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            _httpd, port, _local, _environ = self.start(
                tmp, environ={"WEBUI_ACCESS_TOKEN": TOKEN})
            cookie = self.logged_in_cookie(port)
            code, _headers, _body = self.request(
                port, "GET", "/", headers={"Cookie": cookie})
            self.assertEqual(code, 200)
            code, headers, _body = self.request(
                port, "POST", "/logout", body="", headers={"Cookie": cookie})
            self.assertEqual(code, 303)
            self.assertEqual(headers["location"], "/login")
            self.assertIn("Max-Age=0", headers["set-cookie"])
            code, _headers, _body = self.request(
                port, "GET", "/", headers={"Cookie": cookie})
            self.assertEqual(code, 401)

    def test_healthz_stays_unauthenticated(self):
        record = {"heartbeat_at": time.time(), "pid": os.getpid(),
                  "mode": "standalone", "connected": True,
                  "user": "printhandy#0001", "handlers": 12,
                  "hermes_modules": []}
        with tempfile.TemporaryDirectory() as tmp:
            _httpd, port, _local, _environ = self.start(
                tmp, environ={"WEBUI_ACCESS_TOKEN": TOKEN})
            with mock.patch.object(server.bot_health, "read", return_value=record):
                code, _headers, body = self.request(port, "GET", "/healthz")
            self.assertEqual(code, 200)
            self.assertNotIn(TOKEN, body)

    # -- the cookie is not a write credential ---------------------------------

    def browser_form(self, environ, csrf, **overrides):
        """The (name, value) pairs a browser would submit for the current
        effective settings, with per-field overrides applied."""
        form = []
        for item in config_api.read_fields(environ=environ):
            if item.env_overridden:
                continue
            value = item.value
            if item.field.kind == "toggle":
                if value:
                    form.append((item.id, "true"))
            elif isinstance(value, (list, tuple)):
                form += [(item.id, str(part)) for part in value]
            elif isinstance(value, float) and value == int(value):
                form.append((item.id, str(int(value))))
            else:
                form.append((item.id, "" if value is None else str(value)))
        for field_id, value in overrides.items():
            form = [(k, v) for k, v in form if k != field_id]
            if value is not None:
                form.append((field_id, value))
        form.append(("_csrf", csrf))
        return form

    def test_cookie_alone_cannot_post_but_cookie_plus_csrf_can(self):
        with tempfile.TemporaryDirectory() as tmp:
            _httpd, port, local, environ = self.start(
                tmp, "discord:\n  chat_id: '12345'\n",
                environ={"WEBUI_ACCESS_TOKEN": TOKEN})
            cookie = self.logged_in_cookie(port)
            auth = {"Cookie": cookie}
            # The cookie opens the config page and its embedded _csrf.
            code, _headers, body = self.request(port, "GET", "/config",
                                                headers=auth)
            self.assertEqual(code, 200)
            csrf = dict(hidden_inputs(body))["_csrf"]
            self.assertTrue(csrf)
            form = self.browser_form(environ, csrf, **{"discord.chat_id": "999"})
            bare = [(k, v) for k, v in form if k != "_csrf"]
            # Cookie but no _csrf: refused, nothing written.
            before = local.read_bytes()
            code, _headers, _body = self.request(
                port, "POST", "/config/preview", body=urlencode(bare),
                headers={**FORM_HEADERS, **auth})
            self.assertEqual(code, 403)
            self.assertEqual(local.read_bytes(), before)
            # Cookie + the page's _csrf: preview, then the confirm save.
            code, _headers, body = self.request(
                port, "POST", "/config/preview", body=urlencode(form),
                headers={**FORM_HEADERS, **auth})
            self.assertEqual(code, 200, body)
            code, _headers, body = self.request(
                port, "POST", "/config", body=urlencode(hidden_inputs(body)),
                headers={**FORM_HEADERS, **auth})
            self.assertEqual(code, 200, body)
            self.assertIn("discord.chat_id", body)
            self.assertIn("999", local.read_text())

    # -- no-token posture is unchanged -----------------------------------------

    def test_no_token_mode_login_redirects_and_pages_stay_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            _httpd, port, _local, _environ = self.start(tmp)
            code, headers, _body = self.request(port, "GET", "/login")
            self.assertEqual(code, 303)
            self.assertEqual(headers["location"], "/")
            code, headers, _body = self.login(port, token="anything")
            self.assertEqual(code, 303)
            self.assertEqual(headers["location"], "/")
            self.assertNotIn("set-cookie", headers)
            for path in ("/", "/config", "/api/status"):
                code, _headers, _body = self.request(port, "GET", path)
                self.assertEqual(code, 200, path)


if __name__ == "__main__":
    unittest.main()


class LoginThrottleTests(unittest.TestCase):
    """Failed-login backoff (promotion audit phase 3): unit level, no server."""

    def make(self):
        from print_handy.webui.access import LoginThrottle
        self.now = [0.0]
        return LoginThrottle(max_failures=3, window_s=100, lockout_s=30,
                             max_entries=4, clock=lambda: self.now[0])

    def test_locks_after_limit_then_expires(self):
        t = self.make()
        for _ in range(2):
            t.record_failure("a")
        self.assertEqual(t.retry_after("a"), 0)
        t.record_failure("a")
        self.assertGreater(t.retry_after("a"), 0)
        self.assertEqual(t.retry_after("b"), 0)  # other addresses unaffected
        self.now[0] = 31
        self.assertEqual(t.retry_after("a"), 0)

    def test_failures_outside_window_do_not_count(self):
        t = self.make()
        t.record_failure("a"); t.record_failure("a")
        self.now[0] = 200
        t.record_failure("a")
        self.assertEqual(t.retry_after("a"), 0)

    def test_success_clears_and_table_is_bounded(self):
        t = self.make()
        t.record_failure("a"); t.record_success("a")
        self.assertEqual(t._failures.get("a"), None)
        for i in range(50):
            t.record_failure(f"ip{i}")
        self.assertLessEqual(len(t._failures), 4)

