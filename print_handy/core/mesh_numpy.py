"""Vectorized numpy twin of the native mesh kernel, for installs without it (#1790).

``mesh_native`` serves mesh topology, bounds with area and volume, welding
and the geometry-only 3MF mesh text from the Rust kernel when the bounds
extension carries it. Without the extension those calls returned None and
the callers ran their per-triangle Python loops: about 35 s to combine the
1.1 M-triangle tongs pair of the 2026-11-21 report. This module does the
same work in whole-array numpy steps, under the kernel's exact contract
(``native/bounds/src/mesh.rs``), so its results equal the Python oracle bit
for bit, signed zeros included:

* transforms run per point in the Python lambdas' operation order, one
  elementwise ufunc per operation (numpy never fuses a multiply and an add);
* bounds take the minimum and maximum, and a zero bound takes the sign of
  the first zero in scan order, as the scalar loop's strict comparisons do;
* area and signed volume add one triangle at a time from 0.0, through
  ``np.add.accumulate`` (a sequential sum, unlike the pairwise ``np.sum``);
* ``round(v, 6)`` is ``rint(v * 1e6) / 1e6`` wherever ``v * 1e6`` is
  provably not within its rounding error of a decimal tie, and Python's own
  ``round()`` for the few values that are (and for magnitudes from 4e9 up);
* welding keys on the rounded values with ``-0.0`` equal to ``0.0`` and
  numbers vertices in first-corner order, keeping the first corner's values.

Every function takes the same typed storage the kernel takes (through
``mesh_native.mesh_args``) and returns None for anything else, for a
non-finite coordinate, under ``PRINT_HANDY_BOUNDS_IMPL=python`` (the Python
oracle stays reachable) and for meshes under ``MIN_TRIANGLES``; the caller
then runs its Python loop. An unexpected error is logged and also returns
None: the Python code decides that call.
"""
import logging

import numpy as np

logger = logging.getLogger(__name__)

#: Below this many triangles the Python loops are already fast; tests lower it.
MIN_TRIANGLES = 2048
_CHUNK = 65536
_NEVER_SWALLOW = (KeyboardInterrupt, SystemExit, GeneratorExit)
_VERTEX = '          <vertex x="%s%d.%06d" y="%s%d.%06d" z="%s%d.%06d"/>'
_TRIANGLE = '          <triangle v1="%d" v2="%d" v3="%d"/>'


class _Decline(Exception):
    """Input outside the exact contract (a non-finite coordinate): use Python."""


def _mesh(triangles, allow_lists=False):
    """``_Mesh`` for typed storage this module can take exactly, or None."""
    from .mesh_native import _setting, mesh_args
    if _setting() == 'python':
        return None
    if not hasattr(triangles, '__len__') or len(triangles) < MIN_TRIANGLES:
        return None
    args = mesh_args(triangles, allow_lists=allow_lists)
    if args is None:
        return None
    return _Mesh(*args)


def _guarded(name, work, triangles, allow_lists=False):
    mesh = _mesh(triangles, allow_lists)
    if mesh is None:
        return None
    try:
        return work(mesh)
    except _Decline:
        return None
    except BaseException as exc:
        if isinstance(exc, _NEVER_SWALLOW):
            raise
        logger.warning("numpy mesh fallback failed in %s (%r); using Python", name, exc)
        return None


def _apply(points, ops):
    """``points`` (N, 3) after the transform chain, in the Python lambdas' order.

    An overflow gives inf as in Python; the caller then declines the mesh.
    """
    with np.errstate(over='ignore', invalid='ignore'):
        return _apply_ops(points, ops)


def _apply_ops(points, ops):
    for kind, params in ops:
        x, y, z = points[:, 0], points[:, 1], points[:, 2]
        p = [float(v) for v in params]
        if kind == 'rotz':
            c, s = p
            points = np.stack((c * x - s * y, s * x + c * y, z), axis=1)
        elif kind == 'translate':
            points = np.stack((x + p[0], y + p[1], z + p[2]), axis=1)
        elif kind == 'affine' and len(p) == 12:
            points = np.stack((p[0] * x + p[3] * y + p[6] * z + p[9],
                               p[1] * x + p[4] * y + p[7] * z + p[10],
                               p[2] * x + p[5] * y + p[8] * z + p[11]), axis=1)
        else:
            raise ValueError(f'unknown transform {kind!r}')
    return points


