"""The listener charges a finished multi-slot print to each slot's spool.

Regression for multi-color AD5X prints never charging Spoolman: the launch
clears Moonraker's active spool for a multi-slot job and nothing charged the
spools when the print ended. Case: tool 0 -> slot 2, tool 1 -> slot 3,
`; filament used [g] = 6.80, 28.15`. Drives Listener.on_status with a fake
Spoolman; this file does not import the charging module, so it runs (and
fails) on a base without it.
"""
import asyncio
import json

import pytest

from print_handy.core import spoolman_client
from print_handy.runtime import klipper_report
from print_handy.runtime import moonraker_event_listener as mel
from print_handy.runtime import print_alert_buttons as pab

FILE = "dragon-two-color.gcode"
PLANNED_MM = 11627.68
STARTED_TS = 1794319200.0  # 2026-11-10T14:00:00Z, the context's started_at
CONTEXT = {
    "job_key": FILE,
    "print_number": 41,
    "started_at": "2026-11-10T14:00:00Z",
    "tool_to_physical_slot": {"0": 2, "1": 3},
    "slicer_settings": {
        "filament_used_g_list": [6.8, 28.15],
        "mapped_materials": {"0": {"spoolman_id": 12, "material": "PLA"},
                             "1": {"spoolman_id": 15, "material": "PLA"}},
    },
    "planned_filament_mm": PLANNED_MM,
}


_REAL = object()


def _drive(monkeypatch, tmp_path, transitions, *, stats=None, runout=None):
    context_path = tmp_path / "print_handy_context.json"
    context_path.write_text(json.dumps({"jobs": {FILE: CONTEXT}}))
    monkeypatch.setenv("PRINT_HANDY_CONTEXT_PATH", str(context_path))
    monkeypatch.setenv("PRINT_HANDY_MULTISLOT_USAGE_STATE", str(tmp_path / "ledger.json"))
    calls = []

    def fake(method, path, body=None, **kw):
        calls.append((method, path, body))
        return {}
    monkeypatch.setattr(spoolman_client, "request", fake)
    monkeypatch.setattr(mel, "post", lambda message, **kw: True)
    monkeypatch.setattr(mel, "post_alert", lambda *a, **kw: True)
    monkeypatch.setattr(mel, "run_announcer", lambda reason: None)
    monkeypatch.setattr(mel, "start_claimed", lambda name: False)
    monkeypatch.setattr(mel, "log", lambda line: None); monkeypatch.setattr(mel, "warn", lambda line: None)
    monkeypatch.setattr(mel, "_buttonless_standalone", lambda: None)
    if runout is not _REAL:
        # Never touch the shared runout state: the start-time settle (#1357)
        # writes print_over back, which would leak an open question.
        monkeypatch.setattr(mel, "RUNOUT_PAUSE_STATE", tmp_path / "runout_pause.json")
        monkeypatch.setattr(mel, "load_runout_pause", lambda *a, **kw: runout)

    async def _no_reminder():
        return None
    monkeypatch.setattr(mel, "maintenance_cancel_reminder", _no_reminder)
    monkeypatch.setattr(klipper_report, "report", lambda filename, *a, **kw: "Klipper report stub")
    monkeypatch.setattr(klipper_report, "cancelled_by_bot", lambda: True)
    monkeypatch.setattr(pab, "retire_alerts", lambda *a, **kw: 1)
    # Moonraker's history entry for this print: the end charge checks the
    # context's start falls inside it (#1362 review).
    entry = {"job_id": "000041", "filename": FILE, "status": "in_progress", "start_time": STARTED_TS + 60}

    def get_json(path, timeout=10):
        if path.startswith("/server/history"):
            return {"result": {"jobs": [entry]}}
        return {"result": {}}
    monkeypatch.setattr(mel.announcer, "get_json", get_json)
    listener = mel.Listener()

    async def run():
        for state in transitions:
            await listener.on_status({"state": state, "filename": FILE, **(stats or {})}, "test")
        await asyncio.gather(*listener.background_tasks)
    asyncio.run(run())
    return [c for c in calls if c[1].endswith("/use")]


def test_listener_charges_both_slots_when_a_multislot_print_completes(monkeypatch, tmp_path):
    calls = _drive(monkeypatch, tmp_path, ["standby", "printing", "complete"])
    assert calls == [("PUT", "/api/v1/spool/12/use", {"use_weight": 6.8}),
                     ("PUT", "/api/v1/spool/15/use", {"use_weight": 28.15})]


def test_listener_scales_a_cancelled_multislot_print(monkeypatch, tmp_path):
    calls = _drive(monkeypatch, tmp_path, ["standby", "printing", "cancelled"],
                   stats={"filament_used": PLANNED_MM / 2})
    assert [body["use_weight"] for _, _, body in calls] == [3.4, pytest.approx(14.07, abs=0.01)]


def test_listener_charges_nothing_while_usage_is_suspended(monkeypatch, tmp_path):
    record = {"cause": "runout", "usage_suspended": True, "slot": 2, "recorded_at": 1.0}
    calls = _drive(monkeypatch, tmp_path, ["standby", "printing", "complete"], runout=record)
    assert calls == []


def test_duplicate_end_event_does_not_charge_twice(monkeypatch, tmp_path):
    calls = _drive(monkeypatch, tmp_path, ["standby", "printing", "complete", "printing", "complete"])
    assert len(calls) == 2


def _open_question(monkeypatch, tmp_path, *, bound, present):
    """A runout question from an earlier print, as #1357 leaves it: still suspending usage."""
    path = tmp_path / "runout_pause.json"
    path.write_text(json.dumps({"cause": "runout", "usage_suspended": True, "slot": 2,
                                "prior_spool_id": 12, "filename": "earlier.gcode",
                                "recorded_at": 1.0, "print_over": True}))
    monkeypatch.setattr(mel, "RUNOUT_PAUSE_STATE", path)
    monkeypatch.setattr(mel, "_mapped_spool", lambda slot: bound)

    async def _present(self, slot):
        return present
    monkeypatch.setattr(mel.Listener, "_slot_present_now", _present)
    return path


def test_question_closed_as_refeed_at_start_lets_the_print_be_charged(monkeypatch, tmp_path):
    # #1357: the spool that ran out is back in its slot, so the new print's
    # start closes the question as a refeed; usage is no longer suspended.
    path = _open_question(monkeypatch, tmp_path, bound=12, present=True)
    calls = _drive(monkeypatch, tmp_path, ["standby", "printing", "complete"], runout=_REAL)
    assert not path.exists()
    assert len(calls) == 2


def test_question_still_open_at_the_end_charges_nothing(monkeypatch, tmp_path):
    # Still open after the start check (a different spool is bound): the
    # start notice says this print may not be charged, and it is not.
    path = _open_question(monkeypatch, tmp_path, bound=99, present=True)
    calls = _drive(monkeypatch, tmp_path, ["standby", "printing", "complete"], runout=_REAL)
    assert path.exists()
    assert calls == []
