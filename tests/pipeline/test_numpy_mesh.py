"""The numpy twin of the native mesh kernel: differential tests (#1790).

Installs that cannot load the bounds extension run ``mesh_numpy`` instead of
the per-triangle Python loops. Its results must equal the Python oracle
exactly (bit for bit, signed zeros included), on the same generated meshes,
transform chains and corpus models the native kernel is pinned on
(``test_native_mesh.py``), and the combined 3MF model must be byte-identical.
Every test here runs with the native kernel switched off, so it needs no
wheel and runs in the normal lane; where the wheel is built (the
native-bounds CI job) the last test also pins numpy against native.
"""
import math
import zipfile

import numpy as np
import pytest

from print_handy.core import compact_topology, geometry_layout, mesh_fast, mesh_native, mesh_numpy, model_parts
from print_handy.core.compact_geometry import IndexedTriangles, TransformedTriangles
from print_handy.jobs import audit
from tests.corpus import make_models
from tests.pipeline.test_native_mesh import (
    CASES_GRID, CORPUS, MAX_COMBINE_PARTS, MESHES, _binary_stl, _combine_outcome, _load_outcome,
    _materialize, _native_module, _render, _round6_values, as_compact, bits, chained, impl,
    representations,
)


@pytest.fixture(autouse=True)
def numpy_only(monkeypatch):
    """Native kernel off, numpy on for every mesh size."""
    mesh_native._reset_cache()
    monkeypatch.setattr(mesh_native, '_load', lambda: None)
    monkeypatch.setattr(mesh_numpy, 'MIN_TRIANGLES', 0)
    yield
    mesh_native._reset_cache()


def served(monkeypatch):
    """Record which numpy functions returned a result."""
    calls = []
    real = mesh_numpy._guarded

    def counting(name, work, triangles, allow_lists=False):
        result = real(name, work, triangles, allow_lists)
        calls.append((name, result is not None))
        return result
    monkeypatch.setattr(mesh_numpy, '_guarded', counting)
    return calls


# ------------------------------------------------------------ differential

@pytest.mark.parametrize('name,rep', CASES_GRID)
def test_topology_matches_both_python_paths(name, rep):
    triangles = representations(MESHES[name])[rep]
    with impl('auto'):
        got = mesh_numpy.topology(triangles)
        assert audit.mesh_topology(triangles) == got
    rows = _materialize(triangles)
    with impl('python'):
        small = audit.mesh_topology(rows)
        compact = compact_topology.mesh_topology_compact(rows)
    assert got == small == compact, name
    if isinstance(triangles, IndexedTriangles):
        with impl('python'):
            assert compact_topology.mesh_topology_compact(triangles) == got


@pytest.mark.parametrize('name,rep', CASES_GRID)
def test_measure_matches_the_scalar_loops(name, rep):
    triangles = representations(MESHES[name])[rep]
    with impl('auto'):
        lo, hi, area, volume = mesh_numpy.measure(triangles)
        via_bounds_and_volume = mesh_fast.bounds_and_volume(triangles)
        via_area = geometry_layout._triangle_area_and_signed_volume(triangles)
        via_bounds = mesh_fast.bounds(triangles)
    rows = _materialize(triangles)
    with impl('python'):
        want = mesh_fast.bounds_and_volume(rows)
        want_area = geometry_layout._triangle_area_and_signed_volume(rows)
        want_bounds = mesh_fast.bounds(rows)
    assert bits((lo, hi, volume)) == bits(want)
    assert bits(area) == bits(want_area[0])
    assert bits(via_bounds_and_volume) == bits(want)
    assert bits(via_area) == bits(want_area)
    assert bits(via_bounds) == bits(want_bounds)


@pytest.mark.parametrize('name,rep', CASES_GRID)
def test_threemf_text_matches_the_python_loop(name, rep):
    triangles = representations(MESHES[name])[rep]
    with impl('auto'):
        got = geometry_layout._mesh_xml_lines(triangles)
        text = mesh_numpy.threemf_text(triangles)
    with impl('python'):
        want = geometry_layout._mesh_xml_lines(triangles)
    if name == 'large-offsets':
        # Coordinates from 1e6 up print with the general formatter: Python's.
        assert text is None
    else:
        assert text == ('\n'.join(want[0]), '\n'.join(want[1]))
    assert '\n'.join(got[0]) == '\n'.join(want[0])
    assert '\n'.join(got[1]) == '\n'.join(want[1])


@pytest.mark.parametrize('name', sorted(MESHES))
def test_weld_matches_python_welding(name):
    triangles = as_compact(MESHES[name])
    with impl('auto'):
        vertices, faces = mesh_numpy.weld(triangles, round6=True)
        exact_vertices, exact_faces = mesh_numpy.weld(triangles, round6=False)
    keys, order, ids = {}, [], []
    exact, exact_order, exact_ids = {}, [], []
    for t in triangles:
        for p in t:
            key = tuple(round(float(v), 6) for v in p)
            if key not in keys:
                keys[key] = len(order)
                order.append(key)
            ids.append(keys[key])
            point = tuple(float(v) for v in p)
            if point not in exact:
                exact[point] = len(exact_order)
                exact_order.append(point)
            exact_ids.append(exact[point])
    assert bits([tuple(v) for v in vertices.tolist()]) == bits(order)
    assert faces.reshape(-1).tolist() == ids
    assert bits([tuple(v) for v in exact_vertices.tolist()]) == bits(exact_order)
    assert exact_faces.reshape(-1).tolist() == exact_ids


