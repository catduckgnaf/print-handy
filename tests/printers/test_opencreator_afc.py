from print_handy.printers.flashforge_opencreator.afc import (
    bind_commands,
    identity_map_problems,
    parse_tool_records,
)


def test_lane_data_t_keyed_records_use_matching_extruder_index():
    records = parse_tool_records({"T0": {"extruder_index": 0, "material": "PLA"}})
    assert records == {0: {"extruder_index": 0, "material": "PLA", "tool": 0}}


def test_lane_data_lane_keyed_records_resolve_extruder_index():
    records = parse_tool_records({"c5_tool1": {"material": "PETG"}})
    assert records == {1: {"material": "PETG", "tool": 1}}


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


def test_bind_commands_are_built_only_for_idle_printers():
    assert bind_commands("extruder0", {"material": "PLA"},
                         spoolman_enabled=False, printer_idle=False) == []
    assert bind_commands("extruder0", {"material": "PLA"},
                         spoolman_enabled=False, printer_idle=True) == [
        "SET_MATERIAL LANE=extruder0 MATERIAL=PLA",
        "SET_COLOR LANE=extruder0 COLOR=",
    ]
