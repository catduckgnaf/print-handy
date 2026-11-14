"""#1476: TIMING.jsonl read back into a report of the real waits.

Covers the front-of-flow capture in jobs/store.py, the reader and
span math in jobs/timing_report.py, and the three renderings of the
one report (doctor, diagnostics bundle, release checklist input),
plus the module CLI the hardware smoke calls.
"""
import importlib.util
import json
from pathlib import Path

import pytest

from print_handy import diagnostics_bundle, doctor
from print_handy.jobs import store, timing_report

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "build_release_checklist", ROOT / "scripts" / "build_release_checklist.py")
checklist = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checklist)


def _write_job(archive, name, lines):
    folder = Path(archive) / name
    folder.mkdir(parents=True, exist_ok=True)
    text = "".join(
        (json.dumps(line) if not isinstance(line, str) else line) + "\n" for line in lines)
    (folder / "TIMING.jsonl").write_text(text, encoding="utf-8")
    return folder


def _full_job(archive, name, base=1000.0):
    return _write_job(archive, name, [
        {"stage": "message_received", "t": base},
        {"stage": "first_question_posted", "t": base + 2},
        {"stage": "download_done", "t": base + 3},
        {"stage": "inspection_done", "t": base + 5},
        {"stage": "intake_slice_requested", "t": base + 30},
        {"stage": "slice_start", "t": base + 30},
        {"stage": "verify_begin", "t": base + 38},
        {"stage": "verify_done", "t": base + 42},
        {"stage": "slice_done", "t": base + 42},
        {"stage": "intake_slice_returned", "t": base + 43},
        {"stage": "intake_start_tapped", "t": base + 60},
        {"stage": "print_now_start", "t": base + 61},
        {"stage": "preflight_verify_done", "t": base + 64},
        {"stage": "start_submitted", "t": base + 70},
        {"stage": "intake_start_returned", "t": base + 100},
    ])


# -- front-of-flow capture (jobs/store.py) --------------------------------

def test_timing_at_overrides_the_stamp(tmp_path):
    job = tmp_path / "JOB.json"
    store._timing(job, "slice_start", at=1234.5)
    line = json.loads((tmp_path / "TIMING.jsonl").read_text().splitlines()[0])
    assert line["stage"] == "slice_start" and line["t"] == 1234.5


def test_front_capture_buffers_until_bind_then_writes_in_order(tmp_path):
    token = store.front_begin()
    try:
        store.front_mark("first_question_posted")
        store.front_mark("download_done")
        job = tmp_path / "JOB.json"
        assert not (tmp_path / "TIMING.jsonl").exists()  # nothing written before bind
        store.front_bind(job)
        store.front_mark("inspection_done")  # after bind: written at once
    finally:
        store.front_end(token)
    rows = [json.loads(line) for line in (tmp_path / "TIMING.jsonl").read_text().splitlines()]
    assert [row["stage"] for row in rows] == [
        "message_received", "first_question_posted", "download_done", "inspection_done"]
    times = [row["t"] for row in rows]
    assert times == sorted(times)


def test_front_capture_first_stamp_wins_and_no_buffer_is_a_noop(tmp_path):
    store.front_mark("download_done")  # no buffer open: must not raise or write
    token = store.front_begin()
    try:
        store.front_bind(tmp_path / "JOB.json")
        store.front_mark("download_done")
        first = (tmp_path / "TIMING.jsonl").read_text()
        store.front_mark("download_done")
        assert (tmp_path / "TIMING.jsonl").read_text() == first
    finally:
        store.front_end(token)


def test_front_capture_never_raises_on_a_missing_folder(tmp_path):
    token = store.front_begin()
    try:
        store.front_bind(tmp_path / "gone" / "JOB.json")  # _timing is fail-soft
        store.front_mark("download_done")
    finally:
        store.front_end(token)


# -- reader and span math ---------------------------------------------------

