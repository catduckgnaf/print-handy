"""Pin tests for the #1603 item 3 handler split.

``SessionStore`` and the login/logout/session-cookie handlers moved
from ``webui/server.py`` to ``webui/sessions.py``, and the page
handlers split by route group into ``webui/*_handlers.py``. These
tests pin that the new homes are the real implementations, that the
old ``webui.server`` names still resolve to them, and that login and
logout keep their cookie behavior through the moved handlers.
"""
import http.client
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from print_handy import settings
from print_handy.webui import (
    chat_handlers,
    config_handlers,
    data_handlers,
    design_handlers,
    printer_handlers,
    server,
    sessions,
    status_handlers,
    upload_handlers,
    walkthrough_handlers,
)

TOKEN = "test-token-1603"
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


class NewHomeTests(unittest.TestCase):
    def test_session_store_lives_in_sessions_and_is_reexported(self):
        self.assertIs(server.SessionStore, sessions.SessionStore)
        self.assertIs(server.SESSION_COOKIE, sessions.SESSION_COOKIE)
        self.assertIs(server.session_form_token, sessions.session_form_token)
        store = sessions.SessionStore()
        session_id = store.mint()
        self.assertEqual(store.validate(session_id), session_id)
        store.revoke(session_id)
        self.assertEqual(store.validate(session_id), "")

    def test_handler_groups_live_in_their_modules(self):
        # Each route group's implementation is a function in its own
        # module, and the _Handler delegate calls that same function.
        for module, name in (
            (sessions, "login_page"), (sessions, "logout"),
            (sessions, "start_session"), (sessions, "valid_session_id"),
            (status_handlers, "status_page"), (status_handlers, "healthz_route"),
            (data_handlers, "data_route"), (data_handlers, "wrapped_video_post"),
            (printer_handlers, "printer_status_route"),
            (printer_handlers, "spools_page"),
            (design_handlers, "designs_page"), (design_handlers, "sliced_route"),
            (config_handlers, "config_page"), (config_handlers, "debug_post"),
            (chat_handlers, "chat_page"), (chat_handlers, "workflow_page"),
            (upload_handlers, "upload_post"), (upload_handlers, "receive_upload"),
            (walkthrough_handlers, "walkthrough_page"),
            (walkthrough_handlers, "sync_access"),
        ):
            self.assertTrue(callable(getattr(module, name)), f"{module.__name__}.{name}")


class MovedLoginLogoutTests(unittest.TestCase):
    def start(self, tmp):
        local = Path(tmp) / "local.yaml"
        local.write_text(f"paths:\n  state_home: {tmp}/hermes\n"
                         "webui:\n  enabled: true\n  port: 8765\n")
        environ = {"PRINT_HANDY_CONFIG": str(local), "WEBUI_ACCESS_TOKEN": TOKEN}
        data = settings.load(local=local, environ=environ)
        httpd = server.make_server(data, environ=environ, port=0,
                                   snapshot_func=lambda: FAKE_SNAPSHOT)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        return httpd.server_address[1]

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

    def test_login_sets_session_cookie_and_logout_clears_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            port = self.start(tmp)
            code, headers, _body = self.request(
                port, "POST", "/login", body=urlencode({"token": TOKEN}),
                headers=FORM_HEADERS)
            self.assertEqual(code, 303)
            set_cookie = headers["set-cookie"]
            self.assertIn(f"{sessions.SESSION_COOKIE}=", set_cookie)
            self.assertIn("HttpOnly", set_cookie)
            self.assertIn("SameSite=Strict", set_cookie)
            cookie = set_cookie.split(";", 1)[0].strip()
            code, _h, _b = self.request(port, "GET", "/", headers={"Cookie": cookie})
            self.assertEqual(code, 200)
            code, headers, _b = self.request(
                port, "POST", "/logout", body="", headers={"Cookie": cookie})
            self.assertEqual(code, 303)
            self.assertIn("Max-Age=0", headers["set-cookie"])
            code, _h, _b = self.request(port, "GET", "/", headers={"Cookie": cookie})
            self.assertEqual(code, 401)


if __name__ == "__main__":
    unittest.main()
