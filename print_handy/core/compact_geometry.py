"""Compact, native-scalar triangle sequences for legacy geometry consumers.

Double precision preserves the existing transform arithmetic. Iteration creates
only one triangle at a time; no persistent Python point/face object graph.
"""
from array import array
from collections.abc import Sequence

import numpy as np

_CHUNK = 8192


class CompactTriangles(Sequence):
    def __init__(self, triangles=()):
        self.coordinates = array('d')
        self.extend(triangles)

    def extend(self, triangles):
        if isinstance(triangles, CompactTriangles):
            self.coordinates.extend(triangles.coordinates)
        else:
            for triangle in triangles:
                self.coordinates.extend(value for point in triangle for value in point)

    def append(self, triangle):
        self.coordinates.extend(value for point in triangle for value in point)

    def __len__(self):
        return len(self.coordinates) // 9

    def __getitem__(self, index):
        if isinstance(index, slice):
            return CompactTriangles(self[i] for i in range(*index.indices(len(self))))
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        offset = index * 9
        data = self.coordinates
        return [tuple(data[offset + k:offset + k + 3]) for k in (0, 3, 6)]

    def __repr__(self):
        return repr(list(self))

    def __eq__(self, other):
        if isinstance(other, CompactTriangles):
            return self.coordinates == other.coordinates
        if isinstance(other, (list, tuple)):
            return list(self) == list(other)
        return False


class IndexedTriangles(Sequence):
    """Triangle sequence backed by shared indexed storage.

    ``vertices`` is an (N, 3) float64 array and ``faces`` an (M, 3) integer
    array, so storage is O(N + M) instead of the nine doubles per face an
    expanded triangle soup needs (a 3.38M-face mesh: ~80 MB here versus
    ~243 MB expanded and >600 MB as Python lists of tuples). Indexing and
    iteration still materialize one legacy ``[point, point, point]`` triangle
    at a time, so existing consumers keep their exact API and numeric shape;
    bulk consumers detect this class and work on the arrays in bounded
    chunks instead of iterating.
    """

    __slots__ = ('vertices', 'faces')

    def __init__(self, vertices=None, faces=None):
        if vertices is None:
            vertices = np.empty((0, 3), dtype=np.float64)
        if faces is None:
            faces = np.empty((0, 3), dtype=np.int64)
        vertices = np.asarray(vertices, dtype=np.float64)
        faces = np.asarray(faces)
        if faces.dtype not in (np.dtype(np.int32), np.dtype(np.int64)):
            faces = faces.astype(np.int64)
        self.vertices = vertices.reshape((-1, 3))
        self.faces = faces.reshape((-1, 3))

    # -- construction -------------------------------------------------
    @classmethod
    def from_mesh_table(cls, table):
        return cls(table.vertices, table.triangles)

    @classmethod
    def from_triangles(cls, triangles):
        if isinstance(triangles, IndexedTriangles):
            return triangles
        if isinstance(triangles, CompactTriangles):
            coords = np.frombuffer(triangles.coordinates, dtype=np.float64)
            vertices = coords.reshape((-1, 3)).copy()
            return cls(vertices, np.arange(len(vertices), dtype=np.int64).reshape((-1, 3)))
        # MeshTable-shaped objects carry .vertices/.triangles arrays.
        vertices = getattr(triangles, 'vertices', None)
        faces = getattr(triangles, 'triangles', None)
        if isinstance(vertices, np.ndarray) and isinstance(faces, np.ndarray):
            return cls(vertices, faces)
        storage = array('d')
        for triangle in triangles:
            storage.extend(value for point in triangle for value in point)
        vertices = np.frombuffer(storage, dtype=np.float64).reshape((-1, 3)).copy()
        return cls(vertices, np.arange(len(vertices), dtype=np.int64).reshape((-1, 3)))

    def extend_indexed(self, vertices, faces):
        """Append an indexed mesh, offsetting its face indices."""
        vertices = np.asarray(vertices, dtype=np.float64).reshape((-1, 3))
        faces = np.asarray(faces).reshape((-1, 3))
        if not len(faces):
            return
        offset = len(self.vertices)
        if offset:
            faces = faces + offset
        self.vertices = vertices if not len(self.vertices) else np.concatenate((self.vertices, vertices))
        self.faces = faces if not len(self.faces) else np.concatenate((self.faces, faces))

    def extend(self, triangles):
        other = IndexedTriangles.from_triangles(triangles)
        self.extend_indexed(other.vertices, other.faces)

    def append(self, triangle):
        points = np.array([tuple(float(v) for v in point) for point in triangle],
                          dtype=np.float64).reshape((-1, 3))
        self.extend_indexed(points, np.arange(3, dtype=np.int64).reshape((1, 3)))

    # -- sequence API ---------------------------------------------------
    def __len__(self):
        return len(self.faces)

    def __bool__(self):
        return len(self.faces) > 0

    def __getitem__(self, index):
        if isinstance(index, slice):
            return IndexedTriangles(self.vertices, self.faces[index])
        row = self.faces[index]  # negative wraps; out of range raises IndexError
        points = self.vertices[row].tolist()
        return [tuple(points[0]), tuple(points[1]), tuple(points[2])]

    def _chunk_rows(self, start, stop):
        """Legacy triangle rows for faces[start:stop] as plain lists."""
        idx = self.faces[start:stop].ravel()
        points = self.vertices[idx].tolist()
        return [[tuple(points[k]), tuple(points[k + 1]), tuple(points[k + 2])]
                for k in range(0, len(points), 3)]

    def __iter__(self):
        for start in range(0, len(self), _CHUNK):
            yield from self._chunk_rows(start, min(start + _CHUNK, len(self)))

    def iter_triangles(self):
        return iter(self)

    def __repr__(self):
        # Small meshes show the legacy list form exactly: outcome comparisons
        # in the adversarial suite distinguish nan and -0.0 through repr, and
        # a summary would silently stop comparing coordinates. Large meshes
        # summarise rather than materialising millions of rows.
        if len(self) <= 256:
            return repr(list(self))
        return f'IndexedTriangles({len(self)} triangles, {len(self.vertices)} vertices)'

    def __eq__(self, other):
        if isinstance(other, IndexedTriangles):
            if (np.array_equal(self.vertices, other.vertices)
                    and self.faces.shape == other.faces.shape
                    and np.array_equal(self.faces, other.faces)):
                return True
            if len(self) != len(other):
                return False
            return all(a == b for a, b in zip(self, other))
        if isinstance(other, CompactTriangles):
            if len(self) != len(other):
                return False
            coords = other.coordinates
            for i, triangle in enumerate(self):
                base = i * 9
                flat = [value for point in triangle for value in point]
                if flat != list(coords[base:base + 9]):
                    return False
            return True
        if isinstance(other, (list, tuple)):
            if len(self) != len(other):
                return False
            for mine, theirs in zip(self, other):
                if mine != [tuple(float(v) for v in point) for point in theirs]:
                    return False
            return True
        return NotImplemented


