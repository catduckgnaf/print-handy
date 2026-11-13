"""A slice normalizes its G-code once (#1392).

Seen live on 2.3.2: a 25 MB single-color lantern G-code took about 26 s
between OrcaSlicer finishing and the Ready question. Part of it was the ZMod
finalize running twice on the same file: slice_model finalizes with the
one-slot map built from ``filament_slot``, and every single-color caller in
jobs/slice.py then called ``finalize_multicolor`` with that same map. The
second pass re-normalized the whole file (about 12 s) to produce identical
bytes.

``finalize`` now returns early when the file already carries a valid checksum
and exactly the map and color lines it would write. Anything else (another
slot, a file without Print Handy's map line, ``drop_unused``) still takes the
full path. These tests pin the bytes, the early return, and the pass count.
"""
import copy
import hashlib
import re
from pathlib import Path
from unittest.mock import patch

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


def test_refinalizing_with_the_same_map_keeps_the_bytes_and_skips_normalization(tmp_path):
    st = state()
    path = tmp_path / "plate.gcode"
    path.write_text(sparse_artifact([0], ["PLA"]))
    first = contract.finalize(path, None, st, {"0": 1}, drop_unused=True)
    finalized = path.read_bytes()
    calls, patched = counting_normalize()
    with patched:
        again = contract.finalize(path, None, st, {"0": 1})
    assert calls == []
    assert again == first == hashlib.sha256(finalized).hexdigest()
    assert path.read_bytes() == finalized
    assert job.zmod_md5_valid(path)


def test_the_early_return_gives_the_bytes_a_full_refinalize_gives(tmp_path):
    st = state()
    fast, full = tmp_path / "fast.gcode", tmp_path / "full.gcode"
    for path in (fast, full):
        path.write_text(sparse_artifact([0], ["PLA"]))
        contract.finalize(path, None, st, {"0": 1}, drop_unused=True)
    contract.finalize(fast, None, st, {"0": 1})
    with patch.object(contract, "_header_metadata", return_value=None):
        contract.finalize(full, None, st, {"0": 1})
    assert fast.read_bytes() == full.read_bytes()


def test_another_slot_or_an_unmapped_file_still_finalizes_in_full(tmp_path):
    st = state()
    path = tmp_path / "plate.gcode"
    path.write_text(sparse_artifact([0], ["PLA"]))
    contract.finalize(path, None, st, {"0": 1})
    calls, patched = counting_normalize()
    with patched:
        contract.finalize(path, None, st, {"0": 2})
    assert len(calls) == 1
    assert contract.embedded_mapping(path.read_text(), st) == {"0": 2}

    single = tmp_path / "single.gcode"
    single.write_text(sparse_artifact([0], ["PLA"]))
    contract.finalize(single, ("#FFFFFF", "PLA"))  # checksum, no Print Handy map line
    calls, patched = counting_normalize()
    with patched:
        contract.finalize(single, None, st, {"0": 1})
    assert len(calls) == 1
    assert contract.embedded_mapping(single.read_text(), st) == {"0": 1}


def test_an_invalid_checksum_is_still_refused(tmp_path):
    st = state()
    path = tmp_path / "plate.gcode"
    path.write_text(sparse_artifact([0], ["PLA"]))
    contract.finalize(path, None, st, {"0": 1})
    path.write_bytes(path.read_bytes().replace(b"G1 X10", b"G1 X11"))
    try:
        contract.finalize(path, None, st, {"0": 1})
    except RuntimeError as exc:
        assert "invalid existing ZMod checksum" in str(exc)
    else:
        raise AssertionError("an edited finalized file must be refused")


_OLD_WORD = re.compile(r"\b([XYZE])\s*(-?(?:\d+(?:\.\d*)?|\.\d+))", re.I)


def _old_bed_verdict(text, bed):
    """post_slice's bed loop before #1392, frozen: every word parsed, every point kept."""
    from print_handy.core.slice_guard import MOVE
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


def test_a_single_color_slice_and_its_caller_normalize_the_gcode_once(tmp_path):
    """The jobs/slice.py pattern: slice_model(filament_slot=N), then finalize_multicolor with {'0': N}."""
    from print_handy.jobs import slice as jobs_slice
    source = tmp_path / "part.stl"
    source.write_text("solid x\nendsolid x\n")
    st = state()
    calls, patched = counting_normalize()
    with patched, patch.object(adapter.subprocess, "run", side_effect=fake_slice):
        out, provenance, _ = adapter.slice_model(source, tmp_path / "out", st, filament_slot=1)
        finalized = out.read_bytes()
        jobs_slice.finalize_multicolor(out, None, st, {"0": 1})
    assert len(calls) == 1, f"the G-code was normalized {len(calls)} times"
    assert out.read_bytes() == finalized
    assert provenance["gcode_sha256"] == hashlib.sha256(finalized).hexdigest()
