"""Per-slice Python overhead: the resource guard and clone placement (#1779)."""
import struct
import zipfile

import numpy as np
import pytest

from print_handy.core import intake_resources, orca_adapter, source_colors
from print_handy.core.compact_geometry import IndexedTriangles

CORE = 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'


def _tetra_3mf(path, corner=0):
    vertices = [(corner, 0, 0), (corner + 10, 0, 0), (corner, 10, 0), (corner, 0, 10)]
    triangles = [(0, 2, 1), (0, 1, 3), (1, 2, 3), (0, 3, 2)]
    body = ''.join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in vertices)
    faces = ''.join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in triangles)
    model = (f'<?xml version="1.0"?><model unit="millimeter" xmlns="{CORE}"><resources>'
             f'<object id="1" type="model"><mesh><vertices>{body}</vertices>'
             f'<triangles>{faces}</triangles></mesh></object></resources>'
             '<build><item objectid="1"/></build></model>')
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('3D/3dmodel.model', model)
    return path


@pytest.fixture
def guard_calls(monkeypatch):
    calls = []
    real = intake_resources.check_source

    def counted(path):
        calls.append(path.name if hasattr(path, 'name') else str(path))
        return real(path)
    monkeypatch.setattr(intake_resources, 'check_source', counted)
    return calls


def test_scope_runs_the_resource_guard_once_per_distinct_input(tmp_path, guard_calls):
    first = _tetra_3mf(tmp_path / 'first.3mf')
    second = _tetra_3mf(tmp_path / 'second.3mf', corner=3)
    with source_colors.inventory_scope():
        for _ in range(3):
            source_colors.inventory(first)
        source_colors.inventory(second)
        source_colors.inventory(second)
    # One guard run per distinct input; the base ran it on every call (5).
    assert guard_calls == ['first.3mf', 'second.3mf']
    # Outside a scope nothing is shared: every call is guarded.
    source_colors.inventory(first)
    source_colors.inventory(first)
    assert guard_calls[2:] == ['first.3mf', 'first.3mf']


def test_scope_still_refuses_a_distinct_hostile_input_every_time(tmp_path, monkeypatch):
    good = _tetra_3mf(tmp_path / 'good.3mf')
    bad = _tetra_3mf(tmp_path / 'bad.3mf', corner=7)
    monkeypatch.setattr(intake_resources, 'MAX_TRIANGLES', 3)
    # The recorded-counts shortcut must not hide the streamed refusal.
    monkeypatch.setattr(intake_resources, '_recorded_counts', lambda *a, **k: None)
    with source_colors.inventory_scope():
        for _ in range(2):  # a refusal is never cached
            with pytest.raises(intake_resources.IntakeLimitExceeded):
                source_colors.inventory(bad)
        monkeypatch.setattr(intake_resources, 'MAX_TRIANGLES', 10)
        source_colors.inventory(good)
        monkeypatch.setattr(intake_resources, 'MAX_TRIANGLES', 3)
        # `good` is already checked in this scope (same bytes); `bad` is a different input.
        source_colors.inventory(good)
        with pytest.raises(intake_resources.IntakeLimitExceeded):
            source_colors.inventory(bad)


def _scalar_clones(triangles, shifts):
    return [[(p[0] + dx, p[1] + dy, p[2]) for p in tri] for dx, dy in shifts for tri in triangles]


def test_clone_replication_matches_the_scalar_loop_byte_for_byte(tmp_path):
    rng = np.random.default_rng(1779)
    vertices = rng.uniform(-40, 40, size=(50, 3))
    faces = rng.integers(0, 50, size=(120, 3))
    indexed = IndexedTriangles(vertices, faces)
    shifts = [(0.0, 0.0), (61.37, 0.0), (0.0, 55.125), (61.37, 55.125)]
    fast = orca_adapter._shifted_copies(indexed, shifts)
    assert isinstance(fast, IndexedTriangles)
    assert [list(t) for t in fast] == _scalar_clones(indexed, shifts)
    a, b = tmp_path / 'fast.stl', tmp_path / 'scalar.stl'
    res_fast = orca_adapter._place_triangles(fast, a, bed=(220, 220))
    res_scalar = orca_adapter._place_triangles(_scalar_clones(indexed, shifts), b, bed=(220, 220))
    assert a.read_bytes() == b.read_bytes()
    assert res_fast == {**res_scalar, 'converted_sha256': res_fast['converted_sha256']}
    assert struct.unpack('<I', a.read_bytes()[80:84])[0] == len(indexed) * len(shifts)


def test_clone_replication_keeps_the_scalar_path_for_plain_lists():
    tris = [[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)]]
    assert orca_adapter._shifted_copies(tris, [(5.0, 6.0)]) == [[(5.0, 6.0, 0.0), (6.0, 6.0, 0.0), (5.0, 7.0, 0.0)]]
