"""#1672 Part 1: every printer family's terminal post carries the print's roast.

Driven through the production entry points: the polling
``FamilyEventListener`` with a fake status function (PrusaLink), the
``BambuEventSink`` with a fake report stream, and
``family_control.post_family_event`` itself. One voice per post: a post
with a roast carries no status quip; roasts off is an exact no-op.
"""
from __future__ import annotations

import threading

import pytest

from print_handy.core import status_voice
from print_handy.runtime import family_control, family_event_listener as fel
from print_handy.runtime import moonraker_event_listener as mel
from print_handy.runtime import roast_post, screen_tip

REMOTE = "benchy.gcode"
FACTS = "Print complete: Benchy\nPrinter: Test"
JOB = {"title": "Benchy"}
PLATE = {"material": "PLA", "color": "Purple", "filament_g": 12.0, "seconds": 4500}


def _drain():
    for t in list(threading.enumerate()):
        if t.name.startswith("screen-tip"):
            t.join(5)


@pytest.fixture
def rig(monkeypatch, tmp_path):
    """Chat posts and screen writes captured; the quip voice seeded."""
    posts = []

    class Mel:
        @staticmethod
        def post(text, **kwargs):
            posts.append((text, kwargs))
            return True

    monkeypatch.setattr(family_control, "_mel", lambda: Mel)
    monkeypatch.setattr(family_control.print_summary, "format_event",
                        lambda kind, filename: FACTS)
    monkeypatch.setattr(roast_post.print_summary, "find_job",
                        lambda filename, *a, **kw: (dict(JOB), dict(PLATE)))
    monkeypatch.setattr(status_voice, "quip", lambda stage, **kw: f"[{stage} quip]")
    sent = []
    monkeypatch.setattr(screen_tip, "_post", lambda url, value: sent.append((url, value)))
    monkeypatch.setattr(screen_tip, "_moonraker_url",
                        lambda printer_id: f"http://{printer_id or 'default'}:7125")
    monkeypatch.setattr(mel, "ROAST_STATE", tmp_path / "roast-state.json")
    yield posts, sent
    _drain()


def _listener(tmp_path, statuses, printer_id="prusa1"):
    items = list(statuses)

    def next_status():
        return items.pop(0) if len(items) > 1 else items[0]

    return fel.FamilyListener(
        "prusalink", next_status, printer_id=printer_id,
        claim_fn=lambda _f: False,
        start_poster=lambda kind, fn: True,
        pause_poster=lambda *a: True,
        retirer=lambda fn, headline: 1,
        beat=lambda **d: None, sleep=lambda _s: None, clock=lambda: 0.0,
        state_path=tmp_path / "fam.json")


def st(state, filename=REMOTE):
    return {"state": state, "filename": filename, "progress": 0.5}


def test_prusalink_completion_post_carries_a_roast_and_no_quip(rig, tmp_path):
    posts, sent = rig
    listener = _listener(tmp_path, [st("standby", ""), st("printing"), st("complete")])
    for _ in range(3):
        listener.poll_once()
    _drain()
    (text, kwargs), = posts
    assert kwargs == {"ping_kind": "complete", "ping_job_key": REMOTE}
    assert text.startswith(FACTS + "\n\n")  # facts, a blank line, the roast
    line = text[len(FACTS) + 2:]
    assert line and "\n" not in line
    assert "quip" not in text  # one voice per post
    (url, value), = sent
    assert url == "http://prusa1:7125"  # the posting listener's own printer
    assert value["title"] == line
    assert value["details"] == "Your last print, Benchy, finished. 1h 15m, Purple PLA, 12 g."


def test_bambu_completion_post_carries_a_roast_and_no_quip(rig):
    posts, sent = rig
    sink = family_control.BambuEventSink(
        claim_fn=lambda _f: False, printer_id="bambu1",
        start_poster=lambda kind, fn: True, pause_poster=lambda *a: True,
        retirer=lambda fn, headline: 1)
    sink("status", {"status": {"state": "standby", "filename": ""}})
    sink("status", {"status": {"state": "printing", "filename": REMOTE},
                    "transition": "started"})
    sink("completion", {"status": {"state": "complete", "filename": REMOTE}})
    _drain()
    (text, _kwargs), = posts
    assert text.startswith(FACTS + "\n\n")
    assert "quip" not in text
    (url, value), = sent
    assert url == "http://bambu1:7125"
    assert value["title"] == text[len(FACTS) + 2:]


