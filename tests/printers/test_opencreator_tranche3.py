"""Tranche 3 tests for the OpenCreator Creator models (#1490): lane_data.

Offline, no printer, network or slicer. The two record shapes pinned
here are transcribed from the firmware's own sources, not captured
from hardware (the live namespace capture is still the arbiter):

- the generic AFC shape, from the OpenCreator fork of Kalico, default
  branch ``something`` at 7ce49713: ``klippy/extras/AFC_lane.py``
  (``send_lane_data``) through ``AFC_utils.py`` posts one record per
  mapped tool command under key ``T<n>`` with the field set used in
  ``afc_record`` below;
- the filament-sync shape, from the same fork's ``something-bleeding``
  branch at 1cc8f589: ``klippy/extras/creator5_filament_sync.py``
  posts ``{**record, "lane": "<n>"}`` under key ``c5_tool<n>``, with
  the record fields used in ``sync_record`` below (the same contract
  the ``ace-pro`` branch's tests pin).
"""

from unittest import mock

import pytest

from print_handy import printers
from print_handy.printers.flashforge_opencreator import lane_data
from print_handy.runtime import spool_sync

from .test_adapters import adapter_for

KEYS = ("flashforge_opencreator.creator5", "flashforge_opencreator.creator5_pro")


def afc_record(material="PLA", color="#FF0000", spool_id=None, **overrides):
    """One generic-AFC lane_data record (key T<n>), field set as published."""
    record = {"color": color, "material": material, "vendor_name": "Generic",
              "name": "Red PLA", "bed_temp": 60, "nozzle_temp": 220,
              "scan_time": "", "td": "", "lane": "0", "extruder_index": 0,
              "spool_id": spool_id, "weight": 812.5, "initial_weight": 1000.0}
    record.update(overrides)
    return record


def sync_record(material="PLA", color="#FF0000", spool_id=42, **overrides):
    """One creator5_filament_sync lane_data record (key c5_tool<n>)."""
    record = {"material": material, "color": color, "nozzle_temp": 220,
              "bed_temp": 60, "spool_id": spool_id, "lane": "0"}
    record.update(overrides)
    return record


# ------------------------------------------------------------ one shape each

def test_afc_shape_parses_normalized_and_labeled():
    lanes = lane_data.parse_tool_lane_data({"T0": afc_record(), "T2": afc_record(
        material="PETG", color="#00FF00", lane="2", extruder_index=2)})
    assert sorted(lanes) == [0, 2]
    tool0 = lanes[0]
    assert tool0["source"] == "afc"
    assert tool0["conflict"] is None
    assert (tool0["material"], tool0["color"], tool0["spool_id"]) == ("PLA", "#FF0000", None)
    assert (tool0["nozzle_temp"], tool0["bed_temp"]) == (220.0, 60.0)
    assert (tool0["name"], tool0["vendor_name"], tool0["weight"]) == ("Red PLA", "Generic", 812.5)
    assert lanes[2]["material"] == "PETG"


def test_filament_sync_shape_parses_normalized_and_labeled():
    lanes = lane_data.parse_tool_lane_data({"c5_tool1": sync_record(lane="1")})
    assert sorted(lanes) == [1]
    tool1 = lanes[1]
    assert tool1["source"] == "filament_sync"
    assert tool1["conflict"] is None
    assert (tool1["material"], tool1["color"], tool1["spool_id"]) == ("PLA", "#FF0000", 42)
    assert (tool1["nozzle_temp"], tool1["bed_temp"]) == (220.0, 60.0)
    # The sync record carries no name, vendor or weight: absent, not invented.
    assert (tool1["name"], tool1["vendor_name"], tool1["weight"]) == ("", "", None)


