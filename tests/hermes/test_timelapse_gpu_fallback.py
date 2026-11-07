"""Issue #455 phase 1: GPU encoder selection and the named CPU fallback.

Offline only: device directories are tmp paths, ffmpeg runs are fakes.
What these pin: a GPU is used only when its device is really usable, a
missing or failing GPU always ends in a CPU-encoded timelapse, and every
fallback leaves a log line that names the reason (no silent fallback,
no failed timelapse). Real GPU encodes are not exercised here.
"""
import os
import subprocess
from pathlib import Path

from print_handy import live_stream
from print_handy.hermes import timelapse_encode

ALL = {"h264_vaapi", "h264_nvenc", "h264_qsv", "libx264"}


def probe(encoders=ALL, dri=True, nvidia=True):
    return live_stream.EncoderProbe(encoders=frozenset(encoders),
                                    dri_present=dri, nvidia_present=nvidia)


def make_frames(directory, count=30):
    frames = Path(directory) / "frames"
    frames.mkdir(parents=True)
    for i in range(1, count + 1):
        (frames / f"frame-{i:06d}.jpg").write_bytes(b"\xff\xd8frame")
    return frames


class FakeEncode:
    """ffmpeg stand-in: per-encoder failure with a real-looking stderr, sized output."""

    STDERR = {
        "h264_nvenc": b"[h264_nvenc @ 0x1] Cannot load libnvidia-encode.so.1\n"
                      b"Error while opening encoder\n",
        "h264_vaapi": b"[AVHWDeviceContext @ 0x1] Failed to initialise VAAPI connection: -1\n",
        "h264_qsv": b"Error creating a MFX session: -9.\n",
    }

    def __init__(self, fail=(), oversize=()):
        self.calls = []
        self.fail = set(fail)
        self.oversize = set(oversize)

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        joined = " ".join(cmd)
        for name in ("h264_vaapi", "h264_nvenc", "h264_qsv", "libx264"):
            if name in joined:
                if name in self.fail:
                    raise subprocess.CalledProcessError(1, cmd, stderr=self.STDERR.get(name, b""))
                size = 10_000_000 if name in self.oversize else 500_000
                Path(cmd[-1]).write_bytes(b"x" * size)
                return None
        raise AssertionError(f"no encoder in argv: {joined}")


def assemble(tmp_path, fake, *, preference="auto", found=None):
    lines = []
    frames = make_frames(tmp_path)
    target = tmp_path / "out.mp4"
    out = timelapse_encode.assemble_frames(frames, target, max_bytes=9_500_000,
                                           preference=preference, probe=found or probe(),
                                           run=fake, log=lines.append)
    return out, target, lines


def encoders_tried(fake):
    names = []
    for cmd in fake.calls:
        for name in ("h264_vaapi", "h264_nvenc", "h264_qsv", "libx264"):
            if name in " ".join(cmd):
                names.append(name)
    return names


# ------------------------------------------------------------ device status

def test_dri_missing_is_named(tmp_path):
    usable, note = timelapse_encode.dri_status(tmp_path / "dri")
    assert usable is False
    assert "not passed through" in note


def test_dri_without_render_node_is_named(tmp_path):
    (tmp_path / "dri").mkdir()
    (tmp_path / "dri" / "card0").write_text("")
    usable, note = timelapse_encode.dri_status(tmp_path / "dri")
    assert usable is False
    assert "no renderD* render node" in note


def test_dri_render_node_without_access_names_the_group_fix(tmp_path):
    (tmp_path / "dri").mkdir()
    (tmp_path / "dri" / "renderD128").write_text("")
    usable, note = timelapse_encode.dri_status(tmp_path / "dri", access=lambda path, mode: False)
    assert usable is False
    assert "renderD128" in note and "group_add" in note and "--group-add" in note


def test_dri_accessible_render_node_is_usable(tmp_path):
    (tmp_path / "dri").mkdir()
    (tmp_path / "dri" / "renderD128").write_text("")
    seen = []

    def access(path, mode):
        seen.append(mode)
        return True
    usable, note = timelapse_encode.dri_status(tmp_path / "dri", access=access)
    assert usable is True and "renderD128" in note
    assert seen == [os.R_OK | os.W_OK]


def test_second_render_node_counts_when_the_first_is_inaccessible(tmp_path):
    (tmp_path / "dri").mkdir()
    for name in ("renderD128", "renderD129"):
        (tmp_path / "dri" / name).write_text("")
    usable, note = timelapse_encode.dri_status(
        tmp_path / "dri", access=lambda path, mode: path.endswith("renderD129"))
    assert usable is True and "renderD129" in note


def test_nvidia_status(tmp_path):
    present, note = timelapse_encode.nvidia_status(tmp_path)
    assert present is False and "--runtime=nvidia" in note and "NVIDIA_VISIBLE_DEVICES" in note
    (tmp_path / "nvidia0").write_text("")
    (tmp_path / "nvidiactl").write_text("")
    present, note = timelapse_encode.nvidia_status(tmp_path)
    assert present is True and "nvidia0" in note


def test_probe_uses_render_node_access_not_just_the_directory(monkeypatch):
    def fake_run(cmd, **kw):
        return type("R", (), {"stdout": "h264_vaapi libx264"})()
    monkeypatch.setattr(timelapse_encode, "dri_status", lambda *a, **k: (False, "no access"))
    monkeypatch.setattr(timelapse_encode, "nvidia_status", lambda *a, **k: (False, "none"))
    found = timelapse_encode.probe_ffmpeg("ffmpeg", run=fake_run)
    assert found.dri_present is False and found.nvidia_present is False
    assert [c.vendor for c in timelapse_encode.candidates("auto", found)] == ["cpu"]


