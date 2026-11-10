"""Camera watch coverage and reliability: layerless prints, independent
auto-pause evidence, detector recovery, camera-failure throttling,
deadlines, the next-plate task, reconnect backoff and bounded growth.

All offline: cameras, verdicts, chat posts and clocks are fakes.
"""
import asyncio

import pytest

from print_handy.hermes import moonraker_event_listener as mel
from print_handy.hermes import print_alert_buttons as pab
from print_handy.hermes import visual_watch as vw


def _jpeg():
    return bytes((0xff, 0xd8, 0xff, 0xd9))


# -- 1. prints that report no layer number -----------------------------------


def test_layerless_print_is_watched_on_a_timer():
    """No layer info: the watch runs on printing time, not silently never (P1)."""
    watch = vw.VisualPrintWatch(sensitivity=3)
    # Heat-up and the first minutes of printing are never judged.
    assert watch.tick(None, 1000.0, elapsed_s=0.0) is None
    assert watch.tick(None, 1100.0, elapsed_s=vw.LAYERLESS_FIRST_CHECK_S - 1) is None
    assert watch.tick(None, 1200.0, elapsed_s=vw.LAYERLESS_FIRST_CHECK_S) == "layerless_start"
    assert watch.layerless is True
    # Then the sensitivity profile's periodic interval (900 s at level 3).
    assert watch.tick(None, 1200.0 + 899, elapsed_s=1079.0) is None
    assert watch.tick(None, 1200.0 + 900, elapsed_s=1080.0) == "periodic"


def test_layerless_print_without_print_duration_counts_from_first_beat():
    watch = vw.VisualPrintWatch(sensitivity=3)
    assert watch.tick(None, 500.0) is None
    assert watch.tick(None, 500.0 + vw.LAYERLESS_FIRST_CHECK_S - 1) is None
    assert watch.tick(None, 500.0 + vw.LAYERLESS_FIRST_CHECK_S) == "layerless_start"


def test_layerless_risky_print_uses_printing_time_for_the_early_window():
    watch = vw.VisualPrintWatch(sensitivity=3, early_layers=10, early_interval_s=120)
    watch.set_mode(vw.WATCH_MODE_EARLY)
    window = 10 * vw.EARLY_SECONDS_PER_LAYER
    assert watch.tick(None, 100.0, elapsed_s=119.0) is None
    assert watch.tick(None, 101.0, elapsed_s=120.0) == "layerless_start"
    assert watch.in_early_window(None) is True
    assert watch.tick(None, 221.0, elapsed_s=240.0) == "early_periodic"
    # Past the early window the normal periodic interval applies again.
    assert watch.tick(None, 400.0, elapsed_s=window + 1) is None
    assert watch.in_early_window(None) is False
    assert watch.tick(None, 221.0 + 900, elapsed_s=window + 600) == "periodic"


def test_layered_print_keeps_its_layer_schedule():
    watch = vw.VisualPrintWatch(sensitivity=3)
    assert watch.tick(0, 1000.0, elapsed_s=5000.0) is None  # int layer 0: not layerless
    assert watch.due(2, 1000.0) == "layer2"


def test_layerless_empty_bed_recheck_fires_on_the_timer():
    watch = vw.VisualPrintWatch(sensitivity=3)
    assert watch.tick(None, 1000.0, elapsed_s=200.0) == "layerless_start"
    watch.note_empty_idle(1000.0)
    assert watch.tick(None, 1000.0 + vw.EMPTY_IDLE_RECHECK_S, elapsed_s=260.0) == "empty_idle_recheck"


def test_listener_runs_layerless_checks_and_posts_one_notice(monkeypatch):
    posts = []
    launched = []
    monkeypatch.setattr(mel, "_vision_ready", lambda: True)
    monkeypatch.setattr(mel, "post", lambda text, **kw: posts.append(text) or True)
    monkeypatch.setattr(mel, "log", lambda line: None)
    monkeypatch.setattr(mel._config, "cfg", lambda: {"visual_watch": {"enabled": True, "sensitivity": 3}})

    listener = mel.Listener()
    listener.filename = "nolayers.gcode"
    listener.state = "printing"
    listener._watch_context_seen = True

    async def fake_check(reason, layer):
        launched.append((reason, layer))

    monkeypatch.setattr(listener, "visual_check", fake_check)

    async def run():
        listener.last_stats = {"print_duration": 10.0, "info": {"current_layer": None}}
        assert listener._visual_timer_tick(now=1000.0) is None
        listener.last_stats = {"print_duration": 200.0, "info": {"current_layer": None}}
        assert listener._visual_timer_tick(now=1200.0) == "layerless_start"
        await listener.last_visual_task
        listener.last_stats = {"print_duration": 1100.0, "info": {}}
        assert listener._visual_timer_tick(now=2100.0) == "periodic"
        await listener.last_visual_task
        await asyncio.gather(*listener.background_tasks)

    asyncio.run(run())
    assert launched == [("layerless_start", None), ("periodic", None)]
    assert len(posts) == 1
    assert "no layer numbers" in posts[0] and "on a timer" in posts[0]


