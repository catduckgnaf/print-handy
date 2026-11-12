"""#1262: VAAPI opens the render node that is actually usable, not a fixed renderD128."""
import os

from print_handy import live_stream as ls
from print_handy.runtime import vertical_short as vs


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


def test_unreadable_driver_link_never_hides_the_only_gpu(tmp_path):
    # A container with a limited /sys: no driver link for any node. Unknown is not NVIDIA.
    dri, sysfs = _dirs(tmp_path)
    (dri / "renderD129").write_text("")
    assert ls.vaapi_device(dri, sysfs, environ={}) == str(dri / "renderD129")


def test_a_known_driver_beats_an_unknown_one(tmp_path):
    dri, sysfs = _dirs(tmp_path)
    (dri / "renderD128").write_text("")  # driver unreadable
    _node(dri, sysfs, "renderD129", "i915")
    assert ls.vaapi_device(dri, sysfs, environ={}) == str(dri / "renderD129")


def test_only_nvidia_nodes_fall_back_to_the_default(tmp_path):
    dri, sysfs = _dirs(tmp_path)
    _node(dri, sysfs, "renderD128", "nvidia")
    assert ls.vaapi_device(dri, sysfs, environ={}) == ls.VAAPI_DEVICE
    assert ls.usable_render_node(dri, sysfs) is None


def test_single_gpu_renderD128_is_unchanged(tmp_path):
    dri, sysfs = _dirs(tmp_path)
    _node(dri, sysfs, "renderD128", "i915")
    assert ls.vaapi_device(dri, sysfs, environ={}) == str(dri / "renderD128")


def test_dri_status_agrees_with_vaapi_device(tmp_path):
    from print_handy.runtime import timelapse_encode as te

    dri, sysfs = _dirs(tmp_path)
    _node(dri, sysfs, "renderD128", "nvidia")
    ok, note = te.dri_status(dri, sys_drm=sysfs, environ={})
    assert ok is False and "NVIDIA" in note  # nothing VAAPI can open, so VAAPI is not a candidate
    _node(dri, sysfs, "renderD129", "i915")
    ok, note = te.dri_status(dri, sys_drm=sysfs, environ={})
    assert ok is True and "renderD129" in note
    assert ls.vaapi_device(dri, sysfs, environ={}) == str(dri / "renderD129")


def test_dri_status_judges_the_override(tmp_path):
    from print_handy.runtime import timelapse_encode as te

    dri, sysfs = _dirs(tmp_path)
    _node(dri, sysfs, "renderD128", "i915")
    env = {ls.VAAPI_DEVICE_ENV: str(tmp_path / "nope")}
    ok, note = te.dri_status(dri, sys_drm=sysfs, environ=env)
    assert ok is False and ls.VAAPI_DEVICE_ENV in note
    env = {ls.VAAPI_DEVICE_ENV: str(dri / "renderD128")}
    assert te.dri_status(dri, sys_drm=sysfs, environ=env)[0] is True
