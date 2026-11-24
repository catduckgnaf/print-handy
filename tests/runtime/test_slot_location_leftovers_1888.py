"""A replaced spool does not keep its "IFS Slot N" location (#1888).

2026-11-21 16:17: a screen-side change rebound slot 3 from PETG spool 22 to spool 21.
Spool 22 had no ``ph_slot`` claim, and nothing put it back on the shelf: days later
Spoolman still listed it at "IFS Slot 3", beside spool 24, which was the spool actually
loaded there. The screen-side converge never wrote locations, and a later bind only
shelved the one spool its record knew. All Spoolman and Moonraker answers come from
fakes: nothing contacts a printer.
"""
import json

import pytest

from print_handy.runtime import lane_data_sync, spool_sync
from print_handy.webui import spools
from tests.runtime.test_lane_data_sync import PID, FakeMoonraker, FakeSpoolman, spool
from tests.test_webui_spools import PRINTER, enabled, fake_mr
from tests.test_webui_spools import FakeSpoolman as WebFakeSpoolman


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(spool_sync, "push_screen", lambda *a, **k: "pushed")
    monkeypatch.setattr(spool_sync, "_default_printer_id", lambda: PID)


def _record(tmp_path, slots):
    path = tmp_path / "printer_hardware.json"
    path.write_text(json.dumps({"spool": {}, "filament_slots": {
        str(slot): {"slot": slot, "spoolman_id": sid, "material": "PETG", "hex": "D2B48C",
                    "name": "Tan PETG"} for slot, sid in slots.items()}}))
    return path


def _at(row, location):
    row["location"] = location
    return row


def test_a_screen_side_load_shelves_the_replaced_spool_without_a_claim(tmp_path):
    """The live replay: slot 3 held spool 22 (no claim), the screen loads spool 21."""
    client = FakeSpoolman([_at(spool(22, "Tan", "PETG", "D2B48C"), "IFS Slot 3"),
                           spool(21, "Coffee", "PLA", "6F4E37")])
    state = _record(tmp_path, {3: 22})
    result = lane_data_sync.apply_screen_assignment(
        PID, 3, 21, client=client, mr=FakeMoonraker(lanes={}), state_path=state,
        pending_path=tmp_path / "pending.json")
    assert result["assignments"] == {3: 21}
    assert client.spools[22]["location"] == spool_sync.SHELF
    assert client.spools[21]["location"] == "IFS Slot 3"


def test_a_screen_side_unload_shelves_the_spool_that_left(tmp_path):
    client = FakeSpoolman([_at(spool(22, "Tan", "PETG", "D2B48C", ph_slot=f"{PID}:3"), "IFS Slot 3")])
    state = _record(tmp_path, {3: 22})
    lane_data_sync.apply_screen_assignment(PID, 3, None, client=client, mr=FakeMoonraker(lanes={}),
                                           state_path=state, pending_path=tmp_path / "pending.json")
    assert client.spools[22]["location"] == spool_sync.SHELF


def test_a_bind_shelves_every_spool_left_at_the_slot(tmp_path):
    """Slot 3's record holds spool 21; spool 22 is still at IFS Slot 3 with no claim."""
    client = FakeSpoolman([_at(spool(21, "Coffee", "PLA", "6F4E37", ph_slot=f"{PID}:3"), "IFS Slot 3"),
                           _at(spool(22, "Tan", "PETG", "D2B48C"), "IFS Slot 3"),
                           spool(24, "Gray", "PETG", "808080")])
    result = spool_sync.bind_slot(3, 24, client=client, mr=FakeMoonraker(lanes={}),
                                  state_path=_record(tmp_path, {3: 21}), push_screen_update=False)
    assert result["prior_spool_id"] == 21
    assert client.spools[24]["location"] == "IFS Slot 3"
    assert client.spools[21]["location"] == spool_sync.SHELF  # the recorded spool, as before
    assert client.spools[22]["location"] == spool_sync.SHELF  # the one nothing knew about


