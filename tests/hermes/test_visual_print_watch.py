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


def test_visual_verdict_context_names_the_configured_printer(monkeypatch):
    """The Moonraker vision context names the serving printer, never a hardcoded one.

    Regression: the context was the literal string "Printer: Flashforge
    AD5X, enclosed, smooth PEI plate." whatever printer the listener
    served, so a non-AD5X Moonraker printer's frames were judged against
    the AD5X's description.
    """
    import json
    from unittest import mock

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return json.dumps({"choices": [{"message": {"content":
                '{"verdict":"ok","reason":"printing"}'}}]}).encode()

    seen = {}

    def fake_open(req, timeout=0):
        seen["body"] = json.loads(req.data)
        return Response()

    monkeypatch.setattr(mel.printers, "current",
                        lambda *a, **k: mock.Mock(display_name="Sovol SV06"))
    monkeypatch.setattr(mel, "_profile_env_value", lambda name: "test-key")
    monkeypatch.setattr(mel.urllib.request, "urlopen", fake_open)
    verdict, reason = mel._visual_verdict(b"frame", "watch this print")
    assert verdict == "ok" and reason == "printing"
    text = seen["body"]["messages"][0]["content"][0]["text"]
    assert "Sovol SV06" in text
    assert "AD5X" not in text
    assert "Flashforge" not in text


def test_vision_context_names_an_ad5x_when_it_is_one(monkeypatch):
    from unittest import mock

    monkeypatch.setattr(mel.printers, "current",
                        lambda *a, **k: mock.Mock(display_name="Flashforge AD5X (ZMod)"))
    assert mel._vision_context().startswith("Printer: Flashforge AD5X (ZMod)")


