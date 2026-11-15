"""OpenCreator follows the printer's own AFC tool map (#1490).

Decided on #1490 (2026-11-15): Print Handy no longer refuses a printer
whose map is not the identity. It reads each lane's map from the lanes
AFC itself lists and works out which toolhead prints each file tool. A
map it cannot resolve to one lane and one toolhead per tool still
fails closed, and an attached ACE unit is refused by name.

The ACE shapes come from a live capture shared on #1490
(tests/fixtures/opencreator/c5toolchanger.json).
"""
import copy
import json
from pathlib import Path

import pytest

from print_handy.launch import gates
from print_handy.printers.flashforge_opencreator import afc

from .test_adapters import adapter_for
from .test_opencreator import KEYS, _ready_status

CAPTURE = Path(__file__).parents[1] / "fixtures" / "opencreator" / "c5toolchanger.json"


def _captured_status():
    def find(node):
        if isinstance(node, dict):
            if "AFC" in node:
                return node
            for value in node.values():
                found = find(value)
                if found:
                    return found
        return None
    return copy.deepcopy(find(json.loads(CAPTURE.read_text(encoding="utf-8"))))


def _swap(status, a, b):
    status[f"AFC_lane extruder{a}"]["map"] = [f"T{b}"]
    status[f"AFC_lane extruder{b}"]["map"] = [f"T{a}"]
    return status


def test_identity_map_resolves_each_tool_to_its_own_head():
    resolved, problems = afc.resolve_tool_map(_ready_status())
    assert problems == []
    assert resolved == {tool: {"lane": f"extruder{tool}", "head": tool} for tool in range(4)}


def test_a_swapped_map_is_followed_not_refused():
    status = _swap(_ready_status(), 0, 2)
    resolved, problems = afc.resolve_tool_map(status)
    assert problems == []
    assert resolved[0] == {"lane": "extruder2", "head": 2}
    assert resolved[2] == {"lane": "extruder0", "head": 0}
    assert afc.describe_tool_map(resolved) == (
        "T0 on toolhead 3, T1 on toolhead 2, T2 on toolhead 1, T3 on toolhead 4")
    for key in KEYS:
        adapter = adapter_for(key)
        assert adapter.readiness_problems({}, status) == []
        assert adapter.tool_head_map(status) == {0: 2, 1: 1, 2: 0, 3: 3}


def test_lanes_are_read_from_the_names_afc_lists_under_either_object_name():
    # A stepper-driven lane answers as "AFC_stepper <name>", and lane
    # names are the printer's own.
    status = _ready_status()
    status["AFC"]["lanes"] = ["lane1", "lane2", "lane3", "lane4"]
    for tool in range(4):
        del status[f"AFC_lane extruder{tool}"]
        prefix = "AFC_stepper" if tool % 2 else "AFC_lane"
        status[f"{prefix} lane{tool + 1}"] = {"map": [f"T{3 - tool}"], "extruder": f"extruder{tool}"}
    resolved, problems = afc.resolve_tool_map(status)
    assert problems == []
    assert {tool: row["head"] for tool, row in resolved.items()} == {0: 3, 1: 2, 2: 1, 3: 0}
    assert resolved[0]["lane"] == "lane4"


@pytest.mark.parametrize("mutate, expected", [
    (lambda s: s["AFC"].pop("lanes"), "AFC lane list is not reporting"),
    (lambda s: s["AFC"].__setitem__("multiple_tool_mapping", True), "multiple tool mapping on"),
    (lambda s: s["AFC_lane extruder1"].__setitem__("map", ["T0"]),
     "T0 is mapped to more than one AFC lane (extruder0, extruder1)"),
    (lambda s: s["AFC_lane extruder1"].__setitem__("map", ["NONE"]),
     "AFC lane extruder1 does not report its tool map"),
    (lambda s: s["AFC_lane extruder1"].__setitem__("map", ["LANE_X"]), "which is not a tool"),
    (lambda s: s["AFC_lane extruder1"].__setitem__("extruder", None),
     "AFC lane extruder1 does not report which toolhead it feeds"),
    (lambda s: s["AFC_lane extruder1"].__setitem__("extruder", "extruder0"),
     "toolhead 1 is mapped to more than one tool (T0, T1)"),
    (lambda s: s["AFC_lane extruder1"].__setitem__("extruder", "extruder9"),
     "AFC lane extruder1 does not report which toolhead it feeds"),
])
def test_an_unresolvable_map_fails_closed_and_names_the_cause(mutate, expected):
    status = _ready_status()
    mutate(status)
    resolved, problems = afc.resolve_tool_map(status)
    assert resolved is None
    assert any(expected in p for p in problems), problems
    adapter = adapter_for(KEYS[1])
    assert any(expected in p for p in adapter.readiness_problems({}, status))
    assert adapter.tool_head_map(status) is None