def test_reader_skips_malformed_lines_and_partial_files(tmp_path):
    _write_job(tmp_path, "job-a", [
        {"stage": "slice_start", "t": 10.0},
        "this is not json",
        {"stage": "slice_done"},  # no t: skipped
        {"stage": 42, "t": 12.0},  # stage not a string: skipped
        {"stage": "slice_done", "t": 22.0},
        "",
    ])
    stages, lost = timing_report.read_timing_file(tmp_path / "job-a" / "TIMING.jsonl")
    assert stages == {"slice_start": [10.0], "slice_done": [22.0]}
    assert lost == 0
    assert timing_report.read_timing_file(tmp_path / "missing" / "TIMING.jsonl") is None


def test_span_pairing_uses_the_last_slice_start(tmp_path):
    stages = {"slice_start": [0.0, 100.0], "slice_done": [50.0, 130.0]}
    assert timing_report.job_spans(stages)["slice"] == 30.0


def test_report_computes_median_p90_and_counts(tmp_path):
    _full_job(tmp_path, "job-a", base=1000.0)
    _full_job(tmp_path, "job-b", base=2000.0)
    _write_job(tmp_path, "job-c", [  # partial: slice only, plus a lost start
        {"stage": "slice_start", "t": 5.0},
        {"stage": "slice_done", "t": 65.0},
        {"stage": "start_response_lost", "t": 70.0, "detail": "TimeoutError"},
        {"stage": "start_response_lost", "t": 71.0, "detail": "TimeoutError"},
    ])
    _write_job(tmp_path, "job-d", ["garbage only"])
    report = timing_report.build_report(tmp_path, limit=100)
    assert report["jobs_seen"] == 4 and report["jobs_used"] == 3
    assert report["jobs_skipped"] == 1
    spans = report["spans"]
    assert spans["link_to_first_question"]["median"] == 2.0
    assert spans["link_to_first_question"]["n"] == 2
    assert spans["last_answer_to_ready"]["median"] == 13.0
    assert spans["slice"]["median"] == 12.0 and spans["slice"]["p90"] == 60.0
    assert spans["slice"]["n"] == 3
    assert spans["slice_verify"]["median"] == 4.0
    assert spans["launch_preflight"]["median"] == 3.0
    assert spans["tap_to_start_accepted"]["median"] == 10.0
    assert spans["tap_to_print_started"]["median"] == 40.0
    assert report["start_response_lost"] == {"occurrences": 2, "jobs": 1}
    text = timing_report.render_text(report)
    assert "Link to first question" in text and "Start responses lost: 2" in text


def test_report_on_an_empty_archive_measures_nothing(tmp_path):
    report = timing_report.build_report(tmp_path / "nope")
    assert report["jobs_used"] == 0
    assert all(span["median"] is None for span in report["spans"].values())
    assert "no readable TIMING.jsonl" in timing_report.render_text(report)


def test_budgets_flag_overruns_and_check_budgets_names_them(tmp_path):
    _write_job(tmp_path, "job-a", [
        {"stage": "slice_start", "t": 0.0}, {"stage": "slice_done", "t": 500.0}])
    report = timing_report.build_report(tmp_path)
    assert report["spans"]["slice"]["over_budget"] == ["median", "p90"]
    violations = timing_report.check_budgets(report)
    assert len(violations) == 2 and all("Slice" in v for v in violations)


def test_movement_needs_both_thresholds(tmp_path):
    assert timing_report.movement(10.0, 11.0) is None  # 10%: too small a share
    assert timing_report.movement(1.0, 1.4) is None  # 40% but only 0.4 s
    moved = timing_report.movement(10.0, 13.0)
    assert moved["direction"] == "slower" and moved["delta_s"] == 3.0
    assert timing_report.movement(10.0, 7.0)["direction"] == "faster"
    assert timing_report.movement(None, 7.0) is None


def test_report_flags_movement_against_a_previous_summary(tmp_path):
    _full_job(tmp_path, "job-a", base=1000.0)
    previous = {"spans": {"slice": {"median": 5.0}}}
    report = timing_report.build_report(tmp_path, previous=previous)
    assert report["spans"]["slice"]["moved"]["direction"] == "slower"
    assert report["spans"]["tap_to_print_started"]["moved"] is None


