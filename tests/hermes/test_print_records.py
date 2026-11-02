import importlib.util
import json

import pytest
import sys
from pathlib import Path

from print_handy.hermes import print_records as records
from print_handy.hermes import moonraker_discord_announcements as watcher


def test_batch_copy_and_multipart_guards(monkeypatch, tmp_path):
    monkeypatch.setattr(watcher, "plan_is_complete", lambda value: value == "done")
    monkeypatch.setattr(watcher, "_queued_jobs", lambda: [])
    assert watcher._batch_fully_complete({"total_copies": 3, "copy_index": 1}) is False
    assert watcher._batch_fully_complete({"total_copies": 3, "copy_index": 3, "multipart_plan_id": "pending"}) is False
    assert watcher._batch_fully_complete({"total_copies": 3, "copy_index": 3, "multipart_plan_id": "done"}) is True
    monkeypatch.setattr(watcher, "_queued_jobs", lambda: [{"job_id": "next"}])
    assert watcher._batch_fully_complete({"total_copies": 1, "copy_index": 1}) is False


def test_record_keeps_unavailable_values_blank():
    data = records.build_record_data(
        "part.gcode",
        stats={"state": "complete", "print_duration": 42.0},
        metadata={},
        context={"selected_option": records.NATIVE_PRINT_OPTIONS[0]},
    )
    assert data["print_duration_seconds"] == 42.0
    assert data["source_url"] is None
    assert data["author_directed_tree_supports"] is None
    assert data["spoolman_spool_id"] is None


def test_multipart_summary_tracks_multiple_parts_shared_plates_runs_and_estimates():
    summary = records.format_multipart_summary({"parts": [
        {"id": "frame", "label": "Frame", "status": "completed", "quantity": 1,
         "run_id": 1, "plate_id": 1, "gcode_file": "Frame.gcode", "last_gcode_file": "Frame.gcode"},
        {"id": "clip", "label": "Clip", "quantity": 3, "completed_quantity": 0,
         "run_id": 1, "plate_id": 1, "estimated_part_seconds": 3600},
        {"id": "lid", "label": "Lid", "quantity": 1, "completed_quantity": 0,
         "run_id": 2, "plate_id": 1, "plate_estimate_seconds": 3600, "gcode_file": "lid-next.gcode"},
        {"id": "case", "label": "Case", "quantity": 2, "completed_quantity": 0,
         "run_id": 2, "plate_id": 1, "plate_estimate_seconds": 3600},
        {"id": "base", "label": "Base", "quantity": 1, "completed_quantity": 0, "run_id": 3, "plate_id": 1},
        {"id": "blue", "label": "Blue variant", "optional": True},
        {"id": "red", "label": "Red variant", "alternative_group": "color", "selected": False},
    ]}, "Frame.gcode")
    assert "Current print finished: Frame" in summary
    assert "Current plate/run finished: Run 1, Plate 1" in summary
    assert "Remove the finished pieces and clear the bed before the next plate." in summary
    assert "Next required part: Clip" in summary
    assert "Clip: 3 remaining; per-part estimate ~1h 0m" in summary
    assert "Lid: 1 remaining" in summary and "Run 2, Plate 1" in summary
    assert "Case: 2 remaining" in summary
    assert "shared plate estimate ~1h 0m; these pieces share the plate" in summary
    assert "All remaining required parts" in summary and "Base: 1 remaining" in summary
    assert "Combined remaining duration incomplete" in summary
    assert "Blue variant (optional)" in summary and "Red variant (alternative set color)" in summary
    assert "exact next artifact: lid-next.gcode" not in summary
    assert "Start option:" not in summary


