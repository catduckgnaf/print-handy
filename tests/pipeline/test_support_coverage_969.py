"""Issue #969: validate #966 coverage grading against the archived intakes.

The real archives (724a5b63... and 16fd2142...) live on catduck's
Unraid/Uranium host, which was unreachable from the build VM when this
was written, so the committed fixtures under
tests/fixtures/support_coverage_969/ are SYNTHETIC reproductions of the
two documented scenarios - clearly labeled, generated deterministically
by generate_fixtures.py in that directory. They are written in the
marker and feature-type format OrcaSlicer stable actually emits on the
AD5X:

- ``; printing object <Name> id:<id> copy <n>`` ... ``; stop printing
  object ...``, repeated once per layer per instance (not once per
  print), paired with ``EXCLUDE_OBJECT_START NAME=<Name>_id_<id>_copy_<n>``
  / ``EXCLUDE_OBJECT_END`` markers;
- ``;TYPE:Support`` / ``;TYPE:Support interface`` feature sections and
  a ``; support_type = tree(auto)`` settings comment.

scripts/probe_support_coverage.py runs the same grading against a real
archive folder when the host is reachable.
"""
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

from print_handy.core import support_clearance

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'support_coverage_969'
REPO = Path(__file__).resolve().parents[2]


def _run_case(dirname):
    case = FIXTURES / dirname
    text = (case / 'plate_1.gcode').read_text()
    regions = json.loads((case / 'regions.json').read_text())
    bounds = tuple(json.loads((case / 'bounds.json').read_text())['source_bounds'])
    expected = json.loads((case / 'expected.json').read_text())
    result = support_clearance.verify_coverage_by_plate(
        regions, [{'gcode': text, 'source_bounds': bounds}])
    objects = support_clearance.gcode_object_bounds(text)
    return result, expected, objects, text, regions, bounds


# ------------------------------------------- task 1 + 4: the two intakes

def test_single_hook_fixture_grades_10_of_10_on_footprint():
    result, expected, objects, _text, _regions, _bounds = _run_case('single_hook_synthetic')
    assert expected['synthetic'] is True
    # Three per-layer marker blocks merge into one instance.
    assert Counter(o['name'] for o in objects) == Counter(expected['object_instances'])
    assert result['ok'] is expected['ok']
    assert result['unverified'] == []
    assert len(result['regions']) == len(expected['regions'])
    for got, want in zip(result['regions'], expected['regions']):
        for key, value in want.items():
            assert got[key] == value, (key, got, want)
    # The unflagged low_clearance region is excluded, not graded.
    assert [e['part'] for e in result['excluded_targets']] == expected['excluded_targets']


def test_single_hook_fixture_bbox_method_reproduces_the_80_percent():
    _result, expected, _objects, text, regions, bounds = _run_case('single_hook_synthetic')
    stripped = [dict(r, footprint=None) for r in regions
                if r['kind'] == 'supportable_overhang']
    result = support_clearance.verify_support_coverage(
        stripped, text, source_bounds=bounds,
        object_bounds=support_clearance.gcode_object_bounds(text))
    assert result['regions'][0]['graded'] == 'bbox'
    assert result['regions'][0]['coverage'] == expected['bbox_method_coverage']


def test_plate_40_fixture_attributes_all_80_copies_and_grades_each():
    result, expected, objects, _text, _regions, _bounds = _run_case(
        'plate_40_hooks_40_backs_synthetic')
    assert expected['synthetic'] is True
    # 163 per-layer marker blocks resolve to exactly 80 instances:
    # no copy falls to unverified, and none is graded more than once.
    assert Counter(o['name'] for o in objects) == Counter(expected['object_instances'])
    assert result['unverified'] == []
    assert len(result['regions']) == expected['graded_regions']
    assert result['ok'] is expected['ok']
    hooks = sorted({r['coverage'] for r in result['regions'] if r['part'] == 'Hook'})
    backs = sorted({r['coverage'] for r in result['regions'] if r['part'] == 'Back'})
    assert hooks == expected['hook_coverages']
    assert backs == expected['back_coverages']
    assert all(r['mapping'] == 'object_instance' for r in result['regions'])


# --------------------------------- task 2: OrcaSlicer stable marker format

def _orca_block(name, oid, copy_no, ox, oy, x1, y1, z, exclude=True):
    lines = [f'; printing object {name} id:{oid} copy {copy_no}']
    if exclude:
        lines.append(f'EXCLUDE_OBJECT_START NAME={name}_id_{oid}_copy_{copy_no}')
    lines += [';TYPE:Outer wall', f'G1 Z{z:.2f}',
              f'G1 X{ox} Y{oy} E1', f'G1 X{x1} Y{y1} E1']
    if exclude:
        lines.append(f'EXCLUDE_OBJECT_END NAME={name}_id_{oid}_copy_{copy_no}')
    lines.append(f'; stop printing object {name} id:{oid} copy {copy_no}')
    return lines


def test_orca_per_layer_blocks_merge_into_one_instance_per_copy():
    lines = []
    for layer, z in enumerate((0.2, 2.6, 5.0)):
        for copy_no, ox in enumerate((0, 30)):
            # The last layer of each copy is only a small tip feature.
            span = (7, 7, 9, 9) if layer == 2 else (0, 0, 9, 9)
            lines += _orca_block('Hook', 0, copy_no, ox + span[0], span[1],
                                 ox + span[2], span[3], z)
    objects = support_clearance.gcode_object_bounds('\n'.join(lines) + '\n')
    assert [o['bounds'] for o in objects] == [(0.0, 0.0, 9.0, 9.0), (30.0, 0.0, 39.0, 9.0)]
    assert [o['name'] for o in objects] == ['Hook', 'Hook']


