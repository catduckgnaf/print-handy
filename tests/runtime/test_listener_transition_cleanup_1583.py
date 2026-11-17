"""Regression tests for #1583: a transition's alert and cleanup are lost
when anything raises after the state is committed.

The listener commits a transition's state and at-most-once claim before
its alerts post, so a raise in between can never be retried — a reconnect
re-reads the same state and sees no transition. These tests pin the fix:

- ``classify_transition`` is a pure function and classifies every
  transition shape the body acts on.
- Each side effect in the transition body is guarded on its own: a
  ``post_alert`` that raises on a pause still yields exactly one alert
  (the plain-text fallback), and the steps after the alert still run.
- A terminal transition whose alert raises still runs its end-of-print
  cleanup (``_print_ended``) exactly once, and the operator still gets
  exactly one fallback alert from ``on_status``.

Everything drives Listener.on_status with fake Moonraker state sequences;
no printer, no Discord.
"""
import asyncio

import pytest

from print_handy.runtime import klipper_report
from print_handy.runtime import moonraker_event_listener as mel
from print_handy.runtime import print_alert_buttons as pab

FILE = "charm.gcode"


@pytest.mark.parametrize("old,new_state,expected", [
    # (old, new_state) -> (phase, fresh_start, gone)
    (None, "printing", ("start", True, False)),        # reconnect mid-print
    ("standby", "printing", ("start", True, False)),
    ("paused", "printing", (None, False, False)),      # a resume is not a start
    ("printing", "paused", (None, False, False)),
    ("standby", "paused", (None, False, False)),
    ("printing", "cancelled", ("terminal", False, False)),
    ("paused", "error", ("terminal", False, False)),
    ("printing", "standby", ("terminal", False, True)),   # gone (#370)
    ("paused", "standby", ("terminal", False, True)),     # gone (#370)
    ("printing", "complete", (None, False, False)),    # announcer owns completion
    ("paused", "complete", (None, False, False)),
    (None, "complete", (None, False, False)),
    ("standby", "standby", (None, False, False)),
    (None, "standby", (None, False, False)),
])
def test_classify_transition(old, new_state, expected):
    assert mel.classify_transition(old, new_state) == expected


def _make_listener(monkeypatch, *, post_alert=None, post=None):
    records = {"posted": [], "alerts": [], "announced": [], "ended": [], "retired": []}

    def _post(message, **kw):
        records["posted"].append(message)
        return True

    def _post_alert(message, kind, filename, **kw):
        records["alerts"].append((kind, message))
        return True

    monkeypatch.setattr(mel, "post", post or _post)
    monkeypatch.setattr(mel, "post_alert", post_alert or _post_alert)
    monkeypatch.setattr(mel, "run_announcer",
                        lambda reason: records["announced"].append(reason))
    monkeypatch.setattr(mel, "start_claimed", lambda name: False)
    monkeypatch.setattr(mel, "log", lambda line: None)
    monkeypatch.setattr(mel, "warn", lambda line: None)
    monkeypatch.setattr(mel, "_buttonless_standalone", lambda: None)

    async def _no_reminder():
        return None
    monkeypatch.setattr(mel, "maintenance_cancel_reminder", _no_reminder)
    monkeypatch.setattr(klipper_report, "report", lambda filename, *a, **kw: "Klipper report stub")
    monkeypatch.setattr(klipper_report, "cancelled_by_bot", lambda: True)
    monkeypatch.setattr(pab, "retire_alerts",
                        lambda token, filename, note, kinds=("paused",), headline=None:
                        records["retired"].append((filename, kinds, headline)) or 1)

    original_ended = mel.Listener._print_ended

    async def _counting_ended(self, new_state):
        records["ended"].append(new_state)
        await original_ended(self, new_state)

    monkeypatch.setattr(mel.Listener, "_print_ended", _counting_ended)
    return mel.Listener(), records


def _drive(listener, transitions):
    async def run():
        for state in transitions:
            await listener.on_status({"state": state, "filename": FILE}, "test")
        await asyncio.gather(*listener.background_tasks)
    asyncio.run(run())
    return listener


def _lifecycle_posts(records):
    # The camera-watch coverage notice (#1169) is not a print-lifecycle
    # post; it has its own tests in test_watch_coverage.py.
    return [p for p in records["posted"] if "Camera watch is on but not running" not in p]


def test_post_alert_raising_on_pause_still_yields_one_alert_and_one_cleanup(monkeypatch):
    """The #1583 required test: post_alert raises on the pause.

    The operator still gets exactly one pause alert (the plain-text
    fallback), the announcer still runs for the pause transition, and
    when the print then ends, its cleanup runs exactly once.
    """
    def _raising_alert(message, kind, filename, **kw):
        raise RuntimeError("discord refused the alert")

    listener, records = _make_listener(monkeypatch, post_alert=_raising_alert)
    _drive(listener, ["standby", "printing", "paused", "complete"])

    posted = _lifecycle_posts(records)
    pause_posts = [p for p in posted if "paused" in p.lower()]
    assert len(pause_posts) == 1                     # one alert, not zero, not two
    assert records["announced"].count("printing->paused") == 1
    assert records["ended"] == ["complete"]          # one cleanup


def test_post_alert_raising_on_pause_alone_still_runs_the_announcer(monkeypatch):
    def _raising_alert(message, kind, filename, **kw):
        raise RuntimeError("discord refused the alert")

    listener, records = _make_listener(monkeypatch, post_alert=_raising_alert)
    _drive(listener, ["standby", "printing", "paused"])

    posted = _lifecycle_posts(records)
    assert len([p for p in posted if "paused" in p.lower()]) == 1
    assert records["announced"].count("printing->paused") == 1
    assert listener.state == "paused"


def test_classify_runout_raising_on_pause_still_posts_one_generic_alert(monkeypatch):
    async def _raising_classify(self):
        raise RuntimeError("sensor read exploded")

    monkeypatch.setattr(mel.Listener, "classify_runout", _raising_classify)
    listener, records = _make_listener(monkeypatch)
    _drive(listener, ["standby", "printing", "paused"])

    assert [kind for kind, _ in records["alerts"]] == ["paused"]
    assert records["announced"].count("printing->paused") == 1
    assert listener.state == "paused"


def test_terminal_alert_raising_still_runs_cleanup_once_and_posts_one_fallback(monkeypatch):
    """An error post that raises: _print_ended runs once (from the
    _on_status guarantee) and on_status posts exactly one fallback."""
    def _post(message, **kw):
        if message.startswith("Print stopped with an error"):
            raise RuntimeError("discord refused the error post")
        records_posted.append(message)
        return True

    records_posted = []
    listener, records = _make_listener(monkeypatch, post=_post)
    _drive(listener, ["standby", "printing", "error"])

    assert records["ended"] == ["error"]             # one cleanup
    fallbacks = [p for p in records_posted if p.startswith("Printer now reports error")]
    assert len(fallbacks) == 1                       # one alert
    assert listener.state == "error"