def test_multipart_summary_allows_exact_next_artifact_and_empty_remainder():
    summary = records.format_multipart_summary({"parts": [
        {"id": "done", "label": "Finished widget", "status": "completed", "quantity": 2},
        {"id": "next", "label": "Next widget", "quantity": 1, "gcode_file": "next-widget.gcode",
         "plate_id": 3, "run_id": 2, "estimated_part_seconds": 1800},
    ]})
    assert "Start option: Start next plate · bed is clear; require a fresh confirmation for this exact artifact. No automatic advance." in summary
    assert "Combined remaining duration: ~0h 30m" in summary
    complete = records.format_multipart_summary({"parts": [
        {"id": "only", "label": "Only part", "status": "completed", "quantity": 1, "gcode_file": "only.gcode"},
    ]}, "only.gcode")
    assert "Remaining required parts: none. Job complete." in complete
    assert "Start option:" not in complete


def test_multipart_summary_tracks_current_plate_and_sanitized_remaining_plan():
    summary = records.format_multipart_summary({"parts": [
        {"id": "frame", "label": "Frame", "status": "completed", "quantity": 1,
         "run_id": 1, "plate_id": 1, "gcode_file": "frame.gcode", "last_gcode_file": "frame.gcode"},
        {"id": "clip", "label": "Clip", "quantity": 2, "completed_quantity": 0,
         "run_id": 2, "plate_id": 3, "plate_estimate_seconds": 1800, "gcode_file": "private/clip-deadbeef.gcode"},
        {"id": "lid", "label": "Lid", "quantity": 1, "completed_quantity": 0,
         "run_id": 3, "plate_id": 4, "gcode_file": "private/lid.gcode"},
    ]}, "frame.gcode")
    assert "Current print finished: Frame" in summary
    assert "Current plate/run finished: Run 1, Plate 1" in summary
    assert "Remove the finished pieces and clear the bed before the next plate." in summary
    assert "Next required part: Clip (Run 2, Plate 3)" in summary
    assert "Start option:" not in summary
    assert "Clip: 2 remaining" in summary and "Lid: 1 remaining" in summary
    assert "Combined remaining duration incomplete" in summary
    assert "deadbeef" not in summary


def test_multipart_plan_defers_until_every_part(tmp_path, monkeypatch):
    monkeypatch.setattr(records, "PLANS_DIR", tmp_path / "Plans")
    monkeypatch.setattr(records, "PRINTS_DIR", tmp_path / "Prints")
    base = {
        "completed_at": "2026-09-20T15:00:00Z",
        "gcode_file": "part-a.gcode",
        "multipart_plan_id": "dragon",
        "part_id": "a",
        "required_parts": ["a", "b"],
        "status": "completed",
    }
    first = records.write_completed_record(base)
    assert first["cleanup_eligible"] is False
    plan = json.loads((tmp_path / "Plans" / "dragon.json").read_text())
    assert {part["status"] for part in plan["parts"]} == {"completed", "planned"}
    assert [(part["id"], part["completed_quantity"]) for part in plan["parts"]] == [("a", 1), ("b", 0)]
    again = records.write_completed_record(dict(base, gcode_file="a.gcode", part_id=None))
    assert again["cleanup_eligible"] is False
    plan = json.loads((tmp_path / "Plans" / "dragon.json").read_text())
    assert [(part["id"], part["completed_quantity"]) for part in plan["parts"]] == [("a", 1), ("b", 0)]


