"""Elegoo SDCP launched-print monitor (#295), offline throughout.

The monitor's status/camera/post/encode functions are injected fakes;
the start_monitor tests drive raw SDCP status answers through the real
adapter's normalisation, and the full-lifecycle test drives a real
capture session fed by JPEG stills through an injected camera source
(the seam a verified SDCP camera will use — none is verified today,
so production wiring resolves to no source) and encodes with the real
ffmpeg (skipped where no ffmpeg/ffprobe exists). No socket, no
printer, no chat is ever touched.
"""
import io
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from print_handy.runtime import elegoo_watch, moonraker_event_listener as mel
from print_handy.runtime import timelapse_capture
from print_handy.printers.elegoo_sdcp import camera as elegoo_camera

REMOTE = "benchy.gcode"
STILL_URL = "sdcp-camera://unverified-fixture"

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def jpeg_bytes(colour):
    out = io.BytesIO()
    Image.new("RGB", (320, 240), colour).save(out, format="JPEG")
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
        self.tick_layers = []
        self.stopped = None
        self.filename = REMOTE
        self.frames_dir = Path("/unused/frames")
        self.directory = Path("/unused")

    def tick(self, layer=None):
        self.ticks += 1
        self.tick_layers.append(layer)
        return True

    def stop(self, state="complete"):
        self.stopped = state
        return {"state": state, "frames": self.frames}

    @property
    def frame_count(self):
        return self.frames


