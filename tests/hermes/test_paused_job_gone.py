"""Regression tests for #370: a print that ends at the printer must still notify.

A job that ends from an active state into Moonraker "standby" (printer-side
stop, job removed, Klipper/printer restart during a long pause) never passes
through a TERMINAL state, so the listener posted nothing and the paused
prompt kept live Resume/Cancel buttons on a dead job. These tests drive
Listener.on_status with fake Moonraker state sequences; no printer, no
Discord.
"""
import asyncio

from print_handy.hermes import klipper_report
from print_handy.hermes import moonraker_event_listener as mel
from print_handy.hermes import print_alert_buttons as pab

FILE = "charm.gcode"


def _drive(monkeypatch, transitions):
    posted, alerts, retired = [], [], []
    monkeypatch.setattr(mel, "post", lambda message: posted.append(message) or True)
    monkeypatch.setattr(mel, "post_alert",
                        lambda message, kind, filename, **kw: alerts.append((kind, message)) or True)
    monkeypatch.setattr(mel, "run_announcer", lambda reason: None)
    monkeypatch.setattr(mel, "start_claimed", lambda name: False)
    monkeypatch.setattr(mel, "log", lambda line: None)
    monkeypatch.setattr(mel, "_buttonless_standalone", lambda: None)

    async def _no_reminder():
        return None
    monkeypatch.setattr(mel, "maintenance_cancel_reminder", _no_reminder)
    monkeypatch.setattr(klipper_report, "report", lambda filename, *a, **kw: "Klipper report stub")
    monkeypatch.setattr(klipper_report, "cancelled_by_bot", lambda: True)
    monkeypatch.setattr(pab, "retire_alerts",
                        lambda token, filename, note, kinds=("paused",), headline=None:
                        retired.append((filename, kinds, headline)) or 1)
    listener = mel.Listener()

    async def run():
        for state in transitions:
            await listener.on_status({"state": state, "filename": FILE}, "test")
        await asyncio.gather(*listener.background_tasks)  # the cancel report posts in the background
    asyncio.run(run())
    # The camera-watch coverage notice (#1169: one post at a print start
    # when the watch is on but cannot run) is not a print-lifecycle post;
    # it has its own tests in test_watch_coverage.py. These tests count
    # lifecycle posts, so drop the notice whatever order it landed in.
    posted = [p for p in posted if "Camera watch is on but not running" not in p]
    return posted, alerts, retired


def _terminal_posts(posted):
    """Everything after the start post: the terminal notification sequence."""
    return posted[1:]


def test_paused_then_standby_posts_cancelled_notice_once_and_retires_prompt(monkeypatch):
    posted, alerts, retired = _drive(monkeypatch, ["standby", "printing", "paused", "standby"])
    assert [kind for kind, _ in alerts] == ["paused"]
    terminal = _terminal_posts(posted)
    # One cancelled post: the summary leads the detailed report (no separate ack).
    assert len(terminal) == 1
    assert terminal[0].startswith("Print cancelled: charm") and "Klipper report stub" in terminal[0]
    assert retired == [(FILE, ("paused",), "Print ended at the printer.")]


def test_printing_then_standby_posts_cancelled_notice(monkeypatch):
    posted, alerts, retired = _drive(monkeypatch, ["standby", "printing", "standby"])
    assert alerts == []
    terminal = _terminal_posts(posted)
    assert len(terminal) == 1 and terminal[0].startswith("Print cancelled: charm")
    assert retired == [(FILE, ("paused",), "Print ended at the printer.")]


def test_cancelled_arriving_after_standby_does_not_post_twice(monkeypatch):
    posted, _, retired = _drive(monkeypatch,
                                ["standby", "printing", "paused", "standby", "cancelled"])
    terminal = _terminal_posts(posted)
    assert len(terminal) == 1  # one post: summary plus report
    assert terminal[0].startswith("Print cancelled: charm")
    assert sum("Klipper report stub" in p for p in terminal) == 1
    assert len(retired) == 1


def test_paused_then_cancelled_behaves_as_before_and_retires_prompt(monkeypatch):
    posted, alerts, retired = _drive(monkeypatch, ["standby", "printing", "paused", "cancelled"])
    assert [kind for kind, _ in alerts] == ["paused"]
    terminal = _terminal_posts(posted)
    assert len(terminal) == 1
    assert terminal[0].startswith("Print cancelled: charm")
    assert "Klipper report stub" in terminal[0]
    assert retired == [(FILE, ("paused",), "Print cancelled.")]


def test_paused_then_error_posts_error_and_retires_prompt(monkeypatch):
    posted, _, retired = _drive(monkeypatch, ["standby", "printing", "paused", "error"])
    terminal = _terminal_posts(posted)
    assert len(terminal) == 1 and terminal[0].startswith("Print stopped with an error: charm")
    assert retired == [(FILE, ("paused",), "Print stopped with an error.")]


def test_paused_resume_complete_retires_prompt(monkeypatch):
    # The completion post itself is the announcer's; the listener's job here
    # is only to close out the stale paused prompt.
    posted, alerts, retired = _drive(
        monkeypatch, ["standby", "printing", "paused", "printing", "complete"])
    assert [kind for kind, _ in alerts] == ["paused"]
    assert _terminal_posts(posted) == []
    assert retired == [(FILE, ("paused",), "Print complete.")]