def test_a_cleared_afc_record_is_an_empty_record_not_a_missing_one():
    cleared = afc_record(material="", color="", name="", vendor_name="",
                         bed_temp="", nozzle_temp="", weight=None)
    lanes = lane_data.parse_tool_lane_data({"T0": cleared})
    assert lanes[0]["material"] == "" and lanes[0]["spool_id"] is None
    assert lanes[0]["source"] == "afc" and lanes[0]["conflict"] is None


# ------------------------------------------------------------ both shapes

def test_both_shapes_agreeing_merge_and_carry_both_labels():
    lanes = lane_data.parse_tool_lane_data(
        {"T1": afc_record(lane="1", extruder_index=1),
         "c5_tool1": sync_record(lane="1")})
    tool1 = lanes[1]
    assert tool1["conflict"] is None
    assert tool1["source"] == "afc+filament_sync"
    # The sync record is the Spoolman-linked one: its spool id fills the
    # AFC record's None. The AFC record's name and weight have no sync
    # counterpart and survive.
    assert tool1["spool_id"] == 42
    assert (tool1["name"], tool1["weight"]) == ("Red PLA", 812.5)


def test_both_shapes_family_variant_materials_agree():
    # PLA from one publisher and PLA+ from the other are one claim (the
    # slicing family), the lane_data watcher's precedent: no conflict.
    lanes = lane_data.parse_tool_lane_data(
        {"T0": afc_record(material="PLA"), "c5_tool0": sync_record(material="PLA+")})
    assert lanes[0]["conflict"] is None


def test_both_shapes_disagreeing_on_material_conflict_and_claim_nothing():
    lanes = lane_data.parse_tool_lane_data(
        {"T0": afc_record(material="PLA"), "c5_tool0": sync_record(material="PETG")})
    tool0 = lanes[0]
    assert tool0["source"] == "afc+filament_sync"
    assert tool0["conflict"]["afc"]["material"] == "PLA"
    assert tool0["conflict"]["filament_sync"]["material"] == "PETG"
    # Fail closed: no claim fields survive for a surface to act on.
    assert (tool0["material"], tool0["color"], tool0["spool_id"]) == ("", "", None)
    assert tool0["nozzle_temp"] is None and tool0["bed_temp"] is None


def test_both_shapes_disagreeing_on_color_conflict():
    lanes = lane_data.parse_tool_lane_data(
        {"T2": afc_record(color="#FF0000", lane="2", extruder_index=2),
         "c5_tool2": sync_record(color="#0000FF", lane="2")})
    assert lanes[2]["conflict"] is not None
    assert lanes[2]["conflict"]["afc"]["color"] == "#FF0000"
    assert lanes[2]["conflict"]["filament_sync"]["color"] == "#0000FF"


def test_a_cleared_record_beside_a_loaded_one_is_a_conflict():
    cleared = afc_record(material="", color="", name="", vendor_name="",
                         bed_temp="", nozzle_temp="", weight=None, lane="3",
                         extruder_index=3)
    lanes = lane_data.parse_tool_lane_data(
        {"T3": cleared, "c5_tool3": sync_record(lane="3")})
    assert lanes[3]["conflict"] is not None
    assert lanes[3]["spool_id"] is None


# ------------------------------------------------------------ namespace hygiene

def test_unknown_keys_are_ignored_and_never_error():
    namespace = {
        "T0": afc_record(),
        # The IFS screen shape is another watcher's namespace use.
        "lane1": {"spool_id": 9, "lane": 0, "material": "PLA", "color": "FFFFFF"},
        "seated": 1,
        "T9": afc_record(),            # outside this model's tool range
        "c5_tool4": sync_record(),     # tool index 4 on a four-tool model
        "T1": "not a record",
        "notes": {"anything": True},
    }
    lanes = lane_data.parse_tool_lane_data(namespace)
    assert sorted(lanes) == [0]


def test_a_non_mapping_namespace_is_unknown_never_empty():
    assert lane_data.parse_tool_lane_data(None) is None
    assert lane_data.parse_tool_lane_data("lane_data") is None
    assert lane_data.parse_tool_lane_data({}) == {}
    assert lane_data.parse_tool_lane_data({"seated": 1}) == {}


