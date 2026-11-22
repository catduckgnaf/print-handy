"""Per-layer geometry scans for the print repair skill (#1777): islands and overhangs.

``island_scan`` finds regions of a printed layer that have no overlap with
the layer below (true floating regions, which a slicer either supports or
prints into the air). ``overhang_scan`` finds the new area of each layer
that lies further than the self-supporting angle (45 degrees by default)
beyond the layer below: cantilevers, lips and bridges.

How a layer is read. The mesh is loaded once and welded through the shared
mesh kernel (``mesh_native.weld``, #1790), so corners that are meant to be
one vertex are exactly one vertex. Each layer is cut at its mid height, and
the cross-section is sampled on horizontal scan rows (``pitch_mm`` apart):
every cut segment adds a signed crossing to each row it spans, and a row is
inside the part wherever its nonzero winding number says so. This needs no
contour stitching, so a contour that does not close (near-duplicate
vertices, slivers, small holes) only disturbs the rows it touches instead of
aborting the scan, and overlapping bodies union the way slicers union them.
A layer is held as row intervals (row, x0, x1), continuous in X; overlap,
subtraction and the 45 degree allowance are interval operations, and regions
are the rows' intervals connected across neighboring rows. The work is
proportional to the faces that cross each layer plus the intervals, not to
the bed area.

Limits and datum: Z is measured from the mesh's lowest point (the bed); the
first layer is the bed contact and is never an island. Areas are sampled
(row length times ``pitch_mm``), so regions narrower than a scan row can be
missed, and the 45 degree allowance carries one row pitch of tolerance so
sampling jitter on steep walls is not reported. This is a geometry
diagnostic, not a toolpath check: what the slicer finally prints is checked
by slicing.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_LAYER_MM = 0.2
DEFAULT_PITCH_MM = 0.05
DEFAULT_ANGLE_DEG = 45.0
# Regions smaller than this are sampling dust, not features (mm2).
MIN_ISLAND_MM2 = 0.05
MIN_OVERHANG_MM2 = 1.0
# Bounded work: a scan that would cross more than this many face-layer
# pairs or hold more than this many row intervals in one layer refuses with
# a clear reason instead of running for minutes. Both scale with real
# uploads: a 1 M triangle, 250 mm tall mesh at 0.2 mm stays far below them.
MAX_FACE_LAYER_PAIRS = 400_000_000
MAX_LAYER_INTERVALS = 20_000_000
MAX_LAYERS = 20_000


class ScanError(ValueError):
    """The scan cannot run on this input (empty mesh, bad parameters, over a work limit)."""


@dataclass(frozen=True)
class Mesh:
    """A welded triangle mesh: ``vertices`` (N, 3) float64 and ``faces`` (M, 3) int64."""

    vertices: np.ndarray
    faces: np.ndarray

    @property
    def triangles(self) -> np.ndarray:
        return self.vertices[self.faces]


# ---------------------------------------------------------------- loading

def _python_weld(coordinates):
    """The oracle welding: corners keyed on Python ``round(v, 6)``, first-corner order.

    Identical to ``mesh_native.weld(..., round6=True)`` and to the 3MF
    writer's welding (``geometry_layout._mesh_xml_lines``).
    """
    flat = np.asarray(coordinates, dtype=np.float64).reshape(-1, 3)
    keys = {}
    order = []
    ids = np.empty(len(flat), dtype=np.int64)
    for i, (x, y, z) in enumerate(flat.tolist()):
        key = (round(x, 6), round(y, 6), round(z, 6))
        index = keys.get(key)
        if index is None:
            index = keys[key] = len(order)
            order.append(key)
        ids[i] = index
    vertices = np.asarray(order, dtype=np.float64).reshape(-1, 3)
    return vertices, ids.reshape(-1, 3)


def weld(triangles) -> Mesh:
    """Weld ``triangles`` ((M, 3, 3) array or a ``CompactTriangles``) into a Mesh.

    Uses the native mesh kernel when it is available and takes the input,
    and the Python oracle otherwise; both give the same vertices and faces.
    """
    from . import mesh_native
    from .compact_geometry import CompactTriangles
    if isinstance(triangles, CompactTriangles):
        compact = triangles
    else:
        array = np.ascontiguousarray(np.asarray(triangles, dtype=np.float64).reshape(-1, 3, 3))
        compact = CompactTriangles()
        compact.coordinates.frombytes(array.tobytes())
    native = mesh_native.weld(compact, round6=True)
    if native is not None:
        vertices, faces = native
        return Mesh(np.ascontiguousarray(vertices), np.ascontiguousarray(faces))
    vertices, faces = _python_weld(np.frombuffer(compact.coordinates, dtype=np.float64))
    return Mesh(vertices, faces)


def load(path) -> Mesh:
    """Every printed mesh of an STL or 3MF, in printer coordinates, welded as one Mesh.

    3MF build items, components and transforms are composed as the slicer
    places them (``support_clearance.leaves``); modeled supports count, since
    they print. STL triangles come from the shared reader, which uses the
    native STL parser when it is available.
    """
    path = Path(path)
    if path.suffix.lower() == '.stl':
        from . import orca_adapter
        triangles = orca_adapter.read_stl_triangles(path)
    else:
        from . import support_clearance
        parts = [np.asarray(leaf['tris'], dtype=np.float64).reshape(-1, 3, 3)
                 for leaf in support_clearance.leaves(path)]
        triangles = np.concatenate(parts) if parts else np.zeros((0, 3, 3))
    if not len(triangles):
        raise ScanError(f'{path.name} has no printable triangles')
    return weld(triangles)


# ---------------------------------------------------------------- row intervals
#
# A set of row intervals is three parallel arrays (row int64, x0, x1 float64),
# sorted by (row, x0), disjoint and non-touching within a row.

def _empty():
    return np.zeros(0, dtype=np.int64), np.zeros(0), np.zeros(0)


def _merge(rows, x0, x1):
    """Join intervals that touch within a row (input sorted, disjoint)."""
    if not len(rows):
        return rows, x0, x1
    join = (rows[1:] == rows[:-1]) & (x0[1:] <= x1[:-1])
    first = np.concatenate(([True], ~join))
    starts = np.flatnonzero(first)
    ends = np.concatenate((starts[1:], [len(rows)])) - 1
    return rows[starts], x0[starts], x1[ends]


def _sweep(rows, xs, deltas, inside):
    """Intervals where ``inside(counts)`` holds, for events sorted by (row, x).

    ``deltas`` is (K, E): one +1/-1 coverage change per event and operand;
    ``inside`` maps the (K, E) running counts to a bool per event, meaning
    "inside from this event to the next one in the same row".
    """
    if not len(rows):
        return _empty()
    counts = np.cumsum(deltas, axis=1)
    state = inside(counts)
    same_row = rows[1:] == rows[:-1]
    keep = state[:-1] & same_row & (xs[1:] > xs[:-1])
    return _merge(rows[:-1][keep], xs[:-1][keep], xs[1:][keep])


def _row_keys(rows, xs):
    """One float key per (row, x) that sorts like the pair."""
    if not len(rows):
        return xs
    base = float(xs.min())
    span = float(xs.max() - base) + 1.0
    return rows * span + (xs - base)


def _boolean(a, b, op):
    """``a | b``, ``a & b`` or ``a - b`` of two interval sets (either may overlap itself)."""
    ra, a0, a1 = a
    rb, b0, b1 = b
    rows = np.concatenate((ra, ra, rb, rb))
    xs = np.concatenate((a0, a1, b0, b1))
    na, nb = len(ra), len(rb)
    da = np.concatenate((np.ones(na), -np.ones(na), np.zeros(2 * nb)))
    db = np.concatenate((np.zeros(2 * na), np.ones(nb), -np.ones(nb)))
    # Each block of events is already sorted, so a stable sort merges them
    # in linear time. The order of equal keys does not matter: the sweep
    # drops zero-length pieces and joins touching ones.
    order = np.argsort(_row_keys(rows, xs), kind='stable')
    deltas = np.vstack((da[order], db[order]))
    if op == 'or':
        def inside(c):
            return (c[0] > 0) | (c[1] > 0)
    elif op == 'and':
        def inside(c):
            return (c[0] > 0) & (c[1] > 0)
    elif op == 'sub':
        def inside(c):
            return (c[0] > 0) & (c[1] <= 0)
    else:
        raise ValueError(op)
    return _sweep(rows[order], xs[order], deltas, inside)


def _dilate(intervals, radius, pitch):
    """Every point within ``radius`` (mm) of the set, on the same scan rows."""
    rows, x0, x1 = intervals
    if not len(rows) or radius <= 0:
        return intervals
    reach = int(math.floor(radius / pitch + 1e-9))
    offsets = np.arange(-reach, reach + 1)
    widths = np.sqrt(np.maximum(radius * radius - (offsets * pitch) ** 2, 0.0))
    out_rows = (rows[None, :] + offsets[:, None]).reshape(-1)
    out_x0 = (x0[None, :] - widths[:, None]).reshape(-1)
    out_x1 = (x1[None, :] + widths[:, None]).reshape(-1)
    order = np.argsort(_row_keys(out_rows, out_x0), kind='stable')
    return _boolean((out_rows[order], out_x0[order], out_x1[order]), _empty(), 'or')


def _overlapping(a, b):
    """Bool per interval of ``a``: does it overlap ``b`` by a positive length?"""
    ra, a0, a1 = a
    rb, b0, b1 = b
    if not len(ra) or not len(rb):
        return np.zeros(len(ra), dtype=bool)
    span = float(max(a1.max(), b1.max()) - min(a0.min(), b0.min())) + 1.0
    base = float(min(a0.min(), b0.min()))
    end_keys = rb * span + (b1 - base)
    # The first interval of b whose end lies after a's start ...
    index = np.searchsorted(end_keys, ra * span + (a0 - base), side='right')
    index = np.minimum(index, len(rb) - 1)
    # ... overlaps a when it is on the same row and starts before a ends.
    return (rb[index] == ra) & (b0[index] < a1) & (b1[index] > a0)


def _label(intervals, pitch):
    """Connected-region label per interval (8-connected across neighboring rows)."""
    rows, x0, x1 = intervals
    n = len(rows)
    if not n:
        return np.zeros(0, dtype=np.int64)
    tol = pitch
    base = float(x0.min()) - 2 * tol
    span = float(x1.max() - base) + 4 * tol + 1.0
    start_keys = rows * span + (x0 - base)
    end_keys = rows * span + (x1 - base)
    # For each interval b, the intervals a one row below with
    # a.x1 > b.x0 - tol and a.x0 < b.x1 + tol.
    below = rows - 1
    lo = np.searchsorted(end_keys, below * span + (x0 - tol - base), side='right')
    hi = np.searchsorted(start_keys, below * span + (x1 + tol - base), side='left')
    count = np.maximum(hi - lo, 0)
    b_idx = np.repeat(np.arange(n), count)
    offsets = np.arange(count.sum()) - np.repeat(np.cumsum(count) - count, count)
    a_idx = np.repeat(lo, count) + offsets
    return _components(n, a_idx, b_idx)


def _compress(parent):
    while True:
        jumped = parent[parent]
        if np.array_equal(jumped, parent):
            return parent
        parent = jumped


def _components(n, a_idx, b_idx):
    """Dense component labels of ``n`` nodes joined by edges ``a_idx``-``b_idx``.

    Union-find by hooking: every round links each edge's larger root under
    its smaller root and then compresses every path, so the number of
    rounds grows with the logarithm of the component size, not its length.
    """
    parent = np.arange(n, dtype=np.int64)
    while len(a_idx):
        ra, rb = parent[a_idx], parent[b_idx]
        differ = ra != rb
        if not differ.any():
            break
        a_idx, b_idx, ra, rb = a_idx[differ], b_idx[differ], ra[differ], rb[differ]
        np.minimum.at(parent, np.maximum(ra, rb), np.minimum(ra, rb))
        parent = _compress(parent)
    labels = parent
    _unique, dense = np.unique(labels, return_inverse=True)
    return dense.reshape(-1)


def _regions(intervals, labels, pitch, y0):
    """Area and XY bounds of each labeled region: list of dicts, label order."""
    rows, x0, x1 = intervals
    if not len(rows):
        return []
    count = int(labels.max()) + 1
    length = x1 - x0
    area = np.bincount(labels, weights=length, minlength=count) * pitch
    xmin = np.full(count, np.inf)
    xmax = np.full(count, -np.inf)
    rmin = np.full(count, np.iinfo(np.int64).max)
    rmax = np.full(count, np.iinfo(np.int64).min)
    np.minimum.at(xmin, labels, x0)
    np.maximum.at(xmax, labels, x1)
    np.minimum.at(rmin, labels, rows)
    np.maximum.at(rmax, labels, rows)
    out = []
    for k in range(count):
        out.append({'area_mm2': float(area[k]),
                    'bounds': [float(xmin[k]), float(y0 + rmin[k] * pitch),
                               float(xmax[k]), float(y0 + (rmax[k] + 1) * pitch)]})
    return out


# ---------------------------------------------------------------- sections

class _Sections:
    """Cross-sections of one mesh on scan rows, one layer at a time."""

    def __init__(self, mesh: Mesh, pitch: float):
        if not len(mesh.faces):
            raise ScanError('the mesh has no triangles')
        tris = mesh.triangles
        if not np.isfinite(tris).all():
            raise ScanError('the mesh has a non-finite coordinate')
        self.tris = tris
        self.faces = mesh.faces
        self.pitch = float(pitch)
        lo, hi = tris.reshape(-1, 3).min(axis=0), tris.reshape(-1, 3).max(axis=0)
        self.lo, self.hi = lo, hi
        self.y0 = float(lo[1]) - self.pitch
        z = tris[:, :, 2]
        self.zmin = z.min(axis=1)
        self.zmax = z.max(axis=1)
        normal = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
        self.nx = normal[:, 0]
        self.ny = normal[:, 1]

    def faces_crossing(self, z_values):
        """Face indices crossing each plane (half-open: a corner at z counts as above)."""
        z_values = np.asarray(z_values, dtype=float)
        first = np.searchsorted(z_values, self.zmin, side='right')
        last = np.searchsorted(z_values, self.zmax, side='right')
        count = np.maximum(last - first, 0)
        total = int(count.sum())
        if total > MAX_FACE_LAYER_PAIRS:
            raise ScanError(f'the scan would cut {total:,} face-layer pairs '
                            f'(limit {MAX_FACE_LAYER_PAIRS:,}); use a larger layer height')
        faces = np.repeat(np.arange(len(self.zmin)), count)
        layers = np.repeat(first, count) + (np.arange(total) - np.repeat(np.cumsum(count) - count, count))
        order = np.argsort(layers, kind='stable')
        faces, layers = faces[order], layers[order]
        bounds = np.searchsorted(layers, np.arange(len(z_values) + 1))
        return [faces[bounds[i]:bounds[i + 1]] for i in range(len(z_values))]

    def layer(self, z, faces):
        """``(intervals, open_rows)`` of the cross-section at height ``z``."""
        if not len(faces):
            return _empty(), 0
        t = self.tris[faces]
        above = t[:, :, 2] >= z
        a_idx = np.array([0, 1, 2])
        b_idx = np.array([1, 2, 0])
        crossing = above[:, a_idx] != above[:, b_idx]
        keep = crossing.sum(axis=1) == 2
        t, crossing = t[keep], crossing[keep]
        nx, ny = self.nx[faces][keep], self.ny[faces][keep]
        if not len(t):
            return _empty(), 0
        pa = t[:, a_idx]
        pb = t[:, b_idx]
        # Interpolate every edge from its lower vertex id, so the two faces
        # sharing an edge get bit-identical cut points and their segments
        # meet exactly. Otherwise a joint landing on a scan row's center
        # can count that row twice or not at all.
        ids = self.faces[faces][keep]
        swap = (ids[:, a_idx] > ids[:, b_idx])[:, :, None]
        pa, pb = np.where(swap, pb, pa), np.where(swap, pa, pb)
        # Edges that do not cross divide by zero; only crossed edges are read.
        with np.errstate(divide='ignore', invalid='ignore'):
            f = (z - pa[:, :, 2]) / (pb[:, :, 2] - pa[:, :, 2])
            points = pa[:, :, :2] + f[:, :, None] * (pb[:, :, :2] - pa[:, :, :2])
        # The two crossed edges of each face, in edge order.
        pick = np.argsort(~crossing, axis=1, kind='stable')[:, :2]
        p = np.take_along_axis(points, pick[:, :, None], axis=1)
        p0, p1 = p[:, 0], p[:, 1]
        d = p1 - p0
        # Orient each segment so the outward normal lies on its right.
        flip = d[:, 1] * nx - d[:, 0] * ny < 0
        p0, p1 = np.where(flip[:, None], p1, p0), np.where(flip[:, None], p0, p1)
        ya, yb = p0[:, 1], p1[:, 1]
        ylo, yhi = np.minimum(ya, yb), np.maximum(ya, yb)
        r_lo = np.ceil((ylo - self.y0) / self.pitch - 0.5).astype(np.int64)
        r_hi = np.ceil((yhi - self.y0) / self.pitch - 0.5).astype(np.int64)
        count = np.maximum(r_hi - r_lo, 0)
        total = int(count.sum())
        if total > MAX_LAYER_INTERVALS:
            raise ScanError(f'the section at Z {z:.2f} has {total:,} row crossings '
                            f'(limit {MAX_LAYER_INTERVALS:,}); use a coarser pitch')
        if not total:
            return _empty(), 0
        seg = np.repeat(np.arange(len(p0)), count)
        rows = np.repeat(r_lo, count) + (np.arange(total) - np.repeat(np.cumsum(count) - count, count))
        y = self.y0 + (rows + 0.5) * self.pitch
        x = p0[seg, 0] + (y - ya[seg]) * (p1[seg, 0] - p0[seg, 0]) / (yb[seg] - ya[seg])
        # A segment travelling up (+Y) has the inside on its left: winding -1
        # for the points to its left as the row is read left to right.
        sign = np.where(yb[seg] > ya[seg], -1, 1)
        order = np.lexsort((x, rows))
        rows, x, sign = rows[order], x[order], sign[order]
        winding = np.cumsum(sign)
        row_start = np.concatenate(([True], rows[1:] != rows[:-1]))
        start_index = np.flatnonzero(row_start)
        offset = np.repeat(winding[start_index] - sign[start_index],
                           np.diff(np.concatenate((start_index, [len(rows)]))))
        winding = winding - offset
        row_end = np.concatenate((rows[1:] != rows[:-1], [True]))
        residual = winding[row_end]
        open_rows = int(np.count_nonzero(residual != 0))
        # A closed row reads the same winding from either end. A row whose
        # section does not close (a missing face) differs by the residual:
        # inside means nonzero both ways, so the defect only drops the span
        # between the gap and its partner instead of filling the rest of
        # the row.
        from_right = winding - np.repeat(residual, np.diff(np.concatenate((start_index, [len(rows)]))))
        inside = (winding != 0) & (from_right != 0)
        intervals = _sweep(rows, x, np.zeros((1, len(rows))), lambda _c: inside)
        return intervals, open_rows


def _layer_planes(lo_z, hi_z, layer_mm, first_layer_mm):
    """``(bottoms, samples)``: each layer's bottom Z and its mid-height cut."""
    if not (layer_mm > 0 and math.isfinite(layer_mm)):
        raise ScanError('layer height must be a positive number')
    first = layer_mm if first_layer_mm is None else float(first_layer_mm)
    if not (first > 0 and math.isfinite(first)):
        raise ScanError('first layer height must be a positive number')
    height = float(hi_z - lo_z)
    count = 1 + max(0, int(math.ceil((height - first) / layer_mm - 1e-9)))
    if count > MAX_LAYERS:
        raise ScanError(f'{count:,} layers is over the {MAX_LAYERS:,} layer limit; '
                        'use a larger layer height')
    tops = lo_z + first + layer_mm * np.arange(count)
    bottoms = np.concatenate(([lo_z], tops[:-1]))
    tops = np.minimum(tops, hi_z)
    return bottoms, (bottoms + tops) / 2.0


