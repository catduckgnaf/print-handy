"""Native mesh kernel: differential and fallback tests (#1790).

The Rust kernel in the bounds extension (native/bounds/src/mesh.rs) serves
mesh topology, bounds with area and volume, the geometry-only 3MF mesh
text and STL parsing through print_handy.core.mesh_native. The Python code
it replaces is the permanent fallback and the oracle here: every result
must equal the Python result exactly (bit for bit, signed zeros included),
on generated meshes built to hit the hard corners (welding ties at the
sixth decimal, negative zeros, degenerate and needle triangles, open,
non-manifold, mis-wound and vertex-touching shells, transform chains), on
every model of the generated corpus (tests/corpus/cases), and on the
combine output, whose 3MF model must be byte-identical. Every failure of
the native side must degrade to the Python code with identical results.

In the native-bounds CI job the built extension is placed in
print_handy/core/ and PRINT_HANDY_MESH_REQUIRE_NATIVE=1 turns a missing
kernel from a skip into a failure, so the differential always runs there.
"""
import contextlib
from array import array
import math
import os
import random
import sys
import tomllib
import types
import zipfile
from pathlib import Path

import numpy as np
import pytest

from print_handy.core import compact_topology, geometry_layout, mesh_fast, mesh_native, model_parts
from print_handy.core.compact_geometry import CompactTriangles, IndexedTriangles, TransformedTriangles
from print_handy.core.orca_adapter import read_stl_triangles
from print_handy.jobs import audit
from tests.corpus import make_models

CASES = Path(__file__).resolve().parents[1] / 'corpus' / 'cases'


def _native_module():
    try:
        from print_handy.core import _bounds_native as module
    except Exception:
        return None
    return module if hasattr(module, 'mesh_topology') else None


@pytest.fixture
def native():
    module = _native_module()
    if module is None:
        if os.environ.get('PRINT_HANDY_MESH_REQUIRE_NATIVE'):
            pytest.fail('native mesh kernel required but the extension is not importable')
        pytest.skip('native bounds extension (with the mesh kernel) not built')
    return module


@pytest.fixture(autouse=True)
def _clean_cache():
    mesh_native._reset_cache()
    yield
    mesh_native._reset_cache()


@contextlib.contextmanager
def impl(value):
    old = os.environ.get('PRINT_HANDY_BOUNDS_IMPL')
    os.environ['PRINT_HANDY_BOUNDS_IMPL'] = value
    mesh_native._reset_cache()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop('PRINT_HANDY_BOUNDS_IMPL', None)
        else:
            os.environ['PRINT_HANDY_BOUNDS_IMPL'] = old
        mesh_native._reset_cache()


def bits(value):
    """A comparison key that tells -0.0 from 0.0 (and is exact otherwise)."""
    if isinstance(value, float):
        return ('f', value.hex())
    if isinstance(value, (list, tuple)):
        return tuple(bits(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, bits(v)) for k, v in value.items()))
    return value


# ------------------------------------------------------------ meshes

def tetra(offset=(0.0, 0.0, 0.0), scale=1.0):
    p = [tuple(o + scale * v for o, v in zip(offset, q, strict=True))
         for q in ((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1))]
    return [[p[a], p[b], p[c]] for a, b, c in ((0, 2, 1), (0, 1, 3), (1, 2, 3), (0, 3, 2))]


def grid_box(n=6, size=(20.0, 12.0, 7.0)):
    """A closed box with each face split into an n x n grid of quads."""
    tris = []
    sx, sy, sz = size

    def face(origin, du, dv):
        for i in range(n):
            for j in range(n):
                def at(a, b):
                    return tuple(o + a / n * u + b / n * v for o, u, v in zip(origin, du, dv, strict=True))
                p00, p10, p11, p01 = at(i, j), at(i + 1, j), at(i + 1, j + 1), at(i, j + 1)
                tris.append([p00, p10, p11])
                tris.append([p00, p11, p01])
    face((0, 0, 0), (0, sy, 0), (sx, 0, 0))
    face((0, 0, sz), (sx, 0, 0), (0, sy, 0))
    face((0, 0, 0), (sx, 0, 0), (0, 0, sz))
    face((0, sy, 0), (0, 0, sz), (sx, 0, 0))
    face((0, 0, 0), (0, 0, sz), (0, sy, 0))
    face((sx, 0, 0), (0, sy, 0), (0, 0, sz))
    return tris


