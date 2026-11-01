"""#708 (control endpoints phase): Web UI printer controls.

Fake adapters and fake senders only: nothing here opens a socket to a
printer.
"""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from print_handy import settings
from print_handy.webui import controls, server

TOKEN = "control-test-token"
PRINTER = "flashforge-ad5x"


def load_settings(tmp, extra="", environ=None):
    local = Path(tmp) / "local.yaml"
    local.write_text(f"paths:\n  hermes_home: {tmp}/hermes\n{extra}")
    return settings.load(local=local, environ={} if environ is None else environ)


def enabled(tmp, extra=""):
    return load_settings(tmp, "webui:\n  controls_enabled: true\n" + extra)


class FakeFamilyAdapter:
    """A non-Moonraker family: control() like FamilyControl, optional heater."""

    connection = "fake_lan"

    def __init__(self, temps=True, facts=None):
        self.calls = []
        self._facts = facts or {"max_hotend_c": 300, "max_bed_c": 110}
        if temps:
            self.set_target_temperature = lambda heater, target: self.calls.append(
                ("temp", heater, target))

    def control(self, action):
        self.calls.append(("control", action))
        return {"ok": True}

    def fact(self, name):
        return self._facts[name]


def reads(state):
    return lambda adapter, view: {"state": state}


def unreachable(adapter, view):
    raise OSError("connection refused")


class Recorder:
    def __init__(self):
        self.sent = []

    def __call__(self, adapter, view, command):
        self.sent.append(command)
        return {}


class SettingTests(unittest.TestCase):
    def test_controls_off_by_default_and_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIs(load_settings(tmp)["webui"]["controls_enabled"], False)
            self.assertIs(enabled(tmp)["webui"]["controls_enabled"], True)
            with self.assertRaises(settings.SettingsError):
                load_settings(tmp, "webui:\n  controls_enabled: 'yes'\n")


class ControlTests(unittest.TestCase):
    def test_off_is_403_and_sends_nothing(self):
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            data = load_settings(tmp)
            for action in controls.ACTIONS:
                code, body = controls.control(data, PRINTER, action, {"confirm": True},
                                              read=reads("printing"), send=send)
                self.assertEqual(code, 403)
                self.assertIn("controls_enabled", body["reason"])
            code, _ = controls.set_temperature(data, PRINTER, {"heater": "bed", "target": 60},
                                               read=reads("idle"), send=send)
            self.assertEqual(code, 403)
        self.assertEqual(send.sent, [])

    def test_unknown_printer_is_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, body = controls.control(enabled(tmp), "nope", "pause",
                                          read=reads("printing"), send=Recorder())
        self.assertEqual(code, 404)
        self.assertFalse(body["ok"])
        self.assertIn("nope", body["reason"])

    def test_pause_resume_cancel_through_moonraker_path(self):
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            self.assertEqual(controls.control(data, PRINTER, "pause",
                                              read=reads("printing"), send=send)[0], 200)
            self.assertEqual(controls.control(data, PRINTER, "resume",
                                              read=reads("paused"), send=send)[0], 200)
            code, body = controls.control(data, PRINTER, "cancel", {"confirm": True},
                                          read=reads("printing"), send=send)
        self.assertEqual(code, 200)
        self.assertEqual(body["action"], "cancel")
        self.assertEqual(send.sent, [("control", "pause"), ("control", "resume"),
                                     ("control", "cancel")])

    def test_cancel_needs_confirm_true(self):
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            for body in (None, {}, {"confirm": "true"}, {"confirm": 1}):
                code, out = controls.control(data, PRINTER, "cancel", body,
                                             read=reads("printing"), send=send)
                self.assertEqual(code, 400)
                self.assertIn('"confirm": true', out["reason"])
        self.assertEqual(send.sent, [])

    def test_wrong_state_is_409(self):
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            code, body = controls.control(enabled(tmp), PRINTER, "resume",
                                          read=reads("printing"), send=send)
        self.assertEqual(code, 409)
        self.assertIn("printing", body["reason"])
        self.assertEqual(send.sent, [])

    def test_unreachable_is_explicit_502(self):
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            code, body = controls.control(enabled(tmp), PRINTER, "pause",
                                          read=unreachable, send=send)
        self.assertEqual(code, 502)
        self.assertIn("unreachable", body["reason"])
        self.assertEqual(send.sent, [])

    def test_send_failure_is_502(self):
        def broken(adapter, view, command):
            raise OSError("reset by peer")
        with tempfile.TemporaryDirectory() as tmp:
            code, body = controls.control(enabled(tmp), PRINTER, "pause",
                                          read=reads("printing"), send=broken)
        self.assertEqual(code, 502)
        self.assertIn("reset by peer", body["reason"])

    def test_family_adapter_uses_family_control_vocabulary(self):
        adapter = FakeFamilyAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = controls.control(enabled(tmp), PRINTER, "cancel", {"confirm": True},
                                       adapter=adapter, read=reads("paused"))
        self.assertEqual(code, 200)
        self.assertEqual(adapter.calls, [("control", "stop")])

    def test_offline_family_refuses_cleanly(self):
        adapter = FakeFamilyAdapter()
        adapter.connection = "offline"
        with tempfile.TemporaryDirectory() as tmp:
            code, body = controls.control(enabled(tmp), PRINTER, "pause",
                                          adapter=adapter, read=reads("printing"))
        self.assertEqual(code, 501)
        self.assertIn("not supported", body["reason"])
        self.assertEqual(adapter.calls, [])


