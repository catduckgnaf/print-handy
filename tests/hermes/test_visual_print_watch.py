"""Offline tests for the visual watchdog trigger policy."""
import sys
from pathlib import Path

import pytest

from print_handy.hermes import moonraker_event_listener as mel
from print_handy.hermes import print_alert_buttons as pab


def test_strip_json_fence_and_profile_adapter(monkeypatch):
    import json
    assert mel._strip_json_fence('```json\n{"verdict":"empty_idle","reason":"bed clear"}\n```') == '{"verdict":"empty_idle","reason":"bed clear"}'

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return json.dumps({"choices": [{"message": {"content": '```json\n{"verdict":"empty_idle","reason":"idle bed"}\n```'}}]}).encode()

    seen = {}

    def fake_open(req, timeout=0):
        seen["body"] = json.loads(req.data)
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr(mel, "_profile_env_value", lambda name: "test-key")
    monkeypatch.setattr(mel.urllib.request, "urlopen", fake_open)
    verdict, reason = mel._visual_verdict(b"frame", "idle snapshot")
    assert verdict == "empty_idle" and reason == "idle bed"
    assert seen["body"]["temperature"] == 0
    assert seen["body"]["model"] == "gemini-latest"
    assert "Flashforge AD5X" in seen["body"]["messages"][0]["content"][0]["text"]
    assert "empty_idle" not in mel.VISUAL_ALERT_VERDICTS
    assert mel.VISUAL_ALERT_VERDICTS == {"air", "spaghetti", "detached", "uncertain"}


def test_visual_alert_posts_image_and_components_together(monkeypatch, tmp_path):
    frame_dir = tmp_path / "frames"
    seen = {}

    def posted(token, channel_id, content, filename, image, identity=None):
        seen.update(token=token, channel_id=channel_id, content=content, filename=filename, image=image)
        return True

    monkeypatch.setenv("PRINT_WATCH_FRAME_DIR", str(frame_dir))
    monkeypatch.setattr(mel, "_profile_env_value", lambda name: "test-token")
    monkeypatch.setattr(mel.announcer, "DISCORD_CHAT_ID", "42")
    monkeypatch.setattr(pab, "post_visual_alert", posted)
    monkeypatch.setattr(mel, "post", lambda text: (_ for _ in ()).throw(AssertionError("plain fallback used")))
    path = mel._visual_alert("job.gcode", _jpeg(), "stalled_layers at layer 48")
    assert path == seen["image"] and path.read_bytes() == _jpeg()
    assert seen["filename"] == "job.gcode" and "Buttons:" not in seen["content"]


def test_visual_alert_plain_fallback_has_no_fake_buttons(monkeypatch, tmp_path):
    monkeypatch.setenv("PRINT_WATCH_FRAME_DIR", str(tmp_path / "frames"))
    monkeypatch.setattr(mel, "_profile_env_value", lambda name: "test-token")
    monkeypatch.setattr(pab, "post_visual_alert", lambda *args: False)
    posted = []
    monkeypatch.setattr(mel, "post", posted.append)
    mel._visual_alert("job.gcode", _jpeg(), "stalled_layers at layer 48")
    assert posted and "Buttons:" not in posted[0] and "MEDIA:" in posted[0]


def test_layer_two_empty_bed_requests_check_and_alert_on_air(monkeypatch):
    watch = mel.VisualPrintWatch()
    calls = []
    monkeypatch.setattr(mel, "_visual_verdict", lambda image, prompt, streak=None: ("air", "empty bed"))
    monkeypatch.setattr(mel, "_visual_alert", lambda filename, image, reason, identity=None: calls.append(reason))
    assert watch.due(1, 10) is None
    assert watch.due(2, 11) == "layer2"
    watch.record(2, 11, b"frame")
    verdict, detail = mel._visual_verdict(b"frame", "is there plastic?")
    if verdict in {"air", "no", "unsafe", "uncertain"}:
        mel._visual_alert("test.gcode", b"frame", f"layer2: {verdict}; {detail}")
    assert calls and "air" in calls[0]


def test_layer_progression_without_visual_change_triggers_alert():
    watch = mel.VisualPrintWatch()
    watch.layer2_checked = watch.layer5_checked = True
    watch.record(5, 100, b"frame")
    assert watch.due(9, 101, snapshot_changed=False) == "stalled_layers"
    assert watch.due(12, 102, snapshot_changed=False) is None


