"""#1585: one shared state-file helper, corrupt quarantine, locked claims,
and the alert store under two processes."""
import json
import logging
import multiprocessing as mp
import os
import time
from pathlib import Path

import pytest

from print_handy.runtime import moonraker_discord_announcements as announcer
from print_handy.runtime import moonraker_event_listener as listener
from print_handy.runtime import print_alert_buttons as pab
from print_handy.runtime import state_file


# -- write_atomic / read_object ------------------------------------------------


def test_write_atomic_fsyncs_and_round_trips(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "state.json"
    fsynced = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (fsynced.append(fd), real_fsync(fd)))
    state_file.write_atomic(target, {"b": 1, "a": [1, 2]})
    assert fsynced, "the staged file must be fsynced before the rename"
    assert state_file.read_object(target) == {"b": 1, "a": [1, 2]}
    assert [p.name for p in target.parent.iterdir()] == ["state.json"]  # no temp left


def test_read_object_missing_file_is_empty_and_silent(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="print_handy.runtime.state_file"):
        assert state_file.read_object(tmp_path / "nope.json") == {}
    assert caplog.records == []
    assert list(tmp_path.iterdir()) == []


def test_corrupt_state_file_is_quarantined_and_warned_once(tmp_path, caplog):
    target = tmp_path / "claims.json"
    target.write_text("{not json")
    with caplog.at_level(logging.WARNING, logger="print_handy.runtime.state_file"):
        assert listener.read_json_object(target) == {}
        assert not target.exists()
        assert (tmp_path / "claims.json.corrupt").read_text() == "{not json"
        # A second corruption of the same path is quarantined too, but
        # the warning fires only once per path.
        target.write_text("[1, 2]")
        assert listener.read_json_object(target) == {}
        assert (tmp_path / "claims.json.corrupt").read_text() == "[1, 2]"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_announcer_corrupt_state_is_quarantined(tmp_path, monkeypatch):
    state = tmp_path / "announcements.json"
    monkeypatch.setattr(announcer, "STATE_PATH", state)
    state.write_text("{broken")
    assert announcer.load_state() == {}
    assert not state.exists()
    assert (tmp_path / "announcements.json.corrupt").read_text() == "{broken"
    announcer.save_state({"job_key": "a.gcode"})
    assert announcer.load_state() == {"job_key": "a.gcode"}


def test_alert_store_corrupt_state_is_quarantined(tmp_path, monkeypatch):
    store = tmp_path / "alerts.json"
    monkeypatch.setattr(pab, "STATE", store)
    store.write_text("not json at all")
    assert pab._load() == {}
    assert (tmp_path / "alerts.json.corrupt").read_text() == "not json at all"
    aid = pab.record("paused", "a.gcode")
    assert pab.take(aid)["filename"] == "a.gcode"


# -- locked --------------------------------------------------------------------


def test_locked_raises_on_timeout_when_asked(tmp_path):
    target = tmp_path / "registry.json"
    lock = Path(str(target) + ".lock")
    lock.write_text("")  # another holder, fresh (not stale)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        with state_file.locked(target, wait_s=0.2, stale_s=60.0, raise_on_timeout=True):
            pass
    assert time.monotonic() - started < 5
    lock.unlink()


def test_locked_is_reentrant_per_thread(tmp_path):
    target = tmp_path / "registry.json"
    with state_file.locked(target):
        with state_file.locked(target):
            pass
    assert not Path(str(target) + ".lock").exists()


# -- _claim_notification under two processes ------------------------------------


def _claim_child(path, barrier, queue):
    barrier.wait(timeout=30)
    queue.put(listener._claim_notification("job:7", "started", path))


def test_claim_notification_two_processes_claim_once(tmp_path):
    target = tmp_path / "claims.json"
    ctx = mp.get_context("fork")
    barrier = ctx.Barrier(2)
    queue = ctx.Queue()
    procs = [ctx.Process(target=_claim_child, args=(str(target), barrier, queue))
             for _ in range(2)]
    for proc in procs:
        proc.start()
    results = sorted(queue.get(timeout=30) for _ in procs)
    for proc in procs:
        proc.join(timeout=30)
        assert proc.exitcode == 0
    assert results == [False, True]  # exactly one process claimed the notice
    assert listener._claim_notification("job:7", "started", target) is False


# -- the alert store under two processes -----------------------------------------


def _record_child(store, count, queue):
    pab.STATE = Path(store)
    aids = [pab.record("paused", f"file-{i}.gcode") for i in range(count)]
    queue.put(aids)


def _take_child(store, done_flag, queue):
    pab.STATE = Path(store)
    taken = {}
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        rows = pab._load()
        aids = [aid for aid, row in rows.items() if isinstance(row, dict) and "kind" in row]
        progressed = False
        for aid in aids:
            row = pab.take(aid)
            if row is not None:
                taken[aid] = row["filename"]
                progressed = True
        if Path(done_flag).exists() and not aids:
            break
        if not progressed:
            time.sleep(0.01)
    queue.put(taken)


def test_alert_store_two_processes_no_row_lost_or_resurrected(tmp_path, monkeypatch):
    store = tmp_path / "alerts.json"
    monkeypatch.setattr(pab, "STATE", store)
    done_flag = tmp_path / "recording-done"
    count = 25
    ctx = mp.get_context("fork")
    queue = ctx.Queue()
    recorder = ctx.Process(target=_record_child, args=(str(store), count, queue))
    taker = ctx.Process(target=_take_child, args=(str(store), str(done_flag), queue))
    taker.start()
    recorder.start()
    recorder.join(timeout=60)
    assert recorder.exitcode == 0
    done_flag.write_text("done")
    taker.join(timeout=60)
    assert taker.exitcode == 0
    results = [queue.get(timeout=10), queue.get(timeout=10)]
    recorded = next(r for r in results if isinstance(r, list))
    taken = next(r for r in results if isinstance(r, dict))
    assert len(recorded) == count and len(set(recorded)) == count
    # Every recorded row was taken exactly once: none lost (a row the
    # recorder wrote that the taker's older snapshot overwrote) and none
    # resurrected (a taken row coming back for a second take).
    assert set(taken) == set(recorded)
    assert pab._load() == {}
    for aid in recorded:
        assert pab.take(aid) is None