# -- 2. auto-pause needs independent evidence ---------------------------------


def _frame(seed):
    """A real JPEG camera frame; different seeds are visibly different views."""
    import io
    import random
    from PIL import Image
    rng = random.Random(seed)
    image = Image.new("L", (160, 120))
    image.putdata([rng.randrange(40, 220) for _ in range(160 * 120)])
    out = io.BytesIO()
    image.convert("RGB").save(out, "JPEG")
    return out.getvalue()


def _reencoded(image, quality=70):
    """The same scene, decoded and encoded again: what a frozen feed re-serves."""
    import io
    from PIL import Image
    out = io.BytesIO()
    Image.open(io.BytesIO(image)).save(out, "JPEG", quality=quality)
    return out.getvalue()


def test_frame_signature_tells_a_frozen_feed_from_a_new_view():
    a = vw.frame_signature(_frame(1))
    assert vw.frames_differ(a, vw.frame_signature(_frame(1))) is False
    assert vw.frames_differ(a, vw.frame_signature(_reencoded(_frame(1)))) is False
    assert vw.frames_differ(a, vw.frame_signature(_frame(2))) is True
    assert vw.frame_signature(b"not a jpeg") is None
    assert vw.frames_differ(a, None) is None


def _decide(members, **overrides):
    inputs = dict(enabled=True, already_paused=False, printer_id="p1", print_identity="job-1",
                  state="printing", verdict="spaghetti", frame_counted=True, frame_age_s=5.0,
                  mode=vw.WATCH_MODE_EARLY, in_early_window=True)
    inputs.update(overrides)
    return vw.auto_pause_decision(members, **inputs)


def _members(*spec):
    return [vw.PauseEvidence(printer, identity, vw.frame_signature(_frame(seed)), at)
            for printer, identity, seed, at in spec]


GOOD = _members(("p1", "job-1", 1, 1000.0), ("p1", "job-1", 2, 1000.0 + vw.EVIDENCE_MIN_SPACING_S))


def test_auto_pause_decision_pauses_on_two_independent_views():
    pause, reason = _decide(GOOD)
    assert pause is True and "2 independent suspicious views" in reason


@pytest.mark.parametrize("overrides, why", [
    ({"enabled": False}, "off"),
    ({"already_paused": True}, "already"),
    ({"mode": vw.WATCH_MODE_NORMAL}, "early-layer window"),
    ({"in_early_window": False}, "early-layer window"),
    ({"verdict": "uncertain"}, "does not count"),
    ({"state": "paused"}, "not printing"),
    ({"state": None}, "not printing"),
    ({"frame_age_s": vw.FRAME_MAX_AGE_S + 1}, "too old"),
    ({"frame_age_s": None}, "too old"),
    ({"frame_counted": False}, "not new evidence"),
    ({"print_identity": "job-2"}, "another printer or print"),
    ({"printer_id": "p2"}, "another printer or print"),
])
def test_auto_pause_decision_refuses_without_every_condition(overrides, why):
    pause, reason = _decide(GOOD, **overrides)
    assert pause is False and why in reason


def test_auto_pause_decision_refuses_a_streak_that_is_not_independent():
    too_close = _members(("p1", "job-1", 1, 1000.0),
                         ("p1", "job-1", 2, 1000.0 + vw.EVIDENCE_MIN_SPACING_S - 1))
    frozen = _members(("p1", "job-1", 1, 1000.0), ("p1", "job-1", 1, 1200.0))
    mixed = _members(("p1", "job-0", 1, 1000.0), ("p1", "job-1", 2, 1200.0))
    assert _decide(too_close) == (False, "the streak is not made of independent views")
    assert _decide(frozen) == (False, "the streak is not made of independent views")
    assert _decide(mixed)[0] is False
    single = _decide(GOOD[:1])
    assert single[0] is False and "1 of 2" in single[1]
    assert _decide([])[0] is False


