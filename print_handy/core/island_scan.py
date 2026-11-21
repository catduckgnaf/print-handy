"""Bounded layer-island analysis for closed triangle meshes (no mesh edits)."""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from . import plane_cut

MAX_TRIANGLES = 100_000
MAX_LAYERS = 20_000
MAX_FACE_LAYER_PAIRS = 20_000_000
MAX_SECTION_WORK = 8_000_000
_AREA_EPS = 1e-10


@dataclass(frozen=True)
class Island:
    z: float
    area: float
    bounds: tuple[float, float, float, float]


@dataclass(frozen=True)
class _Region:
    triangles: tuple
    area: float
    bounds: tuple[float, float, float, float]


def _rings(tris, z):
    """Stitch section edges; points at the plane belong to the lower side."""
    segments = set()
    for tri in tris:
        hits = []
        for i, a in enumerate(tri):
            b = tri[(i + 1) % 3]
            if (a[2] <= z) == (b[2] <= z):
                continue
            # Compute shared edge intersections in the same direction on both faces.
            low, high = (a, b) if a[2] < b[2] else (b, a)
            fraction = (z - low[2]) / (high[2] - low[2])
            hits.append(tuple(low[:2] + fraction * (high[:2] - low[:2])))
        if len(hits) == 2 and hits[0] != hits[1]:
            edge = tuple(sorted(hits))
            # Faces on the same side that meet in a tangent edge cancel out.
            segments.symmetric_difference_update((edge,))
    neighbors = defaultdict(list)
    for a, b in sorted(segments):
        neighbors[a].append(b)
        neighbors[b].append(a)
    if any(len(adjacent) != 2 for adjacent in neighbors.values()):
        raise ValueError(f'open or branching section contour at Z {z:g}')
    seen = set()
    rings = []
    for start in neighbors:
        if start in seen:
            continue
        ring = [start]
        seen.add(start)
        prev, node = start, neighbors[start][0]
        while node != start:
            ring.append(node)
            seen.add(node)
            following = next(p for p in neighbors[node] if p != prev)
            prev, node = node, following
        rings.append(ring)
    return rings


def _regions(rings, z):
    """Fill each outer ring minus its holes with nonoverlapping triangles."""
    uv = np.asarray([p for ring in rings for p in ring], dtype=float).reshape(-1, 2)
    if not len(uv):
        return []
    origin = uv[0].copy()
    uv -= origin  # Keep area arithmetic local even when coordinates are translated.
    contours = []
    offset = 0
    for ring in rings:
        ids = list(range(offset, offset + len(ring)))
        offset += len(ring)
        area = plane_cut._area(uv[ids])
        if abs(area) > _AREA_EPS:
            contours.append((ids if area > 0 else ids[::-1], abs(area)))
    parents: list[int | None] = [None] * len(contours)
    depths = [0] * len(contours)
    order = sorted(range(len(contours)), key=lambda i: -contours[i][1])
    for rank, i in enumerate(order):
        ring, _ = contours[i]
        for j in reversed(order[:rank]):
            if plane_cut._inside(uv[contours[j][0]], uv[ring[0]]):
                parents[i] = j
                depths[i] = depths[j] + 1
                break
    regions = []
    for i, (ring, area) in enumerate(contours):
        if depths[i] % 2:
            continue
        holes = []
        for j, (hole, hole_area) in enumerate(contours):
            if parents[j] == i:
                holes.append(hole[::-1])
                area -= hole_area
        triangles = [uv[list(tri)] for tri in plane_cut._triangulate(uv, ring, holes)]
        filled = [plane_cut._area(tri) for tri in triangles]
        if (not filled or min(filled) < -_AREA_EPS
                or not math.isclose(sum(filled), area, rel_tol=1e-8, abs_tol=_AREA_EPS)):
            raise ValueError(f'could not fill section contour at Z {z:g}')
        outer = uv[ring] + origin
        bounds = (*outer.min(axis=0).tolist(), *outer.max(axis=0).tolist())
        filled_triangles = tuple(tuple(map(tuple, tri + origin))
                                 for tri, signed_area in zip(triangles, filled)
                                 if signed_area > 0)
        regions.append(_Region(filled_triangles, area, bounds))
    return regions


