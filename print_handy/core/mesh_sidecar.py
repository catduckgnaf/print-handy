"""Load once per intake: the combine step's mesh tables, reused by the checks and the audit (#1790).

A multi-file intake combines its parts into one geometry-only 3MF in a
worker process, and later steps in other processes read that 3MF back: the
source check streams its XML to count elements, and the audit and color
inventory parse every ``<vertex>`` and ``<triangle>`` into mesh tables. On
the 2026-11-21 tongs pair that is about 100 MB of XML read three times,
some 35 s on the reporting host, for tables the combine step held in memory
a moment earlier.

The combine step now writes those tables beside the 3MF (``<name>.3mf`` +
``SUFFIX``): per object the welded float64 vertices and int32 triangles,
exactly as ``mesh_fast._parse_member`` reads them from the text (each
vertex prints with ``{:.6f}`` from a ``round(v, 6)`` value, which ``float()``
reads back to the same double), the model's structure XML, and the source
check's element counts. ``tables`` and ``xml_counts`` hand them out only
when this install signed the sidecar (an HMAC under the state folder's
``KEY_NAME`` key over metadata that includes every array's SHA-256, so a
sidecar arriving beside an uploaded 3MF is never trusted), it matches the 3MF
byte for byte (SHA-256 of the whole file, recorded at write time) and the
model member's name, CRC and size, and its counts are plausible; anything
else, including a missing, stale or unreadable sidecar, returns None and
the caller parses the XML as before. The sidecar never changes a result:
``tests/pipeline/test_mesh_sidecar.py`` pins its tables, structure and
counts equal to a real parse of the same file.
"""
import hashlib
import json
import logging
import os
import tempfile
import xml.parsers.expat
import zipfile
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

SUFFIX = '.mesh-tables.npz'
SCHEMA = 2
MODEL = '3D/3dmodel.model'
# A sidecar is trusted only when this install wrote it: its metadata (the file's
# SHA-256, the counts, and a SHA-256 of every array) carries an HMAC under a key
# kept in the state folder. Matching the 3MF's hash alone is not enough: whoever
# supplies a 3MF can supply a matching sidecar beside it (an upload, an archive),
# and the source check would then take that sidecar's counts instead of the XML.
KEY_NAME = 'mesh-sidecar.key'
# Stand-ins for the mesh bodies when deriving the structure and counts: the
# fast reader keeps no node for anything inside <vertices> or <triangles>.
PLACEHOLDER_VERTEX = '          <vertex x="0" y="0" z="0"/>'
PLACEHOLDER_TRIANGLE = '          <triangle v1="0" v2="0" v3="0"/>'

_HASHES = {}


def path_for(model_path):
    model_path = Path(model_path)
    return model_path.with_name(model_path.name + SUFFIX)


def discard(model_path):
    """Remove a sidecar left from an earlier write of ``model_path``."""
    try:
        path_for(model_path).unlink(missing_ok=True)
    except OSError as exc:
        logger.debug('could not remove the mesh sidecar of %s: %s', Path(model_path).name, exc)


def _sha256(path):
    """SHA-256 of a file, remembered per (path, size, mtime) for this process."""
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    hit = _HASHES.get(key)
    if hit is None:
        with path.open('rb') as source:
            hit = hashlib.file_digest(source, 'sha256').hexdigest()
        _HASHES.clear()
        _HASHES[key] = hit
    return hit


def _key_path():
    from .. import settings
    return Path(settings.state_dir()) / KEY_NAME


def _key(create):
    """This install's sidecar signing key (32 random bytes), or None when it cannot be read or made."""
    import secrets
    path = _key_path()
    try:
        key = path.read_bytes()
        if len(key) == 32:
            return key
        logger.warning('mesh sidecar key %s has the wrong length; sidecars are not used', path)
        return None
    except FileNotFoundError:
        if not create:
            return None
    except OSError as exc:
        logger.debug('mesh sidecar key unreadable (%s); sidecars are not used', exc)
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(secrets.token_bytes(32))
    except FileExistsError:
        logger.debug('mesh sidecar key %s was created by another process first', path)
    except OSError as exc:
        logger.warning('could not create the mesh sidecar key %s (%s); readers will parse', path, exc)
        return None
    return _key(create=False)