def test_vision_context_falls_back_when_the_printer_is_unknown(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no config")

    monkeypatch.setattr(mel.printers, "current", boom)
    context = mel._vision_context()
    assert context.startswith("Printer: 3D printer")
    assert "empty_idle" in context  # the verdict vocabulary survives


def _family_adapter(label, facts_dict):
    import types
    return types.SimpleNamespace(label=label, facts=facts_dict)


def test_family_idle_context_names_an_open_frame_when_the_facts_say_so():
    """A family printer whose facts record no enclosure is judged as open frame.

    Regression: family_control._idle_context claimed "enclosed" for every
    family printer, so an open-frame Prusa's idle frames were judged
    against an enclosed printer's description.
    """
    from print_handy.hermes import family_control
    from print_handy.printers.prusa import facts

    context = family_control._idle_context(_family_adapter("Prusa MK4S", facts.MK4S))
    assert context.startswith("Printer: Prusa MK4S, open frame, build plate.")
    assert "empty_idle" in context  # the verdict vocabulary survives


def test_family_idle_context_claims_no_enclosure_when_the_facts_record_none():
    """A model whose facts record no enclosure gets no enclosure claim.

    The Bambu A1's sourced facts (printers/bambulab/facts.py) record no
    "enclosed" fact at all; unknown stays unknown, so its idle context
    claims neither "enclosed" nor "open frame".
    """
    from print_handy.hermes import family_control
    from print_handy.printers.bambulab import facts

    context = family_control._idle_context(_family_adapter("Bambu Lab A1", facts.A1))
    assert context.startswith("Printer: Bambu Lab A1, build plate.")
    assert "enclosed" not in context
    assert "open frame" not in context
    assert "empty_idle" in context  # the verdict vocabulary survives


def test_family_idle_context_keeps_enclosed_when_the_facts_record_it():
    """A model whose facts record an enclosure keeps the enclosed wording."""
    from print_handy.hermes import family_control
    from print_handy.printers.elegoo_sdcp import facts

    context = family_control._idle_context(
        _family_adapter("Elegoo Centauri Carbon", facts.CENTAURI_CARBON))
    assert context.startswith("Printer: Elegoo Centauri Carbon, enclosed, build plate.")
    assert "empty_idle" in context  # the verdict vocabulary survives


def test_family_idle_context_claims_nothing_without_facts():
    """An adapter carrying no sourced facts at all gets no enclosure claim.

    The OctoPrint and stock Flashforge adapters record no per-model facts
    (OctoPrint can front any printer), so their idle context is the bare
    label and build plate — the same care the Moonraker context takes.
    """
    import types

    from print_handy.hermes import family_control

    context = family_control._idle_context(
        types.SimpleNamespace(label="OctoPrint printer"))
    assert context.startswith("Printer: OctoPrint printer, build plate.")
    assert "enclosed" not in context
    assert "empty_idle" in context  # the verdict vocabulary survives
# -- Bambu vision context ------------------------------------------------------


def _bambu_adapter(label, facts_dict):
    import types

    return types.SimpleNamespace(label=label, facts=facts_dict)


def test_bambu_vision_context_does_not_call_an_a1_enclosed():
    """An A1's frames must not be judged against an enclosed printer's description.

    Regression: bambu_watch.vision_context claimed "enclosed, smooth PEI
    plate" for every Bambu printer. The A1 line is open-frame, and the
    model's sourced facts (printers/bambulab/facts.py) record no enclosure
    for it, so its context must claim no enclosure at all — and the plate
    the facts do record for it is textured PEI, not smooth.
    """
    from print_handy.hermes import bambu_watch
    from print_handy.printers.bambulab import facts

    context = bambu_watch.vision_context(_bambu_adapter("Bambu Lab A1", facts.A1))
    assert context.startswith("Printer: Bambu Lab A1")
    assert "enclosed" not in context
    assert "smooth" not in context
    assert "textured PEI plate" in context
    assert "empty_idle" in context  # the verdict vocabulary survives


def test_bambu_vision_context_names_an_open_frame_when_the_facts_say_so():
    from print_handy.hermes import bambu_watch
    from print_handy.printers.bambulab import facts

    context = bambu_watch.vision_context(_bambu_adapter("Bambu Lab A1 mini", facts.A1MINI))
    assert "open frame" in context
    assert "enclosed" not in context


def test_bambu_vision_context_keeps_enclosed_for_a_p1s():
    from print_handy.hermes import bambu_watch
    from print_handy.printers.bambulab import facts

    context = bambu_watch.vision_context(_bambu_adapter("Bambu Lab P1S", facts.P1S))
    assert "enclosed, textured PEI plate" in context
    assert "smooth" not in context


def test_bambu_vision_context_claims_nothing_without_facts():
    from print_handy.hermes import bambu_watch

    context = bambu_watch.vision_context(_bambu_adapter("Bambu Lab printer", {}))
    assert context.startswith("Printer: Bambu Lab printer. ")
    assert "enclosed" not in context and "PEI" not in context


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


def _distinct_jpegs():
    """Real, visibly different camera frames: a new view of the print each call."""
    import io
    import random
    from PIL import Image
    rng = random.Random(913)

    def make():
        image = Image.new("L", (160, 120))
        image.putdata([rng.randrange(40, 220) for _ in range(160 * 120)])
        out = io.BytesIO()
        image.convert("RGB").save(out, "JPEG")
        return out.getvalue()

    return make


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
    assert alerts and "Print may have stalled" in alerts[0]
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
            assert alerts and "Print may have stalled" in alerts[0] and verdict[0] in alerts[0], (level, verdict)
        alerts = _stall_alerts(monkeypatch, level, OSError("vision down"))
        assert alerts and "the vision check could not be completed" in alerts[0], level


def test_default_and_higher_sensitivity_alert_on_a_bad_stall(monkeypatch):
    for level in (3, 4, 5):
        for verdict in (("air", "empty"), ("empty_idle", "")):
            alerts = _stall_alerts(monkeypatch, level, verdict)
            assert alerts and "Print may have stalled" in alerts[0], (level, verdict)


def test_stall_alert_names_its_differential(monkeypatch):
    # Stage 2 (#455 item 4): the stall alert states what was observed and
    # lists the possibilities instead of leading with the machine reason
    # and letting the vision verdict read as the diagnosis.
    alerts = _stall_alerts(monkeypatch, 3, ("spaghetti", "mess"))
    text = alerts[0]
    assert text.startswith("Print may have stalled at layer")
    assert "camera frame stopped changing" in text
    for possibility in ("printing in air", "blocked", "parked", "dark"):
        assert possibility in text, possibility
    assert "stalled_layers" not in text
    # The verdict still rides along as evidence, after the observation.
    assert "Vision verdict: spaghetti; mess" in text


def test_stall_alert_text_rules_out_what_the_evidence_settles():
    # A dark verdict settles the differential: the view is dark, and a
    # dark frame's stillness rules the print-side possibilities out.
    # (Pinned at the renderer: through the listener, a dark verdict takes
    # the dark-camera notice path and never reaches the stall alert.)
    from print_handy.hermes import visual_watch as vw
    text = vw.stall_alert_text(48, "dark", "camera image completely dark")
    assert "camera view dark" in text and "ruled out" in text
    for ruled in ("printing in air", "blocked nozzle", "toolhead parked"):
        assert ruled in text, ruled
    # An air verdict supports one possibility and rules nothing out.
    text = vw.stall_alert_text(48, "air", "no plastic under the nozzle")
    assert "supports: printing in air" in text and "ruled out" not in text
    # An unsettled verdict keeps the full flat differential.
    text = vw.stall_alert_text(48, "spaghetti", "mess")
    assert "blocked" in text and "supports:" not in text


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
        sent = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, _n=-1):
            # One frame, then end of stream, like a real HTTP body.
            if self.sent:
                return b""
            self.sent = True
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
            # The body once, then end of stream, like a real HTTP response.
            body, self._body = self._body, b""
            return body

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