def _intersection_area(subject, clip):
    """Clip a filled triangle against another counterclockwise triangle."""
    origin = np.asarray(clip[0])
    out = [p - origin for p in subject]
    clip = np.asarray(clip) - origin
    for a, b in zip(clip, np.roll(clip, -1, axis=0)):
        edge = b - a

        def side(p):
            d = p - a
            return edge[0] * d[1] - edge[1] * d[0]

        points, out = out, []
        if not points:
            break
        prev = points[-1]
        sp = side(prev)
        for cur in points:
            sc = side(cur)
            if (sp < 0) != (sc < 0):
                out.append(prev + (sp / (sp - sc)) * (cur - prev))
            if sc >= 0:
                out.append(cur)
            prev, sp = cur, sc
    return abs(plane_cut._area(np.asarray(out))) if len(out) >= 3 else 0.0


def _overlaps(first, second):
    ax, ay, bx, by = first.bounds
    cx, cy, dx, dy = second.bounds
    if bx <= cx or dx <= ax or by <= cy or dy <= ay:
        return False
    return any(_intersection_area(a, b) > _AREA_EPS
               for a in first.triangles for b in second.triangles)


def scan(triangles, layer_h: float, *, max_triangles: int = MAX_TRIANGLES,
         max_layers: int = MAX_LAYERS, max_face_layer_pairs: int = MAX_FACE_LAYER_PAIRS,
         max_section_work: int = MAX_SECTION_WORK) -> list[Island]:
    """Return islands at layer midpoints, in input units (normally mm and mm²).

    The mesh's minimum Z is the bed datum; its first sampled layer is anchored.
    Each later filled cross-section region needs positive-area overlap with the
    preceding sample. An empty sample supports nothing. This diagnoses sampled
    geometry, not individual extrusion paths or printability.
    """
    try:
        h = float(layer_h)
        tris = np.asarray(triangles, dtype=float)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('triangles and layer_h must be numeric') from exc
    if not math.isfinite(h) or h <= 0:
        raise ValueError('layer_h must be finite and positive')
    if tris.ndim != 3 or tris.shape[1:] != (3, 3):
        raise ValueError('triangles must have shape (n, 3, 3)')
    if len(tris) > max_triangles:
        raise ValueError('triangle limit exceeded')
    if not np.isfinite(tris).all():
        raise ValueError('triangle coordinates must be finite')
    if min(max_triangles, max_layers, max_face_layer_pairs, max_section_work) <= 0:
        raise ValueError('work limits must be positive')
    if not len(tris):
        return []
    low, high = tris[:, :, 2].min(axis=1), tris[:, :, 2].max(axis=1)
    bed = float(low.min())
    layer_span = (float(high.max()) - bed) / h
    if not math.isfinite(layer_span) or layer_span > max_layers:
        raise ValueError('layer limit exceeded')
    count = math.ceil(layer_span)
    if len(tris) * count > max_face_layer_pairs:
        raise ValueError('face-layer work limit exceeded')
    previous = []
    previous_size = 0
    found = []
    for layer in range(count):
        z = bed + (layer + 0.5) * h
        rings = _rings(tris[(low <= z) & (z < high)], z)
        # Bound polygon hierarchy, triangulation, and overlap work separately
        # from the number of face/plane checks. Hole bridges add two vertices.
        size = sum(map(len, rings)) + 2 * len(rings)
        max_section_work -= size * (size + previous_size)
        if max_section_work < 0:
            raise ValueError('section geometry work limit exceeded')
        current = _regions(rings, z)
        if layer:
            for region in sorted(current, key=lambda r: r.bounds):
                if not any(_overlaps(region, old) for old in previous):
                    found.append(Island(z, region.area, region.bounds))
        previous, previous_size = current, size
    return found