class TemperatureTests(unittest.TestCase):
    def test_moonraker_target_is_sent(self):
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            code, body = controls.set_temperature(enabled(tmp), PRINTER,
                                                  {"heater": "nozzle", "target": 210},
                                                  read=reads("idle"), send=send)
        self.assertEqual(code, 200, body)
        self.assertEqual(send.sent, [("temperature", "nozzle", 210.0)])

    def test_validation_and_limits(self):
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            for bad in ({"heater": "chamber", "target": 40}, {"heater": "bed"},
                        {"heater": "bed", "target": True}, {"heater": "bed", "target": -5},
                        {"heater": "bed", "target": 500}, {"heater": "nozzle", "target": 600}):
                code, _ = controls.set_temperature(data, PRINTER, bad,
                                                   read=reads("idle"), send=send)
                self.assertEqual(code, 400, bad)
        self.assertEqual(send.sent, [])

    def test_nozzle_limit_is_the_lower_of_nozzle_type_and_hotend(self):
        adapter = FakeFamilyAdapter(facts={"max_hotend_c": 350, "max_bed_c": 110})
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            view = settings.view_for(PRINTER, data)
            limit = controls.temperature_limit(adapter, view, "nozzle")
            code, body = controls.set_temperature(data, PRINTER,
                                                  {"heater": "nozzle", "target": limit + 1},
                                                  adapter=adapter, read=reads("idle"))
        self.assertLessEqual(limit, 350)
        self.assertEqual(code, 400)
        self.assertIn("outside", body["reason"])
        self.assertEqual(adapter.calls, [])

    def test_family_adapter_temperature_and_bed_fact(self):
        adapter = FakeFamilyAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            code, _ = controls.set_temperature(data, PRINTER, {"heater": "bed", "target": 110},
                                               adapter=adapter, read=reads("idle"))
            self.assertEqual(code, 200)
            code, _ = controls.set_temperature(data, PRINTER, {"heater": "bed", "target": 111},
                                               adapter=adapter, read=reads("idle"))
            self.assertEqual(code, 400)
        self.assertEqual(adapter.calls, [("temp", "bed", 110.0)])

    def test_adapter_without_temperature_path_refuses(self):
        adapter = FakeFamilyAdapter(temps=False)
        with tempfile.TemporaryDirectory() as tmp:
            code, body = controls.set_temperature(enabled(tmp), PRINTER,
                                                  {"heater": "bed", "target": 60},
                                                  adapter=adapter, read=reads("idle"))
        self.assertEqual(code, 501)
        self.assertIn("not supported", body["reason"])

    def test_unreachable_is_502(self):
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = controls.set_temperature(enabled(tmp), PRINTER,
                                               {"heater": "bed", "target": 60},
                                               read=unreachable, send=send)
        self.assertEqual(code, 502)
        self.assertEqual(send.sent, [])


class RouteTests(unittest.TestCase):
    """The HTTP surface: token header, JSON only, 404 shape, real wiring."""

    def start(self, tmp, extra="", environ=None, printer_control_func=None):
        environ = {"WEBUI_ACCESS_TOKEN": TOKEN} if environ is None else environ
        data = load_settings(tmp, "webui:\n  enabled: true\n  port: 8765\n" + extra,
                             environ=environ)
        httpd = server.make_server(data, environ=environ, port=0,
                                   snapshot_func=lambda: {},
                                   printer_control_func=printer_control_func)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        return httpd.server_address[1]

    def post(self, port, path, body=b"{}", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            base = {"Content-Type": "application/json", server.TOKEN_HEADER: TOKEN}
            base.update(headers or {})
            conn.request("POST", path, body=body, headers={k: v for k, v in base.items() if v})
            response = conn.getresponse()
            return response.status, json.loads(response.read().decode() or "{}")
        finally:
            conn.close()

    def test_auth_json_and_dispatch(self):
        seen = []

        def fake(printer_id, verb, body):
            seen.append((printer_id, verb, body))
            return 200, {"ok": True}

        with tempfile.TemporaryDirectory() as tmp:
            port = self.start(tmp, printer_control_func=fake)
            path = f"/api/printers/{PRINTER}/cancel"
            self.assertEqual(self.post(port, path, headers={server.TOKEN_HEADER: ""})[0], 401)
            self.assertEqual(self.post(port, path, headers={server.TOKEN_HEADER: "x"})[0], 401)
            self.assertEqual(self.post(port, path, b"confirm=true", headers={
                "Content-Type": "application/x-www-form-urlencoded"})[0], 415)
            self.assertEqual(self.post(port, path, b"{nope")[0], 400)
            self.assertEqual(self.post(port, f"/api/printers/{PRINTER}/explode")[0], 404)
            self.assertEqual(self.post(port, "/api/printers/a/b/pause")[0], 404)
            code, _ = self.post(port, path, b'{"confirm": true}')
        self.assertEqual(code, 200)
        self.assertEqual(seen, [(PRINTER, "cancel", {"confirm": True})])

    def test_real_wiring_off_by_default_and_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            port = self.start(tmp)
            code, body = self.post(port, f"/api/printers/{PRINTER}/pause")
            self.assertEqual(code, 403)
            self.assertIn("controls_enabled", body["reason"])
        with tempfile.TemporaryDirectory() as tmp:
            port = self.start(tmp, "  controls_enabled: true\n")
            code, body = self.post(port, "/api/printers/no-such-printer/pause")
            self.assertEqual(code, 404)
            self.assertFalse(body["ok"])
            # The default printer names no Moonraker URL: an explicit failure.
            code, body = self.post(port, f"/api/printers/{PRINTER}/pause")
            self.assertEqual(code, 502)
            self.assertIn("unreachable", body["reason"])


if __name__ == "__main__":
    unittest.main()
