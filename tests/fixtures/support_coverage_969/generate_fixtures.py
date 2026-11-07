#!/usr/bin/env python3
"""Generate the SYNTHETIC fixtures for issue #969.

These fixtures are NOT the real archived intakes. The real archives
(724a5b63... single hook, 16fd2142... 40 hooks + 40 backs) live on
catduck's Unraid/Uranium host, which was unreachable from the build VM
when this was written (Tailscale approval expired; SSH probe timed
out). The fixtures below faithfully reproduce the two scenarios exactly
as documented in issues #963 and #969, in the marker and feature-type
format OrcaSlicer stable actually emits, so the grading in #966 is
pinned against them. The real-archive tasks were skipped at catduck's direction
(2026-11-06); scripts/probe_support_coverage.py remains available if
catduck ever wants the real run and a fixture swap.

Regenerate with:  python3 generate_fixtures.py   (from this directory)
Output is deterministic; do not hand-edit the generated files.
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

HEADER = [
    '; SYNTHETIC FIXTURE for issue #969 - not a real archived intake.',
    '; Format follows OrcaSlicer stable output (object markers repeat',
    '; once per layer per instance, paired with EXCLUDE_OBJECT markers).',
    '; support_type = tree(auto)',
    '; support_interface_speed = 25',
]


def obj_block(name, oid, copy_no, ox, oy, x1, y1, z):
    """One layer's worth of one instance, in OrcaSlicer's marker style."""
    return [
        f'; printing object {name} id:{oid} copy {copy_no}',
        f'EXCLUDE_OBJECT_START NAME={name}_id_{oid}_copy_{copy_no}',
        ';TYPE:Outer wall',
        f'G1 Z{z:.2f}',
        f'G1 X{ox:.2f} Y{oy:.2f} E1.0',
        f'G1 X{x1:.2f} Y{y1:.2f} E1.0',
        f'EXCLUDE_OBJECT_END NAME={name}_id_{oid}_copy_{copy_no}',
        f'; stop printing object {name} id:{oid} copy {copy_no}',
    ]


def cell_segment(cx, cy, z):
    """A short extruding segment inside one 3 mm grading cell."""
    return f'G1 X{cx - 0.05:.2f} Y{cy:.2f}\nG1 X{cx + 0.05:.2f} Y{cy:.2f} E0.1'


def write_case(dirname, gcode_lines, bounds, regions, expected, readme):
    case = HERE / dirname
    case.mkdir(parents=True, exist_ok=True)
    (case / 'plate_1.gcode').write_text('\n'.join(gcode_lines) + '\n')
    (case / 'bounds.json').write_text(json.dumps(bounds, indent=2) + '\n')
    (case / 'regions.json').write_text(json.dumps(regions, indent=2) + '\n')
    (case / 'expected.json').write_text(json.dumps(expected, indent=2) + '\n')
    (case / 'README.md').write_text(readme)


# ---------------------------------------------------------- single hook case
def single_hook():
    # Source-space object at (10, 10) .. (22, 25): a 12 x 15 mm bbox =
    # 20 grading cells of 3 mm, matching the shape of the #963 evidence
    # (148 mm2 bbox in the real intake; scaled here, ratios preserved).
    lines = list(HEADER)
    # Three layers; the last layer is only the hook's tip, at one corner.
    # Orca repeats the markers per layer, so a correct instance pass
    # must merge these three blocks into ONE instance with union bounds.
    lines += obj_block('Hook', 0, 0, 10, 10, 22, 25, 0.2)
    lines += obj_block('Hook', 0, 0, 10, 10, 22, 25, 0.6)
    lines += obj_block('Hook', 0, 0, 18, 21, 22, 25, 1.0)
    # Tree supports: the interface layer is what touches the underside.
    # It passes through 16 of the 20 bbox cells - every cell except the
    # four the #963 evidence named as holding no underside:
    # (0,0), (2,4), (3,3), (3,4). The underside footprint occupies the
    # 10 cells of the bbox's left half, and all 10 are covered.
    lines.append(';TYPE:Support interface')
    lines.append('G1 Z1.00')
    uncovered = {(0, 0), (2, 4), (3, 3), (3, 4)}
    for i in range(4):
        for j in range(5):
            if (i, j) in uncovered:
                continue
            lines.append(cell_segment(10 + (i + 0.5) * 3.0, 10 + (j + 0.5) * 3.0, 1.0))
    # A branch (non-interface) segment through an uncovered cell: for a
    # tree slice it must NOT count toward coverage.
    lines.append(';TYPE:Support')
    lines.append('G1 Z1.00')
    lines.append(cell_segment(10 + 0.5 * 3.0, 10 + 0.5 * 3.0, 1.0))

    # Footprint: the 10 grading cells that hold underside - the left
    # half of the box minus (0,0), plus (2,0). Every one of them is
    # interface-covered; the four uncovered box cells hold no underside.
    fp_cells = [(0, 1), (0, 2), (0, 3), (0, 4), (1, 0),
                (1, 1), (1, 2), (1, 3), (1, 4), (2, 0)]
    footprint = [[10 + gi * 3 + 0.5 + dx, 10 + gj * 3 + 0.5 + dy, 1.0]
                 for (gi, gj) in fp_cells
                 for dx in (0, 1, 2) for dy in (0, 1, 2)]
    # The sub-threshold low_clearance region from the #963 evidence:
    # 3 footprint grading cells, 2 of them uncovered in the real intake.
    # It was never flagged (below the area/span finding thresholds), so
    # #966 does not grade it; it is named in excluded_targets instead.
    low_fp = [[10 + i + 0.5 + dx, 10 + 0.5 + dy, 1.0]
              for i in (0, 3, 6) for dx in (0, 1, 2) for dy in (0, 1, 2)]
    regions = [
        {'kind': 'supportable_overhang', 'object': 'Hook', 'part': 'Hook',
         'bbox_mm': [[10.0, 10.0], [22.0, 25.0]],
         'z_mm': {'min': 1.2, 'max': 1.2},
         'footprint': footprint, 'footprint_mm': 1.0},
        {'kind': 'low_clearance', 'object': 'Hook', 'part': 'Hook',
         'bbox_mm': [[10.0, 10.0], [19.0, 13.0]],
         'z_mm': {'min': 0.5, 'max': 0.5}, 'flagged': False,
         'footprint': low_fp, 'footprint_mm': 1.0},
    ]
    expected = {
        'synthetic': True,
        'scenario': 'Issue #963 evidence case (intake 724a5b63...): '
                    'bbox grading read 80 percent; footprint recount 10 of 10.',
        'object_instances': {'Hook': 1},
        'ok': True,
        'regions': [
            {'part': 'Hook', 'kind': 'supportable_overhang', 'graded': 'footprint',
             'total_cells': 10, 'covered_cells': 10, 'coverage': 1.0},
        ],
        'bbox_method_coverage': 0.8,
        'excluded_targets': ['Hook'],
        'unverified': 0,
    }
    readme = (
        '# SYNTHETIC fixture - single hook (issue #969)\n\n'
        'This is NOT the real archived intake `724a5b63...`. That archive lives on\n'
        "catduck's Unraid/Uranium host and was unreachable from the build VM when\n"
        'this fixture was made. The geometry reproduces the scenario documented in\n'
        'issue #963: a 12 x 15 mm bounding box (20 grading cells of 3 mm) whose\n'
        'underside footprint occupies 10 cells; the support interface passes\n'
        'through 16 of the 20 box cells (80 percent by the old bounding-box\n'
        'method) and through all 10 footprint cells (10 of 10). The four box\n'
        'cells with no interface are exactly the four #963 named as holding no\n'
        'underside: (0,0), (2,4), (3,3), (3,4). The unflagged low_clearance\n'
        'region from the same evidence is present and must be excluded from\n'
        'grading, not graded. Object markers repeat per layer in OrcaSlicer\n'
        'style, and the last layer is only the hook tip, so instance merging\n'
        'is exercised. Regenerate with ../generate_fixtures.py; do not hand-edit.\n')
    write_case('single_hook_synthetic', lines,
               {'source_bounds': [10.0, 10.0, 22.0, 25.0]}, regions, expected, readme)


# ------------------------------------------------------- 40 + 40 plate case
def plate_40():
    lines = list(HEADER)
    hooks = [(5 + 20 * c, 5 + 18 * r) for r in range(5) for c in range(8)]
    backs = [(5 + 20 * c, 95 + 18 * r) for r in range(5) for c in range(8)]
    # Two full layers per instance, plus a small tip layer for the first
    # three hooks: 80 instances must come out of 163 marker blocks.
    for layer, z in enumerate((0.2, 2.6)):
        for k, (ox, oy) in enumerate(hooks):
            lines += obj_block('Hook', 0, k, ox, oy, ox + 9, oy + 9, z)
        for k, (ox, oy) in enumerate(backs):
            lines += obj_block('Back', 1, k, ox, oy, ox + 9, oy + 9, z)
    for k in range(3):
        ox, oy = hooks[k]
        lines += obj_block('Hook', 0, k, ox + 6, oy + 6, ox + 9, oy + 9, 4.6)
    cells9 = [(i, j) for i in range(3) for j in range(3)]
    # Hooks (supportable_overhang at Z 5.0, tree supports): the interface
    # reaches 3 of 9 cells on even copies and 4 of 9 on odd copies -
    # 33-44 percent, inside the 25-50 percent band #969 records for the
    # real plate's low_clearance failures. Every hook region fails.
    lines.append(';TYPE:Support interface')
    lines.append('G1 Z4.80')
    for k, (ox, oy) in enumerate(hooks):
        for (i, j) in cells9[:3 if k % 2 == 0 else 4]:
            lines.append(cell_segment(ox + (i + 0.5) * 3.0, oy + (j + 0.5) * 3.0, 4.8))
    # Backs (flagged low_clearance at Z 0.6): the interface reaches 7 of
    # 9 cells on every copy - 77.8 percent, inside the 75-80 percent
    # band #969 records for the real plate's supportable_overhang
    # failures. Every back region fails.
    lines.append('G1 Z0.40')
    for k, (ox, oy) in enumerate(backs):
        for (i, j) in cells9[:7]:
            lines.append(cell_segment(ox + (i + 0.5) * 3.0, oy + (j + 0.5) * 3.0, 0.4))

    regions = [
        {'kind': 'supportable_overhang', 'object': 'Hook', 'part': 'Hook',
         'bbox_mm': [[0.0, 0.0], [9.0, 9.0]], 'z_mm': {'min': 5.0, 'max': 5.0}},
        {'kind': 'low_clearance', 'object': 'Back', 'part': 'Back', 'flagged': True,
         'bbox_mm': [[0.0, 0.0], [9.0, 9.0]], 'z_mm': {'min': 0.6, 'max': 0.6}},
    ]
    expected = {
        'synthetic': True,
        'scenario': 'Issue #969 plate case (intake 16fd2142...): 40 hooks + 40 '
                    'backs on one plate; every region failed - low_clearance at '
                    '25-50 percent, supportable_overhang at 75-80 percent.',
        'object_instances': {'Hook': 40, 'Back': 40},
        'ok': False,
        'graded_regions': 80,
        'hook_coverages': [0.3333, 0.4444],
        'back_coverages': [0.7778],
        'unverified': 0,
    }
    readme = (
        '# SYNTHETIC fixture - 40 hooks + 40 backs (issue #969)\n\n'
        'This is NOT the real archived intake `16fd2142...`. That archive lives on\n'
        "catduck's Unraid/Uranium host and was unreachable from the build VM when\n"
        'this fixture was made. The plate reproduces the scenario documented in\n'
        'issue #969: 40 copies of one object (Hook) and 40 copies of another\n'
        '(Back) on one plate, every region failing coverage - hooks at 33 and\n'
        '44 percent (inside the recorded 25-50 percent band) and backs at\n'
        '77.8 percent (inside the recorded 75-80 percent band). Object markers\n'
        'repeat per layer in OrcaSlicer style: 163 marker blocks must resolve\n'
        'to exactly 80 instances, none of them unverified. Regenerate with\n'
        '../generate_fixtures.py; do not hand-edit.\n')
    write_case('plate_40_hooks_40_backs_synthetic', lines,
               {'source_bounds': [0.0, 0.0, 9.0, 9.0]}, regions, expected, readme)


if __name__ == '__main__':
    single_hook()
    plate_40()
    print('fixtures written')
