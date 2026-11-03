"""Screen-side spool changes observed from HelixScreen's lane_data (#791, step 1).

The fakes follow test_spool_reconcile.py: a stateful callable Spoolman and a
callable Moonraker, here also serving the lane_data namespace.
"""
import copy
import json

from print_handy.hermes import lane_observer, spool_sync

PID = "ad5x"


class FakeSpoolman:
    def __init__(self, spools=()):
        self.spools = {int(s["id"]): copy.deepcopy(s) for s in spools}
        self.calls = []
        self.down = False
        self.reject_ph_slot = False

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if self.down:
            raise OSError("spoolman down")
        if method == "GET" and path == "/api/v1/field/spool":
            return [{"key": "ph_slot", "name": "Print Handy slot", "field_type": "text"}]
        if method == "GET" and path == "/api/v1/spool?allow_archived=true":
            return [copy.deepcopy(s) for s in self.spools.values()]
        if method == "GET" and path.startswith("/api/v1/spool/"):
            return copy.deepcopy(self.spools[int(path.rsplit("/", 1)[1])])
        if method == "PATCH" and path.startswith("/api/v1/spool/"):
            if self.reject_ph_slot and "ph_slot" in (body or {}).get("extra", {}):
                raise OSError("ph_slot write rejected")
            row = self.spools[int(path.rsplit("/", 1)[1])]
            for key, value in (body or {}).items():
                if key == "extra":
                    row.setdefault("extra", {}).update(value)
                else:
                    row[key] = value
            return {}
        return {}

    def writes(self):
        return [c for c in self.calls if c[0] in ("PATCH", "POST", "DELETE")]


def _spool(sid, name="Black PLA", material="PLA", hex_code="161616", ph_slot=None, archived=False):
    row = {"id": sid, "location": "Shelf", "archived": archived, "remaining_weight": 800,
           "filament": {"name": name, "material": material, "color_hex": hex_code}}
    if ph_slot:
        row["extra"] = {"ph_slot": json.dumps(ph_slot)}
    return row


def _lane(sid, material="PLA", color="#161616", locked=False):
    return {"spool_id": sid, "material": material, "color": color, "helix_locked_color": locked}


class FakeMoonraker:
    def __init__(self, lanes=None, state="standby"):
        self.lanes = lanes
        self.state = state
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path))
        if path.startswith("/server/database/item?namespace=lane_data"):
            if self.lanes is None:
                raise OSError("moonraker down")
            return {"result": {"namespace": "lane_data", "key": None, "value": self.lanes}}
        if path.startswith("/printer/objects/query"):
            return {"result": {"status": {"print_stats": {"state": self.state}}}}
        if path == "/server/spoolman/spool_id":
            return {"result": {"spool_id": None}}
        return {"result": "ok"}


def _hardware(tmp_path, slots=None):
    path = tmp_path / "printer_hardware.json"
    path.write_text(json.dumps({"spool": {}, "filament_slots": slots or {}}))
    return path


def _observe(tmp_path, fake, mr, hw=None):
    return lane_observer.observe(PID, client=fake, mr=mr, state_path=hw or _hardware(tmp_path),
                                 observer_path=tmp_path / "lane_observer.json")


def _setup(tmp_path):
    """Spoolman: #3 in slot 2, #7 on the shelf. The screen agrees with Spoolman."""
    fake = FakeSpoolman([_spool(3, "Pink PLA", hex_code="F95D73", ph_slot="ad5x:2"),
                         _spool(7, "Coffee PLA", hex_code="6F4E37")])
    mr = FakeMoonraker({"lane2": _lane(3, color="#F95D73"), "seated": 2})
    return fake, mr, _hardware(tmp_path)


# ---------------------------------------------------------------- reading

def test_read_lanes_keeps_only_real_lanes():
    mr = FakeMoonraker({"lane1": _lane(1), "lane9": _lane(2), "seated": 2, "lane3": "junk"})
    assert list(lane_observer.read_lanes(mr)) == [1]


def test_read_lanes_is_none_when_unreadable():
    assert lane_observer.read_lanes(FakeMoonraker(None)) is None
    assert lane_observer.read_lanes(lambda *a, **k: {"result": {"value": "nope"}}) is None


# ---------------------------------------------------------------- the observe loop

def test_agreement_is_silent_and_writes_nothing(tmp_path):
    fake, mr, hw = _setup(tmp_path)
    result = _observe(tmp_path, fake, mr, hw)
    assert result["ok"] and result["events"] == []
    assert fake.writes() == []


