"""Bambu camera parity (#294): status images and the launched-print monitor, offline.

All transports are fakes: the adapter is a scripted stand-in, the monitor's
status/camera/post functions are injected, and the frame dir is a tmp path.
No socket, no printer, no chat is ever touched.
"""
import types

import pytest

from print_handy.hermes import bambu_watch, print_stop
from print_handy.printers.bambulab.client import BambuLanError

JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"
REMOTE = "benchy_abc.gcode.3mf"


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
        if len(items) > 1:
            return items.pop(0)
        return items[0]

    return next_value


def st(state, filename=REMOTE, layer=3):
    return {"state": state, "filename": filename, "layer": layer, "total_layer": 40,
            "progress": 0.5, "print_duration": None}


class FakeAdapter:
    def __init__(self, statuses, *, configured=True, camera=JPEG):
        self._statuses = scripted(statuses)
        self._configured = configured
        self._camera = camera
        self.control_calls = []

    def lan_configured(self):
        return self._configured

    def client(self):
        return types.SimpleNamespace(snapshot=lambda: {})

    def normalize_status(self, raw):
        return self._statuses()

    def camera_snapshot(self):
        if isinstance(self._camera, Exception):
            raise self._camera
        return self._camera

    def control(self, action):
        self.control_calls.append(action)


@pytest.fixture(autouse=True)
def frame_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PRINT_WATCH_FRAME_DIR", str(tmp_path / "frames"))


# -- Stage 2: status with camera ---------------------------------------------

def test_status_with_camera_returns_image_and_status():
    adapter = FakeAdapter([st("printing")])
    out = bambu_watch.status_with_camera(adapter)
    assert out["state"] == "printing" and out["filename"] == REMOTE
    assert out["camera_note"] == ""
    from pathlib import Path
    assert Path(out["camera_image_path"]).read_bytes() == JPEG


def test_status_with_camera_unconfigured_is_a_plain_note_never_an_exception():
    adapter = FakeAdapter([], configured=False)
    out = bambu_watch.status_with_camera(adapter)
    assert out["state"] == "unknown" and out["camera_image_path"] is None
    assert "camera is not configured" in out["camera_note"]


def test_status_with_camera_failure_is_a_note_not_an_exception():
    adapter = FakeAdapter([st("standby")], camera=BambuLanError("the camera did not send a usable image"))
    out = bambu_watch.status_with_camera(adapter)
    assert out["state"] == "standby" and out["camera_image_path"] is None
    assert "did not send a usable image" in out["camera_note"]


def test_print_stop_status_carries_the_camera_image_for_bambu(monkeypatch):
    adapter = FakeAdapter([st("printing")])
    monkeypatch.setattr(print_stop, "_lan_adapter", lambda: adapter)
    out = print_stop.status()
    assert out["state"] == "printing"
    assert out["camera_image_path"] and out["camera_note"] == ""


# -- Stage 2: the launched-print monitor --------------------------------------

def _monitor(statuses, camera=JPEG):
    posts = []
    clock = FakeClock()
    monitor = bambu_watch.BambuPrintMonitor(
        REMOTE, status_fn=scripted(statuses),
        camera_fn=lambda: (_ for _ in ()).throw(camera) if isinstance(camera, Exception) else camera,
        post=posts.append, clock=clock, wall=clock, sleep=clock.sleep, poll_s=10)
    return monitor, posts


def test_monitor_posts_completion_with_the_final_snapshot():
    monitor, posts = _monitor([st("printing"), st("printing"), st("complete")])
    assert monitor.run() == "complete"
    assert len(posts) == 1
    assert posts[0].startswith("Print complete")
    assert "MEDIA:" in posts[0]


def test_monitor_completion_without_a_camera_still_posts_the_text():
    monitor, posts = _monitor([st("error")], camera=BambuLanError("the camera did not answer"))
    assert monitor.run() == "error"
    assert len(posts) == 1 and "MEDIA:" not in posts[0]


def test_monitor_ignores_a_different_print():
    monitor, posts = _monitor([st("printing", filename="other.gcode.3mf"), st("complete", filename="other.gcode.3mf")])
    assert monitor.run() is None
    assert posts == []


def test_monitor_treats_standby_after_printing_as_completion():
    monitor, posts = _monitor([st("printing"), st("standby")])
    assert monitor.run() == "complete"
    assert len(posts) == 1


def test_start_monitor_with_injected_parts_uses_the_given_thread_factory():
    ran = []
    monitor_thread = bambu_watch.start_monitor(
        FakeAdapter([st("printing")]), REMOTE,
        status_fn=scripted([st("complete")]), camera_fn=lambda: JPEG,
        post=lambda text: ran.append(text),
        thread_factory=lambda target: types.SimpleNamespace(start=lambda: target()))
    assert monitor_thread is not None
    assert len(ran) == 1 and "MEDIA:" in ran[0]


# -- Stage 3: the camera watchdog ----------------------------------------------