def test_spools_that_belong_elsewhere_keep_their_location(tmp_path):
    """Another printer's slot 3, and a spool this printer holds in another slot, stay put."""
    client = FakeSpoolman([_at(spool(30, ph_slot="x1c:3"), "IFS Slot 3"),
                           _at(spool(31, ph_slot=f"{PID}:2"), "IFS Slot 3"),
                           _at(spool(32), "IFS Slot 3"),
                           _at(spool(33, archived=True), "IFS Slot 3"),
                           spool(24, "Gray", "PETG", "808080")])
    spool_sync.bind_slot(3, 24, client=client, mr=FakeMoonraker(lanes={}),
                         state_path=_record(tmp_path, {1: 32}), push_screen_update=False)
    assert client.spools[30]["location"] == "IFS Slot 3"
    assert client.spools[31]["location"] == "IFS Slot 3"
    assert client.spools[32]["location"] == "IFS Slot 3"  # the record holds it in slot 1
    assert client.spools[33]["location"] == "IFS Slot 3"  # archived: not live inventory


def test_a_spoolman_that_does_not_list_still_binds(tmp_path):
    class NoList(FakeSpoolman):
        def __call__(self, method, path, body=None):
            if method == "GET" and path == "/api/v1/spool?allow_archived=true":
                raise spool_sync.SpoolError("Spoolman did not answer")
            return super().__call__(method, path, body)

    client = NoList([_at(spool(22), "IFS Slot 3"), spool(24, "Gray", "PETG", "808080")])
    result = spool_sync.bind_slot(3, 24, client=client, mr=FakeMoonraker(lanes={}),
                                  state_path=_record(tmp_path, {}), push_screen_update=False)
    assert result["spool_id"] == 24
    assert client.spools[22]["location"] == "IFS Slot 3"


def test_spool_sync_names_a_location_that_names_a_slot_the_spool_does_not_hold(tmp_path):
    """Through the Web UI sync route, which shares /spool sync's wording."""
    fake = WebFakeSpoolman()  # spool 3 claims slot 2 and is at IFS Slot 2
    fake.spools[9]["location"] = "IFS Slot 2"  # spool 9 is not loaded anywhere
    fake.spools[7]["location"] = "IFS Slot 4"  # nothing is loaded in slot 4
    state = tmp_path / "printer_hardware.json"
    state.write_text(json.dumps({"spool": {}, "filament_slots": {}}))
    code, out = spools.spool_action(enabled(str(tmp_path)), PRINTER, "sync", {},
                                    client=fake, mr=fake_mr(), state_path=state)
    assert code == 200, out
    detail = out["detail"]
    assert ("Location: spool #9 is at IFS Slot 2 in Spoolman, but slot 2 holds spool #3."
            in detail)
    assert "Location: spool #7 is at IFS Slot 4 in Spoolman, but slot 4 is empty" in detail
    assert "spool #3 is at IFS Slot 2" not in detail
    assert fake.spools[9]["location"] == "IFS Slot 2"  # reported, not changed


def test_spool_sync_names_a_loaded_spool_whose_location_names_another_slot(tmp_path):
    fake = WebFakeSpoolman()
    fake.spools[3]["location"] = "IFS Slot 1"  # claims slot 2
    state = tmp_path / "printer_hardware.json"
    state.write_text(json.dumps({"spool": {}, "filament_slots": {}}))
    result = spool_sync.sync_external_spoolman(PRINTER, client=fake, mr=fake_mr(), state_path=state,
                                               pending_path=tmp_path / "pending.json")
    assert result["stray_locations"] == [{"spool_id": 3, "slot": 1, "holder": None, "held_slot": 2}]
    assert ("spool #3 is at IFS Slot 1 in Spoolman, but it is loaded in slot 2"
            in spool_sync.sync_external_text(result))


def test_spool_sync_with_every_location_right_says_nothing_more(tmp_path):
    fake = WebFakeSpoolman()
    state = tmp_path / "printer_hardware.json"
    state.write_text(json.dumps({"spool": {}, "filament_slots": {}}))
    result = spool_sync.sync_external_spoolman(PRINTER, client=fake, mr=fake_mr(), state_path=state,
                                               pending_path=tmp_path / "pending.json")
    assert result["stray_locations"] == []
    assert "Location:" not in spool_sync.sync_external_text(result)