def test_screen_swap_is_bound_and_announced_once(tmp_path):
    fake, mr, hw = _setup(tmp_path)
    _observe(tmp_path, fake, mr, hw)  # seed
    mr.lanes["lane2"] = _lane(7, color="#6F4E37")  # swapped at the panel
    result = _observe(tmp_path, fake, mr, hw)
    assert [e["kind"] for e in result["events"]] == ["changed"]
    assert "spool #3 -> spool #7" in result["events"][0]["text"]
    assert spool_sync.spool_ph_slot(fake.spools[7]) == (PID, 2)
    assert spool_sync.spool_ph_slot(fake.spools[3]) is None  # the old claim is released
    assert _observe(tmp_path, fake, mr, hw)["events"] == []  # exactly once


def test_first_poll_names_drift_but_binds_nothing(tmp_path):
    fake, mr, hw = _setup(tmp_path)
    mr.lanes["lane2"] = _lane(7, color="#6F4E37")
    result = _observe(tmp_path, fake, mr, hw)
    assert [e["kind"] for e in result["events"]] == ["conflict"]
    assert "Spoolman assigns spool #3" in result["events"][0]["text"]
    assert fake.writes() == []
    assert _observe(tmp_path, fake, mr, hw)["events"] == []  # a standing conflict is not repeated


def test_unreachable_screen_is_unknown_not_empty(tmp_path):
    fake, mr, hw = _setup(tmp_path)
    _observe(tmp_path, fake, mr, hw)
    mr.lanes = None
    result = _observe(tmp_path, fake, mr, hw)
    assert not result["ok"] and "could not be read" in result["error"]
    assert fake.writes() == []
    assert spool_sync.spool_ph_slot(fake.spools[3]) == (PID, 2)


def test_missing_lane_does_not_unassign_the_slot(tmp_path):
    fake, mr, hw = _setup(tmp_path)
    _observe(tmp_path, fake, mr, hw)
    mr.lanes = {"seated": 2}  # namespace answers but no lane records
    result = _observe(tmp_path, fake, mr, hw)
    assert result["ok"] and result["events"] == []
    assert fake.writes() == []


def test_spoolman_down_changes_nothing(tmp_path):
    fake, mr, hw = _setup(tmp_path)
    fake.down = True
    result = _observe(tmp_path, fake, mr, hw)
    assert not result["ok"] and "Spoolman did not answer" in result["error"]


def test_archived_spool_is_a_named_conflict_once(tmp_path):
    fake, mr, hw = _setup(tmp_path)
    fake.spools[18] = _spool(18, "Rose red", archived=True)
    mr.lanes["lane2"] = _lane(18, locked=True)
    result = _observe(tmp_path, fake, mr, hw)
    assert "archived" in result["events"][0]["text"]
    assert fake.writes() == []
    assert _observe(tmp_path, fake, mr, hw)["events"] == []


def test_deleted_spool_is_a_named_conflict(tmp_path):
    fake, mr, hw = _setup(tmp_path)
    mr.lanes["lane2"] = _lane(99)
    assert "no longer has" in _observe(tmp_path, fake, mr, hw)["events"][0]["text"]


def test_material_mismatch_is_not_bound(tmp_path):
    fake, mr, hw = _setup(tmp_path)
    _observe(tmp_path, fake, mr, hw)
    mr.lanes["lane2"] = _lane(7, material="PETG")
    result = _observe(tmp_path, fake, mr, hw)
    assert "PETG" in result["events"][0]["text"] and fake.writes() == []


def test_pla_plus_matches_pla(tmp_path):
    fake, mr, hw = _setup(tmp_path)
    _observe(tmp_path, fake, mr, hw)
    mr.lanes["lane2"] = _lane(7, material="PLA+")
    assert _observe(tmp_path, fake, mr, hw)["events"][0]["kind"] == "changed"


def test_locked_color_that_differs_is_a_conflict_not_an_overwrite(tmp_path):
    fake, mr, hw = _setup(tmp_path)
    _observe(tmp_path, fake, mr, hw)
    mr.lanes["lane2"] = _lane(7, color="#E53935", locked=True)
    result = _observe(tmp_path, fake, mr, hw)
    assert "locked to #E53935" in result["events"][0]["text"] and fake.writes() == []


def test_unlocked_color_difference_still_binds(tmp_path):
    fake, mr, hw = _setup(tmp_path)
    _observe(tmp_path, fake, mr, hw)
    mr.lanes["lane2"] = _lane(7, color="#E53935", locked=False)
    assert _observe(tmp_path, fake, mr, hw)["events"][0]["kind"] == "changed"


def test_unlinked_lane_is_never_guessed(tmp_path):
    fake, mr, hw = _setup(tmp_path)
    mr.lanes["lane2"] = {"material": "PLA", "color": "#161616"}
    result = _observe(tmp_path, fake, mr, hw)
    assert "no Spoolman link" in result["events"][0]["text"] and fake.writes() == []


