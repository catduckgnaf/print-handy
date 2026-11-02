"""Opt-in reviewed CAD split plans and dowel/pin connectors for oversized meshes (#153).

Provides:
- Pure-stdlib schema validation, source preflight, OpenSCAD CSG script synthesis,
  and printable chamfered dowel/pin mesh generation.
- Reviewed calibration presets (`rack_adapter_10to19`, `macmini_mount_a1347`).
- Optional execution of `openscad` CLI when explicitly requested (`--execute`),
  followed by post-boolean topology, shell-count, bounds, and volume conservation
  verification (`removed_volume == expected_pin_bore_volume` within tolerance).
- Never auto-splits unreviewed meshes; never slices, uploads, or starts a print.
"""
from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import tempfile
from pathlib import Path

from .geometry_layout import (
    DEFAULT_BED_MM,
    _bounds_from_triangles,
    _r4,
    _sha256,
    build_status_ladder,
    describe_triangles,
    inspect_geometry,
    rotate_triangles_z,
    split_disconnected_shells,
    write_binary_stl,
)
from .orca_adapter import read_stl_triangles

STRUCTURAL_WARNING = (
    'Socket bores and printed/metal dowels are for locating and bonding parts; '
    'no structural load rating has been performed. Prefer a one-piece diagonal '
    'or rotated print when the intact part fits the build volume.'
)