def test_round6_matches_cpython_round():
    """Value by value against CPython's round(x, 6), on about 3 M hard cases."""
    values = _round6_values()
    got = mesh_numpy.round6(values)
    want = np.array([round(float(v), 6) for v in values.tolist()], dtype=np.float64)
    mismatch = np.flatnonzero(got.view(np.int64) != want.view(np.int64))
    assert len(values) > 3_000_000
    assert not len(mismatch), [(values[i].hex(), got[i], want[i]) for i in mismatch[:5]]
    # The check can fail: without the tie guard, plain rint disagrees.
    plain = np.rint(values * 1e6) / 1e6
    assert np.count_nonzero(plain.view(np.int64) != want.view(np.int64)) > 1000


def test_sums_are_sequential_like_the_scalar_loop():
    """Area and volume add one triangle at a time; a pairwise np.sum would not match."""
    rng = np.random.default_rng(1790)
    soup = rng.uniform(-80.0, 80.0, (60_000, 3, 3))
    triangles = as_compact(soup.tolist())
    with impl('auto'):
        lo, hi, area, volume = mesh_numpy.measure(triangles)
    with impl('python'):
        want_lo, want_hi, want_volume = mesh_fast.bounds_and_volume(_materialize(triangles))
        want_area, _ = geometry_layout._triangle_area_and_signed_volume(_materialize(triangles))
    assert bits((lo, hi, area, volume)) == bits((want_lo, want_hi, want_area, want_volume))
    a, b, c = soup[:, 0], soup[:, 1], soup[:, 2]
    terms = (a[:, 0] * (b[:, 1] * c[:, 2] - b[:, 2] * c[:, 1]) - a[:, 1] * (b[:, 0] * c[:, 2] - b[:, 2] * c[:, 0])
             + a[:, 2] * (b[:, 0] * c[:, 1] - b[:, 1] * c[:, 0])) / 6.0
    assert float(np.sum(terms)) != want_volume  # the test can tell the two sums apart


@pytest.mark.parametrize('first,later', [(0.0, -0.0), (-0.0, 0.0)])
@pytest.mark.parametrize('pad', [0, 70_000])
def test_zero_bounds_keep_the_first_zeros_sign(first, later, pad):
    """numpy's min and max return the last of equal zeros, the scalar loop the first.

    ``pad`` positive triangles put the zeros in a later chunk.
    """
    lead = [[(5.0, 5.0, 5.0), (6.0, 5.0, 5.0), (5.0, 6.0, 5.0)]] * pad
    zeros = [[(first, -first, first), (1.0, -1.0, 1.0), (1.0, -2.0, 2.0)],
             [(later, -later, later), (2.0, -1.0, 1.0), (1.0, -1.0, 3.0)]]
    triangles = as_compact(lead + zeros)
    with impl('auto'):
        got = mesh_numpy.measure(triangles)
    with impl('python'):
        want = mesh_fast.bounds_and_volume(_materialize(triangles))
    assert bits((got[0], got[1], got[3])) == bits(want)


def test_large_mesh_matches_the_compact_python_path():
    tris = list(make_models.torus_triangles(major_segments=320, minor_segments=160))
    tris += [[(0.0, 0.0, 30.0), (1.0, 0.0, 30.0), (0.0, 1.0, 30.0)]]
    compact = as_compact(tris)
    with impl('auto'):
        got = audit.mesh_topology(compact)
        text = mesh_numpy.threemf_text(chained(compact))
        measured = mesh_numpy.measure(compact)
    with impl('python'):
        want = audit.mesh_topology(compact)
        want_text = geometry_layout._mesh_xml_lines(chained(compact))
        want_area = geometry_layout._triangle_area_and_signed_volume(compact)
    assert got == want
    assert got['boundary_edges'] == 3 and got['connected_shells'] == 2
    assert text == ('\n'.join(want_text[0]), '\n'.join(want_text[1]))
    assert bits(measured[2]) == bits(want_area[0])
    assert bits(abs(measured[3])) == bits(want_area[1])


def test_long_strip_components_converge():
    """A 40k-triangle strip numbered backwards: the slowest shape for label hooking."""
    tris = []
    for i in range(20_000, 0, -1):
        x0, x1 = float(i), float(i + 1)
        tris.append([(x0, 0.0, 0.0), (x1, 0.0, 0.0), (x1, 1.0, 0.0)])
        tris.append([(x0, 0.0, 0.0), (x1, 1.0, 0.0), (x0, 1.0, 0.0)])
    compact = as_compact(tris)
    with impl('auto'):
        got = mesh_numpy.topology(compact)
    with impl('python'):
        want = compact_topology.mesh_topology_compact(_materialize(compact))
    assert got == want and got['connected_shells'] == 1