def uv_sphere(rings=12, segments=16, radius=9.0):
    """A UV sphere; the pole rows are needle (degenerate) triangles."""
    def at(i, j):
        theta, phi = math.pi * i / rings, 2 * math.pi * j / segments
        return (radius * math.sin(theta) * math.cos(phi), radius * math.sin(theta) * math.sin(phi),
                radius * math.cos(theta))
    tris = []
    for i in range(rings):
        for j in range(segments):
            a, b, c, d = at(i, j), at(i + 1, j), at(i + 1, j + 1), at(i, j + 1)
            tris.append([a, b, c])
            tris.append([a, c, d])
    return tris


def tie_grid(n=9):
    """A grid on 1/128 steps: corners sit on exact decimal ties at the sixth
    place (x.xxxxxx5), so round(v, 6) ties-to-even decides the welding, and
    some corners carry -0.0."""
    tris = []
    for i in range(n):
        for j in range(n):
            x0, x1 = (i - n // 2) / 128.0, (i + 1 - n // 2) / 128.0
            y0, y1 = -j / 128.0, -(j + 1) / 128.0
            z = -0.0 if (i + j) % 3 == 0 else 1 / 128.0 * ((i * j) % 5)
            tris.append([(x0, y0, z), (x1, y0, 0.0), (x1, y1, z)])
            tris.append([(x0, y0, z), (x1, y1, z), (x0 + 0.0, y1, -0.0)])
    return tris


def noisy(tris, seed=7, amplitude=4e-7):
    """Corners moved by less than the weld grid: some still weld, some split."""
    rng = random.Random(seed)
    return [[tuple(v + rng.uniform(-amplitude, amplitude) for v in p) for p in t] for t in tris]


MESHES = {
    'tetra': tetra(),
    'open-tetra': tetra()[:3],
    'flipped-face': tetra()[:3] + [list(reversed(tetra()[3]))],
    'duplicate-face': tetra() + [tetra()[1]],
    'vertex-touching': tetra() + tetra(offset=(0.0, 0.0, 0.0), scale=-1.0),
    'edge-sharing-fan': tetra() + tetra(offset=(0.0, 0.0, 0.0), scale=1.0)[:1] + [[(0, 0, 0), (0, 1, 0), (-1, 0.5, 0)]],
    'grid-box': grid_box(),
    'two-boxes': grid_box(3) + [[tuple(v + 40.0 for v in p) for p in t] for t in grid_box(3)],
    'uv-sphere': uv_sphere(),
    'tie-grid': tie_grid(),
    'noisy-box': noisy(grid_box(4)),
    'needles': tetra() + [[(0, 0, 0), (0, 0, 1e-7), (1, 0, 0)], [(2, 2, 2), (2, 2, 2), (3, 3, 3)],
                          [(0, 0, 0), (1e-10, 0, 0), (0, 1e-10, 0)]],
    'large-offsets': [[tuple(v + 1.0e6 for v in p) for p in t] for t in grid_box(2)]
                     + [[tuple(v * 1e-3 - 5e9 for v in p) for p in t] for t in tetra()],
}


def as_compact(tris):
    out = CompactTriangles()
    for t in tris:
        out.append(t)
    return out


def as_indexed(tris):
    index, vertices, faces = {}, [], []
    for t in tris:
        row = []
        for p in t:
            key = tuple(float(v).hex() for v in p)
            if key not in index:
                index[key] = len(vertices)
                vertices.append([float(v) for v in p])
            row.append(index[key])
        faces.append(row)
    return IndexedTriangles(np.array(vertices, dtype=np.float64).reshape((-1, 3)),
                            np.array(faces, dtype=np.int64).reshape((-1, 3)))


def chained(base, angle=37.0, offset=(3.25, -1.5, 0.0078125)):
    rad = math.radians(angle)
    rotated = TransformedTriangles.rotation_z(base, math.cos(rad), math.sin(rad))
    return TransformedTriangles.translation(rotated, *offset)


def representations(tris):
    compact = as_compact(tris)
    indexed = as_indexed(tris)
    return {'compact': compact, 'indexed': indexed,
            'compact+rotz': chained(compact), 'indexed+rotz': chained(indexed, 90.0),
            'compact+rotz0': chained(compact, 0.0, (0.0, -0.0, 1.0))}


CASES_GRID = [(name, rep) for name in MESHES
              for rep in ('compact', 'indexed', 'compact+rotz', 'indexed+rotz', 'compact+rotz0')]


def _materialize(triangles):
    """The Python oracle's input: plain lists, which the native path never takes."""
    return [list(t) for t in triangles]


# ------------------------------------------------------------ differential


@pytest.mark.parametrize('name,rep', CASES_GRID)
def test_topology_matches_both_python_paths(native, name, rep):
    triangles = representations(MESHES[name])[rep]
    with impl('native'):
        assert mesh_native.mesh_args(triangles) is not None
        got = audit.mesh_topology(triangles)
    rows = _materialize(triangles)
    with impl('python'):
        small = audit.mesh_topology(rows)
        compact = compact_topology.mesh_topology_compact(rows)
    assert got == small == compact, name
    if isinstance(triangles, IndexedTriangles):
        with impl('python'):
            assert compact_topology.mesh_topology_compact(triangles) == got


@pytest.mark.parametrize('name,rep', CASES_GRID)
def test_measure_matches_the_scalar_loops(native, name, rep):
    triangles = representations(MESHES[name])[rep]
    with impl('native'):
        lo, hi, area, volume = mesh_native.measure(triangles)
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
def test_threemf_text_matches_the_python_loop(native, name, rep):
    triangles = representations(MESHES[name])[rep]
    with impl('native'):
        got = geometry_layout._mesh_xml_lines(triangles)
        assert mesh_native.threemf_text(triangles) is not None
    with impl('python'):
        want = geometry_layout._mesh_xml_lines(triangles)
    assert '\n'.join(got[0]) == '\n'.join(want[0])
    assert '\n'.join(got[1]) == '\n'.join(want[1])


@pytest.mark.parametrize('name', sorted(MESHES))
def test_weld_matches_python_welding(native, name):
    triangles = as_compact(MESHES[name])
    with impl('native'):
        vertices, faces = mesh_native.weld(triangles, round6=True)
        exact_vertices, exact_faces = mesh_native.weld(triangles, round6=False)
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


def _round6_values(seed=1790):
    """About 3 M finite doubles aimed at round(x, 6)'s hard cases."""
    rng = np.random.default_rng(seed)
    parts = []
    # Random magnitudes from 1e-12 to 1e15, both signs (the fast path ends
    # at 4e9, the slow path and the integer shortcut cover the rest).
    exponents = rng.uniform(-12, 15, 600_000)
    parts.append(rng.choice([-1.0, 1.0], 600_000) * 10.0 ** exponents)
    # Plain coordinates in the millimeter range.
    parts.append(rng.uniform(-500.0, 500.0, 600_000))
    # Decimal ties (k + 0.5) / 1e6 and their neighbors, two ulps each way:
    # the nearest double is just above or below the tie, so the result
    # depends on the exact binary value, which is what CPython rounds.
    k = rng.integers(-5_000_000_000, 5_000_000_000, 200_000).astype(np.float64)
    ties = (k + 0.5) / 1e6
    for step in range(-2, 3):
        moved = ties.copy()
        for _ in range(abs(step)):
            moved = np.nextafter(moved, np.inf if step > 0 else -np.inf)
        parts.append(moved)
    # Exact binary ties at the sixth decimal (multiples of 1/128 and 1/2**k),
    # where ties to even decides.
    grid = rng.integers(-2 ** 40, 2 ** 40, 300_000).astype(np.float64)
    parts.append(grid / 128.0)
    parts.append(grid / 2.0 ** rng.integers(7, 30, 300_000))
    # Around the fast path's limits and the 2**52 integer shortcut.
    for edge in (4.0e9, 2.0 ** 52, 1e6, 5e-7, 1e-6):
        around = np.full(20_000, edge)
        offsets = rng.integers(-400, 400, 20_000)
        for i, n in enumerate(offsets):
            around[i] = np.nextafter(edge, np.inf if n > 0 else -np.inf) if n else edge
        around += rng.uniform(-edge * 1e-9, edge * 1e-9, 20_000)
        parts.extend([around, -around])
    values = np.concatenate(parts)
    values = values[np.isfinite(values)]
    return np.concatenate([values, [0.0, -0.0, 5e-7, -5e-7, 4e-7, -4e-7, 1e300, -1e300, 5e-324]])


def test_round6_matches_cpython_round(native):
    """The kernel's round(x, 6) against CPython's real round(), value by value.

    mesh_weld with round6 returns, for every corner, the rounded value of
    its welded key; corners whose keys are equal share one stored value, so
    the comparison is by value (signed zeros are pinned separately below).
    """
    values = _round6_values()
    pad = (-len(values)) % 9
    soup = np.concatenate([values, np.zeros(pad)])
    welded, ids = native.mesh_weld(soup, None, [], True)
    rounded = np.frombuffer(welded, dtype='<f8').reshape((-1, 3))
    per_corner = rounded[np.frombuffer(ids, dtype='<i8')].reshape(-1)[:len(values)]
    want = np.array([round(float(v), 6) for v in values.tolist()], dtype=np.float64)
    mismatch = np.flatnonzero(per_corner != want)
    assert len(values) > 3_000_000
    assert not len(mismatch), [(values[i].hex(), per_corner[i], want[i]) for i in mismatch[:5]]


def test_round6_keeps_the_sign_of_zero_like_cpython(native):
    for value in (-4e-7, -1e-12, -0.0, 4e-7, 0.0, -5e-7, 5e-7):
        soup = array('d', [value, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])
        welded, _ = native.mesh_weld(soup, None, [], True)
        got = np.frombuffer(welded, dtype='<f8')[0]
        assert bits(float(got)) == bits(round(value, 6)), value


def test_exact_cache_does_not_leak_between_keys(native):
    """Repeats, signed zeros and values that round to zero, mixed: the bits
    cache must hand back exactly what the Python welder assigns."""
    rng = random.Random(1790)
    pool = [0.0000004, -0.0000004, 0.0, -0.0, 0.0000005, -0.0000005, 0.0000006,
            1.0000004, 1.0000005, 0.9999996, 1.0, 0.0078125, 0.0078135]
    tris = [[tuple(rng.choice(pool) for _ in range(3)) for _ in range(3)] for _ in range(4000)]
    triangles = as_compact(tris)
    with impl('native'):
        vertices, faces = mesh_native.weld(triangles, round6=True)
        text = mesh_native.threemf_text(triangles)
    keys, order, ids = {}, [], []
    for t in tris:
        for p in t:
            key = tuple(round(float(v), 6) for v in p)
            if key not in keys:
                keys[key] = len(order)
                order.append(key)
            ids.append(keys[key])
    assert bits([tuple(v) for v in vertices.tolist()]) == bits(order)
    assert faces.reshape(-1).tolist() == ids
    with impl('python'):
        want = geometry_layout._mesh_xml_lines(triangles)
    assert text == ('\n'.join(want[0]), '\n'.join(want[1]))


def test_transform_matches_the_python_lambdas(native):
    base = as_compact(MESHES['tie-grid'] + MESHES['uv-sphere'])
    chain = chained(base, 123.0, (-2.5, 0.0078125, -0.0))
    vertices, faces, ops = mesh_native.mesh_args(chain)
    soup = np.frombuffer(native.mesh_transform(vertices, faces, mesh_native._wire_ops(ops)), dtype='<f8')
    want = [v for t in chain for p in t for v in p]
    assert bits(soup.tolist()) == bits(want)


def test_large_mesh_matches_the_compact_python_path(native):
    # Over the 100k threshold, where the Python topology switches to its
    # compact union-find, and over the 32768-face numpy threshold.
    tris = list(make_models.torus_triangles(major_segments=320, minor_segments=160))
    tris += [[(0.0, 0.0, 30.0), (1.0, 0.0, 30.0), (0.0, 1.0, 30.0)]]
    compact = as_compact(tris)
    with impl('native'):
        got = audit.mesh_topology(compact)
        text = mesh_native.threemf_text(chained(compact))
        measured = mesh_native.measure(compact)
    with impl('python'):
        want = audit.mesh_topology(compact)
        want_text = geometry_layout._mesh_xml_lines(chained(compact))
        want_area = geometry_layout._triangle_area_and_signed_volume(compact)
    assert got == want
    assert got['boundary_edges'] == 3 and got['connected_shells'] == 2
    assert text == ('\n'.join(want_text[0]), '\n'.join(want_text[1]))
    assert bits(measured[2]) == bits(want_area[0])
    assert bits(abs(measured[3])) == bits(want_area[1])


# ------------------------------------------------------------ STL parsing

def _binary_stl(path, tris, normals=None):
    import struct
    with open(path, 'wb') as out:
        out.write(b'solid but actually binary'.ljust(80, b'\0'))
        out.write(struct.pack('<I', len(tris)))
        for i, t in enumerate(tris):
            normal = normals[i] if normals else (0.0, 0.0, 1.0)
            out.write(struct.pack('<12fH', *normal, *[v for p in t for v in p], 7))


ASCII_TEXTS = {
    'plain': b'solid x\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\n'
             b'endloop\nendfacet\nendsolid x\n',
    'number-forms': b'solid\n vertex 1. .5 -0\n\tvertex\t+2 1e5 1.E-3\nvertex 3.25e+2   -0.0078125 7\nendsolid',
    'separators': b'vertex\x1c1\x1d2\x1e3\x0bvertex\x1f4 5 6\rvertex 7\x0c8 9',
    'embedded-word': b'endvertex 1 2 3 vertexvertex 4 5 6\nvertex 7 8 9 vertex 1 1 1\n',
    'crlf': b'solid a\r\n  facet normal 0 0 0\r\n    outer loop\r\n      vertex 1 2 3\r\n'
            b'      vertex 4 5 6\r\n      vertex 7 8 9\r\n    endloop\r\n  endfacet\r\nendsolid a\r\n',
    # Outside the native grammar: Python's float() decides.
    'underscores': b'vertex 1_0 2 3\nvertex 4 5 6\nvertex 7 8 9\n',
    'inf-nan': b'vertex inf 2 3\nvertex 4 -nan 6\nvertex 7 8 9\n',
    'non-ascii': 'vertex 1 2 3\nvertex 4 5 6\nvertex 7 8 9 é\n'.encode(),
    'garbage-token': b'vertex 1 2 3\nvertex 4 5 6\nvertex 7 8 x9\n',
    'unreadable': b'solid nothing here\n',
    'short-count': b'vertex 1 2 3\nvertex 4 5 6\n',
}


def _read_outcome(path):
    try:
        result = read_stl_triangles(path)
    except Exception as exc:  # noqa: BLE001 - the outcome under test
        return ('raise', type(exc).__name__, str(exc))
    return (type(result).__name__, bits([[tuple(p) for p in t] for t in result]))


@pytest.mark.parametrize('name', sorted(ASCII_TEXTS))
def test_ascii_stl_reads_like_python(native, tmp_path, name):
    path = tmp_path / f'{name}.stl'
    path.write_bytes(ASCII_TEXTS[name])
    with impl('native'):
        got = _read_outcome(path)
    with impl('python'):
        want = _read_outcome(path)
    assert got == want


def test_binary_stl_reads_like_python(native, tmp_path):
    path = tmp_path / 'b.stl'
    tris = MESHES['uv-sphere'] + MESHES['tie-grid'] + [[(float('nan'), 0.0, float('inf')), (0.1, 0.2, 0.3), (-0.0, 0, 0)]]
    _binary_stl(path, tris)
    with impl('native'):
        got = _read_outcome(path)
    with impl('python'):
        want = _read_outcome(path)
    assert got == want
    # The kernel's own binary parser (for mesh tools; intake streams binary
    # files through numpy to bound memory) reads the same values.
    kind, coords = native.stl_parse(path.read_bytes())
    assert kind == 'binary'
    assert bits(np.frombuffer(coords, dtype='<f8').tolist()) == bits([v for t in want[1] for p in t for v in p])
    # The numpy widening (no wheel) equals the original per-row unpack.
    import struct
    data = path.read_bytes()
    rows = [list(row[3:12]) for row in struct.iter_unpack('<12fH', data[84:])]
    from print_handy.core.orca_adapter import _stl_binary_coordinates
    widened = np.frombuffer(_stl_binary_coordinates(data[84:]), dtype=np.float64)
    assert bits(widened.tolist()) == bits([v for row in rows for v in row])


# ------------------------------------------------------------ corpus and combine

def _case_names():
    names = []
    for case in sorted(CASES.iterdir()):
        toml = case / 'case.toml'
        if toml.is_file() and 'model' in tomllib.loads(toml.read_text(encoding='utf-8')):
            names.append(case.name)
    return names


CORPUS = _case_names()
# Combining hundreds of parts packs them one search node at a time in
# Python on both paths; those cases still compare their load.
MAX_COMBINE_PARTS = 40


def _render(name, tmp_path):
    data = tomllib.loads((CASES / name / 'case.toml').read_text(encoding='utf-8'))
    target = tmp_path / f'{name}{make_models.model_suffix(data["model"])}'
    make_models.render(data, target)
    return target


def _combine_outcome(target, sources):
    try:
        result = model_parts.combine(target, [str(s) for s in sources], [220.0, 220.0, 250.0])
    except Exception as exc:  # noqa: BLE001 - the outcome under test
        return ('raise', type(exc).__name__, str(exc))
    with zipfile.ZipFile(target) as archive:
        return result['objects'], result['on_one_bed'], archive.read('3D/3dmodel.model')


def _load_outcome(path):
    try:
        parts, unit, factor, warnings = geometry_layout.load_parts(path)
    except Exception as exc:  # noqa: BLE001 - the outcome under test
        return ('raise', type(exc).__name__, str(exc))
    return (unit, factor, warnings,
            [bits(geometry_layout.describe_triangles(p['triangles'])) for p in parts])


def test_corpus_has_models():
    assert len(CORPUS) >= 40


@pytest.mark.parametrize('name', CORPUS)
def test_corpus_model_loads_and_combines_identically(native, tmp_path, name):
    model = _render(name, tmp_path)
    with impl('native'):
        loaded = _load_outcome(model)
    with impl('python'):
        want_loaded = _load_outcome(model)
    assert loaded == want_loaded
    if loaded[0] != 'raise' and len(loaded[3]) > MAX_COMBINE_PARTS:
        return
    with impl('native'):
        combined = _combine_outcome(tmp_path / 'native.3mf', [model])
    with impl('python'):
        want_combined = _combine_outcome(tmp_path / 'python.3mf', [model])
    assert combined == want_combined


def test_corpus_stl_files_combine_identically(native, tmp_path):
    stls = []
    for name in CORPUS:
        data = tomllib.loads((CASES / name / 'case.toml').read_text(encoding='utf-8'))
        if make_models.model_suffix(data['model']) == '.stl':
            stls.append(_render(name, tmp_path))
    assert len(stls) >= 6
    served = []
    real_call = mesh_native._call

    def counting(name, *args):
        result = real_call(name, *args)
        served.append((name, result is not None))
        return result
    # The packed path (fits one bed) and the row path (does not).
    for sources in (stls[:2], stls[:4], stls):
        with impl('native'), pytest.MonkeyPatch.context() as patch:
            patch.setattr(mesh_native, '_call', counting)
            combined = _combine_outcome(tmp_path / 'native.3mf', sources)
        with impl('python'):
            want = _combine_outcome(tmp_path / 'python.3mf', sources)
        assert combined == want, [s.name for s in sources]
    # The native side really served every stage (not a silent fallback).
    assert {name for name, ok in served if ok} >= {
        'stl_parse', 'mesh_topology', 'mesh_measure', 'threemf_mesh_text'}


def test_generated_stl_pair_combines_identically(native, tmp_path):
    first, second = tmp_path / 'sphere.stl', tmp_path / 'box.stl'
    _binary_stl(first, MESHES['uv-sphere'] + MESHES['tie-grid'])
    _binary_stl(second, MESHES['noisy-box'])
    for bed in ([220.0, 220.0, 250.0], [30.0, 30.0, 30.0]):
        with impl('native'):
            got = model_parts.combine(tmp_path / 'n.3mf', [str(first), str(second)], bed)
            got_model = zipfile.ZipFile(tmp_path / 'n.3mf').read('3D/3dmodel.model')
        with impl('python'):
            want = model_parts.combine(tmp_path / 'p.3mf', [str(first), str(second)], bed)
            want_model = zipfile.ZipFile(tmp_path / 'p.3mf').read('3D/3dmodel.model')
        assert got['objects'] == want['objects'] and got['on_one_bed'] == want['on_one_bed']
        assert got_model == want_model


# ------------------------------------------------------------ dispatcher

def test_kernel_version_handshake(native):
    assert native.MESH_KERNEL_VERSION == mesh_native.MESH_KERNEL_VERSION
    with impl('auto'):
        assert mesh_native.available()


def _fake(monkeypatch, **attrs):
    real = _native_module()
    module = types.SimpleNamespace(**{name: getattr(real, name) for name in (
        'MESH_KERNEL_VERSION', 'mesh_topology', 'mesh_measure', 'mesh_transform', 'mesh_weld',
        'threemf_mesh_text', 'stl_parse', 'MeshDecline')} if real else {})
    for name, value in attrs.items():
        setattr(module, name, value)
    monkeypatch.setitem(sys.modules, 'print_handy.core._bounds_native', module)
    import print_handy.core as core
    monkeypatch.setattr(core, '_bounds_native', module, raising=False)
    return module


class _Decline(ValueError):
    pass


def _python_topology(triangles):
    with impl('python'):
        return audit.mesh_topology(triangles)


def test_wrong_version_falls_back_to_python(monkeypatch):
    _fake(monkeypatch, MESH_KERNEL_VERSION=999, MeshDecline=_Decline)
    tris = as_compact(MESHES['grid-box'])
    with impl('auto'):
        assert mesh_native.topology(tris) is None
        assert audit.mesh_topology(tris) == _python_topology(tris)
    with impl('native'), pytest.raises(RuntimeError, match='required but unavailable'):
        mesh_native.topology(tris)


def test_self_check_mismatch_falls_back(monkeypatch):
    _fake(monkeypatch, MESH_KERNEL_VERSION=mesh_native.MESH_KERNEL_VERSION, MeshDecline=_Decline,
          mesh_topology=lambda *a: {'triangles': 0}, mesh_measure=lambda *a: None,
          threemf_mesh_text=lambda *a: ('', ''))
    with impl('auto'):
        assert mesh_native.topology(as_compact(MESHES['tetra'])) is None
        assert not mesh_native.available()


def test_decline_uses_python_without_disabling(native, monkeypatch):
    tris = as_compact(MESHES['tetra'] + [[(float('nan'), 0, 0), (1, 0, 0), (0, 1, 0)]])
    with impl('native'):
        assert mesh_native.topology(tris) is None  # a NaN corner: Python decides
        assert mesh_native.topology(as_compact(MESHES['tetra'])) is not None
        assert audit.mesh_topology(tris) == _python_topology(tris)


def test_self_check_fault_falls_back(native, monkeypatch):
    def boom(*args):
        raise RuntimeError('native mesh kernel panicked')
    _fake(monkeypatch, mesh_measure=boom)
    tris = chained(as_compact(MESHES['grid-box']))
    with impl('auto'):
        got = mesh_fast.bounds_and_volume(tris)
        assert not mesh_native.available()
    with impl('python'):
        assert got == mesh_fast.bounds_and_volume(tris)
    with impl('native'), pytest.raises(RuntimeError, match='required but unavailable'):
        mesh_native.measure(tris)


def test_kernel_fault_after_verification_is_logged_once(native, monkeypatch, caplog):
    calls = []
    real_measure = native.mesh_measure

    def flaky(*args):
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError('native mesh kernel panicked')
        return real_measure(*args)
    _fake(monkeypatch, mesh_measure=flaky)
    tris = chained(as_compact(MESHES['grid-box']))
    with impl('auto'):
        assert mesh_native.available()
        got = mesh_fast.bounds_and_volume(tris)
        assert mesh_native.measure(tris) is None  # disabled after the fault
    assert 'native mesh kernel failed' in caplog.text
    with impl('python'):
        assert got == mesh_fast.bounds_and_volume(tris)


def test_python_setting_never_imports(monkeypatch):
    _fake(monkeypatch, MESH_KERNEL_VERSION=None)
    with impl('python'):
        assert mesh_native.topology(as_compact(MESHES['tetra'])) is None
        assert not mesh_native.available()
