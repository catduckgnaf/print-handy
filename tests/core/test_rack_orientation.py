"""Offline orientation regressions; slicer verification boundary is stubbed explicitly."""
from pathlib import Path
from unittest.mock import patch
import math
import struct

import pytest

from print_handy.core import orca_adapter as oa


def test_quarter_turn_on_rectangular_bed_is_not_discarded():
    # An authored 210 x 100 footprint cannot fit the 120 mm X dimension,
    # but yaw 90 fits at 100% without changing the bed-contact face.
    points = [(x, y, z) for x in (0., 210.) for y in (0., 100.) for z in (0., 10.)]
    tris = [(points[0], points[1], points[2])]
    state = {'bed_mm': [120, 220], 'max_print_height_mm': 20,
             'resolved_hop_types': ['Normal Lift']}
    with patch.object(oa, 'source_triangles', return_value=tris), \
         patch.object(oa, '_unique_points', return_value=points), \
         patch.object(oa, '_surface_stats', return_value=(1000, 0)), \
         patch.object(oa, 'orca_plate_capacity', return_value=1) as capacity:
        rows = oa.rotation_alternatives(Path('rectangular.stl'), state, authored_max=0.55)
    assert rows, 'A feasible 90-degree bed turn must reach slicer verification'
    assert rows[0]['axis'] == '+Z'
    assert rows[0]['full_size']
    assert not rows[0]['supports']
    assert capacity.called


def test_explicit_brim_is_included_in_rotation_verification():
    # A user-requested brim must constrain rotated options, not just resize options.
    state = {'bed_mm': [220, 220], 'max_print_height_mm': 220,
             'resolved_hop_types': ['Normal Lift'], 'brim_override': 8.0}
    verified_states = []

    def rotations(source, checked_state, authored_max):
        verified_states.append(checked_state)
        return []

    with patch.object(oa, 'verified_scale_choices', return_value=[0.8, 0.65, 0.5]), \
         patch.object(oa, 'rotation_alternatives', side_effect=rotations):
        oa.fit_options(Path('rack.stl'), state)
    assert oa.required_feature_edge(verified_states[0]) == 8.0


def test_full_size_bed_turn_precedes_equally_fitting_face_change():
    points = [(x, y, z) for x in (0., 245.) for y in (0., 44.5) for z in (0., 20.)]
    state = {'bed_mm': [220, 220], 'max_print_height_mm': 220,
             'resolved_hop_types': ['Normal Lift']}
    with patch.object(oa, 'source_triangles', return_value=[(points[0], points[1], points[2])]), \
         patch.object(oa, '_unique_points', return_value=points), \
         patch.object(oa, '_surface_stats', return_value=(4500, 0)), \
         patch.object(oa, 'orca_plate_capacity', return_value=1):
        rows = oa.rotation_alternatives(Path('rack.stl'), state, authored_max=.85)
    assert rows[0]['full_size']
    assert rows[0]['axis'] == '+Z', 'Preserve the authored bed-contact face when full size already fits'


def test_typed_full_size_selects_unique_verified_rotation():
    from print_handy.intake.fit_flow import _fit_choice_reply
    rows = [{'kind': 'rotate', 'scale': 1.0}, {'kind': 'scale', 'scale': .85},
            {'kind': 'cancel'}]
    assert _fit_choice_reply('full size', rows) == 0


def test_height_and_footprint_swaps_are_not_duplicate_placements():
    points = [(0., 0., 0.), (200., 200., 20.), (200., 0., 0.)]
    state = {'bed_mm': [220, 220], 'max_print_height_mm': 220}
    # Same unordered dimensions, different contact face and XY footprint.
    # A failed tall candidate must not erase the fitting horizontal candidate.
    def capacity(source, checked_state, rotation, **kwargs):
        return int(rotation[6:9] == [0, 0, 1])

    with patch.object(oa, 'source_triangles', return_value=[tuple(points)]), \
         patch.object(oa, '_surface_stats', return_value=(5000, 0)), \
         patch.object(oa, '_orientation_bound', side_effect=[
             (1., 45., [200., 200., 20.]),
             (1., 45., [200., 20., 200.]),
             (1., 45., [20., 200., 200.])]), \
         patch.object(oa, 'orca_plate_capacity', side_effect=capacity):
        rows = oa.rotation_alternatives(Path('rack.stl'), state, authored_max=.85)
    assert rows and rows[0]['axis'] == '+Z'


@pytest.mark.parametrize('dims', [(254., 44.6, 183.4), (245., 44.5, 20.)])
def test_rack_envelope_bed_turn_round_trips_at_full_scale(tmp_path, dims):
    # Generated cuboids reproduce public dimensions, not private model geometry.
    points = [(x, y, z) for x in (0., dims[0]) for y in (0., dims[1]) for z in (0., dims[2])]
    faces = [(0, 2, 3), (0, 3, 1), (4, 5, 7), (4, 7, 6),
             (0, 1, 5), (0, 5, 4), (2, 6, 7), (2, 7, 3),
             (0, 4, 6), (0, 6, 2), (1, 3, 7), (1, 7, 5)]
    source = tmp_path / 'rack.stl'
    data = bytearray(b'generated rack envelope'.ljust(80, b'\0'))
    data += struct.pack('<I', len(faces))
    for face in faces:
        data += struct.pack('<12fH', 0., 0., 0., *(v for i in face for v in points[i]), 0)
    source.write_bytes(data)
    state = {'bed_mm': [220, 220], 'max_print_height_mm': 220,
             'resolved_hop_types': ['Normal Lift']}
    with patch.object(oa, 'orca_plate_capacity', return_value=1):
        rows = oa.rotation_alternatives(source, state, authored_max=.8)
    chosen = rows[0]
    assert chosen['axis'] == '+Z' and chosen['scale'] == 1.
    target = tmp_path / 'oriented.stl'
    oa.place_stl(source, target, scale=chosen['scale'], rotation=chosen['rotation'])
    before, after = oa.source_triangles(source), oa.source_triangles(target)
    assert len(before) == len(after) == len(faces)
    vertices = [p for tri in after for p in tri]
    assert all(1. <= p[i] <= 219. for p in vertices for i in (0, 1))
    assert min(p[2] for p in vertices) == pytest.approx(0.)
    assert max(p[2] for p in vertices) == pytest.approx(dims[2], abs=.001)
    for old, new in zip(before, after):
        for i, j in ((0, 1), (1, 2), (2, 0)):
            assert math.dist(old[i], old[j]) == pytest.approx(math.dist(new[i], new[j]), abs=.001)
