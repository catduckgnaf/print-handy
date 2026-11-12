"""P1-33 / SAFE-6: a control action in ANOTHER process blocks purge and slot load.

The standalone CLIs (ph-purge, ph-load-slot) and the bot are separate
interpreters; the old per-process threading.Lock let them overlap. Here a
child process holds the shared file lock for the configured printer while
this process asks for a purge and a slot load — both must refuse.
"""
import subprocess
import sys
import time
from pathlib import Path

import pytest

from print_handy import settings
from print_handy.runtime import _config, panic, slot_load

REPO = Path(__file__).resolve().parents[2]

CHILD = """
import sys, time
sys.path.insert(0, {repo!r})
from print_handy.core.control_lock import control_lock
lock = control_lock(sys.argv[2], state_dir=sys.argv[1])
if not lock.acquire(blocking=False):
    raise SystemExit(3)
__import__("pathlib").Path(sys.argv[3]).write_text("held")
time.sleep(30)
"""

IDLE = {
    "print_stats": {"state": "standby"},
    "webhooks": {"state": "ready"},
    "filament_switch_sensor head_switch_sensor": {"filament_detected": True},
    "save_variables": {"variables": {"display_off": 1}},
    "extruder": {"target": 0},
}


@pytest.fixture()
def other_process_holds_lock(tmp_path):
    ready = tmp_path / "ready"
    key = settings.default_printer_id(_config.cfg())
    proc = subprocess.Popen(
        [sys.executable, "-c", CHILD.format(repo=str(REPO)), str(_config.state_dir()), key, str(ready)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.monotonic() + 20
    while not ready.is_file():
        if proc.poll() is not None:
            raise AssertionError(f"holder child exited early: {proc.stdout.read()}")
        if time.monotonic() > deadline:
            proc.kill()
            raise AssertionError("holder child never acquired the lock")
        time.sleep(0.05)
    yield proc
    proc.kill()
    proc.wait(timeout=10)


def test_purge_refuses_while_another_process_holds_the_lock(other_process_holds_lock):
    res = panic.purge(reader=lambda: IDLE, caller=lambda *a, **k: {"result": {"status": IDLE}},
                      gcode_sender=lambda script: None)
    assert res["ok"] is False
    assert "in flight" in res["error"]


def test_slot_load_refuses_while_another_process_holds_the_lock(other_process_holds_lock):
    res = slot_load.load_slot(1, reader=lambda: IDLE, gcode_sender=lambda script: None)
    assert res["ok"] is False
    assert "in flight" in res["error"]
