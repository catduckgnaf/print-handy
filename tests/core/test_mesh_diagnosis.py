"""Read-only mesh diagnosis for the print repair skill (#1777): bodies, edges, undersides."""
import math

import numpy as np
import pytest

from print_handy.core import layer_scan, mesh_diagnosis

from .test_layer_scan import box, hexahedron, leaning, mesh


def test_bodies_lists_each_solid_largest_first():
    result = mesh_diagnosis.bodies(mesh(box(20, 0, 0, 22, 2, 2), box(0, 0, 0, 10, 10, 10)))
    assert result['count'] == 2
    big, small = result['bodies']
    assert big['volume_mm3'] == pytest.approx(1000.0)
    assert big['faces'] == 12
    assert big['bounds_min'] == [0.0, 0.0, 0.0] and big['bounds_max'] == [10.0, 10.0, 10.0]
    assert small['volume_mm3'] == pytest.approx(8.0)
    assert small['area_mm2'] == pytest.approx(24.0)


def test_edge_report_of_a_closed_box():
    report = mesh_diagnosis.edge_report(mesh(box(0, 0, 0, 10, 10, 10)))
    assert report['watertight'] and report['manifold']
    assert report['boundary_edges'] == 0 and report['non_manifold_edges'] == 0
    assert report['boundary_edge_midpoints'] == [] and report['non_manifold_edge_midpoints'] == []


def test_edge_report_locates_open_and_non_manifold_edges():
    tris = box(0, 0, 0, 10, 10, 10)
    opened = mesh_diagnosis.edge_report(layer_scan.weld(np.delete(tris, 0, axis=0)))
    assert not opened['watertight']
    assert opened['boundary_edges'] == 3
    assert len(opened['boundary_edge_midpoints']) == 3
    assert all(point[2] == 0.0 for point in opened['boundary_edge_midpoints'])
    doubled = mesh_diagnosis.edge_report(layer_scan.weld(np.concatenate((tris, tris[:1]))))
    assert doubled['non_manifold_edges'] == 3
    assert len(doubled['non_manifold_edge_midpoints']) == 3


def test_edge_report_matches_the_intake_audit():
    from print_handy.core.compact_geometry import CompactTriangles
    from print_handy.jobs.audit import mesh_topology
    tris = np.concatenate((box(0, 0, 0, 10, 10, 10)[1:], box(20, 0, 0, 30, 10, 10)))
    m = layer_scan.weld(tris)
    compact = CompactTriangles()
    compact.coordinates.frombytes(np.ascontiguousarray(tris).tobytes())
    audit = mesh_topology(compact)
    report = mesh_diagnosis.edge_report(m)
    for key in ('watertight', 'manifold', 'shells', 'boundary_edges', 'non_manifold_edges'):
        assert report[key] == audit[key], key


def test_a_flat_underside_reads_as_flat():
    m = mesh(box(0, 0, 0, 10, 10, 20), box(-1, 0, 9, 30, 10, 11))
    shelf = mesh_diagnosis.underside_profile(m, [12, 0, 30, 10], [8, 10])
    assert shelf['verdict'] == 'flat_underside'
    assert shelf['mean_slope_deg'] == 0.0
    assert shelf['z_min'] == pytest.approx(9.0)
    assert shelf['area_mm2'] == pytest.approx(180.0, rel=0.01)


@pytest.mark.parametrize('angle,verdict', [(53.0, 'sloped_flare'), (30.0, 'flat_underside')])
def test_a_leaning_wall_reads_by_its_slope(angle, verdict):
    """The 2026-11-21 false positive: a low gap under a 53 degree flare needs nothing."""
    m = mesh(leaning(10.0 / math.tan(math.radians(angle))))
    profile = mesh_diagnosis.underside_profile(m, [10, 0, 40, 20])
    assert profile['verdict'] == verdict
    assert profile['mean_slope_deg'] == pytest.approx(angle, abs=0.1)


def test_the_bed_contact_is_not_an_underside():
    profile = mesh_diagnosis.underside_profile(mesh(box(0, 0, 0, 10, 10, 10)), [0, 0, 10, 10])
    assert profile == {'verdict': 'none', 'faces': 0, 'area_mm2': 0.0}


def test_a_mixed_underside():
    flat = box(0, 0, 5, 10, 10, 6)
    steep = hexahedron([(20, 0, 0), (30, 0, 0), (30, 10, 0), (20, 10, 0),
                        (20, 0, 10), (36, 0, 10), (36, 10, 10), (20, 10, 10)])
    profile = mesh_diagnosis.underside_profile(mesh(box(0, 0, 0, 1, 1, 1), flat, steep), [0, 0, 40, 10])
    assert profile['verdict'] == 'mixed'


@pytest.mark.parametrize('region', [[0, 0, 1], [1, 0, 0, 1], ['a', 0, 1, 1], [0, 0, float('inf'), 1]])
def test_bad_regions_are_refused(region):
    with pytest.raises(layer_scan.ScanError):
        mesh_diagnosis.underside_profile(mesh(box(0, 0, 0, 1, 1, 1)), region)
