"""Print records written under timestamp names are visible to Data and Wrapped (#1758).

Since 2026-10-31 the Data page and Pasta Wrapped stopped seeing most
finished prints: the listener's completion for a print whose launch
context was unavailable carries no print number, so
``write_completed_record`` wrote it under a timestamp name and left its
numbered start record printing forever — while the stats loader only
read ``print-*.json``.

These tests pin the fix at both ends, through the real writers and the
real loader:

* going forward, a completion with no context finalizes its numbered
  start record in place;
* the loader reads every record the writers produce and joins the
  files that name one physical print, so existing installs reconcile
  without a disk migration;
* the Data totals equal the sum of their shown parts, with prints
  whose outcome was never recorded shown as their own part.
"""

import datetime
import json

import pytest

from print_handy.core import print_stats
from print_handy.runtime import print_records as records

SPOOL = {"vendor": "Generic", "material": "PLA", "color": "Black", "spool_id": 3, "id": 3}
GCODE = "benchy-plate1_0123456789abcdef.gcode"


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    prints = tmp_path / "Prints"
    plans = tmp_path / "Plans"
    prints.mkdir()
    plans.mkdir()
    monkeypatch.setattr(records, "PRINTS_DIR", prints)
    monkeypatch.setattr(records, "PLANS_DIR", plans)
    return tmp_path


def _place(vault, name, **fields):
    record = {
        "print_number": None,
        "record_id": "deadbeef",
        "title": "Benchy",
        "status": "printing",
        "printer": "AD5X",
        "gcode_file": GCODE,
        "job_key": GCODE,
        "started_at": "2026-10-31T12:00:00Z",
    }
    record.update(fields)
    (vault / "Prints" / name).write_text(json.dumps(record))
    return record


def test_missed_context_completion_finalizes_the_numbered_start(vault):
    """#1758: the completion the listener writes with no launch context
    lands in the numbered start record, not beside it under a new name."""
    start_path = records.write_start_record(
        {
            "job_key": GCODE,
            "print_number": 64,
            "title": "Benchy",
            "started_at": "2026-10-31T12:00:00Z",
            "printer": "AD5X",
            "active_spool": SPOOL,
        }
    )
    assert start_path.endswith("print-0064.json")
    epoch = datetime.datetime(2026, 10, 31, 12, 0, 30, tzinfo=datetime.timezone.utc).timestamp()
    # The listener's key is the printer-reported path; the launch sidecar
    # keyed this job by the bare remote name, so the context lookup missed
    # and only the job key itself rides the completion context.
    data = records.build_record_data(
        f"3mf_print/{GCODE}",
        stats={"state": "complete", "print_duration": 3600.0, "filament_used": 1000.0, "start_time": epoch},
        context={"job_key": f"3mf_print/{GCODE}"},
        active_spool=SPOOL,
        completed_at="2026-10-31T13:00:30Z",
    )
    assert data["print_number"] is None
    result = records.write_completed_record(data)
    assert result["record_path"].endswith("print-0064.json")
    files = sorted(p.name for p in (vault / "Prints").glob("*.json"))
    assert files == ["print-0064.json"], f"one canonical record, not two: {files}"
    loaded, skipped = print_stats.load_records(vault)
    assert skipped == [] and len(loaded) == 1
    assert loaded[0]["status"] == "completed" and loaded[0]["print_number"] == 64
    agg = print_stats.aggregate(loaded, year=2026)
    assert agg["total_prints"] == 1 and agg["completed"] == 1


def test_loader_joins_timestamp_completion_with_its_numbered_start(vault):
    """#1758 regression test, as filed: one numbered start plus one
    timestamp-named completion for the same print loads as one
    completed print, and Wrapped counts it."""
    _place(vault, "print-0064.json", print_number=64, record_id="aaaa0001")
    _place(
        vault,
        "20261031T120030-benchy-plate1_0123456789abcdef-62ef3c60.json",
        record_id="62ef3c60",
        status="completed",
        success=True,
        printer="Klipper 3D Printer",
        gcode_file=f"3mf_print/{GCODE}",
        job_key=f"3mf_print/{GCODE}",
        started_at="2026-10-31T12:00:30Z",
        completed_at="2026-10-31T13:00:30Z",
        print_duration_seconds=3600.0,
        filament_used_g=10.0,
    )
    loaded, skipped = print_stats.load_records(vault)
    assert skipped == [] and len(loaded) == 1, "the start and its completion are one print, not two"
    assert loaded[0]["status"] == "completed"
    assert loaded[0]["print_number"] == 64, (
        "the joined print keeps its number even though the completion was written without one"
    )
    agg = print_stats.aggregate(loaded, year=2026)
    assert agg["total_prints"] == 1 and agg["completed"] == 1
    assert agg["finished"] == 1


