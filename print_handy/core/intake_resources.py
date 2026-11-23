"""Conservative offline intake budgets, checked before DOM/mesh allocation.

These are refusal limits, not a promise that every accepted mesh fits in a
particular RSS budget (component instancing and downstream slicers vary).
"""
from pathlib import Path
import shutil
import struct
import xml.parsers.expat
import zipfile

MIB = 1024 * 1024
# No byte-size download or per-STL cap. Geometry budgets (triangles, vertices,
# estimated working storage) and zip-bomb / disk-space guards remain.
# MAX_DOWNLOAD_BYTES and MAX_STL_BYTES removed: a model the slicer can handle
# is never refused solely for its size in bytes. (#1824)
MAX_MEMBERS = 4096
# No fixed expanded, member or model-XML byte ceilings for a 3MF (#1846).
# Geometry is counted by streaming Expat; members that expand suspiciously
# far beyond their compressed sizes still fail the ZIP-bomb check.
MAX_ZIP_RATIO = 100
ZIP_RATIO_FLOOR_BYTES = MIB
# Dense float32/int32 tables plus downstream topology/instance working storage.
MAX_ESTIMATED_GEOMETRY_BYTES = 1536 * MIB
MAX_TRIANGLES = 10_000_000
MAX_VERTICES = 10_000_000
MAX_XML_ELEMENTS = 40_000_000
CHUNK = 64 * 1024
# Not a model-size cap (#1824). A download of any size is kept when the
# volume still has this much free afterwards. Crossing it refuses the
# download and deletes the partial file, so one response cannot fill the disk.
MIN_FREE_BYTES = 512 * MIB


class IntakeLimitExceeded(ValueError):
    """Safe, user-facing refusal; no slicing or printer operation is permitted."""


def _refuse(detail, *, path=None):
    # Links and uploads use the same refusal: name the source and what was
    # measured; a linked model need not exist in the user's modeling app.
    label = f'{Path(path).name}: ' if path is not None else ''
    raise IntakeLimitExceeded(
        f'Model intake refused: {label}{detail}. Nothing was sliced or started.')


def _refuse_disk(detail):
    # The operator's volume is short of space; the model is not at fault, so the
    # mesh-editing advice in _refuse does not apply. (#1833)
    raise IntakeLimitExceeded(
        f'Model intake refused: the server is low on disk space ({detail}). '
        'Nothing was kept, sliced or started.')


_ASCII_FACET = b'endfacet'


def _stl_triangle_count(source, size, header):
    """Triangle count used by the STL geometry guard, read in bounded memory.

    A binary STL whose length matches its header count is trusted. Otherwise an
    ASCII STL is counted by its ``endfacet`` tokens, stopping as soon as the
    count passes MAX_TRIANGLES. A file with neither is sized as a binary STL
    would be, so a large malformed file cannot bypass the guard. (#1833)
    """
    if len(header) == 84:
        declared = struct.unpack('<I', header[80:84])[0]
        if 84 + 50 * declared == size:
            return declared
    count = header.count(_ASCII_FACET)
    tail = header[-(len(_ASCII_FACET) - 1):]
    while count <= MAX_TRIANGLES and (chunk := source.read(CHUNK * 16)):
        window = tail + chunk
        count += window.count(_ASCII_FACET)
        tail = window[-(len(_ASCII_FACET) - 1):]
    if count:
        return count
    return max(0, size - 84) // 50


def save_download(response, target):
    """Stream the response to disk with no byte-size cap; remove partial downloads.

    Geometry and archive-expansion guards are applied later by check_source.
    A model the slicer can handle is never refused solely for its size in bytes. (#1824)
    The stream still stops when the volume would drop below MIN_FREE_BYTES.
    """
    target = Path(target)
    parent = target.parent if str(target.parent) else Path(".")
    try:
        with target.open('wb') as output:
            while chunk := response.read(CHUNK):
                free = shutil.disk_usage(parent).free
                if free < len(chunk) + MIN_FREE_BYTES:
                    _refuse_disk(
                        f'{MIN_FREE_BYTES // MIB} MiB of free disk space must remain and '
                        'this download would use it')
                output.write(chunk)
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def _parse_chunk(parser, data, final):
    try:
        parser.Parse(data, final)
    except xml.parsers.expat.ExpatError as exc:
        # Preserve the existing inventory API's malformed-XML exception contract.
        from xml.etree.ElementTree import ParseError
        raise ParseError(str(exc)) from exc