# -- #913: cadence, early-layer mode, opt-in auto-pause, local fallback -----


def test_cadence_tick_with_fake_clock_and_40min_layer(monkeypatch):
    """Cadence bug (#913): periodic checks are time-based, independent of layer events.

    In V12, layers 3 and 4 took 49 minutes with no layer event, so no periodic
    check ran. The tick timer must fire periodic checks regardless of layer stillness.
    """
    watch = mel.VisualPrintWatch(sensitivity=3)
    watch.record(3, 1000.0)

    # In sensitivity 3, periodic interval is 900s.
    assert watch.tick(3, 1000.0 + 899.0) is None
    assert watch.tick(3, 1000.0 + 900.0) == "periodic"
    assert watch.tick(3, 1000.0 + 1799.0) is None
    assert watch.tick(3, 1000.0 + 1800.0) == "periodic"
    # A 40-minute layer (2400 seconds) fires again at 2700s:
    assert watch.tick(3, 1000.0 + 2699.0) is None
    assert watch.tick(3, 1000.0 + 2700.0) == "periodic"

    # Listener _visual_timer_tick launches visual_check and respects single-flight
    listener = mel.Listener()
    listener.state = "printing"
    listener.last_stats = {"info": {"current_layer": 3}}
    monkeypatch.setattr(mel, "_vision_ready", lambda: True)
    monkeypatch.setattr(mel._config, "cfg", lambda: {"visual_watch": {"enabled": True, "sensitivity": 3}})
    launched = []

    async def fake_visual_check(reason, layer):
        launched.append((reason, layer))

    monkeypatch.setattr(listener, "visual_check", fake_visual_check)
    listener.visual_watch.record(3, 1000.0)

    # Before periodic_s -> None
    assert listener._visual_timer_tick(now=1899.0) is None
    assert launched == []

    # Inside an event loop, at periodic_s -> fires task
    import asyncio

    async def run():
        assert listener._visual_timer_tick(now=1900.0) == "periodic"
        async def noop():
            pass

        task = asyncio.create_task(noop())
        listener.last_visual_task = task
        assert listener._visual_timer_tick(now=3000.0) is None
        await task

    asyncio.run(run())


