"""The placement report on arrays (``placement_numpy``): differential tests (#1790).

``orca_adapter.placement_candidates`` and the fit search's
``_orientation_bound`` run their per-point loops on arrays from
``PLACEMENT_NUMPY_MIN_POINTS`` points. The scalar loops are the oracle: every
report row (rounded fields, ranks and matrices) and every bound must be the
same on both paths, on the generated meshes in each storage form, on every
corpus model and on a dense mesh where the octagon prune declines. The audit's
element counts on a cut model text must equal those on the full text.
None of this needs the native wheel.
"""
import json
import math
import re
import zipfile

import numpy as np
import pytest

from print_handy.core import orca_adapter, placement_numpy, source_colors
from print_handy.core.compact_geometry import IndexedTriangles
from print_handy.jobs import audit
from tests.corpus import make_models
from tests.pipeline.test_native_mesh import CORPUS, MESHES, _render, as_indexed, representations

SCALAR = 10 ** 12


def _fresh():
    orca_adapter._POINTS_CACHE.clear()
    orca_adapter._FACE_CACHE.clear()


def _report(monkeypatch, triangles, minimum, **kwargs):
    _fresh()
    monkeypatch.setattr(orca_adapter, 'PLACEMENT_NUMPY_MIN_POINTS', minimum)
    try:
        return json.dumps(orca_adapter.placement_candidates(triangles, **kwargs))
    except ValueError as exc:
        return f'raise {exc}'


def _bounds(monkeypatch, triangles, minimum):
    _fresh()
    monkeypatch.setattr(orca_adapter, 'PLACEMENT_NUMPY_MIN_POINTS', minimum)
    points = orca_adapter._unique_points(triangles)
    state = {'bed_mm': [220.0, 220.0], 'max_print_height_mm': 250.0, 'resolved_hop_types': ['Normal Lift']}
    rotations = [orca_adapter.IDENTITY_ROT] + [matrix for _name, matrix in orca_adapter.UP_AXES.values()]
    return [orca_adapter._orientation_bound(points, state, rotation) for rotation in rotations]


def _assert_paths_agree(monkeypatch, triangles, **kwargs):
    want = _report(monkeypatch, triangles, SCALAR, **kwargs)
    got = _report(monkeypatch, triangles, 0, **kwargs)
    assert got == want
    if not want.startswith('raise'):
        assert _bounds(monkeypatch, triangles, 0) == _bounds(monkeypatch, triangles, SCALAR)
    return want


def _indexed(tris):
    return as_indexed(tris)


def _dense_part():
    """A long curved part: a torus stretched and bent into a U, with a flat bar on top.

    560 K points are not needed: a few tens of thousands make the octagon prune
    keep a wide band, and the bar gives hull edges with many collinear points.
    """
    tris = []
    for tri in make_models.torus_triangles(major_segments=240, minor_segments=60):
        bent = []
        for x, y, z in tri:
            angle = math.atan2(y, x)
            bent.append((x * 0.4 + 3.0 * math.cos(angle * 0.5), y * 2.5, z * 0.7))
        tris.append(bent)
    for i in range(400):
        x0, x1 = -20.0 + i * 0.1, -20.0 + (i + 1) * 0.1
        tris.append([(x0, 40.0, 0.0), (x1, 40.0, 0.0), (x0, 40.0, 3.0)])
        tris.append([(x1, 40.0, 0.0), (x1, 40.0, 3.0), (x0, 40.0, 3.0)])
    return tris


# ------------------------------------------------------------ report rows

@pytest.mark.parametrize('name', sorted(MESHES))
@pytest.mark.parametrize('rep', ['lists', 'compact', 'indexed', 'indexed+rotz'])
def test_generated_meshes_report_the_same_rows(monkeypatch, name, rep):
    triangles = [list(t) for t in MESHES[name]] if rep == 'lists' else representations(MESHES[name])[rep]
    _assert_paths_agree(monkeypatch, triangles)


@pytest.mark.parametrize('name', CORPUS)
def test_corpus_models_report_the_same_rows(monkeypatch, tmp_path, name):
    model = _render(name, tmp_path)
    _fresh()
    try:
        triangles = orca_adapter.source_triangles(model)
    except Exception as exc:  # noqa: BLE001 - a model the reader refuses has no report on either path
        pytest.skip(f'{name}: not readable as triangles ({exc})')
    if not triangles:
        pytest.skip(f'{name}: no triangles')
    _assert_paths_agree(monkeypatch, triangles, printer_model='ad5x')


