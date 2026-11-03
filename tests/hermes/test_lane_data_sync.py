"""HelixScreen inbound spool sync (#791).

The screen side of spool management: Moonraker's shared ``lane_data``
namespace is polled on the listener's cadence, screen-initiated load /
unload / swap changes converge Print Handy's records through the #798
reconciler (Spoolman ``ph_slot``), and the drift classes - locked lanes,
archived/deleted spools, id-less lanes, mid-print changes, unreachable
state - are named, never silently overwritten. All Moonraker and
Spoolman answers come from fakes: no network, no printer.
"""
import asyncio
import copy
import json
from unittest import mock

from print_handy.hermes import lane_data_sync
from print_handy.hermes import moonraker_event_listener as mel
from print_handy.hermes import spool_sync

PID = "ad5x"


# --------------------------------------------------------------------- fakes

class FakeMoonraker:
    """Moonraker fake: lane_data namespace, print state, spool pointer, gcode posts."""

    def __init__(self, lanes=None, state="standby"):
        self.lanes = lanes  # None means the namespace does not answer (absent)
        self.state = state
        self.calls = []
        self.down = False
        self.active = [None]

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if self.down:
            raise OSError("moonraker down")
        if path.startswith("/server/database/item"):
            if method == "GET":
                if self.lanes is None:
                    return {"result": {}}
                return {"result": {"value": copy.deepcopy(self.lanes)}}
            if method == "POST":
                if self.lanes is None:
                    self.lanes = {}
                self.lanes[body["key"]] = copy.deepcopy(body["value"])
                return {"result": {"value": copy.deepcopy(body["value"])}}
        if path.startswith("/printer/objects/query"):
            return {"result": {"status": {"print_stats": {"state": self.state}}}}
        if path == "/server/spoolman/spool_id":
            if method == "POST":
                self.active[0] = (body or {}).get("spool_id")
                return {}
            return {"result": {"spool_id": self.active[0]}}
        return {"result": "ok"}

    def gcode_scripts(self):
        return [path for method, path, _body in self.calls
                if method == "POST" and path.startswith("/printer/gcode/script")]


class FakeSpoolman:
    """A stateful in-memory Spoolman: field defs, spools with extra values, a call log."""

    def __init__(self, spools=(), fields=()):
        self.spools = {int(s["id"]): copy.deepcopy(s) for s in spools}
        self.fields = [dict(f) for f in fields]
        self.calls = []
        self.down = False

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if self.down:
            raise OSError("spoolman down")
        if method == "GET" and path == "/api/v1/field/spool":
            return copy.deepcopy(self.fields)
        if method == "POST" and path == "/api/v1/field/spool/ph_slot":
            self.fields.append({"key": "ph_slot", **(body or {})})
            return {}
        if method == "GET" and path == "/api/v1/spool?allow_archived=true":
            return [copy.deepcopy(s) for s in self.spools.values()]
        if method == "GET" and path.startswith("/api/v1/spool/"):
            return copy.deepcopy(self.spools[int(path.rsplit("/", 1)[1])])
        if method == "PATCH" and path.startswith("/api/v1/spool/"):
            row = self.spools[int(path.rsplit("/", 1)[1])]
            for key, value in (body or {}).items():
                if key == "extra":
                    row.setdefault("extra", {}).update(value)
                else:
                    row[key] = value
            return {}
        if method == "DELETE" and path.startswith("/api/v1/spool/"):
            del self.spools[int(path.rsplit("/", 1)[1])]
            return {}
        return {}


def spool(sid, name="Black", material="PLA", hex_code="161616", archived=False,
          ph_slot=None, remaining=800, vendor_id=2):
    row = {"id": sid, "location": "Shelf", "archived": archived,
           "remaining_weight": remaining,
           "filament": {"name": name, "material": material, "color_hex": hex_code,
                        "vendor": {"id": vendor_id, "name": "eSun"}}}
    if ph_slot is not None:
        row["extra"] = {"ph_slot": json.dumps(ph_slot)}
    return row


def lane(spool_id, *, slot=1, material="PLA", color="161616", scan_time=100,
         locked_color=False, locked_material=False, name="Black PLA"):
    record = {"spool_id": spool_id, "lane": slot - 1, "scan_time": scan_time,
              "color": color, "material": material, "name": name,
              "helix_locked_color": locked_color, "helix_locked_material": locked_material}
    return record