def test_empty_idle_on_layer_one_rechecks_within_60s_and_alerts_on_second(monkeypatch):
    """An empty_idle verdict on layer 1 while printing re-checks in 60s and alerts on repeat (#913)."""
    import asyncio
    from print_handy.hermes import visual_watch as vw

    alerts = []
    posts = []
    monkeypatch.setattr(mel, "_camera_snapshot_bytes", lambda: _jpeg())
    monkeypatch.setattr(mel, "_frame_changed", lambda before, after: False)
    monkeypatch.setattr(mel, "_visual_alert", lambda filename, image, text, identity=None: alerts.append((text, identity)))
    monkeypatch.setattr(mel, "post", posts.append)
    monkeypatch.setattr(mel, "log", lambda line: None)
    monkeypatch.setattr(pab, "visual_muted", lambda filename, identity: False)

    listener = mel.Listener()
    listener.filename = "plate.gcode"
    listener.state = "printing"

    # 1. First check at layer 1 returns empty_idle
    monkeypatch.setattr(mel, "_visual_verdict", lambda image, prompt, streak=None, local_fallback=False: ("empty_idle", "clear bed"))
    asyncio.run(listener.visual_check("layer1", 1))
    assert alerts == []  # Not alerted on first occurrence
    assert listener.visual_watch.recheck_at > 0

    # tick before 60s -> None; at 60s -> empty_idle_recheck
    assert listener.visual_watch.tick(1, listener.visual_watch.recheck_at - 1) is None
    assert listener.visual_watch.tick(1, listener.visual_watch.recheck_at) == "empty_idle_recheck"

    # 2. Second check with empty_idle_recheck reason also sees empty_idle -> alerts!
    asyncio.run(listener.visual_check("empty_idle_recheck", 1))
    assert len(alerts) == 1
    assert "empty_idle_recheck" in alerts[0][0]
    assert "empty bed again while the printer reports printing" in alerts[0][0]


def test_early_layer_schedule_for_risky_prints_vs_normal():
    """Early-layer mode checks every layer up to early_layers and every early_interval_s (#913)."""
    from print_handy.hermes import visual_watch as vw

    # Normal print: layer 2, layer 5, then periodic
    normal_watch = mel.VisualPrintWatch(sensitivity=3)
    normal_watch.set_mode(vw.WATCH_MODE_NORMAL)
    assert normal_watch.due(1, 100) is None
    assert normal_watch.due(2, 101) == "layer2"
    assert normal_watch.due(3, 102) is None
    assert normal_watch.due(4, 103) is None
    assert normal_watch.due(5, 104) == "layer5"
    assert normal_watch.due(6, 105) is None

    # Risky print: checks every layer change for first early_layers (10)
    risky_watch = mel.VisualPrintWatch(sensitivity=3, early_layers=10, early_interval_s=120)
    risky_watch.set_mode(vw.WATCH_MODE_EARLY)
    for layer in range(1, 11):
        assert risky_watch.due(layer, 200 + layer) == "early_layer"
    # Layer 11 is past early window
    assert risky_watch.due(11, 300) is None

    # Tick inside early window fires at early_interval_s (120s)
    risky_watch.record(2, 1000)
    assert risky_watch.tick(2, 1119) is None
    assert risky_watch.tick(2, 1120) == "early_periodic"

    # Outside early window (layer 12), tick fires at periodic_s (900s), not 120s
    risky_watch.record(12, 2000)
    assert risky_watch.tick(12, 2120) is None
    assert risky_watch.tick(12, 2900) == "periodic"

    # risky_job context recognition
    assert vw.risky_job({}) == ""
    assert "finding" in vw.risky_job({"slice_findings": ["unsupported_overhang"]})
    assert "override" in vw.risky_job({"print_anyway": True})
    assert "supports" in vw.risky_job({"support_decision": {"supports": True}})
    assert "risky marker" in vw.risky_job({"risky": True})


