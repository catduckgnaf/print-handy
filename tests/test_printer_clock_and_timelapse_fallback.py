"""Printer clock skew warning, skew-tolerant history, quality-fallback encode, local archive copy."""
from __future__ import annotations

import time
from email.utils import formatdate
from pathlib import Path

from print_handy.core import launch_state
from print_handy.runtime import timelapse_encode as enc


class _Resp:
    def __init__(self, date):
        self.headers = {"Date": date} if date else {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_urlopen(monkeypatch, date):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp(date))


def test_skew_reads_the_http_date_header(monkeypatch):
    _patch_urlopen(monkeypatch, formatdate(time.time() - 86400 * 307, usegmt=True))
    skew = launch_state.printer_clock_skew("http://printer:7125")
    assert skew is not None
    assert 86400 * 306 < skew < 86400 * 308  # positive: the printer is behind


def test_skew_is_unknown_when_unreadable(monkeypatch):
    _patch_urlopen(monkeypatch, None)
    assert launch_state.printer_clock_skew("http://printer:7125") is None

    import urllib.request

    def boom(*a, **k):
        raise OSError("down")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert launch_state.printer_clock_skew("http://printer:7125") is None


def test_warning_only_when_off_and_says_what_breaks():
    assert launch_state.clock_warning(None) is None
    assert launch_state.clock_warning(30) is None
    assert launch_state.clock_warning(-299) is None
    behind = launch_state.clock_warning(86400 * 307)
    assert behind is not None
    assert "307 day(s) behind" in behind and "uncertain" in behind and "NTP" in behind
    hour = launch_state.clock_warning(-3600)
    assert hour is not None and "ahead of" in hour and "60 minute(s)" in hour


def _frames(tmp_path, n=3):
    d = tmp_path / "frames"
    d.mkdir()
    for i in range(1, n + 1):
        (d / f"frame-{i:06d}.jpg").write_bytes(b"x")
    return d


def test_quality_fallback_steps_down_until_it_fits(tmp_path):
    frames_dir = _frames(tmp_path)
    target, partial = tmp_path / "out.mp4", tmp_path / "out.part"
    sizes = iter([900, 700, 400])  # crf 23 and 28 too big, crf 32 fits
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        partial.write_bytes(b"v" * next(sizes))

    got = enc._assemble_quality_fallback(frames_dir, target, partial, sorted(frames_dir.glob("*.jpg")),
                                         500, 10, "ffmpeg", run, lambda m: None, lambda p: True)
    assert got == target and target.stat().st_size == 400
    assert [c[c.index("-crf") + 1] for c in seen] == ["23", "28", "32"]
    assert not partial.exists()


def test_quality_fallback_discards_undecodable_and_gives_up(tmp_path):
    frames_dir = _frames(tmp_path)
    target, partial = tmp_path / "out.mp4", tmp_path / "out.part"

    def run(cmd, **kw):
        partial.write_bytes(b"v" * 10)

    got = enc._assemble_quality_fallback(frames_dir, target, partial, [], 500, 10, "ffmpeg", run,
                                         lambda m: None, lambda p: False)
    assert got is None and not target.exists() and not partial.exists()


def test_quality_fallback_stops_when_x264_itself_fails(tmp_path):
    frames_dir = _frames(tmp_path)
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        raise RuntimeError("no libx264")

    got = enc._assemble_quality_fallback(frames_dir, tmp_path / "o.mp4", tmp_path / "o.part", [], 500, 10,
                                         "ffmpeg", run, lambda m: None, lambda p: True)
    assert got is None and len(calls) == 1


def test_archive_copy_keeps_the_video_and_never_clobbers(tmp_path):
    video = tmp_path / "timelapse.mp4"
    video.write_bytes(b"abc")
    root = tmp_path / "archive"
    root.mkdir()
    first = enc.archive_local_copy(video, "Pixel Patina V17", directory=root)
    assert first is not None
    assert first.read_bytes() == b"abc" and first.name == "Pixel-Patina-V17.mp4"
    second = enc.archive_local_copy(video, "Pixel Patina V17", directory=root)
    assert second is not None
    assert second != first and first.exists() and second.exists()


def test_archive_copy_is_a_noop_without_the_mount(tmp_path):
    video = tmp_path / "t.mp4"
    video.write_bytes(b"abc")
    assert enc.archive_local_copy(video, "x", directory=tmp_path / "missing") is None


def test_clock_skew_shift_lets_a_stale_history_settle_the_lease():
    lease = {"remote": "V17.gcode", "leased_at": "2026-11-02T16:13:04-0500"}
    printer_clock_start = 1767208064  # the printer's wrong 2025 clock
    from datetime import datetime
    leased = datetime.strptime(lease["leased_at"], "%Y-%m-%dT%H:%M:%S%z").timestamp()  # host clock
    skew = leased - printer_clock_start + 60  # printer started 60 s after the lease, host time
    assert not launch_state.reached_printer(lease, started_files=[("1", "V17.gcode", printer_clock_start)])
    assert launch_state.reached_printer(lease, started_files=[("1", "V17.gcode", printer_clock_start + skew)])


