"""#981: one real-ffmpeg integration test for the Wrapped video pacing fix.

The fake-runner tests in test_print_stats_wrapped.py cannot see the
stream-copy concat pacing bug (two 3-second clips at mismatched frame
rates produced a 72-second video where about 60 was expected in the
#981 prototype). This test generates its own short clips with real
ffmpeg, builds a video through the real build path, probes the result
with ffprobe and asserts the total duration. It skips cleanly when
ffmpeg/ffprobe are absent, following the repo's existing pattern
(tests/hermes/test_timelapse_encode.py). No fixture media is committed.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from print_handy.webui import wrapped_video

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def _make_clip(path, rate, seconds=3):
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
         f"testsrc=duration={seconds}:size=640x360:rate={rate}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True)


def _duration(path):
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True).stdout.strip()
    return float(out)


@unittest.skipUnless(FFMPEG and FFPROBE, "needs ffmpeg and ffprobe on PATH")
class RealFfmpegPacingTests(unittest.TestCase):
    def test_clips_at_mismatched_frame_rates_keep_true_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            clips = []
            for index, rate in enumerate((10, 60)):
                clip = tmp / f"clip-{index}.mp4"
                _make_clip(clip, rate, seconds=3)
                clips.append(clip)
            wrapped = {"year": 2026, "headline": {}, "highlights": {},
                       "script": ["One line."]}
            result = wrapped_video.build_wrapped_video(
                2026, wrapped, tmp / "out", ffmpeg=FFMPEG,
                find_clips=lambda year: clips)
            self.assertTrue(result["ok"], result)
            # Step 4 pacing: slide durations from build_slides (hook +
            # commentary + finale), the full-bleed footage moment at
            # FOOTAGE_SECONDS and the second clip at CLIP_SECONDS —
            # clip segments loop their source, so their durations are
            # the nominal ones even though the sources are 3 seconds.
            expected = (sum(s["duration"] for s in wrapped_video.build_slides(wrapped))
                        + wrapped_video.FOOTAGE_SECONDS + wrapped_video.CLIP_SECONDS)
            actual = _duration(Path(result["path"]))
            self.assertAlmostEqual(actual, expected, delta=1.5)

    def test_synthesized_bed_muxes_a_real_audio_stream(self):
        """Step 4: the synthesized music bed is really muxed in, and
        the total duration stays the slide timeline's duration."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            wrapped = {"year": 2026, "headline": {"failed": 1},
                       "highlights": {}, "script": []}
            result = wrapped_video.build_wrapped_video(
                2026, wrapped, tmp / "out", ffmpeg=FFMPEG,
                find_clips=lambda year: [])
            self.assertTrue(result["ok"], result)
            probe = subprocess.run(
                [FFPROBE, "-v", "error", "-show_entries", "stream=codec_type",
                 "-of", "csv=p=0", result["path"]],
                check=True, capture_output=True, text=True).stdout
            self.assertIn("audio", probe)
            expected = sum(s["duration"] for s in wrapped_video.build_slides(wrapped))
            self.assertAlmostEqual(_duration(Path(result["path"])), expected, delta=1.5)


if __name__ == "__main__":
    unittest.main()
