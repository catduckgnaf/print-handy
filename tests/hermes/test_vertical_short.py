"""Offline tests for #317 vertical shorts: command construction, settings, delivery wiring.

No live rendering is claimed anywhere here: ffmpeg is never run; the command
builder is pinned as data, and the delivery wiring uses a fake builder.
"""
import copy
from pathlib import Path

import pytest

from print_handy import settings
from print_handy.hermes import moonraker_event_listener as mel
from print_handy.hermes import vertical_short as vs


# --- filter graphs / aspect handling -------------------------------------

@pytest.mark.parametrize("in_w,in_h", [(1920, 1080), (1080, 1920), (1000, 1000), (1280, 720)])
def test_pad_graph_fits_any_aspect(in_w, in_h):
    graph = vs.filter_graph(in_w, in_h, "pad")
    assert "split=2[bgin][fgin]" in graph
    assert f"scale={vs.OUT_W}:{vs.OUT_H}:force_original_aspect_ratio=increase" in graph  # blurred bg
    assert "boxblur" in graph
    assert f"scale={vs.OUT_W}:{vs.OUT_H}:force_original_aspect_ratio=decrease" in graph  # fg fits whole frame
    assert "overlay=(W-w)/2:(H-h)/2" in graph
    assert graph.endswith("[vout]")


@pytest.mark.parametrize("in_w,in_h", [(1920, 1080), (1080, 1920), (1000, 1000)])
def test_crop_graph_fits_any_aspect(in_w, in_h):
    graph = vs.filter_graph(in_w, in_h, "crop")
    assert f"scale={vs.OUT_W}:{vs.OUT_H}:force_original_aspect_ratio=increase" in graph
    assert f"crop={vs.OUT_W}:{vs.OUT_H}" in graph
    assert "boxblur" not in graph and "split" not in graph
    assert graph.endswith("[vout]")


def test_output_is_9_16():
    assert vs.OUT_W * 16 == vs.OUT_H * 9


# --- encoder seam (#314: the sizing shrink's hardware choice) -------------

def test_encoder_seam_defaults_to_cpu():
    assert vs.encoder_args() == ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
    assert vs.encoder_input_args() == []
    assert vs.encoder_filter("cpu", "scale=2") == "scale=2"


@pytest.mark.parametrize("encoder,ffmpeg_name",
                         [("vaapi", "h264_vaapi"), ("qsv", "h264_qsv"), ("nvenc", "h264_nvenc")])
def test_encoder_seam_hardware_choices(encoder, ffmpeg_name):
    assert ffmpeg_name in vs.encoder_args(encoder)


def test_encoder_seam_vaapi_device_and_upload():
    assert vs.encoder_input_args("vaapi") == ["-vaapi_device", "/dev/dri/renderD128"]
    assert vs.encoder_filter("vaapi", "scale=2") == "scale=2,format=nv12,hwupload"


def test_encoder_seam_unknown_choice_is_cpu():
    assert vs.encoder_args("bogus") == vs.encoder_args("cpu")


def test_final_frame_hold_default_and_optional():
    assert "tpad=stop_mode=clone:stop_duration=1" in vs.filter_graph(1920, 1080, "pad")
    assert "tpad" not in vs.filter_graph(1920, 1080, "pad", hold_s=0)
    assert "stop_duration=2.5" in vs.filter_graph(1920, 1080, "crop", hold_s=2.5)


def test_invalid_mode_and_dimensions_raise():
    with pytest.raises(ValueError):
        vs.filter_graph(1920, 1080, "stretch")
    with pytest.raises(ValueError):
        vs.filter_graph(0, 1080, "pad")
    with pytest.raises(ValueError):
        vs.filter_graph(1920, -1, "crop")


# --- overlay text ---------------------------------------------------------

