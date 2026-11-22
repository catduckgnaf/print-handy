"""Verify a slot before slicing when the printer screen and Print Handy disagree (#1788).

The 2026-11-21 case: PETG loaded in slot 3, the screen set to PETG, Print
Handy's record still spool #21 Black PLA+. Nothing asked, so a print in that
window would have sliced PLA+ settings with PETG in the nozzle.
"""

import asyncio
import json
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy.core import prepared_intake
from print_handy.intake import slot_verify as sv

PLA_RECORD = {"material": "PLA+", "color": "Black PLA+", "spoolman_id": 21}
SLOTS = {"1": {"material": "PLA", "color": "Matte White", "spoolman_id": 16},
         "3": dict(PLA_RECORD)}


def lane(spool_id, material, **extra):
    return {"slot": extra.pop("slot", 3), "spool_id": spool_id, "material": material,
            "color": "161616", "name": extra.pop("name", ""), "slot_issue": None, **extra}


# ------------------------------------------------------------------ the doubt rule

def test_the_2026_11_21_case_is_a_material_doubt():
    doubts = sv.slot_doubts(SLOTS, {3: lane(21, "PETG")})
    assert [(d.slot, d.reason) for d in doubts] == [(3, "material")]
    assert doubts[0].record_spool_id == 21 and doubts[0].screen_material == "PETG"


def test_a_different_spool_on_the_screen_is_a_spool_doubt():
    doubts = sv.slot_doubts(SLOTS, {3: lane(22, "PETG")})
    assert [(d.slot, d.reason, d.screen_spool_id) for d in doubts] == [(3, "spool", 22)]


@pytest.mark.parametrize("screen", ["PLA", "PLA+", "Silk PLA", "Matte PLA"])
def test_the_same_family_is_no_doubt(screen):
    # PLA+, Matte and Silk are PLA (lane_matches_spool_material, shared with the lane watcher).
    assert sv.slot_doubts(SLOTS, {3: lane(21, screen)}) == []


def test_no_screen_information_is_no_doubt():
    assert sv.slot_doubts(SLOTS, None) == []
    assert sv.slot_doubts(SLOTS, {}) == []
    assert sv.slot_doubts(SLOTS, {3: lane(None, "")}) == []
    assert sv.slot_doubts(SLOTS, {3: lane(22, "PETG", slot_issue="conflict")}) == []  # the lane disputes itself


def test_a_confirmed_disagreement_is_not_asked_again_until_something_changes():
    doubt = sv.slot_doubts(SLOTS, {3: lane(21, "PETG")})[0]
    confirmed = {"3": doubt.fingerprint}
    assert sv.slot_doubts(SLOTS, {3: lane(21, "PETG")}, confirmed=confirmed) == []
    # The screen changes (another spool), so it is asked again.
    assert sv.slot_doubts(SLOTS, {3: lane(22, "PETG")}, confirmed=confirmed)


# ------------------------------------------------------------------ the question

def test_choices_offer_the_screen_spool_matching_shelf_spools_keep_and_cancel():
    doubt = sv.slot_doubts(SLOTS, {3: lane(22, "PETG")})[0]
    shelf = [{"spool_id": 4, "name": "Gray", "material": "PETG"},
             {"spool_id": 7, "name": "Coffee", "material": "PLA+"},
             {"spool_id": 22, "name": "High Speed Matte Black", "material": "PETG"}]
    sv.add_choices(doubt, shelf, lambda spool_id: "High Speed Matte Black PETG")
    labels = [c["label"] for c in doubt.choices]
    assert labels[0] == "#22 High Speed Matte Black PETG (screen)"
    assert "#4 Gray PETG" in labels
    assert not any("Coffee" in label for label in labels)  # PLA+ is not the screen's material
    assert labels.count("#22 High Speed Matte Black PETG (screen)") == 1  # no duplicate of the screen's spool
    assert labels[-2:] == ["Keep #21 Black PLA+", "Cancel"]
    text = sv.question(doubt)
    assert "slot 3 (IFS channel 3)" in text and "spool #22 (PETG)" in text
    assert "spool #21 Black PLA+" in text and "Nothing is sliced until you answer" in text


# ------------------------------------------------------------------ the answer

class Recorder:
    def __init__(self, pick):
        self.pick, self.asked, self.said = pick, [], []

    async def ask(self, text, labels):
        self.asked.append((text, labels))
        return self.pick(labels) if callable(self.pick) else self.pick

    async def send(self, text):
        self.said.append(text)