def test_layer_progression_after_normal_snapshot_does_not_false_trigger():
    watch = mel.VisualPrintWatch()
    watch.layer2_checked = watch.layer5_checked = True
    watch.record(5, 100, b"frame")
    assert watch.due(6, 101, snapshot_changed=True) is None
    watch.record(6, 102, b"changed-frame")
    assert watch.due(9, 103, snapshot_changed=True) is None


def test_normal_print_waits_for_periodic_interval():
    watch = mel.VisualPrintWatch()
    watch.layer2_checked = watch.layer5_checked = True
    watch.record(5, 100, b"frame")
    assert watch.due(6, 200, snapshot_changed=True) is None
    assert watch.due(8, 1001, snapshot_changed=True) == "periodic"


def test_layer_two_trigger_only_once():
    watch = mel.VisualPrintWatch()
    assert watch.due(2, 10) == "layer2"
    assert watch.due(3, 20) is None


def _jpeg():
    return bytes((0xff, 0xd8, 0xff, 0xd9))


def test_session_due_signature_schedules_stall_probe():
    """The live loop calls due(layer, now) with no snapshot flag."""
    watch = mel.VisualPrintWatch()
    watch.layer2_checked = watch.layer5_checked = True
    watch.record(5, 100, _jpeg())
    assert watch.due(8, 200) == "layer_progress"
    assert watch.due(9, 201) == "layer_progress"
    assert watch.due(6, 250) is None


def test_visual_check_alerts_on_air_uncertain_and_endpoint_error(monkeypatch):
    import asyncio

    alerts = []
    monkeypatch.setattr(mel, "_camera_snapshot_bytes", lambda: _jpeg())
    monkeypatch.setattr(mel, "_visual_alert", lambda filename, image, reason, identity=None: alerts.append(reason) or reason)

    async def run(verdict):
        monkeypatch.setattr(mel, "_visual_verdict", lambda image, prompt, streak=None: verdict)
        listener = mel.Listener()
        listener.filename = "bin.gcode"
        await listener.visual_check("layer2", 2)

    asyncio.run(run(("air", "empty bed")))
    asyncio.run(run(("uncertain", "model unsure")))

    def boom(image, prompt, streak=None):
        raise OSError("endpoint down")

    async def run_boom():
        monkeypatch.setattr(mel, "_visual_verdict", boom)
        listener = mel.Listener()
        listener.filename = "bin.gcode"
        await listener.visual_check("layer2", 2)

    asyncio.run(run_boom())
    assert any("air" in item for item in alerts)
    assert any("uncertain" in item for item in alerts)
    assert any("the vision check could not be completed" in item for item in alerts)
    assert all("endpoint down" not in item and "OSError" not in item for item in alerts)  # raw detail demoted (#467)


def test_visual_check_alerts_text_only_when_the_snapshot_itself_fails(monkeypatch, tmp_path):
    # The camera is down: _camera_snapshot_bytes raises, so there is no
    # image to attach — but the operator must still be told the watchdog
    # is blind, not just see a log line (P2-8).
    import asyncio
    from unittest.mock import patch
    from print_handy.hermes import print_alert_buttons as pab

    alerts, texts = [], []

    def boom():
        raise ConnectionError("camera unreachable")

    monkeypatch.setattr(mel, "_camera_snapshot_bytes", boom)
    monkeypatch.setattr(mel, "_visual_alert",
                        lambda filename, image, reason, identity=None: alerts.append(reason))
    monkeypatch.setattr(mel, "_visual_alert_text",
                        lambda filename, reason, identity=None: texts.append(reason), raising=False)

    async def run():
        listener = mel.Listener()
        listener.filename = "blind.gcode"
        with patch.object(pab, "STATE", tmp_path / "alerts.json"):
            await listener.visual_check("layer2", 2)

    asyncio.run(run())
    assert alerts == []  # no image exists to attach
    assert len(texts) == 1 and "camera" in texts[0]