def make_watcher(tmp_path, mr, client, **kw):
    """A watcher with a recording apply_fn; returns (watcher, applied list)."""
    applied = []
    watcher = lane_data_sync.LaneDataWatcher(
        tmp_path / "lane_watch.json", mr=mr, client=client, printer_id=PID,
        printer_state_fn=lambda _mr: mr.state,
        apply_fn=lambda slot, sid: applied.append((slot, sid)), **kw)
    return watcher, applied


def seed(watcher, mr, lanes):
    mr.lanes = lanes
    assert watcher.poll() == []


# --------------------------------------------------------------------- parsing

def test_parse_lane_data_outer_key_is_1_based_inner_lane_is_0_based():
    lanes = lane_data_sync.parse_lane_data({"lane2": lane(19, slot=2), "junk": {"spool_id": 1}})
    assert sorted(lanes) == [2]
    assert lanes[2]["spool_id"] == 19
    assert lanes[2]["lane"] == 1  # the inner field stays 0-based, as written


def test_parse_lane_data_unknown_is_none_never_empty():
    assert lane_data_sync.parse_lane_data(None) is None
    assert lane_data_sync.parse_lane_data("lane_data") is None
    assert lane_data_sync.parse_lane_data({}) == {}  # a genuinely empty namespace


def test_read_lane_data_unreachable_or_absent_is_unknown():
    mr = FakeMoonraker(lanes=None)
    assert spool_sync.read_lane_data(mr) is None  # namespace absent
    mr.down = True
    assert spool_sync.read_lane_data(mr) is None  # Moonraker unreachable
    mr.down = False
    mr.lanes = {"lane1": lane(11)}
    assert spool_sync.read_lane_data(mr) == {"lane1": lane(11)}


# --------------------------------------------------------------------- bind-side write

def test_write_lane_record_shape_and_helix_preservation():
    mr = FakeMoonraker(lanes={"lane2": {**lane(18, slot=2), "helix_source": "screen",
                                        "helix_locked_color": True}})
    record = spool_sync.write_lane_record(2, spool(19, "White", "PLA", "FFFFFF"),
                                          mr=mr, name="White PLA", now=123456)
    assert mr.lanes["lane2"] == record
    assert record["lane"] == 1  # inner lane is 0-based for slot 2
    assert record["spool_id"] == 19
    assert record["spoolman_vendor_id"] == 2
    assert record["material"] == "PLA" and record["color"] == "FFFFFF"
    assert record["name"] == "White PLA"
    assert record["scan_time"] == 123456
    # Print Handy declares nothing on the user's behalf...
    assert record["helix_locked_color"] is False
    assert record["helix_locked_material"] is False
    # ...but other helix_* keys the screen keeps are preserved.
    assert record["helix_source"] == "screen"
    posts = [c for c in mr.calls if c[0] == "POST" and c[1] == "/server/database/item"]
    assert len(posts) == 1 and posts[0][2]["namespace"] == "lane_data"
    assert posts[0][2]["key"] == "lane2"


def test_bind_slot_writes_the_lane_record_beside_one_change_zcolor(tmp_path):
    fake = FakeSpoolman([spool(7, "Coffee", "PLA+", "6F4E37")])
    mr = FakeMoonraker(lanes={})
    state = tmp_path / "printer_hardware.json"
    state.write_text(json.dumps({"spool": {}, "filament_slots": {}}))
    result = spool_sync.bind_slot(2, 7, client=fake, mr=mr, state_path=state)
    assert result["lane_data"] == "written"
    assert mr.lanes["lane2"]["spool_id"] == 7
    assert mr.lanes["lane2"]["lane"] == 1
    assert mr.lanes["lane2"]["material"] == "PLA+"
    # CHANGE_ZCOLOR comes from push_screen alone: exactly one gcode post.
    assert len(mr.gcode_scripts()) == 1
    assert "CHANGE_ZCOLOR" in mr.gcode_scripts()[0]


def test_bind_slot_without_a_screen_update_writes_no_lane_record(tmp_path):
    fake = FakeSpoolman([spool(7, "Coffee", "PLA+", "6F4E37")])
    mr = FakeMoonraker(lanes={})
    state = tmp_path / "printer_hardware.json"
    state.write_text(json.dumps({"spool": {}, "filament_slots": {}}))
    result = spool_sync.bind_slot(2, 7, client=fake, mr=mr, state_path=state,
                                  push_screen_update=False)
    assert result["lane_data"] == "not requested"
    assert mr.lanes == {}