from print_handy.hermes import visual_watch as vw


def _watch_monitor(*, sensitivity=3, verdict=("ok", "looks fine"), changed=True):
    alerts = []
    clock = FakeClock()
    monitor = bambu_watch.BambuPrintMonitor(
        REMOTE, status_fn=scripted([st("complete")]), camera_fn=lambda: JPEG,
        post=lambda text: None,
        watch=vw.VisualPrintWatch(sensitivity=sensitivity),
        verdict=lambda image, prompt: verdict,
        alert=lambda filename, image, reason, identity=None: alerts.append((filename, image, reason)),
        changed=lambda before, after: changed,
        clock=clock, wall=clock, sleep=clock.sleep, poll_s=10)
    return monitor, alerts, clock


def test_watchdog_alerts_on_a_suspicious_verdict_with_the_snapshot():
    monitor, alerts, _clock = _watch_monitor(verdict=("spaghetti", "tangled mess under the nozzle"))
    monitor.poll_active(st("printing", layer=1))
    assert alerts == []
    monitor.poll_active(st("printing", layer=2))
    assert len(alerts) == 1
    filename, image, reason = alerts[0]
    assert filename == REMOTE and image == JPEG
    assert "layer2" in reason and "spaghetti" in reason


def test_watchdog_stays_quiet_on_a_healthy_verdict():
    monitor, alerts, _clock = _watch_monitor(verdict=("ok", "plastic on the bed"))
    monitor.poll_active(st("printing", layer=2))
    monitor.poll_active(st("printing", layer=5))
    assert alerts == []


def test_watchdog_never_controls_the_printer_itself():
    # The monitor is not even given a control callable: with a suspicious
    # verdict the only outward effects are the alert and (later) posts.
    monitor, alerts, _clock = _watch_monitor(verdict=("detached", "part on the floor"))
    assert not hasattr(monitor, "control")
    monitor.poll_active(st("printing", layer=2))
    assert len(alerts) == 1


def test_stall_alerts_at_level_5_even_with_a_healthy_verdict():
    # Layer 2 and layer 5 checks run first (baselines recorded); the next
    # advance over an unchanged frame is the layer_progress stall probe.
    monitor, alerts, _clock = _watch_monitor(sensitivity=5, verdict=("ok", "looks fine"), changed=False)
    monitor.poll_active(st("printing", layer=2))
    monitor.poll_active(st("printing", layer=5))
    monitor.poll_active(st("printing", layer=8))
    assert len(alerts) == 1 and "Print may have stalled" in alerts[0][2]
    # Stage 2 (#455 item 4): the Bambu surface renders the same
    # differential wording as the Moonraker listener.
    for possibility in ("printing in air", "blocked", "parked", "dark"):
        assert possibility in alerts[0][2], possibility
    assert "stalled_layers" not in alerts[0][2]


def test_stall_is_vetoed_by_a_healthy_verdict_at_level_1():
    monitor, alerts, _clock = _watch_monitor(sensitivity=1, verdict=("ok", "looks fine"), changed=False)
    monitor.poll_active(st("printing", layer=2))
    monitor.poll_active(st("printing", layer=5))
    monitor.poll_active(st("printing", layer=13))
    assert alerts == []


def test_periodic_checks_still_run_when_the_firmware_reports_no_layer():
    verdicts = []

    def verdict(image, prompt):
        verdicts.append(prompt)
        return "ok", "fine"

    clock = FakeClock()
    clock.t = 1000.0
    monitor = bambu_watch.BambuPrintMonitor(
        REMOTE, status_fn=scripted([st("complete")]), camera_fn=lambda: JPEG,
        post=lambda text: None,
        watch=vw.VisualPrintWatch(sensitivity=5),  # periodic_s = 450
        verdict=verdict, alert=lambda *args: None, changed=lambda a, b: True,
        clock=clock, wall=clock, sleep=clock.sleep, poll_s=10)
    no_layer = st("printing", layer=None)
    monitor.poll_active(no_layer)
    assert verdicts == []
    clock.t += 500
    monitor.poll_active(no_layer)
    assert len(verdicts) == 1


# -- #476: dark frames and the per-print mute --------------------------------

def _pil_jpeg(gray, size=(64, 48)):
    import io as _io2
    from PIL import Image
    buf = _io2.BytesIO()
    Image.new("L", size, gray).save(buf, "JPEG", quality=95)
    return buf.getvalue()


DARK_JPEG = _pil_jpeg(0)
BRIGHT_JPEG = _pil_jpeg(200)


