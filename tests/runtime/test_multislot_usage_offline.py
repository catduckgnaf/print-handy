"""A multi-slot print that finished while the listener was down is charged once.

The listener charges a multi-slot print's spools at its live end transition.
A print that ends while the listener is down or hung has no such transition;
the announcer's missed-finish check (#1359) posts it from Moonraker history,
and must charge it from there too, through the same ledger, so a later live
end or retry never charges it again. Case: tool 0 -> slot 2, tool 1 -> slot 3,
`; filament used [g] = 6.80, 28.15`. This file does not import the charging
module, so it runs (and fails) on a base without it.
"""
import asyncio
import json
import time
from datetime import datetime, timezone

import pytest

from print_handy.core import spoolman_client
from print_handy.runtime import moonraker_discord_announcements as watcher
from print_handy.runtime import moonraker_event_listener as mel
from print_handy.runtime import print_records as records

_REAL_TIME = time.time
FILE = "dragon-two-color.gcode"
PLANNED_MM = 11627.68


def _job(path, state, completion):
    file_info = {"path": path, "display": path, "name": path} if path else {"name": None}
    return {"job": {"file": file_info}, "progress": {"completion": completion},
            "state": state, "stats": {"state": state.lower()}}


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _setup(tmp_path, monkeypatch, *, status, filament_used=None, runout=None):
    ended = _REAL_TIME() - 20 * 60
    started = ended - 3600
    context = {
        "job_key": FILE, "print_number": 41, "started_at": _iso(started - 120),
        "tool_to_physical_slot": {"0": 2, "1": 3},
        "slicer_settings": {"filament_used_g_list": [6.8, 28.15],
                            "mapped_materials": {"0": {"spoolman_id": 12}, "1": {"spoolman_id": 15}}},
        "planned_filament_mm": PLANNED_MM,
    }
    (tmp_path / "context.json").write_text(json.dumps({"jobs": {FILE: context}}))
    monkeypatch.setenv("PRINT_HANDY_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.setenv("PRINT_HANDY_MULTISLOT_USAGE_STATE", str(tmp_path / "ledger.json"))
    runout_path = tmp_path / "runout_pause.json"
    monkeypatch.setenv("RUNOUT_PAUSE_STATE", str(runout_path))
    if runout:
        runout_path.write_text(json.dumps(runout))
    entry = {"job_id": "0001A", "filename": FILE, "status": status, "start_time": started,
             "end_time": ended, "print_duration": 3500}
    if filament_used is not None:
        entry["filament_used"] = filament_used
    history = {"result": {"jobs": [entry]}}
    responses = iter([_job(FILE, "Printing", 40.0)] + [_job("", "Operational", None)] * 4)
    monkeypatch.setattr(records, "PRINTS_DIR", tmp_path / "Prints")
    monkeypatch.setattr(watcher, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(watcher, "get_job", lambda: next(responses))
    monkeypatch.setattr(watcher, "get_json",
                        lambda path, timeout=10: history if path.startswith("/server/history") else {})
    monkeypatch.setattr(watcher, "get_active_spool", lambda: {})
    monkeypatch.setattr(watcher, "get_file_metadata", lambda filename: {})
    monkeypatch.setattr(watcher.print_summary, "format_event",
                        lambda kind, filename, **_kw: f"{kind}: {filename}")
    calls = []

    def fake(method, path, body=None, **kw):
        calls.append((method, path, body))
        return {}
    monkeypatch.setattr(spoolman_client, "request", fake)
    return calls


def _listener_down_then_back(monkeypatch, capsys):
    monkeypatch.setattr(time, "time", lambda real=_REAL_TIME: real() - 3600)  # first seen an hour ago
    watcher.main()
    capsys.readouterr()
    monkeypatch.setattr(time, "time", _REAL_TIME)
    watcher.main()  # the listener is back: the printer is idle, the job is gone
    return capsys.readouterr().out


def _uses(calls):
    return [(path, body["use_weight"]) for _, path, body in calls if path.endswith("/use")]


def test_two_slot_print_completed_while_the_listener_was_down_is_charged_once(tmp_path, monkeypatch, capsys):
    calls = _setup(tmp_path, monkeypatch, status="completed")
    out = _listener_down_then_back(monkeypatch, capsys)
    assert _uses(calls) == [("/api/v1/spool/12/use", 6.8), ("/api/v1/spool/15/use", 28.15)]
    assert "Spool usage: slot 2 spool #12 6.8 g; slot 3 spool #15 28.15 g" in out
    # Later announcer passes and a late live end for the same print charge nothing more.
    watcher.main()
    listener = mel.Listener()
    listener.filename = FILE
    monkeypatch.setattr(mel, "log", lambda line: None); monkeypatch.setattr(mel, "warn", lambda line: None)
    monkeypatch.setattr(mel, "load_runout_pause", lambda *a, **kw: None)
    asyncio.run(listener._charge_multislot_usage("complete"))
    assert len(_uses(calls)) == 2


def test_cancelled_while_down_is_scaled_by_history_filament_used(tmp_path, monkeypatch, capsys):
    calls = _setup(tmp_path, monkeypatch, status="cancelled", filament_used=PLANNED_MM / 2)
    _listener_down_then_back(monkeypatch, capsys)
    grams = [g for _, g in _uses(calls)]
    assert grams == [3.4, pytest.approx(14.07, abs=0.01)]


def test_missed_finish_while_usage_is_suspended_charges_nothing(tmp_path, monkeypatch, capsys):
    runout = {"cause": "runout", "usage_suspended": True, "slot": 2, "recorded_at": 1.0}
    calls = _setup(tmp_path, monkeypatch, status="completed", runout=runout)
    out = _listener_down_then_back(monkeypatch, capsys)
    assert _uses(calls) == []
    assert "not charged" in out
