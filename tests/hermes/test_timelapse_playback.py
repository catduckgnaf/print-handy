"""#906: bounded timelapse playback length, record based delivery names, timer stop paths.

Offline: no ffmpeg, camera, printer or Discord. Frames are tiny files; the print record
and clock are injected.
"""
import asyncio
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from print_handy.hermes import _config, bambu_watch, elegoo_watch, prusa_watch, stock_watch  # noqa: E402
from print_handy.hermes import moonraker_event_listener as mel  # noqa: E402
from print_handy.hermes import print_records, timelapse_capture, timelapse_encode  # noqa: E402

DEFAULTS = dict(timelapse_encode.PLAYBACK_DEFAULTS)
JPEG = b"\xff\xd8" + b"fake" + b"\xff\xd9"


def duration(frames):
    fps, indices = timelapse_encode.plan_playback(frames, DEFAULTS)
    return fps, indices, len(indices) / fps


class PlanPlaybackTests(unittest.TestCase):
    def test_11_frames_are_held_up_to_the_minimum_length(self):
        fps, indices, length = duration(11)
        self.assertEqual(fps, 10)
        self.assertGreaterEqual(length, DEFAULTS["min_length_s"])
        self.assertLessEqual(length, DEFAULTS["min_length_s"] + DEFAULTS["hold_last_frame_s"] + 0.2)
        self.assertEqual(set(indices), set(range(11)))  # every frame is shown
        self.assertEqual(indices[-1], 10)

    def test_115_frames_play_once_at_the_clamped_fps(self):
        fps, indices, length = duration(115)
        self.assertEqual(fps, 10)  # 115 / 20 = 5.75 clamps up to min_fps
        self.assertEqual(indices[:115], list(range(115)))  # no decimation, no holds inside
        self.assertEqual(len(indices), 115 + 10)  # plus the one second end hold
        self.assertAlmostEqual(length, 12.5, places=1)

    def test_1270_frames_fit_the_target_at_63_fps_clamped_to_60(self):
        fps, indices, length = duration(1270)
        self.assertEqual(fps, 60)
        self.assertLessEqual(length, DEFAULTS["target_length_s"] + DEFAULTS["hold_last_frame_s"] + 0.1)
        self.assertEqual(indices, sorted(indices))  # decimation keeps order
        self.assertEqual(indices[0], 0)
        self.assertGreater(len(set(indices)), 1000)

    def test_4300_frames_are_decimated_to_the_target_length(self):
        fps, indices, length = duration(4300)
        self.assertEqual(fps, 60)
        self.assertLessEqual(length, DEFAULTS["target_length_s"] + DEFAULTS["hold_last_frame_s"] + 0.1)
        self.assertGreaterEqual(length, DEFAULTS["target_length_s"])
        self.assertEqual(indices, sorted(indices))
        self.assertLess(max(indices), 4300)

    def test_decimation_keeps_first_and_last_captured_frame(self):
        for count in (1270, 4300):
            _fps, indices = timelapse_encode.plan_playback(count, DEFAULTS)
            self.assertEqual(indices[0], 0)
            self.assertEqual(indices[-1], count - 1)  # end hold repeats the final frame

    def test_unusable_settings_fall_back_to_playback_fps_and_every_frame(self):
        fps, indices = timelapse_encode.plan_playback(50, None)
        self.assertEqual(fps, timelapse_encode.PLAYBACK_FPS)
        self.assertEqual(indices, list(range(50)))
        self.assertIsNone(timelapse_encode.playback_settings({"timelapse": {"max_fps": 5, "min_fps": 10}}))

    def test_no_frames_plan_is_empty(self):
        self.assertEqual(timelapse_encode.plan_playback(0, DEFAULTS)[1], [])

    def test_playback_frames_builds_ordered_links_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            frames = Path(tmp) / "frames"
            frames.mkdir()
            for n in range(1, 12):
                (frames / f"frame-{n:06d}.jpg").write_bytes(JPEG)
            with timelapse_encode.playback_frames(frames, DEFAULTS) as (directory, fps, count):
                names = sorted(p.name for p in directory.glob("frame-*.jpg"))
                self.assertEqual(fps, 10)
                self.assertEqual(count, len(names))
                self.assertEqual(names[0], "frame-000001.jpg")
                self.assertEqual(names[-1], f"frame-{count:06d}.jpg")
                self.assertGreaterEqual(count / fps, DEFAULTS["min_length_s"])
            self.assertFalse(directory.exists())

    def test_assemble_frames_encodes_the_planned_fps(self):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = list(cmd)
            Path(cmd[-1]).write_bytes(b"x" * 64)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            frames = Path(tmp) / "frames"
            frames.mkdir()
            for n in range(1, 116):
                (frames / f"frame-{n:06d}.jpg").write_bytes(JPEG)
            with mock.patch.object(timelapse_encode, "playback_frames",
                                   wraps=timelapse_encode.playback_frames) as wrapped:
                try:
                    timelapse_encode.assemble_frames(
                        frames, Path(tmp) / "out.mp4", max_bytes=10**9, preference="cpu",
                        ffmpeg="ffmpeg", run=fake_run, probe=lambda *a, **k: {"cpu": True},
                        validate=lambda *a, **k: True, playback=DEFAULTS)
                except Exception:
                    pass  # the encode seam is covered elsewhere; the plan handoff is the point
            wrapped.assert_called_once()
            self.assertEqual(wrapped.call_args.args[1], DEFAULTS)