def test_loader_joins_by_record_id(vault):
    """Two files carrying the same record id are one print, whatever
    their names say."""
    _place(vault, "print-0065.json", print_number=65, record_id="bbbb0002")
    _place(
        vault,
        "20261031T130000-benchy-plate1_0123456789abcdef-bbbb0002.json",
        record_id="bbbb0002",
        status="completed",
        success=True,
        started_at="2026-10-31T12:00:30Z",
        completed_at="2026-10-31T13:00:30Z",
    )
    loaded, _ = print_stats.load_records(vault)
    assert len(loaded) == 1 and loaded[0]["status"] == "completed"


def test_loader_keeps_a_reprint_of_the_same_file_separate(vault):
    """The join never merges a reprint: same number, same file, but a
    start months after the earlier print ended is a different print."""
    _place(
        vault,
        "print-0007.json",
        print_number=7,
        record_id="c001",
        status="completed",
        success=True,
        started_at="2026-03-04T15:00:00Z",
        completed_at="2026-03-04T16:00:00Z",
    )
    _place(
        vault,
        "print-0007-reprint.json",
        print_number=7,
        record_id="c002",
        status="failed",
        success=False,
        started_at="2026-05-02T09:00:00Z",
        completed_at="2026-05-02T09:20:00Z",
    )
    loaded, skipped = print_stats.load_records(vault)
    assert skipped == [] and len(loaded) == 2
    agg = print_stats.aggregate(loaded, year=2026)
    assert agg["total_prints"] == 2
    assert agg["completed"] == 1 and agg["failed"] == 1


def test_loader_keeps_a_quick_reprint_separate(vault):
    """A reprint that starts after the earlier print ended is a
    different print even when both starts fall inside the join window."""
    _place(
        vault,
        "print-0007.json",
        print_number=7,
        record_id="d001",
        status="completed",
        success=True,
        started_at="2026-10-31T12:00:00Z",
        completed_at="2026-10-31T12:03:20Z",
    )
    _place(
        vault, "print-0008.json", print_number=8, record_id="d002", status="printing", started_at="2026-10-31T12:05:00Z"
    )
    loaded, _ = print_stats.load_records(vault)
    assert len(loaded) == 2, (
        "the second print started after the first ended, so it is not the first print's start record"
    )


def test_data_totals_equal_the_sum_of_their_shown_parts(vault):
    """#1758: the headline parts — finished, failed, stopped, printing,
    outcome not recorded — add up to the total."""
    _place(
        vault, "print-0070.json", print_number=70, record_id="e070", started_at="2026-10-31T12:00:00Z"
    )  # stale start: unknown
    _place(
        vault,
        "print-0071.json",
        print_number=71,
        record_id="e071",
        status="completed",
        success=True,
        started_at="2026-10-31T14:00:00Z",
        completed_at="2026-10-31T15:00:00Z",
        gcode_file="vase.gcode",
        job_key="vase.gcode",
    )
    _place(
        vault,
        "print-0072.json",
        print_number=72,
        record_id="e072",
        status="failed",
        success=False,
        started_at="2026-10-31T15:00:00Z",
        completed_at="2026-10-31T15:20:00Z",
        gcode_file="clip.gcode",
        job_key="clip.gcode",
    )
    _place(
        vault,
        "print-0073.json",
        print_number=73,
        record_id="e073",
        status="cancelled",
        started_at="2026-10-31T16:00:00Z",
        completed_at="2026-10-31T16:10:00Z",
        gcode_file="hook.gcode",
        job_key="hook.gcode",
    )
    _place(
        vault,
        "print-0074.json",
        print_number=74,
        record_id="e074",
        started_at="2026-10-31T17:00:00Z",
        gcode_file="tag.gcode",
        job_key="tag.gcode",
    )  # newest on the printer: still printing
    loaded, _ = print_stats.load_records(vault)
    agg = print_stats.aggregate(loaded, year=2026)
    assert agg["total_prints"] == 5
    assert agg["completed"] == 1 and agg["failed"] == 1
    assert agg["canceled"] == 1 and agg["in_progress"] == 1
    assert agg["unknown_outcome"] == 1
    parts = agg["completed"] + agg["failed"] + agg["canceled"] + agg["in_progress"] + agg["unknown_outcome"]
    assert parts == agg["total_prints"], f"shown parts {parts} must add up to the total {agg['total_prints']}"
    assert agg["outcomes"]["unknown"] == 1
    # The per-printer and per-month parts reconcile the same way.
    printer = next(p for p in agg["printers"] if p["printer"] == "AD5X")
    printer_parts = (
        printer["finished"] + printer["failed"] + printer["stopped"] + printer["printing"] + printer["unknown"]
    )
    assert printer_parts == printer["prints"]
    month = agg["monthly_outcomes"]["2026-10"]
    month_parts = month["finished"] + month["failed"] + month["stopped"] + month["printing"] + month["unknown"]
    assert month_parts == month["prints"]
    from print_handy.webui import data_page

    assert "1 outcome not recorded" in data_page._outcome_line(agg)


