"""The lazy START_PRINT view (#1779) answers exactly what the splitlines() view does."""
import random

import pytest

from print_handy.printers.flashforge.zmod import native_startup as ns

PIECES = ["G1 X1 Y2", "; c", "M190 S60", "M104 S220 T0", "START_PRINT BED_TEMP=60 EXTRUDER_TEMP=220 ; x",
          "  START_PRINT  BED_TEMP=1", "; START_PRINT in a comment", "M73 P0 R3", "M106 S0", "", "T1",
          "X START_PRINT Y", "START_PRINTER"]
SEPS = ["\n", "\n", "\n", "\r\n", "\r", "\u2028", "\x0c"]


def _eager(text):
    view = ns._Startup.__new__(ns._Startup)
    view.lines = text.splitlines()
    hits = [i for i, line in enumerate(view.lines)
            if 'START_PRINT' in line and line.partition(';')[0].strip().startswith('START_PRINT ')]
    view.starts = len(hits)
    if view.starts == 1:
        view._line = hits[0]
        view.before = list(ns._command_lines(view.lines[:view._line]))
        view.start = view.lines[view._line].partition(';')[0].strip()
    view._text = None
    return view


def _text(rng, seps):
    return "".join(rng.choice(PIECES) + rng.choice(seps) for _ in range(rng.randint(0, 25)))


@pytest.mark.parametrize("seed", range(100))
@pytest.mark.parametrize("seps", [SEPS[:3], SEPS[:4], SEPS])
def test_lazy_view_equals_eager(seed, seps):
    text = _text(random.Random(seed), seps)
    lazy, eager = ns._Startup(text), _eager(text)
    assert lazy.starts == eager.starts
    if eager.starts == 1:
        assert lazy.before == eager.before and lazy.start == eager.start
        assert list(lazy.after()) == list(eager.after())
        assert lazy.after(2) == eager.after(2)


def test_no_trailing_newline_and_empty():
    for text in ("", "START_PRINT BED_TEMP=1 EXTRUDER_TEMP=2", "M1\nSTART_PRINT a\nM2"):
        assert ns._Startup(text).starts == _eager(text).starts


def _used_tools_lines(text):
    from print_handy.printers.flashforge.zmod import multicolor_contract as mc
    tools, heater = set(), set()
    for line in text.splitlines():
        head = line[:1]
        if head in mc._TOOL_CANDIDATES and (m := mc.TOOL_LINE.fullmatch(line.strip())):
            tools.add(int(m.group(1)))
        if not tools and head in mc._HEATER_CANDIDATES and (m := mc.HEATER_TOOL_LINE.fullmatch(line.strip())):
            heater.add(int(m.group(1)))
    return tools or heater


TOOL_PIECES = PIECES + ["T0", "  T2 ; swap", "\tT3", "t4", "M104 S200 T1", " M109 S210 T2 ; w", "M104 S0", "X M104 T5",
                        "M104 T6 extra", "T", "TT1", "T1x", "\x1fT7", "M109 S1 T8;c"]


@pytest.mark.parametrize("seed", range(120))
@pytest.mark.parametrize("seps", [SEPS[:1], SEPS[:2], SEPS[:3], SEPS])
def test_used_tools_equals_the_line_pass(seed, seps):
    from print_handy.printers.flashforge.zmod import multicolor_contract as mc
    rng = random.Random(seed)
    text = "".join(rng.choice(TOOL_PIECES) + rng.choice(seps) for _ in range(rng.randint(0, 25)))
    if rng.random() < 0.3:
        text += "é"
    assert mc.used_tools(text) == _used_tools_lines(text)


def _finalize_lines(text, metadata):
    from print_handy.gcode import tool_map
    lines = [line for line in text.splitlines(keepends=True)
             if '_' not in line
             or not (line.strip().lower().startswith('; zmod_color_data =') or tool_map.is_map_line(line))]
    index = next((i for i, line in enumerate(lines) if line.strip().lower().startswith('; header_block_end')), 0)
    lines[index:index] = metadata
    return ''.join(lines)


HEADER_PIECES = PIECES + ["; HEADER_BLOCK_END", "  ; header_block_end", "; header_block_end x", "x; header_block_end",
                          "; zmod_color_data = 0|#fff|PLA", "; hermes_tool_slots = {}", ";header_block_end", "é"]


@pytest.mark.parametrize("seed", range(150))
@pytest.mark.parametrize("seps", [SEPS[:1], SEPS[:2], SEPS[:4], SEPS])
def test_metadata_insertion_equals_the_line_pass(seed, seps):
    from print_handy.printers.flashforge.zmod import multicolor_contract as mc
    rng = random.Random(seed)
    text = "".join(rng.choice(HEADER_PIECES) + rng.choice(seps) for _ in range(rng.randint(0, 25)))
    meta = ["; zmod_color_data = 0|#fff|PLA\r\n"]
    fast = mc._insert_metadata_untouched(text, meta)
    assert fast is None or fast == _finalize_lines(text, meta)