PRESET_PLANS = {
    'rack_adapter_10to19': {
        'plan_id': 'rack_adapter_10to19',
        'title': 'AD5X 10-to-19-inch rack adapter (X=0 split with 4x 5.0mm dowels)',
        'bed_mm': [220.0, 220.0, 220.0],
        'expected_extents_mm': [245.0, 44.5, 20.0],
        'expected_extents_tol_mm': [1.0, 1.0, 0.2],
        'expected_bounds_x_mm': [-122.5, 122.5],
        'expected_bounds_x_tol_mm': 1.0,
        'expected_shells': 1,
        'one_piece_z_rotation_deg': 45.0,
        'cut_axis': 'x',
        'cut_coord_mm': 0.0,
        'half_box_Negative': [-124.0, -25.0, -1.0, 124.0, 50.0, 22.0],
        'half_box_positive': [0.0, -25.0, -1.0, 124.0, 50.0, 22.0],
        'plate_spacing_y_mm': 37.25,
        'plate_shift_x_mm': 61.25,
        'volume_conservation_tol_mm3': 35.0,
        'ores': [
            {
                'name': 'dowel_y_neg12_z6',
                'y_mm': -12.0,
                'z_mm': 6.0,
                'bore_diameter_mm': 5.4,
                'depth_each_side_mm': 16.5,
                'pin_diameter_mm': 5.0,
                'pin_length_mm': 30.5,
                'pin_chamfer_mm': 0.8,
                'pin_tip_diameter_mm': 4.3,
                'printable_dowel': True,
            },
            {
                'name': 'dowel_y_neg12_z14',
                'y_mm': -12.0,
                'z_mm': 14.0,
                'bore_diameter_mm': 5.4,
                'depth_each_side_mm': 16.5,
                'pin_diameter_mm': 5.0,
                'pin_length_mm': 30.5,
                'pin_chamfer_mm': 0.8,
                'pin_tip_diameter_mm': 4.3,
                'printable_dowel': True,
            },
            {
                'name': 'dowel_y_pos12_z6',
                'y_mm': 12.0,
                'z_mm': 6.0,
                'bore_diameter_mm': 5.4,
                'depth_each_side_mm': 16.5,
                'pin_diameter_mm': 5.0,
                'pin_length_mm': 30.5,
                'pin_chamfer_mm': 0.8,
                'pin_tip_diameter_mm': 4.3,
                'printable_dowel': True,
            },
            {
                'name': 'dowel_y_pos12_z14',
                'y_mm': 12.0,
                'z_mm': 14.0,
                'bore_diameter_mm': 5.4,
                'depth_each_side_mm': 16.5,
                'pin_diameter_mm': 5.0,
                'pin_length_mm': 30.5,
                'pin_chamfer_mm': 0.8,
                'pin_tip_diameter_mm': 4.3,
                'printable_dowel': True,
            },
        ],
        'hardware_recommendation': '4 x 5.0 mm x 30 mm steel/printable dowels + two-part epoxy',
    },
    'macmini_mount_a1347': {
        'plan_id': 'macmini_mount_a1347',
        'title': 'AD5X Mac Mini A1347 mount (X=0 multi-shell union split with front M3 + rear 1.75mm pin bores)',
        'bed_mm': [220.0, 220.0, 220.0],
        'expected_extents_mm': [254.0, 44.625, 183.4078],
        'expected_extents_tol_mm': [1.0, 1.0, 1.0],
        'expected_bounds_x_mm': [-127.0, 127.0],
        'expected_bounds_x_tol_mm': 1.0,
        'expected_shells': 2,
        'one_piece_z_rotation_deg': 45.0,
        'cut_axis': 'x',
        'cut_coord_mm': 0.0,
        'half_box_Negative': [-150.0, -50.0, -1.0, 150.0, 100.0, 190.0],
        'half_box_positive': [0.0, -50.0, -1.0, 150.0, 100.0, 190.0],
        'plate_gap_mm': 16.0,
        'volume_conservation_tol_mm3': 10.0,
        'joint_test_coupon_box_left': [-16.0, -24.0, -1.0, 16.0, 14.0, 19.0],
        'joint_test_coupon_box_right': [0.0, -24.0, -1.0, 16.0, 14.0, 19.0],
        'bores': [
            {
                'name': 'front_m3_lower',
                'y_mm': -18.7,
                'z_mm': 4.5,
                'bore_diameter_mm': 3.35,
                'depth_each_side_mm': 11.5,
                'pin_diameter_mm': 3.0,
                'pin_length_mm': 20.0,
                'pin_chamfer_mm': 0.5,
                'pin_tip_diameter_mm': 2.5,
                'shell_target': 'main',
                'printable_dowel': False,
            },
            {
                'name': 'front_m3_upper',
                'y_mm': -18.7,
                'z_mm': 11.5,
                'bore_diameter_mm': 3.35,
                'depth_each_side_mm': 11.5,
                'pin_diameter_mm': 3.0,
                'pin_length_mm': 20.0,
                'pin_chamfer_mm': 0.5,
                'pin_tip_diameter_mm': 2.5,
                'shell_target': 'main',
                'printable_dowel': False,
            },
            {
                'name': 'rear_filament_guide',
                'y_mm': 20.7,
                'z_mm': 5.5,
                'bore_diameter_mm': 2.15,
                'depth_each_side_mm': 11.5,
                'pin_diameter_mm': 1.75,
                'pin_length_mm': 20.0,
                'pin_chamfer_mm': 0.3,
                'pin_tip_diameter_mm': 1.4,
                'shell_target': 'both',
                'printable_dowel': False,
            },
        ],
        'hardware_recommendation': '2 x 20mm x 3mm metal pins + 1 x 20mm 1.75mm filament pin + two-part epoxy',
    },
}
# Fix key typo in rack preset if any and expose normalized dict
if 'ores' in PRESET_PLANS['rack_adapter_10to19']:
    PRESET_PLANS['rack_adapter_10to19']['bores'] = PRESET_PLANS['rack_adapter_10to19'].pop('ores')


def load_split_plan(plan_or_id: str | Path | dict) -> dict:
    """Load and validate a reviewed CAD split plan by preset name, JSON path, or dict."""
    if isinstance(plan_or_id, dict):
        plan = dict(plan_or_id)
    elif isinstance(plan_or_id, str) and plan_or_id in PRESET_PLANS:
        plan = json.loads(json.dumps(PRESET_PLANS[plan_or_id]))
    else:
        path = Path(plan_or_id)
        if not path.is_file():
            raise ValueError(
                f'Unknown split plan {plan_or_id!r}; supply a preset ({", ".join(sorted(PRESET_PLANS))}) '
                'or a path to a reviewed split-plan JSON file.'
            )
        plan = json.loads(path.read_text(encoding='utf-8'))
    return validate_split_plan_schema(plan)