class _Mesh:
    """Transformed triangle corners, read in bounded chunks."""

    def __init__(self, vertices, faces, ops):
        values = np.frombuffer(vertices, dtype=np.float64) if not isinstance(vertices, np.ndarray) \
            else np.asarray(vertices, dtype=np.float64)
        if faces is None:
            if values.size % 9:
                raise ValueError('triangle soup length is not a multiple of nine')
            self._soup = values.reshape((-1, 3))
            self._table = None
            self._faces = None
            self.count = values.size // 9
        else:
            faces = np.asarray(faces, dtype=np.int64).reshape((-1, 3))
            table = values.reshape((-1, 3))
            if len(faces) and (int(faces.min()) < 0 or int(faces.max()) >= len(table)):
                raise ValueError('triangle index outside the vertex table')
            # Transforming each vertex once gives the same values as
            # transforming every corner: the arithmetic is per point.
            self._table = _apply(table, ops) if ops else table
            self._faces = faces
            self._soup = None
            self.count = len(faces)
        self._ops = ops

    def chunks(self, size=_CHUNK):
        """``(start, corners (k, 3, 3))`` per chunk; _Decline on a non-finite value."""
        for start in range(0, self.count, size):
            stop = min(self.count, start + size)
            if self._faces is None:
                points = self._soup[start * 3:stop * 3]
                if self._ops:
                    points = _apply(points, self._ops)
                points = points.reshape((-1, 3, 3))
            else:
                points = self._table[self._faces[start:stop]]
            if not np.isfinite(points).all():
                raise _Decline
            yield start, points

    def corners(self):
        """All transformed corners as one (3M, 3) array."""
        if not self.count:
            return np.empty((0, 3), dtype=np.float64)
        return np.concatenate([points.reshape((-1, 3)) for _, points in self.chunks()])


# ------------------------------------------------------------ round(v, 6)

def round6(values):
    """Python's ``round(v, 6)`` of every element of a finite float64 array."""
    values = np.asarray(values, dtype=np.float64)
    scaled = values * 1e6
    nearest = np.rint(scaled)
    out = nearest / 1e6
    # ``scaled`` is within half an ulp of the exact product, and
    # ``scaled - floor(scaled)`` within an ulp of max(|scaled|, 1) of its
    # true fraction. Away from a tie by more than both, the exact product
    # rounds to the same integer as ``scaled``; ties and near ties, and the
    # magnitudes where that bound stops holding, go to Python's round().
    fraction = scaled - np.floor(scaled)
    slack = 4.0 * np.spacing(np.maximum(np.abs(scaled), 1.0))
    unsure = (np.abs(fraction - 0.5) <= slack) | (np.abs(values) >= 4e9)
    if unsure.any():
        flat_out = out.reshape(-1)
        flat_in = values.reshape(-1)
        for index in np.flatnonzero(unsure.reshape(-1)).tolist():
            flat_out[index] = round(float(flat_in[index]), 6)
    return out


# ------------------------------------------------------------ welding

def _weld_ids(keys):
    """First-occurrence welding of (N, 3) float64 keys; ``(first_rows, ids)``.

    ``first_rows[k]`` is the row of vertex k's first corner, ``ids[i]`` the
    vertex of row i. Adding 0.0 folds ``-0.0`` into ``0.0`` so equal values
    weld as Python's ``==`` does; the values are finite, so equal bit
    patterns then mean equal values.
    """
    if not len(keys):
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)
    folded = np.ascontiguousarray(keys + 0.0).view(np.int64).reshape((-1, 3))
    order = np.lexsort((folded[:, 2], folded[:, 1], folded[:, 0]))  # stable
    ranked = folded[order]
    new = np.empty(len(order), dtype=np.bool_)
    new[0] = True
    np.any(ranked[1:] != ranked[:-1], axis=1, out=new[1:])
    group = np.cumsum(new) - 1
    first = order[new]  # lowest row of each group, by the stable sort
    by_first = np.argsort(first, kind='stable')
    label = np.empty(len(first), dtype=np.int64)
    label[by_first] = np.arange(len(first), dtype=np.int64)
    ids = np.empty(len(order), dtype=np.int64)
    ids[order] = label[group]
    return first[by_first], ids


def _weld(mesh, rounded):
    corners = mesh.corners()
    keys = round6(corners) if rounded else corners
    first, ids = _weld_ids(keys)
    return keys[first], ids.reshape((-1, 3))


def weld(triangles, round6=True):
    """``mesh_native.weld``'s indexed mesh ``(vertices (N, 3), faces (M, 3) int64)``, or None."""
    return _guarded('weld', lambda mesh: _weld(mesh, round6), triangles)


# ------------------------------------------------------------ measures

