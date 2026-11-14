"""Camera watch follow-ups after #1148 (#1194), across every family with an in-print watch.

Moonraker (the listener) and Bambu Lab (the launched-print monitor) are the
two families with an in-print camera watch; PrusaLink, OctoPrint, Elegoo
SDCP and stock Flashforge have idle plate checks only, which the coverage
report and the docs now say. All offline: cameras, verdicts, chat posts,
clocks and the detector are fakes; the credential test talks to a local
HTTP server on the loopback interface only.
"""
import asyncio
import base64
import http.server
import io
import os
import random
import threading
import time
from pathlib import Path

import pytest

from print_handy.runtime import bambu_watch
from print_handy.runtime import local_vision as lv
from print_handy.runtime import moonraker_event_listener as mel
from print_handy.runtime import print_alert_buttons as pab
from print_handy.runtime import visual_watch as vw

ROOT = Path(__file__).resolve().parents[2]


def _frame(seed, size=(160, 120)):
    """A real JPEG camera frame; different seeds are visibly different views."""
    from PIL import Image
    rng = random.Random(seed)
    image = Image.new("L", size)
    image.putdata([rng.randrange(40, 220) for _ in range(size[0] * size[1])])
    out = io.BytesIO()
    image.convert("RGB").save(out, "JPEG")
    return out.getvalue()


def _reencoded(image, quality=70):
    from PIL import Image
    out = io.BytesIO()
    Image.open(io.BytesIO(image)).save(out, "JPEG", quality=quality)
    return out.getvalue()


class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def _st(layer, state="printing", **extra):
    return {"state": state, "filename": "part.gcode.3mf", "layer": layer, **extra}


def _bambu(*, verdict=lambda image, prompt: ("ok", "fine"), camera=None, posts=None, alerts=None,
           texts=None, wall=None, **kwargs):
    posts = [] if posts is None else posts
    alerts = [] if alerts is None else alerts
    texts = [] if texts is None else texts
    wall = wall or Clock(10_000.0)
    monitor = bambu_watch.BambuPrintMonitor(
        "part.gcode.3mf", status_fn=lambda: _st(3, state="complete"),
        camera_fn=camera or (lambda: _frame(1)),
        post=posts.append,
        watch=kwargs.pop("watch", None) or vw.VisualPrintWatch(sensitivity=3),
        verdict=verdict,
        alert=lambda filename, image, reason, identity=None: alerts.append(reason),
        text_alert=lambda filename, reason, identity=None: texts.append(reason),
        event_recorder=lambda **kw: None,
        clock=kwargs.pop("clock", wall), wall=wall, sleep=wall.sleep, poll_s=10, **kwargs)
    return monitor, posts, alerts, texts, wall


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(pab, "visual_muted", lambda filename, identity: False)
    monkeypatch.setattr(mel, "_record_watchdog_event", lambda *a, **k: True)
    monkeypatch.setattr(lv, "_announced", set(), raising=False)
    monkeypatch.setattr(lv, "_notices", [], raising=False)
    monkeypatch.setattr(lv, "_failures", {})


# -- 1. a check that times out: deadline and a visible retry notice -----------


def test_bambu_hung_check_hits_the_deadline_and_says_so_once():
    release = threading.Event()

    def hung_camera():
        release.wait(5)
        raise OSError("gave up")

    monitor, posts, alerts, texts, wall = _bambu(camera=hung_camera, check_deadline_s=0.2,
                                                 clock=time.monotonic)
    try:
        started = time.monotonic()
        monitor.poll_active(_st(2))
        # The check runs on its own thread: the poll returns at once, well
        # inside the deadline, and nothing is said before the deadline passes.
        assert time.monotonic() - started < 0.2
        assert getattr(monitor, "check_timeouts", 0) == 0 and texts == []
        time.sleep(0.3)
        wall.t += 30
        monitor.poll_active(_st(3))  # the next poll past the deadline says so, once
        assert monitor.check_timeouts == 1
        assert texts == [vw.TIMEOUT_ALERT_TEXT]
        wall.t += 30
        monitor.poll_active(_st(5))  # the stuck check still runs: no second check, no repeat
        assert len(texts) == 1 and monitor.check_timeouts == 1
    finally:
        release.set()
    monitor._check_thread.join(5)
    assert len(texts) == 1  # the abandoned check's late failure posts nothing more
    assert monitor._check_slot_free() is True


