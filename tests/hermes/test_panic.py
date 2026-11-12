"""Tests for print_handy.runtime.panic: force-restart, purge, and status."""
import json
import threading
from unittest import mock

import pytest

from print_handy.runtime import panic
from print_handy.runtime import print_control as pc


# ============================================================================
# Status Tests
# ============================================================================

def test_status_success():
    def mock_caller(url, **kw):
        if "query" in url:
            return {
                "result": {
                    "status": {
                        "print_stats": {"state": "standby"},
                        "webhooks": {"state": "ready"},
                        "extruder": {"temperature": 23.5, "target": 0.0},
                        "heater_bed": {"temperature": 21.0, "target": 0.0},
                        "filament_switch_sensor head_switch_sensor": {"filament_detected": True},
                    }
                }
            }
        raise AssertionError(f"unexpected call: {url}")

    res = panic.get_status(caller=mock_caller)
    assert res["ok"] is True
    assert res["state"] == "standby"
    assert res["klippy"] == "ready"
    assert res["temperatures"]["extruder"]["temperature"] == 23.5
    assert res["temperatures"]["extruder"]["target"] == 0.0
    assert res["temperatures"]["heater_bed"]["temperature"] == 21.0
    assert res["temperatures"]["heater_bed"]["target"] == 0.0
    assert res["head_sensor"]["filament_detected"] is True


def test_status_fallback_server_info_on_query_failure():
    def mock_caller(url, **kw):
        if "query" in url:
            raise RuntimeError("Klippy not connected")
        if "/server/info" in url:
            return {"result": {"klippy_state": "shutdown"}}
        raise AssertionError(f"unexpected call: {url}")

    res = panic.get_status(caller=mock_caller)
    assert res["ok"] is False
    assert res["state"] == "unknown"
    assert res["klippy"] == "shutdown"
    assert "Klippy not connected" in res["error"]


# ============================================================================
# Force Restart Ladder Tests
# ============================================================================

def test_force_restart_ladder_success_at_firmware_restart():
    calls = []

    def mock_caller(url, method="GET", timeout=None, **kw):
        calls.append((url, method))
        return {}

    states = [
        {"klippy": "shutdown", "state": "error"},
        {"klippy": "ready", "state": "standby"},
    ]

    def mock_reader():
        if states:
            return states.pop(0)
        return {"klippy": "ready", "state": "standby"}

    with mock.patch("time.sleep", return_value=None):
        res = panic.force_restart(
            caller=mock_caller,
            status_reader=mock_reader,
            wait_s=10.0,
            poll_s=0.01,
        )

    assert res["ok"] is True
    assert res["steps"] == ["emergency_stop", "firmware_restart", "klippy_ready"]
    assert calls == [
        ("/printer/emergency_stop", "POST"),
        ("/printer/firmware_restart", "POST"),
    ]
    assert res["final_state"]["klippy"] == "ready"


def test_force_restart_falls_back_to_reboot_macro():
    calls = []

    def mock_caller(url, method="GET", data=None, **kw):
        calls.append((url, method, data))
        return {}

    def mock_reader():
        return {"klippy": "shutdown", "state": "error"}

    # Moonraker and native readers for safe_idle
    mr_readings = [
        {"webhooks": {"state": "ready"}, "print_stats": {"state": "standby"}, "extruder": {"target": 0}, "heater_bed": {"target": 0}},
    ]
    nat_readings = [
        {"status": "ready", "errorCode": "", "duration": 0, "progress": 0, "extruderTarget": 0, "bedTarget": 0},
    ]

    with mock.patch("time.sleep", return_value=None), \
         mock.patch("print_handy.core.clear_job.safe_idle", return_value=True):
        res = panic.force_restart(
            caller=mock_caller,
            status_reader=mock_reader,
            moonraker_reader=lambda: mr_readings[0],
            native_reader=lambda: nat_readings[0],
            wait_s=0.05,
            poll_s=0.01,
            reboot_grace_s=0,
            reboot_wait_s=5.0,
        )

    assert res["ok"] is True
    assert "emergency_stop" in res["steps"]
    assert "firmware_restart" in res["steps"]
    assert "reboot_macro" in res["steps"]
    assert "safe_idle" in res["steps"]
    assert any(c[0] == "/printer/gcode/script" and c[2] == {"script": "REBOOT"} for c in calls)


# ============================================================================
# Guarded Purge Tests
# ============================================================================

def test_purge_refuses_slot_parameter():
    res = panic.purge(slot=1)
    assert res["ok"] is False
    assert "unsupported" in res["error"].casefold()
    assert "--slot" in res["error"]


def test_purge_refuses_when_busy_lock():
    pc._CONTROL_LOCK.acquire()
    try:
        res = panic.purge()
        assert res["ok"] is False
        assert "in flight" in res["error"]
    finally:
        pc._CONTROL_LOCK.release()


def test_purge_refuses_when_klippy_not_ready():
    def mock_reader():
        return {
            "print_stats": {"state": "standby"},
            "webhooks": {"state": "shutdown"},
            "filament_switch_sensor head_switch_sensor": {"filament_detected": True},
            "extruder": {"target": 0},
        }

    res = panic.purge(reader=mock_reader)
    assert res["ok"] is False
    assert "shutdown, not ready" in res["error"]


