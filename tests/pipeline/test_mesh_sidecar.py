"""Load once per intake: the combine step's mesh tables (#1790).

``model_parts.combine`` records the welded mesh tables of the combined 3MF
beside it, and the source check and every 3MF mesh reader
(``mesh_fast.archive_tables``: the audit, the color inventory, layout) use
them instead of parsing the model XML again. The record must never change a
result: its tables, structure XML and element counts equal a real parse of
the same file bit for bit, a record that does not match the file byte for
byte is ignored, and the audit of the combined file is the same with or
without it.
"""
import shutil
import zipfile

import numpy as np
import pytest

from print_handy.core import intake_resources, mesh_fast, mesh_native, mesh_numpy, mesh_sidecar, model_parts
from print_handy.core import model_job as mj
from tests.pipeline.test_native_mesh import CORPUS, CASES, MESHES, _binary_stl, _native_module, _render, impl
from tests.corpus import make_models

import tomllib


def _same_tables(got, want):
    assert list(got) == list(want)
    for oid in want:
        assert got[oid].vertices.dtype == want[oid].vertices.dtype == np.float64
        assert got[oid].triangles.dtype == want[oid].triangles.dtype == np.int32
        # Bitwise: -0.0 and 0.0 must not compare equal here.
        assert np.array_equal(got[oid].vertices.view(np.int64), want[oid].vertices.view(np.int64)), oid
        assert np.array_equal(got[oid].triangles, want[oid].triangles), oid


def _parsed(path):
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(mesh_sidecar.MODEL)
        with archive.open(info) as member:
            tables, structure = mesh_fast._parse_member(member)
        models = [i for i in archive.infolist() if i.filename.lower().endswith('.model')]
        counts = intake_resources._stream_counts(archive, models)
    return info, structure, tables, counts


def _assert_record_matches_parse(path):
    info, structure, tables, counts = _parsed(path)
    recorded = mesh_sidecar.tables(path, info)
    assert recorded is not None, 'no usable sidecar'
    assert recorded[0] == structure
    _same_tables(recorded[1], tables)
    recorded_counts = mesh_sidecar.xml_counts(path, [mesh_sidecar.MODEL])
    assert recorded_counts == {k: counts[k] for k in ('elements', 'vertex', 'triangle')}


def _stl_sources(tmp_path):
    first, second = tmp_path / 'sphere.stl', tmp_path / 'box.stl'
    _binary_stl(first, MESHES['uv-sphere'] + MESHES['tie-grid'])
    _binary_stl(second, MESHES['noisy-box'] + MESHES['needles'])
    return [first, second]


@pytest.fixture(autouse=True)
def _fresh():
    mesh_native._reset_cache()
    mesh_fast._TABLE_CACHE.clear()
    yield
    mesh_native._reset_cache()
    mesh_fast._TABLE_CACHE.clear()


# ------------------------------------------------------------ the record equals a parse

@pytest.mark.parametrize('engine', ['python', 'numpy', 'native'])
@pytest.mark.parametrize('bed', [[220.0, 220.0, 250.0], [30.0, 30.0, 30.0]])
def test_record_equals_a_parse_of_the_combined_file(tmp_path, monkeypatch, engine, bed):
    if engine == 'native' and _native_module() is None:
        pytest.skip('native bounds extension (with the mesh kernel) not built')
    if engine == 'numpy':
        monkeypatch.setattr(mesh_native, '_load', lambda: None)
        monkeypatch.setattr(mesh_numpy, 'MIN_TRIANGLES', 0)
    target = tmp_path / 'parts.3mf'
    with impl('python' if engine == 'python' else 'auto'):
        model_parts.combine(target, [str(s) for s in _stl_sources(tmp_path)], bed)
    assert mesh_sidecar.path_for(target).is_file()
    _assert_record_matches_parse(target)


def test_record_equals_a_parse_on_the_corpus_stl_files(tmp_path):
    stls = []
    for name in CORPUS:
        data = tomllib.loads((CASES / name / 'case.toml').read_text(encoding='utf-8'))
        if make_models.model_suffix(data['model']) == '.stl':
            stls.append(_render(name, tmp_path))
    assert len(stls) >= 6
    for count in (2, 4, len(stls)):
        target = tmp_path / f'parts-{count}.3mf'
        model_parts.combine(target, [str(s) for s in stls[:count]], [220.0, 220.0, 250.0])
        _assert_record_matches_parse(target)


def test_combine_bytes_are_unchanged_by_recording(tmp_path):
    sources = [str(s) for s in _stl_sources(tmp_path)]
    target = tmp_path / 'parts.3mf'
    model_parts.combine(target, sources, [220.0, 220.0, 250.0])
    recorded = target.read_bytes()
    from print_handy.core import geometry_layout
    real_write = geometry_layout.write_geometry_only_3mf
    plain = tmp_path / 'plain.3mf'

    def without_record(objects, path, record_tables=False):
        return real_write(objects, plain)
    import unittest.mock
    with unittest.mock.patch.object(geometry_layout, 'write_geometry_only_3mf', without_record):
        model_parts.combine(tmp_path / 'unused.3mf', sources, [220.0, 220.0, 250.0])
    assert not mesh_sidecar.path_for(plain).exists()
    assert zipfile.ZipFile(plain).read('3D/3dmodel.model') == zipfile.ZipFile(target).read('3D/3dmodel.model')
    assert len(recorded) == plain.stat().st_size