def _dark_monitor(*, verdict=("ok", "looks fine")):
    alerts, notices = [], []
    clock = FakeClock()
    monitor = bambu_watch.BambuPrintMonitor(
        REMOTE, status_fn=scripted([st("complete")]), camera_fn=lambda: DARK_JPEG,
        post=lambda text: None,
        watch=vw.VisualPrintWatch(sensitivity=3),
        verdict=lambda image, prompt: (_ for _ in ()).throw(AssertionError("vision called for a dark frame"))
        if verdict == "explode" else verdict,
        alert=lambda filename, image, reason, identity=None: alerts.append((filename, image, reason)),
        dark_notice=lambda filename, identity=None: notices.append((filename, identity)),
        changed=lambda before, after: True,
        clock=clock, wall=clock, sleep=clock.sleep, poll_s=10)
    return monitor, alerts, notices


def test_dark_frame_posts_one_notice_and_never_alerts():
    monitor, alerts, notices = _dark_monitor(verdict="explode")
    monitor.visual_check("periodic", 1)
    monitor.visual_check("periodic", 2)
    assert alerts == []
    assert len(notices) == 1 and notices[0][0] == REMOTE and notices[0][1] == monitor._identity
    # The dark frames never became a stall baseline.
    assert monitor._last_frame is None
    assert monitor._watch.last_snapshot is None


def test_muted_bambu_print_posts_no_alert(monkeypatch, tmp_path):
    from unittest.mock import patch as _patch
    from print_handy.hermes import print_alert_buttons as pab
    alerts = []
    clock = FakeClock()
    monitor = bambu_watch.BambuPrintMonitor(
        REMOTE, status_fn=scripted([st("complete")]), camera_fn=lambda: BRIGHT_JPEG,
        post=lambda text: None,
        watch=vw.VisualPrintWatch(sensitivity=3),
        verdict=lambda image, prompt: ("spaghetti", "mess"),
        alert=lambda filename, image, reason, identity=None: alerts.append(reason),
        changed=lambda before, after: True,
        clock=clock, wall=clock, sleep=clock.sleep, poll_s=10)
    with _patch.object(pab, "STATE", tmp_path / "alerts.json"):
        pab.mute_visual(REMOTE, monitor._identity)
        monitor.visual_check("layer2", 2)
        # A different print (new identity, same filename) is NOT muted.
        other = bambu_watch.BambuPrintMonitor(
            REMOTE, status_fn=scripted([st("complete")]), camera_fn=lambda: BRIGHT_JPEG,
            post=lambda text: None,
            watch=vw.VisualPrintWatch(sensitivity=3),
            verdict=lambda image, prompt: ("spaghetti", "mess"),
            alert=lambda filename, image, reason, identity=None: alerts.append(reason),
            changed=lambda before, after: True,
            clock=clock, wall=clock, sleep=clock.sleep, poll_s=10)
        other._identity = monitor._identity + "-next"
        other.visual_check("layer2", 2)
    assert len(alerts) == 1  # only the unmuted print alerted


def test_snapshot_failure_alerts_text_only_instead_of_staying_silent():
    # Bonus (found in the DATA-5 lane): the Bambu mirror of the Moonraker
    # watchdog had P2-8's defect — when the camera snapshot itself
    # raises, image stays None and the failure was only logged, so the
    # print silently lost its watchdog. A text-only alert must go out.
    alerts, texts = [], []
    clock = FakeClock()
    monitor = bambu_watch.BambuPrintMonitor(
        REMOTE, status_fn=scripted([st("complete")]),
        camera_fn=lambda: (_ for _ in ()).throw(ConnectionError("camera unreachable")),
        post=lambda text: None,
        watch=vw.VisualPrintWatch(sensitivity=3),
        verdict=lambda image, prompt: ("ok", ""),
        alert=lambda filename, image, reason, identity=None: alerts.append(reason),
        text_alert=lambda filename, reason, identity=None: texts.append(reason),
        clock=clock, wall=clock, sleep=clock.sleep, poll_s=10)
    monitor.visual_check("layer2", 2)
    assert alerts == []  # no image exists to attach
    assert len(texts) == 1 and "camera" in texts[0]


def test_snapshot_failure_alerts_are_throttled_to_the_periodic_interval():
    # Once the camera dies after a baseline capture, the failed capture
    # never advances the watch's snapshot baseline, so due() offers a
    # layer_progress probe on every poll (~30 s). The camera-failure
    # alert must still respect the profile's periodic interval (900 s at
    # sensitivity 3): repeated failed probes inside it post once.
    texts = []
    clock = FakeClock()
    camera = {"fail": False}

    def camera_fn():
        if camera["fail"]:
            raise ConnectionError("camera unreachable")
        return JPEG

    monitor = bambu_watch.BambuPrintMonitor(
        REMOTE, status_fn=scripted([st("complete")]), camera_fn=camera_fn,
        post=lambda text: None,
        watch=vw.VisualPrintWatch(sensitivity=3),
        verdict=lambda image, prompt: ("ok", "fine"),
        alert=lambda filename, image, reason, identity=None: (_ for _ in ()).throw(
            AssertionError("image alert for a failed snapshot")),
        text_alert=lambda filename, reason, identity=None: texts.append(reason),
        changed=lambda before, after: True,
        clock=clock, wall=clock, sleep=clock.sleep, poll_s=10)
    monitor.poll_active(st("printing", layer=2))
    monitor.poll_active(st("printing", layer=5))
    assert texts == []
    camera["fail"] = True
    for layer in (8, 8, 9, 10):
        clock.t += 30  # one status poll
        monitor.poll_active(st("printing", layer=layer))
    assert len(texts) == 1 and "camera" in texts[0]
    clock.t += 900  # the periodic interval elapses with the camera still down
    monitor.poll_active(st("printing", layer=11))
    assert len(texts) == 2
    # Probes were never throttled, only alerts: a recovered camera is
    # picked up on the very next poll, inside the interval.
    camera["fail"] = False
    clock.t += 30
    monitor.poll_active(st("printing", layer=14))
    assert len(texts) == 2