# --------------------------------------------------------------------- watcher: diff + dedupe

def test_first_poll_seeds_silently(tmp_path):
    mr = FakeMoonraker()
    client = FakeSpoolman([spool(11)])
    watcher, applied = make_watcher(tmp_path, mr, client)
    seed(watcher, mr, {"lane1": lane(11)})
    assert applied == []
    assert watcher.poll() == []


def test_screen_side_swap_is_applied_and_announced_exactly_once(tmp_path):
    mr = FakeMoonraker()
    client = FakeSpoolman([spool(11), spool(19, "White", "PLA", "FFFFFF")])
    watcher, applied = make_watcher(tmp_path, mr, client)
    seed(watcher, mr, {"lane1": lane(11)})
    mr.lanes = {"lane1": lane(19, color="FFFFFF", scan_time=200, name="White PLA")}
    events = watcher.poll()
    assert [(e["kind"], e["slot"], e["spool_id"]) for e in events] == [("loaded", 1, 19)]
    assert "#19" in events[0]["text"] and "#11" in events[0]["text"]
    assert applied == [(1, 19)]
    # Not again on the next poll, even before delivery is reported...
    assert watcher.poll() == []
    watcher.delivered(events[0]["key"])
    # ...and not after delivery or a restart over the persisted state.
    assert watcher.poll() == []
    restarted, applied2 = make_watcher(tmp_path, mr, client)
    assert restarted.poll() == []
    assert applied2 == []


def test_rescan_with_only_a_new_scan_time_is_silent(tmp_path):
    mr = FakeMoonraker()
    client = FakeSpoolman([spool(11)])
    watcher, applied = make_watcher(tmp_path, mr, client)
    seed(watcher, mr, {"lane1": lane(11, scan_time=100)})
    mr.lanes = {"lane1": lane(11, scan_time=999)}
    assert watcher.poll() == []
    assert applied == []


def test_screen_side_unload_is_applied_and_announced_once(tmp_path):
    mr = FakeMoonraker()
    client = FakeSpoolman([spool(11)])
    watcher, applied = make_watcher(tmp_path, mr, client)
    seed(watcher, mr, {"lane1": lane(11)})
    mr.lanes = {}
    events = watcher.poll()
    assert [(e["kind"], e["slot"]) for e in events] == [("unloaded", 1)]
    assert "#11" in events[0]["text"]
    assert applied == [(1, None)]
    watcher.delivered(events[0]["key"])
    assert watcher.poll() == []
    assert applied == [(1, None)]


def test_undelivered_announcement_is_retried_after_the_interval(tmp_path):
    mr = FakeMoonraker()
    client = FakeSpoolman([spool(11), spool(19, "White", "PLA", "FFFFFF")])
    clock = [1000.0]
    watcher, _applied = make_watcher(tmp_path, mr, client, clock=lambda: clock[0])
    seed(watcher, mr, {"lane1": lane(11)})
    mr.lanes = {"lane1": lane(19, color="FFFFFF", scan_time=200, name="White PLA")}
    assert len(watcher.poll()) == 1
    clock[0] += lane_data_sync.LANE_RETRY_S - 1
    assert watcher.poll() == []  # not hammered on every poll
    clock[0] += 1
    assert len(watcher.poll()) == 1  # retried
    watcher.delivered(_pending_key(tmp_path))
    assert watcher.poll() == []


def _pending_key(tmp_path):
    state = json.loads((tmp_path / "lane_watch.json").read_text())
    return next(iter(state["pending"]))


# --------------------------------------------------------------------- watcher: prompts, no overwrite

