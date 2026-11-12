"""Resume and Stop when Moonraker holds its reply, and Stop while a start is in flight.

2026-11-08: a Resume tap reported "Resume didn't work" because Moonraker's reply to
/printer/print/resume timed out (the AD5X RESUME macro outlasts the 15 s timeout); the
printer went paused -> printing 10 s later. 2026-11-05: Stop reported "Stop didn't work"
the same way, then the printer reported the print cancelled. The read-back decides.

2026-11-05: Stop 24 s after "Print started" said "Nothing to stop: printer is standby"
while the start was still on its way; the print began 40 s later. A start in flight is
something to stop, not nothing.
"""
import asyncio
import importlib.util
import json
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy import settings
from print_handy.core import launch_state
from print_handy.runtime import print_control, print_stop, start_claims

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("pf_stop_inflight", HERE.parents[1] / "plugins/print-feedback/__init__.py")
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)

FILE = "Top-Mount_plate_1_" + "ab" * 32 + ".gcode.3mf"


def pc_st(state="paused", filename="foo.gcode"):
    return {"state": state, "klippy": "ready", "filename": filename, "layer": 10, "total_layer": 50, "progress": 0.2}


def ps_st(state, filename="a.gcode"):
    return {"state": state, "filename": filename, "layer": None, "total_layer": None,
            "progress": None, "print_duration": None}


def reply_timeout(path, method="GET", timeout=10, **kw):
    """Moonraker accepted the POST but holds its reply past the socket timeout."""
    if method == "POST":
        raise TimeoutError("timed out")
    return {"result": {"status": {"extruder": {"target": 0.0}, "heater_bed": {"target": 0.0}}}}


@pytest.fixture(autouse=True)
def _no_claims(tmp_path, monkeypatch):
    monkeypatch.setattr(start_claims, "START_CLAIMS", tmp_path / "start_claims.json")


# --- A: a reply that times out after the request went out is not a failure ---------------------

def test_resume_reply_timeout_reads_back_and_reports_resumed():
    st = mock.MagicMock(side_effect=[pc_st(), pc_st(), pc_st(state="printing")])
    with mock.patch.object(print_control, "_call", side_effect=reply_timeout) as call, \
            mock.patch.object(print_control, "status", st), mock.patch.object(print_control.time, "sleep"):
        ok, msg = print_control.resume("foo.gcode", wait_s=5, poll_s=0.01)
    assert ok is True and msg.startswith("Resumed. Printer now reports printing")
    posts = [c for c in call.call_args_list if c.kwargs.get("method") == "POST"]
    assert len(posts) == 1  # one resume, never retried


def test_resume_reply_timeout_waits_the_longer_window_before_saying_it_did_not_take():
    clock = iter(range(0, 10_000))
    with mock.patch.object(print_control, "_call", side_effect=reply_timeout), \
            mock.patch.object(print_control, "status", return_value=pc_st()), \
            mock.patch.object(print_control.time, "sleep"), \
            mock.patch.object(print_control.time, "monotonic", side_effect=lambda: float(next(clock))):
        ok, msg = print_control.resume("foo.gcode", wait_s=5, poll_s=1)
    assert ok is False
    assert "has not answered" in msg and f"after {print_stop.NO_REPLY_WAIT_S:.0f}s" in msg
    assert "didn't work" not in msg


def test_resume_connection_failure_still_raises():
    """Nothing reached the printer: that stays an error, never a read-back."""
    def refused(path, method="GET", timeout=10, **kw):
        raise urllib.error.URLError(ConnectionRefusedError("refused"))
    with mock.patch.object(print_control, "_call", side_effect=refused), \
            mock.patch.object(print_control, "status", return_value=pc_st()):
        with pytest.raises(urllib.error.URLError):
            print_control.resume("foo.gcode", wait_s=1, poll_s=0.01)


def test_cancel_reply_timeout_reads_back_and_reports_stopped():
    seq = [ps_st("printing"), ps_st("printing"), ps_st("cancelled")]
    with mock.patch.object(print_stop, "status", side_effect=seq), \
            mock.patch.object(print_stop, "_call", side_effect=reply_timeout) as call, \
            mock.patch.object(print_stop.time, "sleep"):
        sent, text = print_stop.cancel("a.gcode", wait_s=5)
    assert sent and text == "Stopped. Printer now reports cancelled."
    assert [c for c in call.call_args_list if c.kwargs.get("method") == "POST"] == [
        mock.call("/printer/print/cancel", method="POST", timeout=15)]


def test_cancel_reply_timeout_waits_the_longer_window():
    clock = iter(range(0, 10_000))
    with mock.patch.object(print_stop, "status", return_value=ps_st("printing")), \
            mock.patch.object(print_stop, "_call", side_effect=reply_timeout), \
            mock.patch.object(print_stop.time, "sleep"), \
            mock.patch.object(print_stop.time, "monotonic", side_effect=lambda: float(next(clock))):
        sent, text = print_stop.cancel("a.gcode", wait_s=5)
    assert sent and "has not answered" in text and f"after {print_stop.NO_REPLY_WAIT_S:.0f}s" in text


# --- B: a start in flight is something to stop ---------------------------------------------------

def _uncertain_lease():
    lease = launch_state.acquire(settings.default_printer_id(), job_json="/archive/a/JOB.json",
                                 gcode_sha256="a" * 64, remote=FILE)
    lease.submitting()
    lease.uncertain("accepted, still preparing")
    return lease


def test_start_in_flight_sees_a_held_launch_lease():
    assert print_stop.start_in_flight(None, ps_st("standby", "")) is None
    _uncertain_lease()
    assert print_stop.start_in_flight(None, ps_st("standby", "")) == {"file": FILE}
    # Printing already: that is the ordinary stop, not a start in flight.
    assert print_stop.start_in_flight(None, ps_st("printing", FILE)) is None