def test_auto_pause_early_off_by_default_and_on_opt_in(monkeypatch):
    """Auto-pause is off by default; on opt-in it pauses on 2 suspicious verdicts, never cancels (#913)."""
    import asyncio
    from print_handy.hermes import visual_watch as vw

    alerts = []
    pauses = []
    actions = []
    clock = [10_000.0]
    real_auto_pause = mel._auto_pause_early
    monkeypatch.setattr(mel, "_camera_snapshot_bytes", _distinct_jpegs())
    monkeypatch.setattr(mel, "_watch_now", lambda: clock[0])
    monkeypatch.setattr(mel, "_frame_changed", lambda before, after: False)
    monkeypatch.setattr(mel, "_visual_alert", lambda filename, image, text, identity=None: alerts.append(text))
    monkeypatch.setattr(mel, "_auto_pause_early", lambda filename, identity: pauses.append((filename, identity)) or "Paused")
    monkeypatch.setattr(mel, "post", lambda msg: True)
    monkeypatch.setattr(mel, "log", lambda line: None)
    monkeypatch.setattr(pab, "visual_muted", lambda filename, identity: False)

    # 1. Off by default
    listener = mel.Listener()
    listener.filename = "risky_off.gcode"
    listener.state = "printing"
    listener.visual_watch.set_mode(vw.WATCH_MODE_EARLY)
    monkeypatch.setattr(mel._config, "cfg", lambda: {
        "visual_watch": {"enabled": True, "sensitivity": 3, "auto_pause_early": False, "early_layers": 10, "early_interval_s": 120}
    })

    monkeypatch.setattr(mel, "_visual_verdict", lambda *args, **kwargs: ("spaghetti", "spaghetti found"))
    asyncio.run(listener.visual_check("early_layer", 2))
    assert len(alerts) == 1
    assert pauses == []

    # Second suspicious verdict, a minute later on a new frame
    clock[0] += 61
    asyncio.run(listener.visual_check("early_layer", 3))
    assert len(alerts) == 2
    assert pauses == []  # Off by default: NO pause sent
    assert listener.visual_watch.auto_paused is False

    # 2. Opt-in enabled
    listener_on = mel.Listener()
    listener_on.filename = "risky_on.gcode"
    listener_on.state = "printing"
    listener_on.visual_watch.set_mode(vw.WATCH_MODE_EARLY)
    monkeypatch.setattr(mel._config, "cfg", lambda: {
        "visual_watch": {"enabled": True, "sensitivity": 3, "auto_pause_early": True, "early_layers": 10, "early_interval_s": 120}
    })

    alerts.clear()
    pauses.clear()

    # First suspicious verdict (streak 1) -> alert, but NO pause
    asyncio.run(listener_on.visual_check("early_layer", 2))
    assert len(alerts) == 1
    assert "Auto-pause" not in alerts[0]
    assert pauses == []
    assert listener_on.visual_watch.auto_paused is False

    # Second suspicious verdict (streak 2), a minute later on a new frame -> alert + PAUSE!
    clock[0] += 61
    asyncio.run(listener_on.visual_check("early_layer", 3))
    assert len(alerts) == 2
    assert "Auto-pause" in alerts[1]
    assert len(pauses) == 1
    assert pauses[0][0] == "risky_on.gcode"
    assert listener_on.visual_watch.auto_paused is True

    # Third suspicious verdict (streak 3) -> alert, but pause only sent ONCE per print
    clock[0] += 61
    asyncio.run(listener_on.visual_check("early_layer", 4))
    assert len(alerts) == 3
    assert len(pauses) == 1

    # Verify _auto_pause_early only uses action="pause", NEVER "cancel"
    monkeypatch.setattr(pab, "handle_click", lambda act, alert: actions.append(act) or "Paused")
    real_auto_pause("risky_on.gcode", "id123")
    assert actions == ["pause"]