def _history_env(monkeypatch, jobs, skew):
    from print_handy.core import model_job
    monkeypatch.setattr(model_job, "_mr", lambda st, path, **kw: {"result": {"jobs": jobs}})
    monkeypatch.setattr(launch_state, "printer_clock_skew", lambda url, **kw: skew)
    return model_job


def test_started_files_shift_only_runs_stamped_by_the_wrong_clock(monkeypatch):
    skew = 86400 * 307
    now = time.time()
    jobs = [
        # Stamped yesterday by the then-correct clock (before the reset): shifting it by the
        # current skew would move it ~307 days into the future, so it keeps its raw stamp.
        {"job_id": "old", "filename": "V17.gcode", "start_time": now - 86400},
        # Stamped 60 s ago by the wrong clock: the shift lands it on host time.
        {"job_id": "new", "filename": "V17.gcode", "start_time": now - skew - 60},
    ]
    model_job = _history_env(monkeypatch, jobs, skew)
    got = {job_id: start for job_id, _name, start in model_job._moonraker_started_files({})}
    assert got["old"] == now - 86400
    assert abs(got["new"] - (now - 60)) < 2


def test_pre_reset_run_cannot_confirm_a_lease_with_an_empty_baseline(monkeypatch):
    # The Codex P2 on PR #804: the baseline read may fail (empty baseline), so a pre-reset
    # run of the same file must not confirm a new lease just because the skew shift moved it.
    from datetime import datetime
    skew = 86400 * 307
    now = time.time()
    leased_at = datetime.fromtimestamp(now).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    lease = {"remote": "V17.gcode", "leased_at": leased_at}
    old_only = [{"job_id": "old", "filename": "V17.gcode", "start_time": now - 86400}]
    model_job = _history_env(monkeypatch, old_only, skew)
    assert not launch_state.reached_printer(
        lease, started_files=model_job._moonraker_started_files({}), baseline=())
    with_new = old_only + [{"job_id": "new", "filename": "V17.gcode", "start_time": now - skew - 60}]
    model_job = _history_env(monkeypatch, with_new, skew)
    assert launch_state.reached_printer(
        lease, started_files=model_job._moonraker_started_files({}), baseline=())


# --- automatic clock sync -------------------------------------------------------------------------

import io
import json
import urllib.request


