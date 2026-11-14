"""PrusaLink launched-print monitor (#295), offline throughout.

The monitor's status/camera/post/encode functions are injected fakes;
the full-lifecycle test drives a real capture session fed by real PNG
stills transcoded through printers.prusa.camera and encodes with the
real ffmpeg (skipped where no ffmpeg/ffprobe exists). No socket, no
printer, no chat is ever touched.
"""
import io
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from print_handy.runtime import moonraker_event_listener as mel
from print_handy.runtime import prusa_watch, timelapse_capture
from print_handy.printers.prusa import camera as prusa_camera

REMOTE = "benchy.gcode"
SNAP_URL = "http://192.0.2.20/api/v1/cameras/usb-cam/snap"

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def png_bytes(colour):
    out = io.BytesIO()
    Image.new("RGB", (320, 240), colour).save(out, format="PNG")
    return out.getvalue()


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def scripted(values):
    items = list(values)

    def next_value():
        item = items.pop(0) if len(items) > 1 else items[0]
        if isinstance(item, Exception):
            raise item
        return item

    return next_value


def st(state, filename=REMOTE):
    return {"state": state, "filename": filename, "progress": 0.5}


class FakeSession:
    def __init__(self, frames=3, failures=0):
        self.frames = frames
        self.failures = failures
        self.ticks = 0
        self.stopped = None
        self.filename = REMOTE
        self.frames_dir = Path("/unused/frames")
        self.directory = Path("/unused")

    def tick(self):
        self.ticks += 1
        return True

    def stop(self, state="complete"):
        self.stopped = state
        return {"state": state, "frames": self.frames}

    @property
    def frame_count(self):
        return self.frames


class Rig:
    """A monitor wired to recorders; every collaborator is a fake."""

    def __init__(self, statuses, *, camera_source=(SNAP_URL, "fetch"), session="default",
                 encode=None):
        self.posts = []
        self.begins = []
        self.encodes = []
        self.session = FakeSession() if session == "default" else session
        self.clock = FakeClock()
        self.monitor = prusa_watch.PrusaPrintMonitor(
            REMOTE,
            status_fn=scripted(statuses),
            camera_source_fn=scripted([camera_source]),
            post=self.posts.append,
            begin_fn=self._begin,
            encode_fn=encode or self._encode,
            clock=self.clock, sleep=self.clock.sleep,
            poll_s=30.0, capture_poll_s=0.5)

    def _begin(self, url, fetch):
        self.begins.append((url, fetch))
        return self.session

    def _encode(self, session):
        self.encodes.append(session)
        return Path("/tmp/timelapse.mp4")


@pytest.fixture(autouse=True)
def capture_logs(monkeypatch):
    rigs = []
    monkeypatch.setattr(prusa_watch, "log", lambda line: rigs.append(str(line))); monkeypatch.setattr(prusa_watch, "warn", lambda line: rigs.append(str(line)))
    return rigs


# -- the happy path -------------------------------------------------------------


def test_completion_posts_the_timelapse_through_the_media_marker(capture_logs):
    rig = Rig([st("printing"), st("printing"), st("complete")])
    assert rig.monitor.run() == "complete"
    assert rig.begins == [(SNAP_URL, "fetch")]
    assert rig.session.stopped == "complete"
    assert len(rig.encodes) == 1
    assert len(rig.posts) == 1
    assert "MEDIA:/tmp/timelapse.mp4" in rig.posts[0]


def test_standby_after_printing_is_a_completion(capture_logs):
    rig = Rig([st("printing"), st("standby")])
    assert rig.monitor.run() == "complete"
    assert len(rig.posts) == 1 and "MEDIA:" in rig.posts[0]


def test_errored_print_posts_once(capture_logs):
    rig = Rig([st("printing"), st("paused"), st("error")])
    assert rig.monitor.run() == "error"
    assert rig.session.stopped == "error"
    assert len(rig.posts) == 1


