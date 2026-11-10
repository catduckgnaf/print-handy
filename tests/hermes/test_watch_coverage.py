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
