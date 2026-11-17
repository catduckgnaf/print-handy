"""Regression tests for the 2026-11 code review fixes (#1583 to #1590 track the rest)."""
import json
import os
import threading
import time
from types import SimpleNamespace

from print_handy.runtime import family_control as fc
from print_handy.runtime import moonraker_discord_announcements as announcer
from print_handy.runtime import moonraker_event_listener as listener
from print_handy.runtime import print_alert_buttons as pab


def _job(state, filename="a.gcode"):
    return {"state": state, "filename": filename}


def test_family_pause_that_did_not_take_is_not_reported_as_success():
    sent = []
    adapter = SimpleNamespace(control=sent.append, connection="octoprint")
    control = fc.FamilyControl(adapter, status_fn=lambda: _job("printing"), sleep=lambda s: None)
    ok, text = control.pause("a.gcode", wait_s=0.05, poll_s=0)
    assert sent == ["pause"]                      # sent once, never retried
    assert ok is False
    assert "still reports printing" in text


def test_family_pause_that_took_is_success():
    reads = iter([_job("printing"), _job("paused")])
    adapter = SimpleNamespace(control=lambda verb: None, connection="octoprint")
    control = fc.FamilyControl(adapter, status_fn=lambda: next(reads), sleep=lambda s: None)
    ok, text = control.pause("a.gcode", wait_s=5, poll_s=0)
    assert ok is True and "Paused" in text


def test_announcer_state_that_is_not_an_object_reads_as_empty(tmp_path, monkeypatch):
    state = tmp_path / "announcements.json"
    monkeypatch.setattr(announcer, "STATE_PATH", state)
    for content in ("null", "[]", "\"text\"", "{not json", ""):
        state.write_text(content)
        assert announcer.load_state() == {}
    state.write_bytes(b"\xff\xfe\x00bad")           # undecodable bytes used to raise
    assert announcer.load_state() == {}
    state.unlink()
    state.mkdir()                                   # an OSError other than "not found"
    assert announcer.load_state() == {}


def test_listener_atomic_write_uses_its_own_temp_file_and_leaves_none(tmp_path):
    target = tmp_path / "state" / "claims.json"
    shared_tmp = target.with_suffix(".json.tmp")
    shared_tmp.parent.mkdir(parents=True)
    shared_tmp.write_text("another writer's half-written file")
    listener.atomic_write_json(target, {"a": 1})
    assert json.loads(target.read_text()) == {"a": 1}
    # The old fixed name was truncated and then renamed away by this writer.
    assert shared_tmp.read_text() == "another writer's half-written file"
    assert sorted(p.name for p in target.parent.iterdir()) == ["claims.json", "claims.json.tmp"]


def test_alert_store_write_holds_a_lock_file_and_leaves_no_temp(tmp_path, monkeypatch):
    store = tmp_path / "alerts.json"
    monkeypatch.setattr(pab, "STATE", store)
    lock = store.with_suffix(".lock")
    seen = []
    real_save = pab._save

    def watching_save(data):
        seen.append(lock.exists())
        real_save(data)

    monkeypatch.setattr(pab, "_save", watching_save)
    aid = pab.record("paused", "a.gcode")
    assert seen == [True]                           # held across load, modify, replace
    assert not lock.exists()                        # and released
    assert pab.take(aid)["filename"] == "a.gcode"
    assert pab.take(aid) is None
    assert sorted(p.name for p in tmp_path.iterdir()) == ["alerts.json"]


def test_alert_store_waits_for_another_holder_of_the_lock(tmp_path, monkeypatch):
    store = tmp_path / "alerts.json"
    monkeypatch.setattr(pab, "STATE", store)
    lock = store.with_suffix(".lock")
    lock.write_text("")                             # another process holds it
    released_at = []

    def release():
        time.sleep(0.2)
        released_at.append(time.monotonic())
        lock.unlink()

    worker = threading.Thread(target=release)
    worker.start()
    pab.record("paused", "a.gcode")
    wrote_at = time.monotonic()
    worker.join()
    assert released_at and wrote_at >= released_at[0]


def test_alert_store_breaks_a_crashed_holders_lock(tmp_path, monkeypatch):
    store = tmp_path / "alerts.json"
    monkeypatch.setattr(pab, "STATE", store)
    lock = store.with_suffix(".lock")
    lock.write_text("")
    old = time.time() - 3600
    os.utime(lock, (old, old))
    started = time.monotonic()
    aid = pab.record("paused", "a.gcode")
    assert time.monotonic() - started < 2           # did not sit out the wait
    assert pab.peek(aid) is not None
    assert not lock.exists()