def test_dark_notice_delivery_posts_only_the_mute_button(monkeypatch):
    from print_handy.hermes import print_alert_buttons as pab
    seen = {}
    monkeypatch.setattr(pab, "post_visual_dark_notice",
                        lambda token, chat, text, filename, identity=None: seen.update(
                            text=text, filename=filename, identity=identity) or True)
    bambu_watch.deliver_dark_notice(REMOTE, post=lambda text: (_ for _ in ()).throw(AssertionError("fallback used")),
                                    token="tok", chat_id="42", buttonless=None, identity="bambu:job")
    assert seen["filename"] == REMOTE and seen["identity"] == "bambu:job"
    assert "completely dark" in seen["text"]


def test_visual_alert_delivery_posts_buttons_like_the_moonraker_alert(monkeypatch):
    from print_handy.hermes import print_alert_buttons as pab
    seen = {}
    monkeypatch.setattr(pab, "post_visual_alert",
                        lambda token, chat, text, filename, path, identity=None: seen.update(
                            token=token, chat=chat, text=text, filename=filename, path=path) or True)
    path = bambu_watch.deliver_visual_alert(REMOTE, JPEG, "layer2: spaghetti; test",
                                            post=lambda text: (_ for _ in ()).throw(AssertionError("fallback used")),
                                            token="tok", chat_id="42", buttonless=None)
    assert seen["path"] == path and seen["filename"] == REMOTE
    assert "Print camera check" in seen["text"]


def test_visual_alert_delivery_buttonless_posts_plain_media(monkeypatch):
    posts = []
    path = bambu_watch.deliver_visual_alert(REMOTE, JPEG, "periodic: uncertain; test",
                                            post=posts.append, token="tok", chat_id="42", buttonless="telegram")
    assert len(posts) == 1 and f"MEDIA:{path}" in posts[0]


def test_text_alert_delivery_uses_the_real_button_helper(monkeypatch, tmp_path):
    # deliver_visual_text_alert must reach print_alert_buttons'
    # post_visual_text_alert itself — a missing helper raises
    # AttributeError there and the alert loses its buttons — so this
    # test stubs only the Discord transport under the real helper.
    import json as _json
    from unittest.mock import patch as _patch
    from print_handy.hermes import print_alert_buttons as pab
    seen = {}

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False

    def fake_open(request, timeout):
        seen["request"] = request
        return Response()

    with _patch.object(pab, "STATE", tmp_path / "alerts.json"), \
         _patch.object(pab.urllib.request, "urlopen", fake_open):
        bambu_watch.deliver_visual_text_alert(
            REMOTE, "layer_progress at layer 8: the camera snapshot could not be captured",
            post=lambda text: (_ for _ in ()).throw(AssertionError("fallback used")),
            token="tok", chat_id="42", buttonless=False, identity="bambu:job")
    body = _json.loads(seen["request"].data)
    labels = [b["label"] for row in body["components"] for b in row["components"]]
    assert labels == ["Pause", "It's fine", "Stop asking for this print"]
    assert "camera snapshot could not be captured" in body["content"]


def test_text_alert_delivery_buttonless_posts_plain_text():
    posts = []
    bambu_watch.deliver_visual_text_alert(REMOTE, "periodic: camera down", post=posts.append,
                                          token="tok", chat_id="42", buttonless="telegram")
    assert len(posts) == 1 and "Print camera check" in posts[0] and "MEDIA:" not in posts[0]


def test_text_alert_delivery_falls_back_to_plain_text_when_the_button_post_fails(monkeypatch):
    from print_handy.hermes import print_alert_buttons as pab
    monkeypatch.setattr(pab, "post_visual_text_alert", lambda *args, **kwargs: False)
    posts = []
    bambu_watch.deliver_visual_text_alert(REMOTE, "periodic: camera down", post=posts.append,
                                          token="tok", chat_id="42", buttonless=False)
    assert len(posts) == 1 and "Print camera check" in posts[0]


# -- watchdog ping release: a failed delivery must not eat the ping -----------

_WATCH_STARTER = "111222333444555666"