def test_purge_refuses_when_printing_or_paused():
    for active_state in ("printing", "paused"):
        def mock_reader():
            return {
                "print_stats": {"state": active_state},
                "webhooks": {"state": "ready"},
                "filament_switch_sensor head_switch_sensor": {"filament_detected": True},
                "extruder": {"target": 0},
            }

        res = panic.purge(reader=mock_reader)
        assert res["ok"] is False
        assert f"printer is {active_state}" in res["error"]


def test_purge_refuses_when_no_filament_at_head():
    def mock_reader():
        return {
            "print_stats": {"state": "standby"},
            "webhooks": {"state": "ready"},
            "filament_switch_sensor head_switch_sensor": {"filament_detected": False},
            "extruder": {"target": 0},
        }

    res = panic.purge(reader=mock_reader)
    assert res["ok"] is False
    assert "no filament at the head sensor" in res["error"]


def test_purge_success_and_resets_heater():
    scripts_sent = []

    def mock_sender(script):
        scripts_sent.append(script)

    readings = [
        # initial read
        {
            "print_stats": {"state": "standby"},
            "webhooks": {"state": "ready"},
            "filament_switch_sensor head_switch_sensor": {"filament_detected": True},
            "extruder": {"target": 0, "temperature": 25.0},
        },
        # final readback
        {
            "extruder": {"target": 0.0, "temperature": 210.0},
        },
    ]

    def mock_reader():
        return readings.pop(0) if len(readings) > 1 else readings[0]

    res = panic.purge(
        mm=25,
        temp=215,
        reader=mock_reader,
        gcode_sender=mock_sender,
    )

    assert res["ok"] is True
    assert res["mm"] == 25
    assert res["temp"] == 215
    assert res["extruder_target"] == 0.0

    # Verify gcode chunking and heater sequence
    assert "M104 S215" in scripts_sent
    assert "TEMPERATURE_WAIT SENSOR=extruder MINIMUM=210" in scripts_sent
    assert "M83" in scripts_sent
    # 25 mm should be split into 10 + 10 + 5
    assert scripts_sent.count("G1 E10 F150") == 2
    assert scripts_sent.count("G1 E5 F150") == 1
    # Finally block MUST send M104 S0
    assert scripts_sent[-1] == "M104 S0"


def test_purge_clamps_parameters():
    scripts_sent = []

    def mock_sender(script):
        scripts_sent.append(script)

    readings = [
        {
            "print_stats": {"state": "complete"},
            "webhooks": {"state": "ready"},
            "filament_switch_sensor head_switch_sensor": {"filament_detected": True},
            "extruder": {"target": 0},
        },
        {"extruder": {"target": 0}},
    ]

    def mock_reader():
        return readings.pop(0) if len(readings) > 1 else readings[0]

    # Excessive mm is clamped; a low temp is raised to the 180 C floor.
    res = panic.purge(
        mm=999,  # clamped to 150
        temp=120,  # raised to 180
        reader=mock_reader,
        gcode_sender=mock_sender,
    )

    assert res["ok"] is True
    assert res["mm"] == 150
    assert res["temp"] == 180
    assert "M104 S180" in scripts_sent
    # 150 mm chunked into 15 x 10 mm
    assert scripts_sent.count("G1 E10 F150") == 15
    assert scripts_sent[-1] == "M104 S0"


def test_purge_always_resets_heater_on_gcode_error():
    scripts_sent = []

    def mock_sender(script):
        scripts_sent.append(script)
        if "G1" in script:
            raise RuntimeError("Motor driver faulted")

    readings = [
        {
            "print_stats": {"state": "standby"},
            "webhooks": {"state": "ready"},
            "filament_switch_sensor head_switch_sensor": {"filament_detected": True},
            "extruder": {"target": 0},
        },
        {"extruder": {"target": 0}},
    ]

    def mock_reader():
        return readings.pop(0) if len(readings) > 1 else readings[0]

    res = panic.purge(
        mm=20,
        temp=210,
        reader=mock_reader,
        gcode_sender=mock_sender,
    )

    assert res["ok"] is False
    assert "Motor driver faulted" in res["error"]
    assert "M104 S0" in scripts_sent


def test_purge_fails_if_heater_target_not_zero():
    scripts_sent = []

    def mock_sender(script):
        scripts_sent.append(script)

    readings = [
        {
            "print_stats": {"state": "standby"},
            "webhooks": {"state": "ready"},
            "filament_switch_sensor head_switch_sensor": {"filament_detected": True},
            "extruder": {"target": 0},
        },
        {"extruder": {"target": 210.0}},  # failed to reset to 0
    ]

    def mock_reader():
        return readings.pop(0) if len(readings) > 1 else readings[0]

    res = panic.purge(
        mm=10,
        temp=210,
        reader=mock_reader,
        gcode_sender=mock_sender,
    )

    assert res["ok"] is False
    assert "heater target readback is 210.0, not 0" in res["error"]