# ------------------------------------------------------------ named notes

def test_gpu_notes_name_each_missing_vendor():
    notes = timelapse_encode.gpu_notes(probe(encoders={"h264_vaapi", "h264_nvenc", "libx264"},
                                             dri=False, nvidia=False),
                                       dri_note="DRI-WHY", nvidia_note="NV-WHY")
    assert notes == ("vaapi: DRI-WHY", "nvenc: NV-WHY", "qsv: h264_qsv is not in this ffmpeg build")


def test_gpu_notes_empty_when_every_gpu_is_a_candidate():
    assert timelapse_encode.gpu_notes(probe()) == ()


def test_selection_note_for_auto_without_gpu():
    found = probe(dri=False, nvidia=False)
    ordered = timelapse_encode.candidates("auto", found)
    note = timelapse_encode.selection_note("auto", ordered, found, dri_note="D", nvidia_note="N")
    assert note.startswith("timelapse encode: no GPU encoder available (")
    assert "vaapi: D" in note and "nvenc: N" in note and note.endswith("using CPU (libx264)")


def test_selection_note_for_unavailable_explicit_preference():
    found = probe(dri=False, nvidia=True)
    ordered = timelapse_encode.candidates("vaapi", found)
    note = timelapse_encode.selection_note("vaapi", ordered, found, dri_note="no /dev/dri here")
    assert note == ("timelapse encoder preference vaapi unavailable (no /dev/dri here); "
                    "using h264_nvenc")


def test_selection_note_silent_for_cpu_choice_and_gpu_led_plans():
    found = probe(dri=False, nvidia=False)
    assert timelapse_encode.selection_note("cpu", timelapse_encode.candidates("cpu", found), found) == ""
    assert timelapse_encode.selection_note("auto", timelapse_encode.candidates("auto", probe()), probe()) == ""
    assert timelapse_encode.selection_note("nvenc", timelapse_encode.candidates("nvenc", probe()), probe()) == ""


def test_ffmpeg_failure_names_the_last_stderr_line():
    exc = subprocess.CalledProcessError(1, ["ffmpeg"], stderr=FakeEncode.STDERR["h264_nvenc"])
    assert timelapse_encode.ffmpeg_failure(exc) == "ffmpeg exited 1: Error while opening encoder"
    assert timelapse_encode.ffmpeg_failure(subprocess.CalledProcessError(234, ["ffmpeg"])) == "ffmpeg exited 234"
    assert "timed out after 5" in timelapse_encode.ffmpeg_failure(subprocess.TimeoutExpired(["ffmpeg"], 5))
    assert timelapse_encode.ffmpeg_failure(FileNotFoundError(2, "nope", "ffmpeg")).startswith("ffmpeg not found")


# ------------------------------------------------------------ fallback end to end

def test_no_gpu_at_all_encodes_on_cpu_with_a_note(tmp_path):
    fake = FakeEncode()
    out, target, lines = assemble(tmp_path, fake, found=probe(dri=False, nvidia=False))
    assert out == target and target.is_file()
    assert encoders_tried(fake) == ["libx264"]
    assert any(line.startswith("timelapse encode: no GPU encoder available") for line in lines)


def test_every_gpu_failing_falls_back_to_cpu_and_names_each_failure(tmp_path):
    fake = FakeEncode(fail={"h264_vaapi", "h264_nvenc", "h264_qsv"})
    out, target, lines = assemble(tmp_path, fake)
    assert out == target and target.is_file()
    assert encoders_tried(fake) == ["h264_vaapi", "h264_nvenc", "h264_qsv", "libx264"]
    fallback = [line for line in lines if "fell back to CPU" in line]
    assert len(fallback) == 1
    assert "h264_vaapi: ffmpeg exited 1: [AVHWDeviceContext @ 0x1] Failed to initialise VAAPI" in fallback[0]
    assert "h264_nvenc: ffmpeg exited 1: Error while opening encoder" in fallback[0]
    assert "h264_qsv: ffmpeg exited 1: Error creating a MFX session" in fallback[0]


def test_preferred_gpu_failing_tries_the_next_gpu_before_cpu(tmp_path):
    fake = FakeEncode(fail={"h264_nvenc"})
    out, target, lines = assemble(tmp_path, fake, preference="nvenc")
    assert out == target
    assert encoders_tried(fake) == ["h264_nvenc", "h264_vaapi"]
    assert any("h264_nvenc failed: ffmpeg exited 1" in line for line in lines)
    assert not any("fell back to CPU" in line for line in lines)


def test_gpu_success_leaves_no_fallback_note(tmp_path):
    fake = FakeEncode()
    out, target, lines = assemble(tmp_path, fake)
    assert out == target and encoders_tried(fake) == ["h264_vaapi"]
    assert not any("CPU" in line for line in lines)


def test_oversize_gpu_output_is_named_before_the_quality_fallback(tmp_path):
    fake = FakeEncode(oversize={"h264_vaapi", "h264_nvenc", "h264_qsv", "libx264"})
    out, target, lines = assemble(tmp_path, fake)
    # Every bitrate attempt is oversize, so only the constant-quality CPU pass
    # remains; the fake's libx264 output stays oversize, so nothing fits.
    assert out is None
    named = [line for line in lines if "falling back to CPU (libx264, constant quality)" in line]
    assert len(named) == 1 and "h264_vaapi: output over the 9500000-byte cap" in named[0]


def test_cpu_preference_never_touches_the_gpu_first(tmp_path):
    fake = FakeEncode()
    out, target, lines = assemble(tmp_path, fake, preference="cpu")
    assert out == target and encoders_tried(fake) == ["libx264"]
    assert lines and not any("GPU" in line for line in lines)
