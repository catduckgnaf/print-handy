"""A dropped .zip of model sources, opened safely for the one project builder (#1204 item 3).

A designer's download is often a ZIP: the printable STL/3MF parts beside a
STEP, a render, a README, maybe a build script and a reference envelope
that is explicitly not for printing. This module only enumerates and
extracts; it never slices, combines or decides anything about the parts.
The printable files it returns go into the same ``upload_job_set`` and
part chooser as attachments and model-site file lists, so a ZIP is an
entry point to the existing project builder, not a second one.

Safety rules, all enforced before a single byte is written:

* the archive is refused outright when any entry is encrypted, a symlink,
  an absolute path, a drive path or a path that climbs out with ``..``
  (ZIP-slip), and when it is not a readable ZIP at all;
* bounds on the number of entries, each printable file's uncompressed
  size, the total uncompressed size and every entry's compression ratio
  (a ZIP bomb) are checked from the directory, and the extraction itself
  counts the bytes it actually writes, so a lying header cannot get past;
* files that are not printable here (STEP and other CAD sources, images,
  documents, scripts and programs, sliced G-code, nested archives,
  reference envelopes) are listed as ignored with the reason and never
  extracted, and an archive with no printable file is refused by name.

A 3MF inside the ZIP is extracted byte for byte, so its authored colors,
plates and per-object names reach the existing 3MF inventory unchanged.
A .3mf upload is not a ZIP for this module: it keeps its own path.
"""
from __future__ import annotations

import hashlib
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath

from .intake_resources import IntakeLimitExceeded, MAX_DOWNLOAD_BYTES, MIB

ARCHIVE_EXTS = {'.zip'}
PRINTABLE_EXTS = {'.stl', '.3mf'}

MAX_ENTRIES = 1000
MAX_PRINTABLE_FILES = 64
MAX_FILE_BYTES = MAX_DOWNLOAD_BYTES  # one printable file; check_source still applies its own limits after
MAX_TOTAL_BYTES = 512 * MIB          # every entry, uncompressed, printable or not
MAX_RATIO = 100                      # uncompressed / compressed, per entry
RATIO_FLOOR_BYTES = 1 * MIB          # tiny entries compress absurdly well and are harmless
CHUNK = 64 * 1024

_CAD = {'.step', '.stp', '.iges', '.igs', '.f3d', '.f3z', '.scad', '.fcstd', '.sldprt', '.sldasm',
        '.ipt', '.iam', '.blend', '.skp', '.dwg', '.dxf', '.x_t', '.sat', '.3dm', '.obj', '.ply',
        '.amf', '.wrl', '.dae', '.fbx', '.gltf', '.glb'}
_IMAGE = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tif', '.tiff', '.svg', '.heic'}
_DOC = {'.md', '.txt', '.pdf', '.rtf', '.doc', '.docx', '.html', '.htm', '.csv', '.json', '.xml',
        '.yaml', '.yml', '.ini', '.cfg', '.license', '.odt'}
_SCRIPT = {'.py', '.sh', '.bash', '.zsh', '.bat', '.cmd', '.ps1', '.js', '.vbs', '.exe', '.dll',
           '.so', '.dylib', '.app', '.jar', '.msi', '.com', '.scr', '.pl', '.rb', '.lua'}
_SLICED = {'.gcode', '.bgcode', '.gco', '.g'}
_NESTED = {'.zip', '.7z', '.rar', '.tar', '.gz', '.tgz', '.bz2', '.xz'}
_DOC_NAMES = re.compile(r'^(?:readme|license|licence|copying|notice|changelog|authors)(?:\.|$)', re.I)
# A reference envelope says it is not a part: REFERENCE_MAC_MINI_ENVELOPE_NOT_FOR_PRINTING.step,
# "do not print", "not printable". A part merely named "reference block" is not one.
_ENVELOPE = re.compile(r'(?:^reference[_\-. ]|not[_\-. ]?for[_\-. ]?print|do[_\-. ]?not[_\-. ]?print'
                       r'|not[_\-. ]?printable|dont[_\-. ]?print)', re.I)


class ArchiveRefused(IntakeLimitExceeded):
    """A ZIP the intake will not open, with the reason a person can act on."""

    def __init__(self, detail):
        super().__init__(f'Model intake refused: {detail} Nothing was sliced or started.')


def is_archive(path) -> bool:
    return Path(str(path)).suffix.lower() in ARCHIVE_EXTS


def _safe_member_path(name: str) -> PurePosixPath:
    """The entry's path inside the archive, or ArchiveRefused for an unsafe one."""
    if '\x00' in name:
        raise ArchiveRefused(f'the ZIP has an entry with a NUL byte in its name ({name!r}).')
    raw = name.replace('\\', '/')
    if raw.startswith('/') or re.match(r'^[A-Za-z]:', raw):
        raise ArchiveRefused(f'the ZIP has an entry with an absolute path ({name}).')
    parts = [p for p in raw.split('/') if p not in ('', '.')]
    if any(p == '..' for p in parts):
        raise ArchiveRefused(f'the ZIP has an entry that points outside the archive ({name}).')
    return PurePosixPath(*parts) if parts else PurePosixPath('.')


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def _ignore_reason(member: PurePosixPath) -> str | None:
    """Why an entry is not a printable part here, or None for a printable STL/3MF."""
    name = member.name
    lower = name.lower()
    if member.parts and (member.parts[0] == '__MACOSX' or name.startswith('._') or lower in {'.ds_store', 'thumbs.db'}):
        return 'operating-system metadata'
    suffix = PurePosixPath(lower).suffix
    if lower.endswith('.gcode.3mf') or suffix in _SLICED:
        return 'already sliced G-code, never printed from an archive'
    if suffix in PRINTABLE_EXTS:
        if _ENVELOPE.search(PurePosixPath(name).stem):
            return 'reference envelope, marked not for printing'
        return None
    if _ENVELOPE.search(PurePosixPath(name).stem):
        return 'reference envelope, marked not for printing'
    if suffix in _CAD:
        return 'CAD source, not sliced here (export it as STL or 3MF)'
    if suffix in _IMAGE:
        return 'image'
    if suffix in _SCRIPT:
        return 'script or program, never run'
    if suffix in _DOC or _DOC_NAMES.match(name):
        return 'document'
    if suffix in _NESTED:
        return 'nested archive, not opened'
    return 'unsupported file type'


