"""Offline tests for #317 print event markers (recorder, mapping, failure isolation)."""
import threading

import pytest

from print_handy.hermes import moonraker_event_listener as mel
from print_handy.hermes import print_markers as pm


def test_record_each_event_kind(tmp_path):
    assert pm.record("job.gcode", pm.START, at=1000.0, root=tmp_path)
    assert pm.record("job.gcode", pm.WATCHDOG_ALERT, at=1100.0, detail="spaghetti at layer 48", root=tmp_path)
    assert pm.record("job.gcode", pm.PAUSE, at=1200.0, root=tmp_path)
    assert pm.record("job.gcode", pm.RESUME, at=1300.0, root=tmp_path)
    assert pm.record("job.gcode", pm.COMPLETE, at=2000.0, outcome="success", root=tmp_path)
    rows = pm.markers("job.gcode", root=tmp_path)
    assert [row["event"] for row in rows] == [pm.START, pm.WATCHDOG_ALERT, pm.PAUSE, pm.RESUME, pm.COMPLETE]
    assert rows[-1]["outcome"] == "success"
    assert rows[1]["detail"] == "spaghetti at layer 48"
    # Elapsed derives from the recorded start when the caller has no print clock.
    assert rows[0]["elapsed_s"] == 0.0
    assert rows[1]["elapsed_s"] == 100.0
    assert rows[-1]["elapsed_s"] == 1000.0
    data = pm.load("job.gcode", root=tmp_path)
    assert data["job_key"] == "job.gcode"
    assert data["started_wall"] == 1000.0


def test_explicit_elapsed_wins_over_derived(tmp_path):
    pm.record("job.gcode", pm.START, at=1000.0, root=tmp_path)
    pm.record("job.gcode", pm.PAUSE, at=1900.0, elapsed_s=42.5, root=tmp_path)
    assert pm.markers("job.gcode", root=tmp_path)[-1]["elapsed_s"] == 42.5


def test_markers_come_back_in_wall_clock_order(tmp_path):
    pm.record("job.gcode", pm.START, at=1000.0, root=tmp_path)
    pm.record("job.gcode", pm.PAUSE, at=1500.0, root=tmp_path)
    # A late-arriving earlier marker (clock skew between producers) sorts in.
    pm.record("job.gcode", pm.WATCHDOG_ALERT, at=1250.0, root=tmp_path)
    assert [row["event"] for row in pm.markers("job.gcode", root=tmp_path)] == [
        pm.START, pm.WATCHDOG_ALERT, pm.PAUSE]


def test_outcome_values(tmp_path):
    for outcome in ("success", "failed", "cancelled"):
        key = f"{outcome}.gcode"
        pm.record(key, pm.START, at=10.0, root=tmp_path)
        pm.record(key, pm.COMPLETE, at=20.0, outcome=outcome, root=tmp_path)
        assert pm.markers(key, root=tmp_path)[-1]["outcome"] == outcome
    with pytest.raises(ValueError):
        pm.record("job.gcode", pm.COMPLETE, outcome="exploded", root=tmp_path)
    with pytest.raises(ValueError):
        pm.record("job.gcode", "exploded", root=tmp_path)


def test_missing_record_reads_empty(tmp_path):
    assert pm.markers("never.gcode", root=tmp_path) == []
    assert pm.load("never.gcode", root=tmp_path)["markers"] == []


def test_reprint_resets_the_elapsed_origin(tmp_path):
    # P2-108: printing the same file again must move started_wall to the new
    # start, or later alerts inherit the first print's duration plus the idle
    # gap (starts at 1000 and 10000, alert at 10050 -> 50s, not 9050s).
    pm.record("job.gcode", pm.START, at=1000.0, root=tmp_path)
    pm.record("job.gcode", pm.COMPLETE, at=2000.0, outcome="success", root=tmp_path)
    pm.record("job.gcode", pm.START, at=10000.0, root=tmp_path)
    pm.record("job.gcode", pm.WATCHDOG_ALERT, at=10050.0, root=tmp_path)
    assert pm.load("job.gcode", root=tmp_path)["started_wall"] == 10000.0
    rows = pm.markers("job.gcode", root=tmp_path)
    assert rows[-2]["event"] == pm.START and rows[-2]["elapsed_s"] == 0.0
    assert rows[-1]["event"] == pm.WATCHDOG_ALERT and rows[-1]["elapsed_s"] == 50.0