def check_source(path):
    """Guard ZIP ratios, then stream geometry counts without building an XML DOM."""
    path = Path(path)
    size = path.stat().st_size
    # No overall file-size refusal. Geometry budgets and zip-bomb guards apply below.
    if path.suffix.lower() == '.stl':
        # Triangle count is the geometry guard for binary and ASCII STL alike.
        # Byte size alone never refuses. (#1824, #1833)
        with path.open('rb') as source:
            header = source.read(84)
            triangles = _stl_triangle_count(source, size, header)
            if triangles > MAX_TRIANGLES:
                _refuse(f'triangle count at least {triangles:,} exceeds {MAX_TRIANGLES:,} limit',
                        path=path)
        return
    if path.suffix.lower() != '.3mf':
        return
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > MAX_MEMBERS:
            _refuse(f'archive exceeds {MAX_MEMBERS:,} members (zip-bomb / resource guard)',
                    path=path)
        for member in members:
            # No inflated-size ceiling: geometry has its own count and
            # allocation budgets. Check every member's ratio, including
            # non-model payloads, before trusting a signed mesh sidecar.
            if member.file_size > ZIP_RATIO_FLOOR_BYTES and (
                    member.compress_size <= 0
                    or member.file_size > MAX_ZIP_RATIO * member.compress_size):
                _refuse(f'archive member {member.filename} exceeds {MAX_ZIP_RATIO}:1 '
                        'compression ratio (zip-bomb guard)', path=path)
        models = [i for i in members if i.filename.lower().endswith('.model')]
        recorded = _recorded_counts(path, [i.filename for i in models])
        if recorded is not None:
            return
        _stream_counts(archive, models, path=path)


def _within_limits(counts):
    primitives = counts['vertex'] + counts['triangle']
    return (counts['elements'] <= MAX_XML_ELEMENTS and counts['vertex'] <= MAX_VERTICES
            and counts['triangle'] <= MAX_TRIANGLES
            and 160 * primitives <= MAX_ESTIMATED_GEOMETRY_BYTES)


def _recorded_counts(path, model_members):
    """The combine step's recorded XML counts for this exact file, when all within limits (#1790).

    Only a pass is taken from the record: anything over a limit, or no
    usable record, streams the XML so the refusal is the one it gives.
    """
    from .mesh_sidecar import xml_counts
    counts = xml_counts(path, model_members)
    if counts is None or not _within_limits(counts):
        return None
    return counts


def _stream_counts(archive, models, *, path=None):
    """Stream every model member's XML, refusing at the first limit crossed; returns the totals."""
    counts = {'triangle': 0, 'vertex': 0, 'elements': 0, 'estimated_bytes': 0}

    def start(name, attrs):
        local = name.rsplit('}', 1)[-1]
        counts['elements'] += 1
        if counts['elements'] > MAX_XML_ELEMENTS:
            _refuse(f'XML element count at least {counts["elements"]:,} exceeds '
                    f'{MAX_XML_ELEMENTS:,} limit', path=path)
        if local in ('triangle', 'vertex'):
            counts[local] += 1
            maximum = MAX_TRIANGLES if local == 'triangle' else MAX_VERTICES
            if counts[local] > maximum:
                _refuse(f'{local} count at least {counts[local]:,} exceeds {maximum:,} limit',
                        path=path)
            # Vertex/triangle arrays, streaming parser's temporary tuple lists,
            # and one transformed copy per active instance are conservatively
            # budgeted at 160 bytes per primitive. This intentionally errs high.
            counts['estimated_bytes'] += 160
            if counts['estimated_bytes'] > MAX_ESTIMATED_GEOMETRY_BYTES:
                _refuse(f'estimated geometry allocation exceeds '
                        f'{MAX_ESTIMATED_GEOMETRY_BYTES // MIB} MiB safety ceiling '
                        f'after {counts["triangle"]:,} triangles and '
                        f'{counts["vertex"]:,} vertices', path=path)

    def doctype(*args):
        _refuse('XML document type declarations are not supported', path=path)

    for member in models:
        parser = xml.parsers.expat.ParserCreate(namespace_separator='}')
        parser.StartElementHandler = start
        parser.StartDoctypeDeclHandler = doctype
        with archive.open(member) as source:
            while chunk := source.read(CHUNK):
                _parse_chunk(parser, chunk, False)
            _parse_chunk(parser, b'', True)
    return counts
