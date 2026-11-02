"""Memory-bounded streaming 3MF mesh reader.

The Expat callbacks discard vertex/triangle XML elements as they arrive and
append coordinates and indices directly to compact typed buffers. Only the
small non-mesh model structure is retained for the existing 3MF resolver.
Unsupported XML is returned to ElementTree unchanged rather than partially
interpreted.
"""
from array import array
from dataclasses import dataclass
import xml.etree.ElementTree as ET
from xml.parsers import expat

import numpy as np

_CORE_URI = 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'
_CORE = '{' + _CORE_URI + '}'
_MAX_VERTICES = 10_000_000
_MAX_TRIANGLES = 10_000_000
_MAX_ELEMENTS = 40_000_000
_MAX_SOURCE_BUFFER_BYTES = 1536 * 1024 * 1024


class _UnsupportedFastPath(Exception):
    """The XML is legal but outside the compact reader's exact grammar."""


@dataclass(frozen=True)
class MeshTable:
    """Array-backed mesh: float64 vertices and int32 triangle indices."""
    vertices: np.ndarray
    triangles: np.ndarray

    def __eq__(self, other):
        if isinstance(other, MeshTable):
            return (np.array_equal(self.vertices, other.vertices, equal_nan=True)
                    and np.array_equal(self.triangles, other.triangles))
        if isinstance(other, tuple) and len(other) == 2:
            vertices, triangles = other
            return (list(map(tuple, self.vertices.tolist())) == vertices
                    and list(map(tuple, self.triangles.tolist())) == triangles)
        return NotImplemented

    def __len__(self):
        return len(self.triangles)

    def __bool__(self):
        return bool(len(self.triangles))

    def __iter__(self):
        # Preserve the historical unpacking shape: vertices, faces = table.
        yield _LegacyRows(self.vertices)
        yield _LegacyRows(self.triangles)

    def __getitem__(self, index):
        return _LegacyRows((self.vertices, self.triangles)[index])

    def iter_triangles(self):
        for face in self.triangles:
            yield (tuple(float(v) for v in self.vertices[int(face[0])]),
                   tuple(float(v) for v in self.vertices[int(face[1])]),
                   tuple(float(v) for v in self.vertices[int(face[2])]))

    def iter_used_vertices(self):
        """Yield referenced vertices once, in source index order."""
        if not len(self.triangles):
            return
        used = set()
        for face in self.triangles:
            used.update((int(face[0]), int(face[1]), int(face[2])))
        for index in sorted(used):
            yield tuple(float(v) for v in self.vertices[index])

    def to_legacy(self):
        """Compatibility conversion; avoid in intake and large-geometry paths."""
        return [[tuple(point) for point in tri] for tri in self.iter_triangles()]


class _LegacyRows:
    """Lazy tuple rows for indexed legacy callers, without copying the mesh."""
    def __init__(self, rows):
        self.rows = rows

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, index):
        row = self.rows[index]
        return _LegacyRows(row) if isinstance(index, slice) else tuple(row.tolist())

    def __iter__(self):
        return (tuple(row.tolist()) for row in self.rows)


def validate_geometry_budget(vertices, faces, instances=1,
                             ceiling=_MAX_SOURCE_BUFFER_BYTES):
    """Conservative compact + topology estimate for all emitted instances."""
    estimate = (vertices * 24 + faces * 120) * max(1, instances)
    if estimate > ceiling:
        raise ValueError(f'estimated geometry allocation {estimate} exceeds {ceiling} byte ceiling')
    return estimate


def _et_name(name):
    if '}' in name:
        uri, local = name.rsplit('}', 1)
        return '{' + uri + '}' + local
    return name


