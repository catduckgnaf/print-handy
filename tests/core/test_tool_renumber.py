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
    assert "M104 S215 T0\n" in text  # the heater's T0 is the one heater, never a filament
    assert '; hermes_tool_slots = {"2":{"physical_slot":3,"spoolman_id":21}}' in text
    assert "; zmod_color_data = 2|#161616|PLA+" in text
    assert "; filament_colour = #161616;#161616;#161616\n" in text
    assert "; filament_type = PLA+;PLA+;PLA+\n" in text
    assert "; filament used [g] = 0, 0, 157.58\n" in text  # usage stays with the slot that feeds it


def test_two_tools_swap_and_lists_follow():
    text = "; filament_colour = #FFFFFF;#161616\nT0\nG1 X1\nT1\nM109 S220 T0\n"
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
    assert rest == "M104 S215 T0\nG1 X1\n"
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


def test_used_tools_never_reads_the_heater_tool():
    """#1648 review: M104 ... T0 names the one heater. Tool lines win; without them the
    filament metadata names the tool, and nothing at all gives an empty set."""
    assert tool_renumber.used_tools("M104 S215 T0\nT1\nG1 X1\n") == {1}
    assert tool_renumber.used_tools(HEAD) == {0}
    assert tool_renumber.used_tools("M104 S215 T0\nG1 X1\n") == set()
    assert tool_renumber.used_tools("; zmod_color_data = 2|#161616|PLA\nM104 S215 T0\n") == {2}
    assert tool_renumber.used_tools("; filament used [g] = 0.00, 0, 12.5\nM104 S215 T0\n") == {2}
    # Header and usage disagree: no answer, so relabel refuses rather than guesses.
    assert tool_renumber.used_tools("; zmod_color_data = 0|#161616|PLA\n; filament used [g] = 0, 3.1\n") == set()


def test_single_tool_slice_without_tool_lines_still_relabels():
    """#1648 review: a single-tool slice with no T line and a heater with no tool
    parameter. The tool comes from the headers, and the relabel moves the metadata
    to slot 3 (T2) without inventing a tool line."""
    text = ('; hermes_tool_slots = {"0":{"physical_slot":3}}\n; zmod_color_data = 0|#161616|PLA\n'
            "; filament_colour = #161616\n; filament used [g] = 157.58\nM190 S60\nM104 S215\n"
            "START_PRINT EXTRUDER_TEMP=215 BED_TEMP=60\nG1 X1 E1\n")
    out, identity = tool_renumber.relabel(text, {"0": 3}, tool_renumber.used_tools(text))
    assert identity == {"2": 3}
    assert "; zmod_color_data = 2|#161616|PLA\n" in out
    assert "; filament used [g] = 0, 0, 157.58\n" in out
    assert "M190 S60\nM104 S215\nSTART_PRINT" in out


def test_ad5x_fixture_keeps_the_native_startup_contract():
    """#1648 review: the recorded AD5X Silk PLA slice on slot 2. Relabeling must leave
    ``M104 S210 T0`` so native_startup.valid still holds (``T1`` there is refused)."""
    from pathlib import Path
    from print_handy.printers.flashforge.zmod import native_startup
    fixture = Path(__file__).resolve().parents[1] / "fixtures/printers/ad5x_zmod/silk_pla_slot2_sliced.gcode.txt"
    text = fixture.read_bytes().decode().replace("@BED@", "60").replace("@NOZZLE@", "210")
    assert native_startup.valid(text, 60, 210)
    out, identity = tool_renumber.relabel(text, {"0": 2}, tool_renumber.used_tools(text))
    assert identity == {"1": 2}
    assert native_startup.valid(out, 60, 210)
    assert "; zmod_color_data = 1|#0C6283|Silk PLA" in out


def _zmod_file(tmp_path, body):
    """A G-code as ZMod finalize writes it: an MD5 line ending in CRLF over an LF body."""
    g = tmp_path / "plate_1.gcode"
    g.write_bytes(b"; MD5:" + hashlib.md5(body).hexdigest().encode() + b"\r\n" + body)
    return g


def test_launch_copy_keeps_line_endings_and_a_valid_md5(tmp_path):
    """#1648 review: the upload copy is read and written as bytes. An identity map uploads
    the file itself, byte for byte; a relabel keeps CRLF and LF exactly and a correct MD5."""
    from types import SimpleNamespace
    from print_handy.launch import print_now
    from print_handy.printers.flashforge import zmod
    adapter = SimpleNamespace(native=zmod, slot_count=4)
    body = b'; hermes_tool_slots = {"0":{"physical_slot":1}}\nM104 S215 T0\nT0\nG1 X1\n'
    g = _zmod_file(tmp_path, body)
    original = g.read_bytes()
    path, labels = print_now._slot_labeled_copy(adapter, g, {"0": 1})
    assert labels == {"from": {"0": 1}, "to": {"0": 1}}
    assert path.read_bytes() == original

    g = _zmod_file(tmp_path, body.replace(b'"physical_slot":1', b'"physical_slot":3'))
    path, labels = print_now._slot_labeled_copy(adapter, g, {"0": 3})
    assert labels == {"from": {"0": 3}, "to": {"2": 3}}
    first, _, rest = path.read_bytes().partition(b"\n")
    assert first == b"; MD5:" + hashlib.md5(rest).hexdigest().encode() + b"\r"
    assert rest == b'; hermes_tool_slots = {"2":{"physical_slot":3}}\nM104 S215 T0\nT2\nG1 X1\n'