class Rig:
    """A monitor wired to recorders; every collaborator is a fake."""

    def __init__(self, statuses, *, camera_source=(STILL_URL, "fetch"), session="default",
                 encode=None):
        self.posts = []
        self.begins = []
        self.encodes = []
        self.session = FakeSession() if session == "default" else session
        self.clock = FakeClock()
        self.monitor = elegoo_watch.ElegooPrintMonitor(
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
    monkeypatch.setattr(elegoo_watch, "log", lambda line: rigs.append(str(line))); monkeypatch.setattr(elegoo_watch, "warn", lambda line: rigs.append(str(line)))
    return rigs


# -- the happy path -------------------------------------------------------------


def test_completion_posts_the_timelapse_through_the_media_marker(capture_logs):
    rig = Rig([st("printing"), st("printing"), st("complete")])
    assert rig.monitor.run() == "complete"
    assert rig.begins == [(STILL_URL, "fetch")]
    assert rig.session.stopped == "complete"
    assert len(rig.encodes) == 1
    assert len(rig.posts) == 1
    assert "MEDIA:/tmp/timelapse.mp4" in rig.posts[0]


def test_capture_ticks_carry_the_reported_layer(capture_logs):
    # The SDCP adapter's normalized status supplies the current layer;
    # capture_trigger=layer can only fire when the session's ticks carry
    # it (the same defect as the Bambu monitor's, P2-172).
    def stl(state, layer):
        return {"state": state, "filename": REMOTE, "progress": 0.5, "layer": layer}

    rig = Rig([stl("printing", 3), stl("printing", 4), stl("complete", 4)])
    assert rig.monitor.run() == "complete"
    assert rig.session.ticks > 0
    assert all(layer is not None for layer in rig.session.tick_layers)
    assert 3 in rig.session.tick_layers and 4 in rig.session.tick_layers


def test_standby_after_printing_is_a_completion(capture_logs):
    # SDCP returns the printer to standby when a print ends or is
    # stopped at the panel: after an active print, standby is the
    # terminal transition (the adapter maps codes 0/7 to standby).
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


def test_no_camera_source_posts_text_only_with_a_diagnostic(capture_logs):
    # Today's production posture: the SDCP camera seam resolves to no
    # source, so the completion post goes out text-only and the log
    # names the unverified camera as the reason — never a silent skip.
    rig = Rig([st("printing"), st("complete")], camera_source=("", None))
    assert rig.monitor.run() == "complete"
    assert rig.begins == []
    assert len(rig.posts) == 1 and "MEDIA:" not in rig.posts[0]
    assert any("no verified SDCP camera source" in line for line in capture_logs)


def test_camera_read_failure_posts_text_only(capture_logs):
    rig = Rig([st("printing"), st("complete")])
    rig.monitor._camera_source_fn = scripted([RuntimeError("camera read refused")])
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
    monkeypatch.setattr(elegoo_watch, "_too_big_text", lambda: "RECORDED BUT TOO LARGE")
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


def _status_answer(code, filename=REMOTE):
    """One raw SDCP Cmd 0 answer, the shape client.status() returns."""
    return {"Status": {"CurrentStatus": [code], "TempOfNozzle": 200.0, "TempOfHotbed": 60.0},
            "PrintInfo": {"Filename": f"/local/{filename}", "CurrentLayer": 4, "TotalLayer": 9}}


class FakeClient:
    def __init__(self, answers):
        self._answers = scripted(answers)

    def status(self):
        return self._answers()


class FakeAdapter:
    def __init__(self, answers):
        self.config = {"elegoo_sdcp": {"host": "192.0.2.30"}}
        self._client = FakeClient(answers)

    def client(self):
        return self._client

    def normalize_status(self, raw):
        from print_handy.printers.elegoo_sdcp.adapter import ElegooSdcpAdapter
        return ElegooSdcpAdapter.normalize_status(object.__new__(ElegooSdcpAdapter), raw)


class _Thread:
    def __init__(self, target):
        self._target = target

    def start(self):
        self._target()


def _run_inline(adapter, posts):
    targets = []

    def factory(target):
        targets.append(target)
        return _Thread(target)

    thread = elegoo_watch.start_monitor(adapter, REMOTE, post=posts.append,
                                        thread_factory=factory)
    assert thread is not None and len(targets) == 1
    return thread


def test_start_monitor_wires_the_adapter_and_starts_one_thread(capture_logs):
    # The print already finished by the first poll (SDCP code 8
    # normalises to complete): the monitor posts the completion for
    # the matching filename without ever sleeping.
    adapter = FakeAdapter([_status_answer(8)])
    posts = []
    _run_inline(adapter, posts)
    assert len(posts) == 1 and "MEDIA:" not in posts[0]


def test_start_monitor_default_posture_is_the_completion_post_only(capture_logs):
    # The real wiring end to end: printing (code 2) then complete
    # (code 8), normalised by the real adapter — and the camera seam's
    # honest answer (no verified source) means no capture is begun and
    # the completion post carries no video, with the reason logged.
    adapter = FakeAdapter([_status_answer(2), _status_answer(8)])
    posts = []
    _run_inline(adapter, posts)
    assert len(posts) == 1 and "MEDIA:" not in posts[0]
    assert any("no verified SDCP camera source" in line for line in capture_logs)


def test_start_monitor_unconfigured_adapter_starts_nothing():
    class UnconfiguredAdapter(FakeAdapter):
        def client(self):
            from print_handy.printers.elegoo_sdcp.client import SdcpError
            raise SdcpError("SDCP is not configured")

    assert elegoo_watch.start_monitor(UnconfiguredAdapter([]), REMOTE, post=lambda _m: None) is None


# -- the full lifecycle: real session, real JPEG stills, real ffmpeg -------------------


@pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="needs ffmpeg and ffprobe on PATH")
def test_full_lifecycle_produces_a_valid_h264_mp4_under_the_limit(tmp_path, monkeypatch):
    # A verified SDCP camera source, when one exists, hands the seam
    # JPEG frames (the #246 pipeline's frame form); this drives that
    # contract end to end: real capture session, real ffmpeg encode.
    clock = FakeClock()
    root = tmp_path / "cache" / "timelapse"
    stills = scripted([jpeg_bytes(colour) for colour in ("red", "green", "blue")])

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
    monitor = elegoo_watch.ElegooPrintMonitor(
        REMOTE,
        status_fn=scripted([st("printing"), st("printing"), st("printing"),
                            st("printing"), st("complete")]),
        camera_source_fn=lambda: (STILL_URL, lambda _url: stills()),
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
