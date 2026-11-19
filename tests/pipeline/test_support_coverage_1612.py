"""Issue #1612: an unlabeled multi-part plate must not grade 0%.

The AD5X Orca template has ``gcode_label_objects`` off, so a sliced
plate carries no object markers and ``gcode_object_bounds`` returns
nothing. ``verify_coverage_by_plate`` then mapped every region from
the WHOLE plate's bounds: the combined source bounding box stretched
onto the combined sliced bounding box. Orca rearranges the parts on
the plate, so the mapped regions land beside the real parts and grade
0% / 33% even though support exists under the real footprints.

The fix: when per-part attribution is impossible (no object labels on
a plate whose source has more than one part), the result is a named
``unverified`` entry instead of a confident grade -- the intake flow
already routes an unverified result to the Print anyway / Cancel
question (#968), so nothing hard-blocks and nothing passes silently.
Single-part and labeled plates grade exactly as before.
"""
from print_handy.core import support_clearance

# Source layout: Shell at X 0-9, Plunger at X 12-21, both Y 0-9.
SOURCE_BOUNDS = (0.0, 0.0, 21.0, 9.0)

REGIONS = [
    {'kind': 'supportable_overhang', 'object': 'Shell', 'part': 'Shell',
     'bbox_mm': [[0.0, 0.0], [9.0, 9.0]], 'z_mm': {'min': 5.0, 'max': 5.0}},
    {'kind': 'supportable_overhang', 'object': 'Plunger', 'part': 'Plunger',
     'bbox_mm': [[12.0, 0.0], [21.0, 9.0]], 'z_mm': {'min': 5.0, 'max': 5.0}},
]


def _tiles(ox, oy):
    return [(ox + 1.5 + 3 * i, oy + 1.5 + 3 * j) for i in range(3) for j in range(3)]


def _rearranged_unlabeled_gcode():
    """The slice swapped the parts: Plunger sits at X 100-109, Shell at X 130-139
    (both Y 100-109), support tiles both real footprints, and there are
    no object markers anywhere."""
    lines = [';TYPE:Outer wall',
             'G1 X100 Y100 E1', 'G1 X139 Y109 E1',
             ';TYPE:Support', 'G1 Z4.8']
    for points in (_tiles(100, 100), _tiles(130, 100)):
        lines.append(f'G1 X{points[0][0]:.1f} Y{points[0][1]:.1f}')  # travel, no extrusion
        lines += [f'G1 X{x:.1f} Y{y:.1f} E1.0' for x, y in points]
    return '\n'.join(lines) + '\n'


def test_rearranged_unlabeled_multi_part_plate_is_unverified_never_zero():
    result = support_clearance.verify_coverage_by_plate(
        REGIONS, [{'gcode': _rearranged_unlabeled_gcode(), 'source_bounds': SOURCE_BOUNDS}])
    # Never a confident grade: no region is reported at 0% (or any %).
    assert result['regions'] == []
    assert result['checked'] is False
    assert result['ok'] is True
    assert len(result['unverified']) == 1
    reason = result['unverified'][0]['reason']
    assert 'no object labels' in reason
    assert 'unverified' in reason


def test_source_part_count_alone_makes_a_single_target_unverified():
    """Production passes the source's part count (#1612): even when only
    one part carries a coverage target, a multi-part source on an
    unlabeled plate cannot be attributed, because the whole-plate
    mapping is built from every part's combined bounds."""
    result = support_clearance.verify_coverage_by_plate(
        REGIONS[:1], [{'gcode': _rearranged_unlabeled_gcode(),
                       'source_bounds': SOURCE_BOUNDS, 'source_part_count': 3}])
    assert result['regions'] == []
    assert result['checked'] is False and result['ok'] is True
    assert len(result['unverified']) == 1
    assert 'no object labels' in result['unverified'][0]['reason']


def test_unlabeled_multi_plate_fallback_is_unverified_for_multi_part():
    plate = {'gcode': _rearranged_unlabeled_gcode(), 'source_bounds': SOURCE_BOUNDS}
    result = support_clearance.verify_coverage_by_plate(REGIONS, [plate, dict(plate)])
    assert result['regions'] == []
    assert result['checked'] is False and result['ok'] is True
    assert result['unverified']
    assert 'no object labels' in result['unverified'][0]['reason']