def test_bambu_monitor_keeps_polling_and_sees_the_end_while_a_check_hangs():
    # A check that hangs inside its deadline must not hold the status polls:
    # the print finishing meanwhile is seen on the next poll, and the hung
    # check's late (suspicious) verdict posts nothing about a finished print.
    release = threading.Event()
    calls = []

    def camera():
        calls.append(1)
        if len(calls) == 1:
            release.wait(10)
        return _frame(len(calls))

    reads = iter([_st(2), _st(3), _st(3, state="complete")])
    statuses = []

    def status_fn():
        st = next(reads)
        statuses.append(st["state"])
        return st

    alerts, texts, posts = [], [], []
    wall = Clock(10_000.0)
    monitor = bambu_watch.BambuPrintMonitor(
        "part.gcode.3mf", status_fn=status_fn, camera_fn=camera, post=posts.append,
        watch=vw.VisualPrintWatch(sensitivity=3), verdict=lambda image, prompt: ("spaghetti", "a mess"),
        alert=lambda filename, image, reason, identity=None: alerts.append(reason),
        text_alert=lambda filename, reason, identity=None: texts.append(reason),
        event_recorder=lambda **kw: None, clock=wall, wall=wall, sleep=wall.sleep, poll_s=10,
        check_deadline_s=150.0)
    try:
        started = time.monotonic()
        assert monitor.run() == "complete"
        assert time.monotonic() - started < 5  # never held for the check's deadline
        assert statuses == ["printing", "printing", "complete"]
        assert monitor._check_thread is not None and monitor._check_thread.is_alive()
    finally:
        release.set()
    monitor._check_thread.join(5)
    assert alerts == [] and texts == []  # the late verdict is dropped: the monitor stopped


def test_bambu_capture_takes_no_frame_while_a_check_holds_the_camera():
    release = threading.Event()
    ticks = []
    monitor, posts, alerts, texts, wall = _bambu(camera=lambda: release.wait(5) or _frame(1),
                                                 check_deadline_s=150.0)
    monitor._session = type("Session", (), {"tick": lambda self, layer: ticks.append(layer)})()
    try:
        monitor.poll_active(_st(2))
        assert monitor._check_running()
        monitor._sleep_between_polls(tick=True)
        assert ticks == []
    finally:
        release.set()
    monitor._check_thread.join(5)
    monitor._sleep_between_polls(tick=True)
    assert ticks


# -- 2. prints without layer numbers: one notice (Bambu; Moonraker in test_watch_coverage) --


def test_bambu_layerless_print_runs_on_the_timer_with_one_notice():
    verdicts = []
    monitor, posts, alerts, texts, wall = _bambu(
        verdict=lambda image, prompt: verdicts.append(prompt) or ("ok", "fine"))
    monitor.poll_active(_st(None, print_duration=10))
    assert verdicts == []
    wall.t += 200
    monitor.poll_active(_st(None, print_duration=vw.LAYERLESS_FIRST_CHECK_S + 20))
    wall.t += monitor._watch.profile["periodic_s"]
    monitor.poll_active(_st(None, print_duration=2000))
    assert len(verdicts) == 2
    notices = [p for p in posts if "no layer numbers" in p]
    assert len(notices) == 1 and "on a timer" in notices[0]


# -- 3 and 12. the Bambu early-layer check and the shared auto-pause rule ------


def _auto_pause_cfg(monkeypatch, enabled=True):
    data = {"visual_watch": {"enabled": True, "sensitivity": 3, "auto_pause_early": enabled,
                             "early_layers": 10, "early_interval_s": 120}}
    monkeypatch.setattr(vw._config, "cfg", lambda: data)


def _risky_bambu(monkeypatch, frames, *, enabled=True):
    _auto_pause_cfg(monkeypatch, enabled)
    pauses = []
    monitor, posts, alerts, texts, wall = _bambu(
        camera=lambda: frames.pop(0),
        verdict=lambda image, prompt: ("spaghetti", "tangled filament"),
        auto_pause=lambda filename, identity: pauses.append((filename, identity)) or "Paused.",
        watch_mode_fn=lambda: (vw.WATCH_MODE_EARLY, True), printer_id="bambu2")
    return monitor, posts, alerts, pauses, wall


