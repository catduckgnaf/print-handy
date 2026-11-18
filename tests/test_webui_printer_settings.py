"""Web UI Printer settings tab (#1448).

One printer at a time, plain-language fields, written through the
existing settings machinery: catalog fields through
:mod:`print_handy.webui.config`'s validated write, roster entry keys
through the entry writer beside it. Everything runs offline against
temp settings files named by PRINT_HANDY_CONFIG, with the server on an
ephemeral loopback port, over fixture configs for three vendor
families: a Moonraker/ZMod printer (legacy and roster shapes), a Bambu
LAN printer, and a PrusaLink printer (roster shape).
"""
import http.client
import re
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from print_handy import settings
from print_handy.webui import config as config_api
from print_handy.webui import server

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

ZMOD = ("printer:\n  model: flashforge.ad5x\n  name: Garage AD5X\n"
        "services:\n  moonraker_url: http://ad5x.local:7125\n")

BAMBU = ("printer:\n  model: bambulab.a1\n  name: Desk A1\n"
         "bambu:\n  lan_host: 10.0.0.2\n  lan_serial: 00M00A000000000\n")

ROSTER = (
    "printers:\n"
    "  - id: ad5x\n    model: flashforge.ad5x\n    name: Garage AD5X\n"
    "    moonraker_url: http://ad5x.local:7125\n    chat_id: '111'\n"
    "  - id: mk4s\n    model: prusa.mk4s\n    name: Desk Prusa\n"
    "    link_host: prusa.local\n    chat_id: '222'\n")


def hidden_inputs(body):
    return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', body))


