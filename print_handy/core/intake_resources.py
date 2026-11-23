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
MAX_EXPANDED_BYTES = 512 * MIB  # total uncompressed size; guards disk use / zip bombs
MAX_XML_BYTES = 400 * MIB  # expanded model XML is streamed, not retained as a DOM
MAX_MEMBER_BYTES = 400 * MIB  # single member uncompressed; guards expansion
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


def _refuse(detail):
    raise IntakeLimitExceeded(
        f'Model intake refused: {detail}. Simplify the mesh or split it into smaller '
        'files in your modeling app, then upload again. Nothing was sliced or started.')


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
    """Check sizes first, then bounded streaming XML counts without building a tree."""
    path = Path(path)
    size = path.stat().st_size
    # No overall file-size refusal. Geometry budgets and zip-bomb guards apply below.
    if path.suffix.lower() == '.stl':
        # Triangle count is the geometry guard for binary and ASCII STL alike.
        # Byte size alone never refuses. (#1824, #1833)
        with path.open('rb') as source:
            header = source.read(84)
            if _stl_triangle_count(source, size, header) > MAX_TRIANGLES:
                _refuse(f'triangle count exceeds {MAX_TRIANGLES:,} limit')
        return
    if path.suffix.lower() != '.3mf':
        return
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > MAX_MEMBERS:
            _refuse(f'archive exceeds {MAX_MEMBERS:,} members (zip-bomb / resource guard)')
        models = [i for i in members if i.filename.lower().endswith('.model')]
        if sum(i.file_size for i in models) > MAX_XML_BYTES:
            _refuse(f'model XML exceeds {MAX_XML_BYTES // MIB} MiB limit')
        if sum(i.file_size for i in members) > MAX_EXPANDED_BYTES:
            _refuse(f'expanded archive exceeds {MAX_EXPANDED_BYTES // MIB} MiB (disk-space / zip-bomb guard)')
        if any(i.file_size > MAX_MEMBER_BYTES for i in members):
            _refuse(f'archive member exceeds {MAX_MEMBER_BYTES // MIB} MiB uncompressed (expansion guard)')
        recorded = _recorded_counts(path, [i.filename for i in models])
        if recorded is not None:
            return
        _stream_counts(archive, models)


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


def _stream_counts(archive, models):
    """Stream every model member's XML, refusing at the first limit crossed; returns the totals."""
    counts = {'triangle': 0, 'vertex': 0, 'elements': 0, 'estimated_bytes': 0}

    def start(name, attrs):
        local = name.rsplit('}', 1)[-1]
        counts['elements'] += 1
        if counts['elements'] > MAX_XML_ELEMENTS:
            _refuse(f'XML element count exceeds {MAX_XML_ELEMENTS:,} limit')
        if local in ('triangle', 'vertex'):
            counts[local] += 1
            maximum = MAX_TRIANGLES if local == 'triangle' else MAX_VERTICES
            if counts[local] > maximum:
                _refuse(f'{local} count exceeds {maximum:,} limit')
            # Vertex/triangle arrays, streaming parser's temporary tuple lists,
            # and one transformed copy per active instance are conservatively
            # budgeted at 160 bytes per primitive. This intentionally errs high.
            counts['estimated_bytes'] += 160
            if counts['estimated_bytes'] > MAX_ESTIMATED_GEOMETRY_BYTES:
                _refuse(f'estimated geometry allocation exceeds {MAX_ESTIMATED_GEOMETRY_BYTES // MIB} MiB safety ceiling')

    def doctype(*args):
        _refuse('XML document type declarations are not supported')

    for member in models:
        parser = xml.parsers.expat.ParserCreate(namespace_separator='}')
        parser.StartElementHandler = start
        parser.StartDoctypeDeclHandler = doctype
        with archive.open(member) as source:
            total = 0
            while chunk := source.read(CHUNK):
                total += len(chunk)
                if total > MAX_XML_BYTES:
                    _refuse(f'model XML exceeds {MAX_XML_BYTES // MIB} MiB limit')
                _parse_chunk(parser, chunk, False)
            _parse_chunk(parser, b'', True)
    return counts
