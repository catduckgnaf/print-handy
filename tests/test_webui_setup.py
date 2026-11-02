"""#708 first-run setup: the Web UI on a LAN bind asks for a password or an
explicit "leave it open" before it serves anything, and every install that
already works keeps its exact behavior (print_handy.webui.access).

Offline: temp settings and state folder, the server on an ephemeral port.
"""
import http.client
import json
import os
import re
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from print_handy import settings
from print_handy.webui import access, pages, server

TOKEN = "env-token-for-setup-tests-123"
PASSWORD = "correct horse battery"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def load(tmp, extra=""):
    local = Path(tmp) / "local.yaml"
    local.write_text(f"paths:\n  hermes_home: {tmp}/hermes\n"
                     "webui:\n  enabled: true\n  host: 0.0.0.0\n  port: 8765\n" + extra)
    return settings.load(local=local, environ={})


class ResolveOrderTests(unittest.TestCase):
    """The five rules, first match wins; the first two and the fourth are pre-#708 behavior."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = load(self.tmp)

    def test_lan_bind_with_nothing_chosen_is_setup(self):
        self.assertEqual(access.resolve(self.data, "0.0.0.0", {}).mode, access.SETUP)

    def test_loopback_with_nothing_chosen_stays_open(self):
        self.assertEqual(access.resolve(self.data, "127.0.0.1", {}).mode, access.OPEN)

    def test_env_token_wins_over_a_saved_choice(self):
        access.save_open(self.data, True)
        resolved = access.resolve(self.data, "0.0.0.0", {"WEBUI_ACCESS_TOKEN": TOKEN})
        self.assertEqual(resolved.mode, access.TOKEN)
        self.assertTrue(resolved.matches(TOKEN))
        self.assertFalse(resolved.matches(TOKEN + "x"))

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

    def test_unreadable_saved_file_falls_back_to_setup(self):
        path = access.access_path(self.data)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        self.assertEqual(access.resolve(self.data, "0.0.0.0", {}).mode, access.SETUP)


class SetupFlowTests(unittest.TestCase):
    def start(self, extra="", environ=None):
        tmp = tempfile.mkdtemp()
        data = load(tmp, extra)
        environ = environ or {}
        httpd = server.make_server(data, environ=environ, host="0.0.0.0", port=0,
                                   snapshot_func=lambda: {"ok": True})
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

    def form_token(self, port):
        _, _, body = self.request(port, "GET", "/setup")
        return re.search(rf'name="{pages.CSRF_FIELD}" value="([^"]+)"', body).group(1)

    def post_setup(self, port, **fields):
        return self.request(port, "POST", "/setup", body=urlencode(fields), headers=FORM)

    def test_setup_mode_serves_only_setup_and_healthz(self):
        _, port, _ = self.start()
        code, headers, _ = self.request(port, "GET", "/", headers={"Accept": "text/html"})
        self.assertEqual((code, headers["location"]), (303, "/setup"))
        code, _, body = self.request(port, "GET", "/api/status")
        self.assertEqual(code, 503)
        self.assertIn("first-run setup", json.loads(body)["reason"])
        self.assertIn(self.request(port, "GET", "/healthz")[0], (200, 503))
        code, _, body = self.request(port, "GET", "/setup")
        self.assertEqual(code, 200)
        self.assertIn("Set a password", body)
        code, _, _ = self.request(port, "POST", "/config", body="x=1", headers=FORM)
        self.assertEqual(code, 503)

    def test_setup_post_needs_the_form_token(self):
        _, port, data = self.start()
        code, _, _ = self.post_setup(port, choice="open", confirm_open="yes")
        self.assertEqual(code, 403)
        self.assertFalse(access.access_path(data).exists())

    def test_choosing_a_password_signs_in_and_then_requires_it(self):
        httpd, port, _ = self.start()
        code, headers, _ = self.post_setup(port, **{pages.CSRF_FIELD: self.form_token(port),
                                                     "choice": "password", "password": PASSWORD,
                                                     "password_again": PASSWORD})
        self.assertEqual(code, 303)
        cookie = headers["set-cookie"].split(";", 1)[0]
        self.assertEqual(httpd.access.mode, access.PASSWORD)
        self.assertEqual(self.request(port, "GET", "/api/status", headers={"Cookie": cookie})[0], 200)
        self.assertEqual(self.request(port, "GET", "/api/status")[0], 401)
        self.assertEqual(self.request(port, "GET", "/api/status",
                                      headers={server.TOKEN_HEADER: PASSWORD})[0], 200)
        code, _, body = self.request(port, "GET", "/login")
        self.assertIn("password chosen during setup", body)
        # Setup is done: /setup no longer offers a choice.
        self.assertEqual(self.request(port, "GET", "/setup", headers={"Cookie": cookie})[0], 303)

    def test_setup_token_cannot_write_config_after_a_password_is_set(self):
        # Review finding on #737: /setup is served to anyone before setup, so
        # its token must not authorize /config or /debug writes afterwards.
        httpd, port, _ = self.start()
        setup_token = self.form_token(port)
        self.assertNotEqual(setup_token, httpd.form_token)
        self.post_setup(port, **{pages.CSRF_FIELD: setup_token, "choice": "password",
                                 "password": PASSWORD, "password_again": PASSWORD})
        for path, body in (("/debug", {"action": "on"}), ("/config/preview", {}), ("/config", {})):
            with self.subTest(path=path):
                code, _, _ = self.request(port, "POST", path, headers=FORM,
                                          body=urlencode({pages.CSRF_FIELD: setup_token, **body}))
                self.assertEqual(code, 403)

    def test_choosing_open_needs_the_box_ticked(self):
        httpd, port, _ = self.start()
        token = self.form_token(port)
        code, _, body = self.post_setup(port, **{pages.CSRF_FIELD: token, "choice": "open"})
        self.assertEqual(code, 400)
        self.assertIn("Tick the box", body)
        code, _, _ = self.post_setup(port, **{pages.CSRF_FIELD: token, "choice": "open",
                                              "confirm_open": "yes"})
        self.assertEqual(code, 303)
        self.assertEqual(httpd.access.mode, access.OPEN)
        self.assertEqual(self.request(port, "GET", "/api/status")[0], 200)

    def test_bad_password_choice_is_refused_without_saving(self):
        _, port, data = self.start()
        code, _, body = self.post_setup(port, **{pages.CSRF_FIELD: self.form_token(port),
                                                  "choice": "password", "password": PASSWORD,
                                                  "password_again": "different value"})
        self.assertEqual(code, 400)
        self.assertIn("do not match", body)
        self.assertNotIn(PASSWORD, body)
        self.assertFalse(access.access_path(data).exists())

    def test_existing_token_install_never_sees_setup(self):
        _, port, _ = self.start(environ={"WEBUI_ACCESS_TOKEN": TOKEN})
        self.assertEqual(self.request(port, "GET", "/api/status")[0], 401)
        self.assertEqual(self.request(port, "GET", "/api/status",
                                      headers={server.TOKEN_HEADER: TOKEN})[0], 200)
        code, headers, _ = self.request(port, "GET", "/setup", headers={server.TOKEN_HEADER: TOKEN})
        self.assertEqual((code, headers.get("location")), (303, "/"))

    def test_bearer_is_not_accepted(self):
        # #708 decision: X-WebUI-Token is the one header (Home Assistant sends it).
        _, port, _ = self.start(environ={"WEBUI_ACCESS_TOKEN": TOKEN})
        self.assertEqual(self.request(port, "GET", "/api/status",
                                      headers={"Authorization": f"Bearer {TOKEN}"})[0], 401)


if __name__ == "__main__":
    unittest.main()
