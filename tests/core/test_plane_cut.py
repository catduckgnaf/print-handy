"""#1245: the AI fix split is a real capped plane cut.

Every cut is checked the same way: both sides closed and consistently
wound (no open, non-manifold or flipped edge), positive volume, and the
two volumes summing to the source's. Sources: a bar, a tube (a cut across
it leaves an annulus: one outer loop and one hole), a torus cut through
its hole (two separate cut regions) and across its middle (an annulus),
a stepped block cut along its ledge (faces lying in the plane), a dense
sphere, and nested rings. Open and non-manifold sources are refused by
name, and nothing is written.
"""
from __future__ import annotations

import numpy as np
import pytest

from print_handy.core import ai_fix, mesh_repair, plane_cut, support_clearance


def _orient_out(tris):
    tris = np.asarray(tris, dtype=float)
    return tris[:, [0, 2, 1]] if mesh_repair.signed_volume(tris) < 0 else tris


def _box(lo, hi):
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
         (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    faces = [(0, 3, 2), (0, 2, 1), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
             (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7), (4, 5, 6), (4, 6, 7)]
    return np.array([[v[a], v[b], v[c]] for a, b, c in faces], dtype=float)


def _grid(rows, cols, point, wrap_rows=True, wrap_cols=True):
    """Quads of a (rows x cols) parametric grid, two triangles each."""
    tris = []
    for i in range(rows if wrap_rows else rows - 1):
        for j in range(cols if wrap_cols else cols - 1):
            a, b = point(i, j), point((i + 1) % rows, j)
            c, d = point((i + 1) % rows, (j + 1) % cols), point(i, (j + 1) % cols)
            tris += [(a, b, c), (a, c, d)]
    return tris


def _torus(big=20.0, small=5.0, n=48, m=24):
    def point(i, j):
        t, p = 2 * np.pi * i / n, 2 * np.pi * j / m
        return ((big + small * np.cos(p)) * np.cos(t), (big + small * np.cos(p)) * np.sin(t),
                small * np.sin(p))
    return _orient_out(_grid(n, m, point))


def _tube(outer=10.0, inner=6.0, height=20.0, n=40):
    """A closed tube: outer and inner walls, top and bottom annuli."""
    ring = [(np.cos(2 * np.pi * k / n), np.sin(2 * np.pi * k / n)) for k in range(n)]
    tris = []
    for k in range(n):
        (c0, s0), (c1, s1) = ring[k], ring[(k + 1) % n]
        o0b, o1b = (outer * c0, outer * s0, 0.0), (outer * c1, outer * s1, 0.0)
        o0t, o1t = (outer * c0, outer * s0, height), (outer * c1, outer * s1, height)
        i0b, i1b = (inner * c0, inner * s0, 0.0), (inner * c1, inner * s1, 0.0)
        i0t, i1t = (inner * c0, inner * s0, height), (inner * c1, inner * s1, height)
        tris += [(o0b, o1b, o1t), (o0b, o1t, o0t)]          # outer wall, outward
        tris += [(i0b, i1t, i1b), (i0b, i0t, i1t)]          # inner wall, toward the axis
        tris += [(o0t, o1t, i1t), (o0t, i1t, i0t)]          # top annulus, +z
        tris += [(o0b, i1b, o1b), (o0b, i0b, i1b)]          # bottom annulus, -z
    return np.array(tris, dtype=float)


def _sphere(radius=10.0, n=60):
    def point(i, j):
        t, p = np.pi * (i + 1) / (n + 1), 2 * np.pi * j / (2 * n)
        return (radius * np.sin(t) * np.cos(p), radius * np.sin(t) * np.sin(p),
                radius * np.cos(t))
    tris = _grid(n, 2 * n, point, wrap_rows=False)
    top, bottom = (0.0, 0.0, radius), (0.0, 0.0, -radius)
    for j in range(2 * n):
        tris.append((top, point(0, (j + 1) % (2 * n)), point(0, j)))
        tris.append((bottom, point(n - 1, j), point(n - 1, (j + 1) % (2 * n))))
    tris = np.array(tris, dtype=float)
    # Convex and centered on the origin: wind every face away from the center.
    normal = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    inward = np.einsum('ij,ij->i', normal, tris.mean(axis=1)) < 0
    tris[inward] = tris[inward][:, [0, 2, 1]]
    return tris


def _check(tris, axis, at, *, loops, holes):
    result = plane_cut.cut(tris, axis, at)
    assert result is not None and result.capped
    assert (result.loops, result.holes) == (loops, holes)
    volumes = []
    for k, (points, faces) in enumerate((result.lower, result.upper)):
        assert mesh_repair.edge_stats(faces, len(points)) == (0, 0, 0)
        volumes.append(plane_cut.volume(points, faces))
        assert volumes[-1] > 0
        coord = points[faces].reshape(-1, 3)[:, axis]
        assert (coord.max() <= at + 1e-9) if k == 0 else (coord.min() >= at - 1e-9)
    assert sum(volumes) == pytest.approx(mesh_repair.signed_volume(tris), rel=1e-9)
    return volumes


def test_bar_split_in_half_gives_two_closed_1500_mm3_pieces():
    # The #1249 case: a 30 x 10 x 10 mm bar, cut at x = 15.
    bar = _box((0, 0, 0), (30, 10, 10))
    assert _check(bar, 0, 15.0, loops=1, holes=0) == [pytest.approx(1500.0),
                                                      pytest.approx(1500.0)]


@pytest.mark.parametrize('at', [0.0, 7.3, -2.5])
def test_tube_cut_across_leaves_an_annulus_cap(at):
    tube = _tube()
    tube = tube - [0, 0, 10.0]
    low, high = _check(tube, 2, at, loops=1, holes=1)
    area = np.pi * (10.0 ** 2 - 6.0 ** 2) * (40 / (2 * np.pi)) * np.sin(2 * np.pi / 40)
    assert low == pytest.approx(area * (at + 10.0))
    assert high == pytest.approx(area * (10.0 - at))


@pytest.mark.parametrize('axis, at', [(0, 0.0), (0, 3.7), (1, -11.0)])
def test_torus_cut_through_its_hole_gives_two_regions(axis, at):
    _check(_torus(), axis, at, loops=2, holes=0)


@pytest.mark.parametrize('at', [0.0, 1.3, -4.2])
def test_torus_cut_across_its_middle_gives_an_annulus(at):
    _check(_torus(), 2, at, loops=1, holes=1)


def test_torus_cut_through_its_tube_only_gives_one_region():
    _check(_torus(), 1, 21.0, loops=1, holes=0)


def test_nested_rings_cap_island_inside_hole():
    # A thin tube standing in a wide tube's bore: the cut is an annulus with
    # a smaller annulus inside its hole, two outer loops and two holes, each
    # hole capped against its own outer loop.
    inner_tube = _tube(outer=3.0, inner=1.0, height=20.0)
    both = np.concatenate([_tube(), inner_tube])
    _check(both - [0, 0, 10.0], 2, 0.5, loops=2, holes=2)


def test_stepped_block_cut_along_its_ledge():
    # A 20 mm block with a 10 mm block on top; the cut runs along the ledge,
    # so the ledge faces lie in the plane and stay with the lower part.
    lower = _box((0, 0, 0), (20, 20, 10))
    upper = _box((5, 5, 10), (15, 15, 20))
    # Weld into one closed solid: drop the shared faces at z = 10 by hand.
    v = [(0, 0, 0), (20, 0, 0), (20, 20, 0), (0, 20, 0),
         (0, 0, 10), (20, 0, 10), (20, 20, 10), (0, 20, 10),
         (5, 5, 10), (15, 5, 10), (15, 15, 10), (5, 15, 10),
         (5, 5, 20), (15, 5, 20), (15, 15, 20), (5, 15, 20)]
    faces = [(0, 3, 2), (0, 2, 1), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
             (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
             (4, 5, 9), (4, 9, 8), (5, 6, 10), (5, 10, 9), (6, 7, 11), (6, 11, 10),
             (7, 4, 8), (7, 8, 11),
             (8, 9, 13), (8, 13, 12), (9, 10, 14), (9, 14, 13), (10, 11, 15), (10, 15, 14),
             (11, 8, 12), (11, 12, 15), (12, 13, 14), (12, 14, 15)]
    stepped = np.array([[v[a], v[b], v[c]] for a, b, c in faces], dtype=float)
    assert mesh_repair.facts_for(stepped)['sound']
    assert mesh_repair.signed_volume(stepped) == pytest.approx(
        mesh_repair.signed_volume(lower) + mesh_repair.signed_volume(upper))
    assert _check(stepped, 2, 10.0, loops=1, holes=0) == [pytest.approx(4000.0),
                                                          pytest.approx(1000.0)]


def test_dense_sphere_cut_through_its_vertices():
    sphere = _sphere()
    _check(sphere, 2, 0.0, loops=1, holes=0)
    _check(sphere, 0, 2.5, loops=1, holes=0)


def test_a_plane_that_misses_or_grazes_the_model_cuts_nothing():
    bar = _box((0, 0, 0), (30, 10, 10))
    assert plane_cut.cut(bar, 2, 50.0) is None
    assert plane_cut.cut(bar, 2, 0.0) is None    # along the bottom face
    assert plane_cut.cut(bar, 2, 10.0) is None   # along the top face


def test_an_open_source_is_refused_by_name(tmp_path):
    bar = _box((0, 0, 0), (30, 10, 10))[2:]      # no bottom face
    with pytest.raises(ai_fix.FixError, match=r'not a closed solid \(4 open edges\)'):
        plane_cut.cut(bar, 0, 15.0)
    src = tmp_path / 'open.stl'
    support_clearance._write_binary_stl(src, bar)
    out = tmp_path / 'split.3mf'
    with pytest.raises(ai_fix.FixError, match='open edges'):
        ai_fix.split_model(src, out, 'x', 15.0)
    assert not out.exists()


def test_a_non_manifold_source_is_refused_by_name(tmp_path):
    # Two boxes sharing one edge: that edge has four faces.
    pair = np.concatenate([_box((0, 0, 0), (10, 10, 10)), _box((10, 10, 0), (20, 20, 10))])
    assert mesh_repair.facts_for(pair)['nonmanifold_edges']
    src = tmp_path / 'pair.stl'
    support_clearance._write_binary_stl(src, pair)
    out = tmp_path / 'split.3mf'
    with pytest.raises(ai_fix.FixError, match='non-manifold edge'):
        ai_fix.split_model(src, out, 'z', 5.0)
    assert not out.exists()


def test_pieces_already_apart_split_without_a_cap_on_any_mesh(tmp_path):
    # An open box above a closed one: the plane in the gap crosses nothing,
    # so the open source is still split (no cap is needed).
    stack = np.concatenate([_box((0, 0, 0), (10, 10, 10)), _box((0, 0, 12), (10, 10, 22))[2:]])
    result = plane_cut.cut(stack, 2, 11.0)
    assert result is not None and not result.capped
    assert len(result.lower[1]) == 12 and len(result.upper[1]) == 10
    # and the open piece below the plane works the same way
    flipped = np.concatenate([_box((0, 0, 0), (10, 10, 10))[2:], _box((0, 0, 12), (10, 10, 22))])
    result = plane_cut.cut(flipped, 2, 11.0)
    assert result is not None and not result.capped
    assert len(result.lower[1]) == 10 and len(result.upper[1]) == 12


def test_split_model_writes_the_capped_tube_as_two_closed_parts(tmp_path):
    src = tmp_path / 'tube.stl'
    support_clearance._write_binary_stl(src, _tube())
    out = tmp_path / 'tube.3mf'
    report = ai_fix.split_model(src, out, 'z', 8.0)
    assert report['changed'] and report['capped']
    assert (report['cut_regions'], report['cut_holes']) == (1, 1)
    parts = support_clearance.leaves(out)
    assert len(parts) == 2
    total = 0.0
    for part in parts:
        facts = mesh_repair.facts_for(part['tris'])
        assert facts['sound'], facts
        total += mesh_repair.signed_volume(part['tris'])
    assert total == pytest.approx(mesh_repair.signed_volume(_tube()), rel=1e-5)


def _voxels(filled):
    """The closed surface of a voxel set: two triangles per exposed face."""
    tris = []
    shape = filled.shape
    for x, y, z in zip(*np.nonzero(filled)):
        for axis in range(3):
            for step in (-1, 1):
                nb = [x, y, z]
                nb[axis] += step
                if 0 <= nb[axis] < shape[axis] and filled[tuple(nb)]:
                    continue
                # the face square, wound outward along (axis, step)
                u, v = (axis + 1) % 3, (axis + 2) % 3
                base = np.array([x, y, z], dtype=float)
                base[axis] += 1.0 if step > 0 else 0.0
                du, dv = np.eye(3)[u], np.eye(3)[v]
                quad = [base, base + du, base + du + dv, base + dv]
                if step < 0:
                    quad = quad[::-1]
                tris += [(quad[0], quad[1], quad[2]), (quad[0], quad[2], quad[3])]
    return np.array(tris, dtype=float)


def _no_edge_contacts(filled):
    """Fill cells until no two cells meet only along an edge (a 2 x 2
    checkerboard around an edge makes four faces share it)."""
    filled = filled.copy()
    changed = True
    while changed:
        changed = False
        for axis in range(3):
            a, b = [k for k in range(3) if k != axis]
            for idx in np.ndindex(*filled.shape):
                i = list(idx)
                if i[a] + 1 >= filled.shape[a] or i[b] + 1 >= filled.shape[b]:
                    continue
                j = list(i)
                j[a] += 1
                k = list(i)
                k[b] += 1
                m = list(j)
                m[b] += 1
                c00, c10, c01, c11 = (filled[tuple(t)] for t in (i, j, k, m))
                if c00 == c11 and c10 == c01 and c00 != c10:
                    filled[tuple(j if not c10 else i)] = True
                    changed = True
    return filled


@pytest.mark.parametrize('seed', range(12))
def test_random_voxel_solids_cut_into_exact_halves(seed):
    # Rectilinear outlines with holes, islands and long straight runs of
    # collinear cut points: each side holds exactly its voxels' volume. A
    # voxel set whose cells touch only along an edge is non-manifold and
    # must be refused by name instead.
    rng = np.random.default_rng(seed)
    filled = rng.random((9, 9, 4)) < 0.55
    if seed % 3:
        filled = _no_edge_contacts(filled)
    tris = _voxels(filled)
    facts = mesh_repair.facts_for(tris)
    assert facts['sound'] or not seed % 3
    for axis, at in ((2, 1.5), (0, 4.5), (1, 2.25)):
        cells = np.moveaxis(filled, axis, 0)
        below = cells[:int(at)].sum() + (at - int(at)) * cells[int(at)].sum()
        above = cells.sum() - below
        if not below or not above:
            continue
        if not facts['sound']:
            with pytest.raises(ai_fix.FixError, match='non-manifold edge'):
                plane_cut.cut(tris, axis, at)
            continue
        result = plane_cut.cut(tris, axis, at)
        for points, faces in (result.lower, result.upper):
            assert mesh_repair.edge_stats(faces, len(points)) == (0, 0, 0)
        assert plane_cut.volume(*result.lower) == pytest.approx(below)
        assert plane_cut.volume(*result.upper) == pytest.approx(above)