def test_single_part_unlabeled_plate_still_grades():
    """One part on an unlabeled plate: the whole-plate mapping is that
    part's own mapping (a translation), so grading is unchanged."""
    region = {'kind': 'supportable_overhang', 'object': 'P', 'part': 'P',
              'bbox_mm': [[0.0, 0.0], [9.0, 9.0]], 'z_mm': {'min': 5.0, 'max': 5.0}}
    lines = [';TYPE:Outer wall', 'G1 X50 Y60 E1', 'G1 X59 Y69 E1',
             ';TYPE:Support', 'G1 Z4.8']
    lines += [f'G1 X{x:.1f} Y{y:.1f} E1.0' for x, y in _tiles(50, 60)]
    result = support_clearance.verify_coverage_by_plate(
        [region], [{'gcode': '\n'.join(lines) + '\n', 'source_bounds': (0.0, 0.0, 9.0, 9.0)}])
    assert result['unverified'] == []
    assert result['checked'] is True and result['ok'] is True
    assert result['regions'][0]['coverage'] == 1.0


def test_labeled_multi_part_rearranged_plate_still_grades_per_instance():
    """Object labels keep the per-instance grading: the same swap as
    the unlabeled case, but each part is graded against its own slice
    bounds and passes, exactly as before this change."""
    lines = ['; support_type = tree(auto)']
    for name, ox in (('Plunger', 100), ('Shell', 130)):
        lines += [f'; printing object {name}', ';TYPE:Outer wall',
                  f'G1 X{ox} Y100 E1', f'G1 X{ox + 9} Y109 E1',
                  f'; stop printing object {name}']
    lines += [';TYPE:Support interface', 'G1 Z4.8']
    for points in (_tiles(100, 100), _tiles(130, 100)):
        lines.append(f'G1 X{points[0][0]:.1f} Y{points[0][1]:.1f}')
        lines += [f'G1 X{x:.1f} Y{y:.1f} E1.0' for x, y in points]
    text = '\n'.join(lines) + '\n'
    regions = [dict(r, bbox_mm=[[0.0, 0.0], [9.0, 9.0]]) for r in REGIONS]
    result = support_clearance.verify_coverage_by_plate(
        regions, [{'gcode': text, 'source_bounds': (0.0, 0.0, 9.0, 9.0)}])
    assert result['unverified'] == []
    assert result['checked'] is True and result['ok'] is True
    assert len(result['regions']) == 2
    assert all(r['coverage'] == 1.0 for r in result['regions'])
    assert all(r['mapping'] == 'object_instance' for r in result['regions'])


def _labeled_plate(name, ox):
    """One labeled plate: a single part at X ox..ox+9, its underside tiled with interface."""
    lines = ['; support_type = tree(auto)', f'; printing object {name}', ';TYPE:Outer wall',
             f'G1 X{ox} Y100 E1', f'G1 X{ox + 9} Y109 E1', f'; stop printing object {name}',
             ';TYPE:Support interface', 'G1 Z4.8']
    points = _tiles(ox, 100)
    lines.append(f'G1 X{points[0][0]:.1f} Y{points[0][1]:.1f}')
    lines += [f'G1 X{x:.1f} Y{y:.1f} E1.0' for x, y in points]
    return '\n'.join(lines) + '\n'


def test_labeled_multi_plate_multi_part_input_still_grades():
    """#1619 review: a multi-part model split over labeled plates is graded per plate."""
    regions = [dict(r, bbox_mm=[[0.0, 0.0], [9.0, 9.0]]) for r in REGIONS]
    plates = [{'gcode': _labeled_plate('Shell', 100), 'source_bounds': (0.0, 0.0, 9.0, 9.0)},
              {'gcode': _labeled_plate('Plunger', 130), 'source_bounds': (0.0, 0.0, 9.0, 9.0)}]
    result = support_clearance.verify_coverage_by_plate(regions, plates)
    assert result['plate_mode'] == 'per_plate'
    assert result['unverified'] == []
    assert result['checked'] is True and result['ok'] is True
    assert sorted(r['part'] for r in result['regions']) == ['Plunger', 'Shell']


def test_mixed_plates_grade_the_labeled_plate_and_name_the_unlabeled_one():
    """#1619 review: a labeled plate is still graded when another plate has no labels."""
    regions = [dict(r, bbox_mm=[[0.0, 0.0], [9.0, 9.0]]) for r in REGIONS]
    plates = [{'gcode': _labeled_plate('Shell', 100), 'source_bounds': (0.0, 0.0, 9.0, 9.0)},
              {'gcode': _rearranged_unlabeled_gcode(), 'source_bounds': SOURCE_BOUNDS}]
    result = support_clearance.verify_coverage_by_plate(regions, plates)
    assert result['plate_mode'] == 'per_plate_partial'
    assert result['checked'] is True and result['ok'] is True
    assert [r['part'] for r in result['regions']] == ['Shell']
    reasons = [u['reason'] for u in result['unverified']]
    assert any('no object labels' in r for r in reasons)
    assert any('region Plunger matched no object instance' in r for r in reasons)