def test_bambu_risky_print_is_checked_on_every_early_layer():
    verdicts = []
    monitor, *_ = _bambu(verdict=lambda image, prompt: verdicts.append(1) or ("ok", "fine"),
                         camera=lambda: _frame(len(verdicts) + 50),
                         watch_mode_fn=lambda: (vw.WATCH_MODE_EARLY, True))
    for layer in (1, 2, 3, 4):
        monitor.poll_active(_st(layer))
    assert monitor._watch.mode == vw.WATCH_MODE_EARLY
    assert len(verdicts) == 4  # a normal print is checked at layer 2 only here


def test_bambu_watch_mode_waits_for_the_start_context():
    answers = [(vw.WATCH_MODE_NORMAL, False), (vw.WATCH_MODE_EARLY, True)]
    monitor, *_ = _bambu(watch_mode_fn=lambda: answers.pop(0))
    monitor.poll_active(_st(1))
    assert monitor._watch.mode == vw.WATCH_MODE_NORMAL
    monitor.poll_active(_st(1))
    assert monitor._watch.mode == vw.WATCH_MODE_EARLY
    monitor.poll_active(_st(2))  # settled: never asked again
    assert answers == []


def test_bambu_same_frame_seconds_apart_never_pauses(monkeypatch):
    still = _frame(7)
    monitor, posts, alerts, pauses, wall = _risky_bambu(monkeypatch, [still, _reencoded(still)])
    monitor.poll_active(_st(1))
    wall.t += 4
    monitor.poll_active(_st(2))
    assert len(alerts) == 2 and pauses == []


def test_bambu_two_independent_views_pause_once_with_its_printer(monkeypatch):
    monitor, posts, alerts, pauses, wall = _risky_bambu(monkeypatch, [_frame(1), _frame(2), _frame(3)])
    monitor.poll_active(_st(1))
    wall.t += vw.EVIDENCE_MIN_SPACING_S
    monitor.poll_active(_st(2))
    assert pauses == [("part.gcode.3mf", monitor._identity)]
    assert "independent suspicious views" in alerts[-1]
    assert any(p.startswith("Auto-pause:") and "Paused." in p for p in posts)
    wall.t += vw.EVIDENCE_MIN_SPACING_S
    monitor.poll_active(_st(3))
    assert len(pauses) == 1  # once per print


def test_bambu_auto_pause_stays_off_unless_enabled(monkeypatch):
    monitor, posts, alerts, pauses, wall = _risky_bambu(
        monkeypatch, [_frame(1), _frame(2)], enabled=False)
    monitor.poll_active(_st(1))
    wall.t += vw.EVIDENCE_MIN_SPACING_S
    monitor.poll_active(_st(2))
    assert pauses == [] and len(alerts) == 2


def test_bambu_default_auto_pause_is_the_alert_pause_path_for_its_printer(monkeypatch):
    seen = []
    monkeypatch.setattr(pab, "handle_click", lambda action, alert: seen.append((action, alert)) or "ok")
    assert bambu_watch._default_auto_pause("p.3mf", "bambu:p.3mf@1", printer_id="bambu2") == "ok"
    assert seen == [("pause", {"kind": "visual", "filename": "p.3mf",
                               "identity": "bambu:p.3mf@1", "printer_id": "bambu2"})]


# -- 4. visual alerts are bound to the printer ID ------------------------------


def test_moonraker_auto_pause_names_the_listeners_printer(monkeypatch):
    seen = []
    monkeypatch.setattr(pab, "handle_click", lambda action, alert: seen.append(alert) or "ok")
    monkeypatch.setattr(mel, "_watch_printer_id", lambda: "second")
    mel._auto_pause_early("risky.gcode", "ident")
    assert seen == [{"kind": "visual", "filename": "risky.gcode", "identity": "ident",
                     "printer_id": "second"}]


def test_bambu_alerts_carry_the_launched_printer(monkeypatch):
    seen = {}
    monkeypatch.setattr(pab, "post_visual_alert",
                        lambda token, chat, text, filename, path, **kw: seen.setdefault("alert", kw) or True)
    monkeypatch.setattr(pab, "post_visual_text_alert",
                        lambda token, chat, text, filename, **kw: seen.setdefault("text", kw) or True)
    monkeypatch.setattr(pab, "post_visual_dark_notice",
                        lambda token, chat, text, filename, **kw: seen.setdefault("dark", kw) or True)
    common = dict(post=lambda text: None, token="tok", chat_id="42", buttonless=False,
                  identity="bambu:x", printer_id="bambu2")
    bambu_watch.deliver_visual_alert("x.3mf", _frame(3), "spaghetti", **common)
    bambu_watch.deliver_visual_text_alert("x.3mf", "camera down", **common)
    bambu_watch.deliver_dark_notice("x.3mf", **common)
    assert {kind: kw.get("printer_id") for kind, kw in seen.items()} == {
        "alert": "bambu2", "text": "bambu2", "dark": "bambu2"}