def test_dense_part_reports_the_same_rows_and_prunes(monkeypatch):
    tris = _dense_part()
    indexed = _indexed(tris)
    assert len(orca_adapter._unique_points(indexed)) > 10_000
    want = _assert_paths_agree(monkeypatch, indexed)
    assert len(json.loads(want)) > 40
    # The finer polygons are what keep the chain short: the octagon alone
    # leaves thousands of points on this part.
    _fresh()
    points = orca_adapter._unique_points_array(indexed)
    wide = 0
    for _name, matrix in [('Upright', orca_adapter.IDENTITY_ROT), *orca_adapter.UP_AXES.values()]:
        xy = placement_numpy.rotate(points, matrix)[:, :2]
        octagon = orca_adapter._drop_interior(xy)
        kept = len(xy) if octagon is xy else len(octagon)
        if kept <= 4096:
            continue
        wide += 1
        pruned = placement_numpy._prune(np.asarray(octagon, dtype=float).reshape((-1, 2)), float(np.ptp(xy)))
        assert len(pruned) < kept / 2
    assert wide


def test_octagon_decline_still_prunes_and_matches(monkeypatch):
    """When ``_drop_interior`` declines (it does on the tongs' -Y orientation), the finer polygons still run."""
    indexed = _indexed(_dense_part())
    monkeypatch.setattr(orca_adapter, '_drop_interior', lambda points: points)
    want = _report(monkeypatch, indexed, SCALAR)
    assert _report(monkeypatch, indexed, 0) == want


def test_hull_keeps_every_vertex_of_dense_and_degenerate_clouds():
    rng = np.random.default_rng(1790)
    clouds = [
        rng.normal(size=(50_000, 2)),
        rng.uniform(-1, 1, size=(50_000, 2)),
        # Grid points: whole hull edges of collinear points, and repeated rows.
        np.array([(x, y) for x in range(-60, 61) for y in range(-40, 41)], dtype=float),
        np.repeat(rng.uniform(size=(3_000, 2)), 3, axis=0),
        # A segment: no polygon to prune with.
        np.column_stack([np.linspace(0, 1, 9_000), np.linspace(0, 2, 9_000)]),
        np.zeros((5_000, 2)),
    ]
    for cloud in clouds:
        want = orca_adapter._hull([tuple(row) for row in cloud.tolist()])
        assert placement_numpy.hull(cloud) == want


def test_rotate_is_bit_identical_to_the_scalar_rotation():
    rng = np.random.default_rng(7)
    points = rng.normal(scale=50.0, size=(5_000, 3))
    points[:10] = 0.0
    points[10:20] = -0.0
    c, s = math.cos(0.7), math.sin(0.7)
    tilt = [1.0, 0.0, 0.0, 0.0, c, -s, 0.0, s, c]
    dense = orca_adapter._matmul(orca_adapter._matmul(orca_adapter._yaw(33.3), tilt), orca_adapter._yaw(-71.0))
    assert all(v != 0 for v in dense)  # every term counts, so the evaluation order shows
    for matrix in [orca_adapter.IDENTITY_ROT, *(m for _n, m in orca_adapter.UP_AXES.values()),
                   orca_adapter._matmul(orca_adapter._yaw(33.3), orca_adapter.UP_AXES['+X'][1]), dense]:
        got = placement_numpy.rotate(points, matrix)
        want = [orca_adapter._rotate(tuple(p), matrix) for p in points.tolist()]
        assert [tuple(v.hex() for v in row) for row in got.tolist()] == [
            tuple(v.hex() for v in row) for row in want]


def test_yaw_extents_match_the_scalar_loop():
    hull = orca_adapter._hull([tuple(p) for p in np.random.default_rng(3).normal(size=(400, 2)).tolist()])
    yaws = [0.0, 0.1, 44.999, 45.0, 90.0, -12.5, 179.9]
    want = []
    for yaw in yaws:
        ym = orca_adapter._yaw(yaw)
        moved = [(ym[0] * x + ym[1] * y, ym[3] * x + ym[4] * y) for x, y in hull]
        want.append((max(p[0] for p in moved) - min(p[0] for p in moved),
                     max(p[1] for p in moved) - min(p[1] for p in moved)))
    assert placement_numpy.yaw_extents(hull, yaws) == want


def test_non_finite_points_are_refused_on_both_paths(monkeypatch):
    tris = _dense_part()
    tris.append([(math.inf, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)])
    for minimum in (0, SCALAR):
        assert _report(monkeypatch, tris, minimum) == 'raise placement report requires finite mesh coordinates'


# ------------------------------------------------------------ unique points

def test_unique_rows_equals_numpy_unique():
    rng = np.random.default_rng(11)
    base = np.round(rng.normal(size=(20_000, 3)), 2)
    cases = [base, np.concatenate([base, base[::-1]]), base.astype(np.float32), np.zeros((0, 3))]
    signed = base.copy()
    signed[:50, 1] = -0.0
    cases.append(signed)  # a negative zero: np.unique decides
    # Rows equal up to the sign of a zero, in both input orders.
    pairs = [(float(i % 7), sign * 0.0, 1.0) for i in range(4_000) for sign in ((1, -1) if i % 2 else (-1, 1))]
    cases.append(np.array(pairs))
    with_nan = base.copy()
    with_nan[5, 2] = np.nan
    cases.append(with_nan)
    for rows in cases:
        want = np.ascontiguousarray(np.unique(rows, axis=0), dtype=np.float64)
        got = placement_numpy.unique_rows(rows)
        assert got.dtype == np.float64 and got.shape == want.shape
        assert np.array_equal(got, want, equal_nan=True)
        assert got.tobytes() == want.tobytes() or np.isnan(want).any()


