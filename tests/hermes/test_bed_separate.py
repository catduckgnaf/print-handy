"""Prepare to clean (/print-bed-down): bed and nozzle separation.

Fakes only: every test injects the sender/reader or patches the plugin's
printer calls, so no printer, network or Discord is ever contacted.
"""
import asyncio
import importlib.util
import threading
import time
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy import knowledge
from print_handy.bot import runtime
from print_handy.hermes import bed_separate as bs
from print_handy.hermes import print_control as pc

SPEC = importlib.util.spec_from_file_location(
    "print_feedback_bed_down", Path(__file__).parents[2] / "plugins/print-feedback/__init__.py")
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


def _status(job="standby", homed="xyz", z=5.0, z_max=230.0, webhooks="ready"):
    return {"webhooks": {"state": webhooks}, "print_stats": {"state": job},
            "toolhead": {"homed_axes": homed, "position": [10.0, 10.0, z, 0.0],
                         "axis_maximum": [225.0, 232.0, z_max, 0.0]}}


class FakePrinter:
    """Records every G-code script; the readback follows the relative move."""

    def __init__(self, status):
        self.status = status
        self.sent = []

    def send(self, script):
        self.sent.append(script)
        lines = script.splitlines()
        if "G91" in lines:
            for line in lines:
                if line.startswith("G1 Z"):
                    dz = float(line.split()[1][1:])
                    self.status["toolhead"]["position"][2] += dz

    def read(self):
        return self.status


@pytest.fixture(autouse=True)
def _local_lock(monkeypatch):
    lock = threading.Lock()
    monkeypatch.setattr(bs, "_lock_for", lambda printer_id=None: lock)
    return lock


def _separate(printer, **kw):
    kw.setdefault("confirmed_at", time.time())
    kw.setdefault("distance_mm", 20.0)
    kw.setdefault("connection", "moonraker")
    return bs.separate(gcode_sender=printer.send, reader=printer.read, **kw)


# ------------------------------------------------------------- the move itself

def test_move_is_relative_bounded_slow_and_never_homes_or_heats():
    printer = FakePrinter(_status(z=5.0))
    ok, message = _separate(printer)
    assert ok, message
    assert printer.sent == ["G91\nG1 Z20 F300\nG90\nM400"]
    script = printer.sent[0].upper()
    for forbidden in ("G28", "SET_KINEMATIC_POSITION", "FORCE_MOVE", "M104", "M109", "M140", "M190",
                      "SET_HEATER_TEMPERATURE"):
        assert forbidden not in script
    assert "No homing, no heat" in message and "25 mm" in message


def test_default_distance_is_20_mm_and_config_is_clamped():
    assert bs.separate_mm({}) == 20.0
    assert bs.separate_mm({"maintenance": {"bed_separate_mm": 12}}) == 12.0
    assert bs.separate_mm({"maintenance": {"bed_separate_mm": 500}}) == bs.MAX_SEPARATE_MM
    assert bs.separate_mm({"maintenance": {"bed_separate_mm": -3}}) == bs.MIN_SEPARATE_MM
    assert bs.separate_mm({"maintenance": {"bed_separate_mm": "junk"}}) == 20.0


@pytest.mark.parametrize("distance", [0.0, 0.5, 51.0, 1000.0])
def test_out_of_range_distance_sends_nothing(distance):
    printer = FakePrinter(_status())
    ok, message = _separate(printer, distance_mm=distance)
    assert not ok and printer.sent == [] and "Nothing sent" in message


@pytest.mark.parametrize("status, reason", [
    (_status(homed=""), "not homed"),           # the state right after a firmware restart
    (_status(homed="xy"), "not homed"),
    (_status(z=215.0, z_max=230.0), "allowed travel"),   # would pass the Z maximum
    (_status(job="printing"), "printing"),
    (_status(job="paused"), "paused"),
    (_status(webhooks="shutdown"), "shutdown"),
    ({"webhooks": {"state": "ready"}, "print_stats": {"state": "standby"},
      "toolhead": {"homed_axes": "xyz"}}, "did not report"),
])
def test_unsafe_states_send_nothing_and_give_instructions(status, reason):
    printer = FakePrinter(status)
    ok, message = _separate(printer)
    assert not ok and printer.sent == []
    assert reason in message
    assert "Nothing was sent" in message and "Do not home Z" in message


def test_stale_confirmation_sends_nothing():
    printer = FakePrinter(_status())
    ok, message = _separate(printer, confirmed_at=time.time() - bs.CONFIRM_WINDOW_S - 5)
    assert not ok and printer.sent == [] and "expired" in message


def test_busy_control_lock_sends_nothing(_local_lock):
    printer = FakePrinter(_status())
    _local_lock.acquire()
    try:
        ok, message = _separate(printer)
    finally:
        _local_lock.release()
    assert not ok and printer.sent == [] and "in flight" in message


def test_failed_state_read_sends_nothing():
    sent = []

    def broken():
        raise TimeoutError("no answer")

    ok, message = bs.separate(confirmed_at=time.time(), distance_mm=20, connection="moonraker",
                              gcode_sender=sent.append, reader=broken)
    assert not ok and sent == [] and "TimeoutError" in message


def test_readback_mismatch_is_reported_not_claimed_as_done():
    printer = FakePrinter(_status())
    printer.send = lambda script: printer.sent.append(script)   # the head never moves
    ok, message = _separate(printer)
    assert not ok and len(printer.sent) == 1 and "readback" in message


# --------------------------------------------------------- per-family availability