def test_orca_exclude_object_only_markers_merge_by_name_suffix():
    lines = []
    for layer in range(2):
        for copy_no, ox in enumerate((0, 30)):
            lines += [f'EXCLUDE_OBJECT_START NAME=Hook_id_0_copy_{copy_no}',
                      ';TYPE:Outer wall', f'G1 X{ox} Y0 E1', f'G1 X{ox + 9} Y9 E1',
                      f'EXCLUDE_OBJECT_END NAME=Hook_id_0_copy_{copy_no}']
    objects = support_clearance.gcode_object_bounds('\n'.join(lines) + '\n')
    assert [o['bounds'] for o in objects] == [(0.0, 0.0, 9.0, 9.0), (30.0, 0.0, 39.0, 9.0)]


def test_fully_supported_copies_pass_despite_small_tip_layers():
    lines = ['; support_type = tree(auto)']
    for copy_no, ox in enumerate((0, 30)):
        lines += _orca_block('Hook', 0, copy_no, ox, 0, ox + 9, 9, 0.2)
        lines += _orca_block('Hook', 0, copy_no, ox + 7, 7, ox + 9, 9, 5.0)
    lines.append(';TYPE:Support interface')
    lines.append('G1 Z4.80')
    for ox in (0, 30):
        for i in range(3):
            for j in range(3):
                lines.append(f'G1 X{ox + 1.5 + 3 * i:.1f} Y{1.5 + 3 * j:.1f} E1.0')
    text = '\n'.join(lines) + '\n'
    region = {'kind': 'supportable_overhang', 'object': 'Hook', 'part': 'Hook',
              'bbox_mm': [[0.0, 0.0], [9.0, 9.0]], 'z_mm': {'min': 5.0, 'max': 5.0}}
    result = support_clearance.verify_support_coverage(
        [region], text, source_bounds=(0.0, 0.0, 9.0, 9.0),
        object_bounds=support_clearance.gcode_object_bounds(text))
    assert result['ok'] is True
    assert len(result['regions']) == 2
    assert all(r['coverage'] == 1.0 for r in result['regions'])


# --------------------------- task 3: tree supports, mixed interface presence

def _mixed_tree_gcode(header, with_any_interface=True):
    lines = [header, ';TYPE:Outer wall', 'G1 X0 Y0 E1', 'G1 X24 Y3 E1']
    if with_any_interface:
        # Region A (X 0-12) has an interface layer; region B (X 12-24)
        # has only branch (Support) paths under it.
        lines += [';TYPE:Support interface', 'G1 Z9.80',
                  'G1 X0 Y1.5', 'G1 X11.9 Y1.5 E5']
    lines += [';TYPE:Support', 'G1 Z9.80',
              'G1 X0 Y1.5', 'G1 X24 Y1.5 E5']
    return '\n'.join(lines) + '\n'


_MIXED_REGIONS = [
    {'kind': 'supportable_overhang', 'part': 'A',
     'bbox_mm': [[0.0, 0.0], [12.0, 3.0]], 'z_mm': {'min': 10.0, 'max': 10.0}},
    {'kind': 'supportable_overhang', 'part': 'B',
     'bbox_mm': [[12.0, 0.0], [24.0, 3.0]], 'z_mm': {'min': 10.0, 'max': 10.0}},
]


def test_tree_mixed_interface_grades_each_region_on_the_interface():
    # In a tree slice the interface is the only part of the support that
    # touches the underside. Region A, which has one, passes; region B,
    # reached only by branches with no interface under it, does not -
    # branches alone do not hold an underside up in a tree slice.
    result = support_clearance.verify_support_coverage(
        _MIXED_REGIONS, _mixed_tree_gcode('; support_type = tree(auto)'))
    by_part = {r['part']: r for r in result['regions']}
    assert by_part['A']['coverage'] == 1.0
    assert by_part['A']['acceptance_basis'] == 'interface'
    assert by_part['B']['coverage'] == 0.0
    assert result['ok'] is False


def test_tree_slice_with_no_interface_anywhere_falls_back_to_all_support():
    result = support_clearance.verify_support_coverage(
        _MIXED_REGIONS, _mixed_tree_gcode('; support_type = tree(auto)',
                                           with_any_interface=False))
    assert result['ok'] is True
    assert result['regions'][0]['acceptance_basis'] == 'all_support_fallback'


def test_grid_slice_counts_support_paths_for_both_regions():
    result = support_clearance.verify_support_coverage(
        _MIXED_REGIONS, _mixed_tree_gcode('; support_type = normal(auto)'))
    assert result['ok'] is True
    assert result['regions'][0]['acceptance_basis'] == 'all_support'


# ------------------------------------------------------- probe script (task 1)

def test_probe_script_runs_against_a_fixture_folder():
    proc = subprocess.run(
        [sys.executable, str(REPO / 'scripts' / 'probe_support_coverage.py'),
         str(FIXTURES / 'single_hook_synthetic')],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report['ok'] is True
    graded = [r for r in report['regions'] if r['kind'] == 'supportable_overhang']
    assert len(graded) == 1 and graded[0]['coverage'] == 1.0
    assert report['unverified'] == []
