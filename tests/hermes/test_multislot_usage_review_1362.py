"""Multi-slot spool charging, the 2.3.2 review findings (#1362, #1359).

The case throughout: a two-color print mapped tool 0 -> slot 2 (spool #12)
and tool 1 -> slot 3 (spool #15), `; filament used [g] = 6.80, 28.15`. The
launch cleared Moonraker's active spool; the listener charges each slot's
launch spool when the print ends (runtime/multislot_usage.py).

* Answering a spool question during the print pointed Moonraker's usage at
  one spool (resume_usage), so Moonraker charged every tool to it for the
  rest of the print and the end charge charged each launch spool again.
* A print that ended while the listener was down and is still the
  printer's job on reconnect was never charged: no live end transition ran
  and the announcer's missed-finish check saw the same job key.
* A Spoolman charge that timed out after it was sent was retried, so it
  could land twice.
* The live end charged whatever context the file had, even one left by an
  earlier launch of the same file.

Every test drives a production entry point: the chat answer
(_answer_spool_question), the screen-side load (feed_screen_runout_load),
or the listener's status transition (Listener.on_status). Nothing contacts
a printer or Spoolman.
"""
import asyncio
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy.bot.handlers import common
from print_handy.core import spoolman_client
from print_handy.runtime import klipper_report, multislot_usage, spool_sync
from print_handy.runtime import moonraker_event_listener as mel
from print_handy.runtime import print_alert_buttons as pab

FILE = "dragon-two-color.gcode"
PLANNED_MM = 11627.68


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _context(started, **extra):
    ctx = {
        "job_key": FILE, "print_number": 41, "started_at": _iso(started),
        "tool_to_physical_slot": {"0": 2, "1": 3},
        "slicer_settings": {"filament_used_g_list": [6.8, 28.15],
                            "mapped_materials": {"0": {"spoolman_id": 12, "material": "PLA"},
                                                 "1": {"spoolman_id": 15, "material": "PLA"}}},
        "planned_filament_mm": PLANNED_MM,
    }
    ctx.update(extra)
    return ctx


def _write_context(tmp_path, monkeypatch, context):
    path = tmp_path / "print_handy_context.json"
    path.write_text(json.dumps({"jobs": {FILE: context}}))
    monkeypatch.setenv("PRINT_HANDY_CONTEXT_PATH", str(path))
    monkeypatch.setenv("PRINT_HANDY_MULTISLOT_USAGE_STATE", str(tmp_path / "ledger.json"))


class FakeMoonraker:
    """The printer's print_stats and Moonraker's active-spool pointer."""

    def __init__(self, state="paused", filename=FILE, active=None):
        self.state, self.filename, self.active = state, filename, active
        self.pointer_writes = []

    def __call__(self, method, path, body=None, timeout=None):
        if path.startswith("/printer/objects/query"):
            return {"result": {"status": {"print_stats": {"state": self.state, "filename": self.filename}}}}
        if path == "/server/spoolman/spool_id":
            if method == "POST":
                self.pointer_writes.append(body)
                self.active = (body or {}).get("spool_id")
                return {"result": {"spool_id": self.active}}
            return {"result": {"spool_id": self.active}}
        raise AssertionError(f"unexpected Moonraker call {method} {path}")


def _transport(monkeypatch, moonraker):
    """Route spool_sync's Moonraker transport (every default-client call) to the fake."""
    monkeypatch.setattr(spool_sync, "_http", lambda base, method, path, body=None, timeout=8:
                        moonraker(method, path, body))


RUNOUT = {"filename": FILE, "slot": 2, "prior_spool_id": 12, "cause": "runout",
          "recorded_at": 1.0, "usage_suspended": True, "suspended_spool_id": None}


# ------------------------------------------------ 1. spool answers mid-print

def test_refeed_answer_during_a_multislot_print_leaves_usage_cleared(tmp_path, monkeypatch):
    _write_context(tmp_path, monkeypatch, _context(time.time()))
    moonraker = FakeMoonraker(state="paused")
    _transport(monkeypatch, moonraker)
    listener = S(load_runout_pause=lambda: dict(RUNOUT), clear_runout_pause=mock.Mock())
    text = common._answer_spool_question(listener, spool_sync, dict(RUNOUT), {"status": "refeed"})
    assert moonraker.pointer_writes == []  # Moonraker charges no single spool for the rest of the print
    assert moonraker.active is None
    assert "multi-slot print" in text and "Usage tracking is back on" not in text
    listener.clear_runout_pause.assert_called_once()


def test_new_spool_answer_during_a_multislot_print_leaves_usage_cleared(tmp_path, monkeypatch):
    _write_context(tmp_path, monkeypatch, _context(time.time()))
    moonraker = FakeMoonraker(state="paused")
    _transport(monkeypatch, moonraker)
    monkeypatch.setattr(spool_sync, "bind_slot", lambda *a, **kw: {"slot": 2, "spool_id": 30})
    monkeypatch.setattr(spool_sync, "bind_text", lambda result: "Spool #30 is in slot 2.")
    listener = S(load_runout_pause=lambda: dict(RUNOUT), clear_runout_pause=mock.Mock())
    text = common._answer_spool_question(listener, spool_sync, dict(RUNOUT),
                                         {"status": "existing", "spool_id": 30})
    assert moonraker.pointer_writes == []
    assert "multi-slot print" in text