def test_load_context_matches_by_base_name(tmp_path):
    """#1758: the listener's path-prefixed job key finds the launch
    sidecar entry keyed by the bare remote name; a key that matches
    nothing returns no context, never the whole sidecar file."""
    sidecar = tmp_path / "context.json"
    sidecar.write_text(
        json.dumps(
            {"jobs": {GCODE: {"print_number": 64, "job_key": GCODE, "record_path": "/vault/Prints/print-0064.json"}}}
        )
    )
    found = records.load_context(f"3mf_print/{GCODE}", path=sidecar)
    assert found.get("print_number") == 64
    assert records.load_context("other.gcode", path=sidecar) == {}
    # Two sidecar jobs sharing a base name are ambiguous: no guess.
    sidecar.write_text(json.dumps({"jobs": {f"a/{GCODE}": {"print_number": 1}, f"b/{GCODE}": {"print_number": 2}}}))
    assert records.load_context(f"c/{GCODE}", path=sidecar) == {}

@pytest.mark.parametrize("case", ["missing_start", "other_printer", "other_number", "ambiguous"])
def test_completion_does_not_guess_a_start(vault, case):
    _place(vault, "print-0064.json", print_number=64, record_id="a64")
    data = {"gcode_file": GCODE, "job_key": GCODE, "status": "completed",
            "success": True, "started_at": "2026-10-31T12:00:30Z",
            "completed_at": "2026-10-31T13:00:30Z"}
    if case == "missing_start":
        data["started_at"] = None
    elif case == "other_printer":
        data["printer"] = "Other printer"
    elif case == "other_number":
        data["print_number"] = 65
    else:
        _place(vault, "print-0065.json", print_number=65, record_id="a65",
               started_at="2026-10-31T12:00:20Z")
    before = (vault / "Prints/print-0064.json").read_bytes()
    result = records.write_completed_record(data)
    assert not result["record_path"].endswith("print-0064.json")
    assert (vault / "Prints/print-0064.json").read_bytes() == before
    if case == "ambiguous":
        assert json.loads((vault / "Prints/print-0065.json").read_text())["status"] == "printing"


def test_repeated_contextless_completion_keeps_one_immutable_record(vault):
    _place(vault, "print-0064.json", print_number=64, record_id="a64")
    data = {"gcode_file": f"3mf_print/{GCODE}", "job_key": f"3mf_print/{GCODE}",
            "status": "completed", "success": True,
            "started_at": "2026-10-31T12:00:30Z", "completed_at": "2026-10-31T13:00:30Z"}
    first = records.write_completed_record(data)
    before = (vault / "Prints/print-0064.json").read_bytes()
    second = records.write_completed_record({**data, "filament_used_g": 999})
    assert first["record_path"] == second["record_path"]
    assert (vault / "Prints/print-0064.json").read_bytes() == before
    assert len(list((vault / "Prints").glob("*.json"))) == 1


def test_unknown_printer_completion_cannot_bridge_two_printers(vault):
    _place(vault, "print-0064.json", print_number=64, record_id="a64", printer="Printer A")
    _place(vault, "print-0065.json", print_number=65, record_id="a65", printer="Printer B")
    _place(vault, "20261031-completion.json", record_id="done", status="completed",
           printer="Klipper 3D Printer", started_at="2026-10-31T12:00:30Z",
           completed_at="2026-10-31T13:00:30Z")
    loaded, skipped = print_stats.load_records(vault)
    assert skipped == []
    assert len(loaded) == 3  # no evidence chooses either printer's start
