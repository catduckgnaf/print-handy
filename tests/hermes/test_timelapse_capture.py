"""#246 stage 2: in-container timelapse frame capture.

Everything offline: frames come from an injected fetch or a loopback HTTP
fixture, Moonraker events are driven through the listener directly, and
no camera, printer, GPU or Discord is contacted.
"""
import asyncio
import copy
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from print_handy import settings  # noqa: E402
from print_handy.hermes import _config, timelapse_capture  # noqa: E402
from print_handy.hermes import moonraker_event_listener as mel  # noqa: E402

JPEG = b"\xff\xd8" + b"fake-jpeg-payload" + b"\xff\xd9"
SECRET_URL = "http://user:hunter2@cam.example/webcam/?action=snapshot"


def make_data(root, **timelapse):
    base = {"capture_enabled": True, "capture_source_url": "",
            "capture_trigger": "interval", "capture_interval_s": 5.0,
            "capture_max_frames": 3600, "capture_max_bytes": 536870912,
            "capture_encoder": "auto", "local_retention_days": 1}
    base.update(timelapse)
    return {"timelapse": base,
            "services": {"camera_snapshot_url": "http://printer.example/webcam/?action=snapshot"},
            "paths": {"cache": str(root)}}


class Clock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_session(root, clock, **kwargs):
    kwargs.setdefault("trigger", timelapse_capture.make_trigger("interval", 5.0))
    kwargs.setdefault("url", "http://cam.example/snapshot")
    kwargs.setdefault("fetch", lambda url: JPEG)
    kwargs.setdefault("now", clock)
    session = timelapse_capture.CaptureSession(root, "job:7", "benchy.gcode", **kwargs)
    return session


class SessionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "timelapse"
        self.clock = Clock()

    def test_start_creates_frames_dir_and_manifest(self):
        session = make_session(self.root, self.clock)
        self.assertTrue(session.start())
        self.assertTrue(session.frames_dir.is_dir())
        manifest = json.loads(session.manifest_path.read_text())
        self.assertEqual(manifest["job_key"], "job:7")
        self.assertEqual(manifest["filename"], "benchy.gcode")
        self.assertEqual(manifest["state"], "capturing")
        self.assertEqual(manifest["trigger"], "interval")
        self.assertEqual(manifest["frames"], 0)
        self.assertEqual(manifest["started_at"], self.clock.now)

    def test_interval_trigger_captures_first_frame_then_every_interval(self):
        session = make_session(self.root, self.clock)
        session.start()
        self.assertTrue(session.tick())  # first frame is due immediately
        self.assertFalse(session.tick())  # same instant: not due
        self.clock.advance(4.9)
        self.assertFalse(session.tick())
        self.clock.advance(0.1)
        self.assertTrue(session.tick())
        frames = sorted(p.name for p in session.frames_dir.glob("*.jpg"))
        self.assertEqual(frames, ["frame-000001.jpg", "frame-000002.jpg"])
        self.assertEqual((session.frames_dir / "frame-000001.jpg").read_bytes(), JPEG)
        manifest = json.loads(session.manifest_path.read_text())
        self.assertEqual(manifest["frames"], 2)
        self.assertEqual(manifest["bytes"], 2 * len(JPEG))

    def test_stop_stamps_the_manifest_and_keeps_frames(self):
        session = make_session(self.root, self.clock)
        session.start()
        session.tick()
        self.clock.advance(60)
        manifest = session.stop("cancelled")
        self.assertEqual(manifest["state"], "cancelled")
        self.assertEqual(manifest["finished_at"], self.clock.now)
        self.assertEqual(manifest["frames"], 1)
        self.assertTrue((session.frames_dir / "frame-000001.jpg").is_file())
        self.assertFalse(session.active)
        self.assertFalse(session.tick())  # a stopped session never captures

    def test_frame_fetched_after_stop_is_discarded(self):
        # stop() lands while the fetch is in flight (2.2.5 review, P2-133).
        holder = {}

        def fetch(url):
            holder["session"].stop("complete")
            return JPEG

        session = make_session(self.root, self.clock, fetch=fetch)
        holder["session"] = session
        session.start()
        self.assertFalse(session.capture_once())
        self.assertEqual(list(session.frames_dir.glob("*.jpg")), [])
        self.assertEqual(session.frame_count, 0)
        manifest = json.loads(session.manifest_path.read_text())
        self.assertEqual(manifest["frames"], 0)

    def test_start_failure_is_soft(self):
        # frames/ already exists as a file: mkdir fails, start reports False.
        blocker = self.root / "job-7" / "frames"
        blocker.parent.mkdir(parents=True)
        blocker.write_bytes(b"not a directory")
        session = make_session(self.root, self.clock)
        self.assertFalse(session.start())
        self.assertEqual(session.state, "failed")


class TriggerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "timelapse"
        self.clock = Clock()

    def test_layer_trigger_fires_on_change_only(self):
        session = make_session(self.root, self.clock,
                               trigger=timelapse_capture.make_trigger("layer", 5.0))
        session.start()
        self.assertFalse(session.tick())  # no layer seen yet
        self.assertTrue(session.tick(layer=1))  # first layer
        self.assertFalse(session.tick(layer=1))  # same layer
        self.clock.advance(3600)
        self.assertFalse(session.tick(layer=1))  # time alone never fires it
        self.assertTrue(session.tick(layer=2))
        self.assertEqual(session.frame_count, 2)

    def test_both_trigger_fires_on_either(self):
        session = make_session(self.root, self.clock,
                               trigger=timelapse_capture.make_trigger("both", 5.0))
        session.start()
        self.assertTrue(session.tick(layer=1))  # interval's first frame
        self.assertTrue(session.tick(layer=2))  # layer change, interval not due
        self.assertFalse(session.tick(layer=2))
        self.clock.advance(5.0)
        self.assertTrue(session.tick(layer=2))  # interval floor
        self.assertEqual(session.frame_count, 3)

    def test_unknown_trigger_falls_back_to_interval(self):
        trigger = timelapse_capture.make_trigger("nonsense", 5.0)
        self.assertEqual(trigger.name, "interval")


class CapAndFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "timelapse"
        self.clock = Clock()

    def test_frame_cap_stops_capture_and_keeps_frames(self):
        session = make_session(self.root, self.clock, max_frames=2)
        session.start()
        self.assertTrue(session.capture_once())
        self.assertTrue(session.capture_once())
        self.assertFalse(session.capture_once())
        self.assertTrue(session.capped)
        self.assertTrue(session.active)  # capped, not ended: stop() still stamps it
        manifest = session.stop("complete")
        self.assertEqual(manifest["frames"], 2)
        self.assertTrue(manifest["capped"])

    def test_byte_cap_refuses_the_frame_that_would_overflow(self):
        session = make_session(self.root, self.clock, max_bytes=len(JPEG))
        session.start()
        self.assertTrue(session.capture_once())
        self.assertFalse(session.capture_once())
        self.assertTrue(session.capped)
        self.assertEqual(session.frame_count, 1)

    def test_zero_caps_disable_them(self):
        session = make_session(self.root, self.clock, max_frames=0, max_bytes=0)
        session.start()
        for _ in range(5):
            self.assertTrue(session.capture_once())
        self.assertFalse(session.capped)

    def test_fetch_failure_is_soft_and_retried_at_the_interval(self):
        attempts = []

        def flaky(url):
            attempts.append(url)
            if len(attempts) == 1:
                raise ConnectionError("camera gone")
            return JPEG

        logs = []
        session = make_session(self.root, self.clock, fetch=flaky, log=logs.append)
        session.start()
        self.assertFalse(session.tick())
        self.assertEqual(session.failures, 1)
        self.assertTrue(any("fetch failed" in line for line in logs))
        self.assertFalse(session.tick())  # no hot retry on the next event
        self.clock.advance(5.0)
        self.assertTrue(session.tick())
        self.assertEqual(session.frame_count, 1)

    def test_credentials_never_reach_logs_or_manifest(self):
        logs = []

        def broken(url):
            raise ConnectionError("refused")

        session = make_session(self.root, self.clock, url=SECRET_URL,
                               fetch=broken, log=logs.append)
        session.start()
        session.capture_once()
        manifest = json.loads(session.manifest_path.read_text())
        self.assertNotIn("hunter2", "\n".join(logs))
        self.assertNotIn("hunter2", json.dumps(manifest))
        self.assertIn("cam.example", manifest["source"])


def _jpeg(rgb, size=(64, 48)):
    import io

    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", size, rgb).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


DARK_JPEG = _jpeg((0, 0, 0))
BRIGHT_JPEG = _jpeg((200, 200, 200))


