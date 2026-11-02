"""Interprocess printer-control lock (P1-33 / SAFE-6).

The control guard used to be a per-process threading.Lock, so the listener,
the bot, a print-now subprocess and the standalone ph-purge / ph-load-slot
CLIs each held their own lock and could interleave a purge, a slot load and
a print start on the same printer. These tests pin the shared file lock:
processes serialize, a dead holder releases, re-entry refuses instead of
deadlocking, and print_now refuses while a control action holds the lock.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from print_handy import settings
from print_handy.core import control_lock as cl
from print_handy.core import model_job

REPO = Path(__file__).resolve().parents[2]

CHILD = """
import sys, time
sys.path.insert(0, {repo!r})
from print_handy.core.control_lock import control_lock
lock = control_lock(sys.argv[2], state_dir=sys.argv[1])
if not lock.acquire(blocking=False):
    print("REFUSED", flush=True)
    raise SystemExit(3)
Path = __import__("pathlib").Path
Path(sys.argv[3]).write_text("held")
time.sleep(float(sys.argv[4]))
"""


def _spawn_holder(state_dir: Path, printer: str, ready: Path, hold_s: float = 30.0):
    proc = subprocess.Popen(
        [sys.executable, "-c", CHILD.format(repo=str(REPO)), str(state_dir), printer,
         str(ready), str(hold_s)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if ready.is_file():
            return proc
        if proc.poll() is not None:
            raise AssertionError(f"holder child exited early: {proc.stdout.read()}")
        time.sleep(0.05)
    proc.kill()
    raise AssertionError("holder child never acquired the lock")


def test_lock_file_lives_in_the_printer_state_dir(tmp_path):
    lock = cl.control_lock("printer-a", state_dir=tmp_path)
    assert lock.path() == tmp_path / "printers" / "printer-a" / "control.lock"


def test_same_process_distinct_objects_serialize(tmp_path):
    first = cl.PrinterControlLock(lambda: tmp_path / "a.lock")
    second = cl.PrinterControlLock(lambda: tmp_path / "a.lock")
    assert first.acquire(blocking=False)
    try:
        assert not second.acquire(blocking=False)
    finally:
        first.release()
    assert second.acquire(blocking=False)
    second.release()


def test_reentry_same_object_refuses_instead_of_deadlocking(tmp_path):
    lock = cl.PrinterControlLock(lambda: tmp_path / "a.lock")
    assert lock.acquire(blocking=False)
    try:
        # A nested non-blocking acquire reports busy, exactly like the
        # threading.Lock it replaces — it must not hang or self-grant.
        assert not lock.acquire(blocking=False)
    finally:
        lock.release()
    assert lock.acquire(blocking=False)
    lock.release()


def test_locks_are_keyed_by_printer(tmp_path):
    a = cl.control_lock("printer-a", state_dir=tmp_path)
    b = cl.control_lock("printer-b", state_dir=tmp_path)
    assert a.acquire(blocking=False)
    try:
        assert b.acquire(blocking=False)
        b.release()
    finally:
        a.release()


def test_factory_returns_one_object_per_printer_and_dir(tmp_path):
    assert cl.control_lock("printer-a", state_dir=tmp_path) is cl.control_lock("printer-a", state_dir=tmp_path)
    assert cl.control_lock("printer-a", state_dir=tmp_path) is not cl.control_lock("printer-b", state_dir=tmp_path)


def test_two_processes_serialize_and_death_releases(tmp_path):
    ready = tmp_path / "ready"
    proc = _spawn_holder(tmp_path, "printer-a", ready)
    try:
        mine = cl.control_lock("printer-a", state_dir=tmp_path)
        assert not mine.acquire(blocking=False)
    finally:
        proc.kill()  # SIGKILL: no cleanup runs; the kernel must free the lock
        proc.wait(timeout=10)
    assert mine.acquire(blocking=False)
    mine.release()


def test_blocking_acquire_waits_for_the_other_process(tmp_path):
    ready = tmp_path / "ready"
    proc = _spawn_holder(tmp_path, "printer-a", ready, hold_s=1.0)
    try:
        mine = cl.control_lock("printer-a", state_dir=tmp_path)
        started = time.monotonic()
        assert mine.acquire(timeout=10)
        assert time.monotonic() - started >= 0.8
        mine.release()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def test_print_now_refuses_while_a_control_action_holds_the_lock(tmp_path):
    key = settings.default_printer_id()
    held = cl.control_lock(key)  # same state dir print_now resolves
    assert held.acquire(blocking=False)
    try:
        target = tmp_path / "x.gcode"
        target.write_text("G1 X0\n")
        with pytest.raises(SystemExit) as exc:
            model_job.print_now(target)
        assert "control action" in str(exc.value)
    finally:
        held.release()


def _gcode_job(tmp_path, monkeypatch, printer_id="printer-b"):
    """A sliced G-code under its job dir — the target print_next.start passes to print-now."""
    job_dir = tmp_path / "job"
    gcode = job_dir / "sliced" / "orca" / "uuid-1" / "plate_1.gcode"
    gcode.parent.mkdir(parents=True)
    gcode.write_text("G1 X0\n")
    (job_dir / "JOB.json").write_text(json.dumps({"printer_id": printer_id, "gcode": str(gcode)}))
    # The ancestor climb stops at TMPDIR (a profile sets it inside itself);
    # keep it off the job's path, as on a real install.
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp-elsewhere"))
    return gcode


def test_print_now_gcode_target_takes_the_owning_jobs_lock(tmp_path, monkeypatch):
    # Codex P2 on #786: a G-code start on a secondary printer took the
    # default printer's lock, so it blocked (and was blocked by) the wrong
    # printer's control actions.
    gcode = _gcode_job(tmp_path, monkeypatch)
    lock = model_job._print_now_control_lock(str(gcode))
    assert lock is cl.control_lock("printer-b")
    assert lock is not cl.control_lock(None)


def test_print_now_job_json_target_takes_the_jobs_lock(tmp_path, monkeypatch):
    gcode = _gcode_job(tmp_path, monkeypatch)
    job_json = gcode.parents[3] / "JOB.json"
    assert model_job._print_now_control_lock(str(job_json)) is cl.control_lock("printer-b")


def test_print_now_loose_gcode_keeps_the_default_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp-elsewhere"))
    loose = tmp_path / "loose.gcode"
    loose.write_text("G1 X0\n")
    assert model_job._print_now_control_lock(str(loose)) is cl.control_lock(None)


def test_print_now_gcode_start_contends_with_its_own_printers_control_action(tmp_path, monkeypatch):
    gcode = _gcode_job(tmp_path, monkeypatch)
    held = cl.control_lock("printer-b")
    assert held.acquire(blocking=False)
    try:
        with pytest.raises(SystemExit) as exc:
            model_job.print_now(gcode)
        assert "control action" in str(exc.value)
    finally:
        held.release()


def test_print_now_gcode_start_ignores_the_default_printers_control_action(tmp_path, monkeypatch):
    gcode = _gcode_job(tmp_path, monkeypatch)
    held = cl.control_lock(None)  # the default printer's lock, held by an unrelated control action
    assert held.acquire(blocking=False)
    try:
        with pytest.raises(SystemExit) as exc:
            model_job.print_now(gcode)
        assert "control action" not in str(exc.value)
    finally:
        held.release()
