"""Stock Flashforge launched-print monitor (#373), offline throughout.

The monitor's status/camera/stream/post/encode functions are injected
fakes; the full-lifecycle test drives a real capture session fed by
recorded JPEG frames and encodes with the real ffmpeg (skipped where no
ffmpeg/ffprobe exists). No socket, no printer, no chat is ever touched.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from print_handy.hermes import moonraker_event_listener as mel
from print_handy.hermes import stock_watch, timelapse_capture

REMOTE = "bag_clip.gcode"
STREAM_URL = "http://192.0.2.10:8080/?action=stream"
JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


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
    def __init__(self, frames=3, failures=0, dark_frames=0):
        self.frames = frames
        self.failures = failures
        self.dark_frames = dark_frames
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

    def __init__(self, statuses, *, camera_url=STREAM_URL, session=None,
                 encode=None, stream_error=None):
        self.posts = []
        self.begins = []
        self.streams = []
        self.encodes = []
        self.logs = []
        self.session = session if session is not None else FakeSession()
        self.clock = FakeClock()
        self.monitor = stock_watch.StockPrintMonitor(
            REMOTE,
            status_fn=scripted(statuses),
            camera_url_fn=scripted([camera_url]),
            stream_fn=self._stream(stream_error),
            post=self.posts.append,
            begin_fn=self._begin,
            encode_fn=encode or self._encode,
            clock=self.clock, sleep=self.clock.sleep,
            poll_s=30.0, capture_poll_s=0.5)

    def _stream(self, error):
        def stream_fn(action):
            self.streams.append(action)
            if error is not None:
                raise error
        return stream_fn

    def _begin(self, url):
        self.begins.append(url)
        return self.session

    def _encode(self, session):
        self.encodes.append(session)
        return Path("/tmp/timelapse.mp4")


@pytest.fixture(autouse=True)
def capture_logs(monkeypatch):
    rigs = []
    monkeypatch.setattr(stock_watch, "log", lambda line: rigs.append(str(line)))
    return rigs


# -- the happy path -------------------------------------------------------------


def test_completion_posts_the_timelapse_through_the_media_marker(capture_logs):
    rig = Rig([st("printing"), st("printing"), st("complete")])
    assert rig.monitor.run() == "complete"
    assert rig.begins == [STREAM_URL]
    assert rig.streams == ["open", "close"]
    assert rig.session.stopped == "complete"
    assert len(rig.encodes) == 1
    assert len(rig.posts) == 1
    assert "MEDIA:/tmp/timelapse.mp4" in rig.posts[0]


def test_standby_after_printing_is_a_completion(capture_logs):
    rig = Rig([st("printing"), st("standby")])
    assert rig.monitor.run() == "complete"
    assert len(rig.posts) == 1 and "MEDIA:" in rig.posts[0]


@pytest.mark.parametrize("state,kind", [("cancelled", "cancelled"), ("error", "error")])
def test_cancelled_and_errored_prints_post_once(capture_logs, state, kind):
    rig = Rig([st("printing"), st("paused"), st(state)])
    assert rig.monitor.run() == kind
    assert rig.session.stopped == kind
    assert len(rig.posts) == 1


# -- dedupe ------------------------------------------------------------------------


def test_duplicate_polls_flaps_and_reconnects_never_duplicate(capture_logs):
    rig = Rig([st("printing"), RuntimeError("read failed"), st("printing"),
               st("paused"), st("printing"), st("complete"), st("complete")])
    assert rig.monitor.run() == "complete"
    assert rig.begins == [STREAM_URL]          # one session, opened once
    assert rig.streams == ["open", "close"]     # stream opened/closed once
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


def test_no_camera_advertised_posts_text_only_with_a_diagnostic(capture_logs):
    rig = Rig([st("printing"), st("complete")], camera_url="")
    assert rig.monitor.run() == "complete"
    assert rig.begins == [] and rig.streams == []
    assert len(rig.posts) == 1 and "MEDIA:" not in rig.posts[0]
    assert any("no camera source" in line for line in capture_logs)


def test_camera_read_failure_posts_text_only(capture_logs):
    rig = Rig([st("printing"), st("complete")])
    rig.monitor._camera_url_fn = scripted([RuntimeError("detail refused")])
    assert rig.monitor.run() == "complete"
    assert rig.begins == []
    assert len(rig.posts) == 1 and "MEDIA:" not in rig.posts[0]
    assert any("could not be read" in line for line in capture_logs)


def test_stream_open_failure_still_captures(capture_logs):
    rig = Rig([st("printing"), st("complete")], stream_error=RuntimeError("refused"))
    assert rig.monitor.run() == "complete"
    assert rig.begins == [STREAM_URL]
    assert len(rig.posts) == 1 and "MEDIA:" in rig.posts[0]


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


def test_all_dark_session_names_the_camera_dark_outcome(capture_logs):
    rig = Rig([st("printing"), st("complete")],
              session=FakeSession(frames=0, failures=0, dark_frames=4))
    assert rig.monitor.run() == "complete"
    assert rig.encodes == []
    assert len(rig.posts) == 1 and "MEDIA:" not in rig.posts[0]
    assert ("Timelapse not attached: the camera image was completely dark"
            in rig.posts[0])
    assert any("completely dark" in line for line in capture_logs)


def test_oversize_video_posts_the_recorded_but_too_large_line(capture_logs, monkeypatch):
    monkeypatch.setattr(stock_watch, "_too_big_text", lambda: "RECORDED BUT TOO LARGE")
    rig = Rig([st("printing"), st("complete")], encode=lambda session: None)
    assert rig.monitor.run() == "complete"
    assert len(rig.posts) == 1
    assert "MEDIA:" not in rig.posts[0]
    assert rig.posts[0].endswith("RECORDED BUT TOO LARGE")


# -- the encoder ---------------------------------------------------------------------


def _fake_run(writes=True, fail_on=None):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if fail_on and any(fail_on in part for part in cmd):
            raise subprocess.CalledProcessError(1, cmd)
        if writes:
            Path(cmd[-1]).write_bytes(b"video")
        return subprocess.CompletedProcess(cmd, 0)

    return run, calls


def test_encode_frames_hardware_failure_retries_with_cpu(tmp_path):
    run, calls = _fake_run(fail_on="h264_nvenc")
    out = stock_watch.encode_frames(tmp_path, tmp_path / "timelapse.mp4",
                                    encoder="nvenc", run=run)
    assert out == tmp_path / "timelapse.mp4" and out.read_bytes() == b"video"
    assert len(calls) == 2
    assert "h264_nvenc" in calls[0] and "libx264" in calls[1]
    assert "-framerate" in calls[0]  # the frames are the input, not a video file


def test_encode_frames_cpu_failure_raises(tmp_path):
    run, _calls = _fake_run(fail_on="libx264")
    with pytest.raises(subprocess.CalledProcessError):
        stock_watch.encode_frames(tmp_path, tmp_path / "timelapse.mp4",
                                  encoder="cpu", run=run)


# -- start_monitor wiring -------------------------------------------------------------


class FakeAdapter:
    def __init__(self, details):
        self.config = {"flashforge_stock": {"host": "192.0.2.10"}}
        self._details = scripted(details)

    def client(self):
        return self

    def detail(self):
        return self._details()

    def normalize_status(self, raw):
        from print_handy.printers.flashforge_stock.client import StockClient
        return StockClient.status_of(raw)


def test_start_monitor_wires_the_adapter_and_starts_one_thread():
    # The print already finished by the first poll: the monitor posts the
    # completion for the matching filename without ever sleeping.
    adapter = FakeAdapter([{"status": "completed", "printFileName": REMOTE}])
    posts, targets = [], []

    class _Thread:
        def __init__(self, target):
            self._target = target

        def start(self):
            self._target()

    def factory(target):
        targets.append(target)
        return _Thread(target)

    thread = stock_watch.start_monitor(adapter, REMOTE, post=posts.append,
                                       thread_factory=factory)
    assert thread is not None and len(targets) == 1
    assert len(posts) == 1 and "MEDIA:" not in posts[0]


# -- the full lifecycle: real session, recorded frames, real ffmpeg -------------------


@pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="needs ffmpeg and ffprobe on PATH")
def test_full_lifecycle_produces_a_valid_h264_mp4_under_the_limit(tmp_path, monkeypatch):
    # Three recorded frames, generated once by ffmpeg's colour source.
    frames = []
    for colour in ("red", "green", "blue"):
        frame_path = tmp_path / f"{colour}.jpg"
        subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi",
                        "-i", f"color=c={colour}:s=320x240", "-frames:v", "1",
                        str(frame_path)], check=True)
        frames.append(frame_path.read_bytes())
    served = scripted(frames)

    clock = FakeClock()
    root = tmp_path / "cache" / "timelapse"

    def begin_fn(url):
        session = timelapse_capture.CaptureSession(
            root, REMOTE, REMOTE,
            trigger=timelapse_capture.make_trigger("interval", 0.5),
            max_frames=100, max_bytes=0, url=url,
            fetch=lambda _url: served(), log=lambda _line: None, now=clock)
        assert session.start()
        return session

    monkeypatch.setattr(mel, "_media_upload_limit", lambda: 500_000)
    posts, streams = [], []
    monitor = stock_watch.StockPrintMonitor(
        REMOTE,
        status_fn=scripted([st("printing"), st("printing"), st("printing"),
                            st("printing"), st("complete")]),
        camera_url_fn=lambda: STREAM_URL,
        stream_fn=streams.append,
        post=posts.append,
        begin_fn=begin_fn,
        clock=clock, sleep=clock.sleep, poll_s=30.0, capture_poll_s=0.5)
    assert monitor.run() == "complete"
    assert streams == ["open", "close"]
    assert len(posts) == 1 and "MEDIA:" in posts[0]
    video = Path(posts[0].split("MEDIA:", 1)[1].strip())
    assert video.is_file() and 0 < video.stat().st_size <= 500_000
    probe = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                            str(video)], capture_output=True, text=True, check=True)
    assert probe.stdout.strip() == "h264"
    session_dir = next(root.iterdir())
    assert len(list((session_dir / "frames").glob("frame-*.jpg"))) >= 2


def test_media_post_failure_falls_back_to_the_text_completion(capture_logs):
    """#391 (#373 expansion): the platform refusing the MEDIA post must not
    lose the completion — the monitor posts the text alone and logs why,
    the same attach-or-explain contract as the Moonraker route."""
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


# ------------------------------------------------- capture gate ordering (P2-139)

def _monitor_with_begin(begin_fn):
    streams, posts = [], []
    clock = FakeClock()
    monitor = stock_watch.StockPrintMonitor(
        REMOTE,
        status_fn=scripted([st("printing"), st("complete")]),
        camera_url_fn=scripted([STREAM_URL]),
        stream_fn=streams.append,
        post=posts.append,
        begin_fn=begin_fn,
        encode_fn=lambda session: None,
        clock=clock, sleep=clock.sleep, poll_s=30.0, capture_poll_s=0.5)
    return monitor, streams


def test_a_refused_capture_never_opens_the_camera_stream():
    """P2-139: begin() is the capture gate (capture_enabled / no session);
    the stream must not be opened — or later closed — when it refuses."""
    monitor, streams = _monitor_with_begin(lambda url: None)
    monitor.run()
    assert streams == []
    assert monitor._stream_open is False


def test_a_failed_capture_begin_never_opens_the_camera_stream():
    def begin(url):
        raise RuntimeError("capture refused")

    monitor, streams = _monitor_with_begin(begin)
    monitor.run()
    assert streams == []
    assert monitor._stream_open is False


# --------------------------------------------- layer trigger fallback (P2-140)

def test_a_layer_trigger_falls_back_to_interval_on_stock(monkeypatch):
    """P2-140: stock status carries no layer, so a layer-triggered session
    could never fire. The watch substitutes the configured interval."""
    import types

    from print_handy.hermes import timelapse_capture as tc

    session = types.SimpleNamespace(trigger=tc.LayerTrigger())
    monkeypatch.setattr(tc, "begin", lambda *a, **k: session)
    monkeypatch.setattr(tc, "capture_settings",
                        lambda data=None: {"capture_interval_s": 45})
    monitor, _streams = _monitor_with_begin(None)
    out = monitor._begin_default(STREAM_URL)
    assert out is session
    assert isinstance(session.trigger, tc.IntervalTrigger)
    assert session.trigger.interval_s == 45


def test_an_interval_trigger_is_left_alone_on_stock(monkeypatch):
    import types

    from print_handy.hermes import timelapse_capture as tc

    trigger = tc.IntervalTrigger(30)
    session = types.SimpleNamespace(trigger=trigger)
    monkeypatch.setattr(tc, "begin", lambda *a, **k: session)
    monkeypatch.setattr(tc, "capture_settings",
                        lambda data=None: {"capture_interval_s": 45})
    monitor, _streams = _monitor_with_begin(None)
    assert monitor._begin_default(STREAM_URL) is session
    assert session.trigger is trigger


def test_a_refused_media_post_falls_back_to_the_text_completion(capture_logs):
    """The production sender returns False on a refusal instead of raising
    (the P2-169/P2-173 contract): a refused MEDIA post still owes the
    chat the plain completion text."""
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