def test_visual_check_snapshot_failure_respects_a_muted_print(monkeypatch, tmp_path):
    import asyncio
    from unittest.mock import patch
    from print_handy.hermes import print_alert_buttons as pab

    texts = []

    def boom():
        raise ConnectionError("camera unreachable")

    monkeypatch.setattr(mel, "_camera_snapshot_bytes", boom)
    monkeypatch.setattr(mel, "_visual_alert_text",
                        lambda filename, reason, identity=None: texts.append(reason), raising=False)

    async def run():
        listener = mel.Listener()
        listener.filename = "blind.gcode"
        with patch.object(pab, "STATE", tmp_path / "alerts.json"):
            pab.mute_visual("blind.gcode", listener._visual_identity())
            await listener.visual_check("layer2", 2)

    asyncio.run(run())
    assert texts == []


def test_visual_check_promotes_unchanged_layers_and_ignores_motion(monkeypatch):
    import asyncio

    alerts = []
    frames = {"n": 0}

    def snapshot():
        frames["n"] += 1
        return _jpeg() + bytes((frames["n"],))

    monkeypatch.setattr(mel, "_camera_snapshot_bytes", snapshot)
    monkeypatch.setattr(mel, "_visual_alert", lambda filename, image, reason, identity=None: alerts.append(reason))
    monkeypatch.setattr(mel, "_visual_verdict", lambda image, prompt, streak=None: ("uncertain", "nozzle hidden"))

    async def unchanged():
        listener = mel.Listener()
        listener.filename = "air.gcode"
        listener.visual_watch.layer2_checked = True
        listener.visual_watch.layer5_checked = True
        listener.visual_frames["air.gcode"] = _jpeg() + bytes((1,))
        listener.visual_watch.record(5, 100, listener.visual_frames["air.gcode"])
        monkeypatch.setattr(mel, "_frame_changed", lambda before, after: False)
        reason = listener.visual_watch.due(9, 200)
        assert reason == "layer_progress"
        await listener.visual_check(reason, 9)
        return listener

    listener = asyncio.run(unchanged())
    assert alerts and "stalled_layers" in alerts[0]
    assert listener.visual_watch.alerted_stall_layer == 9

    alerts.clear()

    async def moving():
        listener = mel.Listener()
        listener.filename = "ok.gcode"
        listener.visual_watch.layer2_checked = True
        listener.visual_watch.layer5_checked = True
        listener.visual_frames["ok.gcode"] = _jpeg()
        listener.visual_watch.record(5, 100, _jpeg())
        monkeypatch.setattr(mel, "_frame_changed", lambda before, after: True)
        reason = listener.visual_watch.due(9, 200)
        await listener.visual_check(reason, 9)
        assert listener.visual_watch.due(10, 250) is None
        assert listener.visual_watch.due(12, 260) == "layer_progress"

    asyncio.run(moving())
    assert alerts == []


def test_sensitivity_three_is_the_original_behavior():
    profile = mel.VISUAL_SENSITIVITY[3]
    assert profile == {"diff": 3.0, "stall_layers": 3, "periodic_s": 900, "vision_veto": False}
    assert mel.VisualPrintWatch().sensitivity == 3  # home.toml default


def test_sensitivity_scales_stall_layers():
    for level, needed in ((1, 8), (2, 5), (3, 3), (4, 3), (5, 2)):
        watch = mel.VisualPrintWatch(level)
        watch.layer2_checked = watch.layer5_checked = True
        watch.record(5, 100, b"frame")
        assert watch.due(5 + needed - 1, 101, snapshot_changed=False) is None, level
        assert watch.due(5 + needed, 102, snapshot_changed=False) == "stalled_layers", level


def test_sensitivity_scales_periodic_interval():
    for level, seconds in ((1, 1800), (3, 900), (5, 450)):
        watch = mel.VisualPrintWatch(level)
        watch.layer2_checked = watch.layer5_checked = True
        watch.record(5, 0, b"frame")
        assert watch.due(6, seconds - 1, snapshot_changed=True) is None, level
        assert watch.due(6, seconds, snapshot_changed=True) == "periodic", level


def test_sensitivity_rejects_out_of_range():
    import pytest
    for bad in (0, 6):
        with pytest.raises(ValueError):
            mel.VisualPrintWatch(bad)