def _first_zero_sign(column):
    """True when the first zero of ``column`` is -0.0; None without a zero."""
    zeros = np.flatnonzero(column == 0.0)
    if not len(zeros):
        return None
    return bool(np.signbit(column[zeros[0]]))


def _measure(mesh):
    if not mesh.count:
        raise _Decline  # the Python loops raise their own error
    lo = [None] * 3
    hi = [None] * 3
    zero_sign = [None] * 3  # per axis: is the first zero in scan order -0.0
    area = np.zeros(1)
    volume = np.zeros(1)
    for _, points in mesh.chunks():
        flat = points.reshape((-1, 3))
        for axis in range(3):
            column = flat[:, axis]
            low, high = float(column.min()), float(column.max())
            lo[axis] = low if lo[axis] is None else min(lo[axis], low)
            hi[axis] = high if hi[axis] is None else max(hi[axis], high)
            if zero_sign[axis] is None:
                zero_sign[axis] = _first_zero_sign(column)
        a, b, c = points[:, 0], points[:, 1], points[:, 2]
        ax, ay, az = a[:, 0], a[:, 1], a[:, 2]
        bx, by, bz = b[:, 0], b[:, 1], b[:, 2]
        cx, cy, cz = c[:, 0], c[:, 1], c[:, 2]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        area_terms = 0.5 * np.sqrt(nx * nx + ny * ny + nz * nz)
        volume_terms = (ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx) + az * (bx * cy - by * cx)) / 6.0
        area = np.add.accumulate(np.concatenate((area[-1:], area_terms)))[-1:]
        volume = np.add.accumulate(np.concatenate((volume[-1:], volume_terms)))[-1:]
    for axis in range(3):
        # A zero bound keeps the first zero's sign: the scalar loop moves
        # only on a strictly smaller (larger) value, and -0.0 == 0.0.
        if lo[axis] == 0.0:
            lo[axis] = -0.0 if zero_sign[axis] else 0.0
        if hi[axis] == 0.0:
            hi[axis] = -0.0 if zero_sign[axis] else 0.0
    return lo, hi, float(area[0]), float(volume[0])


def measure(triangles):
    """``mesh_native.measure``'s ``(lo, hi, area, signed_volume)``, or None."""
    return _guarded('measure', _measure, triangles)


# ------------------------------------------------------------ 3MF text

def _fixed6(values):
    """``(sign, whole, millionths)`` columns that print ``f'{v:.6f}'`` of round6 output."""
    if (np.abs(values) >= 1e6).any():
        raise _Decline  # the general formatter's range; Python prints it
    scaled = np.rint(np.abs(values) * 1e6).astype(np.int64)
    sign = np.where(np.signbit(values), '-', '')
    return sign, scaled // 1_000_000, scaled % 1_000_000


def _threemf(mesh):
    vertices, faces = _weld(mesh, rounded=True)
    sx, wx, fx = _fixed6(vertices[:, 0])
    sy, wy, fy = _fixed6(vertices[:, 1])
    sz, wz, fz = _fixed6(vertices[:, 2])
    columns = (sx.tolist(), wx.tolist(), fx.tolist(), sy.tolist(), wy.tolist(), fy.tolist(),
               sz.tolist(), wz.tolist(), fz.tolist())
    vertex_text = '\n'.join(map(_VERTEX.__mod__, zip(*columns)))
    triangle_text = '\n'.join(map(_TRIANGLE.__mod__, map(tuple, faces.tolist())))
    return vertex_text, triangle_text


def threemf_text(triangles):
    """``mesh_native.threemf_text``'s ``(vertex_lines, triangle_lines)`` text, or None."""
    return _guarded('threemf_text', _threemf, triangles)


# ------------------------------------------------------------ topology

def _components(count, left, right):
    """Root label per node of the graph with edges ``left[i] -- right[i]``.

    Hook every root onto the smallest root it touches, then shortcut until
    every node points at its root; repeat over the edges still crossing two
    roots. Labels only decrease, so there are no cycles, and every round
    merges at least one pair of roots.
    """
    parent = np.arange(count, dtype=np.int64)
    left = np.asarray(left, dtype=np.int64)
    right = np.asarray(right, dtype=np.int64)
    while len(left):
        a, b = parent[left], parent[right]
        crossing = a != b
        if not crossing.any():
            break
        left, right, a, b = left[crossing], right[crossing], a[crossing], b[crossing]
        np.minimum.at(parent, np.maximum(a, b), np.minimum(a, b))
        while True:
            jumped = parent[parent]
            if np.array_equal(jumped, parent):
                break
            parent = jumped
    return parent


