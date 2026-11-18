"""#1648: relabeling a mapped ZMod G-code so tool numbers are the IFS slots."""
import hashlib

from print_handy.core import tool_renumber

HEAD = ('; hermes_tool_slots = {"0":{"physical_slot":3,"spoolman_id":21}}\n'
        '; zmod_color_data = 0|#161616|PLA+\n'
        '; filament_colour = #161616\n; filament_type = PLA+\n; filament used [g] = 157.58\n'
        'M104 S215 T0\nSTART_PRINT\nG1 X1 E1\n')


def test_single_tool_on_slot_three_becomes_t2():
    text, identity = tool_renumber.relabel(HEAD, {"0": {"physical_slot": 3, "spoolman_id": 21}}, {0})
    assert identity == {"2": 3}
    assert "M104 S215 T2\n" in text and "T0" not in text
    assert '; hermes_tool_slots = {"2":{"physical_slot":3,"spoolman_id":21}}' in text
    assert "; zmod_color_data = 2|#161616|PLA+" in text
    assert "; filament_colour = #161616;#161616;#161616\n" in text
    assert "; filament_type = PLA+;PLA+;PLA+\n" in text
    assert "; filament used [g] = 0, 0, 157.58\n" in text  # usage stays with the slot that feeds it


def test_two_tools_swap_and_lists_follow():
    text = "; filament_colour = #FFFFFF;#161616\nT0\nG1 X1\nT1\nM109 S220 T1\n"
    out, identity = tool_renumber.relabel(text, {"0": 3, "1": 1}, {0, 1})
    assert identity == {"0": 1, "2": 3}
    assert out.splitlines()[1:] == ["T2", "G1 X1", "T0", "M109 S220 T0"]
    # Old T0 (#FFFFFF) is slot 3 -> T2, old T1 (#161616) is slot 1 -> T0; the unused T1 takes the filler.
    assert out.startswith("; filament_colour = #161616;#FFFFFF;#FFFFFF")


def test_identity_map_is_returned_unchanged():
    text = "T0\nT1\n"
    assert tool_renumber.relabel(text, {"0": 1, "1": 2}, {0, 1}) == (text, {"0": 1, "1": 2})


def test_unsafe_maps_are_not_relabeled():
    assert tool_renumber.relabel("T0\nT1\n", {"0": 2, "1": 2}, {0, 1}) == (None, None)  # shared slot
    assert tool_renumber.relabel("T0\n", {"0": 5}, {0}) == (None, None)  # beyond the IFS
    assert tool_renumber.relabel("T0\nT1\n", {"0": 3}, {0, 1}) == (None, None)  # unmapped tool


def test_zmod_md5_header_is_recomputed():
    body = "M104 S215 T0\nG1 X1\n"
    text = "; MD5:" + hashlib.md5(body.encode()).hexdigest() + "\r\n" + body
    out, _ = tool_renumber.relabel(text, {"0": 3}, {0})
    first, _, rest = out.partition("\n")
    assert rest == "M104 S215 T2\nG1 X1\n"
    assert first == "; MD5:" + hashlib.md5(rest.encode()).hexdigest() + "\r"


def test_quoted_material_names_and_usage_separators():
    text = '; filament_type = "Silk PLA";PLA\n; filament used [g] = 1.50, 2.25\nT0\nT1\n'
    out, _ = tool_renumber.relabel(text, {"0": 2, "1": 1}, {0, 1})
    assert '; filament_type = PLA;"Silk PLA"\n' in out
    assert "; filament used [g] = 2.25, 1.50\n" in out


def test_non_tool_t_words_are_left_alone():
    text = "TIMELAPSE_TAKE_FRAME\nT0 ; tool\n;TYPE:Custom\n"
    out, _ = tool_renumber.relabel(text, {"0": 2}, {0})
    assert out == "TIMELAPSE_TAKE_FRAME\nT1 ; tool\n;TYPE:Custom\n"