def test_old_uncertain_lease_is_not_a_start_in_flight():
    _uncertain_lease()
    later = time.time() + launch_state.IN_FLIGHT_UNCERTAIN_S + 60
    assert print_stop.start_in_flight(None, ps_st("standby", ""), now=later) is None


def test_start_in_flight_sees_a_recent_start_claim_only_while_standby(tmp_path):
    start_claims.START_CLAIMS.write_text(json.dumps({FILE: time.time() - 30}))
    assert print_stop.start_in_flight(None, ps_st("standby", "")) == {"file": FILE}
    assert print_stop.start_in_flight(None, ps_st("cancelled", "3mf_print/" + FILE[:-4])) is None
    start_claims.START_CLAIMS.write_text(json.dumps({FILE: time.time() - print_stop.START_IN_FLIGHT_S - 60}))
    assert print_stop.start_in_flight(None, ps_st("standby", "")) is None


def test_cancel_names_the_start_in_flight_instead_of_nothing_to_stop():
    _uncertain_lease()
    with mock.patch.object(print_stop, "status", return_value=ps_st("standby", "")), \
            mock.patch.object(print_stop, "_call") as call:
        sent, text = print_stop.cancel("")
    assert not sent and "Nothing to stop" not in text
    assert "is starting" in text and print_stop.STOP_START_LABEL in text and "Nothing sent" in text
    call.assert_not_called()


def test_stop_pending_start_waits_for_the_print_then_cancels_it_once():
    printing = ps_st("printing", "3mf_print/" + FILE[:-4])
    seq = [ps_st("standby", ""), ps_st("standby", ""), printing]
    _uncertain_lease()
    with mock.patch.object(print_stop, "status", side_effect=seq), \
            mock.patch.object(print_stop, "cancel", return_value=(True, "Stopped. Printer now reports cancelled.")) as cancel, \
            mock.patch.object(print_stop.time, "sleep"):
        sent, text = print_stop.stop_pending_start(FILE, wait_s=60, poll_s=1)
    cancel.assert_called_once_with(printing["filename"])
    assert sent and text.endswith("started, so it was stopped right away. Stopped. Printer now reports cancelled.")


def test_stop_pending_start_refuses_a_different_print():
    _uncertain_lease()
    with mock.patch.object(print_stop, "status", return_value=ps_st("printing", "other.gcode")), \
            mock.patch.object(print_stop, "cancel") as cancel:
        sent, text = print_stop.stop_pending_start(FILE, wait_s=60, poll_s=1)
    assert not sent and "different print" in text and "Nothing sent" in text
    cancel.assert_not_called()


def test_stop_pending_start_says_so_when_the_start_goes_away():
    with mock.patch.object(print_stop, "status", return_value=ps_st("standby", "")), \
            mock.patch.object(print_stop, "cancel") as cancel:
        sent, text = print_stop.stop_pending_start(FILE, wait_s=60, poll_s=1)
    assert not sent and "did not start" in text and "Nothing sent" in text
    cancel.assert_not_called()


class FakeAdapter:
    def __init__(self):
        self.sent, self.questions = [], []

    async def send(self, chat_id=None, content=None, metadata=None):
        self.sent.append(content)

    async def send_clarify(self, **kw):
        self.questions.append(kw)
        return S(success=True)


def _event():
    src = S(platform=S(value="discord"), profile="3dprinting", is_bot=False, profile_route_rejected=False,
            user_id="u", chat_id="c", thread_id=None)
    return S(source=src, text="/print-stop", internal=False)


def _gateway(adapter):
    return S(_is_user_authorized_for_source=lambda s: True, _delivery_adapter_for=lambda s: adapter,
             _session_key_for_source=lambda s: "session-key")


def test_stop_command_during_a_start_offers_to_stop_it_when_it_starts():
    ad = FakeAdapter()
    _uncertain_lease()
    from print_handy.core import prompts as cm
    with mock.patch.object(print_stop, "status", return_value=ps_st("standby", "")), \
            mock.patch.object(print_stop, "stop_pending_start",
                              return_value=(True, "Top Mount started, so it was stopped right away.")) as pending, \
            mock.patch.object(cm, "wait_for_response", return_value=print_stop.STOP_START_LABEL):
        asyncio.run(plugin.print_stop_dispatch(_event(), _gateway(ad)))
    question = ad.questions[-1]
    assert "is starting" in question["question"] and "Nothing to stop" not in question["question"]
    assert question["choices"] == [print_stop.STOP_START_LABEL, print_stop.LET_START_LABEL]
    pending.assert_called_once_with(FILE)
    assert not any("Nothing to stop" in m for m in ad.sent)
    assert ad.sent[-1] == "Top Mount started, so it was stopped right away."


def test_stop_command_during_a_start_let_it_print_sends_nothing():
    ad = FakeAdapter()
    _uncertain_lease()
    from print_handy.core import prompts as cm
    with mock.patch.object(print_stop, "status", return_value=ps_st("standby", "")), \
            mock.patch.object(print_stop, "cancel") as cancel, \
            mock.patch.object(cm, "wait_for_response", return_value=print_stop.LET_START_LABEL):
        asyncio.run(plugin.print_stop_dispatch(_event(), _gateway(ad)))
    cancel.assert_not_called()
    assert ad.sent[-1] == "Letting it print. Nothing sent."


# --- C: the estimate does not read like a long warmup ---------------------------------------------

def test_start_estimate_says_total_including_warmup():
    text = print_control.start_estimate_text({"copies": 1}, 9000, 50.0)
    assert "plus warmup" not in text
    assert text.endswith("total including warmup")