def _check_pitch(pitch_mm):
    if not (pitch_mm > 0 and math.isfinite(pitch_mm)):
        raise ScanError('pitch must be a positive number')


# ---------------------------------------------------------------- scans

def _scan(mesh, layer_mm, first_layer_mm, pitch_mm, on_layer):
    _check_pitch(pitch_mm)
    sections = _Sections(mesh, pitch_mm)
    bottoms, samples = _layer_planes(float(sections.lo[2]), float(sections.hi[2]),
                                     float(layer_mm), first_layer_mm)
    per_layer = sections.faces_crossing(samples)
    previous = _empty()
    open_layers = []
    for index, (bottom, z) in enumerate(zip(bottoms, samples, strict=True)):
        current, open_rows = sections.layer(float(z), per_layer[index])
        if open_rows:
            open_layers.append({'z': round(float(bottom), 3), 'open_rows': open_rows})
        on_layer(index, float(bottom), float(z), current, previous, sections)
        previous = current
    return {'layers': len(bottoms), 'z_datum': float(sections.lo[2]),
            'open_layers': open_layers}


def _round_region(region, bottom, z, base_z, extra=None):
    out = {'z': round(bottom - base_z, 3), 'sample_z': round(z - base_z, 3),
           'area_mm2': round(region['area_mm2'], 3),
           'bounds': [round(v, 3) for v in region['bounds']]}
    if extra:
        out.update(extra)
    return out


