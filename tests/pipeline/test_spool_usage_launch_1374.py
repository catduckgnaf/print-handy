"""The runaway-charge check arms on real launches (#1374 review, 2.3.2).

#1375 recorded the active spool's used weight in JOB.json and the listener
compares Moonraker's charge with it when the print ends. On 2.3.1 the check
never ran on a real print:

* the mapped native route records the baseline for a one-slot job, but
  record_start copied it into the print context only when the job had no
  slot map at all, and a one-slot mapped job has one (``{"0": 2}``), so the
  listener found no baseline and returned;
* the unmapped Moonraker route (generic Klipper, the Sovol printers) never
  recorded a baseline, although Moonraker charges its active spool there too.

Each test drives the launch (print_mapped_native or print_now's generic
route), then the real record_start, then the listener's end transition
(Listener.on_status) against a stateful fake Spoolman. Nothing contacts a
printer.
"""
import json
import time
from datetime import datetime, timezone
from unittest import mock

import pytest

from print_handy.core import model_job, spoolman_client
from print_handy.launch import print_now
from print_handy.runtime import print_records

from tests.runtime.test_spool_usage_runaway_1374 import (
    BASELINE_G, DENSITY, DIAMETER, FILE, PRINT_MM, RUNAWAY_MM, SPOOL, FakeSpoolman, _drive, grams,
)

from .test_launch_state import make_job, mapped, printer  # noqa: F401  (fixtures)
from .test_start_path import MoonrakerScript, moonraker_status

REAL_RECORD_START = print_now.record_start


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _state(slot):
    return {"printer": "Test printer", "moonraker_url": "http://printer.invalid",
            "temps_c": {"nozzle": 220, "bed": 60}, "nozzle": {"diameter_mm": 0.4, "type": "brass"},
            "spool": {"spoolman_id": SPOOL, "name": "Bronze", "material": "PLA+",
                      "diameter_mm": DIAMETER, "density": DENSITY},
            "filament_slots": {str(slot): {"spoolman_id": SPOOL, "material": "PLA+", "hex": "#000000"}}}


def _record_start(tmp_path, monkeypatch, jp, g, *, current, slot):
    """The real record_start for this launch, writing the context the listener reads."""
    context_path = tmp_path / "print_handy_context.json"
    monkeypatch.setattr(print_records, "PRINTS_DIR", tmp_path / "Prints")
    started = time.time()
    with mock.patch.object(model_job, "_job_for_gcode", return_value=json.loads(jp.read_text())), \
         mock.patch.object(model_job.printer_state, "load", return_value=_state(slot)), \
         mock.patch.object(model_job.printers, "current", return_value=current), \
         mock.patch.object(model_job, "CONTEXT", context_path):
        REAL_RECORD_START(str(g), FILE, started_at=_iso(started), print_number=113)
    return json.loads(context_path.read_text())["jobs"][FILE], started


def _end_with_runaway(monkeypatch, tmp_path, fake, started):
    return _drive(monkeypatch, tmp_path, [RUNAWAY_MM, PRINT_MM], "complete", filament_used=PRINT_MM,
                  fake=fake, history_start=started + 30)


def test_one_slot_mapped_launch_keeps_the_baseline_and_the_runaway_is_corrected(mapped, monkeypatch, tmp_path):
    fake = FakeSpoolman(BASELINE_G)
    monkeypatch.setattr(spoolman_client, "request", fake)
    model_job._sync_active_spool.return_value = {"spool_id": SPOOL, "changed": False}
    jp, g = make_job(mapped.root, "a")
    with mock.patch.object(model_job, "_wait_printing", lambda *a, **k: "printing"):
        out = model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1},
                                            json.loads(jp.read_text()), jp, 0, False)
    assert out["started"] is True
    assert json.loads(jp.read_text())["spool_usage_baseline"]["used_weight"] == BASELINE_G
    native = mock.Mock()
    native.embedded_mapping = lambda text, st: {"0": 1}  # one tool, one slot: Moonraker charges it
    context, started = _record_start(tmp_path, monkeypatch, jp, g, slot=1,
                                     current=mock.Mock(multi_slot=True, native_or_none=native))
    assert context["tool_to_physical_slot"] == {"0": 1}
    assert context["spool_usage_baseline"]["spool_id"] == SPOOL
    _end_with_runaway(monkeypatch, tmp_path, fake, started)
    assert fake.used == pytest.approx(BASELINE_G + grams(PRINT_MM), abs=0.01)
    assert len(fake.patches()) == 1