def test_completion_without_ever_printing_still_posts(capture_logs):
    # The print finished between the start confirmation and the first
    # poll: the terminal state names this monitor's file, so the
    # completion post goes out (with no capture — nothing was active).
    rig = Rig([st("complete")])
    assert rig.monitor.run() == "complete"
    assert rig.begins == []
    assert len(rig.posts) == 1 and "MEDIA:" not in rig.posts[0]


# -- dedupe ------------------------------------------------------------------------


def test_duplicate_polls_flaps_and_reconnects_never_duplicate(capture_logs):
    rig = Rig([st("printing"), RuntimeError("read failed"), st("printing"),
               st("paused"), st("printing"), st("complete"), st("complete")])
    assert rig.monitor.run() == "complete"
    assert len(rig.begins) == 1                 # one session, opened once
    assert len(rig.posts) == 1                  # one completion post


def test_a_different_print_taking_over_posts_nothing(capture_logs):
    rig = Rig([st("printing"), st("printing", "other.gcode"), st("complete", "other.gcode")])
    assert rig.monitor.run() is None
    assert rig.posts == []
    assert rig.session.stopped == "superseded"


def test_a_terminal_state_for_another_print_is_ignored(capture_logs):
    rig = Rig([st("complete", "other.gcode")])
    assert rig.monitor.run() is None
    assert rig.posts == [] and rig.begins == []


# -- the fail-soft paths ----------------------------------------------------------


def test_no_camera_registered_posts_text_only_with_a_diagnostic(capture_logs):
    rig = Rig([st("printing"), st("complete")], camera_source=("", None))
    assert rig.monitor.run() == "complete"
    assert rig.begins == []
    assert len(rig.posts) == 1 and "MEDIA:" not in rig.posts[0]
    assert any("no camera" in line for line in capture_logs)


def test_camera_read_failure_posts_text_only(capture_logs):
    rig = Rig([st("printing"), st("complete")])
    rig.monitor._camera_source_fn = scripted([RuntimeError("cameras refused")])
    assert rig.monitor.run() == "complete"
    assert rig.begins == []
    assert len(rig.posts) == 1 and "MEDIA:" not in rig.posts[0]
    assert any("could not be read" in line for line in capture_logs)


def test_capture_unavailable_posts_text_only_and_is_attempted_once(capture_logs):
    rig = Rig([st("printing"), st("printing"), st("complete")], session=None)
    assert rig.monitor.run() == "complete"
    assert len(rig.begins) == 1                 # attempted once, never retried per poll
    assert len(rig.posts) == 1 and "MEDIA:" not in rig.posts[0]
    assert any("capture unavailable" in line for line in capture_logs)


def test_encode_failure_posts_the_completion_text_and_logs(capture_logs):
    def encode(session):
        raise RuntimeError("ffmpeg exploded")

    rig = Rig([st("printing"), st("complete")], encode=encode)
    assert rig.monitor.run() == "complete"
    assert len(rig.posts) == 1 and "MEDIA:" not in rig.posts[0]
    assert any("encode failed" in line for line in capture_logs)


def test_zero_frames_posts_text_only_and_logs_the_failures(capture_logs):
    rig = Rig([st("printing"), st("complete")], session=FakeSession(frames=0, failures=4))
    assert rig.monitor.run() == "complete"
    assert rig.encodes == []
    assert len(rig.posts) == 1 and "MEDIA:" not in rig.posts[0]
    assert any("no frames" in line and "4 failed fetch" in line for line in capture_logs)


def test_oversize_video_posts_the_recorded_but_too_large_line(capture_logs, monkeypatch):
    monkeypatch.setattr(prusa_watch, "_too_big_text", lambda: "RECORDED BUT TOO LARGE")
    rig = Rig([st("printing"), st("complete")], encode=lambda session: None)
    assert rig.monitor.run() == "complete"
    assert len(rig.posts) == 1
    assert "MEDIA:" not in rig.posts[0]
    assert rig.posts[0].endswith("RECORDED BUT TOO LARGE")


