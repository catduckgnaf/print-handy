"""A finalized G-code is re-finalized in full, cheaply, without being rewritten (#1392).

Seen live on 2.3.2: a 25 MB single-color lantern G-code took about 26 s
between OrcaSlicer finishing and the Ready question. Part of it was the ZMod
finalize running twice on the same file: slice_model finalizes with the
one-slot map built from ``filament_slot``, and every single-color caller in
jobs/slice.py then called ``finalize_multicolor`` with that same map.

#1393 skipped the second pass when the file already carried a valid checksum
and Print Handy's own map lines. Review found the hole: both can be written by
hand, so a crafted file skipped the start-heater normalization and the material
and tool checks. The shortcut is gone; the full pass is made cheap instead
(the start code is read around START_PRINT only, the tools in one pass, the
filament_type line found without a whole-text regex), and an unchanged result
is not rewritten.
"""
import copy
import hashlib
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from print_handy.core import model_job as job
from print_handy.core import orca_adapter as adapter
from print_handy.printers.flashforge.zmod import multicolor_contract as contract
from print_handy.printers.flashforge.zmod import native_startup
from tests.pipeline.test_multicolor_contract import sparse_artifact
from tests.pipeline.test_native_contract import fake_slice


def state():
    return copy.deepcopy(job.printer_state.load())


def counting_normalize():
    calls = []
    real = native_startup.normalize

    def normalize(text):
        calls.append(len(text))
        return real(text)

    return calls, patch.object(native_startup, "normalize", normalize)


def _checksummed(payload):
    body = payload.encode()
    return b"; MD5:" + hashlib.md5(body).hexdigest().encode() + b"\r\n" + body


def test_refinalizing_with_the_same_map_keeps_the_bytes_and_writes_nothing(tmp_path):
    st = state()
    path = tmp_path / "plate.gcode"
    path.write_text(sparse_artifact([0], ["PLA"]))
    first = contract.finalize(path, None, st, {"0": 1}, drop_unused=True)
    finalized = path.read_bytes()
    calls, patched = counting_normalize()
    with patched, patch.object(Path, "write_bytes", side_effect=AssertionError("rewritten")):
        again = contract.finalize(path, None, st, {"0": 1})
    assert len(calls) == 1, "every finalize normalizes and validates in full"
    assert again == first == hashlib.sha256(finalized).hexdigest()
    assert path.read_bytes() == finalized
    assert job.zmod_md5_valid(path)


def test_a_checksummed_file_with_print_handys_map_lines_is_still_normalized(tmp_path):
    """The review finding on #1393: a valid MD5 and matching map lines prove nothing about the start code."""
    st = state()
    reference = tmp_path / "reference.gcode"
    reference.write_text(sparse_artifact([0], ["PLA"]))
    contract.finalize(reference, None, st, {"0": 1})
    payload = reference.read_bytes().partition(b"\n")[2].decode()
    assert "M190 S60\nM104 S210 T0\nSTART_PRINT" in payload
    crafted = tmp_path / "crafted.gcode"
    crafted.write_bytes(_checksummed(payload.replace("M190 S60\nM104 S210 T0\n", "")))
    contract.finalize(crafted, None, st, {"0": 1})
    assert crafted.read_bytes() == reference.read_bytes(), "the heaters were not put back"


def test_a_checksummed_file_with_print_handys_map_lines_is_still_material_checked(tmp_path):
    st = state()
    path = tmp_path / "plate.gcode"
    path.write_text(sparse_artifact([0], ["PLA"]))
    contract.finalize(path, None, st, {"0": 1})
    payload = path.read_bytes().partition(b"\n")[2].decode()
    path.write_bytes(_checksummed(payload.replace("; filament_type = PLA", "; filament_type = PETG")))
    with pytest.raises(RuntimeError, match="material"):
        contract.finalize(path, None, st, {"0": 1})


def test_another_slot_or_an_unmapped_file_finalizes_to_the_new_map(tmp_path):
    st = state()
    path = tmp_path / "plate.gcode"
    path.write_text(sparse_artifact([0], ["PLA"]))
    contract.finalize(path, None, st, {"0": 1})
    contract.finalize(path, None, st, {"0": 2})
    assert contract.embedded_mapping(path.read_text(), st) == {"0": 2}

    single = tmp_path / "single.gcode"
    single.write_text(sparse_artifact([0], ["PLA"]))
    contract.finalize(single, ("#FFFFFF", "PLA"))  # checksum, no Print Handy map line
    contract.finalize(single, None, st, {"0": 1})
    assert contract.embedded_mapping(single.read_text(), st) == {"0": 1}


def test_an_invalid_checksum_is_still_refused(tmp_path):
    st = state()
    path = tmp_path / "plate.gcode"
    path.write_text(sparse_artifact([0], ["PLA"]))
    contract.finalize(path, None, st, {"0": 1})
    path.write_bytes(path.read_bytes().replace(b"G1 X10", b"G1 X11"))
    with pytest.raises(RuntimeError, match="invalid existing ZMod checksum"):
        contract.finalize(path, None, st, {"0": 1})