class DarkFrameCaptureTests(unittest.TestCase):
    """#476: dark frames are skipped and counted, never stored."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "timelapse"
        self.clock = Clock()

    def test_dark_frames_are_skipped_and_counted(self):
        frames = iter([DARK_JPEG, BRIGHT_JPEG, DARK_JPEG])
        session = make_session(self.root, self.clock, fetch=lambda url: next(frames))
        session.start()
        self.assertFalse(session.capture_once())
        self.assertTrue(session.capture_once())
        self.assertFalse(session.capture_once())
        self.assertEqual(session.frame_count, 1)
        self.assertEqual(session.dark_frames, 2)
        stored = sorted(path.name for path in session.frames_dir.iterdir())
        self.assertEqual(stored, ["frame-000001.jpg"])
        self.assertEqual((session.frames_dir / "frame-000001.jpg").read_bytes(), BRIGHT_JPEG)
        manifest = session.manifest()
        self.assertEqual(manifest["frames"], 1)
        self.assertEqual(manifest["dark_frames"], 2)

    def test_all_dark_session_keeps_nothing(self):
        logs = []
        session = make_session(self.root, self.clock,
                               fetch=lambda url: DARK_JPEG, log=logs.append)
        session.start()
        for _ in range(3):
            self.assertFalse(session.capture_once())
        manifest = session.stop()
        self.assertEqual(session.frame_count, 0)
        self.assertEqual(session.dark_frames, 3)
        self.assertEqual(manifest["frames"], 0)
        self.assertEqual(manifest["dark_frames"], 3)
        self.assertEqual(list(session.frames_dir.iterdir()), [])
        self.assertTrue(any("completely dark" in line for line in logs))
        self.assertTrue(any("3 dark frame(s) skipped" in line for line in logs))


class FrameUrlTests(unittest.TestCase):
    def test_go2rtc_endpoint_when_the_binary_is_present(self):
        with tempfile.NamedTemporaryFile() as fake_bin:
            os.chmod(fake_bin.name, 0o755)
            with mock.patch.dict(os.environ, {"GO2RTC_BIN": fake_bin.name}):
                data = make_data(Path("/cache"))
                self.assertEqual(timelapse_capture.frame_url(data),
                                 timelapse_capture.GO2RTC_FRAME_URL)

    def test_snapshot_fallback_without_the_binary(self):
        with mock.patch.dict(os.environ, {"GO2RTC_BIN": "/nonexistent/go2rtc"}):
            data = make_data(Path("/cache"))
            self.assertEqual(timelapse_capture.frame_url(data),
                             "http://printer.example/webcam/?action=snapshot")

    def test_rtsp_override_still_names_go2rtc(self):
        with mock.patch.dict(os.environ, {"GO2RTC_BIN": "/nonexistent/go2rtc"}):
            data = make_data(Path("/cache"), capture_source_url="rtsp://cam.example/live")
            self.assertEqual(timelapse_capture.frame_url(data),
                             timelapse_capture.GO2RTC_FRAME_URL)

    def test_no_source_no_url(self):
        data = make_data(Path("/cache"))
        data["services"]["camera_snapshot_url"] = ""
        self.assertIsNone(timelapse_capture.frame_url(data))


class FetchFrameTests(unittest.TestCase):
    def serve(self, payload, content_type="image/jpeg"):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.shutdown)
        import threading
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{server.server_port}/frame"

    def test_fetches_a_jpeg(self):
        self.assertEqual(timelapse_capture.fetch_frame(self.serve(JPEG)), JPEG)

    def test_rejects_a_non_jpeg(self):
        with self.assertRaises(ValueError):
            timelapse_capture.fetch_frame(self.serve(b"<html>not a camera</html>", "text/html"))

    def test_rejects_an_oversized_frame(self):
        with mock.patch.object(timelapse_capture, "MAX_FRAME_BYTES", 4):
            with self.assertRaises(ValueError):
                timelapse_capture.fetch_frame(self.serve(JPEG))


class BeginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "cache"
        self.env = mock.patch.dict(os.environ, {"GO2RTC_BIN": "/nonexistent/go2rtc"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_disabled_capture_begins_nothing_and_creates_nothing(self):
        data = make_data(self.cache, capture_enabled=False)
        self.assertIsNone(timelapse_capture.begin("job:1", "a.gcode", data=data))
        self.assertFalse((self.cache / "timelapse").exists())

    def test_no_source_begins_nothing(self):
        data = make_data(self.cache)
        data["services"]["camera_snapshot_url"] = ""
        self.assertIsNone(timelapse_capture.begin("job:1", "a.gcode", data=data))

    def test_begin_uses_the_settings_knobs(self):
        data = make_data(self.cache, capture_trigger="layer", capture_max_frames=3)
        session = timelapse_capture.begin("job:1", "a.gcode", data=data,
                                          fetch=lambda url: JPEG)
        self.assertIsNotNone(session)
        self.assertEqual(session.trigger.name, "layer")
        self.assertEqual(session.max_frames, 3)
        self.assertEqual(session.url, "http://printer.example/webcam/?action=snapshot")
        self.assertTrue(session.active)
        session.stop()

    def test_repeated_job_key_gets_a_fresh_directory(self):
        data = make_data(self.cache)
        first = timelapse_capture.begin("job:1", "a.gcode", data=data, fetch=lambda url: JPEG)
        first.tick()
        first.stop()
        second = timelapse_capture.begin("job:1", "a.gcode", data=data, fetch=lambda url: JPEG)
        self.assertNotEqual(first.directory, second.directory)
        self.assertTrue(second.directory.name.endswith("-2"))
        second.stop()

    def test_reprint_of_a_long_job_key_terminates_with_a_fresh_directory(self):
        # #1020: a key past the 100 character slug lost its "-2" suffix, so
        # begin() found the same populated directory forever and hung the
        # listener. Run it on a thread with a deadline so a regression fails
        # instead of hanging the suite.
        import threading
        data = make_data(self.cache)
        key = "file-3mf_print-Panel-A-Lower-V21-SELECT-COLORS_plate_1_" + "a" * 64 + ".gcode"
        self.assertGreater(len(key), 100)
        seen = []
        first = timelapse_capture.begin(key, "a.gcode", data=data, fetch=lambda url: JPEG)
        first.tick()
        first.stop()
        seen.append(first.directory)
        for _ in range(3):
            holder = {}
            worker = threading.Thread(target=lambda: holder.update(
                s=timelapse_capture.begin(key, "a.gcode", data=data, fetch=lambda url: JPEG)), daemon=True)
            worker.start()
            worker.join(5)
            self.assertFalse(worker.is_alive(), "begin() did not return: reprint search is unbounded")
            session = holder["s"]
            session.tick()
            session.stop()
            self.assertNotIn(session.directory, seen)
            self.assertLessEqual(len(session.directory.name), 100)
            seen.append(session.directory)

    def test_reprint_search_is_bounded(self):
        data = make_data(self.cache)
        root = timelapse_capture.capture_root(data)
        key = "k" * 150
        short = timelapse_capture._safe_slug(key)[:88].rstrip(".-")
        for name in [timelapse_capture._safe_slug(key)] + [f"{short}-{n}" for n in range(2, 60)]:
            frames = root / name / "frames"
            frames.mkdir(parents=True)
            (frames / "000001.jpg").write_bytes(JPEG)
        chosen = timelapse_capture.unused_job_key(root, key, "a.gcode")
        self.assertFalse(timelapse_capture._has_frames(root, chosen, "a.gcode"))

    def test_capture_settings_defaults_for_plain_dicts(self):
        knobs = timelapse_capture.capture_settings({})
        self.assertEqual(knobs["capture_trigger"], "interval")
        self.assertEqual(knobs["capture_interval_s"], 5.0)
        self.assertEqual(knobs["capture_max_frames"], 3600)
        self.assertEqual(knobs["capture_max_bytes"], 512 * 1024 * 1024)


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "cache"
        self.root = self.cache / "timelapse"
        self.root.mkdir(parents=True)
        self.data = make_data(self.cache)
        self.now = time.time()

    def write_job(self, name, manifest=None, age_days=0):
        directory = self.root / name
        (directory / "frames").mkdir(parents=True)
        (directory / "frames" / "frame-000001.jpg").write_bytes(JPEG)
        if manifest is not None:
            (directory / "capture.json").write_text(json.dumps(manifest))
        stamp = self.now - age_days * 86400
        os.utime(directory, (stamp, stamp))
        os.utime(directory / "capture.json", (stamp, stamp)) if manifest else None
        return directory

    def test_finished_past_retention_is_removed(self):
        self.write_job("old-job", {"state": "complete", "finished_at": self.now - 2 * 86400})
        self.write_job("new-job", {"state": "complete", "finished_at": self.now - 3600})
        removed = timelapse_capture.prune(self.root, self.data, now=self.now)
        self.assertEqual(removed, ["old-job"])
        self.assertTrue((self.root / "new-job").is_dir())

    def test_live_capture_is_never_touched(self):
        self.write_job("live-job", {"state": "capturing", "finished_at": None})
        removed = timelapse_capture.prune(self.root, self.data, now=self.now)
        self.assertEqual(removed, [])

    def test_abandoned_capture_ages_out_by_manifest_mtime(self):
        self.write_job("stale-job", {"state": "capturing", "finished_at": None}, age_days=3)
        removed = timelapse_capture.prune(self.root, self.data, now=self.now)
        self.assertEqual(removed, ["stale-job"])

    def test_zero_retention_removes_nothing(self):
        self.write_job("old-job", {"state": "complete", "finished_at": self.now - 30 * 86400})
        data = make_data(self.cache, local_retention_days=0)
        self.assertEqual(timelapse_capture.prune(self.root, data, now=self.now), [])


class CaptureSettingsValidationTests(unittest.TestCase):
    """The Stage 2 [timelapse] keys: defaults and refusals via settings.load."""

    def test_defaults(self):
        data = settings.load(environ={})
        timelapse = data["timelapse"]
        self.assertEqual(timelapse["capture_trigger"], "interval")
        self.assertEqual(timelapse["capture_interval_s"], 5.0)
        self.assertEqual(timelapse["capture_max_frames"], 3600)
        self.assertEqual(timelapse["capture_max_bytes"], 536870912)
        self.assertEqual(timelapse["capture_encoder"], "auto")

    def test_env_overrides(self):
        data = settings.load(environ={
            "PRINT_HANDY__TIMELAPSE__CAPTURE_TRIGGER": "both",
            "PRINT_HANDY__TIMELAPSE__CAPTURE_INTERVAL_S": "2.5",
            "PRINT_HANDY__TIMELAPSE__CAPTURE_ENCODER": "vaapi",
        })
        self.assertEqual(data["timelapse"]["capture_trigger"], "both")
        self.assertEqual(data["timelapse"]["capture_interval_s"], 2.5)
        self.assertEqual(data["timelapse"]["capture_encoder"], "vaapi")

    def test_bad_values_are_refused(self):
        for environ in (
                {"PRINT_HANDY__TIMELAPSE__CAPTURE_TRIGGER": "sometimes"},
                {"PRINT_HANDY__TIMELAPSE__CAPTURE_ENCODER": "gpu"},
                {"PRINT_HANDY__TIMELAPSE__CAPTURE_INTERVAL_S": "0"},
                {"PRINT_HANDY__TIMELAPSE__CAPTURE_INTERVAL_S": "-3"},
                {"PRINT_HANDY__TIMELAPSE__CAPTURE_MAX_FRAMES": "-1"},
                {"PRINT_HANDY__TIMELAPSE__CAPTURE_MAX_BYTES": "-1"}):
            with self.subTest(environ=environ):
                with self.assertRaises(settings.SettingsError):
                    settings.load(environ=environ)


class ListenerCaptureHookTests(unittest.IsolatedAsyncioTestCase):
    """The listener wiring: off is a no-op; on follows the print lifecycle."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "cache"
        self.env = mock.patch.dict(os.environ, {"GO2RTC_BIN": "/nonexistent/go2rtc"})
        self.env.start()
        self.addCleanup(self.env.stop)
        # tests/conftest.py installs the test settings as the _config
        # override for the whole session: restore that object, never None.
        previous = _config.cfg()
        self.addCleanup(lambda: _config.set_override(previous))

    def install_config(self, **timelapse):
        data = copy.deepcopy(_config.cfg())
        data["timelapse"].update(timelapse)
        data["paths"]["cache"] = str(self.cache)
        _config.set_override(data)
        return data

    async def drive_start(self, listener):
        listener.state, listener.filename = "standby", "benchy.gcode"
        listener.visual_watch.due = lambda *a, **k: None
        with mock.patch.object(mel, "post"), mock.patch.object(mel, "run_announcer"), \
                mock.patch.object(mel, "start_claimed", return_value=True), \
                mock.patch.object(mel, "announce_warmup_done"), \
                mock.patch.object(mel, "_claim_notification", return_value=True):
            await listener.on_status(
                {"state": "printing", "filename": "benchy.gcode",
                 "info": {"current_layer": 1}}, "event")

    async def test_disabled_capture_is_a_noop(self):
        self.install_config(capture_enabled=False)
        listener = mel.Listener()
        listener._capture_begin("job:1")
        self.assertIsNone(listener.capture_session)
        await self.drive_start(listener)
        self.assertIsNone(listener.capture_session)
        self.assertFalse((self.cache / "timelapse").exists())

    async def test_lifecycle_start_capture_complete(self):
        self.install_config(capture_enabled=True, capture_trigger="interval",
                            capture_interval_s=5.0)
        listener = mel.Listener()
        with mock.patch.object(timelapse_capture, "fetch_frame", return_value=JPEG) as fetch:
            await self.drive_start(listener)
            session = listener.capture_session
            self.assertIsNotNone(session)
            self.assertTrue(session.active)
            await listener._capture_due(1)  # first frame is due at once
            self.assertEqual(session.frame_count, 1)
            self.assertEqual(fetch.call_count, 1)
            with mock.patch.object(mel, "post"), mock.patch.object(mel, "run_announcer"), \
                    mock.patch.object(mel, "_claim_notification", return_value=True), \
                    mock.patch.object(listener, "after_print", new=mock.AsyncMock()):
                await listener.on_status({"state": "complete"}, "event")
        self.assertIsNone(listener.capture_session)
        manifest = json.loads(session.manifest_path.read_text())
        self.assertEqual(manifest["state"], "complete")
        self.assertEqual(manifest["frames"], 1)

    async def test_pause_keeps_the_session_open_but_takes_no_frames(self):
        self.install_config(capture_enabled=True)
        listener = mel.Listener()
        with mock.patch.object(timelapse_capture, "fetch_frame", return_value=JPEG):
            await self.drive_start(listener)
            session = listener.capture_session
            self.assertIsNotNone(session)
            listener.state = "paused"
            await listener._capture_due(3)  # paused: the due hook is inert
            self.assertEqual(session.frame_count, 0)
            self.assertIs(listener.capture_session, session)  # still the same session
            listener.state = "printing"
            await listener._capture_due(3)  # resumed: the same session captures again
            self.assertEqual(session.frame_count, 1)
            listener._capture_end("complete")

    async def test_capture_is_closed_before_the_announcer_looks_for_it(self):
        # The completion's timelapse postprocess (find_capture) skips a
        # "capturing" manifest: run first, it found no video for a first
        # print and the previous job's video for a reprint.
        from print_handy.hermes import timelapse_encode
        self.install_config(capture_enabled=True)
        listener = mel.Listener()
        found = []
        with mock.patch.object(timelapse_capture, "fetch_frame", return_value=JPEG):
            await self.drive_start(listener)
            session = listener.capture_session
            await listener._capture_due(1)
            with mock.patch.object(mel, "post"), \
                    mock.patch.object(mel, "run_announcer",
                                      side_effect=lambda reason: found.append(
                                          timelapse_encode.find_capture("/gcodes/benchy.gcode"))), \
                    mock.patch.object(mel, "_claim_notification", return_value=True), \
                    mock.patch.object(listener, "after_print", new=mock.AsyncMock()):
                await listener.on_status({"state": "complete"}, "event")
        self.assertEqual(found, [session.directory])

    async def test_klippy_shutdown_runs_the_end_of_print_cleanup(self):
        self.install_config(capture_enabled=True)
        listener = mel.Listener()

        class Socket:
            def __init__(self, messages):
                self.messages = [json.dumps(m) for m in messages]

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def send(self, data):
                pass

            async def recv(self):
                if not self.messages:
                    raise ConnectionError("script done")
                return self.messages.pop(0)

        with mock.patch.object(timelapse_capture, "fetch_frame", return_value=JPEG):
            await self.drive_start(listener)
            session = listener.capture_session
            await listener._capture_due(1)
            for name in ("poll_native_presence", "poll_spoolman_new_spools", "nag_if_due",
                         "light_idle_tick", "after_print", "_retire_paused_prompt"):
                setattr(listener, name, mock.AsyncMock())
            listener.maintenance_last_check = time.monotonic()
            with mock.patch.object(mel.websockets, "connect",
                                   return_value=Socket([{"method": "notify_klippy_shutdown"}])), \
                    mock.patch.object(mel, "post_shutdown_alert") as alert:
                with self.assertRaises(ConnectionError):
                    await listener.session()
        alert.assert_called_once_with("benchy.gcode")
        self.assertEqual(listener.state, "error")
        self.assertIsNone(listener.capture_session)
        self.assertEqual(json.loads(session.manifest_path.read_text())["state"], "error")
        listener.after_print.assert_awaited_once_with("error")
        listener._retire_paused_prompt.assert_awaited_once_with("error")

    async def test_a_fresh_start_resets_the_shutdown_alert_dedup(self):
        # P2-57: the firmware-stop alert dedups per print ATTEMPT. A first
        # attempt's alert must not suppress the alert for a reprint of the
        # same file: the marker resets when a genuinely new print begins.
        self.install_config(capture_enabled=True)
        listener = mel.Listener()
        listener.shutdown_alerted = "benchy.gcode"  # attempt 1 already alerted
        with mock.patch.object(timelapse_capture, "fetch_frame", return_value=JPEG):
            await self.drive_start(listener)  # attempt 2 of the same file begins
        self.assertIsNone(listener.shutdown_alerted)

    async def test_capture_is_scheduled_single_flight_off_the_receive_loop(self):
        # P2-133: scheduling a capture returns at once (the fetch runs in a
        # task, not inline in the websocket receive path), and a second
        # schedule while a fetch is in flight starts no second fetch.
        import asyncio as _asyncio
        self.install_config(capture_enabled=True)
        listener = mel.Listener()
        with mock.patch.object(timelapse_capture, "fetch_frame", return_value=JPEG):
            await self.drive_start(listener)
            session = listener.capture_session
            ticks = []
            gate = _asyncio.Event()

            async def blocking_tick(layer):
                ticks.append(layer)
                await gate.wait()

            listener._capture_due = blocking_tick
            listener._schedule_capture(1)
            listener._schedule_capture(2)  # in flight already: no second task
            self.assertEqual(ticks, [])  # nothing ran inline in the caller
            await _asyncio.sleep(0)
            self.assertEqual(ticks, [1])
            gate.set()
            await listener._capture_task
            listener._schedule_capture(3)  # free again: a new task may run
            await listener._capture_task
            self.assertEqual(ticks, [1, 3])
            listener._capture_end("complete")

    async def test_interval_timer_captures_without_status_events(self):
        """#906: the interval trigger beats on its own clock, not only on layer events."""
        self.install_config(capture_enabled=True, capture_trigger="interval",
                            capture_interval_s=5.0)
        listener = mel.Listener()
        slept = []
        with mock.patch.object(timelapse_capture, "fetch_frame", return_value=JPEG):
            await self.drive_start(listener)
            session = listener.capture_session
            clock = [1000.0]
            session._now = lambda: clock[0]

            async def fake_sleep(seconds):
                slept.append(seconds)
                clock[0] += seconds
                task = listener._capture_task
                if task is not None:
                    await task
                if len(slept) >= 3:
                    raise asyncio.CancelledError

            with self.assertRaises(asyncio.CancelledError):
                await listener._capture_timer(sleep=fake_sleep)
            await listener._capture_task
            self.assertEqual(slept, [5.0, 5.0, 5.0])
            self.assertEqual(session.frame_count, 2)  # no status event ever arrived
            listener.state = "paused"
            self.assertFalse(listener._capture_timer_tick())
            listener._capture_end("complete")

    async def test_a_new_print_replaces_a_capture_a_missed_end_left_open(self):
        self.install_config(capture_enabled=True)
        listener = mel.Listener()
        with mock.patch.object(timelapse_capture, "fetch_frame", return_value=JPEG):
            await self.drive_start(listener)
            stale = listener.capture_session
            await listener._capture_due(1)
            listener.state = "error"  # the print died without an end transition
            with mock.patch.object(mel, "post"), mock.patch.object(mel, "run_announcer"), \
                    mock.patch.object(mel, "start_claimed", return_value=False), \
                    mock.patch.object(mel, "_claim_notification", return_value=True):
                await listener.on_status({"state": "printing", "filename": "benchy.gcode"}, "event")
            fresh = listener.capture_session
            self.assertIsNotNone(fresh)
            self.assertIsNot(fresh, stale)
            self.assertEqual(json.loads(stale.manifest_path.read_text())["state"], "superseded")
            listener._capture_end("complete")

    async def test_listener_restart_mid_claimed_print_still_captures(self):
        # The restarted listener re-sees the start a Discord print already
        # claimed: only the start post is a duplicate.
        self.install_config(capture_enabled=True)
        state = Path(self.tmp.name) / "notifications.json"
        with mock.patch.object(mel, "NOTIFICATION_STATE", state), \
                mock.patch.object(mel, "start_claim_time", return_value=1000.0):
            self.assertTrue(mel._claim_notification("file:benchy.gcode|start:1000.0", "start"))
            listener = mel.Listener()  # fresh process: state None
            listener.light_print_started = mock.AsyncMock()
            with mock.patch.object(timelapse_capture, "fetch_frame", return_value=JPEG), \
                    mock.patch.object(mel, "post") as post, \
                    mock.patch.object(mel, "announce_warmup_done") as warmup, \
                    mock.patch.object(mel, "run_announcer"):
                await listener.on_status({"state": "printing", "filename": "benchy.gcode"}, "subscribe")
                self.assertEqual(listener.state, "printing")
                self.assertIsNotNone(listener.capture_session)
                listener.light_print_started.assert_awaited_once()
                post.assert_not_called()
                warmup.assert_not_called()
                listener._capture_end("complete")

    async def test_a_timed_out_capture_begin_never_installs_the_late_session(self):
        # #1020 review: cancelling the to_thread await does not stop the
        # thread. Its late result must be closed, never installed over
        # whatever the listener has moved on to.
        self.install_config(capture_enabled=True)
        listener = mel.Listener()
        release = threading.Event()
        stopped = []

        class LateSession:
            def stop(self, state="complete"):
                stopped.append(state)

        def slow_begin(*args, **kwargs):
            release.wait(10)
            return LateSession()

        with mock.patch.object(mel, "CAPTURE_BEGIN_TIMEOUT_S", 0.2), \
                mock.patch.object(timelapse_capture, "begin", side_effect=slow_begin):
            await self.drive_start(listener)
            self.assertIsNone(listener.capture_session)
            release.set()
            await asyncio.sleep(0.5)  # let the thread land and be disposed of
        self.assertIsNone(listener.capture_session)
        self.assertEqual(stopped, ["superseded"])

    async def test_a_capture_begin_finishing_after_the_print_ended_is_closed_not_installed(self):
        # #1020 review: a begin that returns after its print ended (or a
        # newer print took over) belongs to a stale request: close the
        # session instead of installing it.
        self.install_config(capture_enabled=True)
        listener = mel.Listener()
        listener.state, listener.filename = "printing", "benchy.gcode"
        release = threading.Event()
        stopped = []

        class LateSession:
            def stop(self, state="complete"):
                stopped.append(state)

        def slow_begin(*args, **kwargs):
            release.wait(10)
            return LateSession()

        with mock.patch.object(timelapse_capture, "begin", side_effect=slow_begin):
            begin_task = asyncio.create_task(listener._begin_capture("job:1"))
            await asyncio.sleep(0.2)  # the worker thread is inside begin()
            listener._capture_end("complete")  # the print ended meanwhile
            listener.state = "complete"
            release.set()
            await begin_task
        self.assertIsNone(listener.capture_session)
        self.assertEqual(stopped, ["superseded"])