def test_a_tool_with_no_lane_is_named():
    status = _ready_status()
    status["AFC"]["lanes"] = ["extruder0", "extruder1", "extruder2"]
    resolved, problems = afc.resolve_tool_map(status)
    assert resolved is None
    assert problems == ["T3 is not mapped to any AFC lane"]


def test_live_capture_with_an_ace_unit_is_refused_by_name():
    status = _captured_status()
    # The capture is the real printer: AFC lists lane1 to lane4 and
    # there is no "AFC_lane extruderN" object at all.
    assert afc.lane_names(status) == ["lane1", "lane2", "lane3", "lane4"]
    assert afc.ace_units(status) == ["ACE2 ACE_1"]
    assert afc.lane_object_names(status) == [
        f"{prefix} lane{n}" for n in range(1, 5) for prefix in ("AFC_lane", "AFC_stepper")]
    full = {**_ready_status(), **status}
    for tool in range(4):
        full.pop(f"AFC_lane extruder{tool}", None)
    problems = adapter_for(KEYS[1]).readiness_problems({}, full)
    assert any("an ACE unit is attached (ACE2 ACE_1)" in p and "not supported" in p
               for p in problems), problems
    # Readiness names the lanes the printer has, never the absent extruderN ones.
    assert any("AFC lane lane1 is not reporting" in p for p in problems)
    assert not any("extruder0 is not reporting" in p and "AFC lane" in p for p in problems)


def test_an_ace_unit_stays_refused_even_when_its_lanes_resolve():
    status = {**_ready_status(), **_captured_status()}
    for tool in range(4):
        del status[f"AFC_lane extruder{tool}"]
        status[f"AFC_stepper lane{tool + 1}"] = {"map": [f"T{tool}"], "extruder": f"extruder{tool}"}
    problems = adapter_for(KEYS[1]).readiness_problems({}, status)
    assert [p for p in problems if "ACE" in p]
    assert not any("is not reporting" in p for p in problems)


def test_tool_records_keep_the_toolhead_on_a_remapped_printer():
    records = afc.parse_tool_records({
        "T0": {"extruder_index": 2, "material": "PETG"},
        "T2": {"extruder_index": 0, "material": "PLA"},
        "T1": {"material": "ABS"},                        # no toolhead named: omitted
        "T3": {"extruder_index": True},
    })
    assert records == {
        0: {"extruder_index": 2, "material": "PETG", "tool": 0, "head": 2},
        2: {"extruder_index": 0, "material": "PLA", "tool": 2, "head": 0},
    }


def test_identity_report_still_names_a_remap():
    lanes = afc.parse_lanes(_swap(_ready_status(), 0, 1))
    assert afc.identity_map_problems(lanes) == [
        "AFC lane extruder0 is mapped to T1, not T0",
        "AFC lane extruder1 is mapped to T0, not T1"]


def test_launch_readiness_queries_the_lanes_the_printer_named(monkeypatch):
    status = _swap(_ready_status(), 1, 3)
    lanes = {k: status.pop(k) for k in list(status) if k.startswith("AFC_lane ")}
    asked = []

    def fake_request(url, *a, **kw):
        asked.append(url)
        return {"result": {"status": status if len(asked) == 1 else lanes}}

    monkeypatch.setattr(gates, "request_json", fake_request)
    adapter = adapter_for(KEYS[1])
    gates._verify_readiness_adapter(adapter, {"moonraker_url": "http://printer.invalid"})
    assert len(asked) == 2
    assert "AFC_lane%20extruder0" in asked[1] and "AFC_stepper%20extruder3" in asked[1]
    # Without the lane answers the same printer is refused, never assumed.
    asked.clear()
    lanes.clear()
    with pytest.raises(RuntimeError, match="AFC lane extruder0 is not reporting"):
        gates._verify_readiness_adapter(adapter, {"moonraker_url": "http://printer.invalid"})
