"""Cut a closed triangle mesh with an axis plane and cap both sides (#1245).

The AI fix split used to assign whole triangles to each side, which only
separates pieces that are already apart. This module does a real planar
cut, numpy only (trimesh and manifold3d are not runtime dependencies):

1. weld the mesh; a vertex within the weld grid of the plane is moved onto
   it, so no cut point lands within the weld distance of a vertex;
2. clip every triangle that crosses the plane into triangles that lie on
   one side, with one shared cut point per crossed edge, so both sides meet
   on identical edges;
3. faces lying in the plane go to the side their normal faces away from;
4. the open rims of the lower side are chained into loops in the plane
   (``mesh_repair.boundary_loops``), sorted into outer loops and holes by
   their winding, each hole bridged into the smallest outer loop holding
   it, and every outer loop ear-clipped. The lower side gets that cap
   facing +axis, the upper side the same triangles reversed;
5. the result is checked, not trusted: each side must be closed (no open,
   non-manifold or inconsistent edge) with positive volume, and the two
   volumes must sum to the source's. Anything else raises ``FixError``.

A plane through solid material needs a closed, consistently wound source;
an open or non-manifold mesh is refused by name. A plane that crosses no
triangle and opens no rim (pieces that are already apart) needs no cap and
works on any mesh.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import mesh_repair

FixError = mesh_repair.FixError

ON_PLANE_MM = mesh_repair.WELD_MM
VOLUME_RTOL = 1e-6
# (u, v) for each cut axis, right handed with the axis: u x v = +axis, so a
# loop wound counterclockwise in (u, v) faces +axis.
_UV = {0: (1, 2), 1: (2, 0), 2: (0, 1)}


@dataclass
class Cut:
    """Both sides of a cut, each as (points, faces) with outward winding."""
    lower: tuple
    upper: tuple
    capped: bool          # False: the plane only separated pieces already apart
    loops: int = 0        # outer cap loops (separate cut regions)
    holes: int = 0        # holes inside them (a tube cut gives one of each)


def cut(tris, axis: int, at: float) -> Cut | None:
    """Cut ``tris`` at ``points[axis] == at``; None when one side is empty."""
    points, faces = mesh_repair.weld(tris)
    if not len(faces):
        return None
    points = points.copy()
    dist = points[:, axis] - float(at)
    side = np.where(np.abs(dist) <= ON_PLANE_MM, 0, np.sign(dist)).astype(np.int8)
    points[side == 0, axis] = float(at)
    dist[side == 0] = 0.0
    face_side = side[faces]
    crossing = (face_side.min(axis=1) < 0) & (face_side.max(axis=1) > 0)
    needs_cap = bool(crossing.any()) or _has_plane_rim(points, faces, face_side, axis)
    if needs_cap:
        _require_closed(points, faces)
        if mesh_repair.signed_volume(points[faces]) < 0:
            faces = faces[:, [0, 2, 1]]
            face_side = side[faces]
    source_volume = abs(mesh_repair.signed_volume(points[faces]))
    points, lower, upper = _clip(points, faces, face_side, crossing, side, dist, axis, float(at))
    if not len(lower) or not len(upper):
        return None
    if not needs_cap:
        # Pieces already apart: nothing is opened, so nothing is capped, and
        # an open source keeps its own openings on whichever side they are.
        return Cut(_compact(points, lower), _compact(points, upper), capped=False)
    loops = mesh_repair.boundary_loops(lower, len(points))
    if not loops:
        raise FixError('the cut left no rim to cap; nothing was changed')
    cap, outer_count, hole_count = _cap(points, loops, axis, float(at))
    low = _compact(points, np.concatenate([lower, cap]))
    high = _compact(points, np.concatenate([upper, cap[:, ::-1]]))
    _verify(low, high, source_volume)
    return Cut(low, high, capped=True, loops=outer_count, holes=hole_count)


# ---------------------------------------------------------------- source checks

def _whole_sides(points, faces, face_side, axis):
    """(lower, upper) masks for faces that do not cross the plane.

    A face lying in the plane goes to the side its normal faces away from
    (a ledge's top stays with the part below it); a zero-area one to neither.
    """
    flat = (face_side.max(axis=1) == 0) & (face_side.min(axis=1) == 0)
    normal = np.cross(points[faces[:, 1]] - points[faces[:, 0]],
                      points[faces[:, 2]] - points[faces[:, 0]])[:, axis]
    lower = (face_side.min(axis=1) < 0) | (flat & (normal > 0))
    upper = ~lower & ((face_side.max(axis=1) > 0) | (flat & (normal < 0)))
    return lower, upper


def _has_plane_rim(points, faces, face_side, axis) -> bool:
    """Whether faces going to opposite sides meet along an edge (in the plane)."""
    lower, upper = _whole_sides(points, faces, face_side, axis)
    if not lower.any() or not upper.any():
        return False
    n = np.int64(max(len(points), 1))

    def keys(sel):
        f = faces[sel]
        e = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
        return np.unique(e.min(axis=1) * n + e.max(axis=1))
    return bool(len(np.intersect1d(keys(lower), keys(upper))))


def _require_closed(points, faces) -> None:
    boundary, nonmanifold, inconsistent = mesh_repair.edge_stats(faces, len(points))
    degenerate = int(((faces[:, 0] == faces[:, 1]) | (faces[:, 1] == faces[:, 2])
                      | (faces[:, 2] == faces[:, 0])).sum())
    problems = []
    if boundary:
        problems.append(f'{boundary} open edge{"s" if boundary != 1 else ""}')
    if nonmanifold:
        problems.append(f'{nonmanifold} non-manifold edge{"s" if nonmanifold != 1 else ""}')
    if inconsistent:
        problems.append(f'{inconsistent} edge{"s" if inconsistent != 1 else ""} '
                        'between faces wound opposite ways')
    if degenerate:
        problems.append(f'{degenerate} collapsed triangle{"s" if degenerate != 1 else ""}')
    if problems:
        raise FixError('the model is not a closed solid (' + ', '.join(problems) + '), so a '
                       'cut through it cannot be capped into two solid parts; repair the '
                       'mesh first. Nothing was changed')


# ---------------------------------------------------------------- clipping

def _clip(points, faces, face_side, crossing, side, dist, axis, at):
    """(points with cut points appended, lower faces, upper faces)."""
    n = np.int64(len(points))
    cross_faces = faces[crossing]
    edges = np.concatenate([cross_faces[:, [0, 1]], cross_faces[:, [1, 2]],
                            cross_faces[:, [2, 0]]])
    edges = np.sort(edges, axis=1)
    cut_edge = side[edges[:, 0]].astype(np.int64) * side[edges[:, 1]] < 0
    edges = np.unique(edges[cut_edge], axis=0)
    index = {}
    if len(edges):
        i, j = edges[:, 0], edges[:, 1]
        t = dist[i] / (dist[i] - dist[j])
        new = points[i] + t[:, None] * (points[j] - points[i])
        new[:, axis] = at
        index = {int(a) * int(n) + int(b): len(points) + k
                 for k, (a, b) in enumerate(edges.tolist())}
        points = np.concatenate([points, new])

    def x(a, b):
        return index[min(a, b) * int(n) + max(a, b)]

    lower, upper = [], []
    for a, b, c in cross_faces.tolist():
        sa, sb, sc = int(side[a]), int(side[b]), int(side[c])
        if 0 in (sa, sb, sc):
            # rotate the on-plane vertex first: (z, b, c) with b, c apart
            while sa != 0:
                a, b, c, sa, sb, sc = b, c, a, sb, sc, sa
            p = x(b, c)
            (lower if sb < 0 else upper).append((a, b, p))
            (lower if sc < 0 else upper).append((a, p, c))
            continue
        # rotate the lone vertex first: (a, b, c) with b, c on the other side
        if sa == sb:
            a, b, c, sa, sb, sc = c, a, b, sc, sa, sb
        elif sa == sc:
            a, b, c, sa, sb, sc = b, c, a, sb, sc, sa
        pab, pac = x(a, b), x(a, c)
        lone, rest = (lower, upper) if sa < 0 else (upper, lower)
        lone.append((a, pab, pac))
        rest.append((pab, b, c))
        rest.append((pab, c, pac))

    whole = faces[~crossing]
    goes_lower, goes_upper = _whole_sides(points, whole, face_side[~crossing], axis)
    as_array = lambda rows: np.asarray(rows, dtype=np.int64).reshape(-1, 3)  # noqa: E731
    return (points,
            np.concatenate([whole[goes_lower], as_array(lower)]),
            np.concatenate([whole[goes_upper], as_array(upper)]))


# ---------------------------------------------------------------- caps

def _cap(points, loops, axis, at):
    """(cap faces wound to face +axis, outer loop count, hole count)."""
    uv = points[:, list(_UV[axis])]
    rings = []
    for loop in loops:
        ring = loop['ring']
        if loop['branched'] or np.abs(points[ring, axis] - at).max() > ON_PLANE_MM:
            raise FixError('the cut left a rim that does not close in the plane, so it cannot '
                           'be capped; nothing was changed')
        rings.append((ring, _area(uv[ring])))
    outers = [(ring, area) for ring, area in rings if area > 0]
    holes = [ring for ring, area in rings if area < 0]
    if not outers:
        raise FixError('the cut outline has no outer edge to cap; nothing was changed')
    assigned = {k: [] for k in range(len(outers))}
    for hole in holes:
        probe = uv[hole].mean(axis=0) if len(hole) == 3 else uv[hole[0]]
        holders = [k for k, (ring, _area_) in enumerate(outers)
                   if _inside(uv[ring], uv[hole[0]]) or _inside(uv[ring], probe)]
        if not holders:
            raise FixError('the cut outline has a hole outside every outer edge, so it '
                           'cannot be capped; nothing was changed')
        assigned[min(holders, key=lambda k: outers[k][1])].append(hole)
    tris = []
    for k, (ring, _area_) in enumerate(outers):
        tris.extend(_triangulate(uv, list(ring), assigned[k]))
    return np.asarray(tris, dtype=np.int64).reshape(-1, 3), len(outers), len(holes)


def _area(poly) -> float:
    u, v = poly[:, 0], poly[:, 1]
    return float((u * np.roll(v, -1) - np.roll(u, -1) * v).sum() / 2.0)


def _inside(poly, point) -> bool:
    """Even-odd point in polygon (``poly`` an (n, 2) ring)."""
    u, v = poly[:, 0], poly[:, 1]
    u2, v2 = np.roll(u, -1), np.roll(v, -1)
    straddle = (v > point[1]) != (v2 > point[1])
    with np.errstate(divide='ignore', invalid='ignore'):
        at_u = u + (point[1] - v) * (u2 - u) / (v2 - v)
    return bool((straddle & (point[0] < at_u)).sum() % 2)


def _orient(a, b, c) -> float:
    return float((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))


def _triangulate(uv, outer, holes):
    """Ear-clip one counterclockwise outer ring with its clockwise holes."""
    ring = list(outer)
    for hole in sorted(holes, key=lambda h: float(uv[h, 0].min())):
        ring = _bridge(uv, ring, list(hole))
    return _ear_clip(uv, ring)


def _bridge(uv, ring, hole):
    """Splice ``hole`` into ``ring`` along a diagonal from its leftmost vertex.

    The diagonal runs from the hole's leftmost vertex M to the ring vertex
    the ray toward -u reaches first, or, when ring vertices sit inside the
    triangle that ray spans, to the one at the smallest angle from the ray
    (the earcut rule), so the diagonal crosses no edge.
    """
    start = min(range(len(hole)), key=lambda k: (uv[hole[k], 0], uv[hole[k], 1]))
    m = uv[hole[start]]
    pts = uv[ring]
    nxt = np.roll(pts, -1, axis=0)
    v0, v1 = pts[:, 1], nxt[:, 1]
    spans = ((v0 <= m[1]) & (m[1] <= v1)) | ((v1 <= m[1]) & (m[1] <= v0))
    spans &= v0 != v1
    with np.errstate(divide='ignore', invalid='ignore'):
        hit_u = pts[:, 0] + (m[1] - v0) * (nxt[:, 0] - pts[:, 0]) / (v1 - v0)
    spans &= hit_u <= m[0]
    level = (v0 == m[1]) & (pts[:, 0] <= m[0])   # a ring vertex straight left of M
    if not spans.any() and not level.any():
        raise FixError('a hole in the cut outline is not inside its outer edge, so it cannot '
                       'be capped; nothing was changed')
    best_u = max(hit_u[spans].max() if spans.any() else -np.inf,
                 pts[level, 0].max() if level.any() else -np.inf)
    if level.any() and pts[level, 0].max() == best_u:
        candidates = [k for k in np.nonzero(level)[0].tolist() if pts[k, 0] == best_u]
    else:
        e = int(np.nonzero(spans & (hit_u == best_u))[0][0])
        f = (e + 1) % len(ring)
        target = e if pts[e, 0] < pts[f, 0] else f
        hit = np.array([best_u, m[1]])
        # Ring vertices inside the triangle (M, hit, target) would block the
        # straight diagonal; take the one at the smallest angle from the ray.
        a, b, c = m, hit, pts[target]
        if _orient(a, b, c) < 0:
            b, c = c, b
        inside = ((_orient_many(a, b, pts) >= 0) & (_orient_many(b, c, pts) >= 0)
                  & (_orient_many(c, a, pts) >= 0) & (pts[:, 0] < m[0]))
        inside[target] = True
        ks = np.nonzero(inside)[0]
        tan = np.abs(pts[ks, 1] - m[1]) / np.maximum(m[0] - pts[ks, 0], 1e-300)
        best = tan.min()
        near = ks[tan == best]
        far_u = pts[near, 0].max()
        candidates = [int(k) for k in near if pts[k, 0] == far_u]
    # Coincident ring vertices (a pinch, or an earlier bridge): use the copy
    # whose corner the diagonal leaves into the interior.
    for k in candidates + [k for k in range(len(ring))
                           if k not in candidates and (pts[k] == pts[candidates[0]]).all()]:
        if _locally_inside(pts, k, m):
            j = k
            break
    else:
        j = candidates[0]
    loop = hole[start:] + hole[:start]
    return ring[:j + 1] + loop + [loop[0], ring[j]] + ring[j + 1:]


def _locally_inside(pts, k, point) -> bool:
    a, b, c = pts[k - 1], pts[k], pts[(k + 1) % len(pts)]
    if _orient(a, b, c) >= 0:   # convex corner: inside both edges
        return _orient(a, b, point) >= 0 and _orient(b, c, point) >= 0
    return _orient(a, b, point) >= 0 or _orient(b, c, point) >= 0


def _orient_many(a, b, pts):
    return (b[0] - a[0]) * (pts[:, 1] - a[1]) - (b[1] - a[1]) * (pts[:, 0] - a[0])


def _ear_clip(uv, ring):
    """Triangles (vertex ids, counterclockwise) for one simple ring.

    Repeated vertex ids (the two sides of a hole bridge) are fine: a point
    that coincides with a corner of the candidate ear never blocks it.
    """
    n = len(ring)
    if n < 3:
        return []
    pts = uv[ring]
    prev = [(k - 1) % n for k in range(n)]
    nxt = [(k + 1) % n for k in range(n)]
    alive = np.ones(n, dtype=bool)
    out = []
    remaining = n
    ear = 0
    misses = 0
    scale = float(np.abs(pts).max()) or 1.0
    eps = 1e-18 * scale * scale     # twice the area below which a corner is flat

    def area(p, k, q):
        return _orient(pts[p], pts[k], pts[q])

    # Only a corner that is not strictly convex can lie inside an ear of a
    # simple ring, so only those are tested, and only inside the ear's box.
    us, vs = pts[:, 0], pts[:, 1]
    concave = np.array([area(prev[k], k, nxt[k]) <= eps for k in range(n)], dtype=bool)

    def is_ear(p, k, q):
        if area(p, k, q) <= eps:
            return False
        a, b, c = pts[p], pts[k], pts[q]
        lo_u, hi_u = min(a[0], b[0], c[0]), max(a[0], b[0], c[0])
        lo_v, hi_v = min(a[1], b[1], c[1]), max(a[1], b[1], c[1])
        near = concave & (us >= lo_u) & (us <= hi_u) & (vs >= lo_v) & (vs <= hi_v)
        others = np.flatnonzero(near)
        if not len(others):
            return True
        cand = pts[others]
        corner = ((cand == a).all(axis=1) | (cand == b).all(axis=1) | (cand == c).all(axis=1))
        cand = cand[~corner]
        if not len(cand):
            return True
        inside = ((_orient_many(a, b, cand) >= 0) & (_orient_many(b, c, cand) >= 0)
                  & (_orient_many(c, a, cand) >= 0))
        return not inside.any()

    while remaining > 3:
        p, q = prev[ear], nxt[ear]
        if is_ear(p, ear, q) or misses > remaining:
            if misses > remaining:
                # No clean ear in a full lap (rounding on a nearly degenerate
                # outline): clip the most convex corner. The volume check in
                # cut() refuses the result if this produced a wrong cap.
                ear = max((k for k in range(n) if alive[k]),
                          key=lambda k: area(prev[k], k, nxt[k]))
                p, q = prev[ear], nxt[ear]
            out.append((ring[p], ring[ear], ring[q]))
            alive[ear] = False
            concave[ear] = False
            nxt[p], prev[q] = q, p
            for r in (p, q):
                concave[r] = area(prev[r], r, nxt[r]) <= eps
            remaining -= 1
            ear = q
            misses = 0
            continue
        ear = q
        misses += 1
    p, q = prev[ear], nxt[ear]
    out.append((ring[p], ring[ear], ring[q]))
    return out


# ---------------------------------------------------------------- result

def volume(points, faces) -> float:
    return mesh_repair.signed_volume(points[faces])


def _compact(points, faces):
    used, inverse = np.unique(faces.reshape(-1), return_inverse=True)
    return points[used], np.asarray(inverse).reshape(-1, 3).astype(np.int64)


def _verify(low, high, source) -> None:
    """Both sides closed with positive volume, summing to the (snapped) source."""
    volumes = []
    for points, faces in (low, high):
        boundary, nonmanifold, inconsistent = mesh_repair.edge_stats(faces, len(points))
        volume = mesh_repair.signed_volume(points[faces])
        if boundary or nonmanifold or inconsistent or volume <= 0:
            raise FixError('the cut did not come out as two closed solids, so it was not '
                           'written; nothing was changed')
        volumes.append(volume)
    if abs(sum(volumes) - source) > VOLUME_RTOL * max(source, 1.0):
        raise FixError(f'the two cut parts hold {sum(volumes):.3f} mm3 against the '
                       f"source's {source:.3f} mm3, so the cut was not written; "
                       'nothing was changed')