def test_local_detector_fallback_in_early_layer_mode(monkeypatch, tmp_path):
    """Local detector is used as backup in early-layer mode when endpoint fails and model is present (#913)."""
    from print_handy.hermes import local_vision
    from print_handy.hermes import visual_watch as vw

    model_file = tmp_path / "model.onnx"
    model_file.write_bytes(b"dummy-onnx")

    def boom(*args, **kwargs):
        raise OSError("cloud timeout")

    monkeypatch.setattr(vw, "_vision_answer", boom)
    monkeypatch.setattr(vw, "_chat_route_configured", lambda api_key: True)
    monkeypatch.setattr(local_vision, "model_path", lambda: model_file)
    monkeypatch.setattr(local_vision, "local_verdict", lambda image, streak=None: ("spaghetti", "local detector saw spaghetti"))

    # In early-layer mode with model present: falls back to local detector
    verdict, detail = vw.visual_verdict(
        b"image", "prompt", chat_url="http://fake", model="gemini",
        api_key=lambda: "key", context="", local_fallback=True
    )
    assert verdict == "spaghetti"
    assert "local detector fallback" in detail

    # In early-layer mode with model MISSING: degrades to uncertain without downloading
    monkeypatch.setattr(local_vision, "model_path", lambda: tmp_path / "missing.onnx")
    verdict, detail = vw.visual_verdict(
        b"image", "prompt", chat_url="http://fake", model="gemini",
        api_key=lambda: "key", context="", local_fallback=True
    )
    assert verdict == "uncertain"
    assert "the vision check could not be completed" in detail

    # In normal mode (local_fallback=False) even with model present: no fallback
    monkeypatch.setattr(local_vision, "model_path", lambda: model_file)
    verdict, detail = vw.visual_verdict(
        b"image", "prompt", chat_url="http://fake", model="gemini",
        api_key=lambda: "key", context="", local_fallback=False
    )
    assert verdict == "uncertain"
    assert "the vision check could not be completed" in detail


def test_transport_and_settings_parity_for_early_layer_and_auto_pause():
    """Settings, catalog, and validation parity for early_layers, early_interval_s, auto_pause_early (#913)."""
    from print_handy import settings
    from print_handy.webui import catalog

    cfg = settings.load(local=False)
    assert cfg["visual_watch"]["early_layers"] == 10
    assert cfg["visual_watch"]["early_interval_s"] == 120
    assert cfg["visual_watch"]["auto_pause_early"] is False

    assert catalog.field_by_id("visual_watch.early_layers") is not None
    assert catalog.field_by_id("visual_watch.early_interval_s") is not None
    assert catalog.field_by_id("visual_watch.auto_pause_early") is not None

    # Validation errors on invalid inputs
    base = settings.load(local=False)
    bad_layers = {**base, "visual_watch": {**base["visual_watch"], "early_layers": -1}}
    with pytest.raises(settings.SettingsError):
        settings.validate(bad_layers)
    bad_interval = {**base, "visual_watch": {**base["visual_watch"], "early_interval_s": 0}}
    with pytest.raises(settings.SettingsError):
        settings.validate(bad_interval)
    bad_pause = {**base, "visual_watch": {**base["visual_watch"], "auto_pause_early": "yes"}}
    with pytest.raises(settings.SettingsError):
        settings.validate(bad_pause)



# -- Codex findings on #920: provenance shapes, late context, stale verdicts --