def test_locked_lane_with_a_differing_value_prompts_and_overwrites_nothing(tmp_path):
    mr = FakeMoonraker()
    client = FakeSpoolman([spool(11), spool(19, "White", "PLA", "FFFFFF", ph_slot="ad5x:2")])
    watcher, applied = make_watcher(tmp_path, mr, client)
    seed(watcher, mr, {"lane2": lane(11, slot=2)})
    # The live lane2 shape: a spool whose screen-locked color differs from Spoolman.
    mr.lanes = {"lane2": lane(19, slot=2, color="FF0000", scan_time=200,
                              locked_color=True, name="White PLA")}
    events = watcher.poll()
    assert [(e["kind"], e["slot"], e["spool_id"]) for e in events] == [("locked", 2, 19)]
    assert "locked on the screen to FF0000" in events[0]["text"]
    assert "Nothing was overwritten" in events[0]["text"]
    assert "/spool change 2 <spool#>" in events[0]["text"]
    assert applied == []
    assert spool_sync.spool_ph_slot(client.spools[19]) == (PID, 2)  # the claim was left alone


def test_locked_lane_whose_value_agrees_still_converges(tmp_path):
    mr = FakeMoonraker()
    client = FakeSpoolman([spool(11), spool(19, "White", "PLA", "FFFFFF")])
    watcher, applied = make_watcher(tmp_path, mr, client)
    seed(watcher, mr, {"lane2": lane(11, slot=2)})
    mr.lanes = {"lane2": lane(19, slot=2, color="FFFFFF", scan_time=200,
                              locked_color=True, locked_material=True, name="White PLA")}
    events = watcher.poll()
    assert [e["kind"] for e in events] == ["loaded"]
    assert applied == [(2, 19)]


def test_archived_spool_lane_prompts_and_overwrites_nothing(tmp_path):
    # The live AD5X drift: lane2 linked to archived spool #18, both locks true.
    mr = FakeMoonraker()
    client = FakeSpoolman([spool(18, "Old", "PLA", "111111", archived=True),
                           spool(19, "White", "PLA", "FFFFFF", ph_slot="ad5x:2")])
    watcher, applied = make_watcher(tmp_path, mr, client)
    seed(watcher, mr, {"lane2": lane(19, slot=2, color="FFFFFF", name="White PLA")})
    mr.lanes = {"lane2": lane(18, slot=2, color="111111", scan_time=200,
                              locked_color=True, locked_material=True, name="Old PLA")}
    events = watcher.poll()
    assert [(e["kind"], e["slot"], e["spool_id"]) for e in events] == [("stale_spool", 2, 18)]
    assert "#18" in events[0]["text"] and "archived" in events[0]["text"]
    assert "Nothing was overwritten" in events[0]["text"]
    assert applied == []
    assert spool_sync.spool_ph_slot(client.spools[19]) == (PID, 2)


def test_deleted_spool_lane_prompts(tmp_path):
    mr = FakeMoonraker()
    client = FakeSpoolman([spool(11)])
    watcher, applied = make_watcher(tmp_path, mr, client)
    seed(watcher, mr, {"lane1": lane(11)})
    mr.lanes = {"lane1": lane(99, scan_time=200)}
    events = watcher.poll()
    assert [e["kind"] for e in events] == ["stale_spool"]
    assert "#99" in events[0]["text"] and "no longer exists" in events[0]["text"]
    assert applied == []


def test_material_identity_mismatch_is_a_named_conflict_not_a_rebind(tmp_path):
    mr = FakeMoonraker()
    client = FakeSpoolman([spool(11), spool(19, "White", "PLA", "FFFFFF")])
    watcher, applied = make_watcher(tmp_path, mr, client)
    seed(watcher, mr, {"lane1": lane(11)})
    mr.lanes = {"lane1": lane(19, material="PETG", color="FFFFFF", scan_time=200, name="White PETG")}
    events = watcher.poll()
    assert [e["kind"] for e in events] == ["identity_conflict"]
    assert "PETG" in events[0]["text"] and "PLA" in events[0]["text"]
    assert applied == []


def test_lane_without_a_spool_id_is_unresolved_never_guessed(tmp_path):
    mr = FakeMoonraker()
    client = FakeSpoolman([spool(11)])
    watcher, applied = make_watcher(tmp_path, mr, client)
    seed(watcher, mr, {"lane1": lane(11)})
    mr.lanes = {"lane1": {"lane": 0, "scan_time": 200, "material": "PETG",
                          "color": "00FF00", "name": "Green PETG"}}
    events = watcher.poll()
    assert [e["kind"] for e in events] == ["unresolved"]
    assert "PETG" in events[0]["text"]
    assert "No spool was guessed or bound" in events[0]["text"]
    assert applied == []