# ------------------------------------------------------------ the readers use it

def test_readers_use_the_record_instead_of_the_xml(tmp_path, monkeypatch):
    target = tmp_path / 'parts.3mf'
    model_parts.combine(target, [str(s) for s in _stl_sources(tmp_path)], [220.0, 220.0, 250.0])
    info, structure, tables, _counts = _parsed(target)

    def no_parse(*args, **kwargs):
        raise AssertionError('parsed the XML although the record matches')
    monkeypatch.setattr(mesh_fast, '_parse_member', no_parse)
    monkeypatch.setattr(intake_resources, '_stream_counts', no_parse)
    intake_resources.check_source(target)
    with zipfile.ZipFile(target) as archive:
        got_structure, got_tables = mesh_fast.archive_tables(archive, mesh_sidecar.MODEL)
    assert got_structure == structure
    _same_tables(got_tables, tables)


def test_the_audit_is_the_same_with_and_without_the_record(tmp_path, monkeypatch):
    sources = [str(s) for s in _stl_sources(tmp_path)]
    with_record = tmp_path / 'a' / 'parts.3mf'
    with_record.parent.mkdir()
    model_parts.combine(with_record, sources, [220.0, 220.0, 250.0])
    without = tmp_path / 'b' / 'parts.3mf'
    without.parent.mkdir()
    shutil.copy(with_record, without)
    assert not mesh_sidecar.path_for(without).exists()

    def no_parse(*args, **kwargs):
        raise AssertionError('parsed the XML although the record matches')
    with monkeypatch.context() as patch:
        patch.setattr(mesh_fast, '_parse_member', no_parse)
        patch.setattr(intake_resources, '_stream_counts', no_parse)
        recorded = mj.audit(with_record)
    mesh_fast._TABLE_CACHE.clear()
    parsed = mj.audit(without)
    assert recorded == parsed


@pytest.mark.parametrize('damage', ['rewritten-3mf', 'garbage', 'truncated', 'wrong-member-crc'])
def test_a_record_that_does_not_match_is_ignored(tmp_path, damage, caplog):
    sources = [str(s) for s in _stl_sources(tmp_path)]
    target = tmp_path / 'parts.3mf'
    model_parts.combine(target, sources, [220.0, 220.0, 250.0])
    sidecar = mesh_sidecar.path_for(target)
    if damage == 'rewritten-3mf':
        # Other bytes at the same path (the record's SHA-256 no longer matches).
        other = tmp_path / 'other.3mf'
        model_parts.combine(other, sources[:1], [220.0, 220.0, 250.0])
        saved = sidecar.read_bytes()
        shutil.copy(other, target)
        sidecar.write_bytes(saved)
    elif damage == 'garbage':
        sidecar.write_bytes(b'not an npz')
    elif damage == 'truncated':
        sidecar.write_bytes(sidecar.read_bytes()[:200])
    else:
        info = zipfile.ZipFile(target).getinfo(mesh_sidecar.MODEL)
        info.CRC ^= 1
        assert mesh_sidecar.tables(target, info) is None
        return
    info, structure, tables, _counts = _parsed(target)
    assert mesh_sidecar.tables(target, info) is None
    assert mesh_sidecar.xml_counts(target, [mesh_sidecar.MODEL]) is None
    with zipfile.ZipFile(target) as archive:
        got_structure, got_tables = mesh_fast.archive_tables(archive, mesh_sidecar.MODEL)
    assert got_structure == structure
    _same_tables(got_tables, tables)
    intake_resources.check_source(target)


def test_limits_still_refuse_with_the_streaming_message(tmp_path, monkeypatch):
    target = tmp_path / 'parts.3mf'
    model_parts.combine(target, [str(s) for s in _stl_sources(tmp_path)], [220.0, 220.0, 250.0])
    counts = mesh_sidecar.xml_counts(target, [mesh_sidecar.MODEL])
    monkeypatch.setattr(intake_resources, 'MAX_TRIANGLES', counts['triangle'] - 1)
    with pytest.raises(intake_resources.IntakeLimitExceeded, match='triangle count exceeds'):
        intake_resources.check_source(target)


def test_names_the_fast_reader_declines_write_no_record(tmp_path):
    sources = _stl_sources(tmp_path)
    odd = tmp_path / 'fish & chips.stl'
    shutil.copy(sources[0], odd)
    target = tmp_path / 'parts.3mf'
    model_parts.combine(target, [str(odd), str(sources[1])], [220.0, 220.0, 250.0])
    assert not mesh_sidecar.path_for(target).exists()
    intake_resources.check_source(target)


