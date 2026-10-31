"""Issue #246 stage 3: encoder selection, frame assembly and delivery.

All offline: encoder probes and ffmpeg runs are fakes except one
real-ffmpeg assembly smoke (skipped when ffmpeg is absent), mirroring
the issue's acceptance that hardware paths are pinned by
command-construction tests and labelled unverified on real hardware.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from print_handy import live_stream
from print_handy.hermes import _config
from print_handy.hermes import moonraker_event_listener as mel
from print_handy.hermes import plan_timelapse
from print_handy.hermes import timelapse_capture, timelapse_encode

ALL = {"h264_vaapi", "h264_nvenc", "h264_qsv", "libx264"}


def probe(encoders=ALL, dri=True, nvidia=True):
    return live_stream.EncoderProbe(encoders=frozenset(encoders),
                                    dri_present=dri, nvidia_present=nvidia)


def vendors(cands):
    return [c.vendor for c in cands]


# --------------------------------------------------------------- selection

def test_auto_order_is_vaapi_nvenc_qsv_cpu():
    assert vendors(timelapse_encode.candidates("auto", probe())) == ["vaapi", "nvenc", "qsv", "cpu"]


def test_explicit_preference_moves_first_and_cpu_stays_last():
    assert vendors(timelapse_encode.candidates("nvenc", probe())) == ["nvenc", "vaapi", "qsv", "cpu"]
    assert vendors(timelapse_encode.candidates("qsv", probe()))[0] == "qsv"
    assert vendors(timelapse_encode.candidates("cpu", probe())) == ["cpu", "vaapi", "nvenc", "qsv"]


def test_probe_without_devices_excludes_hardware():
    assert vendors(timelapse_encode.candidates("auto", probe(dri=False, nvidia=False))) == ["cpu"]
    assert vendors(timelapse_encode.candidates("auto", probe(dri=False))) == ["nvenc", "cpu"]


def test_probe_missing_libx264_still_offers_cpu_fallback():
    no_cpu = probe(encoders={"h264_vaapi"})
    assert vendors(timelapse_encode.candidates("auto", no_cpu)) == ["vaapi", "cpu"]


def test_unknown_preference_behaves_as_auto():
    assert vendors(timelapse_encode.candidates("bogus", probe())) == ["vaapi", "nvenc", "qsv", "cpu"]


def test_probe_ffmpeg_parses_encoder_listing():
    def fake_run(cmd, **kw):
        return type("R", (), {"stdout": " V..... h264_vaapi  foo\n V....D libx264  bar\n"})()
    found = timelapse_encode.probe_ffmpeg("ffmpeg", run=fake_run, dri_present=True, nvidia_present=False)
    assert found.encoders == frozenset({"h264_vaapi", "libx264"})
    assert found.dri_present is True and found.nvidia_present is False


def test_probe_ffmpeg_failure_yields_empty_set():
    def boom(cmd, **kw):
        raise OSError("no ffmpeg")
    found = timelapse_encode.probe_ffmpeg("ffmpeg", run=boom, dri_present=False, nvidia_present=False)
    assert found.encoders == frozenset()


# --------------------------------------------------------------- assembly

def make_frames(directory, count=30):
    frames = Path(directory) / "frames"
    frames.mkdir(parents=True)
    for i in range(1, count + 1):
        (frames / f"frame-{i:06d}.jpg").write_bytes(b"\xff\xd8frame")
    return frames


class FakeEncode:
    """ffmpeg stand-in: succeeds/fails per encoder name, writes sized output."""

    def __init__(self, fail=(), oversize=()):
        self.calls = []
        self.fail = set(fail)
        self.oversize = set(oversize)  # encoders whose output is always 10 MB

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        joined = " ".join(cmd)
        for name in ("h264_vaapi", "h264_nvenc", "h264_qsv", "libx264"):
            if name in joined:
                if name in self.fail:
                    raise subprocess.CalledProcessError(1, cmd)
                size = 10_000_000 if name in self.oversize else 500_000
                Path(cmd[-1]).write_bytes(b"x" * size)
                return None
        raise AssertionError(f"no encoder in argv: {joined}")


def test_command_construction_per_encoder(tmp_path):
    frames = make_frames(tmp_path)
    cases = {
        "vaapi": ("h264_vaapi", "-vaapi_device", "hwupload"),
        "nvenc": ("h264_nvenc",),
        "qsv": ("h264_qsv",),
        "cpu": ("libx264", "-preset", "veryfast"),
    }
    for vendor, needles in cases.items():
        fake = FakeEncode()
        target = tmp_path / f"{vendor}.mp4"
        out = timelapse_encode.assemble_frames(
            frames, target, max_bytes=9_500_000, preference=vendor,
            probe=probe(), run=fake)
        assert out == target
        argv = fake.calls[0]
        for needle in needles:
            assert needle in argv or needle in " ".join(argv), (vendor, argv)
        assert "scale='min(1280,iw)':-2" in " ".join(argv)
        assert "+faststart" in argv and "-an" in argv


def test_hardware_failure_falls_back_through_candidates_to_cpu(tmp_path):
    frames = make_frames(tmp_path)
    fake = FakeEncode(fail={"h264_vaapi", "h264_nvenc", "h264_qsv"})
    out = timelapse_encode.assemble_frames(
        frames, tmp_path / "tl.mp4", max_bytes=9_500_000, preference="auto",
        probe=probe(), run=fake)
    assert out is not None and out.stat().st_size == 500_000
    used = [" ".join(c) for c in fake.calls]
    assert sum("h264_vaapi" in c for c in used) == 1  # one failure, then next candidate
    assert "libx264" in used[-1]


def test_oversize_retries_at_seventy_percent_then_next_candidate(tmp_path):
    frames = make_frames(tmp_path)
    fake = FakeEncode(oversize={"h264_vaapi"})
    out = timelapse_encode.assemble_frames(
        frames, tmp_path / "tl.mp4", max_bytes=9_500_000, preference="auto",
        probe=probe(), run=fake)
    assert out is not None
    vaapi_calls = [c for c in fake.calls if "h264_vaapi" in " ".join(c)]
    assert len(vaapi_calls) == 2  # budget attempt + 70% attempt, both oversize
    kbps = [int(c[c.index("-b:v") + 1].rstrip("k")) for c in vaapi_calls]
    assert kbps[1] == int(kbps[0] * 0.7)


def test_total_failure_returns_none(tmp_path):
    frames = make_frames(tmp_path)
    fake = FakeEncode(fail={"h264_vaapi", "h264_nvenc", "h264_qsv", "libx264"})
    assert timelapse_encode.assemble_frames(
        frames, tmp_path / "tl.mp4", max_bytes=9_500_000, preference="auto",
        probe=probe(), run=fake) is None


def test_no_frames_returns_none(tmp_path):
    empty = tmp_path / "frames"
    empty.mkdir()
    assert timelapse_encode.assemble_frames(
        empty, tmp_path / "tl.mp4", max_bytes=9_500_000, probe=probe(), run=FakeEncode()) is None


# --------------------------------------------------------------- lookup

def write_capture(root, slug, *, job_key, filename, frames=3, state="complete",
                  finished_at=100.0, dark_frames=0):
    directory = Path(root) / slug
    (directory / "frames").mkdir(parents=True)
    for i in range(1, frames + 1):
        (directory / "frames" / f"frame-{i:06d}.jpg").write_bytes(b"\xff\xd8f")
    manifest = {"job_key": job_key, "filename": filename, "state": state,
                "frames": frames, "finished_at": finished_at,
                "dark_frames": dark_frames}
    (directory / "capture.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


def test_find_capture_by_identity_slug(tmp_path):
    wanted = write_capture(tmp_path, timelapse_capture.job_dir(tmp_path, "job:7").name,
                           job_key="job:7", filename="benchy.gcode")
    assert timelapse_capture.job_dir(tmp_path, "job:7") == wanted
    assert timelapse_encode.find_capture("job:7", root=tmp_path) == wanted


def test_find_capture_by_filename_picks_newest_finished(tmp_path):
    write_capture(tmp_path, "job-3", job_key="job:3", filename="benchy.gcode", finished_at=50.0)
    newest = write_capture(tmp_path, "job-9", job_key="job:9", filename="benchy.gcode", finished_at=90.0)
    write_capture(tmp_path, "job-11", job_key="job:11", filename="benchy.gcode",
                  state="capturing", finished_at=999.0)
    write_capture(tmp_path, "job-12", job_key="job:12", filename="other.gcode", finished_at=999.0)
    assert timelapse_encode.find_capture("prints/benchy.gcode", root=tmp_path) == newest


def test_find_capture_skips_empty_and_missing(tmp_path):
    write_capture(tmp_path, "job-1", job_key="job:1", filename="benchy.gcode", frames=0)
    assert timelapse_encode.find_capture("benchy.gcode", root=tmp_path) is None
    assert timelapse_encode.find_capture("benchy.gcode", root=tmp_path / "nope") is None


# --------------------------------------------------------------- dark outcome (#476)

def test_dark_capture_finds_the_all_dark_manifest(tmp_path):
    write_capture(tmp_path, "job-7", job_key="job:7", filename="benchy.gcode",
                  frames=0, dark_frames=5)
    found = timelapse_encode.dark_capture("job:7", root=tmp_path)
    assert found is not None and found["dark_frames"] == 5
    # …and the encoder lookup never hands it frames to encode.
    assert timelapse_encode.find_capture("job:7", root=tmp_path) is None


def test_dark_capture_matches_by_filename(tmp_path):
    write_capture(tmp_path, "job-9", job_key="job:9", filename="benchy.gcode",
                  frames=0, dark_frames=2, finished_at=90.0)
    found = timelapse_encode.dark_capture("prints/benchy.gcode", root=tmp_path)
    assert found is not None and found["job_key"] == "job:9"


def test_dark_capture_ignores_mixed_plain_empty_and_live(tmp_path):
    write_capture(tmp_path, "job-1", job_key="job:1", filename="a.gcode",
                  frames=2, dark_frames=3)
    write_capture(tmp_path, "job-2", job_key="job:2", filename="b.gcode",
                  frames=0, dark_frames=0)
    write_capture(tmp_path, "job-3", job_key="job:3", filename="c.gcode",
                  frames=0, dark_frames=4, state="capturing")
    assert timelapse_encode.dark_capture("job:1", root=tmp_path) is None
    assert timelapse_encode.dark_capture("job:2", root=tmp_path) is None
    assert timelapse_encode.dark_capture("job:3", root=tmp_path) is None
    assert timelapse_encode.dark_capture("job:1", root=tmp_path / "nope") is None


def test_captured_video_never_encodes_an_all_dark_capture(tmp_path):
    root = Path(tmp_path) / "timelapse"
    write_capture(root, "job-7", job_key="job:7", filename="benchy.gcode",
                  frames=0, dark_frames=6)
    fake = FakeEncode()
    out = timelapse_encode.captured_video(
        "job:7", data=capture_data(tmp_path), max_bytes=9_500_000,
        probe=probe(), run=fake)
    assert out is None
    assert fake.calls == []  # no encode of an all-dark frame set
    assert not (root / "job-7" / "timelapse.mp4").exists()


# --------------------------------------------------------------- captured_video

def capture_data(tmp_path, **timelapse):
    base = {"capture_enabled": True, "capture_encoder": "cpu"}
    base.update(timelapse)
    return {"paths": {"cache": str(tmp_path)}, "timelapse": base}


def test_captured_video_assembles_and_reuses(tmp_path):
    root = Path(tmp_path) / "timelapse"
    write_capture(root, "job-7", job_key="job:7", filename="benchy.gcode")
    fake = FakeEncode()
    data = capture_data(tmp_path)
    out = timelapse_encode.captured_video(
        "job:7", data=data, max_bytes=9_500_000, probe=probe(), run=fake)
    assert out == root / "job-7" / "timelapse.mp4" and out.stat().st_size == 500_000
    again = timelapse_encode.captured_video(
        "job:7", data=data, max_bytes=9_500_000, probe=probe(), run=fake)
    assert again == out and len(fake.calls) == 1  # reused, not re-encoded


def test_captured_video_none_when_disabled_or_absent(tmp_path):
    root = Path(tmp_path) / "timelapse"
    write_capture(root, "job-7", job_key="job:7", filename="benchy.gcode")
    off = capture_data(tmp_path, capture_enabled=False)
    assert timelapse_encode.captured_video(
        "job:7", data=off, max_bytes=9_500_000, probe=probe(), run=FakeEncode()) is None
    on = capture_data(tmp_path)
    assert timelapse_encode.captured_video(
        "job:99", data=on, max_bytes=9_500_000, probe=probe(), run=FakeEncode()) is None


# --------------------------------------------------------------- delivery order

@pytest.fixture
def delivery(monkeypatch, tmp_path):
    posted = []
    monkeypatch.setattr(mel, "_post_completion", lambda msg, orig: posted.append(msg) or True)
    monkeypatch.setattr(mel, "_vertical_short_attachment", lambda video, key: None)
    monkeypatch.setattr(mel, "log", lambda line: None)
    return posted


def test_captured_video_wins_over_printer_rendered(monkeypatch, tmp_path, delivery):
    captured = tmp_path / "timelapse.mp4"
    captured.write_bytes(b"video")
    monkeypatch.setattr(mel, "_captured_timelapse", lambda key: captured)

    def forbidden(key):
        raise AssertionError("printer-rendered path must not run when capture produced a video")
    monkeypatch.setattr(mel.announcer, "archive_completed_timelapse", forbidden)
    mel._timelapse_post("benchy.gcode", "Print complete")
    assert delivery == [f"Print complete\nMEDIA:{captured}"]


def test_dark_capture_names_the_outcome_in_the_completion_post(monkeypatch, tmp_path, delivery):
    monkeypatch.setattr(mel, "_captured_timelapse", lambda key: None)
    monkeypatch.setattr(mel.announcer, "archive_completed_timelapse",
                        lambda key: mel.announcer.ArchiveOutcome(mel.announcer.ARCHIVE_NOT_RENDERED))
    monkeypatch.setattr(mel, "_dark_capture", lambda key: {"dark_frames": 3})
    mel._timelapse_post("benchy.gcode", "Print complete")
    assert delivery == [
        "Print complete\nTimelapse not attached: the camera image was completely "
        "dark — all 3 captured frame(s) were skipped as dark, so there was "
        "nothing to encode. Check the camera or its light."]


def test_delivery_falls_back_to_printer_rendered(monkeypatch, tmp_path, delivery):
    monkeypatch.setattr(mel, "_captured_timelapse", lambda key: None)
    rendered = tmp_path / "rendered.mp4"
    rendered.write_bytes(b"video")
    monkeypatch.setattr(mel.announcer, "archive_completed_timelapse",
                        lambda key: mel.announcer.ArchiveOutcome(mel.announcer.ARCHIVED, "/archive/rendered.mp4"))
    monkeypatch.setattr(mel.announcer, "materialize_archived_timelapse", lambda path: rendered)
    monkeypatch.setattr(mel, "platform_sized_video", lambda path, max_bytes=None: path)
    mel._timelapse_post("benchy.gcode", "Print complete")
    assert delivery == [f"Print complete\nMEDIA:{rendered}"]


def _mode_config(monkeypatch, mode):
    cfg = {"timelapse": {"mode": mode, "capture_enabled": False},
           "services": {"moonraker_url": "http://printer.test:7125"}}
    monkeypatch.setattr(mel, "_config", type("Cfg", (), {"cfg": staticmethod(lambda: cfg)})())


def test_mode_off_posts_the_completion_alone_and_never_asks_the_archive(
        monkeypatch, tmp_path, delivery):
    # 2.2.2 wiring: mode was a dead option — "off" stopped nothing. Now
    # the rendered lookup does not even run, and the bare completion
    # post is the configured behavior.
    _mode_config(monkeypatch, "off")
    monkeypatch.setattr(mel, "_captured_timelapse", lambda key: None)
    monkeypatch.setattr(mel, "_dark_capture", lambda key: None)

    def forbidden(key):
        raise AssertionError("the rendered path must not run in mode off")
    monkeypatch.setattr(mel.announcer, "archive_completed_timelapse", forbidden)
    mel._timelapse_post("benchy.gcode", "Print complete")
    assert delivery == ["Print complete"]


def test_mode_container_names_the_missing_capture_and_skips_the_archive(
        monkeypatch, tmp_path, delivery):
    _mode_config(monkeypatch, "container")
    monkeypatch.setattr(mel, "_captured_timelapse", lambda key: None)
    monkeypatch.setattr(mel, "_dark_capture", lambda key: None)

    def forbidden(key):
        raise AssertionError("the rendered path must not run in container mode")
    monkeypatch.setattr(mel.announcer, "archive_completed_timelapse", forbidden)
    mel._timelapse_post("benchy.gcode", "Print complete")
    assert delivery == ["Print complete\nTimelapse not attached: "
                        "in-container capture produced no video for this job."]


# --------------------------------------------------------------- plan combine seam

def test_combine_reencode_retries_cpu_when_hardware_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(plan_timelapse, "_root", lambda: tmp_path / "plans")
    monkeypatch.setattr(_config, "_override",
                        {"timelapse": {"encoder": "vaapi", "plan_clip_retention_days": 1},
                         "paths": {"ffmpeg": "ffmpeg"}, "discord": {}})
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        joined = " ".join(cmd)
        if "copy" in cmd:
            raise subprocess.CalledProcessError(1, cmd)
        if "h264_vaapi" in joined:
            raise subprocess.CalledProcessError(1, cmd)
        Path(cmd[-1]).write_bytes(b"combined")
        return None
    monkeypatch.setattr(plan_timelapse.subprocess, "run", fake_run)
    first, second = tmp_path / "a.mp4", tmp_path / "b.mp4"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    plan_timelapse.add_clip("trio", 0, first)
    plan_timelapse.add_clip("trio", 1000, second)
    combined = plan_timelapse.combine("trio")
    assert combined.read_bytes() == b"combined"
    assert "h264_vaapi" in " ".join(calls[1]) and "-vaapi_device" in calls[1]
    assert "libx264" in " ".join(calls[2])


# --------------------------------------------------------------- real ffmpeg smoke

@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                    reason="ffmpeg/ffprobe not available")
def test_real_ffmpeg_assembles_frames_under_cap(tmp_path):
    frames = tmp_path / "frames"
    frames.mkdir()
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=duration=1:size=640x360:rate=10",
                    str(frames / "frame-%06d.jpg")], check=True, capture_output=True)
    target = tmp_path / "timelapse.mp4"
    out = timelapse_encode.assemble_frames(
        frames, target, max_bytes=9_500_000, preference="cpu",
        probe=live_stream.EncoderProbe(encoders=frozenset({"libx264"})))
    assert out == target and 0 < target.stat().st_size <= 9_500_000
    info = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                           "-show_entries", "stream=codec_name,width", "-of", "csv=p=0",
                           str(target)], capture_output=True, text=True, check=True).stdout
    assert "h264" in info and int(info.strip().split(",")[-1]) <= 1280


# --------------------------------------------------------------- #706 render collision

REAL = Path(__file__).parent / "fixtures" / "timelapse_real"


def _real_tools():
    return shutil.which("ffmpeg") and shutil.which("ffprobe")


def test_unique_partial_paths_never_collide(tmp_path):
    target = tmp_path / "timelapse.mp4"
    a, b = timelapse_encode.unique_partial(target), timelapse_encode.unique_partial(target)
    assert a != b and a.parent == b.parent == tmp_path


def test_validate_video_rejects_streamless_and_garbage_files(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 4096)
    assert timelapse_encode.validate_video(bad) is False
    assert timelapse_encode.validate_video(tmp_path / "missing.mp4") is False


@pytest.mark.skipif(not _real_tools(), reason="ffmpeg/ffprobe not available")
def test_real_captured_frames_encode_fully_decodes(tmp_path):
    """Frames from the real Print #64 capture (AD5X camera JPEGs, yuvj422p 1280x720)."""
    frames = tmp_path / "frames"
    frames.mkdir()
    for src in sorted(REAL.glob("frame-*.jpg")):
        shutil.copy(src, frames / src.name)
    count = len(list(frames.glob("frame-*.jpg")))
    assert count >= 10
    target = tmp_path / "timelapse.mp4"
    out = timelapse_encode.assemble_frames(
        frames, target, max_bytes=9_500_000, preference="cpu",
        probe=live_stream.EncoderProbe(encoders=frozenset({"libx264"})))
    assert out == target
    assert timelapse_encode.validate_video(target, min_frames=count)
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
         "stream=codec_name,width,height,nb_read_frames", "-of", "json", str(target)],
        capture_output=True, text=True, check=True).stdout)["streams"][0]
    assert info["codec_name"] == "h264" and int(info["nb_read_frames"]) == count