def _note(watch, seed, at, identity="job-1", age=1.0, layer=2, verdict="spaghetti", image=None):
    signature = vw.frame_signature(image if image is not None else _frame(seed))
    return watch.note_verdict(verdict, layer, printer_id="p1", print_identity=identity,
                              signature=signature, frame_age_s=age, now=at)


def test_streak_counts_views_not_checks():
    watch = vw.VisualPrintWatch(sensitivity=3)
    watch.set_mode(vw.WATCH_MODE_EARLY)
    assert _note(watch, 1, 1000.0) == 1
    # Every-layer checks seconds apart: one view, not two.
    assert _note(watch, 2, 1005.0) == 1 and watch.last_counted is False
    assert "independent" in watch.last_evidence
    # A frozen feed a minute later: a camera fault, not evidence.
    assert _note(watch, 0, 1100.0, image=_reencoded(_frame(1))) == 1
    assert "did not change" in watch.last_evidence
    # A stale frame never counts.
    assert _note(watch, 3, 1100.0, age=vw.FRAME_MAX_AGE_S + 1) == 1
    # A new view a minute later counts.
    assert _note(watch, 4, 1100.0) == 2 and watch.last_counted is True
    # A healthy verdict resets the streak.
    assert _note(watch, 5, 1200.0, verdict="ok") == 0


def test_streak_restarts_for_a_new_print():
    watch = vw.VisualPrintWatch(sensitivity=3)
    watch.set_mode(vw.WATCH_MODE_EARLY)
    _note(watch, 1, 1000.0, identity="job-1")
    assert _note(watch, 2, 1100.0, identity="job-2") == 1
    assert watch.pause_members[0].print_identity == "job-2"


def _auto_pause_listener(monkeypatch, frames, clock):
    alerts, pauses = [], []
    monkeypatch.setattr(mel, "_camera_snapshot_bytes", lambda: frames.pop(0))
    monkeypatch.setattr(mel, "_watch_now", lambda: clock[0])
    monkeypatch.setattr(mel, "_frame_changed", lambda before, after: True)
    monkeypatch.setattr(mel, "_visual_alert",
                        lambda filename, image, text, identity=None: alerts.append(text))
    monkeypatch.setattr(mel, "_auto_pause_early",
                        lambda filename, identity: pauses.append(filename) or "Paused")
    monkeypatch.setattr(mel, "post", lambda msg, **kw: True)
    monkeypatch.setattr(mel, "log", lambda line: None)
    monkeypatch.setattr(mel, "_visual_verdict", lambda *a, **k: ("spaghetti", "spaghetti found"))
    monkeypatch.setattr(pab, "visual_muted", lambda filename, identity: False)
    monkeypatch.setattr(mel._config, "cfg", lambda: {"visual_watch": {
        "enabled": True, "sensitivity": 3, "auto_pause_early": True,
        "early_layers": 10, "early_interval_s": 120}})
    listener = mel.Listener()
    listener.filename = "risky.gcode"
    listener.state = "printing"
    listener.visual_watch.set_mode(vw.WATCH_MODE_EARLY)
    return listener, alerts, pauses


def test_two_checks_seconds_apart_on_the_same_frame_never_pause(monkeypatch):
    """The reported failure: layer changes seconds apart on a frozen camera paused a healthy print."""
    still = _frame(7)
    clock = [5000.0]
    listener, alerts, pauses = _auto_pause_listener(monkeypatch, [still, _reencoded(still)], clock)
    asyncio.run(listener.visual_check("early_layer", 2))
    clock[0] += 4
    asyncio.run(listener.visual_check("early_layer", 3))
    assert len(alerts) == 2 and pauses == []
    assert listener.visual_watch.auto_paused is False


def test_frozen_frame_a_minute_later_still_never_pauses(monkeypatch):
    still = _frame(7)
    clock = [5000.0]
    listener, alerts, pauses = _auto_pause_listener(monkeypatch, [still, _reencoded(still)], clock)
    asyncio.run(listener.visual_check("early_layer", 2))
    clock[0] += 120
    asyncio.run(listener.visual_check("early_layer", 3))
    assert pauses == []