def test_unmapped_moonraker_launch_records_the_baseline_and_the_runaway_is_corrected(printer, monkeypatch,
                                                                                    tmp_path):
    fake = FakeSpoolman(BASELINE_G)
    monkeypatch.setattr(spoolman_client, "request", fake)
    script = MoonrakerScript(printer, after=lambda r: moonraker_status(filename=r, state="printing"))

    def mr(st, path, data=None, timeout=30):
        if path == "/server/spoolman/spool_id":
            assert data is None  # read only: this route never moves Moonraker's spool
            return {"result": {"spool_id": SPOOL}}
        return script.mr(st, path, data=data, timeout=timeout)
    printer.mr.side_effect = mr
    jp, g = make_job(printer.root, "a")
    with mock.patch.object(model_job, "_wait_printing", lambda *a, **k: "printing"):
        out = model_job.print_now(jp)
    assert out["started"] is True and script.posts == 1
    baseline = json.loads(jp.read_text())["spool_usage_baseline"]
    assert baseline["spool_id"] == SPOOL and baseline["used_weight"] == BASELINE_G
    context, started = _record_start(tmp_path, monkeypatch, jp, g, slot=1,
                                     current=mock.Mock(multi_slot=False, native_or_none=None))
    assert "tool_to_physical_slot" not in context
    _end_with_runaway(monkeypatch, tmp_path, fake, started)
    assert fake.used == pytest.approx(BASELINE_G + grams(PRINT_MM), abs=0.01)
    assert len(fake.patches()) == 1


def test_unmapped_launch_without_a_moonraker_spool_clears_a_stale_baseline(printer, monkeypatch):
    fake = FakeSpoolman(BASELINE_G)
    monkeypatch.setattr(spoolman_client, "request", fake)
    script = MoonrakerScript(printer, after=lambda r: moonraker_status(filename=r, state="printing"))

    def mr(st, path, data=None, timeout=30):
        if path == "/server/spoolman/spool_id":
            return {"result": {"spool_id": None}}
        return script.mr(st, path, data=data, timeout=timeout)
    printer.mr.side_effect = mr
    jp, g = make_job(printer.root, "a")
    job = json.loads(jp.read_text())
    job["spool_usage_baseline"] = {"spool_id": SPOOL, "used_weight": 1.0, "recorded_at": 1.0}
    jp.write_text(json.dumps(job))
    with mock.patch.object(model_job, "_wait_printing", lambda *a, **k: "printing"):
        assert model_job.print_now(jp)["started"] is True
    assert "spool_usage_baseline" not in json.loads(jp.read_text())
    assert not [c for c in fake.calls if c[0] == "GET"]


def test_two_slot_mapped_launch_carries_no_baseline(mapped, monkeypatch, tmp_path):
    jp, g = make_job(mapped.root, "a")
    job = json.loads(jp.read_text())
    job["spool_usage_baseline"] = {"spool_id": SPOOL, "used_weight": BASELINE_G, "recorded_at": time.time()}
    jp.write_text(json.dumps(job))
    native = mock.Mock()
    native.embedded_mapping = lambda text, st: {"0": 1, "1": 2}
    state = _state(1)
    state["filament_slots"]["2"] = {"spoolman_id": 20, "material": "PLA"}
    context_path = tmp_path / "print_handy_context.json"
    monkeypatch.setattr(print_records, "PRINTS_DIR", tmp_path / "Prints")
    with mock.patch.object(model_job, "_job_for_gcode", return_value=job), \
         mock.patch.object(model_job.printer_state, "load", return_value=state), \
         mock.patch.object(model_job.printers, "current",
                           return_value=mock.Mock(multi_slot=True, native_or_none=native)), \
         mock.patch.object(model_job, "CONTEXT", context_path):
        REAL_RECORD_START(str(g), FILE, started_at=_iso(time.time()), print_number=114)
    context = json.loads(context_path.read_text())["jobs"][FILE]
    assert context["tool_to_physical_slot"] == {"0": 1, "1": 2}
    assert "spool_usage_baseline" not in context