def inspect(path) -> dict:
    """Enumerate a ZIP without extracting it: the printable entries and the ignored ones.

    Raises ArchiveRefused for an unsafe or empty-of-parts archive. The result
    lists ``printable`` rows (``archive_path``, ``name``, ``size``) in archive
    order and ``ignored`` rows (``archive_path``, ``reason``).
    """
    path = Path(path)
    label = path.name
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise ArchiveRefused(f'{label} is not a readable ZIP archive ({type(exc).__name__}).') from None
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ArchiveRefused(f'{label} has {len(infos):,} entries; the limit is {MAX_ENTRIES:,}.')
        printable, ignored, total = [], [], 0
        for info in infos:
            member = _safe_member_path(info.filename)
            if info.flag_bits & 0x1:
                raise ArchiveRefused(f'{label} is encrypted ({info.filename}); send an unencrypted ZIP.')
            if _is_symlink(info):
                raise ArchiveRefused(f'{label} contains a symbolic link ({info.filename}); links are never followed.')
            if info.is_dir():
                continue
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise ArchiveRefused(f'{label} expands past {MAX_TOTAL_BYTES // MIB} MiB.')
            if info.file_size > RATIO_FLOOR_BYTES and (
                    info.compress_size <= 0 or info.file_size / info.compress_size > MAX_RATIO):
                raise ArchiveRefused(f'{label} has an entry compressed more than {MAX_RATIO}:1 '
                                     f'({info.filename}), which is how a ZIP bomb looks.')
            reason = _ignore_reason(member)
            if reason is not None:
                ignored.append({'archive_path': str(member), 'reason': reason})
                continue
            if info.file_size > MAX_FILE_BYTES:
                raise ArchiveRefused(f'{info.filename} in {label} is larger than {MAX_FILE_BYTES // MIB} MiB.')
            printable.append({'archive_path': str(member), 'name': member.name, 'size': info.file_size})
        if not printable:
            found = ', '.join(f"{row['archive_path']} ({row['reason']})" for row in ignored[:8]) or 'nothing'
            more = f' and {len(ignored) - 8} more' if len(ignored) > 8 else ''
            raise ArchiveRefused(f'{label} has no STL or 3MF part to print. It contains: {found}{more}.')
        if len(printable) > MAX_PRINTABLE_FILES:
            raise ArchiveRefused(f'{label} has {len(printable)} printable files; the limit is '
                                 f'{MAX_PRINTABLE_FILES}. Send the parts you want in a smaller ZIP.')
    return {'archive': label, 'printable': printable, 'ignored': ignored}


def extract(path, dest) -> tuple[list[Path], dict]:
    """Inspect, then extract only the printable entries, each into its own numbered folder.

    Two entries may share a file name in different folders; each keeps its
    name under ``dest/<n>/``, so nothing overwrites anything. Bytes are
    copied unchanged and counted as they are written. Returns the extracted
    paths in archive order and the report (``inspect``'s, plus ``sha256``
    for the archive and every extracted part).
    """
    path, dest = Path(path), Path(dest)
    report = inspect(path)
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    by_name = {row['archive_path']: row for row in report['printable']}
    out = []
    with zipfile.ZipFile(path) as archive:
        n = 0
        for info in archive.infolist():
            if info.is_dir():
                continue
            member = str(_safe_member_path(info.filename))
            row = by_name.get(member)
            if row is None or 'sha256' in row:
                continue
            n += 1
            target = (root / str(n) / PurePosixPath(member).name).resolve()
            if root not in target.parents:
                raise ArchiveRefused(f'{info.filename} would extract outside its folder.')
            target.parent.mkdir(parents=True, exist_ok=True)
            digest, written = hashlib.sha256(), 0
            try:
                with archive.open(info) as source, target.open('wb') as sink:
                    while chunk := source.read(CHUNK):
                        written += len(chunk)
                        if written > info.file_size or written > MAX_FILE_BYTES:
                            raise ArchiveRefused(f'{info.filename} in {path.name} is larger than its '
                                                 'header says; the archive is damaged or hostile.')
                        digest.update(chunk)
                        sink.write(chunk)
            except BaseException:
                target.unlink(missing_ok=True)
                raise
            row['sha256'] = digest.hexdigest()
            out.append(target)
    report['sha256'] = _sha256(path)
    return out, report


def _sha256(path) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def summary(report: dict) -> str:
    """One plain sentence about what the ZIP held, for the chat."""
    parts = report.get('printable') or []
    ignored = report.get('ignored') or []
    text = (f"{report.get('archive') or 'The ZIP'} has {len(parts)} printable "
            f"file{'s' if len(parts) != 1 else ''}")
    if ignored:
        shown = ', '.join(PurePosixPath(row['archive_path']).name for row in ignored[:6])
        more = f' and {len(ignored) - 6} more' if len(ignored) > 6 else ''
        text += f"; I left out {len(ignored)} that {'are' if len(ignored) != 1 else 'is'} not printable parts ({shown}{more})"
    return text + '.'