def _pin_dimensions(bore: dict) -> tuple[float, float]:
    """The pin diameter/length a bore's dowel is generated at: the plan's own
    values, or the defaults validation derives from the bore (0.4 mm clearance,
    2.5 mm short of the full bore length)."""
    pin_d = float(bore.get('pin_diameter_mm', float(bore['bore_diameter_mm']) - 0.4))
    pin_l = float(bore.get('pin_length_mm', 2.0 * float(bore['depth_each_side_mm']) - 2.5))
    return pin_d, pin_l


def validate_split_plan_schema(plan: dict) -> dict:
    """Validate required fields, clearances, and bore geometry on a CAD split plan."""
    if not isinstance(plan, dict):
        raise ValueError('Split plan must be a JSON object')
    for req in ('plan_id', 'cut_axis', 'cut_coord_mm', 'bores'):
        if req not in plan:
            raise ValueError(f'Split plan is missing required field {req!r}')
    if str(plan['cut_axis']).lower() != 'x':
        raise ValueError("Only cut_axis='x' is currently supported by reviewed split plans")
    bores = plan.get('bores') or []
    if not bores:
        raise ValueError('Split plan must define at least one alignment/connector bore')
    # Work on copies: validation normalizes the derived pin defaults into the
    # plan (below), and a caller's own dicts must not change under them.
    plan['bores'] = bores = [dict(bore) if isinstance(bore, dict) else bore for bore in bores]
    for idx, bore in enumerate(bores):
        for b_req in ('y_mm', 'z_mm', 'bore_diameter_mm', 'depth_each_side_mm'):
            if b_req not in bore:
                raise ValueError(f'Bore #{idx + 1} is missing {b_req!r}')
        bore_d = float(bore['bore_diameter_mm'])
        depth = float(bore['depth_each_side_mm'])
        if bore_d <= 0 or depth <= 0:
            raise ValueError(f'Bore #{idx + 1} diameter and depth must be > 0')
        pin_d, pin_l = _pin_dimensions(bore)
        if pin_d >= bore_d:
            raise ValueError(
                f'Bore #{idx + 1} pin_diameter_mm ({pin_d} mm) must be smaller than '
                f'bore_diameter_mm ({bore_d} mm) for clearance'
            )
        if pin_l > 2.0 * depth:
            raise ValueError(
                f'Bore #{idx + 1} pin_length_mm ({pin_l} mm) exceeds total bore length ({2.0 * depth} mm)'
            )
        # Normalize the derived defaults into the validated plan, so STL and
        # SCAD generation produce exactly the pin validation checked (P2-48).
        bore['pin_diameter_mm'] = pin_d
        bore['pin_length_mm'] = pin_l
    return plan


def expected_bore_removal_volume_mm3(plan: dict) -> float:
    """Compute theoretical cylindrical volume removed across the split plane by all bores."""
    total = 0.0
    for bore in plan.get('bores') or []:
        r = float(bore['bore_diameter_mm']) / 2.0
        length = 2.0 * float(bore['depth_each_side_mm'])
        total += math.pi * (r * r) * length
    return total


def generate_chamfered_dowel_triangles(
    diameter_mm: float = 5.0,
    length_mm: float = 30.5,
    chamfer_mm: float = 0.8,
    tip_diameter_mm: float = 4.3,
    segments: int = 48,
    center_xy: tuple[float, float] = (0.0, 0.0),
) -> list:
    """Generate a watertight, consistently wound triangulated dowel mesh in pure stdlib."""
    d = float(diameter_mm)
    length = float(length_mm)
    ch = float(chamfer_mm)
    tip_d = float(tip_diameter_mm)
    if d <= 0 or length <= 2 * ch or ch < 0 or tip_d <= 0:
        raise ValueError('Invalid dowel dimensions')
    n = max(12, int(segments))
    cx, cy = float(center_xy[0]), float(center_xy[1])

    z_levels = [0.0, ch, length - ch, length]
    radii = [tip_d / 2.0, d / 2.0, d / 2.0, tip_d / 2.0]

    rings = []
    for z, r in zip(z_levels, radii):
        ring = []
        for i in range(n):
            theta = (2.0 * math.pi * i) / n
            ring.append((cx + r * math.cos(theta), cy + r * math.sin(theta), z))
        rings.append(ring)

    tris = []
    bottom_center = (cx, cy, 0.0)
    top_center = (cx, cy, length)

    # Bottom cap (normal pointing -Z: clockwise when viewed from +Z)
    for i in range(n):
        j = (i + 1) % n
        tris.append([bottom_center, rings[0][j], rings[0][i]])

    # Side frustums/cylinder (normals pointing outward)
    for level in range(len(rings) - 1):
        lower = rings[level]
        upper = rings[level + 1]
        for i in range(n):
            j = (i + 1) % n
            tris.append([lower[i], lower[j], upper[j]])
            tris.append([lower[i], upper[j], upper[i]])

    # Top cap (normal pointing +Z)
    for i in range(n):
        j = (i + 1) % n
        tris.append([top_center, rings[-1][i], rings[-1][j]])

    return tris