def _watchdog_ping_rig(monkeypatch, tmp_path):
    """Real starter-pings state in a tmp file, with the job resolvable."""
    from print_handy.hermes import discord_print_summary as summary
    from print_handy.hermes import starter_pings
    job = {"title": "Widget", "gcode": "/archive/widget/plate.gcode",
           "started_by": {"platform": "discord", "user_id": _WATCH_STARTER}}
    monkeypatch.setattr(starter_pings, "STATE", tmp_path / "starter_pings.json")
    monkeypatch.setattr(starter_pings, "mentions_on", lambda: True)
    monkeypatch.setattr(summary, "find_job", lambda key: (job, None))
    return starter_pings


def test_visual_alert_releases_the_ping_when_every_delivery_fails(monkeypatch, tmp_path):
    """Button post and plain fallback both failing means the recorded
    watchdog ping never landed: it is released, so a later watchdog
    alert for the same print can ping instead of arriving silently."""
    from print_handy.hermes import moonraker_event_listener as mel
    from print_handy.hermes import print_alert_buttons as pab
    _watchdog_ping_rig(monkeypatch, tmp_path)
    monkeypatch.setattr(pab, "post_visual_alert", lambda *args, **kwargs: False)
    delivered = []
    monkeypatch.setattr(mel, "post", lambda text, **kw: delivered.append(kw) or False)
    bambu_watch.deliver_visual_alert(REMOTE, JPEG, "layer2: spaghetti; test",
                                     token="tok", chat_id="42", buttonless=False)
    assert delivered[-1] == {"mention_user_id": _WATCH_STARTER}
    monkeypatch.setattr(mel, "post", lambda text, **kw: delivered.append(kw) or True)
    bambu_watch.deliver_visual_alert(REMOTE, JPEG, "layer2: spaghetti; test",
                                     token="tok", chat_id="42", buttonless=False)
    assert delivered[-1] == {"mention_user_id": _WATCH_STARTER}


def test_visual_text_alert_releases_the_ping_when_every_delivery_fails(monkeypatch, tmp_path):
    from print_handy.hermes import moonraker_event_listener as mel
    from print_handy.hermes import print_alert_buttons as pab
    _watchdog_ping_rig(monkeypatch, tmp_path)
    monkeypatch.setattr(pab, "post_visual_text_alert", lambda *args, **kwargs: False)
    delivered = []
    monkeypatch.setattr(mel, "post", lambda text, **kw: delivered.append(kw) or False)
    bambu_watch.deliver_visual_text_alert(REMOTE, "periodic: camera down",
                                          token="tok", chat_id="42", buttonless=False)
    assert delivered[-1] == {"mention_user_id": _WATCH_STARTER}
    monkeypatch.setattr(mel, "post", lambda text, **kw: delivered.append(kw) or True)
    bambu_watch.deliver_visual_text_alert(REMOTE, "periodic: camera down",
                                          token="tok", chat_id="42", buttonless=False)
    assert delivered[-1] == {"mention_user_id": _WATCH_STARTER}


def test_visual_alert_keeps_the_episode_when_the_fallback_delivers(monkeypatch, tmp_path):
    """A fallback that lands is a delivered ping: the episode stays
    recorded, so the next watchdog alert for the print does not ping
    a second time."""
    from print_handy.hermes import moonraker_event_listener as mel
    from print_handy.hermes import print_alert_buttons as pab
    _watchdog_ping_rig(monkeypatch, tmp_path)
    monkeypatch.setattr(pab, "post_visual_alert", lambda *args, **kwargs: False)
    delivered = []
    monkeypatch.setattr(mel, "post", lambda text, **kw: delivered.append(kw) or True)
    bambu_watch.deliver_visual_alert(REMOTE, JPEG, "layer2: spaghetti; test",
                                     token="tok", chat_id="42", buttonless=False)
    assert delivered[-1] == {"mention_user_id": _WATCH_STARTER}
    button_kwargs = []
    monkeypatch.setattr(pab, "post_visual_alert",
                        lambda *args, **kw: button_kwargs.append(kw) or True)
    bambu_watch.deliver_visual_alert(REMOTE, JPEG, "layer2: spaghetti; test",
                                     token="tok", chat_id="42", buttonless=False)
    assert "mention_user_id" not in button_kwargs[-1]


# -- Stage 3: the alert's Pause button on Bambu ---------------------------------

def test_pause_sends_one_lan_pause_for_the_alerted_file(monkeypatch):
    from unittest import mock
    adapter, _client = _bambu_pause(monkeypatch, [bst2("printing"), bst2("paused")])
    with mock.patch.object(print_stop.time, "sleep"):
        sent, text = print_stop.pause("plate_1.gcode.3mf", wait_s=5)
    assert sent and "Paused" in text
    adapter.control.assert_called_once_with("pause")


def test_pause_refuses_a_different_file_without_sending(monkeypatch):
    from unittest import mock
    adapter, _client = _bambu_pause(monkeypatch, [bst2("printing", filename="other.gcode.3mf")])
    sent, text = print_stop.pause("plate_1.gcode.3mf")
    assert not sent and "different print" in text
    adapter.control.assert_not_called()


