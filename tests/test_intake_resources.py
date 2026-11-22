"""Synthetic resource regressions: never redistribute private model fixtures."""
import io
import zipfile
from unittest.mock import patch

import pytest

from print_handy.core import intake_resources as limits


def test_large_mesh_streams_successfully(tmp_path):
    from print_handy.core import mesh_fast
    import tracemalloc

    source = tmp_path / 'large.3mf'
    # catduck's still-print contract replaces the obsolete 32 MiB refusal:
    # ordinary large geometry must succeed, not merely fail cheaply.
    count = 900_000
    with zipfile.ZipFile(source, 'w', zipfile.ZIP_DEFLATED) as archive:
        with archive.open('3D/Objects/object_1.model', 'w') as member:
            member.write(b'<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources><object id="1"><mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/></vertices><triangles>')
            batch = b'<triangle v1="0" v2="1" v3="2"/>' * 1000
            for _ in range(count // 1000):
                member.write(batch)
            member.write(b'</triangles></mesh></object></resources></model>')
    limits.check_source(source)
    mesh_fast._TABLE_CACHE.clear()
    tracemalloc.start()
    try:
        with zipfile.ZipFile(source) as archive:
            structure, tables = mesh_fast.archive_tables(archive, '3D/Objects/object_1.model')
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(tables['1'].triangles) == count
    assert tables['1'].triangles.nbytes == count * 12
    assert len(structure) < 1024
    assert peak < 64 * limits.MIB


def test_geometry_memory_ceiling(tmp_path):
    source = tmp_path / 'ceiling.3mf'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('a.model', '<model><triangle/><triangle/></model>')
    with patch.object(limits, 'MAX_ESTIMATED_GEOMETRY_BYTES', 160):
        with pytest.raises(limits.IntakeLimitExceeded, match='geometry allocation'):
            limits.check_source(source)


def test_download_limit_ignores_missing_or_false_content_length(tmp_path):
    target = tmp_path / 'model.3mf'
    with patch.object(limits, 'MAX_DOWNLOAD_BYTES', 8):
        with pytest.raises(limits.IntakeLimitExceeded, match='download'):
            limits.save_download(io.BytesIO(b'x' * 9), target)
    assert not target.exists()


def test_triangle_budget_before_dom_parse(tmp_path):
    source = tmp_path / 'mesh.3mf'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('3D/3dmodel.model', '<model><triangle/><triangle/></model>')
    with patch.object(limits, 'MAX_TRIANGLES', 1):
        with pytest.raises(limits.IntakeLimitExceeded, match='triangle'):
            limits.check_source(source)


def test_small_model_allowed(tmp_path):
    source = tmp_path / 'small.3mf'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('3D/3dmodel.model', '<model><triangle/></model>')
    limits.check_source(source)


@pytest.mark.parametrize(('setting', 'value', 'members', 'message'), [
    ('MAX_MEMBERS', 1, [('a', b''), ('b', b'')], 'members'),
    ('MAX_EXPANDED_BYTES', 3, [('asset', b'1234')], 'expanded archive'),
    ('MAX_MEMBER_BYTES', 3, [('asset', b'1234')], 'archive member'),
    ('MAX_XML_BYTES', 3, [('a.model', b'12'), ('b.model', b'34')], 'model XML'),
    ('MAX_VERTICES', 1, [('a.model', b'<m><vertex/><vertex/></m>')], 'vertex'),
    ('MAX_XML_ELEMENTS', 1, [('a.model', b'<m><x/></m>')], 'element'),
])
def test_archive_budgets(tmp_path, setting, value, members, message):
    source = tmp_path / 'budget.3mf'
    with zipfile.ZipFile(source, 'w') as archive:
        for name, data in members:
            archive.writestr(name, data)
    with patch.object(limits, setting, value):
        with pytest.raises(limits.IntakeLimitExceeded, match=message):
            limits.check_source(source)


def test_dtd_refused(tmp_path):
    source = tmp_path / 'dtd.3mf'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('a.model', '<!DOCTYPE m [<!ENTITY a "x">]><m>&a;</m>')
    with pytest.raises(limits.IntakeLimitExceeded, match='document type'):
        limits.check_source(source)


def test_exact_download_boundary(tmp_path):
    target = tmp_path / 'exact.stl'
    with patch.object(limits, 'MAX_DOWNLOAD_BYTES', 8):
        limits.save_download(io.BytesIO(b'12345678'), target)
    assert target.read_bytes() == b'12345678'


def test_stl_triangle_budget(tmp_path):
    import struct
    source = tmp_path / 'binary.stl'
    source.write_bytes(b' ' * 80 + struct.pack('<I', 2) + b' ' * 100)
    with patch.object(limits, 'MAX_TRIANGLES', 1):
        with pytest.raises(limits.IntakeLimitExceeded, match='triangle'):
            limits.check_source(source)


def test_oversized_direct_entry_points_never_parse(tmp_path):
    from print_handy.core import source_colors, geometry_layout
    from print_handy.jobs import audit as jobs_audit
    source = tmp_path / 'large.3mf'
    source.write_bytes(b'not even a zip')
    with patch.object(limits, 'MAX_DOWNLOAD_BYTES', 1):
        for entry in (jobs_audit.audit, source_colors.inventory, geometry_layout.load_parts):
            with pytest.raises(limits.IntakeLimitExceeded, match='file exceeds'):
                entry(source)