def _sync_env(monkeypatch, *, skew, state="standby", filename="", active=False, after=0.0):
    skews = iter([skew, after])
    monkeypatch.setattr(launch_state, "printer_clock_skew", lambda *a, **k: next(skews))
    monkeypatch.setattr(launch_state.time, "sleep", lambda *_: None)
    calls = []

    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(req, timeout=0):
        url = req if isinstance(req, str) else req.full_url
        calls.append(url)
        body = {"result": {"status": {"print_stats": {"state": state, "filename": filename},
                                      "virtual_sdcard": {"is_active": active}}}}
        return R(json.dumps(body).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    return calls


def test_sync_sets_clock_when_idle_and_far_off(monkeypatch):
    calls = _sync_env(monkeypatch, skew=86400 * 307)
    assert launch_state.sync_printer_clock("http://p:7125", "America/New_York", now=1793667044) == "ok"
    sent = [c for c in calls if "gcode/script" in c]
    assert len(sent) == 1 and "DATE_SET" in sent[0] and "2026.11.02-19%3A50%3A44" in sent[0]


def test_sync_never_touches_a_busy_printer(monkeypatch):
    for kw in ({"state": "printing"}, {"filename": "x.gcode"}, {"active": True}):
        calls = _sync_env(monkeypatch, skew=86400 * 307, **kw)
        assert launch_state.sync_printer_clock("http://p:7125", "America/New_York") == "skipped:printer not idle"
        assert not [c for c in calls if "gcode/script" in c]


def test_sync_leaves_a_correct_clock_alone(monkeypatch):
    calls = _sync_env(monkeypatch, skew=3)
    assert launch_state.sync_printer_clock("http://p:7125", "America/New_York") == "ok"
    assert calls == []


def test_sync_reports_failure_when_clock_stays_off(monkeypatch):
    _sync_env(monkeypatch, skew=86400 * 307, after=86400 * 307)
    assert launch_state.sync_printer_clock("http://p:7125", "America/New_York").startswith("failed")


def test_sync_can_be_disabled_and_unreadable_is_skipped(monkeypatch):
    calls = _sync_env(monkeypatch, skew=86400 * 307)
    monkeypatch.setenv("PRINT_HANDY_CLOCK_SYNC", "0")
    assert launch_state.sync_printer_clock("http://p:7125", "America/New_York") == "off"
    monkeypatch.delenv("PRINT_HANDY_CLOCK_SYNC")
    monkeypatch.setattr(launch_state, "printer_clock_skew", lambda *a, **k: None)
    assert launch_state.sync_printer_clock("http://p:7125", "America/New_York").startswith("skipped")
    assert calls == []


def _zone_env(monkeypatch, skews):
    monkeypatch.setattr(launch_state, "_CLOCK_ZONE_GAP_S", {})
    calls = _sync_env(monkeypatch, skew=0)
    readings = iter(skews)
    monkeypatch.setattr(launch_state, "printer_clock_skew", lambda *a, **k: next(readings))
    return calls


def test_sync_corrects_for_a_printer_in_another_zone_and_remembers_it(monkeypatch):
    # 2026-11-03: ZMod's default zone is Asia/Yekaterinburg (UTC+5). DATE_SET reads the stamp
    # in that zone, so a New York stamp lands the clock 10 h behind; the readback shows a whole
    # zone gap, and one corrected set fixes it.
    ten_hours = 10 * 3600
    calls = _zone_env(monkeypatch, [86400 * 307, ten_hours + 3, 1.0])
    assert launch_state.sync_printer_clock("http://p:7125", "America/New_York", now=1793667044) == "ok"
    sent = [c for c in calls if "gcode/script" in c]
    assert len(sent) == 2
    assert "2026.11.02-19%3A50%3A44" in sent[0] and "2026.11.03-05%3A50%3A44" in sent[1]
    # The next sync (after the next power cycle) lands right with one set.
    calls[:] = []
    readings = iter([86400 * 307, 1.0])
    monkeypatch.setattr(launch_state, "printer_clock_skew", lambda *a, **k: next(readings))
    assert launch_state.sync_printer_clock("http://p:7125", "America/New_York", now=1793667044) == "ok"
    sent = [c for c in calls if "gcode/script" in c]
    assert len(sent) == 1 and "2026.11.03-05%3A50%3A44" in sent[0]


def test_a_residual_that_is_not_a_zone_gap_is_a_failure_not_a_second_guess(monkeypatch):
    calls = _zone_env(monkeypatch, [86400 * 307, 4000.0])
    assert launch_state.sync_printer_clock("http://p:7125", "America/New_York").startswith("failed")
    assert len([c for c in calls if "gcode/script" in c]) == 1
    assert launch_state._CLOCK_ZONE_GAP_S == {}


def test_listener_retries_a_busy_printer_and_backs_off_after_a_failure(monkeypatch):
    from print_handy.runtime import moonraker_event_listener as mel
    listener = mel.Listener()
    outcomes = iter(["skipped:printer not idle", "failed:HTTPError", "ok"])
    tried = []
    monkeypatch.setattr(mel.launch_state, "sync_printer_clock",
                        lambda *a, **k: tried.append(1) or next(outcomes))
    monkeypatch.setattr(mel, "log", lambda line: None)
    clock = [1000.0]
    monkeypatch.setattr(mel.time, "monotonic", lambda: clock[0])
    listener._sync_printer_clock("poll")   # busy: no backoff
    listener._sync_printer_clock("poll")   # failed: backs off
    listener._sync_printer_clock("poll")   # inside the backoff: not tried
    assert len(tried) == 2
    clock[0] += mel.CLOCK_SYNC_BACKOFF_S + 1
    listener._sync_printer_clock("poll")
    assert len(tried) == 3


def test_zone_gaps_beyond_14_hours_are_corrected(monkeypatch):
    # Codex on #860: Pacific/Honolulu (UTC-10) against ZMod's default UTC+5 is 15 h apart.
    calls = _zone_env(monkeypatch, [86400 * 307, 15 * 3600 + 2, 0.5])
    assert launch_state.sync_printer_clock("http://p:7125", "Pacific/Honolulu", now=1793667044) == "ok"
    assert len([c for c in calls if "gcode/script" in c]) == 2


def test_the_zone_correction_shifts_wall_time_across_a_dst_change(monkeypatch):
    # Codex on #860: at 2026-03-08T01:00Z New York is 20:00 EST; a UTC+5 printer needs 06:00.
    # Shifting the instant by 10 h lands after the DST change and sends 07:00 instead.
    calls = _zone_env(monkeypatch, [86400 * 307, 10 * 3600, 0.5])
    now = 1772931600.0  # 2026-03-08T01:00:00Z
    assert launch_state.sync_printer_clock("http://p:7125", "America/New_York", now=now) == "ok"
    sent = [c for c in calls if "gcode/script" in c]
    assert "2026.03.07-20%3A00%3A00" in sent[0] and "2026.03.08-06%3A00%3A00" in sent[1]