def test_summary_round_trips_through_json(tmp_path):
    _full_job(tmp_path, "job-a", base=1000.0)
    summary = timing_report.summary_dict(timing_report.build_report(tmp_path))
    path = tmp_path / "summary.json"
    path.write_text(json.dumps(summary), encoding="utf-8")
    loaded = timing_report.load_summary(path)
    assert loaded["spans"]["slice"]["median"] == 12.0
    assert loaded["spans"]["slice"]["label"] == "Slice"
    assert timing_report.load_summary(tmp_path / "missing.json") is None


# -- the CLI the hardware smoke calls ---------------------------------------

def test_cli_exit_codes_and_summary_out(tmp_path):
    archive = tmp_path / "archive"
    _full_job(archive, "job-a", base=1000.0)
    out = tmp_path / "summary.json"
    assert timing_report.main(["--archive", str(archive), "--summary-out", str(out)]) == 0
    assert json.loads(out.read_text())["spans"]["slice"]["median"] == 12.0
    assert timing_report.main(["--archive", str(archive), "--check-budgets"]) == 0
    _write_job(archive, "job-b", [
        {"stage": "slice_start", "t": 0.0}, {"stage": "slice_done", "t": 900.0}])
    assert timing_report.main(["--archive", str(archive), "--check-budgets"]) == 1
    assert timing_report.main(
        ["--archive", str(tmp_path / "empty"), "--check-budgets"]) == 2


# -- doctor and bundle renderings -------------------------------------------

def test_doctor_check_timing(tmp_path):
    data = {"paths": {"archive": str(tmp_path)}}
    assert doctor.check_timing(data) is None  # nothing measured yet
    _full_job(tmp_path, "job-a", base=1000.0)
    text, over_budget = doctor.check_timing(data)
    assert "Tap to print started" in text and over_budget is False


def test_bundle_timing_section(tmp_path):
    data = {"paths": {"archive": str(tmp_path)}}
    assert "no readable TIMING.jsonl" in diagnostics_bundle.timing_section(data)
    _full_job(tmp_path, "job-a", base=1000.0)
    assert "Slice: median 12.0s" in diagnostics_bundle.timing_section(data)
    from print_handy import settings
    loaded = settings.load(local=False, environ={
        "PRINT_HANDY__PATHS__ARCHIVE": str(tmp_path),
        "PRINT_HANDY__PATHS__STATE_HOME": str(tmp_path / "state")})
    bundle = diagnostics_bundle.build_bundle(loaded, include_doctor=False)
    assert "## Timing" in bundle and "Tap to print started" in bundle


# -- release checklist --------------------------------------------------------

def _range_with(**extra):
    data = {"version": "9.9.9", "from_ref": "9.9.8", "to_ref": "abc123", "prs": []}
    data.update(extra)
    return data


def test_checklist_records_not_measured_without_a_summary():
    _title, body = checklist.build_checklist(_range_with())
    assert "## Real-install timing" in body
    assert "**not measured**" in body


def test_checklist_records_medians_and_movement():
    current = {"jobs_seen": 3, "jobs_used": 3,
               "spans": {"slice": {"label": "Slice", "median": 12.0, "p90": 60.0,
                                   "n": 3, "over_budget": []}},
               "start_response_lost": {"occurrences": 1, "jobs": 1}}
    previous = {"spans": {"slice": {"median": 5.0}}}
    _title, body = checklist.build_checklist(
        _range_with(timing_summary=current, previous_timing_summary=previous))
    assert "| Slice | 12.0s | 60.0s | 3 |" in body
    assert "Slice slower by 7.0s" in body
    over = {"jobs_seen": 1, "jobs_used": 1,
            "spans": {"slice": {"label": "Slice", "median": 500.0, "p90": 500.0,
                                "n": 1, "over_budget": ["median", "p90"]}},
            "start_response_lost": {"occurrences": 0, "jobs": 0}}
    _title, body = checklist.build_checklist(_range_with(timing_summary=over))
    assert "spans over budget: Slice (median), Slice (p90)" in body