def test_handle_click_visual_pause_routes_to_the_bambu_pause(monkeypatch):
    from unittest import mock
    from print_handy.hermes import print_alert_buttons as pab
    adapter, _client = _bambu_pause(monkeypatch, [bst2("printing"), bst2("paused")])
    with mock.patch.object(print_stop.time, "sleep"):
        text = pab.handle_click("pause", {"kind": "visual", "filename": "plate_1.gcode.3mf"})
    assert "Paused" in text
    adapter.control.assert_called_once_with("pause")


def bst2(state, filename="plate_1.gcode.3mf", layer=1, total=513, progress=0.01):
    return {"state": state, "filename": filename, "layer": layer, "total_layer": total, "progress": progress}


def _bambu_pause(monkeypatch, snapshots):
    """Route print_stop through a fake Bambu LAN adapter, as in test_print_stop.py."""
    from unittest import mock
    from print_handy import printers
    client = mock.Mock()
    client.snapshot.side_effect = list(snapshots)
    adapter = types.SimpleNamespace(
        transport="bambulab_lan", lan_configured=lambda: True, client=lambda: client,
        normalize_status=lambda raw: raw, control=mock.Mock(),
        camera_snapshot=mock.Mock(side_effect=BambuLanError("no camera in this fake")))
    monkeypatch.setattr(printers, "current", lambda: adapter)
    return adapter, client


# -- #295: the timelapse capture session in the monitor -------------------------

from pathlib import Path as _Path

from print_handy.printers.bambulab.client import BambuLanError as _LanError

SOURCE_URL = "bambu-tcp6000://192.168.1.50"


class FakeSession:
    def __init__(self, frames=3, failures=0, dark_frames=0):
        self.frames = frames
        self.failures = failures
        self.dark_frames = dark_frames
        self.ticks = 0
        self.tick_layers = []
        self.stopped = None
        self.filename = REMOTE
        self.frames_dir = _Path("/unused/frames")
        self.directory = _Path("/unused")

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


class CaptureRig:
    """A monitor wired for capture; every collaborator is a fake."""

    def __init__(self, statuses, *, source=(SOURCE_URL, "fetch"), session="default",
                 encode=None, camera=JPEG):
        self.posts = []
        self.begins = []
        self.encodes = []
        self.logs = []
        self.session = FakeSession() if session == "default" else session
        self.clock = FakeClock()
        self.monitor = bambu_watch.BambuPrintMonitor(
            REMOTE,
            status_fn=scripted(statuses),
            camera_fn=lambda: (_ for _ in ()).throw(camera) if isinstance(camera, Exception) else camera,
            post=self.posts.append,
            camera_source_fn=scripted([source]),
            begin_fn=self._begin,
            encode_fn=encode or self._encode,
            clock=self.clock, wall=self.clock, sleep=self.clock.sleep,
            poll_s=10, capture_poll_s=2)

    def _begin(self, url, fetch):
        self.begins.append((url, fetch))
        return self.session

    def _encode(self, session):
        self.encodes.append(session)
        return _Path("/tmp/timelapse.mp4")


@pytest.fixture
def capture_logs(monkeypatch):
    lines = []
    monkeypatch.setattr(bambu_watch, "log", lines.append)
    return lines


def test_timelapse_attaches_alongside_the_final_snapshot(capture_logs):
    rig = CaptureRig([st("printing"), st("printing"), st("complete")])
    assert rig.monitor.run() == "complete"
    assert rig.begins == [(SOURCE_URL, "fetch")]  # opened exactly once
    assert rig.session.ticks > 0
    assert rig.session.stopped == "complete"
    assert rig.encodes == [rig.session]
    assert len(rig.posts) == 1
    assert rig.posts[0].startswith("Print complete")
    assert rig.posts[0].count("MEDIA:") == 2  # the snapshot and the video
    assert "MEDIA:/tmp/timelapse.mp4" in rig.posts[0]


def test_capture_ticks_carry_the_reported_layer(capture_logs):
    # capture_trigger=layer can only fire when the session sees the layer
    # change: every tick — the status-poll tick and the ticks inside the
    # between-polls sleep — must carry the last layer the printer
    # reported (Bambu's normalized status supplies it as st["layer"]).
    rig = CaptureRig([st("printing", layer=3), st("printing", layer=4), st("complete", layer=4)])
    assert rig.monitor.run() == "complete"
    assert rig.session.ticks > 0
    assert all(layer is not None for layer in rig.session.tick_layers)
    assert 3 in rig.session.tick_layers and 4 in rig.session.tick_layers


def test_timelapse_on_a_cancelled_print_stops_as_cancelled(capture_logs):
    rig = CaptureRig([st("printing"), st("cancelled")])
    assert rig.monitor.run() == "cancelled"
    assert rig.session.stopped == "cancelled"
    assert "MEDIA:/tmp/timelapse.mp4" in rig.posts[0]