@pytest.mark.skipif(not _real_tools(), reason="ffmpeg/ffprobe not available")
def test_concurrent_renders_of_one_job_leave_a_decodable_video(tmp_path):
    """The Print #64 collision: two renders of one capture at once."""
    import threading
    root = Path(tmp_path) / "timelapse"
    directory = write_capture(root, "job-9", job_key="job:9", filename="can.gcode", frames=1)
    for src in sorted(REAL.glob("frame-*.jpg")):
        shutil.copy(src, directory / "frames" / src.name)
    count = len(list((directory / "frames").glob("frame-*.jpg")))
    data = capture_data(tmp_path)
    results = []

    def render():
        results.append(timelapse_encode.captured_video("job:9", data=data, max_bytes=9_500_000))

    threads = [threading.Thread(target=render) for _ in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    target = directory / "timelapse.mp4"
    assert results == [target] * 3
    assert timelapse_encode.validate_video(target, min_frames=count)
    assert not list(directory.glob(".timelapse.mp4.*partial.mp4"))


@pytest.mark.skipif(not _real_tools(), reason="ffmpeg/ffprobe not available")
def test_corrupt_cached_video_is_rerendered_not_attached(tmp_path):
    root = Path(tmp_path) / "timelapse"
    directory = write_capture(root, "job-8", job_key="job:8", filename="can.gcode", frames=1)
    for src in sorted(REAL.glob("frame-*.jpg")):
        shutil.copy(src, directory / "frames" / src.name)
    count = len(list((directory / "frames").glob("frame-*.jpg")))
    target = directory / "timelapse.mp4"
    target.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 8_000)  # streamless, like the live file
    out = timelapse_encode.captured_video("job:8", data=capture_data(tmp_path), max_bytes=9_500_000)
    assert out == target and timelapse_encode.validate_video(target, min_frames=count)


def test_undecodable_encoder_output_is_discarded(tmp_path):
    frames = make_frames(tmp_path)
    fake = FakeEncode()
    out = timelapse_encode.assemble_frames(
        frames, tmp_path / "tl.mp4", max_bytes=9_500_000, preference="cpu",
        probe=probe(), run=fake, validate=lambda path: False)
    assert out is None and not (tmp_path / "tl.mp4").exists()
    assert not list(tmp_path.glob(".tl.mp4.*partial.mp4"))