def island_scan(mesh: Mesh, layer_mm=DEFAULT_LAYER_MM, *, first_layer_mm=None,
                pitch_mm=DEFAULT_PITCH_MM, min_area_mm2=MIN_ISLAND_MM2) -> dict:
    """Regions of each layer with no overlap with the layer below.

    Returns ``{'islands': [{'z', 'sample_z', 'area_mm2', 'bounds'}], 'layers',
    'open_layers', ...}``. ``z`` is the layer's bottom above the bed (the
    mesh's lowest point), ``bounds`` is ``[xmin, ymin, xmax, ymax]`` in the
    mesh's coordinates. ``open_layers`` lists layers whose section did not
    close on some scan rows (they are still scanned).
    """
    islands = []
    base = {}

    def on_layer(index, bottom, z, current, previous, sections):
        base.setdefault('z', float(sections.lo[2]))
        if index == 0 or not len(current[0]):
            return
        touching = _overlapping(current, previous)
        if touching.all():
            return
        labels = _label(current, sections.pitch)
        supported = np.zeros(int(labels.max()) + 1, dtype=bool)
        supported[labels[touching]] = True
        if supported.all():
            return
        for k, region in enumerate(_regions(current, labels, sections.pitch, sections.y0)):
            if not supported[k] and region['area_mm2'] >= min_area_mm2:
                islands.append(_round_region(region, bottom, z, base['z']))

    summary = _scan(mesh, layer_mm, first_layer_mm, pitch_mm, on_layer)
    summary.update({'islands': islands, 'layer_mm': float(layer_mm), 'pitch_mm': float(pitch_mm)})
    return summary