def _signature(key, meta_bytes):
    import hmac
    return hmac.new(key, meta_bytes, hashlib.sha256).hexdigest()


def _array_digest(array):
    return hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest()


def _count_skeleton(skeleton):
    """The source check's counts for the skeleton model (one placeholder per mesh body)."""
    counts = {'elements': 0, 'vertex': 0, 'triangle': 0}

    def start(name, attrs):
        counts['elements'] += 1
        local = name.rsplit('}', 1)[-1]
        if local in ('vertex', 'triangle'):
            counts[local] += 1
    parser = xml.parsers.expat.ParserCreate(namespace_separator='}')
    parser.StartElementHandler = start
    parser.Parse(skeleton, True)
    return counts


def write(model_path, skeleton_lines, objects):
    """Record the mesh tables of a just-written geometry-only 3MF.

    ``skeleton_lines`` are the model's lines with each mesh body reduced to
    one ``PLACEHOLDER_VERTEX`` and one ``PLACEHOLDER_TRIANGLE``; ``objects``
    is ``[(object_id, vertices (N, 3) float64, triangles (M, 3))]`` in
    document order. Nothing is written when the fast reader would not take
    this model (it then falls back to ElementTree, and so do the readers).
    A failure to write is logged and leaves no sidecar.
    """
    from .mesh_fast import _parse_member
    model_path = Path(model_path)
    discard(model_path)
    try:
        skeleton = '\n'.join(skeleton_lines).encode('utf-8')
        parsed = _parse_member(skeleton)
        if parsed is None:
            return None
        skeleton_tables, structure = parsed
        if list(skeleton_tables) != [str(oid) for oid, _v, _t in objects]:
            return None
        counts = _count_skeleton(skeleton)
        arrays = {}
        for index, (_oid, vertices, triangles) in enumerate(objects):
            vertices = np.ascontiguousarray(vertices, dtype=np.float64).reshape((-1, 3))
            triangles = np.ascontiguousarray(triangles, dtype=np.int32).reshape((-1, 3))
            if not len(vertices) or not len(triangles):
                return None
            counts['vertex'] += len(vertices) - 1
            counts['triangle'] += len(triangles) - 1
            counts['elements'] += len(vertices) + len(triangles) - 2
            arrays[f'v{index}'] = vertices
            arrays[f't{index}'] = triangles
        with zipfile.ZipFile(model_path) as archive:
            info = archive.getinfo(MODEL)
            members = [i.filename for i in archive.infolist() if i.filename.lower().endswith('.model')]
        key = _key(create=True)
        if key is None:
            return None
        arrays['structure'] = np.frombuffer(structure, dtype=np.uint8)
        meta = {'schema': SCHEMA, 'sha256': _sha256(model_path), 'member': MODEL,
                'crc': info.CRC, 'file_size': info.file_size, 'models': members,
                'objects': [str(oid) for oid, _v, _t in objects], 'counts': counts,
                'arrays': {name: _array_digest(value) for name, value in sorted(arrays.items())}}
        meta_bytes = json.dumps(meta, sort_keys=True).encode()
        arrays['meta'] = np.frombuffer(meta_bytes, dtype=np.uint8)
        arrays['signature'] = np.frombuffer(_signature(key, meta_bytes).encode(), dtype=np.uint8)
        target = path_for(model_path)
        fd, temp = tempfile.mkstemp(prefix='.mesh-tables-', suffix='.partial', dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                np.savez(stream, **arrays)
            os.replace(temp, target)
        finally:
            Path(temp).unlink(missing_ok=True)
        return target
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, xml.parsers.expat.ExpatError) as exc:
        logger.warning('could not record the mesh tables of %s (%s); readers will parse it', model_path.name, exc)
        discard(model_path)
        return None


