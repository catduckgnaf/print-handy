"""A single-slot print's spool grows by what the print extruded, once (#1374).

The live case: spool #19 (1000 g, IFS slot 2) read 65.96 g used before a
0.6 g print and 1053.43 g after it. Moonraker had restarted while Klipper
kept running; its ``set_active_spool`` resets the extrusion high-water mark
to the last E position it saw, which was still 0, so the first extrusion of
the next print charged all 331 m Klipper had extruded since it last started.

These tests replay Moonraker's per-report ``PUT /spool/{id}/use`` calls
against a stateful fake Spoolman and drive the listener's end transition
(Listener.on_status), the production entry point. On the base commit
nothing compares the charge with the print, so the spool keeps the runaway.
"""
import asyncio
import json
import math

import pytest

from print_handy.core import spoolman_client
from print_handy.runtime import klipper_report
from print_handy.runtime import moonraker_event_listener as mel
from print_handy.runtime import print_alert_buttons as pab

FILE = "giveaway-one-plate.gcode"
SPOOL = 19
BASELINE_G = 65.96
DENSITY, DIAMETER = 1.24, 1.75
STARTED = "2026-11-12T00:39:00Z"
STARTED_TS = 1794443940.0  # STARTED as a Unix time


def grams(mm):
    return mm * math.pi * (DIAMETER / 2) ** 2 * DENSITY / 1000.0


# Klipper's toolhead E position since its last start (11-05), charged in one go.
RUNAWAY_MM = 331070.0
PRINT_MM = 216.0


def _context(**extra):
    ctx = {
        "job_key": FILE,
        "print_number": 113,
        "started_at": STARTED,
        "filament_density": DENSITY,
        "filament_diameter_mm": DIAMETER,
        "tool_to_physical_slot": {"0": 2},
        "slicer_settings": {"filament_used_g_list": [0.53],
                            "mapped_materials": {"0": {"spoolman_id": SPOOL, "material": "PLA+"}}},
        "active_spool": {"id": SPOOL, "material": "PLA+", "color": "Bronze"},
        "spool_usage_baseline": {"spool_id": SPOOL, "used_weight": BASELINE_G,
                                 "recorded_at": STARTED_TS - 30},
    }
    ctx.update(extra)
    return ctx


class FakeSpoolman:
    """Spoolman's spool record and the three calls that change it."""

    def __init__(self, used):
        self.used = used
        self.calls = []

    def report(self, mm):
        """One Moonraker usage report, as its spoolman component sends it."""
        self("PUT", f"/api/v1/spool/{SPOOL}/use", {"use_length": mm})

    def __call__(self, method, path, body=None, **kw):
        self.calls.append((method, path, body))
        if method == "GET":
            return {"id": SPOOL, "used_weight": self.used, "initial_weight": 1000.0,
                    "remaining_weight": max(0.0, 1000.0 - self.used)}
        if method == "PUT" and path.endswith("/use"):
            self.used += body.get("use_weight") or grams(body.get("use_length") or 0)
            return {}
        if method == "PATCH":
            self.used = body["used_weight"]
            return {}
        raise AssertionError(f"unexpected call {method} {path}")

    def patches(self):
        return [c for c in self.calls if c[0] == "PATCH"]


def _drive(monkeypatch, tmp_path, reports, end, *, filament_used, context=None, repeat_end=False):
    context_path = tmp_path / "print_handy_context.json"
    context_path.write_text(json.dumps({"jobs": {FILE: context or _context()}}))
    monkeypatch.setenv("PRINT_HANDY_CONTEXT_PATH", str(context_path))
    monkeypatch.setenv("PRINT_HANDY_MULTISLOT_USAGE_STATE", str(tmp_path / "ledger.json"))
    fake = FakeSpoolman(BASELINE_G)
    monkeypatch.setattr(spoolman_client, "request", fake)
    monkeypatch.setattr(mel, "post", lambda message, **kw: True)
    monkeypatch.setattr(mel, "post_alert", lambda *a, **kw: True)
    monkeypatch.setattr(mel, "run_announcer", lambda reason: None)
    monkeypatch.setattr(mel, "start_claimed", lambda name: False)
    monkeypatch.setattr(mel, "log", lambda line: None)
    monkeypatch.setattr(mel, "_buttonless_standalone", lambda: None)
    monkeypatch.setattr(mel, "RUNOUT_PAUSE_STATE", tmp_path / "runout_pause.json")
    monkeypatch.setattr(mel, "load_runout_pause", lambda *a, **kw: None)

    async def _no_reminder():
        return None
    monkeypatch.setattr(mel, "maintenance_cancel_reminder", _no_reminder)
    monkeypatch.setattr(klipper_report, "report", lambda filename, *a, **kw: "Klipper report stub")
    monkeypatch.setattr(klipper_report, "cancelled_by_bot", lambda: True)
    monkeypatch.setattr(pab, "retire_alerts", lambda *a, **kw: 1)
    listener = mel.Listener()

    async def status(state):
        await listener.on_status({"state": state, "filename": FILE, "filament_used": filament_used}, "test")

    async def run():
        await status("standby")
        await status("printing")
        for mm in reports:  # Moonraker's progress reports while the print runs
            fake.report(mm)
        await status(end)
        if repeat_end:
            await status("printing")
            await status(end)
        await asyncio.gather(*listener.background_tasks)
    asyncio.run(run())
    return fake