def test_bambu_start_monitor_binds_every_camera_post_to_its_printer(monkeypatch):
    built = {}

    class Recorder:
        def __init__(self, filename, **kwargs):
            built.update(kwargs)

        def run(self):
            return None

    data = {"visual_watch": {"enabled": True, "sensitivity": 3}, "timelapse": {}}
    monkeypatch.setattr(bambu_watch._config, "cfg", lambda: data)
    monkeypatch.setattr(mel, "_vision_ready", lambda: True)
    monkeypatch.setattr(bambu_watch, "BambuPrintMonitor", Recorder)
    from print_handy.runtime import family_control
    monkeypatch.setattr(family_control, "pause_alerts_for", lambda *a: None)

    class Adapter:
        label = "Bambu Lab P1S"

        def camera_snapshot(self):
            return _frame(4)

    thread = bambu_watch.start_monitor(Adapter(), "x.3mf", post=lambda text: None,
                                       status_fn=lambda: {}, printer_id="bambu2",
                                       thread_factory=lambda target: threading.Thread(target=target))
    thread.join(5)
    for key in ("alert", "text_alert", "dark_notice", "auto_pause"):
        assert built[key].keywords == {"printer_id": "bambu2"}, key
    assert built["printer_id"] == "bambu2"
    assert built["check_deadline_s"] == bambu_watch.CHECK_DEADLINE_S


# -- 5. the local detector: failure and recovery notices -----------------------


def test_detector_failure_is_announced_once_and_recovery_once():
    lv._fail("file:/models/m.onnx", "downloading the local detector model failed: timed out")
    lv._fail("file:/models/m.onnx", "downloading the local detector model failed: timed out")
    first = lv.take_notices()
    assert len(first) == 1 and "unavailable" in first[0] and "retries on its own" in first[0]
    lv._clear_failure("file:/models/m.onnx")
    lv._clear_failure("file:/models/m.onnx")  # every later success is silent
    assert lv.take_notices() == ["Local failure detector is working again; camera checks "
                                 "that rely on it are judged normally."]
    assert lv.take_notices() == []


def test_failed_download_then_success_makes_the_detector_available(monkeypatch, tmp_path):
    path = tmp_path / "model.onnx"
    attempts = []

    def download(target):
        attempts.append(target)
        if len(attempts) == 1:
            raise lv.LocalVisionError("downloading the local detector model failed: reset")
        target.write_bytes(b"model")

    monkeypatch.setattr(lv, "_download_model", download)
    monkeypatch.setattr(lv, "_is_default_path", lambda p, cfg=None: False)
    clock = [100.0]
    monkeypatch.setattr(lv.time, "monotonic", lambda: clock[0])
    with pytest.raises(lv.LocalVisionError):
        lv._ensure_model_file(path)
    with pytest.raises(lv.LocalVisionError):
        lv._ensure_model_file(path)  # inside its backoff: not retried
    assert len(attempts) == 1
    clock[0] += lv.FAILURE_RETRY_BASE_S
    lv._ensure_model_file(path)
    assert path.is_file() and len(attempts) == 2
    notices = lv.take_notices()
    assert len(notices) == 2 and "unavailable" in notices[0] and "working again" in notices[1]


def test_download_runs_without_the_session_lock(monkeypatch, tmp_path):
    held = []

    def download(target):
        held.append(lv._lock.locked())
        target.write_bytes(b"model")

    monkeypatch.setattr(lv, "_download_model", download)
    monkeypatch.setattr(lv, "_is_default_path", lambda p, cfg=None: False)
    lv._ensure_model_file(tmp_path / "m.onnx")
    assert held == [False]


