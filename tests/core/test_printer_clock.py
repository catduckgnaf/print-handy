"""Printer clock watch (#807).

An AD5X on ZMOD comes back from a power cycle with its clock at
31 Dec 2025 and can take a long time to sync again. The watch is the
post-reboot half of the response: it notices the clock is still wrong,
says so once, keeps re-checking, and says once when the clock catches
up. It never sets the clock and never blocks a print. All clock answers
come from a fake reader or a fake HTTP response — no network, no printer.
"""
import asyncio
from email.message import Message

import pytest

from print_handy.core import printer_clock as pc


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeReader:
    """read_clock_skew's shape: seconds the host is ahead, or None when unknown."""

    def __init__(self, skew):
        self.skew = skew
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if isinstance(self.skew, Exception):
            raise self.skew
        return self.skew


def watch(skew, clock=None):
    reader = FakeReader(skew)
    return pc.PrinterClockWatch(reader, clock=clock or FakeClock()), reader


# --------------------------------------------------------------------- skew text


def test_describe_skew_days_behind():
    text = pc.describe_skew(306 * 86400)
    assert "306" in text and "day" in text and "behind" in text


def test_describe_skew_minutes_ahead():
    text = pc.describe_skew(-25 * 60)
    assert "25" in text and "minute" in text and "ahead" in text


def test_describe_skew_hours():
    text = pc.describe_skew(3 * 3600)
    assert "3" in text and "hour" in text


# --------------------------------------------------------------------- HTTP reader


class FakeResponse:
    def __init__(self, date_header):
        self.headers = Message()
        if date_header is not None:
            self.headers["Date"] = date_header

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_read_clock_skew_from_date_header(monkeypatch):
    stamp = "Wed, 31 Dec 2025 19:00:00 GMT"
    monkeypatch.setattr(pc.urllib.request, "urlopen", lambda req, timeout=0: FakeResponse(stamp))
    from email.utils import parsedate_to_datetime
    expected = 1_800_000_000.0 - parsedate_to_datetime(stamp).timestamp()
    skew = pc.read_clock_skew("http://printer:7125", now=lambda: 1_800_000_000.0)
    assert skew == pytest.approx(expected)
    assert skew > pc.CLOCK_SKEW_WARN_S  # the rebooted-printer case reads as far behind


def test_read_clock_skew_missing_header_is_unknown(monkeypatch):
    monkeypatch.setattr(pc.urllib.request, "urlopen", lambda req, timeout=0: FakeResponse(None))
    assert pc.read_clock_skew("http://printer:7125", now=lambda: 0.0) is None


def test_read_clock_skew_unreachable_is_unknown(monkeypatch):
    def boom(req, timeout=0):
        raise OSError("no route")
    monkeypatch.setattr(pc.urllib.request, "urlopen", boom)
    assert pc.read_clock_skew("http://printer:7125", now=lambda: 0.0) is None


def test_read_clock_skew_empty_url_is_unknown():
    assert pc.read_clock_skew("") is None
    assert pc.read_clock_skew(None) is None


# --------------------------------------------------------------------- watch cycle


def test_first_wrong_reading_announces_once():
    w, reader = watch(306 * 86400)
    messages = w.poll()
    assert len(messages) == 1
    assert "has not synced" in messages[0]
    assert "306" in messages[0]
    assert w.poll() == []  # still wrong, already said: silent
    assert reader.calls == 2  # but it did re-check


def test_small_skew_never_announces():
    w, _ = watch(120)  # two minutes: ordinary drift, not a rebooted clock
    assert w.poll() == []
    assert w.poll() == []


def test_synced_after_wrong_announces_once():
    clock = FakeClock()
    w, reader = watch(306 * 86400, clock)
    assert len(w.poll()) == 1
    clock.advance(600)
    reader.skew = 5
    messages = w.poll()
    assert len(messages) == 1
    assert "synced" in messages[0]
    assert w.poll() == []


def test_unknown_readings_neither_announce_nor_clear_wrong_state():
    w, reader = watch(306 * 86400)
    assert len(w.poll()) == 1
    reader.skew = None  # printer unreachable mid-episode: not "synced"
    assert w.poll() == []
    reader.skew = RuntimeError("boom")  # a raising reader is unknown too
    assert w.poll() == []
    reader.skew = 0  # genuinely synced now
    assert len(w.poll()) == 1


def test_still_wrong_reminder_fires_once_after_remind_delay():
    clock = FakeClock()
    w, _ = watch(3600, clock)
    assert len(w.poll()) == 1
    clock.advance(pc.REMIND_AFTER_S - 1)
    assert w.poll() == []
    clock.advance(2)
    messages = w.poll()
    assert len(messages) == 1
    assert "still has not synced" in messages[0]
    clock.advance(pc.REMIND_AFTER_S * 2)
    assert w.poll() == []  # no nagging beyond the one reminder


def test_ahead_clock_also_announces():
    w, _ = watch(-2 * 86400)
    messages = w.poll()
    assert len(messages) == 1
    assert "ahead" in messages[0]


def test_new_wrong_episode_after_sync_announces_again():
    w, reader = watch(86400)
    assert len(w.poll()) == 1
    reader.skew = 0
    assert len(w.poll()) == 1
    reader.skew = 86400  # rebooted again: a new episode is news again
    assert len(w.poll()) == 1


# --------------------------------------------------------------------- listener wiring


def test_listener_poll_posts_watch_messages_once(monkeypatch):
    from print_handy.hermes import moonraker_event_listener as mel

    listener = mel.Listener()
    clock = FakeClock()
    reader = FakeReader(306 * 86400)
    listener.clock_watch = pc.PrinterClockWatch(reader, clock=clock)
    posted = []
    monkeypatch.setattr(mel, "post", lambda message: posted.append(message) or True)

    asyncio.run(listener.poll_printer_clock())
    asyncio.run(listener.poll_printer_clock())
    assert len(posted) == 1
    assert "has not synced" in posted[0]

    reader.skew = 0
    asyncio.run(listener.poll_printer_clock())
    assert len(posted) == 2
    assert "synced" in posted[1]
