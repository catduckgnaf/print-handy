"""#1262: VAAPI opens the render node that is actually usable, not a fixed renderD128."""
import os

from print_handy import live_stream as ls
from print_handy.hermes import vertical_short as vs


def _node(dri, sysfs, name, driver):
    (dri / name).write_text("")
    drv = sysfs / "drivers" / driver
    drv.mkdir(parents=True, exist_ok=True)
    dev = sysfs / name / "device"
    dev.mkdir(parents=True)
    os.symlink(drv, dev / "driver")


def _dirs(tmp_path):
    dri, sysfs = tmp_path / "dri", tmp_path / "sys"
    dri.mkdir()
    sysfs.mkdir()
    return dri, sysfs


def test_skips_nvidia_and_picks_the_intel_node(tmp_path):
    dri, sysfs = _dirs(tmp_path)
    _node(dri, sysfs, "renderD128", "nvidia")
    _node(dri, sysfs, "renderD129", "i915")
    assert ls.vaapi_device(dri, sysfs, environ={}) == str(dri / "renderD129")


def test_only_mapped_node_is_used_when_renderD128_is_absent(tmp_path):
    dri, sysfs = _dirs(tmp_path)
    _node(dri, sysfs, "renderD129", "i915")
    assert ls.vaapi_device(dri, sysfs, environ={}) == str(dri / "renderD129")


def test_unusable_node_is_skipped(tmp_path):
    dri, sysfs = _dirs(tmp_path)
    _node(dri, sysfs, "renderD128", "i915")
    _node(dri, sysfs, "renderD129", "amdgpu")
    usable = lambda path, mode: path.endswith("renderD129")
    assert ls.vaapi_device(dri, sysfs, environ={}, access=usable) == str(dri / "renderD129")


def test_override_wins(tmp_path):
    dri, sysfs = _dirs(tmp_path)
    _node(dri, sysfs, "renderD128", "i915")
    env = {ls.VAAPI_DEVICE_ENV: "/dev/dri/renderD130"}
    assert ls.vaapi_device(dri, sysfs, environ=env) == "/dev/dri/renderD130"


def test_falls_back_to_the_default_without_a_node(tmp_path):
    dri, sysfs = _dirs(tmp_path)
    assert ls.vaapi_device(dri, sysfs, environ={}) == ls.VAAPI_DEVICE
    assert ls.vaapi_device(tmp_path / "missing", sysfs, environ={}) == ls.VAAPI_DEVICE


def test_encoder_input_args_and_live_argv_use_the_discovered_node(monkeypatch):
    monkeypatch.setenv(ls.VAAPI_DEVICE_ENV, "/dev/dri/renderD129")
    assert vs.encoder_input_args("vaapi") == ["-vaapi_device", "/dev/dri/renderD129"]
    assert vs.encoder_input_args("nvenc") == []
    cand = ls.EncoderCandidate(encoder="h264_vaapi", vendor="vaapi", codec="h264", hardware=True)
    argv = ls.build_ffmpeg_args(cand, input_url="rtsp://camera", target_codec="h264", output="-")
    assert argv[argv.index("-vaapi_device") + 1] == "/dev/dri/renderD129"