def test_listener_posts_detector_notices_while_the_watch_uses_it(monkeypatch):
    posts = []
    data = {"visual_watch": {"enabled": True, "sensitivity": 3},
            "vision": {"local_detector_enabled": True}}
    monkeypatch.setattr(mel._config, "cfg", lambda: data)
    monkeypatch.setattr(mel, "post", lambda text, **kw: posts.append(text) or True)
    monkeypatch.setattr(mel, "log", lambda line: None); monkeypatch.setattr(mel, "warn", lambda line: None)
    lv._fail("session:/m.onnx", "the local detector model at /m.onnx could not be loaded: bad")
    listener = mel.Listener()
    listener.state = "standby"

    async def run():
        listener._visual_timer_tick(now=100.0)
        listener._visual_timer_tick(now=115.0)
        await asyncio.gather(*listener.background_tasks)

    asyncio.run(run())
    assert len(posts) == 1 and "Local failure detector unavailable" in posts[0]


def test_bambu_monitor_posts_detector_notices(monkeypatch):
    data = {"visual_watch": {"enabled": True}, "vision": {"watch_source": "local"}}
    monkeypatch.setattr(vw._config, "cfg", lambda: data)
    monkeypatch.setattr(lv._config, "cfg", lambda: data)
    lv._fail("file:/m.onnx", "downloading failed")
    monitor, posts, *_ = _bambu()
    monitor.poll_active(_st(1))
    assert [p for p in posts if "Local failure detector" in p] and lv.take_notices() == []


# -- 6. camera alert episodes are throttled -----------------------------------


def _failing_listener(monkeypatch, verdicts):
    alerts, posts = [], []
    clock = [50_000.0]
    seeds = iter(range(100, 200))
    monkeypatch.setattr(mel, "_camera_snapshot_bytes", lambda: _frame(next(seeds)))
    monkeypatch.setattr(mel, "_watch_now", lambda: clock[0])
    monkeypatch.setattr(mel, "_visual_verdict", lambda *a, **k: verdicts.pop(0))
    monkeypatch.setattr(mel, "_visual_alert",
                        lambda filename, image, text, identity=None: alerts.append(text))
    monkeypatch.setattr(mel, "post", lambda text, **kw: posts.append(text) or True)
    monkeypatch.setattr(mel, "log", lambda line: None); monkeypatch.setattr(mel, "warn", lambda line: None)
    listener = mel.Listener()
    listener.filename = "watched.gcode"
    listener.state = "printing"
    return listener, alerts, posts, clock


def test_ten_failed_verdicts_alert_once_then_recovery_is_announced(monkeypatch):
    failed = ("uncertain", vw.VISION_FAILED_DETAIL)
    verdicts = [failed] * 10 + [("ok", "plastic on the bed")] + [failed]
    listener, alerts, posts, clock = _failing_listener(monkeypatch, verdicts)
    for _ in range(10):
        asyncio.run(listener.visual_check("periodic", 7))
        clock[0] += 30
    assert len(alerts) == 1
    asyncio.run(listener.visual_check("periodic", 8))
    assert len([p for p in posts if "is being checked again" in p]) == 1
    asyncio.run(listener.visual_check("periodic", 9))
    assert len(alerts) == 2  # a new episode alerts at once


def test_real_uncertain_verdicts_still_alert_every_time(monkeypatch):
    verdicts = [("uncertain", "the nozzle is hidden by the part")] * 3
    listener, alerts, posts, clock = _failing_listener(monkeypatch, verdicts)
    for _ in range(3):
        asyncio.run(listener.visual_check("periodic", 7))
        clock[0] += 30
    assert len(alerts) == 3


def test_ten_dead_camera_checks_alert_once(monkeypatch):
    texts = []
    monkeypatch.setattr(mel, "_camera_snapshot_bytes", lambda: (_ for _ in ()).throw(OSError("down")))
    clock = [1000.0]
    monkeypatch.setattr(mel, "_watch_now", lambda: clock[0])
    monkeypatch.setattr(mel, "_visual_alert_text",
                        lambda filename, reason, identity=None: texts.append(reason))
    monkeypatch.setattr(mel, "log", lambda line: None); monkeypatch.setattr(mel, "warn", lambda line: None)
    listener = mel.Listener()
    listener.filename = "deadcam.gcode"
    listener.state = "printing"
    for _ in range(10):
        asyncio.run(listener.visual_check("layer_progress", 9))
        clock[0] += 30
    assert len(texts) == 1


