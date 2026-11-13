"""The post-slice bed check, tool scan and time-header check (#1392).

The Python bed loop (print_handy.gcode.bed_scan._first_off_bed_python) is the
contract; the Rust port in the bounds extension must agree with it on every
ASCII input. tool_numbers and has_time_header must agree with the whole-file
regexes they replaced. When PRINT_HANDY_BOUNDS_REQUIRE_NATIVE is set (the CI
legs that build the extension), a missing extension fails instead of skipping.
"""

import os
import random
import re
import sys
import types
from pathlib import Path

import pytest

from print_handy.core import slice_guard
from print_handy.gcode import bed_scan

ROOT = Path(__file__).resolve().parents[2]
BEDS = ((220.0, 220.0), (100.0, 100.0), (0.0, 0.0), (300.5, 250.25))
OLD_TOOLS = re.compile(r"(?im)^\s*T(\d+)\s*(?:;.*)?$")
ORACLE = bed_scan._first_off_bed_python

EDGE_CASES = [
    "",
    "G1 X10 Y20 E1\nG0 X220 Y0\n",
    "G1 X10 Y20\nG1 Y-1\nG1 X999\n",
    "G10 X-5\nG92 X-5\nM104 X-5\n; G1 X-5\nG1X-5\nG1 E1X-5\nG1 X5 ; X-5\n",
    " \tg2 x-5\n",
    "G1 X5.Y-5\nG1 X .5 Y5.\nG1 X-.5\nG1 X- 5 Y-\n",
    "G1 X-5 X5\nG1 Y5 Y-5\n",
    "M117 a\rG1 X-1\n", "M117 a\x0bG1 X-1\n", "M117 a\x0cG1 Y-1\n",
    "M117 a\x1cG1 X-1\n", "M117 a\x1dG1 X-1\n", "M117 a\x1eG1 X-1\n",
    "G1 X1\x1fX-1\n", "\x1fG1 X-1\n", "G1 X1\r\nG1 X300\r\n",
    "G1 X1e5\nG1 X1.5.5 Y2..3\nG1 X00012 Y-0\n",
    "G1_X-5\nG1-X5\nG1 _X-5\nG1 aX-5\nG1 .X-5\n",
    "G0\nG1\nG1 ;\nG1 X\n",
    "G1 X220 Y220\nG1 X220.0000001\n",
]


def _fuzz_cases(count=400, seed=1392):
    rng = random.Random(seed)
    tokens = ["G0", "G1", "g1", "G2", "G3", "G4", "G10", "G01", "M104", "T0", " ", "  ", "\t", "\x1f",
              "X", "x", "Y", "y", "Z", "E", "F", "-", ".", "0", "5", "12", "220", "999", "1.5", "-3",
              ";", ";c", "_", "a", "\n", "\n", "\n", "\r", "\r\n", "\x0b", "\x0c", "\x1c", "\x1e"]
    cases = []
    for _ in range(count):
        cases.append("".join(rng.choice(tokens) for _ in range(rng.randint(1, 60))))
    return cases


def _fixtures():
    return [p.read_text(errors="replace") for p in sorted((ROOT / "tests").rglob("*.gcode"))]


def _native():
    try:
        from print_handy.core import _bounds_native as module
    except ImportError:
        if os.environ.get("PRINT_HANDY_BOUNDS_REQUIRE_NATIVE"):
            raise
        pytest.skip("native bounds extension not built")
    return module


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.delenv("PRINT_HANDY_BOUNDS_IMPL", raising=False)
    bed_scan._reset_native_cache()
    yield
    bed_scan._reset_native_cache()


def test_the_native_scan_matches_the_python_contract_on_every_case():
    module = _native()
    assert module.BED_SCAN_VERSION == bed_scan.BED_SCAN_VERSION
    texts = [t for t in EDGE_CASES + _fuzz_cases() + _fixtures() if t.isascii()]
    assert len(texts) > 400
    for bed in BEDS:
        for text in texts:
            got = module.first_off_bed(text, *bed)
            assert (None if got is None else tuple(got)) == bed_scan._first_off_bed_python(text, bed), repr(text[:120])


def test_the_dispatcher_uses_the_native_scan_when_it_is_built(monkeypatch):
    _native()
    calls = []
    monkeypatch.setattr(bed_scan, "_first_off_bed_python", lambda *a: calls.append(a))
    assert bed_scan.first_off_bed("G1 X10 Y20\nG1 Y-1\n", (220.0, 220.0)) == (10.0, -1.0)
    assert calls == [], "an ASCII G-code took the Python loop although the extension is built"


def _fake(monkeypatch, **attrs):
    module = types.ModuleType("print_handy.core._bounds_native")
    module.BED_SCAN_VERSION = bed_scan.BED_SCAN_VERSION
    module.first_off_bed = lambda text, bx, by: ORACLE(text, (bx, by))
    for name, value in attrs.items():
        setattr(module, name, value)
    monkeypatch.setitem(sys.modules, "print_handy.core._bounds_native", module)
    import print_handy.core as core
    monkeypatch.setattr(core, "_bounds_native", module, raising=False)
    return module


def _counting_python(monkeypatch):
    calls = []
    real = bed_scan._first_off_bed_python

    def counted(text, bed):
        calls.append(text)
        return real(text, bed)
    monkeypatch.setattr(bed_scan, "_first_off_bed_python", counted)
    return calls


def test_a_verified_fake_module_is_used(monkeypatch):
    _fake(monkeypatch)
    calls = _counting_python(monkeypatch)
    assert bed_scan.first_off_bed("G1 X1 Y-2\n", (220, 220)) == (1.0, -2.0)
    assert calls == []