# --------------------------------------------------------------------- watcher: unknown / offline

def test_unreachable_or_absent_lane_data_changes_nothing(tmp_path):
    mr = FakeMoonraker()
    client = FakeSpoolman([spool(11)])
    watcher, applied = make_watcher(tmp_path, mr, client)
    mr.down = True
    assert watcher.poll() == []
    assert not (tmp_path / "lane_watch.json").exists()  # not even seeded
    mr.down = False
    seed(watcher, mr, {"lane1": lane(11)})
    before = (tmp_path / "lane_watch.json").read_bytes()
    mr.down = True
    assert watcher.poll() == []
    mr.down = False
    mr.lanes = None  # namespace absent: unknown, never empty
    assert watcher.poll() == []
    assert (tmp_path / "lane_watch.json").read_bytes() == before
    assert applied == []


def test_spoolman_silence_defers_a_change_without_adopting_it(tmp_path):
    mr = FakeMoonraker()
    client = FakeSpoolman([spool(11), spool(19, "White", "PLA", "FFFFFF")])
    watcher, applied = make_watcher(tmp_path, mr, client)
    seed(watcher, mr, {"lane1": lane(11)})
    mr.lanes = {"lane1": lane(19, color="FFFFFF", scan_time=200, name="White PLA")}
    client.down = True
    assert watcher.poll() == []
    assert applied == []
    client.down = False
    events = watcher.poll()  # the same change is picked up once Spoolman answers
    assert [e["kind"] for e in events] == ["loaded"]
    assert applied == [(1, 19)]


# --------------------------------------------------------------------- watcher: mid-print + runout

def test_mid_print_change_is_noted_once_and_applied_when_the_print_ends(tmp_path):
    mr = FakeMoonraker(state="printing")
    client = FakeSpoolman([spool(11), spool(19, "White", "PLA", "FFFFFF")])
    watcher, applied = make_watcher(tmp_path, mr, client)
    seed(watcher, mr, {"lane1": lane(11)})
    mr.lanes = {"lane1": lane(19, color="FFFFFF", scan_time=200, name="White PLA")}
    events = watcher.poll()
    assert [e["kind"] for e in events] == ["noted_mid_print"]
    assert "Nothing was rebound mid-print" in events[0]["text"]
    assert applied == []
    watcher.delivered(events[0]["key"])
    assert watcher.poll() == []  # the note is not repeated while printing
    mr.state = "standby"
    events = watcher.poll()
    assert [e["kind"] for e in events] == ["loaded"]
    assert applied == [(1, 19)]


def test_runout_screen_load_feeds_the_runout_recovery(tmp_path):
    mr = FakeMoonraker(state="paused")
    client = FakeSpoolman([spool(18, "Old", "PLA", "111111"),
                           spool(19, "White", "PLA", "FFFFFF")])
    context = {"slot": 2, "cause": "runout", "prior_spool_id": 18, "filename": "benchy.gcode"}
    fed = []
    watcher, applied = make_watcher(
        tmp_path, mr, client,
        runout_context_fn=lambda: context,
        runout_feed_fn=lambda slot, sid, ctx: fed.append((slot, sid, ctx)) or True)
    seed(watcher, mr, {"lane2": lane(18, slot=2, color="111111", name="Old PLA")})
    mr.lanes = {"lane2": lane(19, slot=2, color="FFFFFF", scan_time=200, name="White PLA")}
    events = watcher.poll()
    assert [e["kind"] for e in events] == ["runout_load"]
    assert "runout recovery" in events[0]["text"]
    assert fed == [(2, 19, context)]
    assert applied == []  # the feed binds; the watcher does not also reconcile-apply


def test_runout_screen_load_without_a_feed_hook_is_detected_and_named(tmp_path):
    mr = FakeMoonraker(state="paused")
    client = FakeSpoolman([spool(18, "Old", "PLA", "111111"),
                           spool(19, "White", "PLA", "FFFFFF")])
    context = {"slot": 2, "cause": "runout", "prior_spool_id": 18, "filename": "benchy.gcode"}
    watcher, applied = make_watcher(tmp_path, mr, client, runout_context_fn=lambda: context)
    seed(watcher, mr, {"lane2": lane(18, slot=2, color="111111", name="Old PLA")})
    mr.lanes = {"lane2": lane(19, slot=2, color="FFFFFF", scan_time=200, name="White PLA")}
    events = watcher.poll()
    assert [e["kind"] for e in events] == ["runout_load"]
    assert "No runout feed is wired" in events[0]["text"]
    assert applied == []
    watcher.delivered(events[0]["key"])
    assert watcher.poll() == []  # named once, not on every poll