def _parse_member(data, want_triangles=True):
    """Parse a byte string or readable stream; never retain mesh XML nodes."""
    tables = {}
    root = None
    # (Clark tag, Element-or-None). Mesh descendants deliberately have no ET node.
    stack = []
    object_id = None
    object_mesh_count = 0
    mesh_active = False
    vertex_buf = face_buf = None
    vertex_total = face_total = element_total = 0
    saw_mesh = False
    unsupported_prefix = False
    namespace_pending = []
    object_ids = set()
    parser = expat.ParserCreate(namespace_separator='}')
    parser.ordered_attributes = False
    vertex_tag = _CORE_URI + '}vertex'
    triangle_tag = _CORE_URI + '}triangle'

    def namespace_decl(prefix, uri):
        if prefix and uri == _CORE_URI:
            # Preserve the strict-reader fallback for prefixed core tags.
            raise _UnsupportedFastPath
        namespace_pending.append((prefix, uri))

    def start(raw_name, raw_attrs):
        nonlocal root, object_id, object_mesh_count, mesh_active
        nonlocal vertex_buf, face_buf, vertex_total, face_total, element_total, saw_mesh
        element_total += 1
        if element_total > _MAX_ELEMENTS:
            raise ValueError('3MF XML element count exceeds hard safety ceiling')
        # Primitive callbacks dominate large meshes. Avoid namespace/attribute
        # dictionaries and structural Element work for the canonical hot path.
        if mesh_active and not namespace_pending:
            parent = stack[-1][0]
            if raw_name == vertex_tag and parent == _CORE + 'vertices':
                x, y, z = raw_attrs['x'], raw_attrs['y'], raw_attrs['z']
                if not (x.isascii() and y.isascii() and z.isascii()):
                    raise _UnsupportedFastPath
                try:
                    vertex_buf.extend((float(x), float(y), float(z)))
                except ValueError:
                    # A coordinate outside the float grammar: decline the fast
                    # path — the fallback reader converts only referenced meshes,
                    # so an unused resource cannot block printable geometry.
                    raise _UnsupportedFastPath from None
                vertex_total += 1
                if vertex_total > _MAX_VERTICES or (vertex_total + face_total) * 32 > _MAX_SOURCE_BUFFER_BYTES:
                    raise ValueError('3MF source geometry exceeds hard safety ceiling')
                stack.append((_CORE + 'vertex', None))
                return
            if want_triangles and raw_name == triangle_tag and parent == _CORE + 'triangles':
                a, b, c = raw_attrs['v1'], raw_attrs['v2'], raw_attrs['v3']
                if not (a.isascii() and b.isascii() and c.isascii()
                        and a.isdigit() and b.isdigit() and c.isdigit()):
                    raise _UnsupportedFastPath
                a, b, c = int(a), int(b), int(c)
                if max(a, b, c) > 2_147_483_647:
                    raise _UnsupportedFastPath
                face_buf.extend((a, b, c))
                face_total += 1
                if face_total > _MAX_TRIANGLES or (vertex_total + face_total) * 32 > _MAX_SOURCE_BUFFER_BYTES:
                    raise ValueError('3MF source geometry exceeds hard safety ceiling')
                stack.append((_CORE + 'triangle', None))
                return
        full_name = _et_name(raw_name)
        attrs = {_et_name(key): value for key, value in raw_attrs.items()}
        parent = stack[-1][0] if stack else None
        if full_name.rsplit('}', 1)[-1] == 'object' and full_name != _CORE + 'object':
            raise _UnsupportedFastPath
        in_mesh = mesh_active

        if namespace_pending:
            # Namespace declaration attributes on primitive tags make their
            # identity ambiguous to the old strict reader; use the XML fallback.
            local = full_name.rsplit('}', 1)[-1]
            if local in ('vertex', 'triangle'):
                raise _UnsupportedFastPath
            namespace_pending.clear()
        if not stack and full_name != _CORE + 'model':
            raise _UnsupportedFastPath

        if in_mesh:
            if full_name == _CORE + 'object':
                raise _UnsupportedFastPath
            if full_name == _CORE + 'vertex' and parent == _CORE + 'vertices':
                if any(key not in raw_attrs for key in ('x', 'y', 'z')):
                    raise KeyError('mesh vertex has missing coordinate')
                # The legacy optimized grammar did not expand XML entities in
                # coordinate attributes. Let ElementTree retain that contract.
                values = (raw_attrs['x'], raw_attrs['y'], raw_attrs['z'])
                if any('&' in value for value in values):
                    raise _UnsupportedFastPath
                if not all(value.isascii() for value in values):
                    raise _UnsupportedFastPath
                try:
                    vertex_buf.extend(float(value) for value in values)
                except ValueError:
                    raise _UnsupportedFastPath from None
                vertex_total += 1
                if vertex_total > _MAX_VERTICES:
                    raise ValueError('3MF vertex count exceeds hard safety ceiling')
                if (vertex_total + face_total) * 32 > _MAX_SOURCE_BUFFER_BYTES:
                    raise ValueError('estimated source geometry allocation exceeds safety ceiling')
                elem = None
            elif full_name == _CORE + 'triangle' and parent == _CORE + 'triangles':
                if want_triangles:
                    values = tuple(raw_attrs.get(f'v{i}') for i in (1, 2, 3))
                    if any(value is None for value in values):
                        raise KeyError('mesh triangle has missing vertex index')
                    # Only canonical ASCII unsigned indices can enter int32.
                    if any(not value.isascii() or not value.isdigit() for value in values):
                        raise _UnsupportedFastPath
                    indices = tuple(int(value) for value in values)
                    if any(index > 2_147_483_647 for index in indices):
                        raise _UnsupportedFastPath
                    face_buf.extend(indices)
                    face_total += 1
                    if face_total > _MAX_TRIANGLES:
                        raise ValueError('3MF triangle count exceeds hard safety ceiling')
                    if (vertex_total + face_total) * 32 > _MAX_SOURCE_BUFFER_BYTES:
                        raise ValueError('estimated source geometry allocation exceeds safety ceiling')
                    elem = None
            elif full_name in (_CORE + 'vertices', _CORE + 'triangles') and parent == _CORE + 'mesh':
                elem = None
            else:
                raise _UnsupportedFastPath
        else:
            elem = ET.Element(full_name, attrs)
            if stack:
                parent_elem = stack[-1][1]
                if parent_elem is not None:
                    parent_elem.append(elem)
            else:
                root = elem

            if full_name == _CORE + 'object':
                if parent != _CORE + 'resources' or not raw_attrs.get('id'):
                    raise _UnsupportedFastPath
                object_id = raw_attrs['id']
                if object_id in object_ids:
                    raise _UnsupportedFastPath
                object_ids.add(object_id)
                object_mesh_count = 0
            elif full_name == _CORE + 'mesh':
                if parent != _CORE + 'object' or object_id is None:
                    raise _UnsupportedFastPath
                object_mesh_count += 1
                if object_mesh_count != 1:
                    raise _UnsupportedFastPath
                saw_mesh = True
                mesh_active = True
                # Vertices stay float64 until after component/build transforms:
                # float32 collapses large-offset coordinates (100000000 vs
                # 100000001) that a compensating translation must separate.
                vertex_buf, face_buf = array('d'), array('i')
        stack.append((full_name, elem))

    def end(raw_name):
        nonlocal object_id, mesh_active, vertex_buf, face_buf
        # Primitive closing tags need no namespace conversion or mesh finalization.
        # Expat already guarantees matching start/end names, including nesting.
        if raw_name == vertex_tag or raw_name == triangle_tag:
            stack.pop()
            return
        full_name = _et_name(raw_name)
        if full_name == _CORE + 'mesh' and mesh_active:
            if object_id is None:
                raise _UnsupportedFastPath
            if not len(vertex_buf) or (want_triangles and not len(face_buf)):
                raise _UnsupportedFastPath
            vertices = np.frombuffer(vertex_buf, dtype=np.float64).reshape((-1, 3))
            triangles = np.frombuffer(face_buf, dtype=np.int32).reshape((-1, 3))
            if len(triangles) and (int(triangles.min()) < 0 or int(triangles.max()) >= len(vertices)):
                raise _UnsupportedFastPath
            tables[object_id] = MeshTable(vertices, triangles)
            mesh_active = False
            vertex_buf = face_buf = None
        if full_name == _CORE + 'object':
            object_id = None
        if stack:
            stack.pop()

    def characters(text):
        if not stack:
            return
        if mesh_active:
            if text.strip():
                raise _UnsupportedFastPath
            return
        elem = stack[-1][1]
        if elem is None:
            return
        if len(elem):
            child = elem[-1]
            child.tail = (child.tail or '') + text
        else:
            elem.text = (elem.text or '') + text

    def reject_markup(*args):
        raise _UnsupportedFastPath

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = characters
    parser.StartNamespaceDeclHandler = namespace_decl
    parser.StartDoctypeDeclHandler = reject_markup
    parser.CommentHandler = reject_markup
    parser.ProcessingInstructionHandler = reject_markup
    parser.StartCdataSectionHandler = reject_markup
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        if hasattr(data, 'read'):
            while chunk := data.read(64 * 1024):
                if b'&' in chunk or b'\x00' in chunk or chunk.startswith((b'\xff\xfe', b'\xfe\xff')):
                    return None
                parser.Parse(chunk, False)
            parser.Parse(b'', True)
        else:
            if b'&' in data or b'\x00' in data or data.startswith((b'\xff\xfe', b'\xfe\xff')):
                return None
            view = memoryview(data)
            for offset in range(0, len(view), 64 * 1024):
                parser.Parse(view[offset:offset + 64 * 1024], False)
            parser.Parse(b'', True)
    except _UnsupportedFastPath:
        return None
    except expat.ExpatError:
        return None
    if not saw_mesh or root is None:
        return None
    return tables, ET.tostring(root, encoding='utf-8', xml_declaration=True)