def test_a_version_mismatch_falls_back(monkeypatch):
    _fake(monkeypatch, BED_SCAN_VERSION=bed_scan.BED_SCAN_VERSION + 1)
    calls = _counting_python(monkeypatch)
    assert bed_scan.first_off_bed("G1 X-1\n", (220, 220)) == (-1.0, 0.0)
    assert calls


def test_a_self_check_mismatch_falls_back(monkeypatch):
    _fake(monkeypatch, first_off_bed=lambda text, bx, by: None)
    calls = _counting_python(monkeypatch)
    assert bed_scan.first_off_bed("G1 X-1\n", (220, 220)) == (-1.0, 0.0)
    assert calls


def test_a_native_error_falls_back_and_sticks(monkeypatch):
    module = _fake(monkeypatch)
    calls = _counting_python(monkeypatch)
    assert bed_scan.first_off_bed("G1 X1\n", (220, 220)) is None  # verified and used
    seen = len(calls)

    def boom(text, bx, by):
        raise ValueError("native defect")
    module.first_off_bed = boom
    assert bed_scan.first_off_bed("G1 X-1\n", (220, 220)) == (-1.0, 0.0)
    module.first_off_bed = lambda text, bx, by: (0.0, 0.0)
    assert bed_scan.first_off_bed("G1 X-3\n", (220, 220)) == (-3.0, 0.0), "the native path stays off"
    assert len(calls) == seen + 2


def test_non_ascii_text_never_reaches_the_native_scan(monkeypatch):
    seen = []
    _fake(monkeypatch, first_off_bed=lambda text, bx, by: seen.append(text)
          or ORACLE(text, (bx, by)))
    bed_scan.first_off_bed("G1 X1\n", (220, 220))
    before = len(seen)
    assert bed_scan.first_off_bed("G1 X-1 ; café\n", (220, 220)) == (-1.0, 0.0)
    assert bed_scan.first_off_bed("G1 X-١\n", (220, 220)) == (-1.0, 0.0)  # a Unicode digit
    assert len(seen) == before


def test_the_python_setting_never_touches_native(monkeypatch):
    def refuse(*a):
        raise AssertionError("native used")
    _fake(monkeypatch, first_off_bed=refuse)
    monkeypatch.setenv("PRINT_HANDY_BOUNDS_IMPL", "python")
    assert bed_scan.first_off_bed("G1 X-1\n", (220, 220)) == (-1.0, 0.0)


def test_the_native_setting_raises_instead_of_falling_back(monkeypatch):
    _fake(monkeypatch, BED_SCAN_VERSION=0)
    monkeypatch.setenv("PRINT_HANDY_BOUNDS_IMPL", "native")
    with pytest.raises(RuntimeError, match="native bed scan required"):
        bed_scan.first_off_bed("G1 X1\n", (220, 220))
    with pytest.raises(RuntimeError, match="native bed scan required"):
        bed_scan.first_off_bed("G1 X1\n", (220, 220))


def test_keyboard_interrupt_is_never_swallowed(monkeypatch):
    module = _fake(monkeypatch)
    bed_scan.first_off_bed("G1 X1\n", (220, 220))

    def interrupt(*a):
        raise KeyboardInterrupt
    module.first_off_bed = interrupt
    with pytest.raises(KeyboardInterrupt):
        bed_scan.first_off_bed("G1 X1\n", (220, 220))


def test_tool_numbers_match_the_whole_file_regex():
    texts = EDGE_CASES + _fuzz_cases() + _fixtures() + [
        "T1\n  T2\n\tt3 ; c\nT4 x\n;T5\n T6\x0b\nG1 T7\nT8\r\nT9\r\n\n\nT10\n;\nT11\n  \n   ;c\nT12",
        "　T13\nT١\n", "T1\x0bT2\n", "T3\rT4\n", "x\n\n  \n T5 \n",
    ]
    for text in texts:
        assert slice_guard.tool_numbers(text) == {int(n) for n in OLD_TOOLS.findall(text)}, repr(text[:120])


def test_has_time_header_matches_the_whole_file_search():
    filler = "G1 X1 Y1 E.1\n" * 40000  # longer than the head and tail windows together
    headers = ["; estimated printing time (normal mode) = 1h 2m", "; model printing time: 1h; total estimated time: 2h",
               "; TIME:123", ";time : 5", "; estimated printing time =", ";x\n; printing time = 1"]
    texts = _fixtures() + [""]
    for header in headers:
        texts += [header + "\n" + filler, filler + header + "\n", filler + header + "\n" + filler, filler]
    texts.append(filler[:-1] + "; TIME:1\n")  # glued to the line before: not at a line start
    for text in texts:
        assert slice_guard.has_time_header(text) == bool(slice_guard.TIME_HEADER.search(text)), repr(text[:80])


def test_post_slice_reads_the_whole_file_only_when_it_must(monkeypatch):
    """Work-count guard: a G-code with its estimate in the footer is searched in the tail only."""
    calls = []
    real = slice_guard.TIME_HEADER

    class Counting:
        def search(self, text, *args):
            calls.append(args)
            return real.search(text, *args)
    monkeypatch.setattr(slice_guard, "TIME_HEADER", Counting())
    text = "G1 X1 Y1 E.1\n" * 40000 + "; estimated printing time (normal mode) = 1h\n"
    result = slice_guard.post_slice(text, bed=(220.0, 220.0), tool_slots={0: 1})
    assert not any("estimated print-time" in w for w in result.warnings)
    assert () not in calls, "the whole file was searched although the footer has the estimate"
