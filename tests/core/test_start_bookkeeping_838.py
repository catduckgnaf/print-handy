"""#838 start bookkeeping, core side: launch checkpoint hashes (item 5),
the pending active-spool retry (item 6), and the blend-spool color check
(item 10). Offline only: Spoolman and Moonraker are fakes throughout.
"""
import json
from pathlib import Path

import pytest

from print_handy.core import filament_color, model_job


# ---------------------------------------------------------------- item 5: the checkpoint guard

def _checkpoint(tmp_path, data):
    cp = tmp_path / "plate.launch.json"
    cp.write_text(json.dumps(data))
    return cp


def test_checkpoint_for_a_different_delivery_does_not_block_resubmission(tmp_path):
    """A stale .gcode checkpoint must not block the same plate resubmitted as .gcode.3mf."""
    cp = _checkpoint(tmp_path, {"phase": "submission_pending", "file": "x_abc.gcode",
                                "sha256": "a" * 64})
    model_job._require_launch_checkpoint_clear(cp, False, remote="x_abc.gcode.3mf")  # no raise


def test_checkpoint_for_the_same_file_still_blocks_replay(tmp_path):
    cp = _checkpoint(tmp_path, {"phase": "submission_pending", "file": "x_abc.gcode.3mf",
                                "sha256": "a" * 64})
    with pytest.raises(RuntimeError, match="checkpoint exists"):
        model_job._require_launch_checkpoint_clear(cp, False, remote="x_abc.gcode.3mf")


def test_checkpoint_without_a_remote_still_blocks_as_before(tmp_path):
    cp = _checkpoint(tmp_path, {"phase": "submission_pending", "file": "x_abc.gcode"})
    with pytest.raises(RuntimeError, match="checkpoint exists"):
        model_job._require_launch_checkpoint_clear(cp, False)


def test_old_checkpoint_without_file_key_does_not_crash_and_still_blocks(tmp_path):
    """Checkpoints written before the file comparison existed carry no usable
    delivery identity beyond the phase; they keep the old fail-closed reading."""
    cp = _checkpoint(tmp_path, {"phase": "submission_pending", "sha256": "a" * 64})
    with pytest.raises(RuntimeError, match="checkpoint exists"):
        model_job._require_launch_checkpoint_clear(cp, False, remote="x_abc.gcode.3mf")


def test_not_sent_checkpoint_never_blocks(tmp_path):
    cp = _checkpoint(tmp_path, {"phase": "not_submitted", "file": "x_abc.gcode.3mf"})
    model_job._require_launch_checkpoint_clear(cp, False, remote="x_abc.gcode.3mf")


# ---------------------------------------------------------------- item 6: pending spool retry

PENDING = {"spool_id": 11, "slot": 2, "reason": "Spoolman unavailable at print start; "
           "usage not yet reported", "recorded_at": 0}


def _job_with_pending(tmp_path, monkeypatch, pending=PENDING, printer_id=None):
    archive = tmp_path / "archive"
    folder = archive / "job-a"
    sliced = folder / "sliced" / "a" / "b"
    sliced.mkdir(parents=True)
    g = sliced / "plate.gcode"
    g.write_text("G1 X0\n")
    # The launch record resolve_print_target maps the printer's file name by.
    g.with_suffix(".launch.json").write_text(json.dumps({"file": "plate.gcode", "phase": "executing"}))
    job = {"title": "job a", "gcode": str(g)}
    if printer_id:
        job["printer_id"] = printer_id
    if pending is not None:
        job["spoolman_usage_pending"] = dict(pending)
    (folder / "JOB.json").write_text(json.dumps(job))
    monkeypatch.setattr(model_job, "ARCHIVE", archive)
    monkeypatch.setattr(model_job.printer_state, "load",
                        lambda printer_id=None: {"filament_slots": {"2": {"spoolman_id": 11}}})
    return folder, g


