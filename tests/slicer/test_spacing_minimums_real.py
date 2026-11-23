"""The "minimum" part spacing preset, proved with the real Orca CLI.

Every row slices through Print Handy's own slice path (the merged single-slot
plate and the multicolor merge with its prime tower), then runs the offline
G-code guard. The test asserts that the shipped minimum values slice cleanly
and prints the boundary sweep, so a slicer upgrade that moves a limit shows up
in the CI log. Skipped unless PRINT_HANDY_REAL_SLICER=1.
"""
import json
import os
import re
import tempfile
from pathlib import Path

import pytest

from print_handy import printers, settings
from print_handy.core import color_geometry, orca_adapter, source_colors
from print_handy.gcode import verify as gcode_verify
from print_handy.jobs import filament as jobs_filament
from print_handy.jobs import slice as jobs_slice
from tests.corpus import make_models as mm

REAL = os.environ.get("PRINT_HANDY_REAL_SLICER") == "1"
pytestmark = pytest.mark.skipif(not REAL, reason="set PRINT_HANDY_REAL_SLICER=1 to slice with the real Orca CLI")
# Flatpak Orca has a private /tmp: work inside the checkout.
WORK = settings.ROOT / ".slicer-work"
STATE = Path(__file__).resolve().parents[1] / "pipeline" / "fixtures" / "printer_hardware.json"
BED = 220.0
MINIMUM = settings.SPACING_PRESETS["minimum"]


def _state():
    return json.loads(STATE.read_text())


def _source(path, shapes, filaments=2):
    """shapes: [((w, d), h, (x, y))] closed boxes; build object ids are 1, 3, 5, ...

    The single-slot plate gets a one-filament project: OrcaSlicer 2.5.0-beta refuses
    (-5, "filament_is_support's count 1 not equal to filament_colour's size 2") a
    derived single-filament slice of a project that lists more filaments.
    """
    rows = [(f"Part {i + 1}", 1, mm.box(w, d, h), at) for i, ((w, d), h, at) in enumerate(shapes)]
    palette = [("#FFFFFF", "PLA"), ("#FF00FF", "PLA")][:filaments]
    mm.write_3mf(path, rows, [("Plate 1", list(range(len(rows))))], palette)
    return path


def _decisions(state, manifest, second_slot_object=None):
    sel = {s: jobs_filament.resolve_filament_selection(state, dict(state["filament_slots"][str(s)], slot=s)) for s in (1, 2)}
    choices = {}
    for part in manifest["parts"]:
        choices["part:" + part["key"]] = sel[2 if str(part["object_id"]) == str(second_slot_object) else 1]
    for row in source_colors.canonical_prompts(manifest):
        choices.setdefault(row["key"], choices.get(row["key"]) or sel[1])
    return {"schema": "part_slots_v1", "mode": "per_part", "source_sha256": manifest["source_sha256"],
            "choices": choices}


def _extents(gcode):
    xs, ys, x, y = [], [], None, None
    for line in Path(gcode).read_text(errors="ignore").splitlines():
        if not line.startswith("G1"):
            continue
        if (m := re.search(r"X(-?[0-9.]+)", line)):
            x = float(m.group(1))
        if (m := re.search(r"Y(-?[0-9.]+)", line)):
            y = float(m.group(1))
        if (m := re.search(r"E([0-9.]+)", line)) and float(m.group(1)) > 0 and x is not None and y is not None:
            xs.append(x)
            ys.append(y)
    return (min(xs), max(xs), min(ys), max(ys)) if xs else None


def _single(tmp, shapes, brim):
    state = _state()
    src = _source(tmp / "src.3mf", shapes, filaments=1)
    manifest = source_colors.inventory(src)
    decisions = _decisions(state, manifest)
    plate = tmp / "plate.3mf"
    color_geometry.export_single_color_plate(
        src, plate, decisions, [p["key"] for p in manifest["parts"]], bed=int(BED),
        item_shifts={str(p["object_id"]): [0.0, 0.0, 0.0] for p in manifest["parts"]})
    out, _prov, _cfg = orca_adapter.slice_model(plate, tmp / "out", state, brim, preserve_layout=True,
                                                filament_slot=1)
    jobs_slice.finalize_multicolor(out, None, state, {"0": 1}, adapter=printers.current())
    gcode_verify.verify_gcode_offline(out, state)
    return _extents(out)


