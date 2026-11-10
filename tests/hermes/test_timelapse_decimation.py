"""Adaptive decimation: a long print yields an evenly sampled clip, not a
truncated one. Fake clock, fake camera, no real time passes."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from print_handy.hermes import timelapse_capture, timelapse_encode  # noqa: E402

JPEG = b"\xff\xd8" + b"fake-jpeg-payload" + b"\xff\xd9"


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def stamped_fetch(clock):
    """A camera whose every frame carries the second it was taken at."""
    def fetch(url):
        return JPEG + str(int(clock.now - 1_000_000.0)).encode()

    return fetch


def run(tmp, hours, *, interval=5.0, max_frames=40, trigger="interval", max_bytes=0):
    clock = Clock()
    session = timelapse_capture.CaptureSession(
        Path(tmp) / "timelapse", "job:1", "long.gcode",
        trigger=timelapse_capture.make_trigger(trigger, interval),
        max_frames=max_frames, max_bytes=max_bytes, url="http://cam/s",
        fetch=stamped_fetch(clock), now=clock)
    session.start()
    taken = []
    for second in range(int(hours * 3600)):
        clock.now = 1_000_000.0 + second
        if session.tick(layer=second // 60 if trigger != "interval" else None):
            taken.append(second)
    return session, taken


def stored_stamps(session):
    """The capture-second each stored frame was taken at (recovered from its bytes)."""
    return [int(frame.read_bytes()[len(JPEG):]) for frame in
            sorted(session.frames_dir.glob("frame-*.jpg"))]


class DecimationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_a_print_far_past_the_cap_is_never_truncated(self):
        session, taken = run(self.tmp.name, hours=2, interval=5.0, max_frames=40)
        # 2 h at 5 s would be 1440 frames; the cap is 40.
        self.assertFalse(session.capped)
        self.assertTrue(10 <= session.frame_count <= 40, session.frame_count)
        manifest = session.stop("complete")
        self.assertGreater(manifest["decimations"], 3)
        self.assertGreater(manifest["interval_s"], 5.0)
        self.assertEqual(manifest["interval_s"], 5.0 * 2 ** manifest["decimations"])
        # The kept frames reach (nearly) to the end of the print: not just the first hours.
        stamps = [t for t in taken]
        self.assertGreater(max(stamps), 2 * 3600 - 2 * manifest["interval_s"])

    def test_surviving_frames_are_evenly_spaced_and_contiguously_numbered(self):
        session, _ = run(self.tmp.name, hours=2, interval=5.0, max_frames=40)
        names = sorted(p.name for p in session.frames_dir.glob("frame-*.jpg"))
        self.assertEqual(names, [f"frame-{n:06d}.jpg" for n in range(1, len(names) + 1)])
        # Frame bytes carry the second they were taken at: after any number of
        # halvings the survivors are one uniform spacing apart.
        counters = stored_stamps(session)
        gaps = {b - a for a, b in zip(counters, counters[1:])}
        self.assertEqual(len(gaps), 1, gaps)

    def test_counters_and_bytes_follow_the_files(self):
        session, _ = run(self.tmp.name, hours=1, interval=5.0, max_frames=40)
        on_disk = list(session.frames_dir.glob("frame-*.jpg"))
        self.assertEqual(session.frame_count, len(on_disk))
        self.assertEqual(session.manifest()["bytes"], sum(p.stat().st_size for p in on_disk))

    def test_the_manifest_on_disk_records_the_effective_interval(self):
        session, _ = run(self.tmp.name, hours=1, interval=5.0, max_frames=40)
        session.stop("complete")
        manifest = timelapse_capture.read_manifest(session.directory)
        self.assertEqual(manifest["interval_s"], session.interval_s())
        self.assertGreater(manifest["decimations"], 0)

    def test_a_short_print_is_untouched(self):
        session, taken = run(self.tmp.name, hours=0.05, interval=5.0, max_frames=3600)
        self.assertEqual(session.manifest()["decimations"], 0)
        self.assertEqual(session.manifest()["interval_s"], 5.0)
        self.assertEqual(session.frame_count, len(taken))

    def test_layer_trigger_keeps_an_even_sample_of_layers(self):
        session, _ = run(self.tmp.name, hours=2, trigger="layer", max_frames=16)
        self.assertFalse(session.capped)
        self.assertGreater(session.manifest()["decimations"], 0)
        # 120 layers, one per minute: what is kept must reach the last layers.
        self.assertLessEqual(session.frame_count, 16)
        self.assertGreater(session.frame_count, 4)

    def test_both_trigger_thins_layer_captures_across_the_print(self):
        session, taken = run(self.tmp.name, hours=2, trigger="both", interval=300,
                             max_frames=16)
        self.assertFalse(session.capped)
        self.assertGreater(session.manifest()["decimations"], 0)
        self.assertGreater(session.frame_count, 4)
        self.assertLessEqual(session.frame_count, 16)
        self.assertLess(taken[0], 2 * 3600 - 60)
        self.assertGreater(taken[-1], 2 * 3600 - 2 * (session.interval_s() or 0))

    def test_the_byte_cap_still_ends_capture_and_sets_capped(self):
        session, _ = run(self.tmp.name, hours=1, interval=5.0, max_frames=0,
                         max_bytes=len(JPEG) * 10)
        self.assertTrue(session.capped)


class CoverageNoteTests(unittest.TestCase):
    def test_a_byte_capped_clip_says_how_much_it_covers(self):
        note = timelapse_encode.coverage_note(
            {"capped": True, "frames": 3600, "interval_s": 5.0})
        self.assertIn("first 5.0 hours", note)

    def test_a_short_cover_is_in_minutes(self):
        note = timelapse_encode.coverage_note({"capped": True, "frames": 60, "interval_s": 5.0})
        self.assertIn("first 5 minutes", note)

    def test_a_thinned_long_print_says_the_sampling(self):
        note = timelapse_encode.coverage_note(
            {"capped": False, "frames": 900, "interval_s": 20.0, "decimations": 2})
        self.assertIn("one frame every 20 seconds", note)

    def test_a_plain_capture_has_no_note(self):
        self.assertEqual(timelapse_encode.coverage_note(
            {"capped": False, "frames": 90, "interval_s": 5.0, "decimations": 0}), "")
        self.assertEqual(timelapse_encode.coverage_note(None), "")

    def test_the_caption_carries_the_note_once(self):
        from unittest import mock
        with mock.patch.object(timelapse_encode, "capture_coverage_note",
                               return_value="Timelapse note: cut short."), \
                mock.patch.object(timelapse_encode, "_record_for", return_value={}):
            text = timelapse_encode.caption_with_number("Done", "job:1")
            self.assertEqual(text, "Done\nTimelapse note: cut short.")
            self.assertEqual(timelapse_encode.caption_with_number(text, "job:1"), text)
            self.assertEqual(timelapse_encode.caption_with_number("Done", None), "Done")


if __name__ == "__main__":
    unittest.main()