def test_pending_spool_sync_retries_and_clears_the_record(tmp_path, monkeypatch):
    folder, _g = _job_with_pending(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(model_job, "_sync_active_spool",
                        lambda st, slot: calls.append(slot) or {"spool_id": 11, "changed": True})
    assert model_job.retry_pending_spool_sync("plate.gcode") is True
    assert calls == [2]
    assert "spoolman_usage_pending" not in json.loads((folder / "JOB.json").read_text())


def test_pending_spool_sync_leaves_the_record_when_the_retry_fails(tmp_path, monkeypatch):
    folder, _g = _job_with_pending(tmp_path, monkeypatch)

    def boom(st, slot):
        raise RuntimeError("Moonraker active spool read failed")
    monkeypatch.setattr(model_job, "_sync_active_spool", boom)
    assert model_job.retry_pending_spool_sync("plate.gcode") is False
    assert json.loads((folder / "JOB.json").read_text())["spoolman_usage_pending"]["spool_id"] == 11


def test_pending_spool_sync_leaves_the_record_when_still_pending(tmp_path, monkeypatch):
    folder, _g = _job_with_pending(tmp_path, monkeypatch)
    monkeypatch.setattr(model_job, "_sync_active_spool",
                        lambda st, slot: {"spool_id": 11, "spoolman_pending": True})
    assert model_job.retry_pending_spool_sync("plate.gcode") is False
    assert "spoolman_usage_pending" in json.loads((folder / "JOB.json").read_text())


def test_pending_spool_sync_without_a_spool_id_is_not_retried(tmp_path, monkeypatch):
    multi = {"spool_id": None, "stale_spool_id": 4, "slots": [1, 2],
             "reason": "multi-slot clear pending", "recorded_at": 0}
    _folder, _g = _job_with_pending(tmp_path, monkeypatch, pending=multi)
    monkeypatch.setattr(model_job, "_sync_active_spool",
                        lambda st, slot: pytest.fail("multi-slot pending must not be retried"))
    assert model_job.retry_pending_spool_sync("plate.gcode") is False


def test_pending_spool_sync_for_an_unknown_file_is_a_quiet_no(tmp_path, monkeypatch):
    _job_with_pending(tmp_path, monkeypatch, pending=None)
    assert model_job.retry_pending_spool_sync("never-uploaded.gcode") is False


def test_pending_spool_sync_routes_to_the_jobs_roster_printer(tmp_path, monkeypatch):
    """Multi-printer roster: the retry must load the JOB's printer record —
    printer_state.load() with no ID reads the legacy/default record, whose
    Moonraker endpoint belongs to a different printer (Codex P1 on #847)."""
    folder, _g = _job_with_pending(tmp_path, monkeypatch, printer_id="printer-b")
    loads = []

    def load(printer_id=None):
        loads.append(printer_id)
        return {"filament_slots": {"2": {"spoolman_id": 11}},
                "moonraker_url": f"http://{printer_id}.example/"}
    monkeypatch.setattr(model_job.printer_state, "load", load)
    seen = {}

    def fake_sync(st, slot, printer_id=None):
        seen.update(st=st, slot=slot, printer_id=printer_id)
        return {"spool_id": 11, "changed": True}
    monkeypatch.setattr(model_job, "_sync_active_spool", fake_sync)
    assert model_job.retry_pending_spool_sync("plate.gcode") is True
    assert loads == ["printer-b"]
    assert seen["printer_id"] == "printer-b"
    assert seen["st"]["moonraker_url"] == "http://printer-b.example/"
    assert seen["slot"] == 2
    assert "spoolman_usage_pending" not in json.loads((folder / "JOB.json").read_text())


def test_pending_spool_sync_explicit_printer_id_wins_over_the_job_record(tmp_path, monkeypatch):
    """The listener's own printer identity, when carried in, is the route."""
    _job_with_pending(tmp_path, monkeypatch, printer_id="printer-a")
    loads = []

    def load(printer_id=None):
        loads.append(printer_id)
        return {"filament_slots": {"2": {"spoolman_id": 11}}}
    monkeypatch.setattr(model_job.printer_state, "load", load)
    monkeypatch.setattr(model_job, "_sync_active_spool",
                        lambda st, slot, printer_id=None: {"spool_id": 11, "changed": True})
    assert model_job.retry_pending_spool_sync("plate.gcode", printer_id="printer-b") is True
    assert loads == ["printer-b"]


def test_sync_active_spool_reloads_and_saves_the_same_printer_record(monkeypatch):
    """_sync_active_spool's post-write reload/save must stay on the printer
    the launch state belongs to, not fall back to the legacy record."""
    st = {"moonraker_url": "http://printer-b.example/",
          "filament_slots": {"2": {"spoolman_id": 11, "material": "PLA", "name": "Black PLA"}},
          "spool": {"spoolman_id": 10}}
    active = [10]

    def mr(_st, _path, data=None, **_kw):
        if data is not None:
            active[0] = data["spool_id"]
        return {"result": {"spool_id": active[0]}}
    monkeypatch.setattr(model_job, "_mr", mr)
    monkeypatch.setattr(model_job.spoolman_client, "get_spool",
                        lambda sid, base_url=None: {"archived": False,
                                                    "filament": {"material": "PLA", "name": "Black PLA"}})
    loads, saves = [], []
    monkeypatch.setattr(model_job.printer_state, "load",
                        lambda printer_id=None: loads.append(printer_id) or {"spool": {}})
    monkeypatch.setattr(model_job.printer_state, "save",
                        lambda fresh, printer_id=None: saves.append((fresh, printer_id)))
    result = model_job._sync_active_spool(st, 2, printer_id="printer-b")
    assert result["spool_id"] == 11
    assert loads == ["printer-b"]
    assert len(saves) == 1 and saves[0][1] == "printer-b"
    assert saves[0][0]["spool"]["spoolman_id"] == 11


# ---------------------------------------------------------------- item 10: blend hexes

BLEND = {"hex": "FF0000", "hex_source": "spoolman", "multi_color_hexes": "FF0000,0000FF"}


def test_blend_second_hex_matches_the_screen_color():
    """A HelixScreen slot set to the blend's second color is the same spool, not a mismatch."""
    model_job._check_mapped_slot_color(1, 0, "0000FF", dict(BLEND))  # no refusal, no warning path


def test_blend_second_hex_is_not_gathered_as_a_mismatch():
    found = []
    model_job._check_mapped_slot_color(1, 0, "0000FF", dict(BLEND), frozenset(), found)
    assert found == []


def test_blend_unrelated_color_still_refuses():
    with pytest.raises(SystemExit, match="REFUSED"):
        model_job._check_mapped_slot_color(1, 0, "00FF00", dict(BLEND))


def test_record_without_multi_color_hexes_behaves_as_before():
    with pytest.raises(SystemExit, match="REFUSED"):
        model_job._check_mapped_slot_color(1, 0, "0000FF",
                                           {"hex": "FF0000", "hex_source": "spoolman"})
    model_job._check_mapped_slot_color(1, 0, "FF0000",
                                       {"hex": "FF0000", "hex_source": "spoolman"})


def test_all_hexes_parses_a_blend_list():
    assert filament_color.all_hexes("FF0000, 0000ff, junk") == ["FF0000", "0000FF"]
    assert filament_color.all_hexes("FF0000") == ["FF0000"]
    assert filament_color.all_hexes(None) == []