def overhang_scan(mesh: Mesh, layer_mm=DEFAULT_LAYER_MM, *, first_layer_mm=None,
                  pitch_mm=DEFAULT_PITCH_MM, angle_deg=DEFAULT_ANGLE_DEG,
                  min_area_mm2=MIN_OVERHANG_MM2) -> dict:
    """New area of each layer that the layer below does not hold within ``angle_deg``.

    ``angle_deg`` is measured from horizontal, as slicers' support threshold
    is: a wall at least that steep holds itself up. A point of a layer is
    held when it lies within ``layer_mm / tan(angle)`` (plus one scan row of
    sampling tolerance) of the layer below. Unheld
    regions of consecutive layers that overlap in XY are joined into one
    overhang: ``{'z_min', 'z_max', 'layers', 'area_mm2' (summed),
    'max_layer_area_mm2', 'bounds', 'size_mm', 'floating'}``. ``size_mm`` is
    the bounds' X and Y extent; ``floating`` is True when the overhang
    starts with an island (no overlap at all with the layer below).
    """
    if not (0 < angle_deg < 90):
        raise ScanError('angle must be between 0 and 90 degrees')
    reach = float(layer_mm) / math.tan(math.radians(angle_deg))
    found = []
    base = {}

    def on_layer(index, bottom, z, current, previous, sections):
        base.setdefault('z', float(sections.lo[2]))
        if index == 0 or not len(current[0]):
            return
        # Only area the layer below does not cover can be unheld, and only
        # rows of the layer below within reach of it can hold it.
        fresh = _boolean(current, previous, 'sub')
        if not len(fresh[0]):
            return
        radius = reach + sections.pitch
        fresh = _keep(fresh, ~_held_in_row(fresh, previous, radius))
        if not len(fresh[0]):
            return
        near = int(math.floor(radius / sections.pitch + 1e-9))
        wanted = np.unique(fresh[0][:, None] + np.arange(-near, near + 1)[None, :])
        nearby = _keep(previous, np.isin(previous[0], wanted))
        unheld = _boolean(fresh, _dilate(nearby, radius, sections.pitch), 'sub')
        if not len(unheld[0]):
            return
        labels = _label(unheld, sections.pitch)
        # An unheld region is floating when the whole-layer region it belongs
        # to never touches the layer below.
        whole = _label(current, sections.pitch)
        touching = np.zeros(int(whole.max()) + 1, dtype=bool)
        touching[whole[_overlapping(current, previous)]] = True
        floating = np.zeros(int(labels.max()) + 1, dtype=bool)
        floating[labels[_overlapping(unheld, _keep(current, ~touching[whole]))]] = True
        for k, region in enumerate(_regions(unheld, labels, sections.pitch, sections.y0)):
            if region['area_mm2'] >= min_area_mm2:
                found.append((index, bottom, bool(floating[k]), region))

    summary = _scan(mesh, layer_mm, first_layer_mm, pitch_mm, on_layer)
    # Consecutive layers' pieces of one sloped overhang lie up to the
    # allowance apart.
    join_mm = max(float(layer_mm), reach + 2 * float(pitch_mm))
    summary.update({'overhangs': _join_overhangs(found, base.get('z', 0.0), float(layer_mm), join_mm),
                    'layer_mm': float(layer_mm), 'pitch_mm': float(pitch_mm),
                    'angle_deg': float(angle_deg)})
    return summary


