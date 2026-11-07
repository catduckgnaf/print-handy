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
                find_clips=lambda year: clips,
                ai_call=lambda lines: lines)
            self.assertTrue(result["ok"], result)
            # 2 cards (title + one script line) at CARD_SECONDS each,
            # plus the two 3-second clips (shorter than CLIP_SECONDS).
            expected = 2 * wrapped_video.CARD_SECONDS + 2 * 3
            actual = _duration(Path(result["path"]))
            self.assertAlmostEqual(actual, expected, delta=1.5)


if __name__ == "__main__":
    unittest.main()