def test_frame_change_threshold_follows_sensitivity(monkeypatch):
    import io
    from PIL import Image

    def frame(gray):
        buf = io.BytesIO()
        Image.new("L", (64, 48), gray).save(buf, "JPEG", quality=100)
        return buf.getvalue()

    before, after = frame(100), frame(103)  # mean difference of about 3
    assert mel._frame_changed(before, after, threshold=mel.VISUAL_SENSITIVITY[1]["diff"]) is True
    assert mel._frame_changed(before, after, threshold=mel.VISUAL_SENSITIVITY[5]["diff"]) is False


def _stall_alerts(monkeypatch, level, verdict):
    import asyncio

    alerts = []
    monkeypatch.setattr(mel, "_camera_snapshot_bytes", lambda: _jpeg() + b"x")
    monkeypatch.setattr(mel, "_visual_alert", lambda filename, image, reason, identity=None: alerts.append(reason))
    monkeypatch.setattr(mel, "_frame_changed", lambda before, after: False)
    if isinstance(verdict, Exception):
        def failing(image, prompt, streak=None):
            raise verdict
        monkeypatch.setattr(mel, "_visual_verdict", failing)
    else:
        monkeypatch.setattr(mel, "_visual_verdict", lambda image, prompt, streak=None: verdict)

    async def run():
        listener = mel.Listener()
        listener.visual_watch = mel.VisualPrintWatch(level)
        listener.filename = "still.gcode"
        listener.visual_watch.layer2_checked = listener.visual_watch.layer5_checked = True
        listener.visual_frames["still.gcode"] = _jpeg()
        listener.visual_watch.record(5, 100, _jpeg())
        layer = 5 + listener.visual_watch.profile["stall_layers"]
        await listener.visual_check(listener.visual_watch.due(layer, 200), layer)

    asyncio.run(run())
    return alerts


def test_every_sensitivity_trusts_healthy_vision_on_a_stall(monkeypatch):
    # Live: "stalled_layers at layer 159: ok; visual check needs operator
    # review" alerted at sensitivity 3. An ok verdict is never an alert.
    for level in (1, 2, 3, 4, 5):
        assert _stall_alerts(monkeypatch, level, ("ok", "")) == [], level
        assert _stall_alerts(monkeypatch, level, ("safe", "plastic present")) == [], level


def test_low_sensitivity_still_alerts_on_a_bad_stall(monkeypatch):
    for level in (1, 2):
        for verdict in (("air", "empty"), ("spaghetti", "mess"), ("uncertain", "dark")):
            alerts = _stall_alerts(monkeypatch, level, verdict)
            assert alerts and "stalled_layers" in alerts[0] and verdict[0] in alerts[0], (level, verdict)
        alerts = _stall_alerts(monkeypatch, level, OSError("vision down"))
        assert alerts and "the vision check could not be completed" in alerts[0], level


def test_default_and_higher_sensitivity_alert_on_a_bad_stall(monkeypatch):
    for level in (3, 4, 5):
        for verdict in (("air", "empty"), ("empty_idle", "")):
            alerts = _stall_alerts(monkeypatch, level, verdict)
            assert alerts and "stalled_layers" in alerts[0], (level, verdict)


def test_vision_dark_verdict_detection():
    assert mel._verdict_says_dark("uncertain", "The camera feed is completely dark")
    assert mel._verdict_says_dark("uncertain", "Image is too dark to judge")
    assert not mel._verdict_says_dark("uncertain", "dark")  # no camera wording: an ordinary uncertain
    assert not mel._verdict_says_dark("uncertain", "nozzle hidden")
    assert not mel._verdict_says_dark("spaghetti", "black filament tangled in the frame")