class PrinterSettingsPageTests(unittest.TestCase):
    def start(self, tmp, extra="", environ=None):
        local = Path(tmp) / "local.yaml"
        local.write_text(f"paths:\n  state_home: {tmp}/hermes\n"
                         "webui:\n  enabled: true\n  port: 8765\n" + extra)
        environ = {"PRINT_HANDY_CONFIG": str(local), **(environ or {})}
        data = settings.load(local=local, environ=environ)
        httpd = server.make_server(data, environ=environ, port=0,
                                   snapshot_func=lambda: FAKE_SNAPSHOT)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        return httpd.server_address[1], local, environ

    def request(self, port, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read().decode()
        finally:
            conn.close()

    def get_page(self, port, printer=None):
        path = "/printer-settings" if printer is None \
            else f"/printer-settings?printer={printer}"
        code, body = self.request(port, "GET", path)
        return code, body, hidden_inputs(body).get("_csrf", "")

    def post(self, port, pairs):
        return self.request(port, "POST", "/printer-settings",
                            body=urlencode(pairs), headers=FORM_HEADERS)

    # -- navigation and rendering --------------------------------------

    def test_nav_and_page_render_for_zmod_printer(self):
        with tempfile.TemporaryDirectory() as tmp:
            port, _local, _env = self.start(tmp, ZMOD)
            code, body, _csrf = self.get_page(port)
            self.assertEqual(code, 200, body)
            # No nav tab of its own (#1625): Cost took the slot, and the
            # page highlights Printers and links back to it.
            self.assertNotIn('href="/printer-settings"', body)
            self.assertIn('<a href="/printers" class="active">', body)
            self.assertIn('href="/printers">&larr; All printers', body)
            self.assertIn("Printer settings", body)
            self.assertIn("Garage AD5X", body)               # effective name
            self.assertIn("Moonraker URL", body)             # its connection field
            self.assertIn("http://ad5x.local:7125", body)
            self.assertIn("ZMod delivery format", body)      # flashforge only
            self.assertIn("Delivery methods", body)
            self.assertIn("settings file", body)             # the value's source

    def test_bambu_page_shows_lan_fields_not_moonraker(self):
        with tempfile.TemporaryDirectory() as tmp:
            port, _local, _env = self.start(tmp, BAMBU)
            code, body, _csrf = self.get_page(port)
            self.assertEqual(code, 200, body)
            self.assertIn("Bambu Lab LAN host", body)
            self.assertIn('name="bambu.lan_serial"', body)
            self.assertNotIn('name="services.moonraker_url"', body)
            self.assertNotIn("ZMod delivery format", body)

    def test_unknown_printer_is_a_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            port, _local, _env = self.start(tmp, ZMOD)
            code, _body = self.request(port, "GET", "/printer-settings?printer=nope")
            self.assertEqual(code, 404)

    # -- legacy shape: save round trip ----------------------------------

    def test_zmod_save_round_trip_through_the_loader(self):
        with tempfile.TemporaryDirectory() as tmp:
            port, local, env = self.start(tmp, ZMOD)
            _code, _body, csrf = self.get_page(port)
            pid = settings.default_printer_id(settings.load(local=local, environ=env))
            code, body = self.post(port, [
                ("_csrf", csrf), ("printer", pid),
                ("printer.name", "Workshop AD5X"),
                ("printer.model", "flashforge.ad5x"),
                ("services.moonraker_url", "http://192.168.1.50:7125"),
                ("printer.slicer", "orca"),
                ("delivery.methods", "printer"),
                ("printer.zmod_delivery_format", "auto"),
            ])
            self.assertEqual(code, 200, body)
            self.assertIn("Saved", body)
            reloaded = settings.load(local=local, environ=env)
            self.assertEqual(reloaded["printer"]["name"], "Workshop AD5X")
            self.assertEqual(reloaded["services"]["moonraker_url"],
                             "http://192.168.1.50:7125")

    def test_bambu_save_round_trip_through_the_loader(self):
        with tempfile.TemporaryDirectory() as tmp:
            port, local, env = self.start(tmp, BAMBU)
            _code, _body, csrf = self.get_page(port)
            pid = settings.default_printer_id(settings.load(local=local, environ=env))
            code, body = self.post(port, [
                ("_csrf", csrf), ("printer", pid),
                ("printer.name", "Desk A1"),
                ("printer.model", "bambulab.a1"),
                ("bambu.lan_host", "10.0.0.9"),
                ("bambu.lan_serial", "00M00A000000000"),
                ("printer.slicer", "bambu_studio"),
                ("delivery.methods", "discord"),
            ])
            self.assertEqual(code, 200, body)
            reloaded = settings.load(local=local, environ=env)
            self.assertEqual(reloaded["bambu"]["lan_host"], "10.0.0.9")
            self.assertEqual(reloaded["bambu"]["lan_serial"], "00M00A000000000")

    # -- locked values ---------------------------------------------------

    def test_environment_value_is_locked_and_named(self):
        locked = {"PRINT_HANDY__SERVICES__MOONRAKER_URL": "http://env.local:7125"}
        with tempfile.TemporaryDirectory() as tmp:
            port, local, env = self.start(tmp, ZMOD, environ=locked)
            code, body, csrf = self.get_page(port)
            self.assertEqual(code, 200, body)
            self.assertIn("Locked:", body)
            self.assertIn("PRINT_HANDY__SERVICES__MOONRAKER_URL", body)
            self.assertIn("http://env.local:7125", body)  # the effective value
            pid = settings.default_printer_id(settings.load(local=local, environ=env))
            code, body = self.post(port, [
                ("_csrf", csrf), ("printer", pid),
                ("printer.name", "Renamed AD5X"),
                ("printer.model", "flashforge.ad5x"),
                ("services.moonraker_url", "http://sneaky.local:7125"),
                ("printer.slicer", "orca"),
                ("delivery.methods", "printer"),
                ("printer.zmod_delivery_format", "auto"),
            ])
            self.assertEqual(code, 200, body)
            reloaded = settings.load(local=local, environ=env)
            self.assertEqual(reloaded["printer"]["name"], "Renamed AD5X")
            self.assertEqual(reloaded["services"]["moonraker_url"],
                             "http://env.local:7125")  # the environment still wins

    # -- invalid values ----------------------------------------------------

    def test_invalid_value_refused_in_place_with_loader_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            port, local, env = self.start(tmp, ZMOD)
            _code, _body, csrf = self.get_page(port)
            pid = settings.default_printer_id(settings.load(local=local, environ=env))
            before = local.read_bytes()
            code, body = self.post(port, [
                ("_csrf", csrf), ("printer", pid),
                ("printer.name", "Renamed AD5X"),
                ("printer.model", "flashforge.ad5x"),
                ("services.moonraker_url", "http://ad5x.local:7125"),
                ("printer.slicer", "bambu_studio"),  # not offered for flashforge
                ("delivery.methods", "printer"),
                ("printer.zmod_delivery_format", "auto"),
            ])
            self.assertEqual(code, 400, body)
            self.assertIn("is not available for vendor", body)
            self.assertIn("Renamed AD5X", body)  # the other edit is not lost
            self.assertEqual(local.read_bytes(), before)  # nothing was written

    def test_bambu_host_without_serial_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            port, local, env = self.start(tmp, BAMBU)
            _code, _body, csrf = self.get_page(port)
            pid = settings.default_printer_id(settings.load(local=local, environ=env))
            before = local.read_bytes()
            code, body = self.post(port, [
                ("_csrf", csrf), ("printer", pid),
                ("printer.name", "Desk A1"),
                ("printer.model", "bambulab.a1"),
                ("bambu.lan_host", "10.0.0.9"),
                ("bambu.lan_serial", ""),  # the pair must be set together
                ("printer.slicer", "bambu_studio"),
                ("delivery.methods", "discord"),
            ])
            self.assertEqual(code, 400, body)
            self.assertIn("must be set together", body)
            self.assertEqual(local.read_bytes(), before)

    # -- roster shape ------------------------------------------------------

    def test_roster_picker_and_prusalink_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            port, local, env = self.start(tmp, ROSTER)
            code, body, _csrf = self.get_page(port)
            self.assertEqual(code, 200, body)
            self.assertIn("printer=ad5x", body)   # the picker lists both
            self.assertIn("printer=mk4s", body)
            code, body, csrf = self.get_page(port, "mk4s")
            self.assertEqual(code, 200, body)
            self.assertIn("Desk Prusa", body)
            self.assertIn("Chat channel", body)         # per-printer channel
            self.assertIn("PrusaLink host", body)
            form = [
                ("_csrf", csrf), ("printer", "mk4s"),
                ("entry.name", "Office Prusa"),
                ("entry.model", "prusa.mk4s"),
                ("entry.chat_id", "333"),
                ("entry.link_host", "192.168.1.61"),
                ("delivery.methods", "printer"),
            ]
            # Moving a credentialed service's host asks for confirmation
            # first, exactly like the Settings page (#1192).
            code, body = self.post(port, form)
            self.assertEqual(code, 200, body)
            self.assertIn("_rebind", body)
            self.assertIn("PrusaLink", body)
            code, body = self.post(port, form + [("_rebind", "entry.link_host")])
            self.assertEqual(code, 200, body)
            self.assertIn("Saved", body)
            reloaded = settings.load(local=local, environ=env)
            entry = settings.by_id("mk4s", reloaded)
            self.assertEqual((entry["name"], entry["chat_id"], entry["link_host"]),
                             ("Office Prusa", "333", "192.168.1.61"))
            other = settings.by_id("ad5x", reloaded)
            self.assertEqual((other["name"], other["moonraker_url"]),
                             ("Garage AD5X", "http://ad5x.local:7125"))

    def test_roster_entry_key_locked_by_environment(self):
        locked = {"PRINT_HANDY__PRINTERS__MK4S__CHAT_ID": "999"}
        with tempfile.TemporaryDirectory() as tmp:
            port, _local, _env = self.start(tmp, ROSTER, environ=locked)
            code, body, _csrf = self.get_page(port, "mk4s")
            self.assertEqual(code, 200, body)
            self.assertIn("PRINT_HANDY__PRINTERS__MK4S__CHAT_ID", body)
            self.assertIn("999", body)  # the effective value

    def test_roster_invalid_entry_value_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            port, local, env = self.start(tmp, ROSTER)
            _code, _body, csrf = self.get_page(port, "ad5x")
            before = local.read_bytes()
            code, body = self.post(port, [
                ("_csrf", csrf), ("printer", "ad5x"),
                ("entry.name", "Garage AD5X"),
                ("entry.model", "flashforge.ad5x"),
                ("entry.chat_id", "111"),
                ("entry.moonraker_url", "not a url"),
                ("delivery.methods", "printer"),
            ])
            self.assertEqual(code, 400, body)
            self.assertIn("must be a URL", body)
            self.assertEqual(local.read_bytes(), before)


class PrinterEntryWriterTests(unittest.TestCase):
    """The roster entry writer in webui/config.py, at file level."""

    def start(self, tmp, extra="", environ=None):
        local = Path(tmp) / "local.yaml"
        local.write_text(f"paths:\n  state_home: {tmp}/hermes\n" + extra)
        environ = {"PRINT_HANDY_CONFIG": str(local), **(environ or {})}
        data = settings.load(local=local, environ=environ)
        return local, environ, data

    def test_entry_state_reports_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            _local, env, data = self.start(tmp, ROSTER)
            state = config_api.printer_entry_state(data, "mk4s", environ=env)
            self.assertEqual(state.shape, "settings_file")
            self.assertTrue(state.writable)
            self.assertEqual(state.keys["link_host"].value, "prusa.local")
            self.assertEqual(state.keys["link_host"].source, config_api.SETTINGS_FILE)
            self.assertEqual(state.keys["moonraker_url"].source, config_api.DEFAULT)

    def test_writer_refuses_environment_overridden_key(self):
        locked = {"PRINT_HANDY__PRINTERS__MK4S__CHAT_ID": "999"}
        with tempfile.TemporaryDirectory() as tmp:
            _local, env, data = self.start(tmp, ROSTER, environ=locked)
            with self.assertRaisesRegex(config_api.WebUIConfigError,
                                        "PRINT_HANDY__PRINTERS__MK4S__CHAT_ID"):
                config_api.write_printer_entry("mk4s", {"chat_id": "5"}, environ=env)

    def test_writer_refuses_unknown_and_multiline_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            _local, env, _data = self.start(tmp, ROSTER)
            with self.assertRaisesRegex(config_api.WebUIConfigError, "not an editable"):
                config_api.write_printer_entry("mk4s", {"bed_mm": [1, 2]}, environ=env)
            with self.assertRaisesRegex(config_api.WebUIConfigError, "one line"):
                config_api.write_printer_entry("mk4s", {"name": "two\nlines"}, environ=env)


if __name__ == "__main__":
    unittest.main()