@pytest.mark.parametrize("connection, live", [
    ("moonraker", True), ("Moonraker", True), ("octoprint", False), ("prusalink", False),
    ("bambu", False), ("elegoo", False), ("", False), (None, False),
])
def test_only_moonraker_gets_a_live_move(connection, live):
    assert bs.supports_live_move(connection) is live
    printer = FakePrinter(_status())
    ok, message = bs.separate(confirmed_at=time.time(), distance_mm=20, connection=connection or "none",
                              gcode_sender=printer.send, reader=printer.read)
    if live:
        assert ok and printer.sent
    else:
        assert not ok and printer.sent == [] and "Nothing was sent" in message


# ------------------------------------------------------- direction from the profile

@pytest.mark.parametrize("key, motion", [
    ("flashforge.ad5x", "bed_down"),
    ("flashforge_stock.ad5x", "bed_down"),
    ("sovol.sv06", "head_up"),
    ("sovol.sv06_ace", "head_up"),
    ("sovol.sv08", "head_up"),
])
def test_direction_follows_the_printer_registry(key, motion):
    entry = next(p for p in knowledge.default_registry().printers if p.key == key)
    assert bs.motion_for(entry.kinematics, entry.key) == motion


def test_unknown_layout_gets_neutral_wording_and_question_names_the_motion():
    assert bs.motion_for(None) == "apart" and bs.motion_for("delta") == "apart"
    question = bs.confirm_question(20, "bed_down")
    assert "moves the bed down about 20 mm" in question and "no homing and no heat" in question
    assert "Is anything else in the way?" in question
    assert "raises the print head about 20 mm" in bs.confirm_question(20, "head_up")


# --------------------------------------------------------- plain-language requests

@pytest.mark.parametrize("text", [
    "reset the bed", "Reset the printer bed", "lower the bed", "drop the bed", "move the bed down",
    "bed down", "separate the nozzle", "separate the bed and nozzle", "get the nozzle off the part",
    "prepare to clean", "get ready to clean", "clean the nozzle", "please lower the bed",
    # typos go through core/fuzzy.py
    "rest the printer bed", "lowr the bed", "seperate the nozzle", "prepare to cleen", "bed dwn",
])
def test_phrasings_resolve_to_the_bed_down_suggestion(text):
    assert runtime.command_suggestion(text) == "/print-bed-down"


@pytest.mark.parametrize("text", [
    "heat the bed", "level the bed", "reset the printer", "lower the bed temperature",
    "the bed is not level", "change the nozzle",
])
def test_neighbours_do_not_resolve_to_bed_down(text):
    assert runtime.command_suggestion(text) != "/print-bed-down"


# ------------------------------------------------------------------ lookup ranking

@pytest.mark.parametrize("query", ["bed reset", "reset the printer bed", "lower the bed", "prepare to clean"])
def test_lookup_ranks_prepare_to_clean_above_heater_gcode(query):
    results = knowledge.load_registry().lookup(query)
    assert results and results[0].kind == "flow" and results[0].item.id == "prepare-to-clean"
    text = knowledge.lookup_text(query)
    assert text.index("prepare-to-clean") < (text.index("M140") if "M140" in text else len(text))


# ------------------------------------------------------------------ the dispatch

class Adapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id=None, content=None, metadata=None):
        self.sent.append(content)


def _event(text="/print-bed-down"):
    source = S(platform=S(value="discord"), profile="3dprinting", is_bot=False,
               profile_route_rejected=False, user_id="u", chat_id="c", thread_id=None)
    return S(source=source, text=text, internal=False)


def _gateway(adapter):
    return S(_is_user_authorized_for_source=lambda s: True, _delivery_adapter_for=lambda s: adapter,
             _session_key_for_source=lambda s: "session-key")


def _dispatch(state, answer):
    adapter = Adapter()
    confirm = mock.AsyncMock(return_value=answer)
    separate = mock.Mock(return_value=(True, "Opened the gap by 20 mm."))
    with mock.patch.object(plugin, "_routed_printer_id", return_value="ad5x"), \
            mock.patch.object(pc, "status", return_value={"state": state}), \
            mock.patch.object(plugin, "_confirm", confirm), \
            mock.patch.object(bs, "printer_motion", return_value="bed_down"), \
            mock.patch.object(bs, "separate", separate):
        out = asyncio.run(plugin.bed_down_dispatch(_event(), _gateway(adapter)))
    return out, adapter, confirm, separate


@pytest.mark.parametrize("state", ["printing", "paused"])
def test_dispatch_refuses_during_a_print_without_asking(state):
    out, adapter, confirm, separate = _dispatch(state, plugin.BED_DOWN_LABEL)
    assert out == {"action": "skip", "reason": "deterministic bed down handled"}
    confirm.assert_not_called()
    separate.assert_not_called()
    assert "Refused" in adapter.sent[-1] and "Nothing sent" in adapter.sent[-1]


def test_dispatch_asks_first_then_moves_once():
    out, adapter, confirm, separate = _dispatch("standby", plugin.BED_DOWN_LABEL)
    question = confirm.call_args.args[4]
    assert "moves the bed down" in question and "no homing and no heat" in question
    separate.assert_called_once()
    assert separate.call_args.kwargs["printer_id"] == "ad5x"
    assert adapter.sent[-1] == "Opened the gap by 20 mm."


def test_dispatch_cancel_sends_nothing():
    _, adapter, _, separate = _dispatch("standby", plugin.CANCEL_BED_DOWN_LABEL)
    separate.assert_not_called()
    assert adapter.sent[-1] == "Not moved. Nothing sent."


def test_dispatch_ignores_other_text():
    assert asyncio.run(plugin.bed_down_dispatch(_event("/print-bed-downx"), _gateway(Adapter()))) is None
    assert asyncio.run(plugin.bed_down_dispatch(_event("lower the bed"), _gateway(Adapter()))) is None