def test_vision_dark_verdict_posts_the_dark_notice_not_an_alert(monkeypatch, tmp_path):
    # Live: "periodic at layer 1: uncertain; The camera feed is completely
    # dark" and repeated "stalled_layers ... uncertain" on a dark static
    # frame both posted as print alerts.
    import asyncio
    from unittest.mock import patch

    alerts, notices = [], []
    monkeypatch.setattr(mel, "_camera_snapshot_bytes", lambda: _jpeg() + b"x")
    monkeypatch.setattr(mel, "_frame_changed", lambda before, after: False)
    monkeypatch.setattr(mel, "_visual_verdict",
                        lambda image, prompt, streak=None: ("uncertain", "The camera feed is completely dark"))
    monkeypatch.setattr(mel, "_visual_alert", lambda filename, image, reason, identity=None: alerts.append(reason))
    monkeypatch.setattr(mel, "_visual_dark_notice", lambda filename, identity=None: notices.append(filename))

    async def run():
        listener = mel.Listener()
        listener.filename = "dim.gcode"
        with patch.object(pab, "STATE", tmp_path / "alerts.json"):
            await listener.visual_check("periodic", 1)
            listener.visual_watch.layer2_checked = listener.visual_watch.layer5_checked = True
            baseline = _jpeg()
            listener.visual_frames["dim.gcode"] = baseline
            listener.visual_watch.record(5, 100, baseline)
            layer = 5 + listener.visual_watch.profile["stall_layers"]
            await listener.visual_check(listener.visual_watch.due(layer, 200), layer)
        return listener, baseline

    listener, baseline = asyncio.run(run())
    assert alerts == []
    assert notices == ["dim.gcode"]
    # The still dark frame was no stall: the stall latch stays free and the
    # dark frame does not replace the baseline.
    assert listener.visual_watch.alerted_stall_layer is None
    assert listener.visual_frames["dim.gcode"] == baseline


DIRECT_URL = "http://printer.example/webcam/?action=snapshot"
FANOUT_URL = "http://127.0.0.1:1984/api/frame.jpeg?src=camera"


def _record_snapshot_requests(monkeypatch, fanout, fail_urls=()):
    """Route _camera_snapshot_bytes at a fake transport; return the URLs hit."""
    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, _n=-1):
            return _jpeg()

    def fake_open(req, timeout=0):
        seen.append(req.full_url)
        if req.full_url in fail_urls:
            raise ConnectionError("refused")
        return Response()

    monkeypatch.setattr(mel, "_camera_url", lambda: DIRECT_URL)
    monkeypatch.setattr(mel, "_config", type("Cfg", (), {"cfg": staticmethod(lambda: {})})())
    monkeypatch.setattr(mel.go2rtc, "fanout_frame_url", lambda data: fanout)
    monkeypatch.setattr(mel, "log", lambda line: None)
    monkeypatch.setattr(mel.urllib.request, "urlopen", fake_open)
    return seen


def test_watchdog_snapshot_uses_go2rtc_fanout_when_it_is_running(monkeypatch):
    seen = _record_snapshot_requests(monkeypatch, FANOUT_URL)
    assert mel._camera_snapshot_bytes() == _jpeg()
    assert seen == [FANOUT_URL]  # the direct camera URL is never opened


def test_watchdog_snapshot_is_direct_when_no_fanout_feature_is_on(monkeypatch):
    seen = _record_snapshot_requests(monkeypatch, None)
    assert mel._camera_snapshot_bytes() == _jpeg()
    assert seen == [DIRECT_URL]


def test_watchdog_snapshot_falls_back_direct_when_the_fanout_fails(monkeypatch):
    seen = _record_snapshot_requests(monkeypatch, FANOUT_URL, fail_urls={FANOUT_URL})
    assert mel._camera_snapshot_bytes() == _jpeg()
    assert seen == [FANOUT_URL, DIRECT_URL]  # sequential fallback, never parallel


# -- camera_source pin (2.2.2 wiring; the pin was a dead option before) -------

def _pin_config(monkeypatch, pin):
    cfg = {"services": {"camera_source": pin}}
    monkeypatch.setattr(mel, "_config", type("Cfg", (), {"cfg": staticmethod(lambda: cfg)})())


def test_watchdog_snapshot_pin_none_takes_no_image(monkeypatch):
    seen = _record_snapshot_requests(monkeypatch, FANOUT_URL)
    _pin_config(monkeypatch, "none")
    with pytest.raises(ValueError, match="camera_source is none"):
        mel._camera_snapshot_bytes()
    assert seen == []  # neither source was opened


def test_watchdog_snapshot_pin_moonraker_never_uses_the_fanout(monkeypatch):
    seen = _record_snapshot_requests(monkeypatch, FANOUT_URL)
    _pin_config(monkeypatch, "moonraker")
    assert mel._camera_snapshot_bytes() == _jpeg()
    assert seen == [DIRECT_URL]