# ============================================================================
# #1147: one heater ceiling (the printer profile's), refused above, never clamped
# ============================================================================

class _CeilingAdapter:
    """A printer whose profile caps the hotend at `hotend` C."""

    def __init__(self, hotend, config=None):
        self._hotend = hotend
        self.config = config or {"printer": {"nozzle_type": "hardened_steel"}}

    def fact(self, name):
        if name == "max_hotend_c":
            return self._hotend
        raise KeyError(name)


def _idle_reader():
    readings = [
        {"print_stats": {"state": "standby"}, "webhooks": {"state": "ready"},
         "filament_switch_sensor head_switch_sensor": {"filament_detected": True}, "extruder": {"target": 0}},
        {"extruder": {"target": 0}},
    ]
    return lambda: readings.pop(0) if len(readings) > 1 else readings[0]


def test_1147_purge_above_the_profile_ceiling_is_refused_and_sends_nothing():
    sent = []
    res = panic.purge(temp=255, reader=_idle_reader(), gcode_sender=sent.append,
                      adapter=_CeilingAdapter(250), state={})
    assert res["ok"] is False and "above this printer's 250 C nozzle ceiling" in res["error"]
    assert "Nothing sent" in res["error"] and sent == []


def test_1147_purge_at_the_profile_ceiling_runs():
    sent = []
    res = panic.purge(temp=250, reader=_idle_reader(), gcode_sender=sent.append,
                      adapter=_CeilingAdapter(250), state={})
    assert res["ok"] is True and "M104 S250" in sent


def test_1147_purge_ceiling_follows_the_fitted_nozzle_not_a_fixed_260():
    """The old fixed 260 C cap is gone: the ceiling is the fitted nozzle's and the model's."""
    sent = []
    res = panic.purge(temp=280, reader=_idle_reader(), gcode_sender=sent.append,
                      adapter=_CeilingAdapter(300), state={"nozzle": {"type": "hardened_steel"}})
    assert res["ok"] is True and "M104 S280" in sent


def test_1147_the_ad5x_profile_caps_the_purge_at_300_c():
    from print_handy import printers, settings
    ad5x = printers.get("flashforge.ad5x", settings.current())
    sent = []
    res = panic.purge(temp=310, reader=_idle_reader(), gcode_sender=sent.append, adapter=ad5x,
                      state={"nozzle": {"type": "hardened_steel"}})
    assert res["ok"] is False and "300 C nozzle ceiling" in res["error"] and sent == []


# ============================================================================
# #1147: ph-force-restart acts on the printer it is given
# ============================================================================

def test_1147_force_restart_without_an_id_uses_the_default_printers_call_path(monkeypatch):
    calls = []
    monkeypatch.setattr(pc, "_call", lambda path, **kw: calls.append(("default", path)) or {})
    monkeypatch.setattr(pc, "_send", lambda path, **kw: calls.append(("send", path, kw.get("printer_id"))) or {})
    with mock.patch("time.sleep", return_value=None):
        res = panic.force_restart(status_reader=lambda: {"klippy": "ready"}, wait_s=1, poll_s=0.01)
    assert res["ok"] is True
    assert calls == [("default", "/printer/emergency_stop"), ("default", "/printer/firmware_restart")]


def test_1147_force_restart_with_an_id_acts_on_that_printer_only(monkeypatch):
    from print_handy import settings
    monkeypatch.setattr(settings, "by_id", lambda pid, data=None: {"id": pid, "model": "flashforge.ad5x"})
    monkeypatch.setattr(settings, "default_printer_id", lambda data=None: "main")
    calls, read = [], []
    monkeypatch.setattr(pc, "_call", lambda path, **kw: calls.append(("default", path)) or {})
    monkeypatch.setattr(pc, "_send", lambda path, **kw: calls.append(("send", path, kw.get("printer_id"))) or {})
    monkeypatch.setattr(pc, "status", lambda printer_id=None: read.append(printer_id) or {"klippy": "ready"})
    with mock.patch("time.sleep", return_value=None):
        res = panic.force_restart(printer_id="shelf", wait_s=1, poll_s=0.01)
    assert res["ok"] is True
    assert calls == [("send", "/printer/emergency_stop", "shelf"), ("send", "/printer/firmware_restart", "shelf")]
    assert read and set(read) == {"shelf"}


def test_1147_force_restart_cli_takes_a_printer_id_and_refuses_an_unknown_one(monkeypatch, capsys):
    seen = []
    fake = lambda printer_id=None: seen.append(printer_id) or {"ok": True}  # noqa: E731
    with mock.patch.object(panic, "force_restart", fake):
        assert panic.main(["force-restart"]) == 0 and seen == [None]
        assert panic.main(["force-restart", "shelf"]) == 0 and seen[-1] == "shelf"
        assert panic.main(["force-restart", "--printer", "shelf"]) == 0 and seen[-1] == "shelf"
    capsys.readouterr()
    assert panic.main(["force-restart", "no-such-printer"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and "no-such-printer" in out["error"] and "Nothing sent" in out["error"]