# ------------------------------------------------------------ contract

def test_python_setting_and_small_meshes_keep_the_python_loops(monkeypatch):
    tris = as_compact(MESHES['grid-box'])
    with impl('python'):
        assert mesh_numpy.topology(tris) is None
        assert mesh_numpy.measure(tris) is None
        assert mesh_numpy.threemf_text(tris) is None
    monkeypatch.setattr(mesh_numpy, 'MIN_TRIANGLES', len(tris) + 1)
    with impl('auto'):
        assert mesh_numpy.measure(tris) is None


def test_untyped_and_non_finite_input_use_python():
    nan = as_compact(MESHES['tetra'] + [[(float('nan'), 0, 0), (1, 0, 0), (0, 1, 0)]])
    overflow = TransformedTriangles.translation(as_compact(MESHES['tetra']), 1e308, 1e308, 0.0)
    overflow = TransformedTriangles.translation(overflow, 1e308, 0.0, 0.0)
    plain = TransformedTriangles(as_compact(MESHES['tetra']), lambda x, y, z: (x, y, z))
    with impl('auto'):
        for triangles in (nan, overflow, plain, MESHES['tetra']):
            assert mesh_numpy.measure(triangles) is None
            assert mesh_numpy.threemf_text(triangles) is None
        assert audit.mesh_topology(nan) == audit.mesh_topology(_materialize(nan))


def test_a_numpy_fault_is_logged_and_python_decides(monkeypatch, caplog):
    def boom(mesh):
        raise RuntimeError('numpy went sideways')
    monkeypatch.setattr(mesh_numpy, '_measure', boom)
    tris = chained(as_compact(MESHES['grid-box']))
    with impl('auto'):
        got = mesh_fast.bounds_and_volume(tris)
    assert 'numpy mesh fallback failed in measure' in caplog.text
    with impl('python'):
        assert bits(got) == bits(mesh_fast.bounds_and_volume(tris))


# ------------------------------------------------------------ corpus and combine

@pytest.mark.parametrize('name', CORPUS)
def test_corpus_model_loads_and_combines_identically(tmp_path, name):
    model = _render(name, tmp_path)
    with impl('auto'):
        loaded = _load_outcome(model)
    with impl('python'):
        want_loaded = _load_outcome(model)
    assert loaded == want_loaded
    if loaded[0] != 'raise' and len(loaded[3]) > MAX_COMBINE_PARTS:
        return
    with impl('auto'):
        combined = _combine_outcome(tmp_path / 'numpy.3mf', [model])
    with impl('python'):
        want_combined = _combine_outcome(tmp_path / 'python.3mf', [model])
    assert combined == want_combined


def test_generated_stl_pair_combines_identically(tmp_path, monkeypatch):
    first, second = tmp_path / 'sphere.stl', tmp_path / 'box.stl'
    _binary_stl(first, MESHES['uv-sphere'] + MESHES['tie-grid'])
    _binary_stl(second, MESHES['noisy-box'])
    calls = served(monkeypatch)
    for bed in ([220.0, 220.0, 250.0], [30.0, 30.0, 30.0]):
        with impl('auto'):
            got = model_parts.combine(tmp_path / 'n.3mf', [str(first), str(second)], bed)
            got_model = zipfile.ZipFile(tmp_path / 'n.3mf').read('3D/3dmodel.model')
        with impl('python'):
            want = model_parts.combine(tmp_path / 'p.3mf', [str(first), str(second)], bed)
            want_model = zipfile.ZipFile(tmp_path / 'p.3mf').read('3D/3dmodel.model')
        assert got['objects'] == want['objects'] and got['on_one_bed'] == want['on_one_bed']
        assert got_model == want_model
    # numpy really served every stage (not a silent Python fallback).
    assert {name for name, ok in calls if ok} >= {'topology', 'measure', 'threemf_text'}


def test_numpy_equals_native_where_the_wheel_is_built(monkeypatch):
    module = _native_module()
    if module is None:
        pytest.skip('native bounds extension (with the mesh kernel) not built')
    monkeypatch.undo()
    monkeypatch.setattr(mesh_numpy, 'MIN_TRIANGLES', 0)
    mesh_native._reset_cache()
    rad = math.radians(33.0)
    for name in sorted(MESHES):
        base = as_compact(MESHES[name])
        triangles = TransformedTriangles.rotation_z(base, math.cos(rad), math.sin(rad))
        with impl('native'):
            native = (mesh_native.topology(triangles), mesh_native.measure(triangles),
                      mesh_native.threemf_text(triangles))
        got = (mesh_numpy.topology(triangles), mesh_numpy.measure(triangles), mesh_numpy.threemf_text(triangles))
        assert got[0] == native[0], name
        assert bits(got[1]) == bits(native[1]), name
        if got[2] is not None:
            assert got[2] == tuple(native[2]), name