def test_watchdog_snapshot_pin_go2rtc_uses_only_the_fanout(monkeypatch):
    seen = _record_snapshot_requests(monkeypatch, FANOUT_URL)
    _pin_config(monkeypatch, "go2rtc")
    assert mel._camera_snapshot_bytes() == _jpeg()
    assert seen == [FANOUT_URL]
    # And when the fan-out is not running, the pin names itself instead
    # of silently falling back to the direct URL.
    seen = _record_snapshot_requests(monkeypatch, None)
    _pin_config(monkeypatch, "go2rtc")
    with pytest.raises(ValueError, match="camera_source is go2rtc"):
        mel._camera_snapshot_bytes()
    assert seen == []


def test_watchdog_snapshot_pin_bambu_names_the_mismatch(monkeypatch):
    seen = _record_snapshot_requests(monkeypatch, FANOUT_URL)
    _pin_config(monkeypatch, "bambu")
    with pytest.raises(ValueError, match="camera_source is bambu"):
        mel._camera_snapshot_bytes()
    assert seen == []


# -- #476: dark frames -------------------------------------------------------

def _pil_jpeg(pixels, size=(64, 48)):
    """A real PIL-encoded JPEG from a pixel function (x, y) -> gray level."""
    import io
    from PIL import Image
    img = Image.new("L", size)
    img.putdata([pixels(x, y) for y in range(size[1]) for x in range(size[0])])
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


def _dark_jpeg():
    return _pil_jpeg(lambda x, y: 0)


def _bright_jpeg():
    return _pil_jpeg(lambda x, y: 200)


def test_frame_dark_pins_completely_dark_frames():
    from print_handy.hermes import visual_watch as vw
    # Pure black and near-black noise are dark.
    assert vw.frame_dark(_dark_jpeg()) is True
    assert vw.frame_dark(_pil_jpeg(lambda x, y: (x + y) % 3)) is True
    # A dim but real scene (mean ~30, a bright patch) is NOT dark.
    assert vw.frame_dark(_pil_jpeg(lambda x, y: 30)) is False
    assert vw.frame_dark(_pil_jpeg(lambda x, y: 200 if x < 8 and y < 8 else 10)) is False
    # A normal bright frame is not dark, and neither is an undecodable one:
    # garbage keeps the old handling (a vision failure alerts).
    assert vw.frame_dark(_bright_jpeg()) is False
    assert vw.frame_dark(_jpeg()) is False
    assert vw.frame_dark(b"") is False


def _record_snapshot_frames(monkeypatch, fanout, frames):
    """Like _record_snapshot_requests, but each URL serves its own frame bytes."""
    seen = []

    class Response:
        def __init__(self, body):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, _n=-1):
            return self._body

    def fake_open(req, timeout=0):
        seen.append(req.full_url)
        return Response(frames[req.full_url])

    monkeypatch.setattr(mel, "_camera_url", lambda: DIRECT_URL)
    monkeypatch.setattr(mel, "_config", type("Cfg", (), {"cfg": staticmethod(lambda: {})})())
    monkeypatch.setattr(mel.go2rtc, "fanout_frame_url", lambda data: fanout)
    monkeypatch.setattr(mel, "log", lambda line: None)
    monkeypatch.setattr(mel.urllib.request, "urlopen", fake_open)
    return seen


def test_watchdog_snapshot_falls_back_direct_when_the_fanout_frame_is_dark(monkeypatch):
    seen = _record_snapshot_frames(monkeypatch, FANOUT_URL,
                                   {FANOUT_URL: _dark_jpeg(), DIRECT_URL: _bright_jpeg()})
    assert mel._camera_snapshot_bytes() == _bright_jpeg()
    assert seen == [FANOUT_URL, DIRECT_URL]  # dark counts as a fan-out failure: one sequential fallback


def test_watchdog_snapshot_dark_direct_frame_is_returned_without_a_second_fanout_fetch(monkeypatch):
    seen = _record_snapshot_frames(monkeypatch, FANOUT_URL,
                                   {FANOUT_URL: _dark_jpeg(), DIRECT_URL: _dark_jpeg()})
    assert mel._camera_snapshot_bytes() == _dark_jpeg()
    assert seen == [FANOUT_URL, DIRECT_URL]