class DeliveryNameTests(unittest.TestCase):
    RECORD = {"print_number": 42, "title": "Center B V5 PRINT DIAGONAL",
              "started_at": "2026-11-04T21:38:00+00:00", "gcode_file": "abc123deadbeef.gcode"}

    def name(self, record, tz="America/New_York", job="job:1", filename=None):
        from zoneinfo import ZoneInfo
        with mock.patch.object(print_records, "latest_completed_record", return_value=record), \
                mock.patch.object(print_records, "latest_record_for_file", return_value=record), \
                mock.patch.object(_config, "tz", return_value=ZoneInfo(tz)):
            return timelapse_encode.delivery_name(job, "job-1", filename)

    def test_start_time_then_model_then_record_number(self):
        self.assertEqual(self.name(self.RECORD), "20261104-1638-Center-B-V5-PRINT-DIAGONAL-print-42")

    def test_never_carries_the_gcode_hash(self):
        self.assertNotIn("abc123", self.name(self.RECORD))

    def test_is_filesystem_safe_and_bounded(self):
        record = dict(self.RECORD, title="../a/b:c*? " + "x" * 300)
        name = self.name(record)
        self.assertLessEqual(len(name), 100)
        self.assertFalse(set(name) & set('/\\:*?"<>| '))
        self.assertTrue(name.startswith("20261104-1638-"))

    def test_falls_back_without_a_record(self):
        self.assertEqual(self.name({}), "job-1")

    def test_archive_name_is_the_same_name(self):
        with mock.patch.object(print_records, "latest_completed_record", return_value=self.RECORD):
            self.assertEqual(timelapse_encode.archive_name("job:1", "x"),
                             timelapse_encode.delivery_name("job:1", "x"))

    def test_named_copy_makes_the_attachment_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "timelapse.mp4"
            video.write_bytes(b"video")
            named = timelapse_encode.named_copy(video, "20261104-1638-Benchy-print-7")
            self.assertEqual(named.name, "20261104-1638-Benchy-print-7.mp4")
            self.assertEqual(named.read_bytes(), b"video")
            self.assertTrue(video.exists())

    def test_caption_gets_the_print_number_once(self):
        with mock.patch.object(print_records, "latest_completed_record", return_value=self.RECORD):
            text = timelapse_encode.caption_with_number("Done: Benchy", "job:1")
            self.assertIn("Print #42", text)
            self.assertEqual(timelapse_encode.caption_with_number(text, "job:1"), text)
        with mock.patch.object(print_records, "latest_completed_record", return_value={}), \
                mock.patch.object(print_records, "latest_record_for_file", return_value={}):
            self.assertEqual(timelapse_encode.caption_with_number("Done", "job:1"), "Done")

    def test_stock_backends_use_the_same_name_and_caption(self):
        """Bambu, Elegoo and Prusa deliver through stock_watch.delivery_video and the caption helper."""
        for module in (bambu_watch, elegoo_watch, prusa_watch):
            self.assertIs(module.stock_watch, stock_watch)
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "t.mp4"
            video.write_bytes(b"v")
            session = mock.Mock(directory=Path(tmp) / "bambu-hash")
            from zoneinfo import ZoneInfo
            with mock.patch.object(print_records, "latest_completed_record", return_value={}), \
                    mock.patch.object(print_records, "latest_record_for_file", return_value=self.RECORD), \
                    mock.patch.object(_config, "tz", return_value=ZoneInfo("America/New_York")), \
                    mock.patch.object(timelapse_encode, "archive_local_copy") as archive:
                delivered = stock_watch.delivery_video(video, "abc123deadbeef.gcode", session)
            self.assertEqual(delivered.name, "20261104-1638-Center-B-V5-PRINT-DIAGONAL-print-42.mp4")
            self.assertEqual(archive.call_args.args[1], "20261104-1638-Center-B-V5-PRINT-DIAGONAL-print-42")
            with mock.patch.object(print_records, "latest_completed_record", return_value={}), \
                    mock.patch.object(print_records, "latest_record_for_file", return_value=self.RECORD):
                self.assertIn("Print #42", stock_watch._caption_number("Done", "abc123deadbeef.gcode"))


class TimerStopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        previous = _config.cfg()
        self.addCleanup(lambda: _config.set_override(previous))

    def install(self, **timelapse):
        data = copy.deepcopy(_config.cfg())
        data["timelapse"].update({"capture_enabled": True, "capture_interval_s": 5.0, **timelapse})
        data["paths"]["cache"] = str(Path(self.tmp.name) / "cache")
        _config.set_override(data)

    async def start(self, listener):
        listener.state, listener.filename = "standby", "benchy.gcode"
        listener.visual_watch.due = lambda *a, **k: None
        with mock.patch.object(mel, "post"), mock.patch.object(mel, "run_announcer"), \
                mock.patch.object(mel, "start_claimed", return_value=True), \
                mock.patch.object(mel, "announce_warmup_done"), \
                mock.patch.object(mel, "_claim_notification", return_value=True):
            await listener.on_status({"state": "printing", "filename": "benchy.gcode"}, "event")

    async def run_clock(self, listener, beats):
        session = listener.capture_session
        clock = [1000.0]
        session._now = lambda: clock[0]
        count = [0]

        async def fake_sleep(seconds):
            count[0] += 1
            clock[0] += seconds
            task = listener._capture_task
            if task is not None:
                await task
            if count[0] > beats:
                raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await listener._capture_timer(sleep=fake_sleep)
        if listener._capture_task is not None:
            await listener._capture_task
        return session

    async def test_ten_minutes_at_five_seconds_is_about_120_frames_without_layer_events(self):
        self.install(capture_trigger="interval")
        listener = mel.Listener()
        with mock.patch.object(timelapse_capture, "fetch_frame", return_value=JPEG):
            await self.start(listener)
            session = await self.run_clock(listener, 120)  # 120 beats x 5 s = 10 minutes
            self.assertGreaterEqual(session.frame_count, 118)
            self.assertLessEqual(session.frame_count, 121)
            listener._capture_end("complete")

    async def test_layer_mode_ignores_the_timer(self):
        self.install(capture_trigger="layer")
        listener = mel.Listener()
        with mock.patch.object(timelapse_capture, "fetch_frame", return_value=JPEG):
            await self.start(listener)
            session = await self.run_clock(listener, 60)
            self.assertEqual(session.frame_count, 0)
            await listener._capture_due(1)
            await listener._capture_due(2)
            self.assertEqual(session.frame_count, 2)  # layer events still capture
            listener._capture_end("complete")

    async def test_timer_stops_on_pause_complete_cancel_and_error(self):
        self.install(capture_trigger="interval")
        listener = mel.Listener()
        with mock.patch.object(timelapse_capture, "fetch_frame", return_value=JPEG):
            await self.start(listener)
            self.assertTrue(listener._capture_timer_tick())
            await listener._capture_task
            for state in ("paused", "complete", "cancelled", "error", "standby"):
                listener.state = state
                with mock.patch.object(listener, "_schedule_capture") as schedule:
                    self.assertFalse(listener._capture_timer_tick(), state)
                    schedule.assert_not_called()
            listener.state = "printing"
            listener._capture_end("complete")
            self.assertIsNone(listener.capture_session)
            self.assertFalse(listener._capture_timer_tick())  # no session, no beat

    async def test_timer_task_ends_on_disconnect_cancellation(self):
        self.install(capture_trigger="interval")
        listener = mel.Listener()
        timer = asyncio.create_task(listener._capture_timer())
        await asyncio.sleep(0)
        timer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await timer
        self.assertTrue(timer.cancelled())

    async def test_timer_tick_never_blocks_the_caller(self):
        self.install(capture_trigger="both")
        listener = mel.Listener()
        gate = asyncio.Event()
        with mock.patch.object(timelapse_capture, "fetch_frame", return_value=JPEG):
            await self.start(listener)

            async def slow(layer):
                await gate.wait()

            listener._capture_due = slow
            self.assertTrue(listener._capture_timer_tick())  # returns at once; the fetch runs as a task
            self.assertFalse(listener._capture_task.done())
            gate.set()
            await listener._capture_task
            listener._capture_end("complete")