def mesh_tables(data, want_triangles=True):
    """Return ``{object_id: MeshTable}``, or ``None`` for ElementTree fallback."""
    parsed = _parse_member(data, want_triangles)
    return None if parsed is None else parsed[0]


def strip_mesh_bodies(data):
    """Return structure-only XML for supported meshes; otherwise preserve input."""
    parsed = _parse_member(data)
    return data if parsed is None else parsed[1]


def _bounds_and_volume_arrays(vertices, faces):
    """Vectorized bounds/volume over indexed storage, gathered in bounded chunks.

    Per-face terms use the same left-to-right operation order as the scalar
    loop; chunk sums are accumulated in face order. Bounds are exact (pure
    selection). Used only for large indexed meshes, where the scalar loop's
    per-triangle Python tuples are the memory/time problem; small meshes keep
    the bit-identical scalar path below.
    """
    import math
    lo = [math.inf, math.inf, math.inf]
    hi = [-math.inf, -math.inf, -math.inf]
    volume = 0.0
    for start in range(0, len(faces), 8192):
        pts = np.asarray(vertices[faces[start:start + 8192].ravel()],
                         dtype=np.float64).reshape((-1, 3, 3))
        chunk_lo = pts.min(axis=(0, 1))
        chunk_hi = pts.max(axis=(0, 1))
        for axis in range(3):
            lo[axis] = min(lo[axis], float(chunk_lo[axis]))
            hi[axis] = max(hi[axis], float(chunk_hi[axis]))
        a, b, c = pts[:, 0], pts[:, 1], pts[:, 2]
        ax, ay, az = a[:, 0], a[:, 1], a[:, 2]
        bx, by, bz = b[:, 0], b[:, 1], b[:, 2]
        cx, cy, cz = c[:, 0], c[:, 1], c[:, 2]
        terms = (ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx)
                 + az * (bx * cy - by * cx)) / 6.0
        volume += float(np.sum(terms))
    return lo, hi, volume