def _topology(mesh):
    corners = mesh.corners()
    first, ids = _weld_ids(round6(corners))
    nv = len(first)
    ids = ids.reshape((-1, 3))
    del first
    points = corners.reshape((-1, 3, 3))
    u = points[:, 1] - points[:, 0]
    v = points[:, 2] - points[:, 0]
    c0 = u[:, 1] * v[:, 2] - u[:, 2] * v[:, 1]
    c1 = u[:, 2] * v[:, 0] - u[:, 0] * v[:, 2]
    c2 = u[:, 0] * v[:, 1] - u[:, 1] * v[:, 0]
    sumsq = c0 * c0 + c1 * c1 + c2 * c2
    del corners, points, u, v, c0, c1, c2
    distinct = (ids[:, 0] != ids[:, 1]) & (ids[:, 1] != ids[:, 2]) & (ids[:, 0] != ids[:, 2])
    keep = distinct & (sumsq > 1e-18)
    faces = ids[keep]
    degenerate = int(len(ids) - len(faces))
    nf = len(faces)
    if not nf:
        return _counts(degenerate, 0, 0, 0, 0, 0)
    # Corner 3f+k runs from faces[f][k] to faces[f][k+1].
    start = faces.reshape(-1)
    end = faces[:, (1, 2, 0)].reshape(-1)
    low, high = np.minimum(start, end), np.maximum(start, end)
    order = np.argsort(low * nv + high, kind='stable')
    key = (low * nv + high)[order]
    new = np.empty(len(order), dtype=np.bool_)
    new[0] = True
    new[1:] = key[1:] != key[:-1]
    heads = np.flatnonzero(new)
    sizes = np.diff(np.append(heads, len(order)))
    group = np.cumsum(new) - 1
    first_corner = order[heads]

    # Shells: every use of an edge joins the face of its first use.
    rest = ~new
    shell_roots = _components(nf, first_corner[group[rest]] // 3, order[rest] // 3)
    shells = int(np.count_nonzero(shell_roots == np.arange(nf)))

    singular = np.zeros(nv, dtype=np.bool_)
    odd = sizes != 2
    singular[start[first_corner[odd]]] = True
    singular[end[first_corner[odd]]] = True
    boundary = int(np.count_nonzero(sizes == 1))
    nonmanifold = int(np.count_nonzero(sizes > 2))

    # Closed edges: same direction is a winding inconsistency; either way
    # the corners on each side of the edge join one fan.
    pair = sizes == 2
    one = first_corner[pair]
    two = order[heads[pair] + 1]
    same = (start[one] < end[one]) == (start[two] < end[two])
    winding = int(np.count_nonzero(same))

    def following(corner):
        return (corner // 3) * 3 + (corner + 1) % 3
    left = np.concatenate((one, following(one)))
    right = np.concatenate((np.where(same, two, following(two)), np.where(same, following(two), two)))
    corner_roots = _components(nf * 3, left, right)
    # A vertex whose corners fall in more than one fan is singular.
    by_vertex = np.argsort(start, kind='stable')
    vertex_sorted = start[by_vertex]
    roots_sorted = corner_roots[by_vertex]
    heads_v = np.flatnonzero(np.r_[True, vertex_sorted[1:] != vertex_sorted[:-1]])
    spread = np.minimum.reduceat(roots_sorted, heads_v) != np.maximum.reduceat(roots_sorted, heads_v)
    singular[vertex_sorted[heads_v[spread]]] = True
    return _counts(degenerate, nf, shells, boundary, nonmanifold, int(np.count_nonzero(singular)),
                   winding)


def _counts(degenerate, nf, shells, boundary, nonmanifold, singular, winding=0):
    count = nf + degenerate
    return {'format': 'triangle-mesh', 'triangles': count, 'shells': shells,
            'triangle_count': count, 'connected_shells': shells,
            'boundary_edges': boundary, 'non_manifold_edges': nonmanifold,
            'non_manifold_vertices': singular, 'singular_vertices': singular,
            'winding_inconsistencies': winding, 'degenerate_triangles': degenerate,
            'watertight': boundary == 0 and nonmanifold == 0,
            'manifold': not (boundary or nonmanifold or singular or degenerate or winding)}


def topology(triangles):
    """``mesh_native.topology``'s dict, or None."""
    from .mesh_native import LIST_TOPOLOGY_MIN
    lists = hasattr(triangles, '__len__') and len(triangles) > LIST_TOPOLOGY_MIN
    return _guarded('topology', _topology, triangles, allow_lists=lists)
