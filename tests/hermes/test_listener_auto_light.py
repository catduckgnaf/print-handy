"""Listener-level auto light-off (#516) checks: the Moonraker listener's
light_idle_tick against a real AutoLightPolicy, with the camera, vision
endpoint and light sender faked. Offline throughout.
"""
import asyncio
import time

from print_handy.hermes import light_control
from print_handy.hermes import moonraker_event_listener as mel

ON_SCRIPT, OFF_SCRIPT = light_control.light_scripts("led chamber_led")


def _listener(monkeypatch, *, verdict=("empty_idle", "clear"), snapshot=b"frame"):
    listener = mel.Listener()
    listener.state = "standby"
    policy = light_control.AutoLightPolicy(10, check_interval_s=0)
    policy.print_finished(time.time() - 3600)
    policy.note_verdict("empty_idle", time.time() - 3600)  # streak already spans the window
    listener._auto_light = policy
    listener.light_target = "led chamber_led"
    listener.light_target_resolved = True
    sent = []
    monkeypatch.setattr(mel, "_vision_ready", lambda: True)
    monkeypatch.setattr(mel, "_send_light_script", lambda script: sent.append(script))
    monkeypatch.setattr(mel, "log", lambda line: None)
    if isinstance(snapshot, Exception):
        def _raise():
            raise snapshot
        monkeypatch.setattr(mel, "_camera_snapshot_bytes", _raise)
    else:
        monkeypatch.setattr(mel, "_camera_snapshot_bytes", lambda: snapshot)
    monkeypatch.setattr(mel._visual_watch, "frame_dark", lambda image: False)
    if callable(verdict):
        monkeypatch.setattr(mel, "_visual_verdict", verdict)
    else:
        monkeypatch.setattr(mel, "_visual_verdict", lambda image, prompt: verdict)
    return listener, policy, sent


def test_idle_check_turns_the_light_off_when_the_streak_spans(monkeypatch):
    listener, _policy, sent = _listener(monkeypatch)
    asyncio.run(listener.light_idle_tick())
    assert sent == [OFF_SCRIPT]


def test_idle_check_revalidates_state_before_switching_off(monkeypatch):
    """P2-200: a print that starts while the vision request is in flight
    must not get the OFF command — the verdict describes the plate as it
    was before the print, and the cached state can only be trusted after
    a fresh look at it."""
    holder = {}

    def verdict(image, prompt):
        holder["listener"].state = "printing"  # the print started mid-check
        return "empty_idle", "clear"

    listener, _policy, sent = _listener(monkeypatch, verdict=verdict)
    holder["listener"] = listener
    asyncio.run(listener.light_idle_tick())
    assert sent == []


def test_a_failed_snapshot_resets_the_empty_streak(monkeypatch):
    """P2-201: an unobservable plate is an uncertain observation. A camera
    outage must not let one post-recovery empty verdict satisfy the
    sustained-empty requirement on a streak from before the outage."""
    listener, policy, sent = _listener(monkeypatch, snapshot=RuntimeError("camera down"))
    assert policy.empty_since is not None
    asyncio.run(listener.light_idle_tick())
    assert policy.empty_since is None
    assert policy.last_check_at is not None  # the cadence still advances
    assert sent == []
