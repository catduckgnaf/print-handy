import numpy as np
import pytest

from print_handy.core.island_scan import scan


def box(x0, y0, z0, x1, y1, z1):
    v = [(x0,y0,z0),(x1,y0,z0),(x1,y1,z0),(x0,y1,z0),
         (x0,y0,z1),(x1,y0,z1),(x1,y1,z1),(x0,y1,z1)]
    faces = [(0,2,1),(0,3,2),(4,5,6),(4,6,7),
             (0,1,5),(0,5,4),(1,2,6),(1,6,5),
             (2,3,7),(2,7,6),(3,0,4),(3,4,7)]
    return np.asarray([[v[a],v[b],v[c]] for a,b,c in faces], dtype=float)


def test_anchored_body_and_unsupported_region():
    anchored = box(0, 0, 0, 10, 10, 10)
    unsupported = box(20, 0, 4, 24, 4, 8)
    result = scan(np.concatenate((anchored, unsupported)), 1.0)
    assert len(result) == 1
    island = result[0]
    assert island.z == pytest.approx(4.5)
    assert island.area == pytest.approx(16)
    assert island.bounds == pytest.approx((20, 0, 24, 4))


def test_sloped_continuous_body_does_not_false_positive():
    # A closed, sheared box shifts laterally with height. Its XY bounds move,
    # but each filled section still overlaps the preceding section.
    tris = box(0, 0, 0, 10, 10, 10)
    tris[:, :, 0] += 0.2 * tris[:, :, 2]
    assert scan(tris, 1.0) == []


def test_empty_layer_does_not_support_a_box_that_starts_on_the_next_midpoint():
    anchored = box(0, 0, 0, 4, 4, 1)
    floating = box(0, 0, 2.5, 4, 4, 4)
    islands = scan(np.concatenate((anchored, floating)), 1.0)
    assert [(i.z, i.area, i.bounds) for i in islands] == [(2.5, 16, (0, 0, 4, 4))]


def test_nested_cross_section_void_does_not_support_a_box():
    outer = box(0, 0, 0, 10, 10, 10)
    cavity = box(2, 2, 1, 8, 8, 9)[:, ::-1]
    floating = box(3, 3, 4, 7, 7, 6)
    islands = scan(np.concatenate((outer, cavity, floating)), 1.0)
    assert [(i.z, i.area, i.bounds) for i in islands] == [(4.5, 16, (3, 3, 7, 7))]


def test_limits_and_invalid_units_are_rejected():
    with pytest.raises(ValueError, match='section geometry work limit'):
        scan(box(0, 0, 0, 1, 1, 10), 1, max_section_work=10)
    with pytest.raises(ValueError, match='layer limit'):
        scan(box(0,0,0,1,1,10), 0.001, max_layers=5)
    with pytest.raises(ValueError, match='positive'):
        scan(box(0,0,0,1,1,1), 0)