def test_dark_frame_check_posts_one_notice_and_never_alerts_or_calls_vision(monkeypatch, tmp_path):
    import asyncio
    from unittest.mock import patch
    from print_handy.hermes import print_alert_buttons as pab

    alerts, notices = [], []
    monkeypatch.setattr(mel, "_camera_snapshot_bytes", lambda: _dark_jpeg())
    monkeypatch.setattr(mel, "_visual_verdict",
                        lambda image, prompt, streak=None: (_ for _ in ()).throw(AssertionError("vision called for a dark frame")))
    monkeypatch.setattr(mel, "_visual_alert",
                        lambda filename, image, reason, identity=None: alerts.append(reason))
    monkeypatch.setattr(mel, "_visual_dark_notice",
                        lambda filename, identity=None: notices.append(filename))

    async def run():
        listener = mel.Listener()
        listener.filename = "dark.gcode"
        with patch.object(pab, "STATE", tmp_path / "alerts.json"):
            await listener.visual_check("periodic", 1)
            await listener.visual_check("periodic", 2)

    asyncio.run(run())
    assert alerts == []
    assert notices == ["dark.gcode"]  # once per print, not once per dark check


def test_dark_frame_does_not_become_a_stall_baseline(monkeypatch, tmp_path):
    import asyncio
    from unittest.mock import patch
    from print_handy.hermes import print_alert_buttons as pab

    monkeypatch.setattr(mel, "_camera_snapshot_bytes", lambda: _dark_jpeg())
    monkeypatch.setattr(mel, "_visual_dark_notice", lambda filename, identity=None: None)

    async def run():
        listener = mel.Listener()
        listener.filename = "dark.gcode"
        with patch.object(pab, "STATE", tmp_path / "alerts.json"):
            await listener.visual_check("periodic", 9)
        return listener

    listener = asyncio.run(run())
    assert listener.visual_frames == {}
    assert listener.visual_watch.last_snapshot is None
    assert listener.visual_watch.alerted_stall_layer is None


def test_muted_print_posts_no_visual_alert(monkeypatch, tmp_path):
    import asyncio
    from unittest.mock import patch
    from print_handy.hermes import print_alert_buttons as pab

    alerts = []
    monkeypatch.setattr(mel, "_camera_snapshot_bytes", lambda: _jpeg())
    monkeypatch.setattr(mel, "_visual_verdict", lambda image, prompt, streak=None: ("air", "empty bed"))
    monkeypatch.setattr(mel, "_visual_alert",
                        lambda filename, image, reason, identity=None: alerts.append(reason))

    async def run():
        listener = mel.Listener()
        listener.filename = "bin.gcode"
        with patch.object(pab, "STATE", tmp_path / "alerts.json"):
            pab.mute_visual("bin.gcode", listener._visual_identity())
            await listener.visual_check("layer2", 2)

    asyncio.run(run())
    assert alerts == []


def test_print_identity_outlives_the_start_claim(monkeypatch, tmp_path):
    # "Stop asking for this print" keyed to file|start-claim; once the claim
    # expired (start_claim_ttl_s) the key flipped and the alerts came back.
    import asyncio
    from unittest.mock import AsyncMock, patch

    claim = {"ts": 1000.0}
    claims = []
    real_claim = mel._claim_notification
    monkeypatch.setattr(mel, "start_claim_time", lambda name, now=None: claim["ts"])
    monkeypatch.setattr(mel, "start_claimed", lambda name, now=None: False)
    monkeypatch.setattr(mel, "NOTIFICATION_STATE", tmp_path / "notifications.json")
    monkeypatch.setattr(mel, "_claim_notification",
                        lambda identity, phase, path=None: claims.append((identity, phase)) or real_claim(identity, phase))
    monkeypatch.setattr(mel, "post", lambda message: True)
    monkeypatch.setattr(mel, "run_announcer", lambda reason: None)
    monkeypatch.setattr(mel, "log", lambda line: None)
    monkeypatch.setattr(pab, "retire_alerts", lambda *args, **kwargs: 0)

    async def run():
        listener = mel.Listener()
        listener.after_print = AsyncMock()
        with patch.object(pab, "STATE", tmp_path / "alerts.json"):
            await listener.on_status({"state": "standby", "filename": "long.gcode"}, "test")
            await listener.on_status({"state": "printing"}, "test")
            started = listener._visual_identity()
            pab.mute_visual("long.gcode", started)
            claim["ts"] = None  # the start claim expired mid-print
            muted = pab.visual_muted("long.gcode", listener._visual_identity())
            await listener.on_status({"state": "error"}, "test")
        return started, muted

    started, muted = asyncio.run(run())
    assert started == "file:long.gcode|start:1000.0"
    assert muted
    assert claims == [(started, "start"), (started, "terminal")]