def test_drawtext_escaping():
    assert vs.escape_drawtext("a:b") == "a\\:b"
    # P2-111: a quote cannot be escaped inside ffmpeg's single quotes at
    # either parsing layer; the apostrophe is written close-quote +
    # \\\' (escaped for both layers, unquoted) + reopen-quote. Verified
    # against real ffmpeg: the graph parses and renders the exact title.
    assert vs.escape_drawtext("it's") == "it'\\\\\\''s"
    assert vs.escape_drawtext("100%") == "100%%"
    assert vs.escape_drawtext("a\\b") == "a\\\\b"
    assert vs.escape_drawtext("line1\nline2") == "line1 line2"
    graph = vs.filter_graph(1920, 1080, "crop", lines=["Benchy: 100% 'done'"])
    assert "text='Benchy\\: 100%% '\\\\\\''done'\\\\\\'''" in graph


def test_overlay_lines_composition():
    meta = {"title": "Benchy", "printer": "AD5X", "material": "PLA",
            "elapsed_s": 7500, "outcome": "Done"}
    assert vs.overlay_lines(meta) == ["Benchy", "AD5X · PLA · 2h 05m · Done"]
    assert vs.overlay_lines({"title": "Solo"}) == ["Solo"]
    assert vs.overlay_lines({}) == []
    assert vs.overlay_lines({"printer": "AD5X"}) == ["AD5X"]


def test_format_elapsed():
    assert vs.format_elapsed(45) == "45s"
    assert vs.format_elapsed(600) == "10m"
    assert vs.format_elapsed(7500) == "2h 05m"
    assert vs.format_elapsed(None) is None
    assert vs.format_elapsed("junk") is None
    assert vs.format_elapsed(-3) is None


# --- command builder / runner --------------------------------------------

def test_build_command_shape():
    cmd = vs.build_command("in.mp4", "out.mp4", in_w=1920, in_h=1080, mode="pad",
                           lines=["T"], ffmpeg="ffmpeg")
    assert cmd[:6] == ["ffmpeg", "-y", "-v", "error", "-i", "in.mp4", "-filter_complex"][:6]
    assert cmd[cmd.index("-map") + 1] == "[vout]"
    assert "-an" in cmd and "libx264" in cmd and "+faststart" in cmd
    assert cmd[-1] == "out.mp4"


def test_build_short_uses_injected_probe_and_run(tmp_path):
    source = tmp_path / "tl.mp4"
    source.write_bytes(b"video")
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        Path(command[-1]).write_bytes(b"short")

    out = vs.build_short(source, meta={"title": "Benchy"}, probe=lambda path, ffprobe: (1920, 1080),
                         run=fake_run)
    assert out == tmp_path / "tl.vertical.mp4"
    assert out.read_bytes() == b"short"
    assert "drawtext" in seen["command"][7]


def test_build_short_failure_cleans_partial_and_raises(tmp_path):
    source = tmp_path / "tl.mp4"
    source.write_bytes(b"video")

    def failing_run(command, **kwargs):
        Path(command[-1]).write_bytes(b"junk")
        raise RuntimeError("ffmpeg died")

    with pytest.raises(RuntimeError):
        vs.build_short(source, probe=lambda path, ffprobe: (640, 480), run=failing_run)
    assert not (tmp_path / ".tl.vertical.mp4.partial.mp4").exists()
    assert not (tmp_path / "tl.vertical.mp4").exists()


# --- settings --------------------------------------------------------------

def test_settings_defaults_are_off():
    data = settings.load(local=False, environ={})
    timelapse = data["timelapse"]
    assert timelapse["vertical_short"] is False
    assert timelapse["vertical_short_mode"] == "pad"
    assert timelapse["vertical_short_overlay"] is True


def test_settings_env_override_and_validation():
    data = settings.load(local=False, environ={
        "PRINT_HANDY__TIMELAPSE__VERTICAL_SHORT": "true",
        "PRINT_HANDY__TIMELAPSE__VERTICAL_SHORT_MODE": "crop",
    })
    assert data["timelapse"]["vertical_short"] is True
    assert data["timelapse"]["vertical_short_mode"] == "crop"
    with pytest.raises(settings.SettingsError):
        settings.load(local=False, environ={
            "PRINT_HANDY__TIMELAPSE__VERTICAL_SHORT_MODE": "stretch"})


# --- delivery wiring --------------------------------------------------------

