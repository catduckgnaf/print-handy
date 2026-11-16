"""#1536 box 5: /spool sync and the Web UI sync go both ways.

A slot only Print Handy's local record holds is written to Spoolman as
loaded, then Spoolman's claims rebuild the record. Before this, both
entry points ran reconcile alone, which dropped the local-only slot.
Drives the Web UI route handler (spools.spool_action) and the chat
handler's sync call (spool_sync.sync_external_spoolman) against a
stateful fake Spoolman. Nothing contacts a printer.
"""
import json
from unittest import mock

import pytest

from print_handy.runtime import spool_sync
from print_handy.webui import spools
from tests.test_webui_spools import PRINTER, FakeSpoolman, enabled, fake_mr


@pytest.fixture(autouse=True)
def _no_screens(monkeypatch):
    monkeypatch.setattr(spool_sync, "push_screen", lambda *a, **k: "pushed")
    monkeypatch.setattr(spool_sync, "_printer_adapter", lambda pid: None)


def _record(tmp_path, slots):
    path = tmp_path / "printer_hardware.json"
    path.write_text(json.dumps({"spool": {}, "ph_slot_migrated": True, "filament_slots": slots}))
    return path


def _local(sid, grams=500):
    return {"spoolman_id": sid, "name": "Coffee PLA+", "material": "PLA+", "color": "coffee",
            "hex": "6F4E37", "remaining_grams": grams}


def _slots(path):
    return {int(k): v["spoolman_id"] for k, v in json.loads(path.read_text())["filament_slots"].items()}


def test_web_ui_sync_writes_a_local_only_slot_to_spoolman_and_keeps_it(tmp_path):
    fake = FakeSpoolman()  # spool #3 claims slot 2; #7 is on the shelf with no claim
    state = _record(tmp_path, {"1": _local(7)})
    code, out = spools.spool_action(enabled(str(tmp_path)), PRINTER, "sync", {},
                                    client=fake, mr=fake_mr(), state_path=state)
    assert code == 200, out
    assert json.loads(fake.spools[7]["extra"]["ph_slot"]) == f"{PRINTER}:1"
    assert _slots(state) == {1: 7, 2: 3}
    assert out["pushed"] == [{"slot": 1, "spool_id": 7}]
    assert "written to Spoolman as loaded" in out["detail"]


def test_sync_never_claims_an_archived_or_missing_spool(tmp_path):
    fake = FakeSpoolman()  # #11 is archived, #999 does not exist
    state = _record(tmp_path, {"1": _local(11), "3": _local(999)})
    result = spool_sync.sync_external_spoolman(PRINTER, client=fake, mr=fake_mr(), state_path=state,
                                               pending_path=tmp_path / "pending.json")
    assert "ph_slot" not in fake.spools[11]["extra"]
    reasons = {item["spool_id"]: item["reason"] for item in result["not_pushed"]}
    assert reasons == {11: "the spool is archived in Spoolman", 999: "Spoolman has no such spool"}
    assert 1 not in _slots(state) and 3 not in _slots(state)
    assert "so the slot follows Spoolman" in spool_sync.sync_external_text(result)


def test_a_conflict_is_named_and_spoolmans_claim_wins(tmp_path):
    fake = FakeSpoolman()  # #3 claims slot 2 in Spoolman
    state = _record(tmp_path, {"2": _local(7)})
    result = spool_sync.sync_external_spoolman(PRINTER, client=fake, mr=fake_mr(), state_path=state,
                                               pending_path=tmp_path / "pending.json")
    assert "ph_slot" not in fake.spools[7]["extra"]
    assert _slots(state) == {2: 3}
    assert "Conflict: slot 2 holds spool #7" in spool_sync.sync_external_text(result)


def test_chat_spool_sync_runs_the_two_way_sync():
    from tests.runtime.test_spool_guided import _run

    calls = []

    def two_way(**kw):
        calls.append(kw)
        return {"reconcile": {"ok": True, "printer_id": PRINTER, "assignments": {1: 7}},
                "pushed": [{"slot": 1, "spool_id": 7}], "not_pushed": [],
                "plan": {"conflicts": []}}

    with mock.patch.object(spool_sync, "sync_external_spoolman", side_effect=two_way), \
         mock.patch.object(spool_sync, "reconcile", side_effect=AssertionError("one-way reconcile")), \
         mock.patch.object(spool_sync, "sync_all_screens", return_value=["Slot 1: pushed"]):
        _out, adapter, _fake = _run("/spool sync", [])
    assert len(calls) == 1
    assert "written to Spoolman as loaded" in adapter.sent[-1]


def test_an_unreachable_spoolman_changes_nothing_through_the_web_ui(tmp_path):
    def down(method, path, body=None):
        raise OSError("connection refused")

    state = _record(tmp_path, {"1": _local(7)})
    before = state.read_text()
    code, out = spools.spool_action(enabled(str(tmp_path)), PRINTER, "sync", {},
                                    client=down, mr=fake_mr(), state_path=state)
    assert code == 502 and "did not answer" in out["reason"]
    assert state.read_text() == before