def test_a_new_write_removes_the_old_record(tmp_path):
    from print_handy.core import geometry_layout
    target = tmp_path / 'parts.3mf'
    model_parts.combine(target, [str(s) for s in _stl_sources(tmp_path)], [220.0, 220.0, 250.0])
    assert mesh_sidecar.path_for(target).exists()
    geometry_layout.write_geometry_only_3mf([{'name': 'x', 'triangles': MESHES['tetra']}], target)
    assert not mesh_sidecar.path_for(target).exists()


def test_a_same_size_replacement_with_its_mtime_restored_is_not_trusted(tmp_path):
    """The digest is never cached by path, size and mtime: a replaced model of
    the same size, its modification time put back, must not match the old record."""
    import os
    target = tmp_path / 'parts.3mf'
    model_parts.combine(target, [str(s) for s in _stl_sources(tmp_path)], [220.0, 220.0, 250.0])
    assert mesh_sidecar.xml_counts(target, [mesh_sidecar.MODEL]) is not None
    before = target.stat()
    data = bytearray(target.read_bytes())
    data[-1] ^= 0xFF
    target.write_bytes(bytes(data))
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = target.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert mesh_sidecar.xml_counts(target, [mesh_sidecar.MODEL]) is None
    assert mesh_sidecar.tables(target, None) is None


# ------------------------------------------------------------ only this install's own sidecars are trusted

def _forge(target, counts, *, key=None):
    """A sidecar an uploader could place beside their own 3MF: right SHA-256, chosen counts."""
    import hmac as _hmac
    import json as _json
    import numpy as np
    with zipfile.ZipFile(target) as archive:
        info = archive.getinfo(mesh_sidecar.MODEL)
    meta = {'schema': mesh_sidecar.SCHEMA, 'sha256': mesh_sidecar._sha256(target), 'member': mesh_sidecar.MODEL,
            'crc': info.CRC, 'file_size': info.file_size, 'models': [mesh_sidecar.MODEL], 'objects': [],
            'counts': counts, 'arrays': {'structure': mesh_sidecar._array_digest(np.zeros(0, dtype=np.uint8))}}
    meta_bytes = _json.dumps(meta, sort_keys=True).encode()
    arrays = {'meta': np.frombuffer(meta_bytes, dtype=np.uint8), 'structure': np.zeros(0, dtype=np.uint8)}
    if key is not None:
        signature = _hmac.new(key, meta_bytes, 'sha256').hexdigest().encode()
        arrays['signature'] = np.frombuffer(signature, dtype=np.uint8)
    with mesh_sidecar.path_for(target).open('wb') as stream:
        np.savez(stream, **arrays)


@pytest.mark.parametrize('forgery', ['unsigned', 'other-install-key'])
def test_a_sidecar_this_install_did_not_sign_is_ignored(tmp_path, monkeypatch, forgery):
    """duckcatgnaf (8267b60f): a crafted sidecar with zero counts beside an oversized upload must not pass."""
    target = tmp_path / 'parts.3mf'
    model_parts.combine(target, [str(s) for s in _stl_sources(tmp_path)], [220.0, 220.0, 250.0])
    real = mesh_sidecar.xml_counts(target, [mesh_sidecar.MODEL])
    zero = {'elements': 0, 'vertex': 0, 'triangle': 0}
    _forge(target, zero, key=None if forgery == 'unsigned' else b'x' * 32)
    assert mesh_sidecar.xml_counts(target, [mesh_sidecar.MODEL]) is None
    monkeypatch.setattr(intake_resources, 'MAX_TRIANGLES', real['triangle'] - 1)
    with pytest.raises(intake_resources.IntakeLimitExceeded, match='triangle count exceeds'):
        intake_resources.check_source(target)


def test_implausible_counts_are_ignored_even_when_signed(tmp_path):
    target = tmp_path / 'parts.3mf'
    model_parts.combine(target, [str(s) for s in _stl_sources(tmp_path)], [220.0, 220.0, 250.0])
    key = mesh_sidecar._key(create=False)
    assert key is not None
    _forge(target, {'elements': 0, 'vertex': 0, 'triangle': 0}, key=key)
    assert mesh_sidecar.xml_counts(target, [mesh_sidecar.MODEL]) is None
    _forge(target, {'elements': 5, 'vertex': 10, 'triangle': 10}, key=key)
    assert mesh_sidecar.xml_counts(target, [mesh_sidecar.MODEL]) is None


def test_tampered_tables_in_a_signed_sidecar_are_ignored(tmp_path):
    import numpy as np
    target = tmp_path / 'parts.3mf'
    model_parts.combine(target, [str(s) for s in _stl_sources(tmp_path)], [220.0, 220.0, 250.0])
    sidecar = mesh_sidecar.path_for(target)
    with np.load(sidecar, allow_pickle=False) as data:
        arrays = {name: np.array(data[name]) for name in data.files}
    arrays['v0'] = arrays['v0'] + 1.0  # move the geometry, keep the signed metadata
    with sidecar.open('wb') as stream:
        np.savez(stream, **arrays)
    info = zipfile.ZipFile(target).getinfo(mesh_sidecar.MODEL)
    assert mesh_sidecar.tables(target, info) is None
    assert mesh_sidecar.xml_counts(target, [mesh_sidecar.MODEL]) is not None  # counts metadata is untouched