def _mjpeg_stream(*frames):
    """A recorded multipart MJPEG body: one part per frame, then the closing boundary."""
    parts = []
    for frame in frames:
        parts.append(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
    return b"".join(parts) + b"--frame--\r\n"


class FetchMjpegFrameTests(unittest.TestCase):
    """#373: stream-only camera sources (the stock Flashforge cameraStreamUrl)."""

    def serve(self, payload, content_type="multipart/x-mixed-replace; boundary=frame"):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.shutdown)
        import threading
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{server.server_port}/?action=stream"

    def test_returns_the_first_complete_frame(self):
        url = self.serve(_mjpeg_stream(JPEG, JPEG))
        self.assertEqual(timelapse_capture.fetch_mjpeg_frame(url), JPEG)

    def test_a_stream_with_no_frame_raises(self):
        with self.assertRaises(ValueError):
            timelapse_capture.fetch_mjpeg_frame(self.serve(b"<html>not a camera</html>", "text/html"))

    def test_an_oversized_frame_raises(self):
        big = b"\xff\xd8" + b"x" * 64 + b"\xff\xd9"
        with mock.patch.object(timelapse_capture, "MAX_FRAME_BYTES", 8):
            with self.assertRaises(ValueError):
                timelapse_capture.fetch_mjpeg_frame(self.serve(_mjpeg_stream(big)))