def generate_dowel_row_triangles(plan: dict, segments: int = 48) -> list:
    """Generate a row of printable chamfered dowels for all printable_dowel bores in a plan."""
    printable = [b for b in (plan.get('bores') or []) if b.get('printable_dowel', True)]
    if not printable:
        return []
    count = len(printable)
    spacing = 18.0
    start_x = -0.5 * (count - 1) * spacing
    all_tris = []
    for idx, bore in enumerate(printable):
        cx = start_x + idx * spacing
        pin_d, pin_l = _pin_dimensions(bore)
        all_tris.extend(
            generate_chamfered_dowel_triangles(
                diameter_mm=pin_d,
                length_mm=pin_l,
                chamfer_mm=float(bore.get('pin_chamfer_mm', 0.8)),
                tip_diameter_mm=float(bore.get('pin_tip_diameter_mm', 4.3)),
                segments=segments,
                center_xy=(cx, 0.0),
            )
        )
    return all_tris


def check_source_against_split_plan(source: Path, plan: dict) -> dict:
    """Verify source STL topology, dimensions, shell count, and check if intact diagonal rotation fits."""
    source = Path(source)
    if source.suffix.lower() != '.stl':
        raise ValueError('CAD split plans currently require a watertight STL source mesh')
    plan = validate_split_plan_schema(plan)
    tris = read_stl_triangles(source)
    desc = describe_triangles(tris)
    if not desc['watertight'] or not desc['winding_consistent']:
        raise ValueError(
            f'{source.name} is not watertight or has inconsistent winding '
            f'(watertight={desc["watertight"]}, winding_inconsistencies={desc["winding_inconsistencies"]}); '
            'repair before CAD boolean cutting.'
        )

    expected_shells = plan.get('expected_shells')
    if expected_shells is not None and desc['shell_count'] != int(expected_shells):
        raise ValueError(
            f'{source.name} has {desc["shell_count"]} connected shells, '
            f'but split plan {plan["plan_id"]!r} requires {expected_shells}'
        )

    expected_ext = plan.get('expected_extents_mm')
    tol_ext = plan.get('expected_extents_tol_mm') or [1.0, 1.0, 1.0]
    if expected_ext is not None:
        for axis, (actual, exp, tol) in enumerate(zip(desc['dimensions_mm'], expected_ext, tol_ext)):
            if abs(float(actual) - float(exp)) > float(tol):
                raise ValueError(
                    f'{source.name} extent on axis {axis} is {actual:.4f} mm, '
                    f'expected {exp:.4f} ± {tol:.4f} mm for plan {plan["plan_id"]!r}'
                )

    expected_bx = plan.get('expected_bounds_x_mm')
    bx_tol = float(plan.get('expected_bounds_x_tol_mm', 1.0))
    if expected_bx is not None:
        if (
            abs(desc['bounds_min_mm'][0] - float(expected_bx[0])) > bx_tol
            or abs(desc['bounds_max_mm'][0] - float(expected_bx[1])) > bx_tol
        ):
            raise ValueError(
                f'{source.name} X bounds [{desc["bounds_min_mm"][0]}, {desc["bounds_max_mm"][0]}] '
                f'do not match expected {expected_bx} ± {bx_tol} mm'
            )

    bed = tuple(float(v) for v in (plan.get('bed_mm') or DEFAULT_BED_MM))
    rot_deg = float(plan.get('one_piece_z_rotation_deg', 45.0))
    rot_tris, rot_ext, _ = rotate_triangles_z(tris, rot_deg)
    intact_fits_nominal_bed = (
        rot_ext[0] <= bed[0] - 1e-3
        and rot_ext[1] <= bed[1] - 1e-3
        and rot_ext[2] <= bed[2] - 1e-3
    )
    margin_xy = [(bed[0] - rot_ext[0]) / 2.0, (bed[1] - rot_ext[1]) / 2.0]

    return {
        'source': str(source),
        'source_sha256': _sha256(source),
        'source_metrics': desc,
        'one_piece_diagonal_rotation_deg': rot_deg,
        'one_piece_diagonal_dimensions_mm': _r4(rot_ext),
        'one_piece_diagonal_margin_each_side_xy_mm': _r4(margin_xy),
        'one_piece_fits_nominal_bed': bool(intact_fits_nominal_bed),
        'prefer_intact_one_piece': bool(intact_fits_nominal_bed),
    }