def test_bambu_failed_verdicts_share_the_episode():
    verdicts = [("uncertain", vw.VISION_FAILED_DETAIL)] * 10 + [("ok", "fine")]
    seeds = iter(range(300, 400))
    monitor, posts, alerts, texts, wall = _bambu(camera=lambda: _frame(next(seeds)),
                                                 verdict=lambda image, prompt: verdicts.pop(0))
    for _ in range(10):
        monitor.visual_check("periodic", 7)
        wall.t += 30
    assert len(alerts) == 1
    monitor.visual_check("periodic", 8)
    assert len([p for p in posts if "is being checked again" in p]) == 1


# -- 7. the heartbeat under saturated camera work (the experiment) ------------


def test_heartbeat_keeps_beating_with_every_camera_worker_blocked(monkeypatch):
    """The #1194 experiment: fill the camera-watch pool (and its queue) with
    checks whose camera never answers, and watch the heartbeat. Result: the
    loop keeps stamping and the writer reports no lag, because camera work
    runs on its own pool and the heartbeat never waits on any pool."""
    block = threading.Event()

    def stuck_camera():
        block.wait(10)
        raise OSError("camera never answered")

    monkeypatch.setattr(mel, "_camera_snapshot_bytes", stuck_camera)
    monkeypatch.setattr(mel, "_visual_alert_text", lambda *a, **k: None)
    monkeypatch.setattr(mel, "log", lambda line: None); monkeypatch.setattr(mel, "warn", lambda line: None)
    monkeypatch.setattr(mel.healthcheck, "HEARTBEAT_S", 0.05)
    lags = []
    monkeypatch.setattr(mel.healthcheck, "beat", lambda **kw: lags.append(kw["loop_lag_ms"]))
    listener = mel.Listener()
    listener.filename = "busy.gcode"
    listener.state = "printing"
    listener.moonraker_connected = True
    stamps = []

    async def run():
        beat = asyncio.create_task(listener.heartbeat())
        checks = [asyncio.create_task(listener.visual_check("periodic", n))
                  for n in range(mel.WATCH_IO_WORKERS * 3)]
        await asyncio.sleep(0.05)
        for _ in range(8):
            await asyncio.sleep(0.06)
            stamps.append(listener._loop_tick)
            # The writer thread's beat, taken from another thread like the real one.
            writer = threading.Thread(target=listener._beat_once)
            writer.start()
            writer.join(2)
        block.set()
        await asyncio.wait_for(asyncio.gather(*checks), 10)
        beat.cancel()
        await asyncio.gather(beat, return_exceptions=True)

    try:
        asyncio.run(run())
    finally:
        block.set()
    assert len(set(stamps)) >= 7
    assert lags and max(lags) < 500


# -- 10. bounded growth: frames, the baseline cache, the listener log ---------


def test_frame_folder_keeps_the_newest_and_drops_old_frames(tmp_path):
    now = 1_000_000.0
    for index in range(6):
        path = tmp_path / f"{index}-a.jpg"
        path.write_bytes(b"x")
        os.utime(path, (now - 600 + index, now - 600 + index))
    old = tmp_path / "ancient.jpg"
    old.write_bytes(b"x")
    os.utime(old, (now - 30 * 86400, now - 30 * 86400))
    other = tmp_path / "notes.txt"
    other.write_text("kept")
    removed = vw.prune_frames(tmp_path, keep=3, now=now)
    assert sorted(p.name for p in tmp_path.glob("*.jpg")) == ["3-a.jpg", "4-a.jpg", "5-a.jpg"]
    assert old in removed and other.exists()


def test_writing_an_alert_frame_prunes_the_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("PRINT_WATCH_FRAME_DIR", str(tmp_path))
    monkeypatch.setattr(vw, "FRAME_KEEP", 2)
    for index in range(4):
        (tmp_path / f"{index}-old.jpg").write_bytes(b"x")
        recent = time.time() - 600 + index
        os.utime(tmp_path / f"{index}-old.jpg", (recent, recent))
    path = bambu_watch.write_frame("part.3mf", _frame(9))
    assert path.exists()
    assert sorted(p.name for p in tmp_path.glob("*.jpg")) == sorted([path.name, "3-old.jpg"])



def test_listener_keeps_only_the_current_prints_baseline_frame(monkeypatch):
    monkeypatch.setattr(mel, "log", lambda line: None); monkeypatch.setattr(mel, "warn", lambda line: None)
    listener = mel.Listener()
    listener.filename = "first.gcode"
    listener.visual_frames["first.gcode"] = _frame(1)
    asyncio.run(listener.on_status({"filename": "second.gcode"}, "event"))
    assert listener.visual_frames == {}