def test_unique_points_list_and_array_agree():
    indexed = _indexed(_dense_part())
    _fresh()
    array = orca_adapter._unique_points_array(indexed)
    points = orca_adapter._unique_points(indexed)
    assert points == [tuple(row) for row in array.tolist()]
    assert isinstance(indexed, IndexedTriangles)
    referenced = np.zeros(len(indexed.vertices), dtype=bool)
    referenced[indexed.faces.ravel()] = True
    assert array.tobytes() == np.unique(indexed.vertices[referenced], axis=0).astype(np.float64).tobytes()
    _fresh()
    lists = [list(t) for t in _dense_part()]
    assert orca_adapter._unique_points_array(lists).tolist() == [list(p) for p in orca_adapter._unique_points(lists)]


# ------------------------------------------------------------ audit element counts

PATTERNS = [r'<(?:\w+:)?object\b', r'<(?:\w+:)?component\b', r'<(?:\w+:)?item\b',
            r'<metadata key="name" value="([^"]+)']


def _model_text(bodies):
    """A 3MF model text of several objects; ``bodies`` replaces the plain mesh bodies."""
    parts = ['<?xml version="1.0"?>\n<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">',
             '<metadata name="Title">t</metadata>\n<resources>']
    for index, (vertices, triangles) in enumerate(bodies):
        parts.append(f'<object id="{index + 1}" type="model"><metadata key="name" value="part {index}"/>'
                     f'<mesh>\n<vertices>{vertices}</vertices>\n<triangles>{triangles}</triangles>\n</mesh></object>')
    parts.append('<object id="99" type="model"><components><component objectid="1"/></components></object>')
    parts.append('</resources>\n<build><item objectid="1"/><item objectid="99"/></build>\n</model>')
    return '\n'.join(parts)


def _plain(count):
    vertices = ''.join(f'\n<vertex x="{i}.5" y="{i}" z="0"/>' for i in range(count))
    triangles = ''.join(f'\n<triangle v1="{i}" v2="{i + 1}" v3="{i + 2}"/>' for i in range(count))
    return vertices, triangles


@pytest.mark.parametrize('variant', ['plain', 'comment', 'namespaced', 'tab', 'object-inside', 'unclosed', 'small'])
def test_cut_model_text_counts_the_same(variant):
    vertices, triangles = _plain(30_000)
    bodies = [(vertices, triangles), (vertices, triangles)]
    if variant == 'comment':
        bodies.append((vertices + '<!-- <object id="5"> <item objectid="1"/> -->', triangles))
    elif variant == 'namespaced':
        bodies.append((vertices.replace('<vertex ', '<m:vertex '), triangles))
    elif variant == 'tab':
        bodies.append((vertices, triangles.replace('<triangle ', '<triangle\t')))
    elif variant == 'object-inside':
        bodies.append((vertices + '<m:object id="7"/><metadata key="name" value="hidden"/>', triangles))
    elif variant == 'unclosed':
        bodies.append((vertices, triangles + '<triangles>'))
    elif variant == 'small':
        bodies = [_plain(5)]
    text = _model_text(bodies)
    cut = audit._without_mesh_bodies(text)
    if variant == 'small':
        assert cut is text
    elif variant == 'plain':
        assert len(cut) < 2_000
    assert [re.findall(p, cut) for p in PATTERNS] == [re.findall(p, text) for p in PATTERNS]


def test_audit_counts_on_a_large_combined_model(tmp_path, monkeypatch):
    """The production entry point: the counts with the cut equal those on the full text."""
    from print_handy.core import model_parts
    from tests.pipeline.test_native_mesh import _binary_stl
    first, second = tmp_path / 'u.stl', tmp_path / 'bar.stl'
    _binary_stl(first, _dense_part())
    _binary_stl(second, MESHES['grid-box'])
    target = tmp_path / 'parts.3mf'
    model_parts.combine(target, [str(first), str(second)], [220.0, 220.0, 250.0])
    with zipfile.ZipFile(target) as archive:
        assert archive.getinfo('3D/3dmodel.model').file_size > (1 << 20)
    monkeypatch.setattr(source_colors, 'inventory', lambda path: {})
    monkeypatch.setattr(source_colors, 'prompts', lambda manifest: [])
    monkeypatch.setattr(audit, 'mesh_audit', lambda path, printer_model=None: {})
    monkeypatch.setattr(audit, 'model_info_output', lambda path: '')
    got = audit.audit(target)
    monkeypatch.setattr(audit, '_without_mesh_bodies', lambda text: text)
    want = audit.audit(target)
    keys = ('objects', 'components', 'build_items', 'named_parts')
    assert {k: got[k] for k in keys} == {k: want[k] for k in keys}
    assert got['objects'] >= 2 and got['build_items'] >= 1
