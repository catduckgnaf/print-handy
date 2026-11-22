"""A slot reloaded with nobody confirming the spool is asked about at intake (#1788).

2026-11-21: the PETG spool went into slot 3 during a runout pause. The slot
read loaded before and after, the screen still named spool #21, and the
runout question closed as a refeed of #21 with nobody saying so in chat.
The screen and the record agreed, both wrong, so the disagreement check had
nothing to catch.
"""

import asyncio
import json
from unittest import mock

from print_handy.intake import slot_verify as sv
from print_handy.runtime import moonraker_event_listener as listener

PLA_RECORD = {"material": "PLA+", "color": "Black PLA+", "spoolman_id": 21}
SLOTS = {"1": {"material": "PLA", "color": "Matte White", "spoolman_id": 16},
         "3": dict(PLA_RECORD)}
AGREEING_LANE = {3: {"slot": 3, "spool_id": 21, "material": "PLA+", "name": "", "slot_issue": None}}


def _mark(tmp_path, slot=3, spool_id=21, cause="runout"):
    path = tmp_path / "slot-verify.json"
    assert sv.mark_unconfirmed(slot, spool_id, cause, path=path)
    return path


# ------------------------------------------------------------------ the doubt rule

def test_a_marked_slot_is_in_doubt_even_when_the_screen_agrees(tmp_path):
    marks = sv.load_unconfirmed(_mark(tmp_path))
    doubts = sv.slot_doubts(SLOTS, AGREEING_LANE, unconfirmed=marks)
    assert [(d.slot, d.reason, d.reload_cause) for d in doubts] == [(3, "reload", "runout")]


def test_a_marked_slot_is_in_doubt_with_the_screen_unreadable(tmp_path):
    marks = sv.load_unconfirmed(_mark(tmp_path))
    assert [d.reason for d in sv.slot_doubts(SLOTS, None, unconfirmed=marks)] == ["reload"]


def test_the_mark_goes_stale_once_the_slot_is_bound_to_another_spool(tmp_path):
    marks = sv.load_unconfirmed(_mark(tmp_path))
    rebound = {"3": {"material": "PETG", "color": "High Speed Matte Black", "spoolman_id": 22}}
    assert sv.slot_doubts(rebound, None, unconfirmed=marks) == []


def test_no_mark_and_an_agreeing_screen_is_no_doubt():
    assert sv.slot_doubts(SLOTS, AGREEING_LANE, unconfirmed={}) == []


def test_a_confirmed_disagreement_is_asked_again_after_a_reload(tmp_path):
    lanes = {3: {"slot": 3, "spool_id": 21, "material": "PETG", "name": "", "slot_issue": None}}
    doubt = sv.slot_doubts(SLOTS, lanes)[0]
    marks = sv.load_unconfirmed(_mark(tmp_path))
    again = sv.slot_doubts(SLOTS, lanes, confirmed={"3": doubt.fingerprint}, unconfirmed=marks)
    assert [d.reason for d in again] == ["material"]


# ------------------------------------------------------------------ the question

def test_the_reload_question_names_the_record_and_offers_any_shelf_spool(tmp_path):
    doubt = sv.slot_doubts(SLOTS, None, unconfirmed=sv.load_unconfirmed(_mark(tmp_path)))[0]
    shelf = [{"spool_id": 22, "name": "High Speed Matte Black", "material": "PETG"},
             {"spool_id": 21, "name": "Black", "material": "PLA+"},
             {"spool_id": 7, "name": "Coffee", "material": "PLA+"}]
    sv.add_choices(doubt, shelf, lambda _id: "")
    labels = [c["label"] for c in doubt.choices]
    assert labels == ["#22 High Speed Matte Black PETG", "#7 Coffee PLA+", "Keep #21 Black PLA+", "Skip, print as is", "Cancel"]
    text = sv.question(doubt)
    assert "slot 3 (IFS channel 3) was reloaded after a runout" in text
    assert "nobody confirmed which spool went in" in text and "spool #21 Black PLA+" in text


# ------------------------------------------------------------------ the answer

class Recorder:
    def __init__(self, pick):
        self.pick, self.said = pick, []

    async def ask(self, text, labels):
        return self.pick(labels)

    async def send(self, text):
        self.said.append(text)