def test_listener_writes_no_legacy_log_file(tmp_path, monkeypatch, caplog):
    # #1413: the Moonraker listener's own dated log file is retired; its lines
    # go only to the shared logger, which owns files, levels and retention.
    import logging
    monkeypatch.setattr(mel.announcer, "PROFILE_HOME", tmp_path)
    with caplog.at_level(logging.INFO, logger=mel.logger.name):
        mel.log("camera watch beat")
    assert not hasattr(mel, "LOG") and not hasattr(mel, "_log_file")
    assert not (tmp_path / "logs").exists()
    assert [r.getMessage() for r in caplog.records] == ["camera watch beat"]


# -- 11. which families are watched: the report and the docs ------------------


def _model_for(connection):
    from print_handy import printers
    for key in printers.MODELS:
        model = printers.MODELS.get(key)
        if getattr(getattr(model, "vendor", None), "connection", None) == connection:
            return key
    pytest.skip(f"no model with connection {connection}")


def test_coverage_report_names_each_printers_watch(monkeypatch):
    monkeypatch.setattr(vw, "watch_unavailable_reason", lambda api_key, prefetch=True: "")
    cfg = {"visual_watch": {"enabled": True},
           "printers": [{"id": "k1", "model": _model_for("moonraker")},
                        {"id": "x1", "model": _model_for("bambulab_lan")},
                        {"id": "mk4", "model": _model_for("prusalink")},
                        {"id": "ff", "model": _model_for("flashforge_stock")}]}
    rows = vw.coverage_report(cfg, lambda: "")
    assert all(ok for ok, _ in rows)
    text = [detail for _, detail in rows]
    assert text[0].startswith("k1: Moonraker (Klipper): in-print watch for every print")
    assert text[1].startswith("x1: Bambu Lab: in-print watch for prints Print Handy launched")
    assert text[2] == "mk4: PrusaLink: no in-print watch (idle plate checks only)"
    assert text[3] == "ff: stock Flashforge: no in-print watch (idle plate checks only)"


def test_docs_list_every_family_and_the_pause_rule():
    docker = (ROOT / "docs" / "DOCKER.md").read_text(encoding="utf-8")
    section = docker.split("## Camera watch (visual_watch)", 1)[1].split("\n## ", 1)[0]
    for name in ("Moonraker (Klipper)", "Bambu Lab", "PrusaLink", "OctoPrint", "Elegoo SDCP",
                 "stock Flashforge"):
        assert f"| {name} |" in section, name
    assert "auto_pause_early" in section and "never cancels" in section


# -- 13. watch_source=endpoint with no endpoint is not silent -----------------


def test_bot_check_fails_a_pinned_source_that_cannot_work(monkeypatch):
    monkeypatch.delenv("PRINT_VISION_URL", raising=False)
    monkeypatch.setattr(lv, "watch_source", lambda cfg=None: "endpoint")
    monkeypatch.setattr(vw._config, "service_url", lambda key: "")
    monkeypatch.setattr(vw.ai_routes, "overrides_route", lambda route: False)
    monkeypatch.setattr(vw.local_empty, "reference_ready", lambda: False)
    started = []
    monkeypatch.setattr(lv, "prefetch_model", lambda *a, **k: started.append(1))
    cfg = {"visual_watch": {"enabled": True}, "printers": [{"id": "k1", "model": _model_for("moonraker")}]}
    [(ok, detail)] = vw.coverage_report(cfg, lambda: "")
    assert ok is False and "watch_source is endpoint" in detail and "not running" in detail
    assert started == []  # bot check never starts a download


def test_bot_check_has_a_camera_watch_row():
    from print_handy import settings
    from print_handy.bot import __main__ as bot_main
    rows = bot_main.checks(settings.current())
    assert any(label == "camera watch" for label, _ok, _detail in rows)


# -- 14. credentials in the snapshot URL --------------------------------------