def _run(doubts, recorder, *, state=None, bind=None, loaded=None, ledger=None):
    state = {} if state is None else state
    bind = bind or mock.Mock(return_value={"slot": 3, "spool_id": 22, "prior_spool_id": 21,
                                           "record": {"slot": 3, "material": "PETG", "remaining_grams": 1000.0,
                                                      "name": "High Speed Matte Black PETG"},
                                           "screen": "pushed", "usage": "now charged to #22",
                                           "prior_disposition": "shelf"})
    with mock.patch.object(sv, "read_doubts", return_value=doubts), \
            mock.patch("print_handy.runtime.spool_sync.bind_slot", bind), \
            mock.patch("print_handy.runtime.spool_sync.moonraker_for", return_value="mr"), \
            mock.patch("print_handy.runtime.spool_sync.bind_text", return_value="Slot 3 -> spool #22."), \
            mock.patch("print_handy.core.printer_state.load", return_value=loaded or {"filament_slots": {}}), \
            mock.patch.object(sv, "_path", return_value=ledger):
        ok = asyncio.run(sv.verify_slots(state, "flashforge-ad5x", recorder.ask, recorder.send))
    return ok, bind, state


def _doubt():
    doubt = sv.slot_doubts(SLOTS, {3: lane(22, "PETG")})[0]
    return sv.add_choices(doubt, [], lambda _id: "High Speed Matte Black PETG")


def test_picking_the_screen_spool_binds_it_and_reloads_state(tmp_path):
    state = {"filament_slots": {"3": dict(PLA_RECORD)}}
    fresh = {"filament_slots": {"3": {"material": "PETG", "spoolman_id": 22}}}
    rec = Recorder(0)
    ok, bind, state = _run([_doubt()], rec, state=state, loaded=fresh, ledger=tmp_path / "slot-verify.json")
    assert ok is True
    bind.assert_called_once()
    args, kwargs = bind.call_args
    assert args == (3, 22)
    assert kwargs["expected_prior_spool_id"] == 21 and kwargs["prior_disposition"] == "shelf"
    assert state == fresh  # the caller's colors and slicing now see the confirmed spool
    assert rec.said == ["Slot 3 -> spool #22."]


def test_keep_changes_nothing_and_is_remembered(tmp_path):
    ledger = tmp_path / "slot-verify.json"
    rec = Recorder(lambda labels: labels.index("Keep #21 Black PLA+"))
    ok, bind, _ = _run([_doubt()], rec, ledger=ledger)
    assert ok is True
    bind.assert_not_called()
    assert json.loads(ledger.read_text())["confirmed"]["3"] == _doubt().fingerprint
    assert "won't ask again" in rec.said[0]


def test_cancel_stops_before_anything_is_sliced(tmp_path):
    rec = Recorder(lambda labels: labels.index("Cancel"))
    ok, bind, _ = _run([_doubt()], rec, ledger=tmp_path / "slot-verify.json")
    assert ok is False
    bind.assert_not_called()
    assert "Nothing was sliced" in rec.said[0]


def test_a_failed_rebind_stops_and_says_so(tmp_path):
    rec = Recorder(0)
    ok, _bind, _ = _run([_doubt()], rec, bind=mock.Mock(side_effect=RuntimeError("readback mismatch")),
                        ledger=tmp_path / "slot-verify.json")
    assert ok is False
    assert "Could not link spool #22 to slot 3" in rec.said[0] and "Nothing was sliced" in rec.said[0]


def test_no_doubt_asks_nothing():
    rec = Recorder(0)
    ok, bind, _ = _run([], rec)
    assert ok is True and rec.asked == [] and rec.said == []
    bind.assert_not_called()


def test_unreadable_screen_lanes_mean_no_doubt(tmp_path):
    with mock.patch("print_handy.runtime.spool_sync.read_lane_data", side_effect=OSError("offline")), \
            mock.patch("print_handy.runtime.spool_sync.moonraker_for", return_value="mr"), \
            mock.patch.object(sv, "_path", return_value=tmp_path / "slot-verify.json"):
        assert sv.read_doubts({"filament_slots": SLOTS}, "flashforge-ad5x") == []


# ------------------------------------------------------------------ intake wiring

class FakeAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return S(success=True)


def test_intake_asks_through_its_own_question_and_cancel_returns_false(tmp_path):
    adapter = FakeAdapter()
    source = S(chat_id="c1")

    async def fake_ask(_adapter, _source, _key, text, labels, _timeout, **_kw):
        assert "Before slicing" in text
        return labels.index("Cancel")

    with mock.patch.object(prepared_intake, "_ask", fake_ask), \
            mock.patch.object(sv, "read_doubts", return_value=[_doubt()]), \
            mock.patch.object(sv, "_path", return_value=tmp_path / "slot-verify.json"):
        ok = asyncio.run(prepared_intake._verify_slots(adapter, source, "k", {}, {}, "flashforge-ad5x", 60))
    assert ok is False
    assert adapter.sent == ["Cancelled. Nothing was sliced, uploaded or started."]


def test_the_intake_checks_slots_before_offering_colors():
    """The hook sits after the native-screen purge refresh and before the color selections are built."""
    import inspect
    body = inspect.getsource(prepared_intake._prepare_link)
    hook = body.index("await _verify_slots(")
    assert body.index("purge.require_jacker_capacity(") < hook < body.index("selections = [mj.resolve_filament_selection(")
    assert "CANCELLED_SLOT_UNVERIFIED" in body