def test_unclaimed_print_gets_a_stable_identity(monkeypatch):
    import asyncio

    monkeypatch.setattr(mel, "start_claim_time", lambda name, now=None: None)
    monkeypatch.setattr(mel, "post", lambda message: True)
    monkeypatch.setattr(mel, "run_announcer", lambda reason: None)
    monkeypatch.setattr(mel, "log", lambda line: None)

    async def run():
        listener = mel.Listener()
        await listener.on_status({"state": "standby", "filename": "screen.gcode"}, "test")
        await listener.on_status({"state": "printing"}, "test")
        return listener._visual_identity(), listener._visual_identity()

    first, again = asyncio.run(run())
    # Not the mute store's filename + day fallback, which flips at midnight.
    assert first and first.startswith("file:screen.gcode|seen:") and first == again


# -- no vision endpoint: the watch stays off ------------------------------------


def test_vision_ready_needs_an_endpoint_the_watch_can_actually_use(monkeypatch):
    from print_handy.hermes import visual_watch as vw
    monkeypatch.delenv("PRINT_VISION_URL", raising=False)
    monkeypatch.setattr(vw, "_vision_missing_logged", False)
    urls = {"vision_chat_url": ""}
    monkeypatch.setattr(vw._config, "service_url", lambda key: urls.get(key, ""))
    lines = []
    assert vw.vision_ready(lambda: "key", lines.append) is False
    assert vw.vision_ready(lambda: "key", lines.append) is False
    assert len(lines) == 1 and "camera watch skipped" in lines[0]  # said once, not per check
    urls["vision_chat_url"] = "https://vision.example.com/v1/chat/completions"
    assert vw.vision_ready(lambda: "", None) is False  # a hosted URL without its key judges nothing
    assert vw.vision_ready(lambda: "key", None) is True
    urls["vision_chat_url"] = "http://vision.local/v1/chat/completions"
    assert vw.vision_ready(lambda: "", None) is True  # a LAN server takes no key
    urls["vision_chat_url"] = ""
    monkeypatch.setenv("PRINT_VISION_URL", "http://detector.local/judge")
    assert vw.vision_ready(lambda: "", None) is True


def _printing_with_a_due_check(monkeypatch, ready):
    import asyncio
    checks = []
    monkeypatch.setattr(mel, "post", lambda message: True)
    monkeypatch.setattr(mel, "run_announcer", lambda reason: None)
    monkeypatch.setattr(mel, "start_claimed", lambda name: False)
    monkeypatch.setattr(mel, "log", lambda line: None)
    monkeypatch.setattr(mel, "_buttonless_standalone", lambda: None)
    monkeypatch.setattr(mel, "_vision_ready", lambda: ready, raising=False)

    async def record(self, reason, layer):
        checks.append(reason)
    monkeypatch.setattr(mel.Listener, "visual_check", record)
    listener = mel.Listener()
    listener.visual_watch.due = lambda *args, **kwargs: "layer2"

    async def run():
        for state in ("standby", "printing"):
            await listener.on_status({"state": state, "filename": "clip.gcode"}, "test")
        if listener.last_visual_task is not None:
            await listener.last_visual_task
    asyncio.run(run())
    return checks


def test_no_vision_endpoint_means_no_camera_checks_and_no_false_alerts(monkeypatch):
    assert _printing_with_a_due_check(monkeypatch, ready=False) == []


def test_a_configured_vision_endpoint_still_runs_the_due_check(monkeypatch):
    assert _printing_with_a_due_check(monkeypatch, ready=True) == ["layer2"]