def test_bambu_error_post_draws_from_the_failed_bank(rig):
    posts, sent = rig
    sink = family_control.BambuEventSink(
        claim_fn=lambda _f: False, printer_id="bambu1",
        start_poster=lambda kind, fn: True, pause_poster=lambda *a: True,
        retirer=lambda fn, headline: 1)
    sink("status", {"status": {"state": "standby", "filename": ""}})
    sink("status", {"status": {"state": "printing", "filename": REMOTE},
                    "transition": "started"})
    sink("error", {"status": {"state": "error", "filename": REMOTE}})
    _drain()
    (text, _kwargs), = posts
    assert text.startswith(FACTS + "\n\n") and "quip" not in text
    (_url, value), = sent
    assert value["details"] == (
        "Your last print, Benchy, stopped with an error. 1h 15m, Purple PLA, 12 g.")


def test_cancelled_post_draws_its_own_bank_and_details_carry_no_grams(rig, tmp_path):
    posts, sent = rig
    listener = _listener(tmp_path, [st("standby", ""), st("printing"), st("standby", "")])
    for _ in range(3):
        listener.poll_once()
    _drain()
    (text, kwargs), = posts
    assert kwargs["ping_kind"] == "cancelled"
    assert text.startswith(FACTS + "\n\n")
    (_url, value), = sent
    assert value["details"] == "Your last print, Benchy, was cancelled. 1h 15m, Purple PLA."
    assert "12 g" not in value["details"]


def test_roasts_off_the_terminal_post_is_exactly_todays(rig, monkeypatch):
    posts, sent = rig
    monkeypatch.setitem(mel._config.cfg()["vision"], "roast_enabled", False)
    assert family_control.post_family_event("complete", REMOTE, printer_id="prusa1") is True
    _drain()
    assert posts == [(FACTS + "\n[print_complete quip]",
                      {"ping_kind": "complete", "ping_job_key": REMOTE})]
    assert sent == []


def test_a_reposted_terminal_notice_repeats_the_same_line(rig):
    posts, sent = rig
    family_control.post_family_event("complete", REMOTE, printer_id="prusa1")
    family_control.post_family_event("complete", REMOTE, printer_id="prusa1")
    _drain()
    assert posts[0][0] == posts[1][0]
    assert sent[0][1]["id"] == sent[1][1]["id"]


def test_the_screen_write_goes_to_the_posting_printers_own_id(rig):
    _posts, sent = rig
    family_control.post_family_event("complete", REMOTE, printer_id="printer-a")
    family_control.post_family_event("complete", REMOTE, printer_id="printer-b")
    _drain()
    assert [url for url, _value in sent] == [
        "http://printer-a:7125", "http://printer-b:7125"]


def test_roast_for_print_statuses_and_the_cancelled_grams_rule(rig):
    _posts, sent = rig
    line = roast_post.roast_for_print(REMOTE, "cancelled", printer_id="prusa1",
                                      key="cancelled:benchy.gcode")
    _drain()
    assert line
    (_url, value), = sent
    assert "12 g" not in value["details"]
    assert roast_post.roast_for_print(REMOTE, "completed", printer_id="prusa1",
                                      key="completed:benchy.gcode")


def test_cancelled_roast_drops_grams_even_from_caller_facts(rig):
    # duckcatgnaf on #1673 (roast_post.py): caller facts must not re-add
    # grams to a cancelled print's details.
    _posts, sent = rig
    assert roast_post.roast_for_print(REMOTE, "cancelled", printer_id="prusa1",
                                      key="cancelled:caller-grams",
                                      facts={"grams": 99})
    _drain()
    (_url, value), = sent
    assert "99 g" not in value["details"]
