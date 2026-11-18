"""The quip a chat message carries is mirrored, unchanged, to the printer's screen."""
from __future__ import annotations

import random
import threading

import pytest

from print_handy.core import status_voice
from print_handy.runtime import screen_tip

STATUS_VOICE_LIVE = True  # tests/conftest.py: the real selector, so the shared line is the real one


@pytest.fixture
def posts(monkeypatch):
    """Every screen write, captured synchronously; printers resolve to a URL named for their id."""
    done = threading.Event()

    class Sent(list):
        def wait(self):
            return done.wait(5)

    sent = Sent()

    def fake_post(url, value):
        sent.append((url, value))
        done.set()

    monkeypatch.setattr(screen_tip, "_post", fake_post)
    monkeypatch.setattr(screen_tip, "_moonraker_url",
                        lambda printer_id: f"http://{printer_id or 'default'}:7125")
    return sent


def test_the_screen_gets_the_exact_line_the_chat_message_carried(tmp_path, posts):
    with screen_tip.routed_to("discord:voron"):
        line = status_voice.quip("quantity_selected", copies=2, identity="discord:user:1",
                                 event_key="intake-1:copies:2", state_path=tmp_path / "v.json",
                                 rng=random.Random(3))
    assert posts.wait()
    url, value = posts[0]
    assert url == "http://discord:voron:7125"
    assert value["title"] == line
    assert "Two prints at the same time man." in line
    assert "Office Space" in value["details"]
    assert value["source"] == "Print Handy"
    assert value["id"] and isinstance(value["posted_at"], float)


def test_a_replayed_message_resends_the_same_id(tmp_path, posts):
    kwargs = dict(copies=4, identity="discord:user:1", event_key="intake-2:copies:4",
                  state_path=tmp_path / "v.json")
    first = status_voice.quip("quantity_selected", rng=random.Random(1), **kwargs)
    second = status_voice.quip("quantity_selected", rng=random.Random(99), **kwargs)
    for t in list(threading.enumerate()):
        if t.name == "screen-tip":
            t.join(5)
    assert first == second
    assert len(posts) == 2
    assert posts[0][1]["id"] == posts[1][1]["id"]
    assert posts[0][1]["details"] == ""  # an ordinary quip has no details


def test_no_quip_means_no_screen_write(tmp_path, posts):
    # quantity_selected without a verified count never chooses a line.
    assert status_voice.quip("quantity_selected", identity="x",
                             state_path=tmp_path / "v.json") == ""
    assert posts == []


def test_a_printer_without_moonraker_is_skipped(monkeypatch):
    monkeypatch.setattr(screen_tip, "_moonraker_url", lambda printer_id: "")
    assert screen_tip.publish(event_id="a", title="joke") is False


def test_a_failing_write_never_reaches_the_chat_path(monkeypatch):
    failed = threading.Event()

    def boom(url, value):
        failed.set()
        raise OSError("printer offline")

    monkeypatch.setattr(screen_tip, "_post", boom)
    monkeypatch.setattr(screen_tip, "_moonraker_url", lambda printer_id: "http://p:7125")
    assert screen_tip.publish(event_id="a", title="joke") is True
    assert failed.wait(5)


def test_routing_is_scoped_to_the_block():
    with screen_tip.routed_to("discord:a"):
        assert screen_tip._PRINTER.get() == "discord:a"
    assert screen_tip._PRINTER.get() is None


def test_record_shape_is_the_one_helixscreen_reads():
    value = screen_tip.record(event_id="e", title="t", details="d", now=12.3456)
    assert value == {"id": "e", "title": "t", "details": "d", "source": "Print Handy",
                     "posted_at": 12.346}
    assert (screen_tip.NAMESPACE, screen_tip.KEY) == ("helix-screen", "featured_tip")
