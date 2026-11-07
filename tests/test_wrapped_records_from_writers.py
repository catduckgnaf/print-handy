"""Wrapped records-from-writers tests (testing-hardening pass, E8).

The existing Wrapped tests aggregate HAND-WRITTEN record dicts
(``tests/test_print_stats_wrapped.py``'s RECORDS). That is the fixture
realism drift pattern: if the real record writer's shape drifts from
the hand-written shape, the aggregate tests stay green while the Data
tab reads garbage. These tests build every record through the real
writers (``print_records.build_record_data`` /
``write_completed_record``), persist them into a synthetic vault, and
aggregate what ``print_stats.load_records`` reads back.

Two defects from the #981 deep dive are pinned here as fixed by Wrapped
phase 2 (PR #986), and were strict xfails until it landed:

* bucketing in the configured display timezone: a print started Dec 31
  19:00 America/New_York (stored as 2027-01-01T00:00Z) counts in the
  operator's 2026 when the configured zone is passed, as every Wrapped
  caller does;
* the video compile's mixed-fps pacing: two 4 s clips at 30 fps and
  10 fps plus three 3 s cards must measure 17 s with a real ffmpeg
  (it measured 15.6 s before the fix).
"""
import shutil
import subprocess

import pytest

from print_handy.core import print_stats
from print_handy.hermes import print_records as records

SPOOL = {"vendor": "Generic", "material": "PLA", "color": "Black",
         "spool_id": 3, "id": 3}


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    prints = tmp_path / "Prints"
    plans = tmp_path / "Plans"
    prints.mkdir()
    plans.mkdir()
    monkeypatch.setattr(records, "PRINTS_DIR", prints)
    monkeypatch.setattr(records, "PLANS_DIR", plans)
    return tmp_path


def write_print(vault, number, filename, *, state, started_at,
                duration=3600.0, used_mm=1000.0):
    data = records.build_record_data(
        filename,
        stats={"state": state, "print_duration": duration,
               "filament_used": used_mm},
        context={"print_number": number, "title": f"Print {number}",
                 "started_at": started_at, "printer": "AD5X",
                 "active_spool": SPOOL})
    records.write_completed_record(data)
    return data


def test_aggregates_come_from_writer_produced_records(vault):
    write_print(vault, 1, "benchy.gcode", state="complete",
                started_at="2026-03-04T15:00:00Z")
    write_print(vault, 2, "clip.gcode", state="complete",
                started_at="2026-03-05T16:00:00Z")
    write_print(vault, 3, "vase.gcode", state="error",
                started_at="2026-04-01T02:30:00Z")
    loaded, skipped = print_stats.load_records(vault)
    assert skipped == [] and len(loaded) == 3
    agg = print_stats.aggregate(loaded, year=2026)
    assert agg["total_prints"] == 3
    assert agg["completed"] == 2 and agg["failed"] == 1
    assert agg["success_rate"] == round(2 / 3, 4)
    assert agg["monthly"] == {"2026-03": 2, "2026-04": 1}
    # The writer's spool fields feed the breakdowns unchanged.
    assert agg["breakdowns"]["by_color"]["Black"]["count"] == 3
    assert agg["breakdowns"]["by_material"]["PLA"]["count"] == 3


def test_a_start_record_finalizes_in_place_through_the_writers(vault):
    path = records.write_start_record(
        {"job_key": "job-9", "print_number": 9, "title": "Hook",
         "started_at": "2026-05-01T12:00:00Z", "printer": "AD5X"})
    loaded, _ = print_stats.load_records(vault)
    assert len(loaded) == 1 and loaded[0]["status"] == "printing"
    data = records.build_record_data(
        "job-9",  # the start record's identity is the job key, as in the real flow
        stats={"state": "complete", "print_duration": 60.0},
        context={"print_number": 9, "title": "Hook", "job_key": "job-9",
                 "started_at": "2026-05-01T12:00:00Z", "printer": "AD5X",
                 "record_path": path})
    records.write_completed_record(data)
    loaded, skipped = print_stats.load_records(vault)
    assert skipped == [] and len(loaded) == 1  # finalized, not duplicated
    assert loaded[0]["status"] == "completed"
    agg = print_stats.aggregate(loaded, year=2026)
    assert agg["completed"] == 1 and agg["in_progress"] == 0


def test_new_years_eve_print_buckets_into_the_local_year(vault):
    """#981 phase 2 (PR #986): Wrapped buckets in the configured display
    zone, so the operator's Dec 31 evening print counts in their year."""
    write_print(vault, 4, "nye.gcode", state="complete",
                started_at="2026-12-31T19:00:00-05:00")
    loaded, skipped = print_stats.load_records(vault)
    assert skipped == [] and len(loaded) == 1
    agg = print_stats.aggregate(loaded, year=2026, tz="America/New_York")
    assert agg["total_prints"] == 1, (
        "the operator's Dec 31 evening print must count in their 2026")
    assert agg["monthly"] == {"2026-12": 1}


# ------------------------------------------------------------- ffmpeg pacing

ffmpeg = shutil.which("ffmpeg")
ffprobe = shutil.which("ffprobe")


@pytest.mark.skipif(not (ffmpeg and ffprobe),
                    reason="ffmpeg/ffprobe not on PATH")
def test_wrapped_video_duration_matches_its_segments(tmp_path):
    from print_handy.webui import wrapped_video

    def make_clip(name, rate):
        path = tmp_path / name
        subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i",
             f"testsrc=size=640x360:rate={rate}:duration=5",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
            check=True)
        return path

    clips = [make_clip("clip30.mp4", 30), make_clip("clip10.mp4", 10)]
    wrapped = {"script": ["Your year in prints", "Second card"],
               "headline": {}, "highlights": {}}
    out_dir = tmp_path / "out"
    result = wrapped_video.build_wrapped_video(
        2026, wrapped, out_dir,
        find_clips=lambda year: clips)
    assert result["ok"], result.get("reason")
    probe = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", result["path"]],
        capture_output=True, text=True, check=True)
    measured = float(probe.stdout.strip())
    # Phase 5 (#981) timeline: the slide arc, then ONE full-bleed footage
    # moment (the first clip) and the remaining clips at CLIP_SECONDS each.
    expected = (sum(s["duration"] for s in wrapped_video.build_slides(wrapped))
                + wrapped_video.FOOTAGE_SECONDS
                + (len(clips) - 1) * wrapped_video.CLIP_SECONDS)
    assert abs(measured - expected) <= 0.75, (
        f"compiled video runs {measured:.2f}s for segments totaling "
        f"{expected:.2f}s — the mixed-fps concat pacing is off")