def _cfg_with_short(monkeypatch, enabled):
    cfg = copy.deepcopy(mel._config.cfg())
    cfg["timelapse"]["vertical_short"] = enabled
    cfg["timelapse"]["vertical_short_mode"] = "pad"
    cfg["timelapse"]["vertical_short_overlay"] = True
    monkeypatch.setattr(mel._config, "_override", cfg)
    return cfg


def _wire_post(monkeypatch, tmp_path):
    announcer = mel.announcer
    monkeypatch.setattr(announcer, "STATE_PATH", tmp_path / "announcement.json")
    announcer.save_state({"pending_completion_message": "Print complete: Stand"})
    monkeypatch.setattr(announcer, "archive_completed_timelapse",
                        lambda key: announcer.ArchiveOutcome(announcer.ARCHIVED, "/archive/stand.mp4"))
    video = tmp_path / "stand.mp4"
    video.write_bytes(b"v" * 100)
    monkeypatch.setattr(announcer, "materialize_archived_timelapse", lambda path: str(video))
    monkeypatch.setattr(mel, "platform_sized_video", lambda path, max_bytes=None: path)
    monkeypatch.setattr(mel, "log", lambda text: None)
    posted = []
    monkeypatch.setattr(mel, "post", lambda text: posted.append(text) or True)
    return posted


def test_completion_post_attaches_short_when_enabled(monkeypatch, tmp_path):
    _cfg_with_short(monkeypatch, True)
    posted = _wire_post(monkeypatch, tmp_path)
    short = tmp_path / "stand.vertical.mp4"
    short.write_bytes(b"s" * 50)
    calls = []
    monkeypatch.setattr(mel.vertical_short, "build_short",
                        lambda video, **kwargs: calls.append(kwargs) or short)
    mel._timelapse_post("stand", "Print complete: Stand")
    assert len(posted) == 1
    media = [line for line in posted[0].splitlines() if line.startswith("MEDIA:")]
    assert len(media) == 2
    assert media[1] == f"MEDIA:{short}"
    assert calls and calls[0]["mode"] == "pad" and calls[0]["overlay"] is True


def test_completion_post_skips_short_when_disabled(monkeypatch, tmp_path):
    _cfg_with_short(monkeypatch, False)
    posted = _wire_post(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(mel.vertical_short, "build_short",
                        lambda video, **kwargs: calls.append(kwargs))
    mel._timelapse_post("stand", "Print complete: Stand")
    assert calls == []  # not even built
    media = [line for line in posted[0].splitlines() if line.startswith("MEDIA:")]
    assert len(media) == 1


def test_short_builder_failure_never_breaks_the_announcement(monkeypatch, tmp_path):
    _cfg_with_short(monkeypatch, True)
    posted = _wire_post(monkeypatch, tmp_path)

    def boom(video, **kwargs):
        raise RuntimeError("no ffmpeg")

    monkeypatch.setattr(mel.vertical_short, "build_short", boom)
    mel._timelapse_post("stand", "Print complete: Stand")
    assert len(posted) == 1
    media = [line for line in posted[0].splitlines() if line.startswith("MEDIA:")]
    assert len(media) == 1  # the timelapse itself still posted


def test_oversize_short_is_dropped_not_the_timelapse(monkeypatch, tmp_path):
    _cfg_with_short(monkeypatch, True)
    posted = _wire_post(monkeypatch, tmp_path)
    big = tmp_path / "big.vertical.mp4"
    big.write_bytes(b"s" * 5000)
    monkeypatch.setattr(mel.vertical_short, "build_short", lambda video, **kwargs: big)
    monkeypatch.setattr(mel.delivery, "upload_limit", lambda config=None: 1000)

    def cannot_shrink(path, max_bytes=None):
        raise RuntimeError("cannot shrink")

    monkeypatch.setattr(mel, "platform_sized_video", cannot_shrink)
    mel._timelapse_post("stand", "Print complete: Stand")
    media = [line for line in posted[0].splitlines() if line.startswith("MEDIA:")]
    assert len(media) == 1