def test_risky_job_reads_support_warnings_from_job_provenance():
    """Support risk lives under provenance, not top-level fields (Codex on #920).

    core/slice_warnings stores warnings as job['provenance']['slice_warnings']
    with a support_needed flag (and the same on derived plates), and an
    operator can settle supports through job['orientation']['supports'].
    risky_job must recognize those real shapes.
    """
    from print_handy.hermes import visual_watch as vw

    warned = {"provenance": {"slice_warnings": {"support_needed": True,
                                                 "support_texts": ["floating regions"]}}}
    assert vw.risky_job(warned) == "support warning"
    plated = {"derived_plates": [{"provenance": {"slice_warnings": {"support_needed": True}}}]}
    assert vw.risky_job(plated) == "support warning"
    decided = {"orientation": {"rotation": [90, 0, 0], "supports": False}}
    assert vw.risky_job(decided) == "supports decision"
    # No warning recorded anywhere: still a normal print.
    quiet = {"provenance": {"slice_warnings": {"support_needed": False}}}
    assert vw.risky_job(quiet) == ""


def test_watch_mode_upgrades_when_start_context_arrives_late(monkeypatch):
    """A start context written after the first printing event still upgrades the mode (Codex on #920).

    On the generic Moonraker launch path record_start writes the sidecar only
    after the start is confirmed, so the listener's first _apply_watch_mode can
    see no context and pick normal. The timer tick must retry the lookup until
    a context exists, instead of leaving the risky print unmonitored.
    """
    from print_handy.hermes import visual_watch as vw

    contexts = {"late.gcode": {}}
    monkeypatch.setattr(mel.print_records, "load_context",
                        lambda filename: contexts.get(filename, {}))
    monkeypatch.setattr(mel._config, "cfg", lambda: {
        "visual_watch": {"enabled": True, "sensitivity": 3,
                         "auto_pause_early": True, "early_layers": 10, "early_interval_s": 120}})
    monkeypatch.setattr(mel, "log", lambda line: None)

    listener = mel.Listener()
    listener.filename = "late.gcode"
    listener.state = "printing"
    listener.last_stats = {"info": {"current_layer": 3}}
    assert listener._apply_watch_mode() == vw.WATCH_MODE_NORMAL

    # record_start lands after the start was already observed.
    contexts["late.gcode"] = {"watch_mode": "early_layer", "watch_risk": "slicer finding"}
    listener._visual_timer_tick(now=50)
    assert listener.visual_watch.mode == vw.WATCH_MODE_EARLY


def test_stale_verdict_from_previous_print_never_alerts_or_pauses(monkeypatch):
    """A verdict captured for one print must not act on the next print (Codex P1 on #920).

    While visual_check awaits the vision call, a new print can start: the old
    verdict must be discarded, not counted toward the new print's streak and
    never sent as its PAUSE.
    """
    import asyncio
    from print_handy.hermes import visual_watch as vw

    alerts = []
    pauses = []
    monkeypatch.setattr(mel, "_camera_snapshot_bytes", lambda: _jpeg())
    monkeypatch.setattr(mel, "_frame_changed", lambda before, after: False)
    monkeypatch.setattr(mel, "_visual_alert", lambda filename, image, text, identity=None: alerts.append((filename, text)))
    monkeypatch.setattr(mel, "_auto_pause_early", lambda filename, identity: pauses.append((filename, identity)) or "Paused")
    monkeypatch.setattr(mel, "post", lambda msg: True)
    monkeypatch.setattr(mel, "log", lambda line: None)
    monkeypatch.setattr(pab, "visual_muted", lambda filename, identity: False)
    monkeypatch.setattr(mel._config, "cfg", lambda: {
        "visual_watch": {"enabled": True, "sensitivity": 3, "auto_pause_early": True,
                         "early_layers": 10, "early_interval_s": 120}})

    listener = mel.Listener()
    listener.filename = "old.gcode"
    listener.state = "printing"
    listener.visual_watch.set_mode(vw.WATCH_MODE_EARLY)

    def verdict_after_print_changed(*args, **kwargs):
        # The new print starts while the vision call is in flight: on_status
        # swaps the filename and resets the watch for the new print.
        listener.filename = "new.gcode"
        listener.visual_watch.reset()
        listener.visual_watch.set_mode(vw.WATCH_MODE_EARLY)
        return ("spaghetti", "spaghetti found")

    monkeypatch.setattr(mel, "_visual_verdict", verdict_after_print_changed)
    asyncio.run(listener.visual_check("early_layer", 2))

    assert alerts == []
    assert pauses == []
    assert listener.visual_watch.suspicious_streak == 0
    assert listener.visual_watch.auto_paused is False


