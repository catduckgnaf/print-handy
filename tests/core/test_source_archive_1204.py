"""#1204 item 3: a dropped .zip opens safely and yields only its printable parts.

Fixtures are built in memory with zipfile: a designer-style source ZIP
(STL, STEP, PNG, README, a script and a reference envelope), and hostile
archives (traversal, absolute path, symlink, bomb, encrypted, too many
entries, oversized). Every hostile one is refused before anything is
written; the source ZIP extracts only the STL/3MF parts, byte for byte.
"""
import hashlib
import io
import stat
import zipfile
from pathlib import Path

import pytest

from print_handy.core import prepared_intake, source_archive
from print_handy.core.intake_resources import IntakeLimitExceeded
from print_handy.core.source_archive import ArchiveRefused


def _zip(path, entries, compression=zipfile.ZIP_DEFLATED):
    """entries: name -> bytes, or name -> (bytes, ZipInfo tweak callable)."""
    with zipfile.ZipFile(path, 'w', compression) as z:
        for name, data in entries.items():
            tweak = None
            if isinstance(data, tuple):
                data, tweak = data
            info = zipfile.ZipInfo(name)
            info.compress_type = compression
            if tweak:
                tweak(info)
            z.writestr(info, data)
    return Path(path)


def _stl(seed):
    return b'\0' * 80 + (1).to_bytes(4, 'little') + bytes([seed]) * 50


SOURCE_ZIP = {
    'MacMini/arm_left.stl': _stl(1),
    'MacMini/arm_right.stl': _stl(2),
    'MacMini/REFERENCE_MAC_MINI_ENVELOPE_NOT_FOR_PRINTING.step': b'ISO-10303-21;',
    'MacMini/REFERENCE_MAC_MINI_ENVELOPE_NOT_FOR_PRINTING.stl': _stl(9),
    'MacMini/tray.step': b'ISO-10303-21;',
    'MacMini/render.png': b'\x89PNG\r\n',
    'README.md': b'# Mac mini rack',
    'LICENSE': b'CC-BY',
    'tools/build.py': b'print("hi")',
    'tools/build.sh': b'echo hi',
    'old/plate_1.gcode': b'G28',
    '__MACOSX/MacMini/._arm_left.stl': b'junk',
    'MacMini/': b'',
}


def test_source_zip_yields_only_printable_parts(tmp_path):
    archive = _zip(tmp_path / 'macmini.zip', SOURCE_ZIP)
    report = source_archive.inspect(archive)
    assert [r['archive_path'] for r in report['printable']] == ['MacMini/arm_left.stl', 'MacMini/arm_right.stl']
    reasons = {r['archive_path']: r['reason'] for r in report['ignored']}
    assert reasons['MacMini/REFERENCE_MAC_MINI_ENVELOPE_NOT_FOR_PRINTING.step'].startswith('reference envelope')
    assert reasons['MacMini/REFERENCE_MAC_MINI_ENVELOPE_NOT_FOR_PRINTING.stl'].startswith('reference envelope')
    assert reasons['MacMini/tray.step'].startswith('CAD source')
    assert reasons['MacMini/render.png'] == 'image'
    assert reasons['README.md'] == reasons['LICENSE'] == 'document'
    assert reasons['tools/build.py'] == reasons['tools/build.sh'] == 'script or program, never run'
    assert reasons['old/plate_1.gcode'].startswith('already sliced')
    assert reasons['__MACOSX/MacMini/._arm_left.stl'] == 'operating-system metadata'

    paths, report = source_archive.extract(archive, tmp_path / 'out')
    assert [p.name for p in paths] == ['arm_left.stl', 'arm_right.stl']
    assert paths[0].read_bytes() == _stl(1) and paths[1].read_bytes() == _stl(2)
    # Nothing that is not a printable part was written anywhere.
    written = sorted(p.name for p in (tmp_path / 'out').rglob('*') if p.is_file())
    assert written == ['arm_left.stl', 'arm_right.stl']
    assert report['printable'][0]['sha256'] == hashlib.sha256(_stl(1)).hexdigest()
    assert report['sha256'] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert 'left out' in source_archive.summary(report)


