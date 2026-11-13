"""#1374: one-step spool management on the Web UI Spools page.

Archived spools are hidden until asked for, a spool's grams left are set in
one form, a shelf spool is loaded into (or swapped into) a slot in one form,
and a new spool can be added straight into a slot. Driven through the real
server (GET and POST /spools/<id>) with the real spools view and actions on
the fake Spoolman from test_webui_spools.py; nothing reaches a printer.
"""
import http.client
import re
import tempfile
import threading
import unittest
import urllib.parse
from unittest import mock

from print_handy.runtime import spool_sync
from print_handy.webui import server, spools

from tests.test_webui_spools import (PRINTER, TOKEN, FakeSpoolman, fake_mr, load_settings,
                                     signed_in, state_file)


class OneStepPageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = self._tmp.name
        for patch in (mock.patch.object(spool_sync, "push_screen", lambda *a, **k: "pushed"),
                      mock.patch.object(spool_sync, "_printer_adapter", lambda pid: None)):
            patch.start()
            self.addCleanup(patch.stop)
        self.fake = FakeSpoolman()
        self.state = state_file(tmp)
        environ = {"WEBUI_ACCESS_TOKEN": TOKEN}
        self.data = load_settings(tmp, "webui:\n  enabled: true\n  port: 8765\n"
                                       "  controls_enabled: true\n", environ=environ)
        deps = {"client": self.fake, "mr": fake_mr(), "state_path": self.state}
        self.httpd = server.make_server(
            self.data, environ=environ, port=0, snapshot_func=lambda: {},
            spools_view_func=lambda pid: spools.spools_view(
                self.data, pid, client=self.fake, state_path=self.state),
            spools_action_func=lambda pid, action, body: spools.spool_action(
                self.data, pid, action, body, **deps))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.port = self.httpd.server_address[1]

    def _request(self, method, path, body=b"", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally:
            conn.close()

    def page(self, query=""):
        code, _, raw = self._request("GET", f"/spools/{PRINTER}{query}",
                                     headers={server.TOKEN_HEADER: TOKEN})
        self.assertEqual(code, 200, raw)
        return raw

    def post(self, **fields):
        cookie, csrf = signed_in(self.httpd)
        form = urllib.parse.urlencode({"_csrf": csrf, **fields}).encode()
        code, headers, raw = self._request(
            "POST", f"/spools/{PRINTER}", form,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie})
        self.assertEqual(code, 303, raw)
        return urllib.parse.unquote(headers["Location"])

    def test_archived_spools_are_hidden_until_the_toggle(self):
        raw = self.page()
        # Spool #11 is the archived Gray PETG.
        self.assertNotIn("#11", raw)
        self.assertIn(f'href="/spools/{PRINTER}?archived=1"', raw)
        self.assertIn("Show archived spools (1)", raw)
        shown = self.page("?archived=1")
        self.assertIn("#11", shown)
        self.assertIn("Hide archived spools", shown)
        self.assertIn("Unarchive", shown)

    def test_grams_left_is_set_in_one_step(self):
        raw = self.page()
        # Every live spool row and every loaded slot has its own weight form.
        self.assertGreaterEqual(len(re.findall(r'name="remaining_weight"', raw)), 3)
        location = self.post(spool_action="edit", spool_id="3", remaining_weight="932.4")
        self.assertIn("932.4 g left", location)
        self.assertEqual(self.fake.spools[3]["remaining_weight"], 932.4)
        self.assertIn("932.4 g left", self.page())

    def test_a_shelf_spool_is_swapped_into_a_loaded_slot_in_one_step(self):
        self.assertIn("Replace with", self.page())
        self.post(spool_action="assign", slot="2", spool_id="7")
        claims = {sid: row["extra"].get("ph_slot") for sid, row in self.fake.spools.items()}
        self.assertEqual(claims[7], f'"{PRINTER}:2"')
        self.assertIn(claims[3], (None, "", '""', "null"))

    def test_a_new_spool_is_added_straight_into_a_slot(self):
        location = self.post(spool_action="add", material="PLA", brand="Sunlu", color="Black",
                             grams="1000", slot="4")
        self.assertIn("loaded it into slot 4", location)
        made = max(self.fake.spools)
        self.assertEqual(self.fake.spools[made]["extra"].get("ph_slot"), f'"{PRINTER}:4"')

    def test_add_without_a_slot_still_goes_to_the_shelf(self):
        location = self.post(spool_action="add", material="PLA", brand="Sunlu", color="Black",
                             grams="1000", slot="")
        self.assertIn("on the shelf", location)


if __name__ == "__main__":
    unittest.main()