def test_timelapse_on_an_errored_print_stops_as_error(capture_logs):
    rig = CaptureRig([st("printing"), st("error")])
    assert rig.monitor.run() == "error"
    assert rig.session.stopped == "error"


def test_capture_refused_keeps_the_pre_timelapse_completion_post(capture_logs):
    rig = CaptureRig([st("printing"), st("complete")], session=None)
    rig.session = None
    rig.monitor._begin_fn = lambda url, fetch: rig.begins.append((url, fetch)) or None
    assert rig.monitor.run() == "complete"
    assert len(rig.posts) == 1 and rig.posts[0].count("MEDIA:") == 1  # snapshot only
    assert any("capture unavailable" in line for line in capture_logs)


def test_camera_source_failure_is_named_and_never_blocks_the_post(capture_logs):
    reason = _LanError("X1 camera stills come from the printer's RTSPS stream through "
                       "go2rtc, and go2rtc is not running for this printer")
    rig = CaptureRig([st("printing"), st("complete")], source=reason, camera=reason)
    rig.monitor._camera_source_fn = lambda: (_ for _ in ()).throw(reason)
    assert rig.monitor.run() == "complete"
    assert rig.begins == []
    assert len(rig.posts) == 1 and "MEDIA:" not in rig.posts[0]  # text-only, still posted
    assert any("go2rtc is not running" in line for line in capture_logs)


def test_zero_frames_posts_the_snapshot_without_encoding(capture_logs):
    rig = CaptureRig([st("printing"), st("complete")], session=FakeSession(frames=0, failures=2))
    assert rig.monitor.run() == "complete"
    assert rig.encodes == []
    assert rig.posts[0].count("MEDIA:") == 1
    assert any("has no frames" in line for line in capture_logs)


def test_all_dark_session_names_the_camera_dark_outcome(capture_logs):
    rig = CaptureRig([st("printing"), st("complete")],
                     session=FakeSession(frames=0, failures=0, dark_frames=4))
    assert rig.monitor.run() == "complete"
    assert rig.encodes == []
    assert ("Timelapse not attached: the camera image was completely dark"
            in rig.posts[0])
    assert any("completely dark" in line for line in capture_logs)


def test_encode_failure_degrades_to_the_snapshot_post(capture_logs):
    def encode(session):
        raise RuntimeError("ffmpeg died")

    rig = CaptureRig([st("printing"), st("complete")], encode=encode)
    assert rig.monitor.run() == "complete"
    assert rig.posts[0].count("MEDIA:") == 1
    assert any("encode failed" in line for line in capture_logs)


def test_oversize_timelapse_posts_the_recorded_but_too_large_line(monkeypatch, capture_logs):
    monkeypatch.setattr(bambu_watch, "_too_big_text", lambda: "RECORDED BUT TOO LARGE")
    rig = CaptureRig([st("printing"), st("complete")], encode=lambda session: None)
    assert rig.monitor.run() == "complete"
    assert "RECORDED BUT TOO LARGE" in rig.posts[0]
    assert "timelapse.mp4" not in rig.posts[0]


def test_video_post_refusal_falls_back_to_the_snapshot_post(capture_logs):
    posts = []

    def post(text):
        posts.append(text)
        if "timelapse.mp4" in text:
            raise RuntimeError("chat refused the video post")

    rig = CaptureRig([st("printing"), st("complete")])
    rig.monitor._post = post
    assert rig.monitor.run() == "complete"
    assert len(posts) == 2 and "timelapse.mp4" not in posts[1] and "MEDIA:" in posts[1]


def test_pause_holds_the_session_without_taking_frames(capture_logs):
    rig = CaptureRig([st("paused"), st("paused"), st("complete")])
    assert rig.monitor.run() == "complete"
    assert rig.session is not None and rig.session.ticks == 0
    assert rig.session.stopped == "complete"


def test_superseded_print_stops_the_session_and_posts_nothing(capture_logs):
    rig = CaptureRig([st("printing"), st("printing", filename="other.gcode.3mf")])
    assert rig.monitor.run() is None
    assert rig.session.stopped == "superseded"
    assert rig.posts == []


def test_status_read_failure_costs_a_poll_never_the_session(capture_logs):
    statuses = scripted([st("printing"), RuntimeError("mqtt dropped"), st("complete")])

    def status_fn():
        value = statuses()
        if isinstance(value, Exception):
            raise value
        return value

    rig = CaptureRig([st("complete")])
    rig.monitor._status_fn = status_fn
    assert rig.monitor.run() == "complete"
    assert rig.session.stopped == "complete"
    assert len(rig.posts) == 1