def test_watcher_records_mocked_moonraker_completion(tmp_path, monkeypatch):
    monkeypatch.setattr(records, "PRINTS_DIR", tmp_path / "Prints")
    monkeypatch.setattr(records, "PLANS_DIR", tmp_path / "Plans")
    monkeypatch.setattr(watcher, "STATE_PATH", tmp_path / "state.json")
    context_path = tmp_path / "context.json"
    context_path.write_text(json.dumps({"jobs": {"part.gcode": {
        "selected_option": records.NATIVE_PRINT_OPTIONS[0],
        "discord_session_id": "session-1",
        "discord_chat_id": "100000000000000001",
    }}}))
    monkeypatch.setenv("PRINT_HANDY_CONTEXT_PATH", str(context_path))
    responses = iter((
        {"job": {"file": {"path": "part.gcode", "display": "part.gcode"}}, "progress": {"completion": 25}, "state": "Printing", "stats": {"state": "printing"}},
        {"job": {"file": {"path": "part.gcode", "display": "part.gcode"}}, "progress": {"completion": 100}, "state": "Completed", "stats": {"state": "complete", "print_duration": 30}},
    ))
    monkeypatch.setattr(watcher, "get_job", lambda: next(responses))
    monkeypatch.setattr(watcher, "get_file_metadata", lambda filename: {"slicer_settings": {"layer_height": 0.2}})
    monkeypatch.setattr(watcher, "get_active_spool", lambda: {})
    assert watcher.main() == 0
    assert watcher.main() == 0
    files = list((tmp_path / "Prints").glob("*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text())
    assert record["status"] == "completed" and record["gcode_file"] == "part.gcode"
    assert record["print_duration_seconds"] == 30 and record["print_number"] is None
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["record_written"] is True
    # #524: a completion no longer queues any agent-session cleanup.
    assert "pending_cleanups" not in state


def test_failing_record_write_does_not_break_the_watcher(tmp_path, monkeypatch):
    """A records-folder failure is logged into watcher state, never raised:
    the print flow (and its Discord posts) must survive a broken record sink."""
    monkeypatch.setattr(records, "PRINTS_DIR", tmp_path / "Prints")
    monkeypatch.setattr(records, "PLANS_DIR", tmp_path / "Plans")
    monkeypatch.setattr(watcher, "STATE_PATH", tmp_path / "state.json")
    context_path = tmp_path / "context.json"
    context_path.write_text(json.dumps({"jobs": {"part.gcode": {
        "selected_option": records.NATIVE_PRINT_OPTIONS[0],
        "discord_session_id": "session-1",
        "discord_chat_id": "100000000000000001",
    }}}))
    monkeypatch.setenv("PRINT_HANDY_CONTEXT_PATH", str(context_path))
    responses = iter((
        {"job": {"file": {"path": "part.gcode", "display": "part.gcode"}}, "progress": {"completion": 25}, "state": "Printing", "stats": {"state": "printing"}},
        {"job": {"file": {"path": "part.gcode", "display": "part.gcode"}}, "progress": {"completion": 100}, "state": "Completed", "stats": {"state": "complete", "print_duration": 30}},
    ))
    monkeypatch.setattr(watcher, "get_job", lambda: next(responses))
    monkeypatch.setattr(watcher, "get_file_metadata", lambda filename: {})
    monkeypatch.setattr(watcher, "get_active_spool", lambda: {})

    def broken_write(data):
        raise OSError("records folder unavailable")

    monkeypatch.setattr(watcher, "write_completed_record", broken_write)
    assert watcher.main() == 0
    assert watcher.main() == 0
    state = json.loads((tmp_path / "state.json").read_text())
    assert "records folder unavailable" in state["record_error"]
    assert not state.get("record_written")

def test_start_record_then_cancel_finalizes_same_record(tmp_path, monkeypatch):
    monkeypatch.setattr(records, "PRINTS_DIR", tmp_path / "Prints")
    monkeypatch.setattr(records, "PLANS_DIR", tmp_path / "Plans")
    ctx = {"job_key": "stand.gcode", "title": "Stand", "started_at": "2026-09-24T03:30:00Z",
           "hardware": "0.6 mm ruby", "estimated_time": "17h 56m", "filament_diameter_mm": 1.75, "filament_density": 1.24}
    path = records.write_start_record(ctx)
    start = json.loads(Path(path).read_text())
    assert start["status"] == "printing" and start["started_at"] == "2026-09-24T03:30:00Z"
    assert start["title"] == "Stand" and "completed_at" not in start
    assert not (tmp_path / "Plans").exists()
    data = records.build_record_data("stand.gcode", stats={"state": "cancelled", "print_duration": 3600, "filament_used": 1000},
                                     context={**ctx, "record_path": path}, completed_at="2026-09-24T04:30:00Z")
    result = records.write_completed_record(data)
    assert result["record_path"] == path and len(list((tmp_path / "Prints").glob("*.json"))) == 1
    final = Path(path).read_text()
    record = json.loads(final)
    assert record["status"] == "cancelled" and record["success"] is False
    assert record["print_duration_seconds"] == 3600 and record["filament_used_g"] == 3.0
    assert record["completed_at"] == "2026-09-24T04:30:00Z"
    records.write_completed_record(records.build_record_data("stand.gcode", stats={"state": "complete"}, context={**ctx, "record_path": path}))
    assert Path(path).read_text() == final  # finalized records are immutable


def test_completed_record_named_by_print_number_with_lookup_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(records, "PRINTS_DIR", tmp_path / "Prints")
    monkeypatch.setattr(records, "PLANS_DIR", tmp_path / "Plans")
    ctx = {"job_key": "bag-clip.gcode", "title": "Print in Place Bag Clip", "print_number": 57,
           "started_at": "2026-10-26T20:00:00Z", "printer": "Flashforge AD5X",
           "source_url": "https://example.test/bag-clip", "source_platform": "Printables",
           "active_spool": {"id": 12, "material": "PLA", "color": "Black", "vendor": "Elegoo"},
           "filament_diameter_mm": 1.75, "filament_density": 1.24}
    start_path = records.write_start_record(ctx)
    assert Path(start_path).name == "print-0057.json"
    assert json.loads(Path(start_path).read_text())["status"] == "printing"
    data = records.build_record_data(
        "bag-clip.gcode",
        stats={"state": "complete", "print_duration": 5432, "filament_used": 4200},
        context={**ctx, "record_path": start_path},
        completed_at="2026-10-26T21:30:32Z",
    )
    result = records.write_completed_record(data)
    assert result["record_path"] == start_path
    files = list((tmp_path / "Prints").glob("*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text())
    assert record["print_number"] == 57 and record["title"] == "Print in Place Bag Clip"
    assert record["gcode_file"] == "bag-clip.gcode" and record["status"] == "completed"
    assert record["success"] is True and record["printer"] == "Flashforge AD5X"
    assert record["started_at"] == "2026-10-26T20:00:00Z" and record["completed_at"] == "2026-10-26T21:30:32Z"
    assert record["print_duration_seconds"] == 5432
    assert record["filament_material"] == "PLA" and record["filament_color"] == "Black"
    assert record["spoolman_spool_id"] == 12 and record["source_url"] == "https://example.test/bag-clip"


def test_second_print_gets_its_own_numbered_file(tmp_path, monkeypatch):
    monkeypatch.setattr(records, "PRINTS_DIR", tmp_path / "Prints")
    monkeypatch.setattr(records, "PLANS_DIR", tmp_path / "Plans")

    def complete(number, name):
        ctx = {"job_key": name, "title": name, "print_number": number,
               "started_at": "2026-10-26T20:00:00Z"}
        data = records.build_record_data(name, stats={"state": "complete", "print_duration": 60},
                                         context=ctx, completed_at="2026-10-26T20:01:00Z")
        return records.write_completed_record(data)["record_path"]

    first = complete(57, "first.gcode")
    first_text = Path(first).read_text()
    second = complete(58, "second.gcode")
    assert Path(first).name == "print-0057.json" and Path(second).name == "print-0058.json"
    assert Path(first).read_text() == first_text  # the second print never touches the first record
    assert json.loads(Path(second).read_text())["print_number"] == 58


def test_print_number_resolved_from_launch_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(records, "PRINTS_DIR", tmp_path / "Prints")
    monkeypatch.setattr(records, "PLANS_DIR", tmp_path / "Plans")
    from print_handy.hermes import print_control
    job_dir = tmp_path / "archive" / "bag-clip"
    job_dir.mkdir(parents=True)
    job_json = job_dir / "JOB.json"
    job_json.write_text(json.dumps({"title": "Bag Clip"}))
    registry = tmp_path / "print_numbers.json"
    registry.write_text(json.dumps({"numbers": {str(job_json.resolve()): 57}}))
    monkeypatch.setattr(print_control, "PRINT_NUMBERS", registry)
    ctx = {"job_key": "bag-clip.gcode", "started_at": "2026-10-26T20:00:00Z",
           "slicer_settings": {"local_gcode": str(job_dir / "bag-clip.gcode")}}
    data = records.build_record_data("bag-clip.gcode", stats={"state": "complete"}, context=ctx,
                                     completed_at="2026-10-26T21:00:00Z")
    assert data["print_number"] == 57
    result = records.write_completed_record(data)
    assert Path(result["record_path"]).name == "print-0057.json"


def test_cleanup_status_lands_on_the_json_record(tmp_path, monkeypatch):
    monkeypatch.setattr(records, "PRINTS_DIR", tmp_path / "Prints")
    monkeypatch.setattr(records, "PLANS_DIR", tmp_path / "Plans")
    ctx = {"job_key": "stand.gcode", "title": "Stand", "started_at": "2026-09-24T03:30:00Z"}
    data = records.build_record_data("stand.gcode", stats={"state": "complete"}, context=ctx,
                                     completed_at="2026-09-24T04:30:00Z")
    path = records.write_completed_record(data)["record_path"]
    assert json.loads(Path(path).read_text())["cleanup_status"] == "pending"
    records.update_cleanup_record(path, "archived", "session-9", "unique channel session matched")
    record = json.loads(Path(path).read_text())
    assert record["cleanup_status"] == "archived" and record["cleanup_session"] == "session-9"
    assert record["status"] == "completed"  # the record itself is untouched otherwise


def test_cancel_to_standby_finalizes_record_from_history(tmp_path, monkeypatch):
    """Klipper drops to standby with an empty filename on cancel; the record must still close."""
    monkeypatch.setattr(records, "PRINTS_DIR", tmp_path / "Prints")
    monkeypatch.setattr(watcher, "STATE_PATH", tmp_path / "state.json")
    ctx = {"job_key": "stand.gcode", "title": "Stand", "started_at": "2026-09-24T03:31:54Z",
           "filament_diameter_mm": 1.75, "filament_density": 1.24}
    ctx["record_path"] = records.write_start_record(ctx)
    context_path = tmp_path / "context.json"
    context_path.write_text(json.dumps({"jobs": {"stand.gcode": ctx}}))
    monkeypatch.setenv("PRINT_HANDY_CONTEXT_PATH", str(context_path))
    responses = iter((
        {"job": {"file": {"path": "stand.gcode", "display": "stand.gcode"}}, "progress": {"completion": 0.4}, "state": "Printing", "stats": {"state": "printing"}},
        {"job": {"file": {"path": "stand.gcode", "display": "stand.gcode"}}, "progress": {"completion": 0.4}, "state": "Printing", "stats": {"state": "printing"}},
        {"job": {"file": {"name": None}}, "progress": {"completion": None}, "state": "Operational", "stats": {"state": "standby"}},
    ))
    monkeypatch.setattr(watcher, "get_job", lambda: next(responses))
    history = {"result": {"jobs": [{"filename": "stand.gcode", "status": "cancelled", "print_duration": 790,
                                    "filament_used": 478.5, "end_time": 1790221997}]}}
    monkeypatch.setattr(watcher, "get_json", lambda path: history if path.startswith("/server/history") else {})
    monkeypatch.setattr(watcher, "get_active_spool", lambda: {})
    monkeypatch.setattr(watcher, "get_file_metadata", lambda filename: {})
    for _ in range(3):
        assert watcher.main() == 0
    record = json.loads(Path(ctx["record_path"]).read_text())
    assert record["status"] == "cancelled" and record["success"] is False
    assert record["print_duration_seconds"] == 790
    assert len(list((tmp_path / "Prints").glob("*.json"))) == 1


def test_record_identity_uses_history_start_time_not_the_completion_clock():
    """P2-150: with no started_at in the context, Moonraker's history
    start time (epoch, riding stats) anchors the record — a completion
    retry at a later wall-clock time is the same record, not a copy."""
    from datetime import datetime, timezone
    start = 1790220700
    iso = datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stats = {"state": "complete", "print_duration": 60, "start_time": start}
    first = records.build_record_data("job.gcode", stats=stats, metadata={}, context={},
                                      completed_at="2026-09-24T04:00:00Z")
    retry = records.build_record_data("job.gcode", stats=stats, metadata={}, context={},
                                      completed_at="2026-09-24T04:05:00Z")
    assert first["started_at"] == iso
    assert records._record_id(first) == records._record_id(retry)
    assert records._record_path_for(first, "x") == records._record_path_for(retry, "x")


def test_record_completion_retry_writes_one_record(tmp_path, monkeypatch):
    """P2-150 end to end: record_completion consults Moonraker history
    for the finished job, so a retry names the same file and the record
    carries the history start/end instead of per-call clock values."""
    from datetime import datetime, timezone
    monkeypatch.setattr(records, "PRINTS_DIR", tmp_path / "Prints")
    monkeypatch.setattr(records, "PLANS_DIR", tmp_path / "Plans")
    start, end = 1790220700, 1790224300
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    history = {"result": {"jobs": [{"filename": "job.gcode", "status": "completed",
                                    "start_time": start, "end_time": end,
                                    "print_duration": 3600, "filament_used": 100.0}]}}
    monkeypatch.setattr(watcher, "get_json",
                        lambda path: history if path.startswith("/server/history") else {})
    monkeypatch.setattr(watcher, "get_file_metadata", lambda filename: {})
    monkeypatch.setattr(watcher, "get_active_spool", lambda: {})
    monkeypatch.setattr(watcher, "load_context", lambda job_key: {})
    clocks = iter(("2026-09-24T05:00:00Z", "2026-09-24T05:07:00Z"))
    monkeypatch.setattr(watcher, "utc_now", lambda: next(clocks))
    stats = {"state": "complete", "print_duration": 3600}
    one = watcher.record_completion("job.gcode", "job.gcode", stats)
    two = watcher.record_completion("job.gcode", "job.gcode", stats)
    assert one["record_path"] == two["record_path"]
    files = list((tmp_path / "Prints").glob("*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text())
    assert record["started_at"] == datetime.fromtimestamp(start, timezone.utc).strftime(fmt)
    assert record["completed_at"] == datetime.fromtimestamp(end, timezone.utc).strftime(fmt)


def test_multipart_summary_shows_remaining_filament_for_multi_copy_parts():
    """#438: remaining filament scales by the copies still to go, like the remaining duration."""
    summary = records.format_multipart_summary({"parts": [
        {"id": "clip", "label": "Clip", "quantity": 3, "completed_quantity": 1,
         "estimated_part_seconds": 3600, "estimated_filament_g": 10.0},
        {"id": "lid", "label": "Lid", "quantity": 2, "completed_quantity": 0,
         "estimated_part_seconds": 1800, "estimated_filament_g": 5.0}]})
    assert "Combined remaining duration: ~3h 0m" in summary
    assert "Combined remaining filament: ~30 g" in summary  # 2x10 g + 2x5 g still to go


def test_multipart_summary_filament_line_only_when_copies_in_play():
    one_each = records.format_multipart_summary({"parts": [
        {"id": "clip", "label": "Clip", "quantity": 1, "completed_quantity": 0,
         "estimated_part_seconds": 3600, "estimated_filament_g": 10.0}]})
    assert "Combined remaining filament" not in one_each  # single-copy wording unchanged
    partial = records.format_multipart_summary({"parts": [
        {"id": "clip", "label": "Clip", "quantity": 2, "completed_quantity": 0,
         "estimated_part_seconds": 3600, "estimated_filament_g": 10.0},
        {"id": "lid", "label": "Lid", "quantity": 1, "completed_quantity": 0,
         "estimated_part_seconds": 1800}]})
    assert "Combined remaining filament: at least ~20 g (unknown for some remaining parts)" in partial
