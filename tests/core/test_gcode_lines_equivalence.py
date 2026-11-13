"""Offline verification asks its G-code questions from one scan, with the same answers (#1153 item 7).

``verify_gcode``, ``verify_gcode_offline``, ``_with_gcode_temperatures``,
``sovol_style_start_matches`` and the printer_state header helpers used to
ask each line-anchored question with its own ``re.search`` / ``re.findall`` /
``re.split`` over the whole file. They now ask ``gcode_lines.GcodeLines``,
which scans the text once. ``OLD`` below is the removed path, frozen
verbatim: every question the verify code asks, as the whole-text call it
used to make. The test runs old and new over the corpus G-code fixtures and
a set of adversarial texts and requires identical answers (match span and
groups, or the findall/split result), and one scan for all of them.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from corpus import harness
from gcode_factory import TOOLPATH, gcode_text
from pipeline.native_bounds_corpus import CASES as BOUNDS_CASES
from print_handy.core import model_job, printer_state
from print_handy.core.gcode_lines import Anchor, GcodeLines

ROOT = Path(__file__).resolve().parents[1]
NUMBER = r'([+-]?(?:\d+(?:\.\d*)?|\.\d+))'


class _Adapter:
    """An adapter carrying the configured macro names verify reads."""

    def __init__(self, start_macro, end_macro):
        self.start_macro, self.end_macro = start_macro, end_macro


MACROS = [("PRINT_START", "PRINT_END"), ("START_PRINT", "END_PRINT"),
          ("MY_START", "MY_END"), ("PRINT_START_EXTRA", "PRINT_END_CUSTOM")]


def questions(start_macro, end_macro):
    """(name, kind, pattern, anchor): every line-anchored question the verify code asks."""
    out = []
    for key in ("filament_type", "nozzle_diameter", "first_layer_height", "initial_layer_print_height",
                "nozzle_temperature_initial_layer", "nozzle_temperature"):
        out.append((f"config {key}", "search", rf'^; {re.escape(key)} = (.+)$', f"; {key} = "))
    for macro in dict.fromkeys(("PRINT_START", "START_PRINT", start_macro)):
        out.append((f"start {macro}", "search", rf'^{re.escape(macro)}\s+([^;]*)', macro))
    out.append(("sovol start", "search", r'^START_PRINT\b\s*([^;]*)', "START_PRINT"))
    for command in ("M190", "M109"):
        out.append((f"sovol {command}", "search", rf'^{command}\s+S{NUMBER}', command))
        out.append((f"octoprint {command}", "findall", rf'^{command}\s+S([\d.]+)', command))
    for command in ("M104", "M140"):
        out.append((f"octoprint {command} off", "search", rf'^{command}\s+S0(?:\s|$)', command))
    for macro in dict.fromkeys(("PRINT_END", "END_PRINT", end_macro)):
        out.append((f"end {macro}", "search", rf'^{re.escape(macro)}(?:\s|$)', macro))
    out.append(("tools", "findall", r"^[ \t]*T(\d+)\b", printer_state.TOOL_LINE_ANCHOR))
    out.append(("tool slots", "search", r"^;\s*hermes_tool_slots\s*=\s*(.+)$", printer_state.TOOL_SLOTS_ANCHOR))
    return out


def _shape(found):
    return None if found is None else (found.span(), found.groups())


def old_answers(text, start_macro, end_macro):
    """The removed path: one whole-text re call per question."""
    out = {}
    for name, kind, pattern, _anchor in questions(start_macro, end_macro):
        if kind == "search":
            out[name] = _shape(re.search(pattern, text, re.M))
        else:
            out[name] = re.findall(pattern, text, re.M)
    layers = re.split(r'^;LAYER_CHANGE', text, flags=re.M)
    out["layers"] = (layers[:6], len(layers))
    return out


def new_answers(lines, start_macro, end_macro):
    out = {}
    for name, kind, pattern, anchor in questions(start_macro, end_macro):
        if kind == "search":
            out[name] = _shape(lines.search(pattern, anchor))
        else:
            out[name] = lines.findall(pattern, anchor)
    out["layers"] = lines.split(';LAYER_CHANGE', 6)
    return out


def _fixture_texts():
    texts = {}
    for path in sorted((ROOT / "fixtures").rglob("*.gcode*")):
        texts[str(path.relative_to(ROOT))] = path.read_text(errors="ignore")
    for support in ("0", "1"):
        for tools in ("", "T0\nT1\n", "  T1\nT0 ; swap\n"):
            texts[f"corpus fake slicer support={support} tools={tools!r}"] = harness.FAKE_GCODE.format(
                hotend=215, bed=60, nozzle="0.4", support=support, types="PLA;PLA", tools=tools,
                layer="0.2", first_layer="0.2", infill="15%", pattern="gyroid",
                support_lines="; FEATURE: Support\nG1 X110 Y110 E2\n" if support == "1" else "")
    for family in ("orca", "bambu", "prusa", "cura"):
        texts[f"gcode_factory {family}"] = gcode_text(family)
        texts[f"gcode_factory {family} no footer"] = gcode_text(family, footer=False)
    for n, case in enumerate(BOUNDS_CASES):
        texts[f"bounds corpus {n}"] = case
    return texts


ADVERSARIAL = {
    "empty": "",
    "no newline at end": "START_PRINT BED_TEMP=60 EXTRUDER_TEMP=215\nEND_PRINT",
    "crlf": "; nozzle_diameter = 0.4\r\nPRINT_START BED=60 HOTEND=215\r\nPRINT_END\r\n",
    "start spans lines": "START_PRINT\nBED_TEMP=60\nEXTRUDER_TEMP=215 ; trailing\nM190 S60\nM109 S215\n",
    "macro prefix collisions": ("PRINT_START_EXTRA BED=1 HOTEND=2\nPRINT_START BED=60 HOTEND=215\n"
                                "PRINT_END_CUSTOM\nPRINT_ENDING\nPRINT_END\n"),
    "indented and commented": "  PRINT_START BED=60 HOTEND=215\n;PRINT_END\nG1 X1 ; PRINT_END\n",
    "empty and repeated config": ("; nozzle_diameter = \n; nozzle_diameter =  0.6 \n; nozzle_diameter = 0.4\n"
                                  "; first_layer_height = 0\n; initial_layer_print_height = 0.2\n"),
    "config key prefixes": "; nozzle_temperature_initial_layer = 220,210\n; nozzle_temperature = 215,205\n",
    "layers adjacent and late": ";LAYER_CHANGE\n;LAYER_CHANGE\n;WIDTH:0.4\n;LAYER_CHANGE;LAYER_CHANGE\nX;LAYER_CHANGE\n",
    "many layers": "".join(f";LAYER_CHANGE\n;Z:{i}\n;WIDTH:0.4\nG1 X{i} E1\n" for i in range(12)),
    "tool lines": "T0\n\tT1\n T12x\nT3 ; c\nTT1\n  T\n T7\n",
    "tool slots variants": (';hermes_tool_slots={"0": 1}\n; hermes_tool_slots = {"0": {"spoolman_id": 9}}\n'),
    "tool slots across lines": ';\n  hermes_tool_slots\n=\n{"0": 2}\n',
    "octoprint heaters": "M190 S60.5\nM109 S\nM109 S215.\nM104 S0\nM140 S0 ; off\nM1040 S0\nM104 S00\n",
    "sovol bare start": "START_PRINT\nM190 S+60\nM109 S.5\nM190 S70\n",
    "sovol word boundary": "START_PRINT_X\nSTART_PRINTER BED_TEMP=1\nSTART_PRINT;c\n",
    "carriage returns": "PRINT_START BED=60\rHOTEND=215\nPRINT_END\r\n; filament_type = PLA\r\n",
    "custom macros": "MY_START BED=60 HOTEND=215\nMY_START_LATE\nMY_END\n",
    "unicode": "; filament_type = \"Silk PLA\"\nPRINT_START BED=60 HOTEND=215 ; é\nPRINT_END\n",
}

ADVERSARIAL["large synthetic plate"] = (
    "PRINT_START BED=60 HOTEND=215\nM190 S60\nM109 S215\n"
    + "".join(f";LAYER_CHANGE\n;TYPE:Outer wall\n;WIDTH:0.42\n" + TOOLPATH + ("T1\n" if i % 7 == 0 else "")
              for i in range(400))
    + "PRINT_END\n; filament_type = PLA;PETG\n; nozzle_diameter = 0.4\n; nozzle_temperature = 215,240\n"
    + '; hermes_tool_slots = {"0": 1, "1": 2}\n')

TEXTS = {**_fixture_texts(), **ADVERSARIAL}


@pytest.mark.parametrize("name", sorted(TEXTS))
def test_one_scan_gives_the_removed_path_answers(name):
    text = TEXTS[name]
    for macros in MACROS:
        lines = model_job._verify_lines(text, _Adapter(*macros))
        assert new_answers(lines, *macros) == old_answers(text, *macros), macros
        assert lines.scans == 1


def test_the_corpus_reaches_every_question():
    """Guard against a vacuous comparison: some text answers every question."""
    seen = set()
    for text in TEXTS.values():
        for name, answer in old_answers(text, "MY_START", "MY_END").items():
            if name != "layers" and answer not in (None, []):
                seen.add(name)
    assert {q[0] for q in questions("MY_START", "MY_END")} <= seen


def test_an_undeclared_or_unscannable_anchor_falls_back_to_the_plain_call():
    text = "A\nPRINT_END\n\nPRINT_END\nB\n"
    lines = GcodeLines(text, ("PRINT_END",))
    assert lines.search(r'^A', "A").span() == (0, 1)  # scanned with the declared set
    assert lines.search(r'^B', "B").span() == re.search(r'^B', text, re.M).span()  # declared after the scan
    assert _shape(lines.search(r'^(?:\s|$)', "")) == _shape(re.search(r'^(?:\s|$)', text, re.M))
    assert _shape(lines.search(r'^D\nPRINT', "D\nPRINT")) is None
    assert lines.split("\n", 3) == (re.split("^\n", text, flags=re.M)[:3], len(re.split("^\n", text, flags=re.M)))
    assert lines.scans == 1


def test_a_lookahead_anchor_never_hides_the_next_line():
    text = ";\nhermes_tool_slots=1\nPRINT_END\n"
    lines = GcodeLines(text, ("PRINT_END", Anchor(r";\s*hermes_tool_slots\s*=")))
    assert lines.search(r'^PRINT_END(?:\s|$)', "PRINT_END").start() == text.index("PRINT_END")
    assert lines.search(r'^;\s*hermes_tool_slots\s*=\s*(.+)$', Anchor(r";\s*hermes_tool_slots\s*=")).group(1) == "1"


def test_verify_asks_its_questions_from_one_scan(monkeypatch):
    """The production lookups share the caller's scan: no whole-text re call on the G-code."""
    text = TEXTS["large synthetic plate"]
    single = text.replace("PLA;PETG", "PLA")
    plain_search, plain_findall = re.search, re.findall

    def guard(real):
        def call(pattern, string, *args, **kwargs):
            if string is text or string is single:
                pytest.fail(f"whole-text pass over the G-code: {pattern!r}")
            return real(pattern, string, *args, **kwargs)
        return call

    monkeypatch.setattr(printer_state, "slice_temperatures",
                        lambda state, *a, **k: ({"nozzle": 215, "bed": 60}, None))
    monkeypatch.setattr(re, "search", guard(plain_search))
    monkeypatch.setattr(re, "findall", guard(plain_findall))
    state = {"spool": {"material": "PLA", "spoolman_id": 1}, "temps_c": {"nozzle": 215, "bed": 60}}
    lines = model_job._verify_lines(single, _Adapter("PRINT_START", "PRINT_END"))
    assert model_job._with_gcode_temperatures(state, single, lines=lines)["temps_c"] == {"nozzle": 215, "bed": 60}
    assert model_job.sovol_style_start_matches(single, 60, 215, lines=lines) is False
    assert lines.scans == 1
    multi = model_job._verify_lines(text, _Adapter("PRINT_START", "PRINT_END"))
    try:
        printer_state.gcode_tool_temperatures(state, ["PLA", "PETG"], text, lines=multi)
    except RuntimeError:
        pass  # a temperature refusal is fine here; only the passes are under test
    assert multi.scans == 1