class BeginExplicitUrlTests(unittest.TestCase):
    """#373: a caller with a per-printer camera source passes it explicitly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "cache"

    def test_explicit_url_skips_the_settings_source(self):
        data = make_data(self.cache)
        data["services"]["camera_snapshot_url"] = ""  # no settings source at all
        session = timelapse_capture.begin(
            "job:9", "a.gcode", data=data,
            url="http://192.0.2.10:8080/?action=stream", fetch=lambda url: JPEG)
        self.assertIsNotNone(session)
        self.assertEqual(session.url, "http://192.0.2.10:8080/?action=stream")
        session.stop()

    def test_explicit_url_still_honours_the_capture_gate(self):
        data = make_data(self.cache, capture_enabled=False)
        self.assertIsNone(timelapse_capture.begin(
            "job:9", "a.gcode", data=data,
            url="http://192.0.2.10:8080/?action=stream", fetch=lambda url: JPEG))
        self.assertFalse((self.cache / "timelapse").exists())

    def test_mode_off_and_camera_none_begin_nothing(self):
        # 2.2.2 wiring: [timelapse] mode and [services] camera_source
        # were dead options at this seam — mode=off stopped nothing and
        # camera_source=none still captured. Both gates now apply,
        # including to an explicit per-printer URL.
        data = make_data(self.cache, mode="off")
        self.assertIsNone(timelapse_capture.begin(
            "job:9", "a.gcode", data=data,
            url="http://192.0.2.10:8080/?action=stream", fetch=lambda url: JPEG))
        data = make_data(self.cache)
        data["services"]["camera_source"] = "none"
        self.assertIsNone(timelapse_capture.begin(
            "job:9", "a.gcode", data=data,
            url="http://192.0.2.10:8080/?action=stream", fetch=lambda url: JPEG))
        self.assertFalse((self.cache / "timelapse").exists())

    def test_mode_container_begins_without_the_capture_switch(self):
        data = make_data(self.cache, capture_enabled=False, mode="container")
        session = timelapse_capture.begin("job:9", "a.gcode", data=data,
                                          fetch=lambda url: JPEG)
        self.assertIsNotNone(session)
        session.stop()


if __name__ == "__main__":
    unittest.main()