def _verify(doubt, pick, ledger, bind=None):
    rec = Recorder(pick)
    bind = bind or mock.Mock(return_value={"slot": 3, "spool_id": 22})
    with mock.patch.object(sv, "read_doubts", return_value=[doubt]), \
            mock.patch("print_handy.runtime.spool_sync.bind_slot", bind), \
            mock.patch("print_handy.runtime.spool_sync.moonraker_for", return_value="mr"), \
            mock.patch("print_handy.runtime.spool_sync.bind_text", return_value="Slot 3 -> spool #22."), \
            mock.patch("print_handy.core.printer_state.load", return_value={"filament_slots": {}}), \
            mock.patch.object(sv, "_path", return_value=ledger):
        ok = asyncio.run(sv.verify_slots({}, "flashforge-ad5x", rec.ask, rec.send))
    return ok, rec, bind


def _reload_doubt(ledger):
    doubt = sv.slot_doubts(SLOTS, None, unconfirmed=sv.load_unconfirmed(ledger))[0]
    return sv.add_choices(doubt, [{"spool_id": 22, "name": "High Speed Matte Black", "material": "PETG"}],
                          lambda _id: "")


def test_keep_ends_the_mark_without_remembering_a_fingerprint(tmp_path):
    ledger = _mark(tmp_path)
    ok, rec, bind = _verify(_reload_doubt(ledger), lambda labels: labels.index("Keep #21 Black PLA+"), ledger)
    assert ok is True
    bind.assert_not_called()
    data = json.loads(ledger.read_text())
    assert data["unconfirmed"] == {} and data["confirmed"] == {}
    assert rec.said == ["Keeping #21 Black PLA+ in slot 3."]


def test_binding_another_spool_ends_the_mark(tmp_path):
    ledger = _mark(tmp_path)
    ok, rec, bind = _verify(_reload_doubt(ledger), lambda labels: 0, ledger)
    assert ok is True
    assert bind.call_args.args == (3, 22)
    assert json.loads(ledger.read_text())["unconfirmed"] == {}


def test_clear_and_mark_never_raise_on_a_bad_slot(tmp_path):
    path = tmp_path / "slot-verify.json"
    assert sv.mark_unconfirmed("x", 21, "runout", path=path) is False
    sv.clear_unconfirmed("x", path=path)
    sv.clear_unconfirmed(3, path=path)  # nothing marked: nothing written
    assert not path.exists()


# ------------------------------------------------------------------ the listener marks

def _screen_refeed(context, ledger):
    with mock.patch("print_handy.runtime.spool_sync.resume_usage") as resume, \
            mock.patch.object(listener, "clear_runout_pause"), \
            mock.patch.object(sv, "_path", return_value=ledger):
        assert listener.feed_screen_runout_load(3, 21, context) is True
    resume.assert_called_once()


def test_a_screen_refeed_after_the_print_ended_marks_the_slot(tmp_path):
    ledger = tmp_path / "slot-verify.json"
    _screen_refeed({"cause": "runout", "prior_spool_id": 21, "print_over": True}, ledger)
    assert sv.load_unconfirmed(ledger)["3"]["cause"] == "runout"
    assert sv.load_unconfirmed(ledger)["3"]["spool_id"] == 21


def test_a_screen_refeed_during_the_paused_print_marks_nothing(tmp_path):
    # The ordinary feed-failure recovery: the same roll re-fed mid print.
    ledger = tmp_path / "slot-verify.json"
    _screen_refeed({"cause": "runout", "prior_spool_id": 21}, ledger)
    assert sv.load_unconfirmed(ledger) == {}


def test_the_insertion_answer_same_spool_ends_the_mark():
    """The Discord 'Same spool' answer clears the mark (source check: the handler is a closure)."""
    import inspect
    from print_handy.bot.handlers import common
    body = inspect.getsource(common)
    start = body.index("async def handle_insertion(")
    handler = body[start:body.index("async def handle_insertion_select(")]
    assert "slot_verify.clear_unconfirmed, slot" in handler


def test_the_listener_marks_an_idle_insertion_and_an_automatic_refeed():
    import inspect
    poll = inspect.getsource(listener.Listener.poll_native_presence)
    assert 'slot_verify.mark_unconfirmed, slot, _mapped_spool(slot), "insertion"' in poll
    settle = inspect.getsource(listener.Listener.settle_spool_question)
    assert 'slot_verify.mark_unconfirmed, slot, bound, "runout"' in settle