def _held_in_row(fresh, previous, radius):
    """Bool per interval of ``fresh = current - previous``: held by its own row.

    Subtraction copies endpoints exactly, so a fresh interval that starts
    where an interval of the layer below ends (or ends where one starts)
    touches it. Touching on one side it is held when no longer than
    ``radius``, touching on both sides when no longer than twice that.
    This settles the thin slivers sloped walls add on every layer without
    dilating the whole layer below.
    """
    rows, x0, x1 = fresh
    prow, p0, p1 = previous
    if not len(rows) or not len(prow):
        return np.zeros(len(rows), dtype=bool)
    every = np.concatenate((x0, x1, p0, p1))
    base = float(every.min())
    span = float(every.max() - base) + 1.0
    ends = prow * span + (p1 - base)
    starts = prow * span + (p0 - base)
    left = np.isin(rows * span + (x0 - base), ends)
    right = np.isin(rows * span + (x1 - base), starts)
    length = x1 - x0
    return ((left & right) & (length <= 2 * radius)) | ((left | right) & (length <= radius))


def _keep(intervals, mask):
    rows, x0, x1 = intervals
    return rows[mask], x0[mask], x1[mask]


def _boxes_touch(a, b, tol):
    return a[0] <= b[2] + tol and b[0] <= a[2] + tol and a[1] <= b[3] + tol and b[1] <= a[3] + tol


