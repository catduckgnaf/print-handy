"""Real-Orca checks for the chat edit plumbing (#903): a supports edit
slices G-code that actually contains support toolpaths, the edit's
support type lands in the sliced config, supports-off produces none,
and a mirror edit slices through the real geometry path.

Skipped unless PRINT_HANDY_REAL_SLICER=1; CI runs these in the Slicer
test jobs. The offline half of every claim here is covered in
tests/core/test_edit_plan.py and tests/pipeline/test_edit_pipeline.py.
"""
import json
import os
import tempfile
from pathlib import Path

import pytest

from print_handy import settings
from print_handy.core import edit_plan as ep
from print_handy.core import orca_adapter

REAL = os.environ.get("PRINT_HANDY_REAL_SLICER") == "1"
WORK = settings.ROOT / ".slicer-work"
STATE = Path(__file__).resolve().parents[1] / "pipeline" / "fixtures" / "printer_hardware.json"

pytestmark = pytest.mark.skipif(not REAL, reason="set PRINT_HANDY_REAL_SLICER=1 to slice with the real Orca CLI")


def _box(lines, ox, oy, oz, sx, sy, sz):
    v = [(ox + x, oy + y, oz + z) for x in (0, sx) for y in (0, sy) for z in (0, sz)]
    faces = [(0, 2, 3), (0, 3, 1), (4, 5, 7), (4, 7, 6), (0, 1, 5), (0, 5, 4),
             (2, 6, 7), (2, 7, 3), (0, 4, 6), (0, 6, 2), (1, 3, 7), (1, 7, 5)]
    for a, b, c in faces:
        lines += ["facet normal 0 0 0", " outer loop"]
        lines += [f"  vertex {v[i][0]} {v[i][1]} {v[i][2]}" for i in (a, b, c)]
        lines += [" endloop", "endfacet"]


def write_overhang(path):
    """A 10 mm pillar holding a 40 mm bar: the bar's underside beyond the
    pillar is a real overhang that needs supports."""
    lines = ["solid overhang"]
    _box(lines, 0, 0, 0, 10, 10, 20)      # pillar
    _box(lines, 0, 0, 20, 40, 10, 10)     # bar, overhanging from x=10 to x=40
    lines.append("endsolid overhang")
    path.write_text("\n".join(lines) + "\n")
    return path


def write_cube(path, size=20):
    lines = ["solid cube"]
    _box(lines, 0, 0, 0, size, size, size)
    lines.append("endsolid cube")
    path.write_text("\n".join(lines) + "\n")
    return path


def _slice(tmp, source, **kwargs):
    state = json.loads(STATE.read_text())
    out, _provenance, _configs = orca_adapter.slice_model(source, Path(tmp) / "slice", state, **kwargs)
    return Path(out).read_text(errors="replace")


def test_supports_edit_produces_support_toolpaths():
    WORK.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as tmp:
        text = _slice(tmp, write_overhang(Path(tmp) / "overhang.stl"), supports=True)
        assert ep.support_feature_lines(text) > 0
        assert ep.gcode_config(text)["enable_support"] == "1"
        ok, lines = ep.verify_slice({"kind": "supports", "enabled": True, "type": "tree"},
                                    gcode_text=text, before_dims=None)
        assert ok, lines


def test_supports_off_and_normal_type_edits():
    WORK.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as tmp:
        text = _slice(tmp, write_overhang(Path(tmp) / "overhang.stl"), supports=False)
        assert ep.support_feature_lines(text) == 0
        assert ep.gcode_config(text)["enable_support"] == "0"
    with tempfile.TemporaryDirectory(dir=WORK) as tmp:
        text = _slice(tmp, write_overhang(Path(tmp) / "overhang.stl"), supports=True,
                      edits={"support_type": "normal"})
        assert "normal" in (ep.gcode_config(text)["support_type"] or "")


def test_mirror_edit_slices():
    WORK.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as tmp:
        text = _slice(tmp, write_cube(Path(tmp) / "cube.stl"),
                      edits={"mirror": ep.mirror_matrix("x")})
        assert "G1" in text  # a real slice of the mirrored cube