def test_attribution_is_by_key_number_not_the_inner_lane_field():
    # Both publishers derive the inner lane field from the key, so the
    # key is the authority: a record whose inner field disagrees (or is
    # absent) still lands on the tool its key names.
    record = sync_record(lane="0")
    lanes = lane_data.parse_tool_lane_data({"c5_tool2": record})
    assert sorted(lanes) == [2]
    bare = afc_record()
    del bare["lane"]
    assert sorted(lane_data.parse_tool_lane_data({"T1": bare})) == [1]


@pytest.mark.parametrize("key", KEYS)
def test_adapter_reads_tool_lane_data_for_both_models(key):
    adapter = adapter_for(key)
    namespace = {"T0": afc_record(), "c5_tool3": sync_record(lane="3")}
    lanes = adapter.tool_lane_data(namespace)
    assert lanes[0]["source"] == "afc"
    assert lanes[3]["source"] == "filament_sync"
    assert adapter.tool_lane_data(None) is None


# ------------------------------------------------------------ spool writes

class FakeMoonraker:
    """The lane_data subset of Moonraker's database API, with a call log."""

    def __init__(self, lanes=None):
        self.lanes = dict(lanes or {})
        self.calls = []

    def __call__(self, method, path, payload):
        self.calls.append((method, path, payload))
        if method == "GET":
            return {"result": {"value": dict(self.lanes)}}
        if payload and payload.get("namespace") == spool_sync.LANE_DATA_NAMESPACE:
            self.lanes[payload["key"]] = payload["value"]
        return {"result": "ok"}

    def database_posts(self):
        return [call for call in self.calls
                if call[0] == "POST" and call[1] == "/server/database/item"]


SPOOL = {"id": 7, "filament": {"name": "Coffee", "material": "PLA+",
                               "color_hex": "6F4E37", "vendor": {"id": 3}}}


@pytest.mark.parametrize("key", KEYS)
def test_opencreator_spool_writes_are_refused_by_name(key, monkeypatch):
    adapter = adapter_for(key)
    monkeypatch.setattr(spool_sync.printers, "current", lambda: adapter)
    mr = FakeMoonraker()
    # A bind-shaped write and a clear-shaped write alike: neither key
    # shape (T<n> or c5_tool<n>) is written, and the outcome names why.
    assert spool_sync.write_lane_side(1, SPOOL, mr=mr) == adapter.lane_data_write_refusal
    assert spool_sync.write_lane_side(1, None, mr=mr) == adapter.lane_data_write_refusal
    assert "not written" in adapter.lane_data_write_refusal
    assert mr.database_posts() == []
    assert mr.lanes == {}


def test_zmod_creator_spool_writes_stay_the_family_skip(monkeypatch):
    # Unchanged by tranche 3: the ZMod Creator's namespace is the known
    # T0..T3 shape (#1304), so its skip stays the generic family outcome.
    creator = adapter_for("flashforge.creator5")
    assert creator.multi_tool is True
    assert getattr(creator, "lane_data_write_refusal", None) is None
    monkeypatch.setattr(spool_sync.printers, "current", lambda: creator)
    mr = FakeMoonraker()
    assert spool_sync.write_lane_side(2, SPOOL, mr=mr) == "unsupported"
    assert mr.database_posts() == []


def test_ifs_family_spool_writes_stay_written(monkeypatch):
    # Unchanged by tranche 3: a non-multi-tool family keeps the #791
    # record write, under the IFS laneN key.
    monkeypatch.setattr(spool_sync.printers, "current",
                        lambda: mock.Mock(multi_slot=False, multi_tool=False))
    mr = FakeMoonraker()
    assert spool_sync.write_lane_side(2, SPOOL, mr=mr) == "written"
    assert mr.lanes["lane2"]["spool_id"] == 7 and mr.lanes["lane2"]["lane"] == 1