def test_url_credentials_travel_as_basic_auth_and_never_in_the_host():
    from print_handy import camera_url
    clean, auth = camera_url.split_credentials("http://cam:p%40ss@192.0.2.5:8080/snap?x=1")
    assert clean == "http://192.0.2.5:8080/snap?x=1"
    assert base64.b64decode(auth.split()[1]) == b"cam:p@ss"
    assert camera_url.split_credentials("http://192.0.2.5/snap") == ("http://192.0.2.5/snap", None)
    request = camera_url.request("http://cam:secret@192.0.2.5/snap")
    assert request.host == "192.0.2.5" and "secret" not in request.full_url
    assert request.unredirected_hdrs["Authorization"].startswith("Basic ")
    assert "Authorization" not in request.headers  # never forwarded on a redirect


def test_snapshot_from_a_camera_that_needs_its_url_credentials():
    seen = []

    class Camera(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            if self.headers.get("Authorization") != "Basic " + base64.b64encode(b"cam:s3cret").decode():
                self.send_response(401)
                self.end_headers()
                return
            body = _frame(5)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Camera)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        image = mel._fetch_snapshot(f"http://cam:s3cret@127.0.0.1:{server.server_address[1]}/snap")
    finally:
        server.shutdown()
    assert image.startswith(b"\xff\xd8") and seen[-1].startswith("Basic ")


def test_a_failing_camera_never_puts_its_password_in_logs_or_posts(monkeypatch):
    lines, texts = [], []

    def refuse(request, timeout=None):
        raise OSError(f"cannot reach {request.host}")  # what urllib names: the host it was given

    monkeypatch.setattr(mel.urllib.request, "urlopen", refuse)
    monkeypatch.setattr(mel, "_camera_url", lambda: "http://cam:hunter2pass@192.0.2.9/snap")
    monkeypatch.setattr(mel.go2rtc, "camera_source", lambda data=None: "moonraker")
    monkeypatch.setattr(mel, "log", lines.append); monkeypatch.setattr(mel, "warn", lines.append)
    monkeypatch.setattr(mel, "_visual_alert_text",
                        lambda filename, reason, identity=None: texts.append(reason))
    listener = mel.Listener()
    listener.filename = "cam.gcode"
    listener.state = "printing"
    asyncio.run(listener.visual_check("periodic", 3))
    assert lines and texts
    assert not any("hunter2pass" in item for item in lines + texts)


# -- 15. one decode per frame for the dark, change and evidence checks ---------


def test_one_check_decodes_its_new_frame_once(monkeypatch):
    from PIL import Image
    first, second = _frame(901), _frame(902)
    frames = [first, second]
    monkeypatch.setattr(mel, "_camera_snapshot_bytes", lambda: frames.pop(0))
    monkeypatch.setattr(mel, "_visual_verdict", lambda *a, **k: ("spaghetti", "mess"))
    monkeypatch.setattr(mel, "_visual_alert", lambda *a, **k: None)
    monkeypatch.setattr(mel, "log", lambda line: None); monkeypatch.setattr(mel, "warn", lambda line: None)
    listener = mel.Listener()
    listener.filename = "decode.gcode"
    listener.state = "printing"
    asyncio.run(listener.visual_check("periodic", 3))
    real_open = Image.open
    opened = []
    monkeypatch.setattr(Image, "open", lambda fp, *a, **k: opened.append(1) or real_open(fp, *a, **k))
    asyncio.run(listener.visual_check("periodic", 9))
    # frame_dark, the change test against the previous frame and the
    # evidence signature all came from one decode of the new frame.
    assert len(opened) == 1


def test_frame_facts_match_the_single_purpose_answers():
    from PIL import Image
    dark = io.BytesIO()
    Image.new("L", (64, 48), 0).save(dark, "JPEG")
    facts = vw.frame_facts(dark.getvalue())
    assert facts.dark is True and len(facts.thumb) == 64 * 48 and len(facts.signature) == 32 * 24
    assert vw.frame_facts(b"not a jpeg") == vw.FrameFacts(False, None, None)
    a, b = _frame(1), _frame(2)
    assert vw._frame_changed(a, _reencoded(a)) is False and vw._frame_changed(a, b) is True


# -- 16. the privacy note for the hosted small vision model -------------------


def test_settings_doc_says_where_camera_frames_go():
    text = (ROOT / "docs" / "SETTINGS.md").read_text(encoding="utf-8")
    assert "### Camera frames and privacy" in text
    note = text.split("### Camera frames and privacy", 1)[1].split("\n#", 1)[0]
    for phrase in ("[ai.small]", "services.vision_chat_url", "PRINT_VISION_URL",
                   "local failure detector", "leave"):
        assert phrase in note, phrase
