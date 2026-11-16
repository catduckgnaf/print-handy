"""A single-spool print on a printer without a Moonraker Spoolman link is charged once (#1570).

Forge-X ships Moonraker's spoolman component without a ``[spoolman]``
section, so ``/server/spoolman/spool_id`` is not served and nothing on the
printer charges Spoolman. With spool accounting on ``auto`` Print Handy is
the authority there, and docs/OUTSIDE-SPOOLMAN.md says it charges at the end
of the print. On the base commit the generic Moonraker launch only read
Moonraker's (missing) active spool for the runaway check, and the end of the
print charged a one-spool print through nothing, so the spool never moved.

Each test drives the generic launch (print_now), the real record_start and
the listener's end transition (Listener.on_status) against a stateful fake
Spoolman. Nothing contacts a printer.
"""
import json
import time
from unittest import mock
from urllib.error import HTTPError

import pytest

from print_handy.core import model_job, spoolman_client

from tests.runtime.test_spool_usage_runaway_1374 import (
    BASELINE_G, PRINT_MM, SPOOL, FakeSpoolman, _drive,
)

from .test_launch_state import make_job, printer  # noqa: F401  (fixture)
from .test_spool_usage_launch_1374 import _record_start, _state
from .test_start_path import MoonrakerScript, moonraker_status

# The sliced file's own header: 216 mm, 0.65 g of one filament.
SLICED_G = 0.65
HEADER = ("; filament used [mm] = 216.00\n"
          "; filament used [g] = 0.65\n"
          "; total filament used [g] = 0.65\n")


def _gcode(g):
    g.write_text(HEADER + g.read_text())


def _launch(printer, *, linked, monkeypatch, settings_accounting=None):  # noqa: F811
    """The generic Moonraker launch with or without a Moonraker Spoolman link."""
    script = MoonrakerScript(printer, after=lambda r: moonraker_status(filename=r, state="printing"))

    def mr(st, path, data=None, timeout=30):
        if path == "/server/spoolman/spool_id":
            assert data is None  # this route never moves Moonraker's spool
            if not linked:  # Moonraker serves the endpoint only with a [spoolman] section
                raise HTTPError("http://printer.invalid" + path, 404, "Not Found", None, None)
            return {"result": {"spool_id": SPOOL}}
        if path == "/server/config":
            config = {"spoolman": {"server": "http://spoolman.invalid"}} if linked else {}
            return {"result": {"config": config}}
        return script.mr(st, path, data=data, timeout=timeout)
    printer.mr.side_effect = mr
    if settings_accounting is not None:
        from print_handy import settings
        current = settings.current()
        patched = {**current, "spool": {**(current.get("spool") or {}), "accounting": settings_accounting}}
        monkeypatch.setattr(settings, "current", lambda *a, **k: patched)
    jp, g = make_job(printer.root, "a")
    _gcode(g)
    state = {**_state(1), "moonraker_url": "http://printer.invalid"}
    with mock.patch.object(model_job.printer_state, "load", return_value=state), \
         mock.patch.object(model_job, "_wait_printing", lambda *a, **k: "printing"):
        out = model_job.print_now(jp)
    assert out["started"] is True and script.posts == 1
    return jp, g


def _end(monkeypatch, tmp_path, fake, started, end, *, filament_used, reports=(), active=None):
    return _drive(monkeypatch, tmp_path, list(reports), end, filament_used=filament_used,
                  fake=fake, active=active, history_start=started + 30)


def _print_handy_charges(fake):
    """Print Handy's own charges: a weight. Moonraker's reports carry a length."""
    return [c for c in fake.calls
            if c[0] == "PUT" and c[1] == f"/api/v1/spool/{SPOOL}/use" and "use_weight" in (c[2] or {})]


def test_unlinked_single_spool_print_is_charged_once_at_the_end(printer, monkeypatch, tmp_path):  # noqa: F811
    fake = FakeSpoolman(BASELINE_G)
    monkeypatch.setattr(spoolman_client, "request", fake)
    jp, g = _launch(printer, linked=False, monkeypatch=monkeypatch)
    context, started = _record_start(tmp_path, monkeypatch, jp, g, slot=1,
                                     current=mock.Mock(multi_slot=False, native_or_none=None))
    _end(monkeypatch, tmp_path, fake, started, "complete", filament_used=PRINT_MM)
    charges = _print_handy_charges(fake)
    assert len(charges) == 1, "the finished print was not charged to its spool"
    assert charges[0][2]["use_weight"] == pytest.approx(SLICED_G, abs=0.01)
    assert fake.used == pytest.approx(BASELINE_G + SLICED_G, abs=0.01)
    # A second end event (a listener restart, a duplicate transition) charges nothing more.
    _end(monkeypatch, tmp_path, fake, started, "complete", filament_used=PRINT_MM)
    assert len(_print_handy_charges(fake)) == 1


def test_unlinked_stopped_print_is_charged_its_share(printer, monkeypatch, tmp_path):  # noqa: F811
    fake = FakeSpoolman(BASELINE_G)
    monkeypatch.setattr(spoolman_client, "request", fake)
    jp, g = _launch(printer, linked=False, monkeypatch=monkeypatch)
    context, started = _record_start(tmp_path, monkeypatch, jp, g, slot=1,
                                     current=mock.Mock(multi_slot=False, native_or_none=None))
    _end(monkeypatch, tmp_path, fake, started, "cancelled", filament_used=54.0)  # a quarter of the plan
    charges = _print_handy_charges(fake)
    assert len(charges) == 1
    assert charges[0][2]["use_weight"] == pytest.approx(SLICED_G / 4, abs=0.01)