def test_spool_assigned_to_another_slot_is_a_conflict(tmp_path):
    fake, mr, hw = _setup(tmp_path)
    fake.spools[7]["extra"] = {"ph_slot": json.dumps("ad5x:4")}
    _observe(tmp_path, fake, mr, hw)
    mr.lanes["lane2"] = _lane(7)
    assert "assigns to slot 4" in _observe(tmp_path, fake, mr, hw)["events"][0]["text"]


def test_unsaved_ph_slot_is_retried_not_marked_observed(tmp_path):
    fake, mr, hw = _setup(tmp_path)
    _observe(tmp_path, fake, mr, hw)
    mr.lanes["lane2"] = _lane(7)
    fake.reject_ph_slot = True
    result = _observe(tmp_path, fake, mr, hw)
    assert [e["kind"] for e in result["events"]] == ["conflict"]
    assert "did not save" in result["events"][0]["text"]
    assert _observe(tmp_path, fake, mr, hw)["events"] == []  # a standing failure is not repeated
    fake.reject_ph_slot = False
    result = _observe(tmp_path, fake, mr, hw)  # Spoolman recovers: the same change is retried
    assert [e["kind"] for e in result["events"]] == ["changed"]
    assert spool_sync.spool_ph_slot(fake.spools[7]) == (PID, 2)


def test_spool_assigned_to_another_printer_is_not_taken(tmp_path):
    fake, mr, hw = _setup(tmp_path)
    fake.spools[7]["extra"] = {"ph_slot": json.dumps("other:1")}
    _observe(tmp_path, fake, mr, hw)
    mr.lanes["lane2"] = _lane(7)
    result = _observe(tmp_path, fake, mr, hw)
    assert "slot 1 on printer other" in result["events"][0]["text"]
    assert fake.writes() == []
    assert spool_sync.spool_ph_slot(fake.spools[7]) == ("other", 1)


def test_lane_first_seen_later_is_seeded_not_swapped(tmp_path):
    fake, mr, hw = _setup(tmp_path)
    mr.lanes = {"seated": 2}  # first successful read has no lane records at all
    assert _observe(tmp_path, fake, mr, hw)["events"] == []
    mr.lanes = {"lane2": _lane(7, color="#6F4E37")}  # the lane appears holding a different spool
    result = _observe(tmp_path, fake, mr, hw)
    assert [e["kind"] for e in result["events"]] == ["conflict"]
    assert "Spoolman assigns spool #3" in result["events"][0]["text"]
    assert fake.writes() == []
    assert spool_sync.spool_ph_slot(fake.spools[3]) == (PID, 2)


def test_busy_printer_defers_then_applies_after_the_print(tmp_path):
    fake, mr, hw = _setup(tmp_path)
    _observe(tmp_path, fake, mr, hw)
    mr.lanes["lane2"] = _lane(7)
    mr.state = "printing"
    result = _observe(tmp_path, fake, mr, hw)
    assert [e["kind"] for e in result["events"]] == ["deferred"] and fake.writes() == []
    assert _observe(tmp_path, fake, mr, hw)["events"] == []  # noted once while printing
    mr.state = "standby"
    assert _observe(tmp_path, fake, mr, hw)["events"][0]["kind"] == "changed"
    assert spool_sync.spool_ph_slot(fake.spools[7]) == (PID, 2)


def test_unknown_printer_state_counts_as_busy(tmp_path):
    fake, mr, hw = _setup(tmp_path)
    _observe(tmp_path, fake, mr, hw)
    mr.lanes["lane2"] = _lane(7)
    mr.state = "unknown"
    assert _observe(tmp_path, fake, mr, hw)["events"][0]["kind"] == "deferred"


def test_held_control_lock_retries_later(tmp_path):
    class Held:
        def acquire(self, blocking=True):
            return False

        def release(self):
            raise AssertionError("never acquired, never released")

    fake, mr, hw = _setup(tmp_path)
    from unittest import mock
    with mock.patch.object(spool_sync, "_reconcile_lock", return_value=Held()):
        result = _observe(tmp_path, fake, mr, hw)
    assert not result["ok"] and "in flight" in result["error"] and fake.writes() == []


def test_no_derivable_printer_changes_nothing(tmp_path):
    from unittest import mock
    fake, mr, _ = _setup(tmp_path)
    with mock.patch.object(spool_sync, "_default_printer_id", return_value=None):
        result = lane_observer.observe(None, client=fake, mr=mr,
                                       observer_path=tmp_path / "lane_observer.json")
    assert not result["ok"] and fake.writes() == []