def test_screen_load_during_a_multislot_print_leaves_usage_cleared(tmp_path, monkeypatch):
    _write_context(tmp_path, monkeypatch, _context(time.time()))
    moonraker = FakeMoonraker(state="paused")
    monkeypatch.setattr(mel, "clear_runout_pause", lambda: None)
    assert mel.feed_screen_runout_load(2, 12, dict(RUNOUT), mr=moonraker) is True
    assert moonraker.pointer_writes == []


def test_answer_on_a_one_slot_print_still_resumes_usage(tmp_path, monkeypatch):
    _write_context(tmp_path, monkeypatch, _context(time.time(), tool_to_physical_slot={"0": 2}))
    moonraker = FakeMoonraker(state="paused")
    _transport(monkeypatch, moonraker)
    listener = S(load_runout_pause=lambda: dict(RUNOUT), clear_runout_pause=mock.Mock())
    text = common._answer_spool_question(listener, spool_sync, dict(RUNOUT), {"status": "refeed"})
    assert moonraker.pointer_writes == [{"spool_id": 12}]
    assert "Usage tracking is back on #12" in text


# ------------------------------------------------ listener end transitions

class FakeSpoolman:
    def __init__(self):
        self.uses = []

    def __call__(self, method, path, body=None, **kw):
        if method == "PUT" and path.endswith("/use"):
            self.uses.append((int(path.split("/")[4]), body["use_weight"]))
            return {}
        return {}


def _listener(monkeypatch, history):
    monkeypatch.setattr(mel, "post", lambda message, **kw: True)
    monkeypatch.setattr(mel, "post_alert", lambda *a, **kw: True)
    monkeypatch.setattr(mel, "run_announcer", lambda reason: None)
    monkeypatch.setattr(mel, "start_claimed", lambda name: False)
    monkeypatch.setattr(mel, "log", lambda line: None)
    monkeypatch.setattr(mel, "_buttonless_standalone", lambda: None)
    monkeypatch.setattr(mel, "load_runout_pause", lambda *a, **kw: None)

    async def _no_reminder():
        return None
    monkeypatch.setattr(mel, "maintenance_cancel_reminder", _no_reminder)
    monkeypatch.setattr(klipper_report, "report", lambda filename, *a, **kw: "Klipper report stub")
    monkeypatch.setattr(klipper_report, "cancelled_by_bot", lambda: True)
    monkeypatch.setattr(pab, "retire_alerts", lambda *a, **kw: 1)

    def get_json(path, timeout=10):
        if path.startswith("/server/history"):
            return {"result": {"jobs": [history]}}
        if path == "/server/spoolman/spool_id":
            return {"result": {"spool_id": None}}
        if path.startswith("/server/files/metadata"):
            return {"result": {}}
        raise AssertionError(f"unexpected Moonraker read {path}")
    monkeypatch.setattr(mel.announcer, "get_json", get_json)
    return mel.Listener()


def _transitions(listener, states, filament_used=None):
    async def run():
        for state in states:
            stats = {"state": state, "filename": FILE}
            if filament_used is not None:
                stats["filament_used"] = filament_used
            await listener.on_status(stats, "test")
        await asyncio.gather(*listener.background_tasks)
    asyncio.run(run())


# ------------------------------------------------ 2. ended while the listener was down

def test_print_that_ended_while_the_listener_was_down_is_charged_on_reconnect(tmp_path, monkeypatch):
    ended = time.time() - 20 * 60
    started = ended - 3600
    _write_context(tmp_path, monkeypatch, _context(started - 120))
    history = {"job_id": "0001A", "filename": FILE, "status": "completed", "start_time": started,
               "end_time": ended, "filament_used": PLANNED_MM}
    fake = FakeSpoolman()
    monkeypatch.setattr(spoolman_client, "request", fake)
    # The listener comes back to the printer still showing the finished job.
    _transitions(_listener(monkeypatch, history), ["complete"])
    assert fake.uses == [(12, 6.8), (15, 28.15)]
    # Another reconnect, or the announcer's own missed-finish check, charges nothing more.
    _transitions(_listener(monkeypatch, history), ["complete"])
    assert multislot_usage.charge_history_job(history, _context(started - 120), suspended=False) is not None
    assert len(fake.uses) == 2
    line = multislot_usage.summary_line(multislot_usage.entry_for(_context(started - 120)))
    assert line == "Spool usage: slot 2 spool #12 6.8 g; slot 3 spool #15 28.15 g"


