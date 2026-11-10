"""#708 (control endpoints phase): Web UI printer controls.

Fake adapters and fake senders only: nothing here opens a socket to a
printer.
"""
import http.client
import json
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

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

    def test_printer_refusal_is_409_unless_already_in_target_state(self):
        # Moonraker answers an invalid transition with HTTP 400. Re-read:
        # already there (a race with the printer screen) is success, else 409.
        cases = [("pause", "printing", "paused", 200), ("pause", "printing", "printing", 409),
                 ("resume", "paused", "printing", 200), ("resume", "paused", "paused", 409),
                 ("cancel", "printing", "cancelled", 200), ("cancel", "paused", "paused", 409),
                 ("cancel", "printing", "unknown", 409)]
        for action, before, after, expected in cases:
            with self.subTest(action=action, after=after):
                states = iter([before, after])
                sent = []

                def refuse(adapter, view, command):
                    sent.append(command)
                    raise urllib.error.HTTPError("http://moonraker/x", 400, "Bad Request", {}, None)
                with tempfile.TemporaryDirectory() as tmp:
                    code, body = controls.control(
                        enabled(tmp), PRINTER, action, {"confirm": True},
                        read=lambda adapter, view: {"state": next(states)}, send=refuse)
                self.assertEqual(code, expected)
                self.assertEqual(sent, [("control", action)])  # never resent
                self.assertIn(after, body["detail"] if expected == 200 else body["reason"])

    def test_refusal_with_unreadable_printer_is_409(self):
        calls = iter([{"state": "printing"}])

        def read(adapter, view):
            try:
                return next(calls)
            except StopIteration:
                raise OSError("gone") from None

        def refuse(adapter, view, command):
            raise urllib.error.HTTPError("http://moonraker/x", 400, "Bad Request", {}, None)
        with tempfile.TemporaryDirectory() as tmp:
            code, body = controls.control(enabled(tmp), PRINTER, "pause", read=read, send=refuse)
        self.assertEqual(code, 409)
        self.assertIn("unknown", body["reason"])

    def test_printer_server_error_stays_502(self):
        def broken(adapter, view, command):
            raise urllib.error.HTTPError("http://moonraker/x", 500, "Server Error", {}, None)
        with tempfile.TemporaryDirectory() as tmp:
            code, body = controls.control(enabled(tmp), PRINTER, "pause",
                                          read=reads("printing"), send=broken)
        self.assertEqual(code, 502)
        self.assertIn("500", body["reason"])

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

    def _hardware_record(self, tmp, record):
        """Point printer_state at a tmp state root holding ``record`` for PRINTER."""
        from unittest import mock

        from print_handy.core import printer_state
        state = Path(tmp) / "state" / "printer_hardware.json"
        if record is not None:
            target = state.parent / "printers" / PRINTER / "printer_hardware.json"
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps(record))
        return mock.patch.object(printer_state, "STATE", str(state))

    def test_recorded_nozzle_type_wins_over_config(self):
        # P1-125: config says hardened steel (450 C), but the fitted nozzle
        # recorded by /nozzle change is stainless steel (300 C). A 340 C
        # target must be refused; the recorded 300 C cap is the limit.
        adapter = FakeFamilyAdapter(facts={"max_hotend_c": 350, "max_bed_c": 110})
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            view = settings.view_for(PRINTER, data)
            view["printer"]["nozzle_type"] = "hardened_steel"
            record = {"printer": "Test", "nozzle": {"diameter_mm": 0.4,
                                                     "type": "stainless_steel"}}
            with self._hardware_record(tmp, record):
                self.assertEqual(controls.temperature_limit(adapter, view, "nozzle",
                                                            printer_id=PRINTER), 300)
                code, body = controls.set_temperature(data, PRINTER,
                                                      {"heater": "nozzle", "target": 340},
                                                      adapter=adapter, read=reads("idle"),
                                                      send=send)
                self.assertEqual(code, 400)
                self.assertIn("outside 0..300", body["reason"])
                code, _ = controls.set_temperature(data, PRINTER,
                                                   {"heater": "nozzle", "target": 300},
                                                   adapter=adapter, read=reads("idle"),
                                                   send=send)
                self.assertEqual(code, 200)
        self.assertEqual(send.sent, [("temperature", "nozzle", 300.0)])

    def test_no_hardware_record_keeps_config_limit(self):
        # No record at all: the configured nozzle type decides, unchanged.
        adapter = FakeFamilyAdapter(facts={"max_hotend_c": 350, "max_bed_c": 110})
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            view = settings.view_for(PRINTER, data)
            view["printer"]["nozzle_type"] = "hardened_steel"
            with self._hardware_record(tmp, None):
                self.assertEqual(controls.temperature_limit(adapter, view, "nozzle",
                                                            printer_id=PRINTER), 350)
                code, _ = controls.set_temperature(data, PRINTER,
                                                    {"heater": "nozzle", "target": 340},
                                                    adapter=adapter, read=reads("idle"),
                                                    send=send)
                self.assertEqual(code, 200)
        self.assertEqual(send.sent, [("temperature", "nozzle", 340.0)])

    def test_record_without_a_type_keeps_config_limit(self):
        adapter = FakeFamilyAdapter(facts={"max_hotend_c": 350, "max_bed_c": 110})
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            view = settings.view_for(PRINTER, data)
            view["printer"]["nozzle_type"] = "hardened_steel"
            with self._hardware_record(tmp, {"printer": "Test", "nozzle": {}}):
                self.assertEqual(controls.temperature_limit(adapter, view, "nozzle",
                                                            printer_id=PRINTER), 350)

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