def test_the_first_filament_type_line_is_found_like_the_whole_text_search():
    texts = ["", "; filament_type = PLA\n", "x; filament_type = A\n; filament_type = B\n",
             "; filament_type = \n; filament_type = C\r\n", "\n\n; filament_type = D", "; filament_type = E\rF\n",
             ";  filament_type = G\n; FILAMENT_TYPE = H\n", "a\r; filament_type = I\n"]
    for text in texts:
        fast = contract._first_line_match(text, contract.FILAMENT_TYPE_LINE, "; filament_type = ")
        slow = re.search(r"^; filament_type = (.+)$", text, re.M)
        assert (None if fast is None else (fast.span(), fast.groups())) == \
            (None if slow is None else (slow.span(), slow.groups())), repr(text)


def test_used_tools_matches_the_two_pass_scan():
    def old(text):
        def scan(pattern, candidates):
            return {int(m.group(1)) for line in text.splitlines()
                    if line[:1] in candidates and (m := pattern.fullmatch(line.strip()))}
        return (scan(contract.TOOL_LINE, contract._TOOL_CANDIDATES)
                or scan(contract.HEATER_TOOL_LINE, contract._HEATER_CANDIDATES))
    texts = [sparse_artifact([0, 2], ["PLA"] * 3), sparse_artifact([], ["PLA"]), "M104 S200 T1\nM109 S200 T0\n",
             "M104 S200 T1\nT3\n", " T2 ; c\n\tM104 S1 T0\n", "", "T\nM104 T\n", "M104 S1 T2\n　T4\n"]
    for text in texts:
        assert contract.used_tools(text) == old(text), repr(text)


_OLD_WORD = re.compile(r"\b([XYZE])\s*(-?(?:\d+(?:\.\d*)?|\.\d+))", re.I)
MOVE = re.compile(r"^\s*G[0-3]\b([^;]*)", re.I)


def _old_bed_verdict(text, bed):
    """post_slice's bed loop before #1392, frozen: every word parsed, every point kept."""
    coords = []
    x = y = 0.0
    for line in text.splitlines():
        m = MOVE.match(line)
        if not m:
            continue
        vals = {k.upper(): float(v) for k, v in _OLD_WORD.findall(m.group(1))}
        x = vals.get("X", x)
        y = vals.get("Y", y)
        if "X" in vals or "Y" in vals:
            coords.append((x, y))
            if x < 0 or y < 0 or x > bed[0] or y > bed[1]:
                return [f"G-code move goes off the {bed[0]:g}x{bed[1]:g} mm bed at X{x:g} Y{y:g}."]
    return []


def test_the_narrowed_bed_check_blocks_exactly_what_the_old_loop_blocked():
    from print_handy.core import slice_guard
    root = Path(__file__).resolve().parents[2]
    texts = [p.read_text(errors="replace") for p in sorted((root / "tests").rglob("*.gcode"))]
    texts += [
        "G1 X10 Y10\nG1 Z5 E2\nG1 X230 Y5\n",          # off the bed in X after a Z/E-only move
        "G0 x5 y-1 ; lower case, negative Y\n",
        "G1 E1X300\nG1 X1Y2Z3E4\nG2 X5 Y5 I1 J1\n",   # glued words, an arc
        "G1 X1 Y1 X250\n",                             # the last X on a line wins
        "G1 Z.2\nG1 X.5 Y219.9\nG1 Y220.1\n",
        "M104 S200\n; G1 X999\nG1 F3000\n",            # no XY move at all
    ]
    for bed in ((220.0, 220.0), (100.0, 100.0)):
        for text in texts:
            got = [b for b in slice_guard.post_slice(text, bed=bed, tool_slots={0: 1}).blocked
                   if "goes off the" in b]
            assert got == _old_bed_verdict(text, bed), text[:80]


def test_a_single_color_slice_and_its_caller_never_list_every_command(tmp_path):
    """Work-count guard: the slice and its caller's re-finalize read the start code around
    START_PRINT only (commands() lists every command line: 1.2 s on 25 MB) and write once."""
    from print_handy.jobs import slice as jobs_slice
    source = tmp_path / "part.stl"
    source.write_text("solid x\nendsolid x\n")
    st = state()
    with patch.object(native_startup, "commands", side_effect=AssertionError("listed every command")), \
            patch.object(adapter.subprocess, "run", side_effect=fake_slice):
        out, provenance, _ = adapter.slice_model(source, tmp_path / "out", st, filament_slot=1)
        finalized = out.read_bytes()
        with patch.object(Path, "write_bytes", side_effect=AssertionError("rewritten")):
            jobs_slice.finalize_multicolor(out, None, st, {"0": 1})
    assert out.read_bytes() == finalized
    assert provenance["gcode_sha256"] == hashlib.sha256(finalized).hexdigest()
