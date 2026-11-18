"""The roast a print's post carries is mirrored, unchanged, to that printer's screen."""
from __future__ import annotations

import threading

import pytest

from print_handy import listeners
from print_handy.runtime import moonraker_event_listener as mel
from print_handy.runtime import screen_tip


@pytest.fixture
def posts(monkeypatch, tmp_path):
    """Every screen write, captured; printers resolve to a URL named for their id."""
    sent = []
    lock = threading.Lock()

    def fake_post(url, value):
        with lock:
            sent.append((url, value))

    monkeypatch.setattr(screen_tip, "_post", fake_post)
    monkeypatch.setattr(screen_tip, "_moonraker_url",
                        lambda printer_id: f"http://{printer_id or 'default'}:7125")
    monkeypatch.setattr(mel, "ROAST_STATE", tmp_path / "roast-state.json")
    return sent


def _drain():
    for t in list(threading.enumerate()):
        if t.name == "screen-tip":
            t.join(5)


def test_the_screen_gets_the_exact_roast_the_post_carried(posts, monkeypatch):
    monkeypatch.setattr(listeners, "instance_printer_id", lambda: "voron")
    line = mel.generate_roast("Benchy", facts={"duration_s": 4500, "material": "PLA",
                                               "grams": 12.4}, key="done:benchy.gcode")
    _drain()
    assert line
    url, value = posts[0]
    assert url == "http://voron:7125"  # this listener's printer, not the default
    assert value["title"] == line
    assert value["details"] == "Your last print, Benchy, finished. 1h 15m, PLA, 12 g."
    assert value["source"] == "Print Handy"
    assert value["id"] and isinstance(value["posted_at"], float)


def test_a_retried_post_resends_the_same_id(posts):
    first = mel.generate_roast("Benchy", key="done:benchy.gcode")
    second = mel.generate_roast("Benchy", key="done:benchy.gcode")
    _drain()
    assert first == second
    assert len(posts) == 2
    assert posts[0][1]["id"] == posts[1][1]["id"]
    assert posts[0][0] == "http://default:7125"


def test_a_failed_print_says_how_it_ended(posts):
    line = mel.generate_roast("Dragon", status="failed", key="failed:dragon.gcode")
    _drain()
    assert line
    assert posts[0][1]["details"] == "Your last print, Dragon, stopped with an error."


def test_roasts_off_means_no_screen_write(posts, monkeypatch):
    monkeypatch.setitem(mel._config.cfg()["vision"], "roast_enabled", False)
    assert mel.generate_roast("Benchy", key="done:benchy.gcode") == ""
    _drain()
    assert posts == []


def test_a_printer_without_moonraker_is_skipped(monkeypatch):
    monkeypatch.setattr(screen_tip, "_moonraker_url", lambda printer_id: "")
    assert screen_tip.publish_roast("joke", title="Benchy") is False


def test_a_failing_write_never_reaches_the_post(monkeypatch):
    failed = threading.Event()

    def boom(url, value):
        failed.set()
        raise OSError("printer offline")

    monkeypatch.setattr(screen_tip, "_post", boom)
    monkeypatch.setattr(screen_tip, "_moonraker_url", lambda printer_id: "http://p:7125")
    assert screen_tip.publish_roast("joke", title="Benchy") is True
    assert failed.wait(5)


def test_details_name_the_print_number_and_copies(posts):
    """roast_facts() supplies print_number and copies (#1654); the tap names them."""
    mel.generate_roast("Benchy", facts={"print_number": 12, "copies": 3, "material": "PLA"},
                       key="done:benchy-12.gcode")
    _drain()
    assert posts[0][1]["details"] == "Your last print, Benchy, finished. print #12, 3 copies, PLA."
    assert screen_tip.details("Benchy", "completed", {"copies": 1}) == (
        "Your last print, Benchy, finished.")


def test_details_skip_facts_it_does_not_have():
    assert screen_tip.details("Vase", "cancelled") == "Your last print, Vase, was cancelled."
    assert (screen_tip.details("Vase", "spaghetti", {"color": "Red", "material": "PETG"})
            == "Your last print, Vase, turned into spaghetti. Red PETG.")


def test_record_shape_is_the_one_helixscreen_reads():
    value = screen_tip.record(event_id="e", title="t", details="d", now=12.3456)
    assert value == {"id": "e", "title": "t", "details": "d", "source": "Print Handy",
                     "posted_at": 12.346}
    assert (screen_tip.NAMESPACE, screen_tip.KEY) == ("helix-screen", "featured_tip")