class TargetTests(unittest.TestCase):
    """POST /target: the ha-print-handy 0.3.0 body, both heaters at once."""

    def test_both_heaters_sent_in_order(self):
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            code, body = controls.set_targets(enabled(tmp), PRINTER,
                                              {"hotend_c": 200.0, "bed_c": 60.0},
                                              read=reads("idle"), send=send)
        self.assertEqual(code, 200, body)
        self.assertEqual(send.sent, [("temperature", "nozzle", 200.0),
                                     ("temperature", "bed", 60.0)])
        self.assertEqual(body["targets"], {"hotend_c": 200.0, "bed_c": 60.0})

    def test_null_leaves_that_heater_alone(self):
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = controls.set_targets(enabled(tmp), PRINTER,
                                           {"hotend_c": 210.0, "bed_c": None},
                                           read=reads("printing"), send=send)
        self.assertEqual(code, 200)
        self.assertEqual(send.sent, [("temperature", "nozzle", 210.0)])

    def test_bad_or_empty_bodies_send_nothing(self):
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            for bad in ({}, {"hotend_c": None, "bed_c": None}, {"bed_c": True},
                        {"hotend_c": "hot"}, {"bed_c": -1}, {"bed_c": 500},
                        {"hotend_c": 200, "bed_c": 500}, {"hotend_c": 600, "bed_c": 60}):
                code, _ = controls.set_targets(data, PRINTER, bad,
                                               read=reads("idle"), send=send)
                self.assertEqual(code, 400, bad)
        self.assertEqual(send.sent, [])

    def test_off_unknown_and_unreachable_send_nothing(self):
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = controls.set_targets(load_settings(tmp), PRINTER, {"bed_c": 60},
                                           read=reads("idle"), send=send)
            self.assertEqual(code, 403)
            code, _ = controls.set_targets(enabled(tmp), "ghost", {"bed_c": 60},
                                           read=reads("idle"), send=send)
            self.assertEqual(code, 404)
            code, _ = controls.set_targets(enabled(tmp), PRINTER, {"bed_c": 60},
                                           read=unreachable, send=send)
            self.assertEqual(code, 502)
        self.assertEqual(send.sent, [])

    def test_family_without_temperature_path_refuses(self):
        adapter = FakeFamilyAdapter(temps=False)
        with tempfile.TemporaryDirectory() as tmp:
            code, body = controls.set_targets(enabled(tmp), PRINTER, {"bed_c": 60},
                                              adapter=adapter, read=reads("idle"))
        self.assertEqual(code, 501)
        self.assertIn("not supported", body["reason"])

    def test_second_send_failure_names_what_was_already_sent(self):
        calls = []

        def flaky(adapter, view, command):
            calls.append(command)
            if command[1] == "bed":
                raise OSError("reset by peer")

        with tempfile.TemporaryDirectory() as tmp:
            code, body = controls.set_targets(enabled(tmp), PRINTER,
                                              {"hotend_c": 200, "bed_c": 60},
                                              read=reads("idle"), send=flaky)
        self.assertEqual(code, 502)
        self.assertIn("already sent: hotend_c=200", body["reason"])
        self.assertEqual(len(calls), 2)

class FakeMoonrakerAdapter:
    connection = "moonraker"


class FakeOctoAdapter:
    connection = "octoprint"

    def __init__(self):
        self.gcode_lines = []

    def send_gcode(self, lines):
        self.gcode_lines.append(list(lines))
        return {}


def live_objects(state="printing", objects=("cube", "gear", "clip"), excluded=("clip",),
                 current="cube"):
    return lambda adapter, view: {"state": state, "objects": list(objects),
                                  "excluded_objects": list(excluded),
                                  "current_object": current}