def _join_overhangs(found, base_z, layer_mm, join_mm):
    """Join per-layer unheld regions of consecutive layers whose bounds come within ``join_mm``."""
    parent = list(range(len(found)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    by_layer = {}
    for i, (index, _bottom, _floating, _region) in enumerate(found):
        by_layer.setdefault(index, []).append(i)
    for i, (index, _bottom, _floating, region) in enumerate(found):
        for j in by_layer.get(index - 1, ()):
            if _boxes_touch(region['bounds'], found[j][3]['bounds'], join_mm):
                parent[root(i)] = root(j)
    groups = {}
    for i in range(len(found)):
        groups.setdefault(root(i), []).append(i)
    out = []
    for members in groups.values():
        rows = [found[i] for i in members]
        bounds = [min(r[3]['bounds'][0] for r in rows), min(r[3]['bounds'][1] for r in rows),
                  max(r[3]['bounds'][2] for r in rows), max(r[3]['bounds'][3] for r in rows)]
        first = min(rows, key=lambda r: r[0])
        z_min = min(r[1] for r in rows) - base_z
        z_max = max(r[1] for r in rows) - base_z
        out.append({'z_min': round(z_min, 3), 'z_max': round(z_max + layer_mm, 3),
                    'layers': len({r[0] for r in rows}),
                    'area_mm2': round(sum(r[3]['area_mm2'] for r in rows), 3),
                    'max_layer_area_mm2': round(max(r[3]['area_mm2'] for r in rows), 3),
                    'bounds': [round(v, 3) for v in bounds],
                    'size_mm': [round(bounds[2] - bounds[0], 3), round(bounds[3] - bounds[1], 3)],
                    'floating': first[2]})
    out.sort(key=lambda o: (o['z_min'], o['bounds']))
    return out