def render_openscad_script(source_filename: str, plan: dict) -> str:
    """Synthesize a reproducible OpenSCAD script for the reviewed split plan."""
    plan = validate_split_plan_schema(plan)
    cut_x = float(plan.get('cut_coord_mm', 0.0))
    neg_box = plan.get('half_box_Negative') or [-150.0, -60.0, -2.0, 150.0, 120.0, 225.0]
    pos_box = plan.get('half_box_positive') or [cut_x, -60.0, -2.0, 150.0, 120.0, 225.0]
    bores = plan.get('bores') or []
    spacing_y = float(plan.get('plate_spacing_y_mm', 37.25))
    shift_x = float(plan.get('plate_shift_x_mm', 61.25))

    bore_lines = []
    for b in bores:
        d = float(b['bore_diameter_mm'])
        depth = float(b['depth_each_side_mm'])
        y = float(b['y_mm'])
        z = float(b['z_mm'])
        bore_lines.append(
            f'    translate([{cut_x - depth:.4f}, {y:.4f}, {z:.4f}]) '
            f'rotate([0, 90, 0]) cylinder(h={2.0 * depth:.4f}, d={d:.4f}, center=false);'
        )

    printable = [b for b in bores if b.get('printable_dowel', True)]
    first_pin = printable[0] if printable else bores[0]
    pin_d, pin_l = _pin_dimensions(first_pin)
    pin_ch = float(first_pin.get('pin_chamfer_mm', 0.8))
    pin_tip = float(first_pin.get('pin_tip_diameter_mm', 4.3))

    lines = [
        f'// Generated by Print Handy cad_split for plan: {plan["plan_id"]}',
        f'// {STRUCTURAL_WARNING}',
        'part = "left";',
        '$fn = 48;',
        f'DOWEL_D = {pin_d:.4f};',
        f'DOWEL_LENGTH = {pin_l:.4f};',
        f'PLATE_SPACING_Y = {spacing_y:.4f};',
        f'PLATE_SHIFT_X = {shift_x:.4f};',
        '',
        f'module source_model() {{ import("{source_filename}", convexity=20); }}',
        '',
        'module bores() {',
        *bore_lines,
        '}',
        '',
        'module half_cut(which) {',
        '  difference() {',
        '    intersection() {',
        '      source_model();',
        f'      if (which == "left") translate([{neg_box[0]:.4f}, {neg_box[1]:.4f}, {neg_box[2]:.4f}]) '
        f'cube([{neg_box[3]:.4f}, {neg_box[4]:.4f}, {neg_box[5]:.4f}]);',
        f'      if (which == "right") translate([{pos_box[0]:.4f}, {pos_box[1]:.4f}, {pos_box[2]:.4f}]) '
        f'cube([{pos_box[3]:.4f}, {pos_box[4]:.4f}, {pos_box[5]:.4f}]);',
        '    }',
        '    bores();',
        '  }',
        '}',
        '',
        'module dowel() {',
        f'  cylinder(h={pin_ch:.4f}, d1={pin_tip:.4f}, d2=DOWEL_D);',
        f'  translate([0, 0, {pin_ch:.4f}]) cylinder(h=DOWEL_LENGTH - {2.0 * pin_ch:.4f}, d=DOWEL_D);',
        f'  translate([0, 0, DOWEL_LENGTH - {pin_ch:.4f}]) cylinder(h={pin_ch:.4f}, d1=DOWEL_D, d2={pin_tip:.4f});',
        '}',
        '',
        f'module pin_row() {{ for (i=[0:{max(0, len(printable) - 1)}]) translate([i*18 - {0.5 * max(0, len(printable) - 1) * 18:.4f}, 0, 0]) dowel(); }}',
        'module on_plate_left() { translate([PLATE_SHIFT_X, -PLATE_SPACING_Y, 0]) half_cut("left"); }',
        'module on_plate_right() { translate([-PLATE_SHIFT_X, PLATE_SPACING_Y, 0]) half_cut("right"); }',
        '',
        'if (part == "left") half_cut("left");',
        'if (part == "right") half_cut("right");',
        'if (part == "pins") pin_row();',
        'if (part == "both_halves") { on_plate_left(); on_plate_right(); }',
        'if (part == "all_plate") { on_plate_left(); on_plate_right(); pin_row(); }',
        '',
    ]
    return '\n'.join(lines)