def bounds_and_volume(triangles):
    """(lo, hi, signed volume), without materializing a per-triangle point list."""
    from .compact_geometry import IndexedTriangles
    if isinstance(triangles, IndexedTriangles) and len(triangles) >= 32768:
        if not len(triangles):
            raise ValueError('empty triangle table')
        return _bounds_and_volume_arrays(triangles.vertices, triangles.faces)
    if isinstance(triangles, MeshTable) and len(triangles) >= 32768:
        return _bounds_and_volume_arrays(triangles.vertices, triangles.triangles)
    source = triangles.iter_triangles() if isinstance(triangles, MeshTable) else iter(triangles)
    lo = hi = None
    volume = 0.0
    xs, ys, zs = [], [], []

    def flush():
        nonlocal lo, hi
        low = [min(xs), min(ys), min(zs)]
        high = [max(xs), max(ys), max(zs)]
        if lo is None:
            lo, hi = low, high
        else:
            for axis in range(3):
                if low[axis] < lo[axis]:
                    lo[axis] = low[axis]
                if high[axis] > hi[axis]:
                    hi[axis] = high[axis]
        xs.clear()
        ys.clear()
        zs.clear()

    for (ax, ay, az), (bx, by, bz), (cx, cy, cz) in source:
        xs += (ax, bx, cx)
        ys += (ay, by, cy)
        zs += (az, bz, cz)
        volume += (ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx) + az * (bx * cy - by * cx)) / 6.0
        if len(xs) >= 24576:
            flush()
    if xs:
        flush()
    if lo is None:
        raise ValueError('empty triangle table')
    return lo, hi, volume


_TABLE_CACHE = {}


def archive_tables(archive, name):
    """Return structure XML and compact tables, cached for one archive version."""
    info = archive.getinfo(name)
    source = str(getattr(archive, 'filename', '') or id(archive))
    key = (name, info.CRC, info.file_size)
    if _TABLE_CACHE.get('source') != source:
        _TABLE_CACHE.clear()
        _TABLE_CACHE['source'] = source
    hit = _TABLE_CACHE.get(key)
    if hit is not None:
        return hit
    with archive.open(info) as member:
        parsed = _parse_member(member)
    if parsed is None:
        result = (archive.read(info), None)
    else:
        tables, structure = parsed
        result = (structure, tables)
    _TABLE_CACHE[key] = result
    return result