class PowerActionTests(unittest.TestCase):
    """ha-print-handy #1: emergency stop and the two restarts."""

    def test_off_is_403_for_every_new_route_and_sends_nothing(self):
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            data = load_settings(tmp)
            for action in controls.POWER_ACTIONS:
                code, body = controls.power_action(data, PRINTER, action, {"confirm": True},
                                                   read=reads("printing"), send=send)
                self.assertEqual(code, 403, action)
                self.assertIn("controls_enabled", body["reason"])
            code, _ = controls.send_gcode(data, PRINTER, {"script": "G28"},
                                          read=reads("idle"), send=send)
            self.assertEqual(code, 403)
            code, _ = controls.exclude_object(data, PRINTER, {"object": "cube", "confirm": True},
                                              read=live_objects(), send=send)
            self.assertEqual(code, 403)
        self.assertEqual(send.sent, [])

    def test_confirm_required_for_each_power_action(self):
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            for action in controls.POWER_ACTIONS:
                for body in (None, {}, {"confirm": "true"}, {"confirm": 1}):
                    code, out = controls.power_action(data, PRINTER, action, body,
                                                      read=reads("printing"), send=send)
                    self.assertEqual(code, 400, (action, body))
                    self.assertIn('"confirm": true', out["reason"])
        self.assertEqual(send.sent, [])

    def test_emergency_stop_has_no_state_gate(self):
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            for state in ("printing", "paused", "idle", "complete", "error", "unknown"):
                code, body = controls.power_action(data, PRINTER, "emergency_stop",
                                                   {"confirm": True}, read=reads(state), send=send)
                self.assertEqual(code, 200, (state, body))
        self.assertEqual(send.sent, [("power", "emergency_stop")] * 6)

    def test_restarts_send_their_power_command(self):
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            for action in ("firmware_restart", "server_restart"):
                code, body = controls.power_action(data, PRINTER, action, {"confirm": True},
                                                   read=reads("idle"), send=send)
                self.assertEqual(code, 200, body)
                self.assertEqual(body["action"], action)
        self.assertEqual(send.sent, [("power", "firmware_restart"), ("power", "server_restart")])

    def test_unreachable_is_502_and_unknown_is_404(self):
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            code, _ = controls.power_action(data, PRINTER, "firmware_restart", {"confirm": True},
                                            read=unreachable, send=send)
            self.assertEqual(code, 502)
            code, _ = controls.power_action(data, "ghost", "emergency_stop", {"confirm": True},
                                            read=reads("idle"), send=send)
            self.assertEqual(code, 404)
        self.assertEqual(send.sent, [])

    def test_server_restart_sends_even_when_status_cannot_be_read(self):
        # Moonraker rejects the objects query while Klippy is
        # disconnected, but /server/restart does not depend on Klippy:
        # it is the recovery control for exactly that failure, so its
        # status read is best-effort and the restart POST decides.
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            code, body = controls.power_action(enabled(tmp), PRINTER, "server_restart",
                                               {"confirm": True}, read=unreachable, send=send)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["from_state"], "unknown")
        self.assertEqual(send.sent, [("power", "server_restart")])

    def test_firmware_restart_still_requires_a_readable_printer(self):
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = controls.power_action(enabled(tmp), PRINTER, "firmware_restart",
                                            {"confirm": True}, read=unreachable, send=send)
            self.assertEqual(code, 502)
        self.assertEqual(send.sent, [])

    def test_emergency_stop_is_never_blocked_by_a_status_read(self):
        """A hung controller is when the stop is needed: no status read is
        made at all (a read that would fail or hang is never called), and
        the stop is sent."""
        def must_not_read(adapter, view):
            raise AssertionError("emergency stop must not wait on a status read")
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            code, body = controls.power_action(enabled(tmp), PRINTER, "emergency_stop",
                                               {"confirm": True}, read=must_not_read, send=send)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["sent"], "emergency_stop")
        self.assertEqual(send.sent, [("power", "emergency_stop")])

    def test_emergency_stop_falls_back_to_m112_and_says_so(self):
        sent = []

        def endpoint_down(adapter, view, command):
            sent.append(command)
            if command[0] == "power":
                raise OSError("timed out")
        with tempfile.TemporaryDirectory() as tmp:
            code, body = controls.power_action(enabled(tmp), PRINTER, "emergency_stop",
                                               {"confirm": True}, read=unreachable,
                                               send=endpoint_down)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["sent"], "M112")
        self.assertIn("M112 was sent", body["detail"])
        self.assertEqual(sent, [("power", "emergency_stop"), ("gcode", "M112")])

    def test_emergency_stop_reports_both_failures_truthfully(self):
        def all_down(adapter, view, command):
            raise OSError("connection refused")
        with tempfile.TemporaryDirectory() as tmp:
            code, body = controls.power_action(enabled(tmp), PRINTER, "emergency_stop",
                                               {"confirm": True}, read=unreachable,
                                               send=all_down)
        self.assertEqual(code, 502)
        self.assertIn("M112 fallback failed", body["reason"])
        self.assertIn("cut its power", body["reason"])

    def test_moonraker_paths(self):
        posts = []
        adapter = FakeMoonrakerAdapter()
        view = {"services": {"moonraker_url": "http://printer:7125"}}
        with mock.patch.object(
                controls, "_moonraker_post",
                lambda v, path, payload=None: posts.append((path, payload))):
            for action, path in (("emergency_stop", "/printer/emergency_stop"),
                                 ("firmware_restart", "/printer/firmware_restart"),
                                 ("server_restart", "/server/restart")):
                controls.default_send(adapter, view, ("power", action))
        self.assertEqual([p for p, _ in posts],
                         ["/printer/emergency_stop", "/printer/firmware_restart", "/server/restart"])

    def test_non_moonraker_power_actions_refuse_501(self):
        adapter = FakeFamilyAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            for action in controls.POWER_ACTIONS:
                code, body = controls.power_action(enabled(tmp), PRINTER, action,
                                                   {"confirm": True}, adapter=adapter,
                                                   read=reads("printing"))
                self.assertEqual(code, 501, action)
                self.assertIn("not supported", body["reason"])
        self.assertEqual(adapter.calls, [])