class TransformedTriangles(Sequence):
    """Reiterable scalar transform view; orientations share the source storage.

    ``ops`` optionally describes ``transform`` for the native mesh kernel
    (#1790) as a list of ``(kind, params)`` steps applied in order; use
    ``rotation_z`` / ``translation`` to build the view and its recipe from
    one place, so the two can never disagree. Without ``ops`` the view is
    Python only.
    """
    def __init__(self, source, transform, ops=None):
        self.source = source
        self.transform = transform
        self.ops = ops

    @classmethod
    def rotation_z(cls, source, c, s):
        """``(c*x - s*y, s*x + c*y, z)`` per point."""
        return cls(source, lambda x, y, z: (c * x - s * y, s * x + c * y, z), [('rotz', (c, s))])

    @classmethod
    def translation(cls, source, dx, dy, dz):
        """``(x + dx, y + dy, z + dz)`` per point."""
        return cls(source, lambda x, y, z: (x + dx, y + dy, z + dz), [('translate', (dx, dy, dz))])

    def __len__(self):
        return len(self.source)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return CompactTriangles(self[i] for i in range(*index.indices(len(self))))
        return [self.transform(*point) for point in self.source[index]]


def transformed_rows(vertices, chain, transform):
    """Apply transforms with historical scalar operation order, compactly."""
    from .mesh_fast import _LegacyRows
    import numpy as np
    def points():
        for point in vertices:
            for matrix in chain:
                point = transform(matrix, point)
            yield from point
    storage = array('d', points())
    return _LegacyRows(np.frombuffer(storage, dtype=np.float64).reshape((-1, 3)))


def transform_vertices_array(vertices, chain, apply):
    """Vectorized twin of applying ``apply(matrix, point)`` per vertex in order.

    Each output coordinate is built from the same left-to-right multiply/add
    sequence the scalar ``apply`` uses (geometry_layout.apply form:
    ``t[0]*x + t[3]*y + t[6]*z + t[9]`` per row), evaluated as separate
    elementwise ufunc steps, so every element rounds exactly as the scalar
    code does. ``apply`` is accepted for signature parity with
    transformed_rows and documents which scalar contract is mirrored.
    """
    result = np.asarray(vertices, dtype=np.float64).reshape((-1, 3))
    for t in chain:
        x, y, z = result[:, 0], result[:, 1], result[:, 2]
        result = np.stack((
            t[0] * x + t[3] * y + t[6] * z + t[9],
            t[1] * x + t[4] * y + t[7] * z + t[10],
            t[2] * x + t[5] * y + t[8] * z + t[11],
        ), axis=1)
    return result