def _multi(tmp, shapes, tower_xy, monkeypatch):
    state = _state()
    src = _source(tmp / "src.3mf", shapes)
    manifest = source_colors.inventory(src)
    decisions = _decisions(state, manifest, second_slot_object=3)
    monkeypatch.setattr(color_geometry, "prime_tower_candidates", lambda *a, **k: [tuple(tower_xy)])
    out, _prov, _cfg = orca_adapter.slice_model(
        src, tmp / "out", state, None, color_decisions=decisions,
        item_shifts={str(p["object_id"]): [0.0, 0.0, 0.0] for p in manifest["parts"]})
    gcode_verify.verify_gcode_offline(out, state)
    return _extents(out)


def _try(label, fn, *args):
    WORK.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as d:
        try:
            extents = fn(Path(d), *args)
            print(f"SPACING {label:58s} OK  extruded {extents}")
            return True
        except Exception as exc:  # noqa: BLE001 - the sweep records every refusal
            print(f"SPACING {label:58s} NO  {type(exc).__name__}: {str(exc)[:1500]}")
            for path in sorted(Path(d).rglob("*.json")):
                # The slicer inputs Print Handy wrote, so a slicer upgrade that rejects one is visible.
                try:
                    json.loads(path.read_text())
                    state = "valid JSON"
                except ValueError as err:
                    state = f"INVALID JSON: {err}"
                print(f"SPACING   input {path.relative_to(d)} ({path.stat().st_size} B, {state})")
            for path in sorted(Path(d).rglob("*.log")):
                tail = path.read_text(errors="replace").strip().splitlines()[-25:]
                print(f"SPACING   log {path.relative_to(d)}:")
                for line in tail:
                    print("SPACING     " + line)
            return False


def test_minimum_part_gap_and_bed_edge_slice_cleanly():
    gap = MINIMUM[("layout", "arrange_gap_mm")]
    edge = MINIMUM[("layout", "arrange_edge_mm")]
    assert MINIMUM[("layout", "merge_spacing_mm")] == gap
    assert MINIMUM[("layout", "merge_edge_margin_mm")] == edge
    for brim in (None, 3):
        assert _try(f"gap {gap} mm, brim {brim}", _single,
                    [((30, 30), 5, (80, 80)), ((30, 30), 5, (110 + gap, 80))], brim)
    # A part at the minimum edge margin; with a brim the margin grows by its width.
    assert _try(f"edge {edge} mm, no brim", _single,
                [((30, 30), 5, (edge, edge)), ((30, 30), 5, (150, 150))], None)
    assert _try(f"edge {edge} + 3 mm brim", _single,
                [((30, 30), 5, (edge + 3, edge + 3)), ((30, 30), 5, (150, 150))], 3)
    # Boundary sweep, informational: Orca accepted all of these on 2.4.2 and clips a
    # brim at the bed edge instead of refusing the plate.
    for e in (0.5, 2.0):
        _try(f"sweep: edge {e} mm, brim 5", _single,
             [((30, 30), 5, (e, e)), ((30, 30), 5, (150, 150))], 5)
    _try("sweep: gap 0.5 mm, brim 3", _single, [((30, 30), 5, (80, 80)), ((30, 30), 5, (110.5, 80))], 3)


def test_minimum_prime_tower_clearances_slice_cleanly(monkeypatch):
    clearance = MINIMUM[("prime_tower", "part_clearance_mm")]
    tower_edge = MINIMUM[("prime_tower", "edge_mm")]
    tower = settings.current()["prime_tower"]["size_mm"]
    parts = [((40, 40), 5, (60, 100)), ((30, 30), 5, (20, 20))]
    assert _try(f"tower {clearance} mm from a part", _multi, parts, (100 + clearance, 100), monkeypatch)
    corner = BED - tower - tower_edge
    assert _try(f"tower {tower_edge} mm from the bed edges", _multi,
                [((40, 40), 5, (20, 100)), ((30, 30), 5, (20, 20))], (corner, corner), monkeypatch)
    # Boundary sweep, informational (2.4.2: part conflicts at 2.5 mm, off the bed at 3 mm).
    for c in (2.5, 3.0):
        _try(f"sweep: tower {c} mm from a part", _multi, parts, (100 + c, 100), monkeypatch)
    for t in (3.0, 3.5):
        _try(f"sweep: tower {t} mm from the bed edges", _multi,
             [((40, 40), 5, (20, 100)), ((30, 30), 5, (20, 20))], (BED - tower - t, BED - tower - t), monkeypatch)