def test_monitor_without_a_camera_source_never_captures():
    # The pre-#295 construction: no camera_source_fn, so no session is
    # ever attempted and the completion post is exactly the old one.
    posts = []
    clock = FakeClock()
    begins = []
    monitor = bambu_watch.BambuPrintMonitor(
        REMOTE, status_fn=scripted([st("printing"), st("complete")]),
        camera_fn=lambda: JPEG, post=posts.append,
        begin_fn=lambda url, fetch: begins.append((url, fetch)),
        clock=clock, wall=clock, sleep=clock.sleep, poll_s=10)
    assert monitor.run() == "complete"
    assert begins == []
    assert len(posts) == 1 and posts[0].count("MEDIA:") == 1


def test_start_monitor_wires_the_adapters_frame_source():
    ran = []
    adapter = FakeAdapter([st("printing"), st("complete")])
    adapter.camera_frame_source = lambda: (SOURCE_URL, lambda _url: JPEG)
    thread = bambu_watch.start_monitor(
        adapter, REMOTE, post=ran.append,
        thread_factory=lambda target: types.SimpleNamespace(start=lambda: target()))
    assert thread is not None
    assert len(ran) == 1 and "MEDIA:" in ran[0]


# -- the full lifecycle: real session, real JPEG stills, real ffmpeg -------------

import io as _io
import shutil as _shutil
import subprocess as _subprocess

import pytest as _pytest
from PIL import Image as _Image

from print_handy.hermes import moonraker_event_listener as _mel
from print_handy.hermes import timelapse_capture as _capture

_FFMPEG = _shutil.which("ffmpeg")
_FFPROBE = _shutil.which("ffprobe")


def _jpeg_bytes(colour):
    out = _io.BytesIO()
    _Image.new("RGB", (320, 240), colour).save(out, format="JPEG")
    return out.getvalue()


@_pytest.mark.skipif(not (_FFMPEG and _FFPROBE), reason="needs ffmpeg and ffprobe on PATH")
def test_full_lifecycle_produces_a_valid_h264_mp4_under_the_limit(tmp_path, monkeypatch):
    stills = scripted([_jpeg_bytes(colour) for colour in ("red", "green", "blue")])
    clock = FakeClock()
    root = tmp_path / "cache" / "timelapse"

    def begin_fn(url, fetch):
        session = _capture.CaptureSession(
            root, REMOTE, REMOTE,
            trigger=_capture.make_trigger("interval", 0.5),
            max_frames=100, max_bytes=0, url=url,
            fetch=fetch, log=lambda _line: None, now=clock)
        assert session.start()
        return session

    monkeypatch.setattr(_mel, "_media_upload_limit", lambda: 500_000)
    posts = []
    monitor = bambu_watch.BambuPrintMonitor(
        REMOTE,
        status_fn=scripted([st("printing"), st("printing"), st("printing"), st("complete")]),
        camera_fn=lambda: JPEG,
        post=posts.append,
        camera_source_fn=lambda: (SOURCE_URL, lambda _url: stills()),
        begin_fn=begin_fn,
        clock=clock, wall=clock, sleep=clock.sleep, poll_s=10, capture_poll_s=2)
    assert monitor.run() == "complete"
    assert len(posts) == 1 and posts[0].count("MEDIA:") == 2
    video = _Path(posts[0].rsplit("MEDIA:", 1)[1].strip())
    assert video.is_file() and 0 < video.stat().st_size <= 500_000
    probe = _subprocess.run([_FFPROBE, "-v", "error", "-select_streams", "v:0",
                             "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                             str(video)], capture_output=True, text=True, check=True)
    assert probe.stdout.strip() == "h264"
    session_dir = next(root.iterdir())
    frames = list((session_dir / "frames").glob("frame-*.jpg"))
    assert len(frames) >= 2
    assert frames[0].read_bytes().startswith(b"\xff\xd8")


def test_a_refused_video_post_falls_back_to_the_snapshot_post(capture_logs):
    """P2-173: the production sender returns False on a refusal instead of
    raising. A refused video post must not end delivery — the monitor
    continues to the snapshot post without the video."""
    rig = CaptureRig([st("printing"), st("complete")])
    attempts = []

    def post(message):
        attempts.append(message)
        return "timelapse.mp4" not in message

    rig.monitor._post = post
    assert rig.monitor.run() == "complete"
    assert len(attempts) == 2
    assert "MEDIA:/tmp/timelapse.mp4" in attempts[0]
    assert "timelapse.mp4" not in attempts[1] and "MEDIA:" in attempts[1]


def test_a_refused_snapshot_post_falls_back_to_the_text_completion(capture_logs):
    """P2-173: the same False contract on the snapshot fallback post —
    a refusal there still owes the chat the plain completion text."""
    monitor, _posts = _monitor([st("printing"), st("complete")])
    attempts = []

    def post(message):
        attempts.append(message)
        return "MEDIA:" not in message

    monitor._post = post
    assert monitor.run() == "complete"
    assert len(attempts) == 2
    assert "MEDIA:" in attempts[0]
    assert "MEDIA:" not in attempts[1]