class SendGcodeTests(unittest.TestCase):
    """ha-print-handy #1: the send-G-code route."""

    def test_moonraker_script_is_sent_verbatim(self):
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            code, body = controls.send_gcode(enabled(tmp), PRINTER, {"script": "G28\nM400"},
                                             read=reads("idle"), send=send)
        self.assertEqual(code, 200, body)
        self.assertEqual(send.sent, [("gcode", "G28\nM400")])
        posts = []
        with mock.patch.object(
                controls, "_moonraker_post",
                lambda v, path, payload=None: posts.append((path, payload))):
            controls.default_send(FakeMoonrakerAdapter(),
                                  {"services": {"moonraker_url": "http://p:7125"}},
                                  ("gcode", "G28"))
        self.assertEqual(posts, [("/printer/gcode/script", {"script": "G28"})])

    def test_validation_refusals_send_nothing(self):
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            for bad in (None, {}, {"script": None}, {"script": 42}, {"script": ""},
                        {"script": "  \n  "}, {"script": "G28" * (controls.GCODE_MAX_CHARS + 1)}):
                code, body = controls.send_gcode(data, PRINTER, bad, read=reads("idle"), send=send)
                self.assertEqual(code, 400, bad if not isinstance(bad, dict)
                                 or len(str(bad.get("script") or "")) < 100 else "oversized")
                self.assertIn("Nothing sent", body["reason"])
        self.assertEqual(send.sent, [])

    def test_octoprint_sends_one_command_per_non_empty_line(self):
        adapter = FakeOctoAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            code, body = controls.send_gcode(enabled(tmp), PRINTER,
                                             {"script": "G28\n\n  M104 S200 \n"},
                                             adapter=adapter, read=reads("idle"))
        self.assertEqual(code, 200, body)
        self.assertEqual(adapter.gcode_lines, [["G28", "M104 S200"]])

    def test_octoprint_client_posts_to_printer_command(self):
        import io

        from print_handy.printers.octoprint.client import OctoPrintClient

        captured = {}

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opener(request, timeout=None):
            captured["url"] = request.full_url
            captured["data"] = request.data
            return Response(b"{}")

        client = OctoPrintClient("http://octo:5000", "key", opener=opener)
        client.send_commands(["G28", "M104 S200"])
        self.assertTrue(captured["url"].endswith("/api/printer/command"))
        self.assertEqual(json.loads(captured["data"]), {"commands": ["G28", "M104 S200"]})

        from print_handy.printers.octoprint.adapter import OctoPrintAdapter

        adapter = object.__new__(OctoPrintAdapter)
        seen = {}

        class FakeClient:
            def send_commands(self, lines):
                seen["lines"] = list(lines)

        adapter.client = lambda: FakeClient()
        adapter.send_gcode(["G28", ""])
        self.assertEqual(seen["lines"], ["G28"])

    def test_other_families_refuse_501(self):
        adapter = FakeFamilyAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            code, body = controls.send_gcode(enabled(tmp), PRINTER, {"script": "G28"},
                                             adapter=adapter, read=reads("idle"))
        self.assertEqual(code, 501)
        self.assertIn("not supported", body["reason"])
        self.assertEqual(adapter.calls, [])

    def test_above_limit_heater_commands_refuse_by_name_and_send_nothing(self):
        # HW-1: the temperature ceiling that governs /temperature also
        # governs heater words inside a raw script.
        adapter = FakeFamilyAdapter(facts={"max_hotend_c": 300, "max_bed_c": 110})
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            view = settings.view_for(PRINTER, data)
            nozzle_limit = controls.temperature_limit(adapter, view, "nozzle", PRINTER)
            bed_limit = controls.temperature_limit(adapter, view, "bed", PRINTER)
            scripts = (
                f"M104 S{nozzle_limit + 100:g}",
                f"M109 S{nozzle_limit + 100:g}",
                f"M109 R{nozzle_limit + 100:g}",
                f"M140 S{bed_limit + 100:g}",
                f"M190 S{bed_limit + 100:g}",
                f"SET_HEATER_TEMPERATURE HEATER=extruder TARGET={nozzle_limit + 100:g}",
                "G28\nM104 S500",  # one bad line refuses the whole script
            )
            for script in scripts:
                code, body = controls.send_gcode(data, PRINTER, {"script": script},
                                                 adapter=adapter, read=reads("idle"),
                                                 send=send)
                self.assertEqual(code, 400, script)
                self.assertIn("outside", body["reason"])
                self.assertIn("Nothing sent", body["reason"])
        self.assertEqual(send.sent, [])

    def test_plus_signed_heater_targets_refuse_by_name_and_send_nothing(self):
        # A leading + is a valid signed target to Klipper/Marlin, so a
        # plus-signed target above the ceiling must refuse exactly like
        # the unsigned form: 400 naming the word, heater, target and
        # limit, with nothing sent.
        adapter = FakeFamilyAdapter(facts={"max_hotend_c": 300, "max_bed_c": 110})
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            view = settings.view_for(PRINTER, data)
            nozzle_limit = controls.temperature_limit(adapter, view, "nozzle", PRINTER)
            bed_limit = controls.temperature_limit(adapter, view, "bed", PRINTER)
            cases = (
                ("M104 S+500", "M104", "nozzle", "500", nozzle_limit),
                ("M109 R+500", "M109", "nozzle", "500", nozzle_limit),
                ("M140 S+200", "M140", "bed", "200", bed_limit),
                ("SET_HEATER_TEMPERATURE HEATER=extruder TARGET=+500",
                 "SET_HEATER_TEMPERATURE", "nozzle", "500", nozzle_limit),
            )
            for script, word, heater, target, limit in cases:
                code, body = controls.send_gcode(data, PRINTER, {"script": script},
                                                 adapter=adapter, read=reads("idle"),
                                                 send=send)
                self.assertEqual(code, 400, script)
                self.assertIn(word, body["reason"])
                self.assertIn(heater, body["reason"])
                self.assertIn(target, body["reason"])
                self.assertIn(f"{limit:g}", body["reason"])
                self.assertIn("outside", body["reason"])
                self.assertIn("Nothing sent", body["reason"])
        self.assertEqual(send.sent, [])

    def test_quoted_heater_parameters_refuse_and_send_nothing(self):
        # Klipper parses extended commands with shlex, so quoted
        # parameter values are valid Klipper and must be checked
        # exactly like unquoted ones.
        adapter = FakeFamilyAdapter(facts={"max_hotend_c": 300, "max_bed_c": 110})
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            view = settings.view_for(PRINTER, data)
            nozzle_limit = controls.temperature_limit(adapter, view, "nozzle", PRINTER)
            bed_limit = controls.temperature_limit(adapter, view, "bed", PRINTER)
            cases = (
                ('SET_HEATER_TEMPERATURE HEATER="extruder" TARGET="500"',
                 "SET_HEATER_TEMPERATURE", "nozzle", "500", nozzle_limit),
                ("SET_HEATER_TEMPERATURE HEATER='heater_bed' TARGET='200'",
                 "SET_HEATER_TEMPERATURE", "bed", "200", bed_limit),
                ('PID_CALIBRATE HEATER="extruder" TARGET="500"',
                 "PID_CALIBRATE", "nozzle", "500", nozzle_limit),
            )
            for script, word, heater, target, limit in cases:
                code, body = controls.send_gcode(data, PRINTER, {"script": script},
                                                 adapter=adapter, read=reads("idle"),
                                                 send=send)
                self.assertEqual(code, 400, script)
                self.assertIn(word, body["reason"])
                self.assertIn(heater, body["reason"])
                self.assertIn(target, body["reason"])
                self.assertIn(f"{limit:g}", body["reason"])
                self.assertIn("outside", body["reason"])
                self.assertIn("Nothing sent", body["reason"])
        self.assertEqual(send.sent, [])

    def test_duplicate_heater_keys_last_value_wins_and_refuses(self):
        # Klipper builds a parameter dict, so the last value of a
        # repeated key is the one that runs; the scan must check that
        # value, not the first one a search happens to find.
        adapter = FakeFamilyAdapter(facts={"max_hotend_c": 300, "max_bed_c": 110})
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            view = settings.view_for(PRINTER, data)
            nozzle_limit = controls.temperature_limit(adapter, view, "nozzle", PRINTER)
            bed_limit = controls.temperature_limit(adapter, view, "bed", PRINTER)
            cases = (
                ("SET_HEATER_TEMPERATURE HEATER=extruder TARGET=100 TARGET=500",
                 "SET_HEATER_TEMPERATURE", "nozzle", "500", nozzle_limit),
                ("SET_HEATER_TEMPERATURE HEATER=extruder HEATER=heater_bed TARGET=200",
                 "SET_HEATER_TEMPERATURE", "bed", "200", bed_limit),
                ("PID_CALIBRATE HEATER=extruder TARGET=100 TARGET=500",
                 "PID_CALIBRATE", "nozzle", "500", nozzle_limit),
            )
            for script, word, heater, target, limit in cases:
                code, body = controls.send_gcode(data, PRINTER, {"script": script},
                                                 adapter=adapter, read=reads("idle"),
                                                 send=send)
                self.assertEqual(code, 400, script)
                self.assertIn(word, body["reason"])
                self.assertIn(heater, body["reason"])
                self.assertIn(target, body["reason"])
                self.assertIn(f"{limit:g}", body["reason"])
                self.assertIn("outside", body["reason"])
                self.assertIn("Nothing sent", body["reason"])
        self.assertEqual(send.sent, [])

    def test_pid_calibrate_targets_refuse_and_send_nothing(self):
        # PID_CALIBRATE heats its named heater to TARGET, so it is
        # held to the same ceiling as SET_HEATER_TEMPERATURE.
        adapter = FakeFamilyAdapter(facts={"max_hotend_c": 300, "max_bed_c": 110})
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            view = settings.view_for(PRINTER, data)
            nozzle_limit = controls.temperature_limit(adapter, view, "nozzle", PRINTER)
            bed_limit = controls.temperature_limit(adapter, view, "bed", PRINTER)
            cases = (
                ("PID_CALIBRATE HEATER=extruder TARGET=500",
                 "PID_CALIBRATE", "nozzle", "500", nozzle_limit),
                ("PID_CALIBRATE HEATER=heater_bed TARGET=200",
                 "PID_CALIBRATE", "bed", "200", bed_limit),
            )
            for script, word, heater, target, limit in cases:
                code, body = controls.send_gcode(data, PRINTER, {"script": script},
                                                 adapter=adapter, read=reads("idle"),
                                                 send=send)
                self.assertEqual(code, 400, script)
                self.assertIn(word, body["reason"])
                self.assertIn(heater, body["reason"])
                self.assertIn(target, body["reason"])
                self.assertIn(f"{limit:g}", body["reason"])
                self.assertIn("outside", body["reason"])
                self.assertIn("Nothing sent", body["reason"])
        self.assertEqual(send.sent, [])

    def test_m303_targets_refuse_and_send_nothing(self):
        # Marlin M303 (PID autotune) heats to its S target: the
        # default extruder is the nozzle, and E-1 selects the bed.
        adapter = FakeFamilyAdapter(facts={"max_hotend_c": 300, "max_bed_c": 110})
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            view = settings.view_for(PRINTER, data)
            nozzle_limit = controls.temperature_limit(adapter, view, "nozzle", PRINTER)
            bed_limit = controls.temperature_limit(adapter, view, "bed", PRINTER)
            cases = (
                ("M303 S500", "M303", "nozzle", "500", nozzle_limit),
                ("M303 E-1 S200", "M303", "bed", "200", bed_limit),
            )
            for script, word, heater, target, limit in cases:
                code, body = controls.send_gcode(data, PRINTER, {"script": script},
                                                 adapter=adapter, read=reads("idle"),
                                                 send=send)
                self.assertEqual(code, 400, script)
                self.assertIn(word, body["reason"])
                self.assertIn(heater, body["reason"])
                self.assertIn(target, body["reason"])
                self.assertIn(f"{limit:g}", body["reason"])
                self.assertIn("outside", body["reason"])
                self.assertIn("Nothing sent", body["reason"])
        self.assertEqual(send.sent, [])

    def test_exponent_form_heater_targets_refuse_and_send_nothing(self):
        # A parser that accepts scientific notation reads S5e2 as
        # 500 °C, so the scan must read the exponent too: checking the
        # mantissa alone (5 °C) would wave an over-limit target
        # through. Every exponent spelling — e/E, signed exponent,
        # signed mantissa, R as well as S, a leading-dot mantissa,
        # M303 as well as the set words — is checked at the value the
        # loosest parser would act on.
        adapter = FakeFamilyAdapter(facts={"max_hotend_c": 300, "max_bed_c": 110})
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            view = settings.view_for(PRINTER, data)
            nozzle_limit = controls.temperature_limit(adapter, view, "nozzle", PRINTER)
            bed_limit = controls.temperature_limit(adapter, view, "bed", PRINTER)
            cases = (
                ("M104 S5e2", "M104", "nozzle", 500, nozzle_limit),
                ("M104 S5E2", "M104", "nozzle", 500, nozzle_limit),
                ("M104 S5e+2", "M104", "nozzle", 500, nozzle_limit),
                ("M104 S+5e2", "M104", "nozzle", 500, nozzle_limit),
                ("M104 S1e3", "M104", "nozzle", 1000, nozzle_limit),
                ("M104 S.5e3", "M104", "nozzle", 500, nozzle_limit),
                ("M109 S5e2", "M109", "nozzle", 500, nozzle_limit),
                ("M109 R5e2", "M109", "nozzle", 500, nozzle_limit),
                ("M140 S2e2", "M140", "bed", 200, bed_limit),
                ("M190 S2E2", "M190", "bed", 200, bed_limit),
                ("M303 S5e2", "M303", "nozzle", 500, nozzle_limit),
                # An exponent-form E still selects the M303 heater:
                # -0.1e1 is -1, the bed, whose limit is the lower one.
                ("M303 E-0.1e1 S200", "M303", "bed", 200, bed_limit),
                ("SET_HEATER_TEMPERATURE HEATER=extruder TARGET=5e2",
                 "SET_HEATER_TEMPERATURE", "nozzle", 500, nozzle_limit),
            )
            for script, word, heater, target, limit in cases:
                code, body = controls.send_gcode(data, PRINTER, {"script": script},
                                                 adapter=adapter, read=reads("idle"),
                                                 send=send)
                self.assertEqual(code, 400, script)
                self.assertIn(word, body["reason"])
                self.assertIn(heater, body["reason"])
                self.assertIn(f"{target:g}", body["reason"])
                self.assertIn(f"{limit:g}", body["reason"])
                self.assertIn("outside", body["reason"])
                self.assertIn("Nothing sent", body["reason"])
        self.assertEqual(send.sent, [])

    def test_exponent_form_targets_at_or_under_the_limit_pass(self):
        # Exponent forms whose true value is inside the ceiling are
        # ordinary targets and send exactly like their plain spellings.
        adapter = FakeFamilyAdapter(facts={"max_hotend_c": 300, "max_bed_c": 110})
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            view = settings.view_for(PRINTER, data)
            nozzle_limit = controls.temperature_limit(adapter, view, "nozzle", PRINTER)
            bed_limit = controls.temperature_limit(adapter, view, "bed", PRINTER)
            scripts = (
                "M104 S2e2",       # 200
                "M104 S25e1",      # 250
                "M104 S5e-1",      # 0.5
                f"M104 S{nozzle_limit / 100:g}e2",  # at the nozzle limit
                "M140 S1e2",       # 100
                f"M140 S{bed_limit / 100:g}e2",     # at the bed limit
                "M303 S2e2",       # 200
                "M303 E-1e0 S60",  # E is -1: the bed, 60
                "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=2e2",  # 200
            )
            for script in scripts:
                code, body = controls.send_gcode(data, PRINTER, {"script": script},
                                                 adapter=adapter, read=reads("idle"),
                                                 send=send)
                self.assertEqual(code, 200, (script, body))
        self.assertEqual(send.sent, [("gcode", script) for script in scripts])

    # Every spelling below is one a firmware still executes as a heater
    # command (Klipper: uppercased letter/value pairs, spaces stripped,
    # '*' read as one more letter; Marlin: spaces skipped), so each must
    # refuse with nothing sent. (script, word in the reason, heater label)
    BYPASS_FORMS = (
        ("M104 S 500", "M104", "nozzle"),
        ("M 104 S500", "M104", "nozzle"),
        ("M104\tS500", "M104", "nozzle"),
        ("M104 S\t500", "M104", "nozzle"),
        ("M104 *S500", "M104", "nozzle"),
        ("M104S500", "M104", "nozzle"),
        ("m104 s500", "M104", "nozzle"),
        ("M0104 S500", "M104", "nozzle"),
        ("N10 M104 S500*45", "M104", "nozzle"),
        ("M104 S100 S500", "M104", "nozzle"),
        ("M104 S200 B500", "M104", "nozzle"),
        ("M109 R 500", "M109", "nozzle"),
        ("M140 S 200", "M140", "bed"),
        ("M 190 S200", "M190", "bed"),
        ("M141 S100", "M141", "chamber"),
        ("M191 S100", "M191", "chamber"),
        ("M191 R100", "M191", "chamber"),
        ("M303 E-2 S100", "M303", "chamber"),
        ("SET_HEATER_TEMPERATURE HEATER=chamber TARGET=100",
         "SET_HEATER_TEMPERATURE", "chamber"),
        ("SET_HEATER_TEMPERATURE HEATER=filament_dryer TARGET=100",
         "SET_HEATER_TEMPERATURE", "heater filament_dryer"),
        ("PID_CALIBRATE HEATER=my_heater TARGET=100", "PID_CALIBRATE", "heater my_heater"),
        ("PRINT_START EXTRUDER=500 BED=60", "PRINT_START EXTRUDER", "nozzle"),
        ("PRINT_START BED=200", "PRINT_START BED", "bed"),
        ("PRINT_START BED_TEMP=200", "PRINT_START BED_TEMP", "bed"),
        ("print_start extruder_temp='500'", "PRINT_START EXTRUDER_TEMP", "nozzle"),
        ("LOAD_FILAMENT TEMP=500", "LOAD_FILAMENT TEMP", "nozzle"),
        ("START_PRINT HOTEND_TEMP=500", "START_PRINT HOTEND_TEMP", "nozzle"),
        ("PRINT_START CHAMBER=100", "PRINT_START CHAMBER", "chamber"),
        ("S3D_START BED=200", "S3D_START BED", "bed"),
        ('PRINT_START BED="200', "PRINT_START BED", "bed"),  # unbalanced quote
    )
    # Ordinary scripts that must keep sending.
    ALLOWED_FORMS = (
        "M104 S 200", "M 140 S60", "M141 S40", "M191 R45", "M303 E-2 S50",
        "PRINT_START EXTRUDER=220 BED=60 CHAMBER=40",
        "ACTIVATE_EXTRUDER EXTRUDER=extruder1",
        "SET_PRESSURE_ADVANCE EXTRUDER=extruder ADVANCE=0.05",
        "MY_MACRO TEMP_OFFSET=-5",
        "SET_HEATER_TEMPERATURE HEATER=filament_dryer TARGET=50",
        "G1 X10 S500", "M106 S255", "M117 heating to 500",
        "SET_FAN_SPEED FAN=part SPEED=0.5",
        "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=200 ; TARGET=500",
    )

    def test_every_firmware_spelling_of_an_over_limit_heater_refuses(self):
        adapter = FakeFamilyAdapter(facts={"max_hotend_c": 300, "max_bed_c": 110})
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            for script, word, label in self.BYPASS_FORMS:
                with self.subTest(script=script):
                    code, body = controls.send_gcode(data, PRINTER, {"script": script},
                                                     adapter=adapter, read=reads("idle"),
                                                     send=send)
                    self.assertEqual(code, 400, body)
                    self.assertIn(word, body["reason"])
                    self.assertIn(f"the {label} to", body["reason"])
                    self.assertIn("Nothing sent", body["reason"])
        self.assertEqual(send.sent, [])

    def test_ordinary_scripts_with_temperature_like_words_still_send(self):
        adapter = FakeFamilyAdapter(facts={"max_hotend_c": 300, "max_bed_c": 110})
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            for script in self.ALLOWED_FORMS:
                with self.subTest(script=script):
                    code, body = controls.send_gcode(data, PRINTER, {"script": script},
                                                     adapter=adapter, read=reads("idle"),
                                                     send=send)
                    self.assertEqual(code, 200, body)
        self.assertEqual(len(send.sent), len(self.ALLOWED_FORMS))

    def test_chamber_and_unknown_heater_ceilings_come_from_one_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            view = settings.view_for(PRINTER, enabled(tmp))
            plain = FakeFamilyAdapter(facts={"max_hotend_c": 300, "max_bed_c": 110})
            heated = FakeFamilyAdapter(facts={"max_hotend_c": 300, "max_bed_c": 110,
                                              "max_chamber_c": 65})
            self.assertEqual(controls.temperature_limit(plain, view, "chamber"),
                             controls.UNKNOWN_HEATER_CEILING_C)
            self.assertEqual(controls.temperature_limit(heated, view, "chamber"), 65)
            self.assertEqual(controls.temperature_limit(heated, view, "other"),
                             controls.UNKNOWN_HEATER_CEILING_C)

    def test_malformed_extended_heater_lines_refuse_and_send_nothing(self):
        # A SET_HEATER_TEMPERATURE / PID_CALIBRATE line the scan cannot
        # parse fails closed instead of being sent unchecked.
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            for script in ('SET_HEATER_TEMPERATURE HEATER="extruder TARGET=500',
                           "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=hot",
                           "PID_CALIBRATE HEATER=extruder TARGET=hot"):
                code, body = controls.send_gcode(data, PRINTER, {"script": script},
                                                 read=reads("idle"), send=send)
                self.assertEqual(code, 400, script)
                self.assertIn("Nothing sent", body["reason"])
        self.assertEqual(send.sent, [])

    def test_at_limit_under_limit_and_off_heater_commands_pass(self):
        adapter = FakeFamilyAdapter(facts={"max_hotend_c": 300, "max_bed_c": 110})
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            view = settings.view_for(PRINTER, data)
            nozzle_limit = controls.temperature_limit(adapter, view, "nozzle", PRINTER)
            bed_limit = controls.temperature_limit(adapter, view, "bed", PRINTER)
            scripts = (
                f"M104 S{nozzle_limit:g}",  # at the limit is allowed, as on /temperature
                "M104 S0",  # turning a heater off is always allowed
                "M104 S200",
                f"M140 S{bed_limit:g}",
                f"SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET={bed_limit:g}",
                'SET_HEATER_TEMPERATURE HEATER="extruder" TARGET=200',
                "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=500 TARGET=100",
                "PID_CALIBRATE HEATER=extruder TARGET=200",
                "M303 S200",
                "M303 E-1 S60",
            )
            for script in scripts:
                code, body = controls.send_gcode(data, PRINTER, {"script": script},
                                                 adapter=adapter, read=reads("idle"),
                                                 send=send)
                self.assertEqual(code, 200, (script, body))
        self.assertEqual(send.sent, [("gcode", script) for script in scripts])

    def test_comment_words_and_unheated_scripts_are_unaffected(self):
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            for script, state in (("G1 X10 ; M104 S500", "idle"),  # a comment sets nothing
                                  ("G28\nM400", "printing"),  # unheated, mid-print
                                  ("M1040 S500", "idle")):  # not a heater word
                code, body = controls.send_gcode(data, PRINTER, {"script": script},
                                                 read=reads(state), send=send)
                self.assertEqual(code, 200, (script, body))
        self.assertEqual(len(send.sent), 3)

    def test_state_gate_refuses_states_that_cannot_accept_commands(self):
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            for state in ("unknown", "error", "shutdown", "unavailable"):
                code, body = controls.send_gcode(data, PRINTER, {"script": "G28"},
                                                 read=reads(state), send=send)
                self.assertEqual(code, 409, state)
                self.assertIn(state, body["reason"])
                self.assertIn("Nothing sent", body["reason"])
            for state in ("idle", "printing", "paused"):
                code, _ = controls.send_gcode(data, PRINTER, {"script": "G28"},
                                              read=reads(state), send=send)
                self.assertEqual(code, 200, state)
        self.assertEqual(send.sent, [("gcode", "G28")] * 3)