def _run_openscad(scad_path: Path, out_path: Path, defines: dict | None = None, timeout: int = 210):
    exe = shutil.which('openscad')
    if not exe:
        raise RuntimeError('openscad executable not found on PATH; cannot execute CSG boolean split')
    cmd = [exe]
    for k, v in (defines or {}).items():
        if isinstance(v, str):
            cmd.extend(['-D', f'{k}="{v}"'])
        else:
            cmd.extend(['-D', f'{k}={v}'])
    cmd.extend(['-o', str(out_path), '--export-format', 'binstl', str(scad_path)])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=scad_path.parent)
    combined_out = (proc.stdout or '') + '\n' + (proc.stderr or '')
    if (
        proc.returncode != 0
        or 'ERROR:' in combined_out
        or 'CGAL error' in combined_out
        or not out_path.is_file()
        or out_path.stat().st_size < 84
    ):
        raise RuntimeError(f'OpenSCAD failed on {scad_path.name} -> {out_path.name}:\n{combined_out.strip()}')


def verify_split_artifacts(
    *,
    whole_triangles: list,
    left_triangles: list,
    right_triangles: list,
    plan: dict,
) -> dict:
    """Verify post-boolean watertightness, cut-plane interface, bed fit, and volume conservation."""
    plan = validate_split_plan_schema(plan)
    bed = tuple(float(v) for v in (plan.get('bed_mm') or DEFAULT_BED_MM))
    cut_x = float(plan.get('cut_coord_mm', 0.0))

    whole_desc = describe_triangles(whole_triangles)
    left_desc = describe_triangles(left_triangles)
    right_desc = describe_triangles(right_triangles)

    for label, desc in (('left_half', left_desc), ('right_half', right_desc)):
        if not desc['watertight'] or not desc['winding_consistent']:
            raise ValueError(f'{label} is not watertight or has inconsistent winding after boolean split')
        if desc['shell_count'] != 1:
            raise ValueError(f'{label} has {desc["shell_count"]} shells, expected 1 connected solid')
        if any(desc['dimensions_mm'][a] > bed[a] + 1e-3 for a in range(3)):
            raise ValueError(f'{label} dimensions {desc["dimensions_mm"]} exceed nominal bed {list(bed)}')

    if abs(left_desc['bounds_max_mm'][0] - cut_x) > 1e-2 or abs(right_desc['bounds_min_mm'][0] - cut_x) > 1e-2:
        raise ValueError(
            f'Cut interface is not aligned with x={cut_x}: '
            f'left max_x={left_desc["bounds_max_mm"][0]}, right min_x={right_desc["bounds_min_mm"][0]}'
        )

    whole_vol = float(whole_desc['abs_volume_mm3'] or 0.0)
    left_vol = float(left_desc['abs_volume_mm3'] or 0.0)
    right_vol = float(right_desc['abs_volume_mm3'] or 0.0)
    measured_removed = whole_vol - (left_vol + right_vol)
    expected_removed = expected_bore_removal_volume_mm3(plan)
    vol_tol = float(plan.get('volume_conservation_tol_mm3', 35.0))
    if abs(measured_removed - expected_removed) > vol_tol:
        raise ValueError(
            f'Split modified unexpected material: measured removed volume {measured_removed:.2f} mm3 '
            f'vs expected bore volume {expected_removed:.2f} mm3 (tolerance {vol_tol:.2f} mm3)'
        )

    return {
        'whole_volume_mm3': round(whole_vol, 3),
        'left_volume_mm3': round(left_vol, 3),
        'right_volume_mm3': round(right_vol, 3),
        'measured_removed_volume_mm3': round(measured_removed, 3),
        'expected_bore_volume_mm3': round(expected_removed, 3),
        'volume_delta_mm3': round(abs(measured_removed - expected_removed), 3),
        'left_metrics': left_desc,
        'right_metrics': right_desc,
    }


