"""#981: one real-ffmpeg integration test for the Wrapped video pacing fix.

The fake-runner tests in test_print_stats_wrapped.py cannot see the
stream-copy concat pacing bug (two 3-second clips at mismatched frame
rates produced a 72-second video where about 60 was expected in the
#981 prototype). This test generates its own short clips with real
ffmpeg, builds a video through the real build path, probes the result
with ffprobe and asserts the total duration. It skips cleanly when
ffmpeg/ffprobe are absent, following the repo's existing pattern
(tests/runtime/test_timelapse_encode.py). No fixture media is committed.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from print_handy.webui import pasta_reel, wrapped_video

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def _make_clip(path, rate, seconds=3):
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
         f"testsrc=duration={seconds}:size=640x360:rate={rate}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True)


def _mean_volume(path, *seek):
    """The mean volume in dB of a stretch of ``path`` (``seek`` picks it)."""
    out = subprocess.run(
        [FFMPEG, "-hide_banner", *seek, "-i", str(path), "-t", "0.4",
         "-af", "volumedetect", "-f", "null", "-"],
        check=True, capture_output=True, text=True).stderr
    line = next(row for row in out.splitlines() if "mean_volume:" in row)
    return float(line.split("mean_volume:")[1].split("dB")[0])


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

    def test_default_bed_muxes_a_real_audio_stream(self):
        """Step 4: the default music bed (the bundled track) is really
        muxed in, and the total duration stays the slide timeline's."""
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

    def test_titles_with_percent_and_filter_syntax_compile(self):
        """A print titled "50% scale cube" (or one carrying %{...}, quotes,
        colons, brackets) used to break every drawtext in the compile."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            title = "50% scale cube %{pts} it's: [a], {b}; c=d"
            wrapped = {"year": 2026, "headline": {"total_prints": 3},
                       "highlights": {"longest_print": {"title": title, "seconds": 7200},
                                      "most_reprinted": {"title": title, "count": 4}},
                       "script": [title]}
            result = wrapped_video.build_wrapped_video(
                2026, wrapped, tmp / "out", ffmpeg=FFMPEG,
                find_clips=lambda year: [])
            self.assertTrue(result["ok"], result)
            self.assertGreater(_duration(Path(result["path"])), 10)

    def test_pasta_reel_fits_the_track_with_audio_and_a_faded_end(self):
        """The reel: label cards and cuts at one pace, the bundled track
        under it, faded out at the end, never longer than the track."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            clips, records = [], []
            for number, seconds in ((1, 4), (2, 12), (3, 2)):
                path = tmp / f"20260304-1000-Printer-print-{number}.mp4"
                _make_clip(path, 30, seconds=seconds)
                clips.append({"path": str(path), "print_number": number})
                records.append({"print_number": number, "status": "failed",
                                "title": f"Widget {number}",
                                "started_at": "2026-03-04T10:00:00Z",
                                "print_duration_seconds": 3600, "filament_used_g": 20.0})
            wrapped = {"year": 2026,
                       "headline": {"failed": 2, "pasta": 1, "pasta_state": "recorded"},
                       "highlights": {"failed_print_numbers": [1, 2],
                                      "pasta_print_numbers": [3]}}
            result = pasta_reel.build_pasta_reel(
                2026, wrapped, tmp / "out", ffmpeg=FFMPEG, find_clips=lambda year: clips,
                records=records, markers_for=lambda record: [])
            self.assertTrue(result["ok"], result)
            path = Path(result["path"])
            expected = pasta_reel.reel_seconds(3, pasta_reel.CUT_MAX_SECONDS)
            self.assertAlmostEqual(_duration(path), expected, delta=1.5)
            self.assertLessEqual(_duration(path), _duration(wrapped_video.BUNDLED_MUSIC))
            probe = subprocess.run(
                [FFPROBE, "-v", "error", "-show_entries", "stream=codec_type",
                 "-of", "csv=p=0", str(path)],
                check=True, capture_output=True, text=True).stdout
            self.assertIn("audio", probe)
            middle = _mean_volume(path, "-ss", str(expected / 2))
            end = _mean_volume(path, "-sseof", "-0.4")
            self.assertLess(end, middle - 6, (middle, end))


if __name__ == "__main__":
    unittest.main()
