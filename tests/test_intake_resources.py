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


def test_large_download_accepted(tmp_path):
    """A download over the former 200 MiB cap is streamed successfully. (#1824)"""
    target = tmp_path / 'large.stl'
    # 201 MiB of data; no byte cap applies.
    data = b'x' * (201 * limits.MIB)
    limits.save_download(io.BytesIO(data), target)
    assert target.stat().st_size == len(data)
    # Partial file is still removed on error (existing contract).
    target.unlink()
    class Boom(Exception):
        pass
    def boom_read(n):
        raise Boom()
    with pytest.raises(Boom):
        limits.save_download(type('R', (), {'read': staticmethod(boom_read)})(), target)
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


def test_download_streams_exact_bytes(tmp_path):
    target = tmp_path / 'exact.stl'
    limits.save_download(io.BytesIO(b'12345678'), target)
    assert target.read_bytes() == b'12345678'


def test_stl_triangle_budget(tmp_path):
    import struct
    source = tmp_path / 'binary.stl'
    source.write_bytes(b' ' * 80 + struct.pack('<I', 2) + b' ' * 100)
    with patch.object(limits, 'MAX_TRIANGLES', 1):
        with pytest.raises(limits.IntakeLimitExceeded, match='triangle'):
            limits.check_source(source)


def test_large_stl_accepted(tmp_path):
    """An STL larger than the former 32 MiB per-file cap is accepted when
    its triangle count is under the geometry budget. (#1824)
    """
    import struct
    # ~40 MiB binary STL: 84 + 50 * triangles
    triangles = 800_000
    size = 84 + 50 * triangles
    assert size > 32 * limits.MIB
    source = tmp_path / 'large.stl'
    # Header + triangle count; the rest can be zeros (valid enough for the size match).
    source.write_bytes(b' ' * 80 + struct.pack('<I', triangles) + b'\0' * (size - 84))
    # Should not refuse on byte size; triangle check passes.
    limits.check_source(source)


def _ascii_stl(facets):
    facet = (b'facet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex 1 0 0\n'
             b'  vertex 0 1 0\n endloop\nendfacet\n')
    return b'solid part\n' + facet * facets + b'endsolid part\n'


def test_ascii_stl_triangle_budget(tmp_path):
    """ASCII STL has no header count; its facets are counted instead (#1833)."""
    from print_handy.core import geometry_layout
    source = tmp_path / 'ascii.stl'
    source.write_bytes(_ascii_stl(3))
    with patch.object(limits, 'MAX_TRIANGLES', 2):
        with pytest.raises(limits.IntakeLimitExceeded, match='triangle'):
            limits.check_source(source)
        # The production loader runs the same guard before reading the mesh.
        with pytest.raises(limits.IntakeLimitExceeded, match='triangle'):
            geometry_layout.load_parts(source)
    with patch.object(limits, 'MAX_TRIANGLES', 3):
        limits.check_source(source)


def test_ascii_stl_facets_counted_across_read_chunks(tmp_path):
    source = tmp_path / 'ascii-large.stl'
    data = _ascii_stl(limits.CHUNK)  # several read chunks, tokens straddle boundaries
    source.write_bytes(data)
    with patch.object(limits, 'MAX_TRIANGLES', limits.CHUNK - 1):
        with pytest.raises(limits.IntakeLimitExceeded, match='triangle'):
            limits.check_source(source)
    with patch.object(limits, 'MAX_TRIANGLES', limits.CHUNK):
        limits.check_source(source)


def test_malformed_stl_is_sized_as_binary(tmp_path):
    """A file that is neither a size-matched binary STL nor ASCII cannot skip the guard."""
    source = tmp_path / 'garbage.stl'
    source.write_bytes(b'\0' * 80 + b'\x01\0\0\0' + b'\x07' * (50 * 5 + 3))
    with patch.object(limits, 'MAX_TRIANGLES', 4):
        with pytest.raises(limits.IntakeLimitExceeded, match='triangle'):
            limits.check_source(source)
    with patch.object(limits, 'MAX_TRIANGLES', 5):
        limits.check_source(source)


def test_download_stops_before_filling_the_disk(tmp_path):
    """A download that would leave the volume under MIN_FREE_BYTES is refused."""
    target = tmp_path / 'full.stl'
    usage = type('U', (), {'free': limits.MIN_FREE_BYTES})()
    with patch('print_handy.core.intake_resources.shutil.disk_usage', return_value=usage):
        with pytest.raises(limits.IntakeLimitExceeded, match='free disk') as refused:
            limits.save_download(io.BytesIO(b'x' * 4096), target)
    assert not target.exists()
    # The operator's disk is short, not the model: no mesh-editing advice. (#1833)
    assert 'Simplify' not in str(refused.value)
    assert 'low on disk space' in str(refused.value)


def test_non_archive_3mf_fails_cleanly(tmp_path):
    """A .3mf that is not a zip is rejected by the zip parser, not by a byte-size cap. (#1824)"""
    from print_handy.core import model_job, source_colors, geometry_layout
    source = tmp_path / 'notazip.3mf'
    source.write_bytes(b'not even a zip')
    for entry in (model_job.audit, source_colors.inventory, geometry_layout.load_parts):
        with pytest.raises(zipfile.BadZipFile):
            entry(source)
