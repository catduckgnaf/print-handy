"""Unit tests for guarded idle IFS slot loading (Part B)."""
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from print_handy.runtime import print_control as pc
from print_handy.runtime import slot_load


def test_slot_load_refuses_invalid_slot():
    for invalid in [0, 5, -1, "foo", None]:
        res = slot_load.load_slot(invalid)
        assert res["ok"] is False
        assert "integer from 1 through 4" in res["error"]


def test_slot_load_refuses_when_control_lock_held():
    pc._CONTROL_LOCK.acquire()
    try:
        res = slot_load.load_slot(1)
        assert res["ok"] is False
        assert "in flight" in res["error"]
    finally:
        pc._CONTROL_LOCK.release()


def test_slot_load_refuses_while_printing():
    def mock_reader():
        return {
            "print_stats": {"state": "printing"},
            "webhooks": {"state": "ready"},
            "save_variables": {"variables": {"display_off": 0}},
        }

    res = slot_load.load_slot(2, reader=mock_reader)
    assert res["ok"] is False
    assert "cannot load slot while printer is printing" in res["error"]


def test_slot_load_refuses_while_paused():
    def mock_reader():
        return {
            "print_stats": {"state": "paused"},
            "webhooks": {"state": "ready"},
            "save_variables": {"variables": {"display_off": 0}},
        }

    res = slot_load.load_slot(2, reader=mock_reader)
    assert res["ok"] is False
    assert "cannot load slot while printer is paused" in res["error"]


def test_slot_load_refuses_when_klippy_not_ready():
    def mock_reader():
        return {
            "print_stats": {"state": "standby"},
            "webhooks": {"state": "shutdown"},
            "save_variables": {"variables": {"display_off": 0}},
        }

    res = slot_load.load_slot(2, reader=mock_reader)
    assert res["ok"] is False
    assert "Klipper is shutdown, not ready" in res["error"]


def test_slot_load_native_screen_mode():
    sent_scripts = []

    def mock_reader():
        return {
            "print_stats": {"state": "standby"},
            "webhooks": {"state": "ready"},
            "save_variables": {"variables": {"display_off": 0}},
        }

    def mock_sender(script):
        sent_scripts.append(script)

    res = slot_load.load_slot(3, reader=mock_reader, gcode_sender=mock_sender)
    assert res["ok"] is True
    assert res["slot"] == 3
    assert res["display_off"] == 0
    assert len(sent_scripts) == 1
    script = sent_scripts[0]
    assert "REMOVE_PRUTOK_IFS" in script
    assert "INSERT_PRUTOK_IFS" in script
    assert "SET_EXTRUDER_SLOT SLOT=3" in script


def test_slot_load_screenless_display_off_mode():
    sent_scripts = []

    def mock_reader():
        return {
            "print_stats": {"state": "standby"},
            "webhooks": {"state": "ready"},
            "save_variables": {"variables": {"display_off": 1}},
        }

    def mock_sender(script):
        sent_scripts.append(script)

    res = slot_load.load_slot(4, reader=mock_reader, gcode_sender=mock_sender)
    assert res["ok"] is True
    assert res["slot"] == 4
    assert res["display_off"] == 1
    assert len(sent_scripts) == 1
    script = sent_scripts[0]
    assert "IFS_F11" in script
    assert "IFS_F10" in script
    assert "SET_EXTRUDER_SLOT SLOT=4" in script


def _ready_status(variables_marker):
    """Build an otherwise ready/idle status; variables_marker controls display_off."""
    status = {
        "print_stats": {"state": "standby"},
        "webhooks": {"state": "ready"},
    }
    if variables_marker != "no_save_variables":
        if variables_marker == "no_display_off":
            status["save_variables"] = {"variables": {}}
        elif variables_marker == "no_variables":
            status["save_variables"] = {}
        else:
            status["save_variables"] = {"variables": {"display_off": variables_marker}}
    return status


def test_slot_load_refuses_when_display_off_missing():
    # P1-34: a missing display_off must not default to native-screen mode.
    for marker in ["no_display_off", "no_variables", "no_save_variables"]:
        sent_scripts = []

        def mock_reader(marker=marker):
            return _ready_status(marker)

        def mock_sender(script):
            sent_scripts.append(script)

        res = slot_load.load_slot(2, reader=mock_reader, gcode_sender=mock_sender)
        assert res["ok"] is False, marker
        assert "display" in res["error"].lower(), marker
        assert "unknown" in res["error"].lower(), marker
        assert "Nothing sent" in res["error"], marker
        assert sent_scripts == [], marker


@pytest.mark.parametrize(
    "garbage",
    [2, -1, "0", "1", "unknown", None, 1.0, 0.0, True, False, [], {}],
)
def test_slot_load_refuses_when_display_off_is_garbage(garbage):
    # P1-34: only an explicit integer 0 or 1 is a known mode; anything else
    # (including values that merely compare equal to 0/1) must fail closed.
    sent_scripts = []

    def mock_reader():
        return _ready_status(garbage)

    def mock_sender(script):
        sent_scripts.append(script)

    res = slot_load.load_slot(2, reader=mock_reader, gcode_sender=mock_sender)
    assert res["ok"] is False, garbage
    assert "display" in res["error"].lower(), garbage
    assert "unknown" in res["error"].lower(), garbage
    assert "Nothing sent" in res["error"], garbage
    assert sent_scripts == [], garbage


def test_slot_load_refuses_unknown_display_mode_via_caller_without_sending():
    # P1-34 on the caller path: no REMOVE/INSERT (or screenless) script call
    # may be issued when the query response carries no display_off.
    calls = []

    def mock_caller(path, method=None, data=None, timeout=None):
        calls.append(path)
        return {
            "result": {
                "status": {
                    "print_stats": {"state": "standby"},
                    "webhooks": {"state": "ready"},
                    "save_variables": {"variables": {}},
                }
            }
        }

    res = slot_load.load_slot(1, caller=mock_caller)
    assert res["ok"] is False
    assert "unknown" in res["error"].lower()
    assert "Nothing sent" in res["error"]
    assert not any("gcode/script" in c for c in calls)


def test_pb_load_slot_cli_script_invocations():
    bin_path = Path(__file__).resolve().parents[2] / "bin" / "ph-load-slot"
    assert bin_path.is_file()

    # Test invalid slot CLI. Parsing leaves validation to load_slot so failures
    # retain the stable JSON contract used by Home Assistant callers.
    run_invalid = subprocess.run(
        [sys.executable, str(bin_path), "5", "--json"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    assert run_invalid.returncode != 0
    data = json.loads(run_invalid.stdout)
    assert data["ok"] is False
    assert "integer from 1 through 4" in data["error"]