def test_runaway_moonraker_charge_is_corrected_to_the_print_grams(monkeypatch, tmp_path):
    # The live sequence: the runaway arrives with the first extrusion, then
    # the print's own reports every few seconds.
    reports = [RUNAWAY_MM] + [PRINT_MM / 4] * 4
    fake = _drive(monkeypatch, tmp_path, reports, "complete", filament_used=PRINT_MM)
    assert fake.used == pytest.approx(BASELINE_G + grams(PRINT_MM), abs=0.01)
    assert len(fake.patches()) == 1


def test_cancelled_print_keeps_only_what_it_extruded(monkeypatch, tmp_path):
    reports = [RUNAWAY_MM, 37.0]
    fake = _drive(monkeypatch, tmp_path, reports, "cancelled", filament_used=37.0)
    assert fake.used == pytest.approx(BASELINE_G + grams(37.0), abs=0.01)


def test_duplicate_end_event_corrects_once(monkeypatch, tmp_path):
    fake = _drive(monkeypatch, tmp_path, [RUNAWAY_MM, PRINT_MM], "complete",
                  filament_used=PRINT_MM, repeat_end=True)
    assert len(fake.patches()) == 1
    assert fake.used == pytest.approx(BASELINE_G + grams(PRINT_MM), abs=0.01)


def test_a_normal_charge_is_left_alone(monkeypatch, tmp_path):
    # Moonraker's ordinary charge, a little over filament_used (an IFS load
    # before the print), is never rewritten.
    reports = [300.0] + [5000.0] * 6
    fake = _drive(monkeypatch, tmp_path, reports, "complete", filament_used=30000.0)
    assert fake.patches() == []
    assert fake.used == pytest.approx(BASELINE_G + grams(30300.0), abs=0.01)


def test_no_baseline_means_no_check(monkeypatch, tmp_path):
    ctx = _context()
    ctx.pop("spool_usage_baseline")
    fake = _drive(monkeypatch, tmp_path, [RUNAWAY_MM], "complete", filament_used=PRINT_MM, context=ctx)
    assert fake.patches() == []
    assert not [c for c in fake.calls if c[0] == "GET"]


def test_stale_baseline_from_an_earlier_launch_is_ignored(monkeypatch, tmp_path):
    ctx = _context()
    ctx["spool_usage_baseline"]["recorded_at"] = STARTED_TS - 2 * 86400
    fake = _drive(monkeypatch, tmp_path, [RUNAWAY_MM], "complete", filament_used=PRINT_MM, context=ctx)
    assert fake.patches() == []


def test_finish_post_line_names_the_correction(monkeypatch, tmp_path):
    from print_handy.runtime import multislot_usage
    _drive(monkeypatch, tmp_path, [RUNAWAY_MM, PRINT_MM], "complete", filament_used=PRINT_MM)
    line = multislot_usage.summary_line(multislot_usage.entry_for(_context()))
    assert line.startswith("Spool usage: Moonraker charged spool #19 988.08 g for a print that used 0.64 g")
    assert "corrected to 0.64 g" in line


def test_missed_finish_while_the_listener_was_down_is_checked(tmp_path, monkeypatch):
    from print_handy.runtime import multislot_usage
    fake = FakeSpoolman(BASELINE_G)
    fake.report(RUNAWAY_MM)
    fake.report(PRINT_MM)
    job = {"status": "completed", "start_time": STARTED_TS, "end_time": STARTED_TS + 300,
           "filament_used": PRINT_MM}
    entry = multislot_usage.charge_history_job(job, _context(), suspended=False, request=fake,
                                               path=tmp_path / "ledger.json")
    assert entry["status"] == "corrected"
    assert fake.used == pytest.approx(BASELINE_G + grams(PRINT_MM), abs=0.01)


def test_launch_records_the_baseline_and_a_failed_read_clears_it(tmp_path, monkeypatch):
    from print_handy.launch import print_now
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    written = []
    monkeypatch.setattr(print_now, "write_job", lambda folder, job: written.append(dict(job)))
    fake = FakeSpoolman(BASELINE_G)
    monkeypatch.setattr(spoolman_client, "request", fake)
    job = {"title": "x", "spool_usage_baseline": {"spool_id": 7, "used_weight": 1.0, "recorded_at": 1.0}}
    print_now._record_usage_baseline({}, job, job_dir / "JOB.json", SPOOL)
    assert job["spool_usage_baseline"]["spool_id"] == SPOOL
    assert job["spool_usage_baseline"]["used_weight"] == BASELINE_G

    def down(*a, **kw):
        raise spoolman_client.SpoolmanUnavailable("connection refused")
    monkeypatch.setattr(spoolman_client, "request", down)
    print_now._record_usage_baseline({}, job, job_dir / "JOB.json", SPOOL)
    assert "spool_usage_baseline" not in job
    assert len(written) == 2
