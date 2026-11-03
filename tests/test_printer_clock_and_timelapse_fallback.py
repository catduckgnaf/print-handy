"""Printer clock skew warning, skew-tolerant history, quality-fallback encode, local archive copy."""
from __future__ import annotations

import time
from email.utils import formatdate
from pathlib import Path

from print_handy.core import launch_state
from print_handy.hermes import timelapse_encode as enc


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
