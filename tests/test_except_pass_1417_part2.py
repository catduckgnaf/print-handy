"""#1417 part 2: behavior at the first silent except-pass sites (gcode bounds cache,
launch timing, default-printer stamping, filament slot limit).

Each handler used to swallow its exception with a bare ``pass``. Now each one either
removes the cause or logs the failure, and the print path keeps its result.
"""
import json
import logging

from print_handy import settings
from print_handy.gcode import bounds
from print_handy.jobs import filament, store

SMALL = b"G90\nG1 X10 Y10 Z0.2 E1\nG1 X20 Y20 E2\n"

def test_bounds_cache_corrupt_entry_is_recomputed(tmp_path, monkeypatch):
    monkeypatch.setattr(bounds, "BOUNDS_CACHE", tmp_path)
    expected = bounds.cached_motion_bounds(SMALL)
    entry = next(tmp_path.glob("*.json"))
    entry.write_text("{not json", encoding="utf-8")

    assert bounds.cached_motion_bounds(SMALL) == expected
    assert json.loads(entry.read_text(encoding="utf-8")) != {}

def test_bounds_cache_unwritable_folder_still_returns_bounds(tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("a file, so the cache folder cannot be created", encoding="utf-8")
    monkeypatch.setattr(bounds, "BOUNDS_CACHE", blocker / "cache")

    with caplog.at_level(logging.WARNING, logger=bounds.logger.name):
        result = bounds.cached_motion_bounds(SMALL)

    assert result == bounds.gcode_motion_bounds(SMALL.decode())
    assert any("could not store gcode bounds cache entry" in r.getMessage() for r in caplog.records)

def test_prune_tolerates_entries_removed_by_another_process(tmp_path, monkeypatch):
    monkeypatch.setattr(bounds, "BOUNDS_CACHE", tmp_path)
    monkeypatch.setattr(bounds, "BOUNDS_CACHE_MAX_ENTRIES", 1)
    for i in range(3):
        (tmp_path / f"{i:064x}.p{bounds.BOUNDS_PARSER_VERSION}.json").write_text("{}", encoding="utf-8")

    bounds._prune_bounds_cache()

    assert len(list(tmp_path.glob("*.json"))) == 1

def test_timing_write_failure_never_raises(tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")

    with caplog.at_level(logging.DEBUG, logger=store.logger.name):
        store._timing(blocker / "job.json", "orca_begin")

    assert any("launch timing stage orca_begin not recorded" in r.getMessage() for r in caplog.records)

def test_timing_writes_a_line_when_the_folder_is_writable(tmp_path):
    job = tmp_path / "job.json"
    store._timing(job, "orca_begin", detail="ok")

    lines = (tmp_path / "TIMING.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["stage"] == "orca_begin"

def test_stamp_default_printer_failure_leaves_job_unstamped(monkeypatch, caplog):
    def boom(*_args, **_kwargs):
        raise RuntimeError("roster unavailable")

    monkeypatch.setattr(settings, "default_printer_id", boom)
    job = {"name": "plate"}

    with caplog.at_level(logging.WARNING, logger=store.logger.name):
        store.stamp_job_printer(job)

    assert "printer_id" not in job
    assert any("could not stamp the default printer id" in r.getMessage() for r in caplog.records)

def test_position_limit_keeps_configured_floor_when_adapter_has_no_slot_count():
    class NoSlots:
        @property
        def slot_count(self):
            raise AttributeError("adapter does not report toolheads")

    assert filament.position_limit(NoSlots()) == filament.FILAMENT_SLOTS