class ExcludeObjectTests(unittest.TestCase):
    """ha-print-handy #1: exclude-object through core.exclude_object."""

    def test_confirmed_exclude_sends_exclude_object_script(self):
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            code, body = controls.exclude_object(enabled(tmp), PRINTER,
                                                 {"object": "gear", "confirm": True},
                                                 read=live_objects(), send=send)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["object"], "gear")
        self.assertEqual(send.sent, [("gcode", "EXCLUDE_OBJECT NAME=gear")])

    def test_no_confirm_is_400_and_sends_nothing(self):
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            for body in ({"object": "gear"}, {"object": "gear", "confirm": "true"}):
                code, out = controls.exclude_object(enabled(tmp), PRINTER, body,
                                                    read=live_objects(), send=send)
                self.assertEqual(code, 400)
                self.assertIn('"confirm": true', out["reason"])
        self.assertEqual(send.sent, [])

    def test_last_remaining_object_is_409_and_sends_nothing(self):
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            # "clip" is already excluded, so "gear" is the last one left.
            code, body = controls.exclude_object(
                enabled(tmp), PRINTER, {"object": "gear", "confirm": True},
                read=live_objects(objects=("gear", "clip"), excluded=("clip",)), send=send)
        self.assertEqual(code, 409)
        self.assertIn("last object", body["reason"])
        self.assertEqual(send.sent, [])

    def test_unknown_or_already_excluded_object_refuses(self):
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            for name in ("nope", "clip", ""):
                code, body = controls.exclude_object(data, PRINTER,
                                                     {"object": name, "confirm": True},
                                                     read=live_objects(), send=send)
                self.assertEqual(code, 400, name)
                self.assertIn("Nothing sent", body["reason"])
        self.assertEqual(send.sent, [])

    def test_non_moonraker_refuses_501(self):
        adapter = FakeFamilyAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            code, body = controls.exclude_object(enabled(tmp), PRINTER,
                                                 {"object": "gear", "confirm": True},
                                                 adapter=adapter, read=live_objects())
        self.assertEqual(code, 501)
        self.assertIn("Klipper", body["reason"])
        self.assertEqual(adapter.calls, [])

    def test_unreachable_is_502(self):
        send = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = controls.exclude_object(enabled(tmp), PRINTER,
                                              {"object": "gear", "confirm": True},
                                              read=unreachable, send=send)
        self.assertEqual(code, 502)
        self.assertEqual(send.sent, [])