def test_cancelled_while_the_listener_was_down_is_scaled_on_reconnect(tmp_path, monkeypatch):
    ended = time.time() - 20 * 60
    started = ended - 3600
    _write_context(tmp_path, monkeypatch, _context(started - 120))
    history = {"job_id": "0001B", "filename": FILE, "status": "cancelled", "start_time": started,
               "end_time": ended, "filament_used": PLANNED_MM / 2}
    fake = FakeSpoolman()
    monkeypatch.setattr(spoolman_client, "request", fake)
    _transitions(_listener(monkeypatch, history), ["cancelled"])
    assert [g for _, g in fake.uses] == [3.4, pytest.approx(14.07, abs=0.01)]


# ------------------------------------------------ 3. timeout after send

def test_a_charge_that_timed_out_after_sending_is_not_retried(tmp_path, monkeypatch):
    started = time.time() - 3600
    _write_context(tmp_path, monkeypatch, _context(started))
    history = {"job_id": "0001C", "filename": FILE, "status": "in_progress", "start_time": started + 60}
    sent = []

    def urlopen(req, timeout=None):
        if "spoolman.invalid" not in getattr(req, "full_url", str(req)):
            raise OSError("no network in tests")
        # The request reached Spoolman; its reply did not come back in time.
        sent.append((req.get_method(), req.full_url))
        raise TimeoutError("timed out")
    monkeypatch.setattr(spoolman_client, "_resolve_base", lambda base_url: "http://spoolman.invalid")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    _transitions(_listener(monkeypatch, history), ["standby", "printing", "complete"])
    assert len(sent) == 2
    entry = multislot_usage.entry_for(_context(started))
    assert [slot["status"] for slot in entry["slots"]] == ["unconfirmed", "unconfirmed"]
    # The listener's retry pass (run whenever Spoolman answers again) leaves them alone.
    assert multislot_usage.retry_pending() == []
    assert len(sent) == 2
    line = multislot_usage.summary_line(multislot_usage.entry_for(_context(started)))
    assert "slot 2 spool #12 6.8 g (charge unconfirmed" in line
    assert "not retried" in line


def test_a_charge_spoolman_never_received_is_still_retried(tmp_path, monkeypatch):
    started = time.time() - 3600
    _write_context(tmp_path, monkeypatch, _context(started))
    history = {"job_id": "0001D", "filename": FILE, "status": "in_progress", "start_time": started + 60}

    def refused(req, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
    monkeypatch.setattr(spoolman_client, "_resolve_base", lambda base_url: "http://spoolman.invalid")
    monkeypatch.setattr(urllib.request, "urlopen", refused)
    _transitions(_listener(monkeypatch, history), ["standby", "printing", "complete"])
    entry = multislot_usage.entry_for(_context(started))
    assert [slot["status"] for slot in entry["slots"]] == ["pending", "pending"]
    up = FakeSpoolman()
    assert multislot_usage.retry_pending(request=up) == [f"{FILE}#41"]
    assert up.uses == [(12, 6.8), (15, 28.15)]


# ------------------------------------------------ 4. the context must be this print's

def test_live_end_never_charges_an_earlier_launchs_context(tmp_path, monkeypatch):
    # The file was launched by Print Handy two days ago; this run was started
    # at the printer, so the context on file is the earlier launch's.
    now = time.time()
    _write_context(tmp_path, monkeypatch, _context(now - 2 * 86400, print_number=None))
    history = {"job_id": "0001E", "filename": FILE, "status": "in_progress", "start_time": now - 3600}
    fake = FakeSpoolman()
    monkeypatch.setattr(spoolman_client, "request", fake)
    _transitions(_listener(monkeypatch, history), ["standby", "printing", "complete"])
    assert fake.uses == []


@pytest.mark.parametrize("context_age_s, charged", [(30, True), (2 * 86400, False)])
def test_without_moonraker_history_the_start_seen_live_is_the_window(context_age_s, charged, tmp_path,
                                                                     monkeypatch):
    # A Moonraker without [history]: the listener's own view of the start decides.
    _write_context(tmp_path, monkeypatch, _context(time.time() - context_age_s))
    fake = FakeSpoolman()
    monkeypatch.setattr(spoolman_client, "request", fake)
    listener = _listener(monkeypatch, {})

    def no_history(path, timeout=10):
        raise OSError("HTTP 404: history component not loaded")
    monkeypatch.setattr(mel.announcer, "get_json", no_history)
    _transitions(listener, ["standby", "printing", "complete"])
    assert fake.uses == ([(12, 6.8), (15, 28.15)] if charged else [])


def test_live_end_charges_this_prints_context(tmp_path, monkeypatch):
    now = time.time()
    _write_context(tmp_path, monkeypatch, _context(now - 3600))
    history = {"job_id": "0001F", "filename": FILE, "status": "completed", "start_time": now - 3540,
               "end_time": now}
    fake = FakeSpoolman()
    monkeypatch.setattr(spoolman_client, "request", fake)
    _transitions(_listener(monkeypatch, history), ["standby", "printing", "complete"])
    assert fake.uses == [(12, 6.8), (15, 28.15)]