def test_slug_colliding_filenames_get_separate_records(tmp_path):
    # P2-110: _safe_slug maps both names to 'part-one.gcode'; the records
    # must still be distinct files keyed by the full job key.
    pm.record("part one.gcode", pm.START, at=1000.0, root=tmp_path)
    pm.record("part-one.gcode", pm.START, at=2000.0, root=tmp_path)
    assert pm._path("part one.gcode", root=tmp_path) != pm._path("part-one.gcode", root=tmp_path)
    assert pm.load("part one.gcode", root=tmp_path)["started_wall"] == 1000.0
    assert pm.load("part-one.gcode", root=tmp_path)["started_wall"] == 2000.0
    assert len(pm.markers("part one.gcode", root=tmp_path)) == 1
    assert len(pm.markers("part-one.gcode", root=tmp_path)) == 1


def test_concurrent_records_do_not_lose_markers(tmp_path, monkeypatch):
    # P2-109: the listener records on its thread while a watchdog alert
    # records from an asyncio.to_thread worker; the read-modify-write must
    # be serialized or one append overwrites the other.
    barrier = threading.Barrier(2)
    real_read = pm._read

    def slow_read(job_key, root=None):
        data = real_read(job_key, root)
        try:
            barrier.wait(timeout=3)
        except threading.BrokenBarrierError:
            pass
        return data

    monkeypatch.setattr(pm, "_read", slow_read)
    results = []
    threads = [threading.Thread(target=lambda: results.append(pm.record("job.gcode", pm.START, at=1000.0, root=tmp_path))),
               threading.Thread(target=lambda: results.append(pm.record("job.gcode", pm.PAUSE, at=1100.0, root=tmp_path)))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert results == [True, True]
    assert sorted(row["event"] for row in pm.markers("job.gcode", root=tmp_path)) == [pm.PAUSE, pm.START]


def test_corrupt_record_file_is_replaced_not_fatal(tmp_path):
    pm._path("job.gcode", root=tmp_path).write_text("{not json", encoding="utf-8")
    assert pm.record("job.gcode", pm.START, at=5.0, root=tmp_path)
    assert pm.markers("job.gcode", root=tmp_path)[0]["event"] == pm.START


def test_storage_failure_returns_false_and_never_raises(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(pm, "_write", boom)
    assert pm.record("job.gcode", pm.START, at=1.0, root=tmp_path) is False
    # An unwritable root (a file where the directory should be) also fails soft.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    assert pm.record("job.gcode", pm.START, at=1.0, root=blocker / "sub") is False
    assert pm.record("", pm.START, at=1.0, root=tmp_path) is False


def test_map_to_video_time_proportional():
    assert pm.map_to_video_time(0, 3600, 60) == 0.0
    assert pm.map_to_video_time(1800, 3600, 60) == 30.0
    assert pm.map_to_video_time(3600, 3600, 60) == 60.0


def test_map_to_video_time_edge_cases():
    assert pm.map_to_video_time(10, 0, 60) is None  # zero print duration
    assert pm.map_to_video_time(10, -5, 60) is None
    assert pm.map_to_video_time(10, 3600, 0) is None  # zero video duration
    assert pm.map_to_video_time(None, 3600, 60) is None
    assert pm.map_to_video_time(10, None, 60) is None
    assert pm.map_to_video_time(10, 3600, None) is None
    # Past the end clamps onto the final frame; before the start clamps to 0.
    assert pm.map_to_video_time(7200, 3600, 60) == 60.0
    assert pm.map_to_video_time(-30, 3600, 60) == 0.0


def test_marker_video_time_prefers_elapsed_then_wall_fallback():
    marker = {"event": pm.PAUSE, "elapsed_s": 900.0, "wall_s": 5000.0}
    assert pm.marker_video_time(marker, print_duration_s=3600, video_duration_s=60,
                                started_wall=4100.0) == 15.0
    wall_only = {"event": pm.WATCHDOG_ALERT, "elapsed_s": None, "wall_s": 5900.0}
    assert pm.marker_video_time(wall_only, print_duration_s=3600, video_duration_s=60,
                                started_wall=4100.0) == 30.0
    assert pm.marker_video_time(wall_only, print_duration_s=3600, video_duration_s=60) is None
    assert pm.marker_video_time("junk", print_duration_s=3600, video_duration_s=60) is None


def _wired_root(monkeypatch, tmp_path):
    monkeypatch.setattr(pm, "_root", lambda: tmp_path)
    return tmp_path


def test_transition_markers_full_lifecycle(monkeypatch, tmp_path):
    _wired_root(monkeypatch, tmp_path)
    stats = {"print_duration": 0}
    mel._record_transition_markers("standby", "printing", "job.gcode", stats)
    mel._record_transition_markers("printing", "paused", "job.gcode", {"print_duration": 100})
    mel._record_transition_markers("paused", "printing", "job.gcode", {"print_duration": 100})
    mel._record_transition_markers("printing", "complete", "job.gcode", {"print_duration": 900})
    rows = pm.markers("job.gcode", root=tmp_path)
    assert [row["event"] for row in rows] == [pm.START, pm.PAUSE, pm.RESUME, pm.COMPLETE]
    assert rows[1]["elapsed_s"] == 100.0
    assert rows[-1]["outcome"] == "success"


def test_transition_markers_cancel_and_error_outcomes(monkeypatch, tmp_path):
    _wired_root(monkeypatch, tmp_path)
    mel._record_transition_markers("standby", "printing", "a.gcode", {})
    mel._record_transition_markers("printing", "cancelled", "a.gcode", {})
    rows = pm.markers("a.gcode", root=tmp_path)
    assert [row["event"] for row in rows] == [pm.START, pm.CANCEL, pm.COMPLETE]
    assert rows[-1]["outcome"] == "cancelled"

    mel._record_transition_markers("standby", "printing", "b.gcode", {})
    mel._record_transition_markers("paused", "error", "b.gcode", {})
    rows = pm.markers("b.gcode", root=tmp_path)
    assert [row["event"] for row in rows] == [pm.START, pm.ERROR, pm.COMPLETE]
    assert rows[-1]["outcome"] == "failed"


def test_transition_markers_ignore_non_print_transitions(monkeypatch, tmp_path):
    _wired_root(monkeypatch, tmp_path)
    mel._record_transition_markers("standby", "complete", "job.gcode", {})  # no active print
    mel._record_transition_markers(None, "standby", "job.gcode", {})
    mel._record_transition_markers("printing", "paused", "", {})  # no filename yet
    assert pm.markers("job.gcode", root=tmp_path) == []


def test_transition_markers_survive_storage_failure(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(pm, "_write", boom)
    # The listener path must be unaffected: the helper simply records nothing.
    mel._record_transition_markers("standby", "printing", "job.gcode", {"print_duration": 0})
    mel._record_transition_markers("printing", "complete", "job.gcode", {"print_duration": 5})


def test_visual_alert_records_watchdog_marker(monkeypatch, tmp_path):
    _wired_root(monkeypatch, tmp_path)
    monkeypatch.setenv("PRINT_WATCH_FRAME_DIR", str(tmp_path / "frames"))
    monkeypatch.setattr(mel, "_buttonless_standalone", lambda: True)
    posted = []
    monkeypatch.setattr(mel, "post", lambda text: posted.append(text) or True)
    mel._visual_alert("job.gcode", b"\xff\xd8\xff\xd9", "spaghetti at layer 48: spaghetti")
    rows = pm.markers("job.gcode", root=tmp_path)
    assert [row["event"] for row in rows] == [pm.WATCHDOG_ALERT]
    assert "spaghetti" in rows[0]["detail"]
    assert posted  # the alert itself still went out