def _load(model_path, want_arrays):
    """The sidecar's ``(meta, npz)`` when it matches ``model_path`` byte for byte, else None."""
    model_path = Path(model_path)
    sidecar = path_for(model_path)
    if not sidecar.is_file():
        return None
    import hmac
    key = _key(create=False)
    if key is None:
        return None
    try:
        data = np.load(sidecar, allow_pickle=False)
        meta_bytes = bytes(data['meta'])
        signature = bytes(data['signature']).decode()
        if not hmac.compare_digest(signature, _signature(key, meta_bytes)):
            logger.warning('mesh sidecar of %s is not signed by this install; parsing it', model_path.name)
            data.close()
            return None
        meta = json.loads(meta_bytes.decode())
        if meta.get('schema') != SCHEMA or meta.get('sha256') != _sha256(model_path):
            logger.debug('mesh sidecar of %s does not match the file; parsing it', model_path.name)
            return None
        if not want_arrays:
            data.close()
            data = None
        return meta, data
    except (OSError, ValueError, KeyError, TypeError, EOFError, zipfile.BadZipFile) as exc:
        logger.debug('mesh sidecar of %s unreadable (%s); parsing it', model_path.name, exc)
        return None


def tables(model_path, info):
    """``(structure_xml, {object_id: MeshTable})`` for one archive member, or None.

    ``info`` is the member's ZipInfo; name, CRC and size must match the sidecar.
    """
    from .mesh_fast import MeshTable
    loaded = _load(model_path, want_arrays=True)
    if loaded is None:
        return None
    meta, data = loaded
    try:
        if (meta.get('member') != info.filename or meta.get('crc') != info.CRC
                or meta.get('file_size') != info.file_size):
            return None
        digests = meta.get('arrays') or {}
        if _array_digest(data['structure']) != digests.get('structure'):
            return None
        found = {}
        for index, oid in enumerate(meta['objects']):
            raw_v, raw_t = data[f'v{index}'], data[f't{index}']
            if _array_digest(raw_v) != digests.get(f'v{index}') or _array_digest(raw_t) != digests.get(f't{index}'):
                return None
            vertices = np.array(raw_v, dtype=np.float64).reshape((-1, 3))
            triangles = np.array(raw_t, dtype=np.int32).reshape((-1, 3))
            if len(triangles) and (int(triangles.min()) < 0 or int(triangles.max()) >= len(vertices)):
                return None
            found[str(oid)] = MeshTable(vertices, triangles)
        return bytes(data['structure']), found
    except (KeyError, ValueError, TypeError, OSError, EOFError, zipfile.BadZipFile) as exc:
        logger.debug('mesh sidecar of %s incomplete (%s); parsing it', Path(model_path).name, exc)
        return None
    finally:
        data.close()


def xml_counts(model_path, model_members):
    """The source check's ``{'elements', 'vertex', 'triangle'}`` totals, or None.

    ``model_members`` are the archive's ``.model`` member names; they must be
    exactly the ones the sidecar was written for.
    """
    loaded = _load(model_path, want_arrays=False)
    if loaded is None:
        return None
    meta, _ = loaded
    counts = meta.get('counts')
    if meta.get('models') != list(model_members) or not isinstance(counts, dict):
        return None
    try:
        found = {key: int(counts[key]) for key in ('elements', 'vertex', 'triangle')}
    except (KeyError, TypeError, ValueError):
        return None
    # A combined model always has geometry, and every vertex and triangle is an element.
    if min(found.values()) <= 0 or found['elements'] < found['vertex'] + found['triangle']:
        logger.warning('mesh sidecar of %s records implausible counts; parsing it', Path(model_path).name)
        return None
    return found