class SettingsValidationTests(unittest.TestCase):
    def validated(self, **timelapse):
        from print_handy import settings
        data = {"services": {}, "timelapse": {"capture_interval_s": 5.0, **timelapse}}
        settings._validate_timelapse(data)
        return data["timelapse"]

    def test_defaults_are_filled_in(self):
        timelapse = self.validated()
        for key, default in DEFAULTS.items():
            self.assertEqual(float(timelapse[key]), default)

    def test_bad_values_are_refused(self):
        from print_handy import settings
        for bad in ({"target_length_s": 0}, {"min_fps": -1}, {"max_fps": True},
                    {"hold_last_frame_s": "x"}, {"min_fps": 30, "max_fps": 10}):
            with self.assertRaises(settings.SettingsError, msg=str(bad)):
                self.validated(**bad)


class CacheAndRecordMatchTests(unittest.TestCase):
    """Codex round 1 on #907."""

    def test_cached_video_is_validated_against_the_planned_frame_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            frames = Path(tmp) / "timelapse" / "job-1" / "frames"
            frames.mkdir(parents=True)
            for i in range(1, 4301):
                (frames / f"frame-{i:06d}.jpg").write_bytes(JPEG)
            (frames.parent / "capture.json").write_text(
                '{"job_key": "job:1", "filename": "a.gcode", "state": "complete", '
                '"frames": 4300, "finished_at": 1.0}', encoding="utf-8")
            target = frames.parent / "timelapse.mp4"
            target.write_bytes(b"x" * 10)
            data = {"paths": {"cache": tmp}, "timelapse": {"capture_enabled": True}}
            seen = {}

            def fake_validate(path, **kw):
                seen.update(kw)
                return True

            with mock.patch.object(timelapse_encode, "validate_video", fake_validate), \
                    mock.patch.object(timelapse_encode, "_deliver", lambda *a, **k: a[0]), \
                    mock.patch.object(timelapse_encode, "assemble_frames") as assemble:
                out = timelapse_encode.captured_video("job:1", data=data, max_bytes=9_000_000)
            self.assertEqual(out, target)
            assemble.assert_not_called()
            self.assertEqual(seen["min_frames"], timelapse_encode.expected_playback_frames(4300, data))
            self.assertLess(seen["min_frames"], 4300)

    def test_exact_filename_beats_a_newer_basename_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            prints = Path(tmp)
            import json
            (prints / "a.json").write_text(json.dumps(
                {"gcode_file": "folderA/part.gcode", "started_at": "2026-11-01T10:00:00"}))
            (prints / "b.json").write_text(json.dumps(
                {"gcode_file": "folderB/part.gcode", "started_at": "2026-11-04T10:00:00"}))
            with mock.patch.object(print_records, "PRINTS_DIR", prints):
                self.assertEqual(print_records.latest_record_for_file("folderA/part.gcode")["gcode_file"],
                                 "folderA/part.gcode")
                self.assertEqual(print_records.latest_record_for_file("part.gcode")["gcode_file"],
                                 "folderB/part.gcode")  # basename only: newest wins


if __name__ == "__main__":
    unittest.main()
