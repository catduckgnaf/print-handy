"""A hung hardware encoder must not hold the completion post for half an hour."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from print_handy import live_stream  # noqa: E402
from print_handy.hermes import timelapse_encode  # noqa: E402


def test_hardware_gets_a_short_limit_and_software_scales_with_frames():
    assert timelapse_encode.encode_timeout("vaapi", 100) < 300
    assert timelapse_encode.encode_timeout("nvenc", 3600) < 600
    assert timelapse_encode.encode_timeout("cpu", 100) < timelapse_encode.encode_timeout("cpu", 3600)
    assert timelapse_encode.encode_timeout("cpu", 3600) > 1800  # a long capture is not cut at 30 min
    assert timelapse_encode.encode_timeout("cpu", 0) >= 300
    assert timelapse_encode.encode_timeout("vaapi", 3600) < timelapse_encode.encode_timeout("cpu", 3600)


def test_a_hung_hardware_encoder_falls_back_to_cpu_after_its_short_limit(tmp_path):
    frames = tmp_path / "frames"
    frames.mkdir()
    for number in range(1, 41):
        (frames / f"frame-{number:06d}.jpg").write_bytes(b"\xff\xd8x\xff\xd9")
    seen = []

    def fake_run(cmd, **kwargs):
        joined = " ".join(map(str, cmd))
        if "-framerate" in cmd and "-c:v" not in cmd and "libx264" not in joined:
            pass
        vendor = "cpu" if "libx264" in joined else "hw"
        seen.append((vendor, kwargs.get("timeout")))
        if vendor == "hw":
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
        Path(cmd[-1]).write_bytes(b"video")
        return None

    probe = live_stream.EncoderProbe(
        encoders=frozenset({"libx264", "h264_vaapi"}), dri_present=True)
    target = tmp_path / "timelapse.mp4"
    out = timelapse_encode.assemble_frames(
        frames, target, max_bytes=9_000_000, preference="vaapi", probe=probe,
        run=fake_run, validate=lambda path: True, fps=10)
    assert out == target
    hardware = [t for vendor, t in seen if vendor == "hw"]
    software = [t for vendor, t in seen if vendor == "cpu"]
    assert hardware and software
    assert hardware[0] == timelapse_encode.encode_timeout("vaapi", 40) < 300
    assert software[0] == timelapse_encode.encode_timeout("cpu", 40)