def test_equal_names_in_different_folders_are_both_kept(tmp_path):
    archive = _zip(tmp_path / 'two.zip', {'a/arm.stl': _stl(1), 'b/arm.stl': _stl(2)})
    paths, report = source_archive.extract(archive, tmp_path / 'out')
    assert [p.name for p in paths] == ['arm.stl', 'arm.stl'] and paths[0] != paths[1]
    assert {p.read_bytes() for p in paths} == {_stl(1), _stl(2)}
    assert [r['archive_path'] for r in report['printable']] == ['a/arm.stl', 'b/arm.stl']


def test_a_3mf_inside_is_extracted_byte_for_byte(tmp_path):
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, 'w') as z:
        z.writestr('3D/3dmodel.model', '<model><metadata name="Title">Painted</metadata></model>')
        z.writestr('Metadata/model_settings.config', '<config><plate/></config>')
        z.writestr('Metadata/project_settings.config', '{"filament_colour": ["#FF0000", "#00FF00"]}')
    data = inner.getvalue()
    archive = _zip(tmp_path / 'project.zip', {'painted.3mf': data, 'painted.png': b'png'})
    paths, _ = source_archive.extract(archive, tmp_path / 'out')
    assert len(paths) == 1 and paths[0].read_bytes() == data  # authored colors and plates untouched


@pytest.mark.parametrize('name', ['../evil.stl', 'parts/../../evil.stl', '/etc/evil.stl',
                                  'C:/evil.stl', '..\\evil.stl'])
def test_traversal_and_absolute_paths_are_refused(tmp_path, name):
    archive = _zip(tmp_path / 'slip.zip', {'ok.stl': _stl(1), name: _stl(2)})
    with pytest.raises(ArchiveRefused, match='outside the archive|absolute path'):
        source_archive.extract(archive, tmp_path / 'out')
    assert not (tmp_path / 'out').exists() and not (tmp_path / 'evil.stl').exists()


def test_symlink_is_refused(tmp_path):
    def link(info):
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
    archive = _zip(tmp_path / 'link.zip', {'ok.stl': _stl(1), 'part.stl': (b'/etc/passwd', link)})
    with pytest.raises(ArchiveRefused, match='symbolic link'):
        source_archive.extract(archive, tmp_path / 'out')
    assert not (tmp_path / 'out').exists()


def test_zip_bomb_is_refused(tmp_path):
    archive = _zip(tmp_path / 'bomb.zip', {'bomb.stl': b'\0' * (8 * 1024 * 1024)})
    with pytest.raises(ArchiveRefused, match='ZIP bomb'):
        source_archive.extract(archive, tmp_path / 'out')
    assert not (tmp_path / 'out').exists()


def test_encrypted_archive_is_refused(tmp_path):
    archive = _zip(tmp_path / 'locked.zip', {'part.stl': _stl(1)}, zipfile.ZIP_STORED)
    # zipfile cannot write encrypted entries: set the "encrypted" flag bit the
    # way an encrypting tool does, in the local header and the central directory.
    data = bytearray(archive.read_bytes())
    for signature, offset in ((b'PK\x03\x04', 6), (b'PK\x01\x02', 8)):
        at = data.find(signature)
        data[at + offset] |= 0x1
    archive.write_bytes(bytes(data))
    with pytest.raises(ArchiveRefused, match='encrypted'):
        source_archive.inspect(archive)