class ObjectStatusTests(unittest.TestCase):
    """ha-print-handy #1: object data in the normalized status."""

    def test_klipper_queries_exclude_object(self):
        from print_handy.printers.klipper.adapter import KlipperAdapter
        adapter = object.__new__(KlipperAdapter)
        self.assertIn("exclude_object", adapter.moonraker_objects())

    def test_every_moonraker_adapter_queries_exclude_object(self):
        # The ZMod and Snapmaker adapters carry their own object lists:
        # without the query their printers normalize to empty object
        # lists and every exclude request would refuse as unknown.
        from print_handy.printers.flashforge.adapter import (
            ZmodAdapter,
            ZmodMultiToolAdapter,
            ZmodSingleExtruderAdapter,
        )
        from print_handy.printers.snapmaker.adapter import SnapmakerAdapter
        for cls in (ZmodAdapter, ZmodSingleExtruderAdapter, ZmodMultiToolAdapter,
                    SnapmakerAdapter):
            adapter = object.__new__(cls)
            adapter.config = {"printer": {"tool_count": 2}}
            self.assertIn("exclude_object", adapter.moonraker_objects(), cls.__name__)

    def test_basic_status_normalizes_object_entries(self):
        from print_handy.printers.base import basic_status
        out = basic_status({"print_stats": {"state": "printing"},
                            "exclude_object": {
                                "current_object": "cube",
                                "objects": [{"name": "cube", "center": [1, 2]},
                                            {"name": "gear"}, "clip", {"no_name": 1}],
                                "excluded_objects": ["clip"]}})
        self.assertEqual(out["current_object"], "cube")
        self.assertEqual(out["objects"], ["cube", "gear", "clip"])
        self.assertEqual(out["excluded_objects"], ["clip"])

    def test_basic_status_object_defaults(self):
        from print_handy.printers.base import basic_status
        out = basic_status({})
        self.assertIsNone(out["current_object"])
        self.assertEqual(out["objects"], [])
        self.assertEqual(out["excluded_objects"], [])


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
            self.assertEqual(self.post(port, f"/api/printers/{PRINTER}/target",
                                       b'{"hotend_c": null, "bed_c": 60}')[0], 200)
        self.assertEqual(code, 200)
        self.assertEqual(seen, [(PRINTER, "cancel", {"confirm": True}),
                                (PRINTER, "target", {"hotend_c": None, "bed_c": 60})])

    def test_new_verbs_dispatch(self):
        seen = []

        def fake(printer_id, verb, body):
            seen.append((printer_id, verb, body))
            return 200, {"ok": True}

        with tempfile.TemporaryDirectory() as tmp:
            port = self.start(tmp, printer_control_func=fake)
            for verb, payload in (("emergency_stop", b'{"confirm": true}'),
                                  ("firmware_restart", b'{"confirm": true}'),
                                  ("server_restart", b'{"confirm": true}'),
                                  ("gcode", b'{"script": "G28"}'),
                                  ("exclude", b'{"object": "gear", "confirm": true}')):
                code, _ = self.post(port, f"/api/printers/{PRINTER}/{verb}", payload)
                self.assertEqual(code, 200, verb)
        self.assertEqual([verb for _, verb, _ in seen],
                         ["emergency_stop", "firmware_restart", "server_restart",
                          "gcode", "exclude"])

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
