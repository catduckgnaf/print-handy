"""Extending a sloped low-clearance underside (2.3.0 live run).

A peg's underside sloped up from Z 0.07 mm. The analysis offered "Extend
underside to plate adds about 23 mm3", yet the AI's extend failed twice with
"the proposed extend moves nothing on it": extend_underside only moved
vertices at exactly the gap it was given, and a sloped underside's measured
gap is sampled between its vertices, so no vertex ever matched. The AI path
also took the largest gap of every region, a supportable overhang included.
"""
import math

import numpy as np
import pytest

from print_handy.core import ai_fix, support_clearance

from .test_support_clearance_phase2 import box, prism_xz, write_3mf


def sloped_peg_file(tmp_path, z0=0.07, name='peg.3mf'):
    """A 21 mm long peg whose underside rises at 30 degrees on both sides of a
    lowest edge ``z0`` mm over the plate, with 1 mm wide shelves above it
    that are supportable overhangs (more than 1 mm over the plate)."""
    rise = 2.0 * math.tan(math.radians(30))
    poly = [(0, z0), (2.0, z0 + rise), (3.0, z0 + rise), (3.0, 4.0),
            (-3.0, 4.0), (-3.0, z0 + rise), (-2.0, z0 + rise)]
    return write_3mf(tmp_path / name, [('PEG', prism_xz(poly, 0, 21), '', None)])


def _finding(rep):
    return next(f for f in rep['findings'] if f['code'] == support_clearance.CODE_CLEARANCE)


def _low_edge_z(path):
    leaves = support_clearance.leaves(str(path))
    return float(min(np.asarray(p['tris'])[:, :, 2].min() for p in leaves))


@pytest.mark.parametrize('end', ['min', 'max'])
def test_extend_moves_a_sloped_underside_at_any_gap_in_its_measured_band(tmp_path, end):
    src = sloped_peg_file(tmp_path)
    finding = _finding(support_clearance.analyze(str(src)))
    assert finding['sloped']
    out = tmp_path / f'extended-{end}.3mf'
    res = support_clearance.extend_underside(str(src), str(out), gap_mm=finding['clearance_mm'][end])
    assert res['modified_vertices'] > 0, res
    assert res['mode'] == 'band'
    assert res['added_volume_mm3'] > 0
    assert _low_edge_z(out) == pytest.approx(0.0, abs=1e-6)  # the low edge now sits on the plate
    # Nothing outside the low-clearance band moved: the shelves and the top stay put.
    after = support_clearance.leaves(str(out))[0]['tris'][:, :, 2]
    before = support_clearance.leaves(str(src))[0]['tris'][:, :, 2]
    moved = np.abs(after - before) > 1e-6
    assert np.all(before[moved] < finding['z_mm']['max'] + 1e-3)


def test_a_flat_underside_still_extends_exactly(tmp_path):
    src = write_3mf(tmp_path / 'slab.3mf', [('FOOT', box(-20, -20, 0, -10, -10, 3), '', None),
                                            ('SLAB', box(0, 0, 0.5, 30, 30, 3), '', None)])
    res = support_clearance.extend_underside(str(src), str(tmp_path / 'out.3mf'), gap_mm=0.5)
    assert res['mode'] == 'exact' and res['modified_vertices'] == 4
    assert res['added_volume_mm3'] == pytest.approx(450.0, abs=1.0)


def test_extend_at_a_height_no_finding_holds_still_moves_nothing(tmp_path):
    src = sloped_peg_file(tmp_path)
    res = support_clearance.extend_underside(str(src), str(tmp_path / 'out.3mf'), gap_mm=3.0)
    assert res['modified_vertices'] == 0


def test_the_ai_extend_on_the_measured_gap_changes_the_model(tmp_path):
    """The AI geometry repair with the job's measured gap is no longer a no-op."""
    src = sloped_peg_file(tmp_path)
    rep = support_clearance.analyze(str(src))
    gap = ai_fix.measured_clearance_gap(rep)
    # The low-clearance finding's gap, not the supportable overhang above it.
    assert gap == _finding(rep)['clearance_mm']['max']
    assert any(r['kind'] == 'supportable_overhang' and r['clearance_mm']['max'] > gap
               for r in rep['regions'])
    report = ai_fix.apply_geometry(str(src), str(tmp_path / 'fixed.3mf'),
                                   {'kind': 'extend', 'mm': 0.5}, measured_gap_mm=gap)
    assert report['changed'] is True


def test_measured_gap_without_a_finding_keeps_the_region_gaps():
    report = {'findings': [], 'regions': [{'kind': 'supportable_overhang',
                                           'clearance_mm': {'min': 1.2, 'max': 1.4}}]}
    assert ai_fix.measured_clearance_gap(report) == 1.4
    assert ai_fix.measured_clearance_gap(None) is None


def test_a_fix_error_is_quoted_without_its_own_closing_nothing_was_changed():
    exc = ai_fix.FixError('that fix would not change the model (the proposed extend '
                          'moves nothing on it), so nothing was changed')
    assert ai_fix.reason(exc) == ('that fix would not change the model (the proposed '
                                  'extend moves nothing on it)')
    assert ai_fix.reason(ai_fix.FixError('the AI reply was not a JSON fix proposal; '
                                         'nothing was changed')) == 'the AI reply was not a JSON fix proposal'
    assert ai_fix.reason(ai_fix.FixError('this 3MF is painted, so it is refused')) == \
        'this 3MF is painted, so it is refused'
