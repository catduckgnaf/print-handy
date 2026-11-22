"""The one tool map header module (#1817 W1): spelling, parse, format, and the hard cut."""
import hashlib

import pytest

from print_handy.core.gcode_lines import GcodeLines
from print_handy.gcode import tool_map
from print_handy.printers.flashforge.zmod import multicolor_contract as contract

OLD = '; hermes_tool_slots = {"0":{"physical_slot":2,"spoolman_id":17}}\n'


def test_format_writes_the_dict_form_compact_and_sorted():
    line = tool_map.format_line({"1": tool_map.entry(3), "0": tool_map.entry(2, 17)})
    assert line == '; print_handy_tool_slots = {"0":{"physical_slot":2,"spoolman_id":17},"1":{"physical_slot":3}}'


def test_format_refuses_a_plain_slot_value():
    with pytest.raises(ValueError):
        tool_map.format_line({"0": 2})


def test_parse_round_trips_format():
    mapping = {"0": tool_map.entry(2, 17), "2": tool_map.entry(4)}
    text = "; HEADER_BLOCK_START\n" + tool_map.format_line(mapping) + "\r\nG90\n"
    assert tool_map.parse(text) == mapping
    lines = GcodeLines(text, (tool_map.ANCHOR,))
    assert tool_map.parse(text, lines) == mapping
    assert lines.scans == 1


def test_parse_without_a_map_is_none():
    assert tool_map.parse("; filament_type = PLA\nG90\n") is None
    assert tool_map.parse(None) is None


@pytest.mark.parametrize("payload", ['{"0": 2}', "[1, 2]", "not json", '{"0": {"spoolman_id": 9}}'])
def test_parse_refuses_unreadable_maps_including_the_plain_int_form(payload):
    with pytest.raises(tool_map.ToolMapError, match="malformed tool mapping metadata"):
        tool_map.parse(tool_map.PREFIX + payload + "\n")


def test_parse_refuses_a_duplicated_map():
    line = tool_map.format_line({"0": tool_map.entry(1)}) + "\n"
    with pytest.raises(tool_map.ToolMapError, match="duplicate tool mapping metadata"):
        tool_map.parse(line + line)


def test_a_file_with_only_the_pre_2_4_0_header_is_refused():
    with pytest.raises(tool_map.StaleToolMap, match="sliced by an older Print Handy; reslice"):
        tool_map.parse(OLD + "G90\n")
    with pytest.raises(tool_map.StaleToolMap):
        tool_map.parse_line(OLD.rstrip("\n"))


def test_the_old_name_inside_another_line_is_not_a_map():
    assert tool_map.parse("; note: hermes_tool_slots = {} was the old name\n") is None


def test_launch_refuses_an_archived_file_with_the_old_header():
    with pytest.raises(RuntimeError, match="older Print Handy; reslice"):
        contract.embedded_mapping(OLD + "T0\n", {"filament_slots": {}})


def test_finalize_strips_the_old_header_instead_of_keeping_it_beside_the_new_one(tmp_path):
    state = {"filament_slots": {"1": {"material": "PLA", "physical_color_verified": True,
                                      "color": "#FF0000", "spoolman_id": 5}}}
    path = tmp_path / "old.gcode"
    path.write_text("; HEADER_BLOCK_START\n" + OLD + "; HEADER_BLOCK_END\n"
                    "; filament_type = PLA\nM104 S210 T0\nT0\nG1 X1\n")
    contract.finalize(path, None, state, {"0": 1})
    raw = path.read_bytes()
    first, _, payload = raw.partition(b"\n")
    assert first.rstrip(b"\r") == b"; MD5:" + hashlib.md5(payload).hexdigest().encode()
    text = payload.decode()
    assert "hermes_tool_slots" not in text
    assert tool_map.parse(text) == {"0": tool_map.entry(1, 5)}


def test_is_map_line_matches_both_spellings_only_at_line_start():
    assert tool_map.is_map_line(tool_map.PREFIX + "{}")
    assert tool_map.is_map_line(OLD)
    assert not tool_map.is_map_line(" " + OLD)
