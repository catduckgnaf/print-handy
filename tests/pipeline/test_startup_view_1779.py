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


@pytest.mark.parametrize("seed", range(400))
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
