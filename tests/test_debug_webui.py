"""#478: the Web UI debug-mode control — form on the status page, POST /debug."""
import http.client
import re
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path

from print_handy import debug_mode, settings
from print_handy.webui import server


def load_settings(tmp):
    local = Path(tmp) / "local.yaml"
    local.write_text(f"paths:\n  state_home: {tmp}/hermes\nwebui:\n  enabled: true\n  port: 8765\n")
    return settings.load(local=local, environ={})


class DebugWebUITests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = load_settings(self._tmp.name)
        debug_mode._sync_cache.update({"stamp": None, "started": set(), "active": None})

        def snapshot():
            row = debug_mode.check_row(self.data)
            return {
                "version": "0.test", "host": "testhost", "ok": True,
                "printer": {"model": "flashforge.ad5x", "name": "Flashforge AD5X",
                            "vendor": "flashforge", "connection": "moonraker"},
                "bot": {"platform": "discord", "mode": "standalone", "ok": True,
                        "reason": "connected", "heartbeat": {}},
                "listener": {"required": False, "ok": True, "reason": "not required"},
                "spoolman": {"bundled": False, "url": "", "ok": False, "detail": "not configured"},
                "services": {},
                "doctor": [{"label": "debug mode", "status": "warn" if row else "ok",
                            "detail": row or "off"}],
            }

        self.httpd = server.make_server(self.data, environ={}, port=0, snapshot_func=snapshot)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_address[1]
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(self.httpd.server_close)

    def tearDown(self):
        debug_mode.end(self.data)
        debug_mode._sync_cache.update({"stamp": None, "started": set(), "active": None})
        self._tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read().decode()
        finally:
            conn.close()

    def form_token(self):
        code, body = self.request("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn("Debug log mode", body)
        match = re.search(r'name="_csrf" value="([^"]+)"', body)
        self.assertIsNotNone(match, body)
        return match.group(1)

    def post_debug(self, fields):
        body = urllib.parse.urlencode(fields)
        return self.request("POST", "/debug", body=body,
                            headers={"Content-Type": "application/x-www-form-urlencoded"})

    def test_post_without_the_form_token_is_refused(self):
        code, body = self.post_debug({"action": "on"})
        self.assertEqual(code, 403)
        # #840: a browser form refusal is a themed page naming the
        # stale-tab case, not a raw JSON dump.
        self.assertIn('<header class="site">', body)
        self.assertIn("out of date (Print Handy restarted)", body)
        self.assertNotIn('"ok": false', body)
        self.assertIsNone(debug_mode.status(self.data))

    def test_status_page_carries_the_submit_script_and_turn_off_confirm(self):
        _code, body = self.request("GET", "/")
        self.assertIn("<script>", body)  # the shell's shared submit script
        self.assertIn("data-confirm", body)  # on the debug Turn off button

    def test_on_then_off_through_the_form(self):
        token = self.form_token()
        code, _ = self.post_debug({"_csrf": token, "action": "on", "minutes": "15"})
        self.assertEqual(code, 303)
        state = debug_mode.status(self.data)
        self.assertIsNotNone(state)
        self.assertEqual(state["minutes"], 15)
        code, body = self.request("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn("Debug log mode", body)
        self.assertIn("ON", body)
        code, _ = self.post_debug({"_csrf": token, "action": "off"})
        self.assertEqual(code, 303)
        self.assertIsNone(debug_mode.status(self.data))

    def test_bad_minutes_is_a_400(self):
        token = self.form_token()
        code, body = self.post_debug({"_csrf": token, "action": "on", "minutes": "soon"})
        self.assertEqual(code, 400)
        # #840: the status page itself is re-rendered with the error.
        self.assertIn("minutes must be a whole number", body)
        self.assertIn('<header class="site">', body)
        self.assertIn("Debug log mode", body)
        self.assertIsNone(debug_mode.status(self.data))


if __name__ == "__main__":
    unittest.main()
