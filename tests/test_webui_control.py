"""Web UI control API (#708): per-printer pause / resume / cancel / target.

Everything runs offline: the control module's live read and transport
send are injected, the dispatch helpers are exercised with fake
adapters, and the HTTP routes bind an ephemeral loopback port with
fake route functions (plus one real-wiring test that never reaches a
network: the default settings name no Moonraker URL).
"""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from print_handy import settings
from print_handy.webui import control, server

TOKEN = "webui-test-token-708"

FAKE_SNAPSHOT = {"version": "0.test", "ok": True}

# One roster entry per connection family the control API dispatches.
ROSTER = (
    "printers:\n"
    "  - id: moon\n    model: klipper.generic\n"
    "  - id: bambu\n    model: bambulab.a1\n"
    "  - id: prusa\n    model: prusa.mk4s\n"
    "  - id: elegoo\n    model: elegoo_sdcp.centauricarbon\n"
    "  - id: octo\n    model: octoprint.marlin\n"
    "  - id: stock\n    model: flashforge_stock.ad5x\n"
)

FAMILY_IDS = {  # printer id -> the connection its adapter must report
    "moon": "moonraker",
    "bambu": "bambulab_lan",
    "prusa": "prusalink",
    "elegoo": "elegoo_sdcp",
    "octo": "octoprint",
    "stock": "flashforge_stock",
}

PRINTING = {"state": "printing", "filename": "benchy.gcode", "progress": 0.4,
            "hotend_c": 210.0, "bed_c": 60.0}
PAUSED = {**PRINTING, "state": "paused"}
STOPPED = {**PRINTING, "state": "standby", "progress": None}


def load_settings(tmp, extra="", environ=None):
    local = Path(tmp) / "local.yaml"
    local.write_text(f"paths:\n  hermes_home: {tmp}/hermes\n{extra}")
    return settings.load(local=local, environ={} if environ is None else environ)


def scripted_read(states):
    """A read(adapter, view) that walks the given states, sticking at the last."""
    queue = list(states)

    def read(adapter, view):
        if len(queue) > 1:
            return dict(queue.pop(0))
        return dict(queue[0])

    return read


class RecordingSend:
    def __init__(self):
        self.calls = []

    def __call__(self, adapter, view, action):
        self.calls.append((adapter, view, action))

    @property
    def connections(self):
        return [control._connection_of(adapter) for adapter, _view, _action in self.calls]


