from pathlib import Path

from print_handy.printers.flashforge_opencreator.afc import (
    identity_map_problems,
    parse_tool_records,
)


def test_lane_data_t_keyed_records_use_matching_extruder_index():
    records = parse_tool_records({"T0": {"extruder_index": 0, "material": "PLA"}})
    assert records == {0: {"extruder_index": 0, "material": "PLA", "tool": 0, "head": 0}}


def test_lane_data_lane_keyed_records_resolve_extruder_index():
    records = parse_tool_records({"c5_tool1": {"material": "PETG"}})
    assert records == {1: {"material": "PETG", "tool": 1, "head": 1}}


def test_identity_tool_map_is_accepted():
    lanes = {f"extruder{i}": {"map": f"T{i}"} for i in range(4)}
    assert identity_map_problems(lanes) == []


def test_remapped_tool_map_is_refused_and_names_the_lane():
    lanes = {f"extruder{i}": {"map": f"T{i}"} for i in range(4)}
    lanes["extruder2"]["map"] = "T3"
    problems = identity_map_problems(lanes)
    assert len(problems) == 1
    assert "extruder2" in problems[0]
    assert "T3" in problems[0]


def test_print_handy_builds_no_afc_spool_commands():
    # #235 audit O4: AFC's SET_SPOOL_ID moves the printer's active spool
    # (an accounting write the printer owns under #1536), SET_MATERIAL reads
    # one word and SET_COLOR wants a hex code and cannot clear (AFC
    # extras/AFC_spool.py). Spools are bound at the printer, so no builder
    # for these commands exists to be wired up by mistake.
    from print_handy.printers.flashforge_opencreator import afc
    assert not hasattr(afc, "bind_commands")
    source = Path(afc.__file__).read_text()
    for command in ("SET_SPOOL_ID", "SET_MATERIAL", "SET_COLOR"):
        assert f'"{command} ' not in source and f"'{command} " not in source and f'f"{command}' not in source