def test_feed_screen_runout_load_binds_resumes_and_clears_the_question(tmp_path, monkeypatch):
    client = FakeSpoolman([spool(18, "Old", "PLA", "111111", ph_slot="ad5x:2"),
                           spool(19, "White", "PLA", "FFFFFF")])
    mr = FakeMoonraker(lanes={})
    mr.active[0] = 18
    state = tmp_path / "printer_hardware.json"
    state.write_text(json.dumps({
        "spool": {"spoolman_id": 18, "name": "Old PLA", "material": "PLA"},
        "filament_slots": {"2": {"spoolman_id": 18, "name": "Old PLA", "material": "PLA",
                                 "color": "old", "hex": "111111"}}}))
    question = tmp_path / "runout_pause.json"
    question.write_text(json.dumps({"cause": "runout", "slot": 2, "prior_spool_id": 18,
                                    "recorded_at": 1.0, "usage_suspended": True}))
    monkeypatch.setattr(mel, "RUNOUT_PAUSE_STATE", question)
    context = {"slot": 2, "cause": "runout", "prior_spool_id": 18}
    assert mel.feed_screen_runout_load(2, 19, context, client=client, mr=mr, state_path=state) is True
    # No printer is named (the plugin's answer path names none either), so
    # the bind claims the slot for the default roster printer.
    assert spool_sync.spool_ph_slot(client.spools[19]) == (spool_sync._default_printer_id(), 2)
    assert client.spools[18]["archived"] is True  # depleted by the bind itself
    assert mr.active[0] == 19  # usage resumed on the new spool
    assert not question.exists()  # the open question is closed
    # The depleted spool itself can never answer its own runout question.
    assert mel.feed_screen_runout_load(2, 18, context, client=client, mr=mr,
                                       state_path=state) is False


# --------------------------------------------------------------------- converge through reconcile

def test_apply_screen_assignment_writes_ph_slot_and_rebuilds_the_cache(tmp_path):
    client = FakeSpoolman([spool(11, ph_slot="ad5x:1"),
                           spool(19, "White", "PLA", "FFFFFF")])
    mr = FakeMoonraker(lanes={})
    state = tmp_path / "printer_hardware.json"
    state.write_text(json.dumps({
        "spool": {}, "ph_slot_migrated": True,
        "filament_slots": {"1": {"spoolman_id": 11, "name": "Black PLA", "material": "PLA",
                                 "color": "black", "hex": "161616"}}}))
    result = lane_data_sync.apply_screen_assignment(
        PID, 1, 19, client=client, mr=mr, state_path=state,
        pending_path=tmp_path / "pending.json")
    assert result["ok"] is True and result["assignments"] == {1: 19}
    assert spool_sync.spool_ph_slot(client.spools[19]) == (PID, 1)
    assert spool_sync.spool_ph_slot(client.spools[11]) is None
    saved = json.loads(state.read_text())
    assert saved["filament_slots"]["1"]["spoolman_id"] == 19
    # Idempotent: applying the same screen state again changes nothing.
    again = lane_data_sync.apply_screen_assignment(
        PID, 1, 19, client=client, mr=mr, state_path=state,
        pending_path=tmp_path / "pending.json")
    assert again["ok"] is True and again["changed"] is False


def test_apply_screen_unload_clears_the_claim(tmp_path):
    client = FakeSpoolman([spool(11, ph_slot="ad5x:1")])
    mr = FakeMoonraker(lanes={})
    state = tmp_path / "printer_hardware.json"
    state.write_text(json.dumps({
        "spool": {}, "ph_slot_migrated": True,
        "filament_slots": {"1": {"spoolman_id": 11, "name": "Black PLA", "material": "PLA",
                                 "color": "black", "hex": "161616"}}}))
    result = lane_data_sync.apply_screen_assignment(
        PID, 1, None, client=client, mr=mr, state_path=state,
        pending_path=tmp_path / "pending.json")
    assert result["assignments"] == {}
    assert spool_sync.spool_ph_slot(client.spools[11]) is None
    assert json.loads(state.read_text())["filament_slots"] == {}