def test_bounds_on_entries_and_sizes(tmp_path, monkeypatch):
    many = _zip(tmp_path / 'many.zip', {f'n{i}.txt': b'x' for i in range(6)})
    monkeypatch.setattr(source_archive, 'MAX_ENTRIES', 5)
    with pytest.raises(ArchiveRefused, match='entries'):
        source_archive.inspect(many)
    monkeypatch.setattr(source_archive, 'MAX_ENTRIES', 1000)
    big = _zip(tmp_path / 'big.zip', {'big.stl': _stl(1) * 4})
    monkeypatch.setattr(source_archive, 'MAX_FILE_BYTES', 100)
    with pytest.raises(ArchiveRefused, match='larger than'):
        source_archive.inspect(big)
    monkeypatch.setattr(source_archive, 'MAX_FILE_BYTES', 10 ** 9)
    monkeypatch.setattr(source_archive, 'MAX_TOTAL_BYTES', 300)
    with pytest.raises(ArchiveRefused, match='expands past'):
        source_archive.inspect(big)
    monkeypatch.setattr(source_archive, 'MAX_TOTAL_BYTES', 10 ** 9)
    monkeypatch.setattr(source_archive, 'MAX_PRINTABLE_FILES', 1)
    with pytest.raises(ArchiveRefused, match='printable files'):
        source_archive.inspect(_zip(tmp_path / 'parts.zip', {'a.stl': _stl(1), 'b.stl': _stl(2)}))


def test_unsupported_content_is_refused_by_name(tmp_path):
    archive = _zip(tmp_path / 'cad.zip', {'tray.step': b'ISO', 'readme.txt': b'hi', 'run.bat': b'x'})
    with pytest.raises(ArchiveRefused, match='no STL or 3MF part') as caught:
        source_archive.inspect(archive)
    assert 'tray.step (CAD source' in str(caught.value) and 'run.bat (script' in str(caught.value)
    # The refusal is the intake's curated limit refusal, so chat shows its text.
    assert isinstance(caught.value, IntakeLimitExceeded)
    bad = tmp_path / 'notazip.zip'
    bad.write_bytes(b'not a zip')
    with pytest.raises(ArchiveRefused, match='not a readable ZIP'):
        source_archive.inspect(bad)


def test_archive_job_is_the_upload_set_inventory(tmp_path):
    archive = _zip(tmp_path / ('doc_' + 'a' * 12 + '_MacMini.zip'), SOURCE_ZIP)
    job = prepared_intake.archive_job([archive], tmp_path / 'stage.archive')
    assert job['site'] == 'upload' and job['source_kind'] == 'upload_archive'
    assert [r['name'] for r in job['sources']] == ['arm_left.stl', 'arm_right.stl']
    assert [r['archive_path'] for r in job['sources']] == ['MacMini/arm_left.stl', 'MacMini/arm_right.stl']
    assert job['title'] == 'MacMini (2 files)'
    assert job['source_archive']['archive'] == 'MacMini.zip'
    with pytest.raises(ArchiveRefused, match='one ZIP per message'):
        prepared_intake.archive_job([archive, archive], tmp_path / 'stage2.archive')


def test_archive_files_selects_zip_only(tmp_path):
    from types import SimpleNamespace as S
    z = _zip(tmp_path / 'a.zip', {'a.stl': _stl(1)})
    three = tmp_path / 'a.3mf'
    three.write_bytes(b'PK')
    event = S(media_urls=[str(z), str(three), str(tmp_path / 'missing.zip')])
    assert prepared_intake.archive_files(event) == [z]
    assert prepared_intake.model_files(event) == [three]  # a 3MF keeps its own path


@pytest.mark.parametrize('second', ['a/arm.stl', 'a//arm.stl', 'a\arm.stl', './a/arm.stl'])
def test_two_entries_with_one_path_are_refused(tmp_path, second):
    """Review of #1220: extraction keyed parts by path, so a repeated path listed
    two parts in the report but extracted one. The ZIP is refused instead."""
    path = tmp_path / 'dup.zip'
    import warnings
    with warnings.catch_warnings(), zipfile.ZipFile(path, 'w') as z:
        warnings.simplefilter('ignore')  # zipfile warns on an exact duplicate name
        z.writestr('a/arm.stl', _stl(1))
        z.writestr(second, _stl(2))
    with pytest.raises(ArchiveRefused, match='same path'):
        source_archive.inspect(path)
    with pytest.raises(ArchiveRefused, match='same path'):
        source_archive.extract(path, tmp_path / 'out')
    assert not (tmp_path / 'out').exists()