def prepare_cad_split(
    source: Path,
    outdir: Path,
    plan_or_id: str | Path | dict,
    *,
    execute_openscad: bool = False,
) -> dict:
    """Prepare (and optionally execute via OpenSCAD) a reviewed CAD split plan for an oversized STL.

    Always writes:
    - The preferred intact rotated STL (`*_OnePiece_Diagonal45.stl`) when rotation fits the bed.
    - Printable chamfered dowel STL (`*_Printable_Dowels.stl`) when the plan includes printable dowels.
    - Reproducible OpenSCAD script (`*_split.scad`).
    - Machine-readable `split_validation.json` with the 5-stage `status_ladder` and structural warnings.

    When `execute_openscad=True`, invokes OpenSCAD to generate left/right/combined split STLs and
    runs `verify_split_artifacts` (watertightness, shell count, bounds, and volume conservation).
    """
    # Resolve both paths up front: the OpenSCAD subprocess runs with its
    # working directory changed to the scad's (temporary) directory, so a
    # relative output path would land there instead of the caller's outdir (P1-51).
    source = Path(source).resolve()
    outdir = Path(outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    plan = load_split_plan(plan_or_id)
    preflight = check_source_against_split_plan(source, plan)
    bed = tuple(float(v) for v in (plan.get('bed_mm') or DEFAULT_BED_MM))

    raw_tris = read_stl_triangles(source)
    rot_deg = float(plan.get('one_piece_z_rotation_deg', 45.0))
    rot_tris, rot_ext, _ = rotate_triangles_z(raw_tris, rot_deg)
    centered_one_piece = [
        [
            (
                x + (bed[0] - rot_ext[0]) / 2.0,
                y + (bed[1] - rot_ext[1]) / 2.0,
                z,
            )
            for x, y, z in tri
        ]
        for tri in rot_tris
    ]
    one_piece_path = outdir / f'{source.stem}_OnePiece_Diagonal{int(round(rot_deg))}.stl'
    write_binary_stl(centered_one_piece, one_piece_path)
    one_piece_desc = describe_triangles(read_stl_triangles(one_piece_path))

    dowel_tris = generate_dowel_row_triangles(plan)
    dowel_path = None
    dowel_desc = None
    if dowel_tris:
        dowel_path = outdir / f'{source.stem}_Printable_Dowels.stl'
        write_binary_stl(dowel_tris, dowel_path)
        dowel_desc = describe_triangles(read_stl_triangles(dowel_path))

    scad_path = outdir / f'{source.stem}_split.scad'
    scad_path.write_text(render_openscad_script(source.name, plan), encoding='utf-8')
    # The exported script imports the source by bare name, resolved relative
    # to the script: copy the source alongside it so the reproducible SCAD
    # artifact can actually load its model later (P2-47).
    copied_source = outdir / source.name
    if copied_source != source:
        shutil.copy2(source, copied_source)

    boolean_verification = None
    generated_split_files = {}
    if execute_openscad:
        with tempfile.TemporaryDirectory(prefix='pb_cad_split_') as tmp_str:
            tmp = Path(tmp_str)
            local_src = tmp / source.name
            shutil.copy2(source, local_src)
            local_scad = tmp / scad_path.name
            local_scad.write_text(scad_path.read_text(encoding='utf-8'), encoding='utf-8')

            if int(plan.get('expected_shells', 1)) == 2 and plan['plan_id'] == 'macmini_mount_a1347':
                shells = sorted(
                    split_disconnected_shells(raw_tris),
                    key=lambda sh: describe_triangles(sh)['abs_volume_mm3'] or 0.0,
                    reverse=True,
                )
                write_binary_stl(shells[0], tmp / 'main.stl')
                write_binary_stl(shells[1], tmp / 'sheet.stl')
                neg_box = plan['half_box_Negative']
                pos_box = plan['half_box_positive']
                for side, box in (('left', neg_box), ('right', pos_box)):
                    for sh_name in ('main', 'sheet'):
                        bore_Snippets = []
                        for b in plan['bores']:
                            target_sh = b.get('shell_target', 'both')
                            if target_sh not in ('both', sh_name):
                                continue
                            d = float(b['bore_diameter_mm'])
                            depth = float(b['depth_each_side_mm'])
                            y = float(b['y_mm'])
                            z = float(b['z_mm'])
                            bore_Snippets.append(
                                f'translate([0,{y},{z}]) rotate([0,90,0]) cylinder(h={2.0*depth},d={d},center=true);'
                            )
                        sh_scad = tmp / f'{side}_{sh_name}.scad'
                        sh_scad.write_text(
                            f'$fn=48;\ndifference() {{\n intersection() {{\n'
                            f'  import("{sh_name}.stl",convexity=10);\n'
                            f'  translate([{box[0]},{box[1]},{box[2]}]) cube([{box[3]},{box[4]},{box[5]}]);\n'
                            f' }}\n ' + '\n '.join(bore_Snippets) + '\n}\n',
                            encoding='utf-8',
                        )
                        _run_openscad(sh_scad, tmp / f'{side}_{sh_name}.stl')
                    u_scad = tmp / f'{side}_union.scad'
                    u_scad.write_text(
                        f'union() {{import("{side}_main.stl",convexity=10);import("{side}_sheet.stl",convexity=10);}}\n',
                        encoding='utf-8',
                    )
                    out_half = outdir / f'{source.stem}_{side.capitalize()}_Half.stl'
                    _run_openscad(u_scad, out_half)
                    generated_split_files[f'{side}_half'] = out_half.name
                w_scad = tmp / 'whole_union.scad'
                w_scad.write_text(
                    'union() {import("main.stl",convexity=10);import("sheet.stl",convexity=10);}\n',
                    encoding='utf-8',
                )
                whole_stl = tmp / 'whole_union.stl'
                _run_openscad(w_scad, whole_stl)
                whole_tris = read_stl_triangles(whole_stl)
                left_tris = read_stl_triangles(outdir / generated_split_files['left_half'])
                right_tris = read_stl_triangles(outdir / generated_split_files['right_half'])
            else:
                for part_key, suffix in (
                    ('left', 'Left_Half.stl'),
                    ('right', 'Right_Half.stl'),
                    ('both_halves', 'TwoHalves_OnePlate.stl'),
                ):
                    out_file = outdir / f'{source.stem}_{suffix}'
                    _run_openscad(local_scad, out_file, defines={'part': part_key})
                    generated_split_files[part_key] = out_file.name
                whole_tris = raw_tris
                left_tris = read_stl_triangles(outdir / generated_split_files['left'])
                right_tris = read_stl_triangles(outdir / generated_split_files['right'])

            boolean_verification = verify_split_artifacts(
                whole_triangles=whole_tris,
                left_triangles=left_tris,
                right_triangles=right_tris,
                plan=plan,
            )

    report = {
        'plan_id': plan['plan_id'],
        'title': plan.get('title', plan['plan_id']),
        'preflight': preflight,
        'one_piece_intact_stl': one_piece_path.name,
        'one_piece_intact_sha256': _sha256(one_piece_path),
        'one_piece_intact_metrics': one_piece_desc,
        'openscad_script': scad_path.name,
        'openscad_script_sha256': _sha256(scad_path),
        'printable_dowels_stl': dowel_path.name if dowel_path else None,
        'printable_dowels_metrics': dowel_desc,
        'expected_bore_removal_volume_mm3': round(expected_bore_removal_volume_mm3(plan), 3),
        'openscad_executed': bool(execute_openscad),
        'generated_split_files': generated_split_files,
        'boolean_verification': boolean_verification,
        'hardware_recommendation': plan.get('hardware_recommendation'),
        'structural_warning': STRUCTURAL_WARNING,
        'status_ladder': build_status_ladder(
            geometry_inspected=True,
            geometry_nominal_fit=bool(preflight['one_piece_fits_nominal_bed'] or boolean_verification is not None),
        ),
    }
    (outdir / 'split_validation.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report