def test_linked_moonraker_charges_live_and_print_handy_does_not(printer, monkeypatch, tmp_path):  # noqa: F811
    fake = FakeSpoolman(BASELINE_G)
    monkeypatch.setattr(spoolman_client, "request", fake)
    jp, g = _launch(printer, linked=True, monkeypatch=monkeypatch)
    assert "spool_charge_at_end" not in json.loads(jp.read_text())
    context, started = _record_start(tmp_path, monkeypatch, jp, g, slot=1,
                                     current=mock.Mock(multi_slot=False, native_or_none=None))
    assert "spool_charge_at_end" not in context
    # Moonraker's own reports while the print runs are the only charge.
    _end(monkeypatch, tmp_path, fake, started, "complete", filament_used=PRINT_MM,
         reports=[PRINT_MM], active=SPOOL)
    assert _print_handy_charges(fake) == []
    assert fake.patches() == []


def test_printer_authority_is_never_charged_by_print_handy(printer, monkeypatch, tmp_path):  # noqa: F811
    # The printer as the authority needs a linked Moonraker (an unlinked one
    # falls back to Print Handy, #1536). Its live charge is the only one.
    fake = FakeSpoolman(BASELINE_G)
    monkeypatch.setattr(spoolman_client, "request", fake)
    jp, g = _launch(printer, linked=True, monkeypatch=monkeypatch, settings_accounting="printer")
    job = json.loads(jp.read_text())
    assert job["spool_accounting_authority"] == "printer"
    assert "spool_charge_at_end" not in job
    context, started = _record_start(tmp_path, monkeypatch, jp, g, slot=1,
                                     current=mock.Mock(multi_slot=False, native_or_none=None))
    _end(monkeypatch, tmp_path, fake, started, "complete", filament_used=PRINT_MM,
         reports=[PRINT_MM], active=SPOOL)
    assert _print_handy_charges(fake) == []


def test_a_context_from_another_spool_is_not_charged(printer, monkeypatch, tmp_path):  # noqa: F811
    # The launch recorded spool #19; the start record finds a different
    # active spool (swapped between upload and start). The charge is never
    # moved to a spool the launch did not record.
    fake = FakeSpoolman(BASELINE_G)
    monkeypatch.setattr(spoolman_client, "request", fake)
    jp, g = _launch(printer, linked=False, monkeypatch=monkeypatch)
    other = {**_state(1)}
    other["spool"] = {**other["spool"], "spoolman_id": SPOOL + 1}
    context_path = tmp_path / "print_handy_context.json"
    from print_handy.runtime import print_records
    from .test_spool_usage_launch_1374 import FILE, REAL_RECORD_START, _iso
    monkeypatch.setattr(print_records, "PRINTS_DIR", tmp_path / "Prints")
    with mock.patch.object(model_job, "_job_for_gcode", return_value=json.loads(jp.read_text())), \
         mock.patch.object(model_job.printer_state, "load", return_value=other), \
         mock.patch.object(model_job.printers, "current",
                           return_value=mock.Mock(multi_slot=False, native_or_none=None)), \
         mock.patch.object(model_job, "CONTEXT", context_path):
        REAL_RECORD_START(str(g), FILE, started_at=_iso(time.time()), print_number=113)
    context = json.loads(context_path.read_text())["jobs"][FILE]
    assert "spool_charge_at_end" not in context


def test_a_print_that_ended_while_the_listener_was_down_is_charged_once(tmp_path):
    # The missed-finish path (Moonraker history) charges through the same
    # ledger, so the live end and the missed-end check never both charge.
    from print_handy.runtime import multislot_usage
    from tests.runtime.test_spool_usage_runaway_1374 import STARTED, STARTED_TS
    fake = FakeSpoolman(BASELINE_G)
    context = {"job_key": "f.gcode", "print_number": 7, "started_at": STARTED,
               "spool_charge_at_end": {"spool_id": SPOOL}, "planned_filament_mm": 216.0,
               "slicer_settings": {"filament_used_g_list": [SLICED_G]},
               "spool_accounting_authority": "print_handy"}
    job = {"status": "completed", "start_time": STARTED_TS + 60, "end_time": STARTED_TS + 600,
           "filament_used": PRINT_MM}
    ledger = tmp_path / "ledger.json"
    for _ in range(2):
        entry = multislot_usage.charge_history_job(job, context, suspended=False, request=fake, path=ledger)
    assert len(_print_handy_charges(fake)) == 1
    assert multislot_usage.summary_line(entry) == f"Spool usage: the print's spool #{SPOOL} 0.65 g"


def test_a_recorded_printer_authority_stands_down_even_with_a_charge_marker(tmp_path):
    from print_handy.runtime import multislot_usage
    fake = FakeSpoolman(BASELINE_G)
    context = {"job_key": "f.gcode", "print_number": 8, "started_at": "2026-11-12T00:39:00Z",
               "spool_charge_at_end": {"spool_id": SPOOL},
               "slicer_settings": {"filament_used_g_list": [SLICED_G]},
               "spool_accounting_authority": "printer"}
    entry = multislot_usage.charge_finished_print(context, "complete", request=fake,
                                                  path=tmp_path / "ledger.json")
    assert entry["reason"] == multislot_usage.PRINTER_AUTHORITY_REASON
    assert _print_handy_charges(fake) == []
