"""#1471: the mapped native start is fire-and-readback.

SET_ZCOLOR holds its Moonraker request open through the printer's own preparation, so
the start POST is sent with a short timeout and a read timeout is the held-open shape of
a healthy start: no `start_response_lost`, straight to the readback that decides truth.
Chat hears "accepted, preparing" from the `preparation_seen` TIMING stage seconds in,
while print-now keeps watching (a preparation the printer abandons still refuses).
Nothing here contacts a printer; a start is still sent exactly once, never resubmitted.
"""
import json
import urllib.error
from pathlib import Path
from unittest import mock

from print_handy.core import model_job
from print_handy.launch import print_now

from .test_launch_state import leases, make_job, mapped, uploads  # noqa: F401  (mapped is a fixture)

REAL_WAIT_PRINTING = model_job._wait_printing


def timing_stages(job_path):
    path = Path(job_path).parent / "TIMING.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)["stage"] for line in path.read_text().splitlines() if line.strip()]


def held_open_post(mapped, exc):
    """Replace the mapped fixture's transport: the SET_ZCOLOR POST raises `exc`."""
    original = model_job._mr.side_effect
    posts = []

    def mr(st, path, data=None, timeout=30):
        if path == "/printer/gcode/script":
            posts.append(timeout)
            raise exc
        return original(st, path, data=data, timeout=timeout)

    model_job._mr.side_effect = mr
    return posts


def test_held_open_start_is_not_a_lost_response(mapped):
    jp, g = make_job(mapped.root, "held")
    job = json.loads(jp.read_text())
    posts = held_open_post(mapped, TimeoutError("timed out"))
    # The heaters come on once the start is out (the pre-POST reads see them off).
    model_job._preparation_evidence.side_effect = lambda st: bool(posts)
    out = model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1}, job, jp, 0, False)
    assert out["phase"] == "preparing" and out["accepted"] is True
    assert out["start_response"] == "held_open"
    # One POST, sent with the short held-open timeout, never resent.
    assert posts == [print_now.BLOCKING_START_POST_TIMEOUT_S]
    assert len(uploads(mapped.run)) == 1
    stages = timing_stages(jp)
    assert "start_submitted" in stages and "start_response_lost" not in stages
    # Provisional, exactly as an answered preparing start: lease uncertain, claim held.
    assert any(row["phase"] == "uncertain" for row in leases())
    assert model_job._read_claims() != {}


def test_held_open_confirmed_printing_keeps_the_duplicate_guard(mapped):
    jp, g = make_job(mapped.root, "held-printing")
    job = json.loads(jp.read_text())
    posts = held_open_post(mapped, TimeoutError("timed out"))
    with mock.patch.object(model_job, "_wait_printing", lambda *a, **k: "printing"):
        out = model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1}, job, jp, 0, False)
    assert out["phase"] == "executing" and out["started"] is True and len(posts) == 1
    # The response never arrived, so the duplicate guard holds until the printer's
    # history settles it — the lost-response rule — even though the readback
    # already shows this file printing.
    assert [row["phase"] for row in leases()] == ["uncertain"]


def test_held_open_heating_without_a_proven_off_baseline_is_not_credited(mapped):
    """#836's rule survives the fast path: heater targets after an unanswered start
    count only when the baseline proved every heater off before the POST."""
    jp, g = make_job(mapped.root, "held-unproven")
    job = json.loads(jp.read_text())
    posts = held_open_post(mapped, TimeoutError("timed out"))
    # Unreadable before the POST (no baseline proven), on after it.
    model_job._preparation_evidence.side_effect = lambda st: True if posts else None
    out = model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1}, job, jp, 0, False)
    assert out["phase"] == "preparing_or_unresolved" and out["accepted"] is False
    assert [row["phase"] for row in leases()] == ["uncertain"] and len(posts) == 1


def test_a_reset_start_post_is_still_a_lost_response(mapped):
    jp, g = make_job(mapped.root, "reset")
    job = json.loads(jp.read_text())
    posts = held_open_post(mapped, ConnectionResetError("reset"))
    out = model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1}, job, jp, 0, False)
    assert out["phase"] == "preparing_or_unresolved" and out["accepted"] is False
    assert len(posts) == 1  # never resubmitted
    stages = timing_stages(jp)
    assert "start_response_lost" in stages and "start_submitted" not in stages


def test_read_timeout_classification():
    assert print_now._is_read_timeout(TimeoutError("timed out"))
    assert print_now._is_read_timeout(urllib.error.URLError(TimeoutError("timed out")))
    assert not print_now._is_read_timeout(urllib.error.URLError(ConnectionRefusedError("refused")))
    assert not print_now._is_read_timeout(ConnectionResetError("reset"))
    assert not print_now._is_read_timeout(urllib.error.HTTPError("http://m", 503, "x", {}, None))


def test_wait_printing_records_preparation_seen_once(tmp_path, monkeypatch):
    """The stage is written at the first sighting (seconds in, while the wait is still
    running), not when the wait ends, and only once however long preparation lasts."""
    jp = tmp_path / "JOB.json"
    jp.write_text("{}")
    clock = iter(range(0, 60))
    monkeypatch.setattr(model_job.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(model_job.time, "sleep", lambda s: None)
    monkeypatch.setattr(model_job, "_printer_phase", lambda st: ("standby", "", "Ready"))
    assert REAL_WAIT_PRINTING({}, "x.gcode", 30, prep_check=lambda st: True, job_path=jp) == "standby"
    assert timing_stages(jp) == ["preparation_seen"]


def test_wait_printing_without_preparation_records_no_stage(tmp_path, monkeypatch):
    jp = tmp_path / "JOB.json"
    jp.write_text("{}")
    clock = iter(range(0, 60))
    monkeypatch.setattr(model_job.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(model_job.time, "sleep", lambda s: None)
    monkeypatch.setattr(model_job, "_printer_phase", lambda st: ("standby", "", "Ready"))
    assert REAL_WAIT_PRINTING({}, "x.gcode", 30, prep_check=lambda st: False, job_path=jp) == "standby"
    assert timing_stages(jp) == []