# --------------------------------------------------------------------- listener wiring

def test_listener_poll_posts_each_lane_event_once(tmp_path, monkeypatch):
    from print_handy import listeners
    monkeypatch.setattr(listeners, "instance_printer_id", lambda: None)
    mr = FakeMoonraker(lanes={"lane1": lane(11)})
    client = FakeSpoolman([spool(11), spool(19, "White", "PLA", "FFFFFF")])
    watcher, _applied = make_watcher(tmp_path, mr, client)
    listener = mel.Listener()
    listener.lane_watcher = watcher
    posted = []
    with mock.patch.object(mel, "post_lane_event",
                           lambda event: posted.append(event["kind"]) or True):
        asyncio.run(listener.poll_lane_data())  # seeds silently
        assert posted == []
        mr.lanes = {"lane1": lane(19, color="FFFFFF", scan_time=200, name="White PLA")}
        asyncio.run(listener.poll_lane_data())
        assert posted == ["loaded"]
        asyncio.run(listener.poll_lane_data())
        assert posted == ["loaded"]  # never twice


def test_lane_event_text_is_the_event_text():
    assert lane_data_sync.lane_event_text({"text": "hello"}) == "hello"
    assert lane_data_sync.lane_event_text({}) == ""
    assert "color" in lane_data_sync.lane_event_text({"text": "the color is locked"})


# ---------------------------------------------------------------- Codex fixes on PR #809

def test_removing_a_lock_reprocesses_the_lane_so_it_can_converge(tmp_path):
    mr = FakeMoonraker()
    client = FakeSpoolman([spool(11), spool(19, "White", "PLA", "FFFFFF")])
    watcher, applied = make_watcher(tmp_path, mr, client)
    seed(watcher, mr, {"lane2": lane(11, slot=2)})
    mr.lanes = {"lane2": lane(19, slot=2, color="FF0000", scan_time=200,
                              locked_color=True, name="White PLA")}
    assert [e["kind"] for e in watcher.poll()] == ["locked"]
    assert applied == []
    # The operator follows the prompt and removes the lock on the screen;
    # spool, material and color are unchanged, only the lock flag moved.
    mr.lanes = {"lane2": lane(19, slot=2, color="FF0000", scan_time=300,
                              locked_color=False, name="White PLA")}
    events = watcher.poll()
    assert applied == [(2, 19)]
    assert "loaded" in [e["kind"] for e in events]


def test_deleted_spool_through_the_production_transport_still_prompts(tmp_path):
    # The production transport wraps a deleted spool's 404 in SpoolError,
    # exactly like an outage: the watcher must confirm through the spool
    # list before calling it missing.
    class TransportSpoolman(FakeSpoolman):
        def __call__(self, method, path, body=None):
            if method == "GET" and path.startswith("/api/v1/spool/") \
                    and int(path.rsplit("/", 1)[1]) not in self.spools:
                raise spool_sync.SpoolError("Spoolman request failed: 404")
            return super().__call__(method, path, body)

    mr = FakeMoonraker()
    client = TransportSpoolman([spool(11)])
    watcher, applied = make_watcher(tmp_path, mr, client)
    seed(watcher, mr, {"lane1": lane(11)})
    mr.lanes = {"lane1": lane(99, scan_time=200)}
    events = watcher.poll()
    assert [e["kind"] for e in events] == ["stale_spool"]
    assert "no longer exists" in events[0]["text"]
    assert applied == []


def test_spoolman_silence_through_the_transport_is_unknown_not_missing(tmp_path):
    class DownSpoolman(FakeSpoolman):
        def __call__(self, method, path, body=None):
            raise spool_sync.SpoolError("Spoolman did not answer")

    mr = FakeMoonraker()
    client = DownSpoolman([spool(11)])
    watcher, applied = make_watcher(tmp_path, mr, client)
    seed(watcher, mr, {"lane1": lane(11)})
    mr.lanes = {"lane1": lane(99, scan_time=200)}
    assert watcher.poll() == []
    assert applied == []