class ControlModuleTests(unittest.TestCase):
    """control.printer_control — gates, dispatch, read-back, failures."""

    def run_control(self, data, printer_id, action, states, send=None):
        send = send or RecordingSend()
        code, body = control.printer_control(
            data, printer_id, action, read=scripted_read(states),
            send=send, sleep=lambda _s: None)
        return code, body, send

    def test_pause_dispatches_for_every_family(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = load_settings(tmp, ROSTER)
            for printer_id, connection in FAMILY_IDS.items():
                code, body, send = self.run_control(
                    data, printer_id, "pause", [PRINTING, PAUSED])
                self.assertEqual(code, 200, printer_id)
                self.assertTrue(body["ok"], printer_id)
                self.assertEqual(body["id"], printer_id)
                self.assertEqual(body["connection"], connection)
                self.assertEqual(body["action"], "pause")
                self.assertEqual(body["detail"], "Paused.")
                self.assertEqual(body["status"]["state"], "paused")
                self.assertEqual(send.connections, [connection], printer_id)
                self.assertEqual([call[2] for call in send.calls], ["pause"])

    def test_resume_and_cancel_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = load_settings(tmp, ROSTER)
            code, body, _send = self.run_control(data, "moon", "resume", [PAUSED, PRINTING])
            self.assertEqual(code, 200)
            self.assertEqual(body["detail"], "Resumed.")
            code, body, _send = self.run_control(data, "octo", "cancel", [PRINTING, STOPPED])
            self.assertEqual(code, 200)
            self.assertEqual(body["detail"], "Stopped. Printer now reports standby.")
            self.assertEqual(body["status"]["state"], "standby")

    def test_state_gates_refuse_and_send_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = load_settings(tmp, ROSTER)
            for action, states in (("pause", [PAUSED]), ("resume", [PRINTING]),
                                   ("cancel", [STOPPED]), ("pause", [STOPPED])):
                code, body, send = self.run_control(data, "moon", action, states)
                self.assertEqual(code, 409, action)
                self.assertFalse(body["ok"])
                self.assertIn("nothing sent", body["reason"])
                self.assertEqual(send.calls, [], action)

    def test_unknown_printer_is_404_like_the_status_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = load_settings(tmp, ROSTER)
            code, body, send = self.run_control(data, "nope", "pause", [PRINTING])
        self.assertEqual(code, 404)
        self.assertFalse(body["ok"])
        self.assertIn("nope", body["reason"])
        self.assertEqual(send.calls, [])

    def test_unreachable_printer_is_an_explicit_failure(self):
        def dead_read(adapter, view):
            raise ConnectionError("connection refused")

        with tempfile.TemporaryDirectory() as tmp:
            data = load_settings(tmp, ROSTER)
            send = RecordingSend()
            code, body = control.printer_control(
                data, "moon", "pause", read=dead_read, send=send, sleep=lambda _s: None)
        self.assertEqual(code, 502)
        self.assertFalse(body["ok"])
        self.assertIn("refused", body["reason"])
        self.assertEqual(send.calls, [])

    def test_failed_send_is_an_explicit_failure(self):
        def bad_send(adapter, view, action):
            raise ConnectionError("dropped")

        with tempfile.TemporaryDirectory() as tmp:
            data = load_settings(tmp, ROSTER)
            code, body = control.printer_control(
                data, "moon", "cancel", read=scripted_read([PRINTING]),
                send=bad_send, sleep=lambda _s: None)
        self.assertEqual(code, 502)
        self.assertFalse(body["ok"])
        self.assertIn("failed", body["reason"])

    def test_unsupported_connection_is_a_named_refusal(self):
        def unsupported_send(adapter, view, action):
            raise control.UnsupportedControl("no print control for connection 'offline'")

        with tempfile.TemporaryDirectory() as tmp:
            data = load_settings(tmp, ROSTER)
            code, body = control.printer_control(
                data, "moon", "pause", read=scripted_read([PRINTING]),
                send=unsupported_send, sleep=lambda _s: None)
        self.assertEqual(code, 422)
        self.assertFalse(body["ok"])
        self.assertIn("offline", body["reason"])

    def test_sent_but_unconfirmed_state_is_reported_truthfully(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = load_settings(tmp, ROSTER)
            code, body, _send = self.run_control(data, "moon", "pause", [PRINTING])
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertIn("still reports printing", body["detail"])
        self.assertEqual(body["status"]["state"], "printing")

    def test_unknown_action_is_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = load_settings(tmp, ROSTER)
            code, body, _send = self.run_control(data, "moon", "explode", [PRINTING])
        self.assertEqual(code, 404)
        self.assertFalse(body["ok"])


class FakeAdapter:
    def __init__(self, connection):
        self.connection = connection
        self.controlled = []
        self.targets = None

    def control(self, action):
        self.controlled.append(action)


class OctoFakeAdapter(FakeAdapter):
    def set_targets(self, hotend_c=None, bed_c=None):
        self.targets = (hotend_c, bed_c)


class SendDispatchTests(unittest.TestCase):
    """control._send_action / _send_target — the real transport dispatch."""

    def test_moonraker_actions_post_to_the_print_endpoints(self):
        posts = []
        original = control._moonraker_post
        control._moonraker_post = lambda view, path, body=None: posts.append(path)
        try:
            for action in ("pause", "resume", "cancel"):
                control._send_action(FakeAdapter("moonraker"), {"services": {}}, action)
        finally:
            control._moonraker_post = original
        self.assertEqual(posts, ["/printer/print/pause", "/printer/print/resume",
                                 "/printer/print/cancel"])

    def test_family_actions_translate_cancel_to_stop(self):
        for connection in ("bambulab_lan", "prusalink", "elegoo_sdcp",
                           "octoprint", "flashforge_stock"):
            adapter = FakeAdapter(connection)
            for action in ("pause", "resume", "cancel"):
                control._send_action(adapter, {"services": {}}, action)
            self.assertEqual(adapter.controlled, ["pause", "resume", "stop"], connection)

    def test_offline_and_unknown_connections_refuse(self):
        for connection in ("offline", "carrier_pigeon"):
            with self.assertRaises(control.UnsupportedControl, msg=connection):
                control._send_action(FakeAdapter(connection), {"services": {}}, "pause")

    def test_moonraker_target_sends_heater_script(self):
        posts = []
        original = control._moonraker_post
        control._moonraker_post = lambda view, path, body=None: posts.append((path, body))
        try:
            control._send_target(FakeAdapter("moonraker"), {"services": {}}, 220, 60)
            control._send_target(FakeAdapter("moonraker"), {"services": {}}, None, 65)
        finally:
            control._moonraker_post = original
        self.assertEqual(posts[0][0], "/printer/gcode/script")
        self.assertIn("SET_HEATER_TEMPERATURE HEATER=extruder TARGET=220",
                      posts[0][1]["script"])
        self.assertIn("SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET=60",
                      posts[0][1]["script"])
        self.assertNotIn("extruder", posts[1][1]["script"])
        self.assertIn("heater_bed TARGET=65", posts[1][1]["script"])

    def test_octoprint_target_uses_the_adapter_setter(self):
        adapter = OctoFakeAdapter("octoprint")
        control._send_target(adapter, {"services": {}}, 200, None)
        self.assertEqual(adapter.targets, (200, None))

    def test_other_families_refuse_targets_by_name(self):
        for connection in ("bambulab_lan", "prusalink", "elegoo_sdcp",
                           "flashforge_stock", "offline"):
            with self.assertRaises(control.UnsupportedControl, msg=connection) as caught:
                control._send_target(FakeAdapter(connection), {"services": {}}, 200, 60)
            self.assertIn(connection, str(caught.exception))


class TargetModuleTests(unittest.TestCase):
    """control.printer_target — validation, dispatch result, refusals."""

    def run_target(self, data, printer_id, hotend_c, bed_c, send=None, read=None):
        sends = []

        def recording_send(adapter, view, hotend, bed):
            sends.append((control._connection_of(adapter), hotend, bed))
            if send is not None:
                send(adapter, view, hotend, bed)

        code, body = control.printer_target(
            data, printer_id, hotend_c, bed_c,
            read=read or (lambda adapter, view: dict(PRINTING)), send=recording_send)
        return code, body, sends

    def test_target_success_reports_what_was_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = load_settings(tmp, ROSTER)
            code, body, sends = self.run_target(data, "octo", 210, None)
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["action"], "target")
        self.assertEqual(body["targets"], {"hotend_c": 210})
        self.assertIn("hotend_c 210", body["detail"])
        self.assertEqual(body["status"]["state"], "printing")
        self.assertEqual(sends, [("octoprint", 210, None)])

    def test_target_requires_at_least_one_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = load_settings(tmp, ROSTER)
            code, body, sends = self.run_target(data, "octo", None, None)
        self.assertEqual(code, 400)
        self.assertFalse(body["ok"])
        self.assertEqual(sends, [])

    def test_target_rejects_bad_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = load_settings(tmp, ROSTER)
            for hotend_c, bed_c in (("210", None), (True, None), (float("nan"), None),
                                    (-5, None), (None, 9999), (None, -1)):
                code, body, sends = self.run_target(data, "octo", hotend_c, bed_c)
                self.assertEqual(code, 400, (hotend_c, bed_c))
                self.assertFalse(body["ok"])
                self.assertEqual(sends, [], (hotend_c, bed_c))

    def test_target_allows_zero_to_turn_a_heater_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = load_settings(tmp, ROSTER)
            code, body, _sends = self.run_target(data, "octo", 0, 0)
        self.assertEqual(code, 200)
        self.assertEqual(body["targets"], {"hotend_c": 0, "bed_c": 0})

    def test_target_unsupported_family_is_a_named_refusal(self):
        def refusing_send(adapter, view, hotend, bed):
            raise control.UnsupportedControl(
                "setting temperatures is not supported for connection "
                "'bambulab_lan' through Print Handy")

        with tempfile.TemporaryDirectory() as tmp:
            data = load_settings(tmp, ROSTER)
            code, body = control.printer_target(
                data, "bambu", 210, 60, read=lambda a, v: dict(PRINTING),
                send=refusing_send)
        self.assertEqual(code, 422)
        self.assertFalse(body["ok"])
        self.assertIn("bambulab_lan", body["reason"])

    def test_target_unknown_printer_is_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = load_settings(tmp, ROSTER)
            code, body, _sends = self.run_target(data, "nope", 210, None)
        self.assertEqual(code, 404)
        self.assertFalse(body["ok"])


class ControlsSettingTests(unittest.TestCase):
    """The [webui] controls_enabled setting: default off, bool only."""

    def test_defaults_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = load_settings(tmp)
        self.assertIs(data["webui"]["controls_enabled"], False)

    def test_must_be_a_boolean(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(settings.SettingsError) as caught:
                load_settings(tmp, "webui:\n  controls_enabled: \"yes\"\n")
        self.assertIn("controls_enabled", str(caught.exception))

    def test_environment_override_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = load_settings(
                tmp, environ={"PRINT_HANDY__WEBUI__CONTROLS_ENABLED": "true"})
        self.assertIs(data["webui"]["controls_enabled"], True)


FAKE_LIVE = {"state": "printing", "filename": "benchy.gcode", "progress": 0.42,
             "hotend_c": 219.8, "bed_c": 60.1}


class ControlRouteTests(unittest.TestCase):
    """The HTTP routes — the gate, the header rule, body handling."""

    def start(self, tmp, *, controls=True, environ=None, control_func=None,
              target_func=None):
        extra = ("webui:\n  enabled: true\n  port: 8765\n"
                 + ("  controls_enabled: true\n" if controls else ""))
        data = load_settings(tmp, extra, environ=environ)
        httpd = server.make_server(
            data, environ={} if environ is None else environ, port=0,
            snapshot_func=lambda: FAKE_SNAPSHOT,
            printer_control_func=control_func, printer_target_func=target_func)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        return httpd, httpd.server_address[1]

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

    def post(self, port, path, **kwargs):
        return self.request(port, "POST", path, **kwargs)

    def test_controls_disabled_routes_are_404_and_reads_unaffected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _httpd, port = self.start(tmp, controls=False,
                                      environ={"WEBUI_ACCESS_TOKEN": TOKEN})
            for action in ("pause", "resume", "cancel", "target"):
                code, _headers, body = self.post(
                    port, f"/api/printers/flashforge-ad5x/{action}",
                    body=b"{}" if action == "target" else None,
                    headers={server.TOKEN_HEADER: TOKEN,
                             "Content-Type": "application/json"})
                self.assertEqual(code, 404, action)
                self.assertIn("not enabled", body)
            # The read route answers exactly as before (200, unavailable).
            code, _headers, body = self.request(
                port, "GET", "/api/printers/flashforge-ad5x/status",
                headers={server.TOKEN_HEADER: TOKEN})
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)["status"]["state"], "unavailable")

    def test_header_required_exactly(self):
        calls = []

        def fake_control(printer_id, action):
            calls.append((printer_id, action))
            return 200, {"ok": True, "id": printer_id, "action": action}

        with tempfile.TemporaryDirectory() as tmp:
            _httpd, port = self.start(tmp, environ={"WEBUI_ACCESS_TOKEN": TOKEN},
                                      control_func=fake_control)
            path = "/api/printers/flashforge-ad5x/pause"
            code, _h, _b = self.post(port, path)
            self.assertEqual(code, 401)
            code, _h, _b = self.post(port, path, headers={server.TOKEN_HEADER: "wrong"})
            self.assertEqual(code, 401)
            code, _h, body = self.post(port, path, headers={server.TOKEN_HEADER: TOKEN})
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)["action"], "pause")
            self.assertEqual(calls, [("flashforge-ad5x", "pause")])
            self.assertNotIn(TOKEN, body)

    def test_login_session_does_not_authorize_a_control_post(self):
        calls = []

        def fake_control(printer_id, action):
            calls.append((printer_id, action))
            return 200, {"ok": True}

        with tempfile.TemporaryDirectory() as tmp:
            _httpd, port = self.start(tmp, environ={"WEBUI_ACCESS_TOKEN": TOKEN},
                                      control_func=fake_control)
            code, headers, _b = self.post(
                port, "/login", body=urlencode({"token": TOKEN}),
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(code, 303)
            cookie = headers["set-cookie"].split(";", 1)[0].strip()
            code, _h, _b = self.post(port, "/api/printers/flashforge-ad5x/pause",
                                     headers={"Cookie": cookie})
            self.assertEqual(code, 401)
            self.assertEqual(calls, [])

    def test_target_route_parses_and_dispatches(self):
        seen = []

        def fake_target(printer_id, hotend_c, bed_c):
            seen.append((printer_id, hotend_c, bed_c))
            return 200, {"ok": True, "id": printer_id, "action": "target"}

        with tempfile.TemporaryDirectory() as tmp:
            _httpd, port = self.start(tmp, environ={"WEBUI_ACCESS_TOKEN": TOKEN},
                                      target_func=fake_target)
            path = "/api/printers/flashforge-ad5x/target"
            headers = {server.TOKEN_HEADER: TOKEN, "Content-Type": "application/json"}
            code, _h, body = self.post(port, path, body=b'{"hotend_c": 220, "bed_c": null}',
                                       headers=headers)
            self.assertEqual(code, 200)
            self.assertEqual(seen, [("flashforge-ad5x", 220, None)])
            self.assertTrue(json.loads(body)["ok"])

    def test_target_route_rejects_malformed_bodies(self):
        def fake_target(printer_id, hotend_c, bed_c):  # must never run
            raise AssertionError("dispatched a malformed body")

        with tempfile.TemporaryDirectory() as tmp:
            _httpd, port = self.start(tmp, environ={"WEBUI_ACCESS_TOKEN": TOKEN},
                                      target_func=fake_target)
            path = "/api/printers/flashforge-ad5x/target"
            auth = {server.TOKEN_HEADER: TOKEN}
            code, _h, _b = self.post(port, path, body=b"{}",
                                     headers={**auth, "Content-Type": "text/plain"})
            self.assertEqual(code, 415)
            code, _h, _b = self.post(port, path, body=b"{not json",
                                     headers={**auth, "Content-Type": "application/json"})
            self.assertEqual(code, 400)
            code, _h, _b = self.post(port, path, body=b"[1, 2]",
                                     headers={**auth, "Content-Type": "application/json"})
            self.assertEqual(code, 400)
            code, _h, body = self.post(port, path, body=b'{"chamber_c": 40}',
                                       headers={**auth, "Content-Type": "application/json"})
            self.assertEqual(code, 400)
            self.assertIn("chamber_c", body)

    def test_malformed_control_paths_are_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            _httpd, port = self.start(tmp, environ={"WEBUI_ACCESS_TOKEN": TOKEN},
                                      control_func=lambda pid, a: (200, {"ok": True}))
            for path in ("/api/printers/", "/api/printers/flashforge-ad5x",
                         "/api/printers/flashforge-ad5x/status",
                         "/api/printers/flashforge-ad5x/pause/extra",
                         "/api/printers//pause"):
                code, _h, _b = self.post(port, path, headers={server.TOKEN_HEADER: TOKEN})
                self.assertEqual(code, 404, path)

    def test_real_wiring_unreachable_printer_fails_explicitly(self):
        # No fakes: the default settings name no Moonraker URL, so the
        # module's pre-action read fails and the route answers 502,
        # never a silent success — without any network access.
        with tempfile.TemporaryDirectory() as tmp:
            _httpd, port = self.start(tmp, environ={"WEBUI_ACCESS_TOKEN": TOKEN})
            code, _h, body = self.post(port, "/api/printers/flashforge-ad5x/pause",
                                       headers={server.TOKEN_HEADER: TOKEN})
            self.assertEqual(code, 502)
            self.assertFalse(json.loads(body)["ok"])
            code, _h, body = self.post(port, "/api/printers/nope/pause",
                                       headers={server.TOKEN_HEADER: TOKEN})
            self.assertEqual(code, 404)
            self.assertFalse(json.loads(body)["ok"])


if __name__ == "__main__":
    unittest.main()