def test_empty_idle_recheck_keeps_the_earliest_deadline():
    """Repeated empty_idle verdicts must not postpone the recheck (Codex re-review on #920).

    Early layers can take less than 60 s each; if every empty-bed result
    pushed the deadline out another 60 s, the timer would never reach
    empty_idle_recheck and the promised alert would never fire.
    """
    watch = mel.VisualPrintWatch(sensitivity=3)
    watch.note_empty_idle(100)
    assert watch.recheck_at == 160
    watch.note_empty_idle(130)
    assert watch.recheck_at == 160
    assert watch.tick(1, 160) == "empty_idle_recheck"
    # The fired recheck clears the deadline, so a later empty-bed verdict
    # schedules a fresh one.
    watch.note_empty_idle(200)
    assert watch.recheck_at == 260


def test_concurrent_suspicious_checks_pause_only_once(monkeypatch):
    """Overlapping checks must not each send the auto-pause (Codex re-review on #920).

    Due layer checks are scheduled without waiting for the previous check, so
    two suspicious checks can sit in the alert block at once. The once-per-
    print reservation must happen before the alert await, not after it.
    """
    import asyncio
    import time
    from print_handy.hermes import visual_watch as vw

    alerts = []
    pauses = []

    def slow_alert(filename, image, text, identity=None):
        time.sleep(0.3)
        alerts.append(text)

    frames = _distinct_jpegs()
    monkeypatch.setattr(mel, "_camera_snapshot_bytes", frames)
    monkeypatch.setattr(mel, "_watch_now", lambda: 50_000.0)
    monkeypatch.setattr(mel, "_frame_changed", lambda before, after: False)
    monkeypatch.setattr(mel, "_visual_alert", slow_alert)
    monkeypatch.setattr(mel, "_auto_pause_early", lambda filename, identity: pauses.append(filename) or "Paused")
    monkeypatch.setattr(mel, "post", lambda msg: True)
    monkeypatch.setattr(mel, "log", lambda line: None)
    monkeypatch.setattr(pab, "visual_muted", lambda filename, identity: False)
    monkeypatch.setattr(mel._config, "cfg", lambda: {
        "visual_watch": {"enabled": True, "sensitivity": 3, "auto_pause_early": True,
                         "early_layers": 10, "early_interval_s": 120}})
    monkeypatch.setattr(mel, "_visual_verdict", lambda *args, **kwargs: ("spaghetti", "spaghetti found"))

    listener = mel.Listener()
    listener.filename = "race.gcode"
    listener.state = "printing"
    listener.visual_watch.set_mode(vw.WATCH_MODE_EARLY)
    # One independent suspicious view two minutes earlier: each racing check
    # could complete the streak, and only one may send the PAUSE.
    listener.visual_watch.pause_members = [vw.PauseEvidence(
        mel._watch_printer_id(), listener._visual_identity(),
        vw.frame_signature(frames()), 50_000.0 - 120)]

    async def run():
        await asyncio.gather(*(listener.visual_check("early_layer", layer) for layer in (2, 3, 4)))

    asyncio.run(run())
    assert len(alerts) == 3
    assert pauses == ["race.gcode"]
    assert listener.visual_watch.auto_paused is True