def test_media_post_failure_falls_back_to_the_text_completion(capture_logs):
    """#391 contract: the platform refusing the MEDIA post must not lose
    the completion — the monitor posts the text alone and logs why."""
    rig = Rig([st("printing"), st("complete")])
    posts = []

    def flaky_post(message):
        posts.append(message)
        if "MEDIA:" in message:
            raise RuntimeError("platform refused the attachment")

    rig.monitor._post = flaky_post
    assert rig.monitor.run() == "complete"
    assert len(posts) == 2
    assert "MEDIA:" in posts[0]
    assert "MEDIA:" not in posts[1]
    assert any("post with timelapse failed" in line for line in capture_logs)


# -- start_monitor wiring -------------------------------------------------------------


class FakeClient:
    def __init__(self, snapshots, cameras=None):
        self.base = "http://192.0.2.20"
        self._snapshots = scripted(snapshots)
        self._cameras = cameras or []

    def snapshot(self):
        return self._snapshots()

    def cameras(self):
        return self._cameras

    def camera_snapshot(self, camera_id=None):
        return png_bytes("blue")


class FakeAdapter:
    def __init__(self, snapshots, cameras=None):
        self.config = {"prusa": {"link_host": "192.0.2.20"}}
        self._client = FakeClient(snapshots, cameras)

    def client(self):
        return self._client

    def normalize_status(self, raw):
        from print_handy.printers.prusa.adapter import PrusaAdapter
        return PrusaAdapter.normalize_status(object.__new__(PrusaAdapter), raw)


def _snapshot(state, filename=REMOTE, progress=100.0):
    return {"info": {}, "status": {"printer": {"state": state}, "job": {}},
            "job": {"id": 7, "progress": progress,
                    "file": {"name": filename, "display_name": filename}}}


def test_start_monitor_wires_the_adapter_and_starts_one_thread():
    # The print already finished by the first poll (PrusaLink FINISHED
    # normalises to complete): the monitor posts the completion for the
    # matching filename without ever sleeping.
    adapter = FakeAdapter([_snapshot("FINISHED")])
    posts, targets = [], []

    class _Thread:
        def __init__(self, target):
            self._target = target

        def start(self):
            self._target()

    def factory(target):
        targets.append(target)
        return _Thread(target)

    thread = prusa_watch.start_monitor(adapter, REMOTE, post=posts.append,
                                       thread_factory=factory)
    assert thread is not None and len(targets) == 1
    assert len(posts) == 1 and "MEDIA:" not in posts[0]


def test_start_monitor_camera_source_uses_the_registered_camera():
    # The composition start_monitor wires: the registered camera's id,
    # its snapshot URL, and a fetch that returns the still as JPEG.
    client = FakeClient([_snapshot("PRINTING")],
                        cameras=[{"camera_id": "usb-cam", "connected": True}])
    camera_id = prusa_camera.default_camera_id(client)
    assert camera_id == "usb-cam"
    assert prusa_camera.snapshot_url(client, camera_id) == SNAP_URL
    fetch = lambda _url: prusa_camera.snapshot_jpeg(client, camera_id)
    assert fetch(SNAP_URL).startswith(b"\xff\xd8")  # the PNG still arrived as JPEG


# -- the full lifecycle: real session, real PNG stills, real ffmpeg -------------------


@pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="needs ffmpeg and ffprobe on PATH")
def test_full_lifecycle_produces_a_valid_h264_mp4_under_the_limit(tmp_path, monkeypatch):
    # Three recorded PNG stills, generated once with Pillow (the only
    # form PrusaLink serves); the fetch transcodes each through the
    # real printers.prusa.camera seam, exactly as start_monitor wires it.
    client = FakeClient([_snapshot("PRINTING")])
    stills = scripted([png_bytes(colour) for colour in ("red", "green", "blue")])
    client.camera_snapshot = lambda camera_id=None: stills()

    clock = FakeClock()
    root = tmp_path / "cache" / "timelapse"

    def begin_fn(url, fetch):
        session = timelapse_capture.CaptureSession(
            root, REMOTE, REMOTE,
            trigger=timelapse_capture.make_trigger("interval", 0.5),
            max_frames=100, max_bytes=0, url=url,
            fetch=fetch, log=lambda _line: None, now=clock)
        assert session.start()
        return session

    monkeypatch.setattr(mel, "_media_upload_limit", lambda: 500_000)
    posts = []
    monitor = prusa_watch.PrusaPrintMonitor(
        REMOTE,
        status_fn=scripted([st("printing"), st("printing"), st("printing"),
                            st("printing"), st("complete")]),
        camera_source_fn=lambda: (SNAP_URL, lambda _url: prusa_camera.snapshot_jpeg(client, "usb-cam")),
        post=posts.append,
        begin_fn=begin_fn,
        clock=clock, sleep=clock.sleep, poll_s=30.0, capture_poll_s=0.5)
    assert monitor.run() == "complete"
    assert len(posts) == 1 and "MEDIA:" in posts[0]
    video = Path(posts[0].split("MEDIA:", 1)[1].strip())
    assert video.is_file() and 0 < video.stat().st_size <= 500_000
    probe = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                            str(video)], capture_output=True, text=True, check=True)
    assert probe.stdout.strip() == "h264"
    session_dir = next(root.iterdir())
    frames = list((session_dir / "frames").glob("frame-*.jpg"))
    assert len(frames) >= 2
    assert frames[0].read_bytes().startswith(b"\xff\xd8")  # stored frames are JPEG


def test_a_refused_media_post_falls_back_to_the_text_completion(capture_logs):
    """P2-169: the production sender returns False on a refusal instead of
    raising. A refused MEDIA post must not be reported as delivered —
    the completion still reaches the chat as text."""
    rig = Rig([st("printing"), st("complete")])
    attempts = []

    def post(message):
        attempts.append(message)
        return "MEDIA:" not in message

    rig.monitor._post = post
    assert rig.monitor.run() == "complete"
    assert len(attempts) == 2
    assert "MEDIA:" in attempts[0]
    assert "MEDIA:" not in attempts[1]


# -- layer trigger fallback (stock_watch's P2-140 twin) -------------------------


def test_a_layer_trigger_falls_back_to_interval_on_prusa(monkeypatch):
    """2.2.5 review: Prusa status carries no layer, so a layer-triggered
    session could never fire. The watch substitutes the configured interval."""
    import types

    session = types.SimpleNamespace(trigger=timelapse_capture.LayerTrigger())
    monkeypatch.setattr(timelapse_capture, "begin", lambda *a, **k: session)
    monkeypatch.setattr(timelapse_capture, "capture_settings",
                        lambda data=None: {"capture_interval_s": 45})
    rig = Rig([st("printing")])
    assert rig.monitor._begin_default(SNAP_URL, "fetch") is session
    assert isinstance(session.trigger, timelapse_capture.IntervalTrigger)
    assert session.trigger.interval_s == 45


def test_an_interval_trigger_is_left_alone_on_prusa(monkeypatch):
    import types

    trigger = timelapse_capture.IntervalTrigger(30)
    session = types.SimpleNamespace(trigger=trigger)
    monkeypatch.setattr(timelapse_capture, "begin", lambda *a, **k: session)
    monkeypatch.setattr(timelapse_capture, "capture_settings",
                        lambda data=None: {"capture_interval_s": 45})
    rig = Rig([st("printing")])
    assert rig.monitor._begin_default(SNAP_URL, "fetch") is session
    assert session.trigger is trigger