def test_two_independent_views_pause_once(monkeypatch):
    clock = [5000.0]
    listener, alerts, pauses = _auto_pause_listener(
        monkeypatch, [_frame(1), _frame(2), _frame(3)], clock)
    asyncio.run(listener.visual_check("early_layer", 2))
    clock[0] += vw.EVIDENCE_MIN_SPACING_S
    asyncio.run(listener.visual_check("early_layer", 3))
    assert pauses == ["risky.gcode"]
    assert "independent suspicious views" in alerts[-1]
    clock[0] += vw.EVIDENCE_MIN_SPACING_S
    asyncio.run(listener.visual_check("early_layer", 4))
    assert pauses == ["risky.gcode"]


def test_no_pause_when_the_printer_stopped_printing_during_the_alert(monkeypatch):
    clock = [5000.0]
    listener, alerts, pauses = _auto_pause_listener(monkeypatch, [_frame(1), _frame(2)], clock)
    asyncio.run(listener.visual_check("early_layer", 2))
    clock[0] += vw.EVIDENCE_MIN_SPACING_S

    def alert_then_pause(filename, image, text, identity=None):
        alerts.append(text)
        listener.state = "paused"  # the operator pressed Pause meanwhile

    monkeypatch.setattr(mel, "_visual_alert", alert_then_pause)
    asyncio.run(listener.visual_check("early_layer", 3))
    assert pauses == []


def test_local_escalation_needs_spaced_changed_views():
    from print_handy.hermes import local_vision as lv
    weak = [(0.35, [0.1, 0.1, 0.5, 0.5])]
    a, b, c = (vw.frame_signature(_frame(seed)) for seed in (1, 2, 3))
    streak = lv.FailureStreak()
    assert streak.note(weak, now=1000.0, signature=a)[0] == "uncertain"
    verdict, detail = streak.note(weak, now=1010.0, signature=b)
    assert verdict == "uncertain" and "not counted" in detail
    assert streak.note(weak, now=1100.0, signature=a)[0] == "uncertain"  # frozen frame
    assert streak.positives == 1
    verdict, detail = streak.note(weak, now=1100.0, signature=c)
    assert verdict == "spaghetti" and "2 consecutive checks" in detail


# -- 3. a watch that is on but cannot run says so, once per print ------------


def _start_print(monkeypatch, *, ready, reason="the local failure detector is not ready: x"):
    posts = []
    monkeypatch.setattr(mel, "post", lambda text, **kw: posts.append(text) or True)
    monkeypatch.setattr(mel, "run_announcer", lambda reason: None)
    monkeypatch.setattr(mel, "start_claimed", lambda name: True)
    monkeypatch.setattr(mel, "announce_warmup_done", lambda name: None)
    monkeypatch.setattr(mel, "post_exclude_objects", lambda name: None)
    monkeypatch.setattr(mel, "log", lambda line: None)
    monkeypatch.setattr(mel, "_vision_ready", lambda: ready)
    monkeypatch.setattr(vw, "watch_unavailable_reason", lambda api_key: reason)
    listener = mel.Listener()

    async def noop(*a, **k):
        return None

    for name in ("_begin_capture", "light_print_started", "_slot_self_check"):
        monkeypatch.setattr(listener, name, noop)
    monkeypatch.setattr(listener, "_settle_launch_lease", lambda: None)

    async def run():
        for state in ("standby", "printing", "paused", "printing"):
            await listener.on_status({"state": state, "filename": "watched.gcode"}, "test")
        await asyncio.gather(*listener.background_tasks)

    asyncio.run(run())
    return [p for p in posts if "Camera watch" in p]


def test_print_start_posts_why_the_watch_is_not_running(monkeypatch):
    notices = _start_print(monkeypatch, ready=False)
    # One notice for the print; the resume after a pause is not a new start.
    assert len(notices) == 1
    assert "not running" in notices[0] and "local failure detector is not ready" in notices[0]


def test_print_start_is_quiet_when_the_watch_runs(monkeypatch):
    assert _start_print(monkeypatch, ready=True) == []


def test_reason_names_the_misconfigured_source(monkeypatch):
    from print_handy.hermes import local_vision as lv
    monkeypatch.delenv("PRINT_VISION_URL", raising=False)
    monkeypatch.setattr(lv, "watch_source", lambda cfg=None: "endpoint")
    monkeypatch.setattr(vw._config, "service_url", lambda key: "")
    monkeypatch.setattr(vw.ai_routes, "overrides_route", lambda route: False)
    monkeypatch.setattr(vw.local_empty, "reference_ready", lambda: False)
    assert "watch_source is endpoint" in vw.watch_unavailable_reason(lambda: "")