def test_failed_assignment_write_keeps_the_old_claim_and_adopts_nothing(tmp_path):
    class ExtraRefusingSpoolman(FakeSpoolman):
        def __call__(self, method, path, body=None):
            if method == "PATCH" and isinstance(body, dict) and "extra" in body:
                raise OSError("spoolman refused the extra write")
            return super().__call__(method, path, body)

    client = ExtraRefusingSpoolman([
        spool(11, ph_slot="ad5x:1"),
        spool(19, "White", "PLA", "FFFFFF"),
    ])
    state = tmp_path / "printer_hardware.json"
    state.write_text(json.dumps(
        {"spool": {}, "filament_slots": {"1": {"spoolman_id": 11, "name": "Black PLA",
                                               "material": "PLA", "color": "black",
                                               "hex": "161616"}}}))
    mr = FakeMoonraker(lanes={"lane1": lane(19, color="FFFFFF", name="White PLA")})
    try:
        lane_data_sync.apply_screen_assignment(PID, 1, 19, client=client, mr=mr,
                                               state_path=state)
        raised = None
    except spool_sync.SpoolError as exc:
        raised = exc
    assert raised is not None and "did not accept" in str(raised)
    # The previous spool's claim was NOT cleared behind the failed write.
    assert spool_sync.spool_ph_slot(client.spools[11]) == (PID, 1)
    assert spool_sync.spool_ph_slot(client.spools[19]) is None


# ------------------------------------------------- ZMod-authored lanes (#851)

# The exact payload shape ZMod's orca_lanes publishing writes (ZMod wiki,
# AD5X "Filament sync with OrcaSlicer"): keys are 0-based (slot 1 -> lane0),
# the inner lane is a string, and a record carries only lane/material/color/
# name - no spool_id, no scan_time, no helix_* keys. Slot 3 has type "?" on
# the printer, so ZMod removed its key.
ZMOD_LANES = {
    "lane0": {"lane": "0", "material": "PETG", "color": "#161616", "name": "PETG"},
    "lane1": {"lane": "1", "material": "PLA", "color": "#FFFFFF", "name": "PLA"},
    "lane3": {"lane": "3", "material": "ABS", "color": "#FF0000", "name": "ABS"},
}


def test_parse_lane_data_ignores_zmod_authored_records():
    # Read under the screen's 1-based keys, lane1 would hand slot 2's PLA to
    # slot 1 and drop slot 1's PETG entirely: every ZMod lane lands one slot
    # off. ZMod records name no spool, so the watcher must not act on them.
    assert lane_data_sync.parse_lane_data(copy.deepcopy(ZMOD_LANES)) == {}


def test_parse_lane_data_zmod_records_do_not_shadow_screen_records():
    mixed = {"lane2": lane(18, slot=2), **copy.deepcopy(ZMOD_LANES)}
    lanes = lane_data_sync.parse_lane_data(mixed)
    assert sorted(lanes) == [2]
    assert lanes[2]["spool_id"] == 18


def test_parse_lane_data_screen_record_without_spool_id_still_parses():
    # An id-less SCREEN lane (scan_time, no spool_id) is the unresolved case
    # the watcher names in a prompt; only the marker-less ZMod shape is skipped.
    record = {"lane": 0, "scan_time": 200, "material": "PETG",
              "color": "FFFFFF", "name": "White PETG"}
    lanes = lane_data_sync.parse_lane_data({"lane1": record})
    assert sorted(lanes) == [1]
    assert lanes[1]["material"] == "PETG"


def test_zmod_publish_change_and_removal_make_no_watcher_events(tmp_path):
    client = FakeSpoolman([spool(11, ph_slot="ad5x:1")])
    mr = FakeMoonraker(lanes={})
    watcher, applied = make_watcher(tmp_path, mr, client)
    seed(watcher, mr, {})
    mr.lanes = copy.deepcopy(ZMOD_LANES)
    assert watcher.poll() == []
    # ZMod republishes only the changed slot: its slot 2 (key lane1) turns PETG.
    mr.lanes = {**copy.deepcopy(ZMOD_LANES),
                "lane1": {"lane": "1", "material": "PETG", "color": "#0000FF", "name": "PETG"}}
    assert watcher.poll() == []
    # Its slot 2's type becomes "?", so ZMod removes key lane1.
    mr.lanes = {key: value for key, value in copy.deepcopy(ZMOD_LANES).items()
                if key != "lane1"}
    assert watcher.poll() == []
    assert applied == []
