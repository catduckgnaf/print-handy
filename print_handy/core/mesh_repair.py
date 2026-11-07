"""Deterministic mesh health facts and mesh repair for "Fix this for my printer" (#978).

Everything here is numpy-only (numpy is a runtime dependency; scipy and
trimesh are not) and never touches the source file: a repair writes a new
binary STL and reports exactly what it changed.

What a repair does, in order:

1. weld duplicate vertices (0.1 micron grid), drop degenerate and
   duplicate triangles;
2. make face orientation consistent: faces are re-oriented across every
   two-face edge so neighbours agree, then each connected piece is turned
   outward (positive volume). A mesh with half its faces flipped reads as
   closed but has a meaningless volume; the old repair only flipped a
   globally negative mesh;
3. fill boundary loops, telling *damage* holes from *designed openings*:
   a large, flat loop (the end of a tube, the mouth of a vent) is an
   opening, filled only when the caller asks for it; a loop that passes
   through a vertex with more than one open edge (a branched rim) is never
   fanned, because a fan across it is a guess that can double a volume;
4. refuse instead of reporting a bad result: a repair that changes nothing,
   a result that still has non-manifold edges (3+ faces on one edge, which
   only a full remesh can fix), or damage-hole fills that change the
   volume by more than a quarter all raise ``FixError`` and nothing is
   applied.

``wall_thickness`` measures how thick the walls are by casting rays inward
from sampled faces, so a thin-wall upload is named with a number.
"""
from __future__ import annotations

from collections import deque
from pathlib import Path

import numpy as np

from . import support_clearance

WELD_MM = 1e-4
MAX_REPAIR_TRIANGLES = 5_000_000
MAX_REORIENT_TRIANGLES = 2_000_000   # the orientation walk is a Python BFS over faces
MAX_WALL_TRIANGLES = 400_000         # each sampled ray is tested against every triangle
OPENING_MIN_AREA_MM2 = 25.0          # a designed opening is at least this large ...
OPENING_MIN_SURFACE_SHARE = 0.02     # ... and at least this share of the surface ...
OPENING_PLANAR_MM = 0.05             # ... and flat to within max(this, 1% of its width)
DAMAGE_FILL_MAX_VOLUME_CHANGE = 0.25
THIN_WALL_MM = 0.8                   # two 0.4 mm perimeters


class FixError(RuntimeError):
    """The fix could not be prepared or would be wrong; nothing was changed."""


# ---------------------------------------------------------------- loading

def load_triangles(path):
    """(n, 3, 3) float triangles for an STL or a single-part 3MF source."""
    path = Path(path)
    if path.suffix.lower() == '.stl':
        return np.asarray(support_clearance._stl_tris(path), dtype=float).reshape(-1, 3, 3)
    parts = support_clearance.leaves(path)
    if len(parts) != 1:
        raise FixError(f'this is a {len(parts)}-part model: a mesh repair would flatten it to '
                       'one mesh and lose the per-part assignments, so it is refused')
    return np.asarray(parts[0]['tris'], dtype=float).reshape(-1, 3, 3)


def weld(tris):
    """(points, faces): vertices merged on the weld grid, each kept at full
    precision from its first occurrence."""
    verts = np.asarray(tris, dtype=float).reshape(-1, 3)
    if not len(verts):
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)
    keys = np.round(verts / WELD_MM).astype(np.int64)
    _uniq, first, inverse = np.unique(keys, axis=0, return_index=True, return_inverse=True)
    return verts[first].astype(float), np.asarray(inverse).reshape(-1, 3).astype(np.int64)


# ---------------------------------------------------------------- facts

def _directed_edges(faces):
    return np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]], axis=0)


def edge_stats(faces, n_points=None):
    """(boundary, non-manifold, inconsistent) edge counts.

    boundary: edges used by one face; non-manifold: edges used by three
    or more; inconsistent: two-face edges both faces walk in the same
    direction (their orientations disagree).
    """
    if not len(faces):
        return 0, 0, 0
    n = np.int64((n_points if n_points is not None else int(faces.max()) + 1) or 1)
    directed = _directed_edges(faces)
    lo, hi = directed.min(axis=1), directed.max(axis=1)
    undirected = lo * n + hi
    _u, inv, ucount = np.unique(undirected, return_inverse=True, return_counts=True)
    boundary = int((ucount == 1).sum())
    nonmanifold = int((ucount > 2).sum())
    code = directed[:, 0] * n + directed[:, 1]
    dcodes, dcount = np.unique(code, return_counts=True)
    twice = dcodes[dcount >= 2]
    # an inconsistent manifold edge: two faces, both with the same direction
    two_face = np.unique(undirected[ucount[np.asarray(inv).reshape(-1)] == 2])
    same_dir = np.unique(np.minimum(twice // n, twice % n) * n + np.maximum(twice // n, twice % n))
    inconsistent = int(np.isin(same_dir, two_face).sum())
    return boundary, nonmanifold, inconsistent


def signed_volume(tris):
    tris = np.asarray(tris, dtype=float)
    if not len(tris):
        return 0.0
    return float(np.einsum('ij,ij->i', tris[:, 0], np.cross(tris[:, 1], tris[:, 2])).sum() / 6.0)


def facts_for(tris) -> dict:
    points, faces = weld(tris)
    boundary, nonmanifold, inconsistent = edge_stats(faces, len(points))
    volume = signed_volume(tris)
    watertight = boundary == 0 and nonmanifold == 0
    pts = np.asarray(tris, dtype=float).reshape(-1, 3)
    size = [round(float(v), 2) for v in (pts.max(axis=0) - pts.min(axis=0))] if len(pts) else None
    return {'triangles': int(len(tris)),
            'size_mm': size,
            'watertight': watertight,
            'consistent': inconsistent == 0,
            'sound': watertight and inconsistent == 0,
            'boundary_edges': boundary,
            'nonmanifold_edges': nonmanifold,
            'inconsistent_edges': inconsistent,
            'inverted': bool(volume < 0),
            'signed_volume_mm3': round(volume, 1)}


def mesh_facts(path) -> dict:
    """The mesh health facts a repair decision (and the operator) needs."""
    return facts_for(load_triangles(path))


# ---------------------------------------------------------------- orientation

def _components_and_parity(faces, n_points):
    """Face adjacency across two-face edges, with the flip parity of each pair."""
    directed = _directed_edges(faces)
    n_faces = len(faces)
    face_of = np.tile(np.arange(n_faces), 3)
    n = np.int64(n_points)
    lo, hi = directed.min(axis=1), directed.max(axis=1)
    undirected = lo * n + hi
    order = np.argsort(undirected, kind='stable')
    u_sorted = undirected[order]
    starts = np.flatnonzero(np.r_[True, u_sorted[1:] != u_sorted[:-1]])
    sizes = np.diff(np.r_[starts, len(u_sorted)])
    pairs = starts[sizes == 2]
    a_idx, b_idx = order[pairs], order[pairs + 1]
    fa, fb = face_of[a_idx], face_of[b_idx]
    # Same direction on the shared edge -> the faces disagree (parity 1).
    parity = (directed[a_idx, 0] == directed[b_idx, 0]).astype(np.int8)
    nbr = [[] for _ in range(n_faces)]
    for f, g, p in zip(fa.tolist(), fb.tolist(), parity.tolist()):
        nbr[f].append((g, p))
        nbr[g].append((f, p))
    return nbr


def orient_consistently(points, faces):
    """(faces, reoriented): neighbours agree, and each connected piece faces outward."""
    if not len(faces):
        return faces, 0
    if len(faces) > MAX_REORIENT_TRIANGLES:
        raise FixError(f'this mesh has {len(faces)} triangles; fixing mixed face orientation '
                       f'is limited to {MAX_REORIENT_TRIANGLES}, so nothing was changed')
    nbr = _components_and_parity(faces, len(points))
    flip = np.full(len(faces), -1, dtype=np.int8)
    component = np.full(len(faces), -1, dtype=np.int64)
    comp_id = 0
    for seed in range(len(faces)):
        if flip[seed] >= 0:
            continue
        flip[seed] = 0
        component[seed] = comp_id
        queue = deque([seed])
        while queue:
            f = queue.popleft()
            for g, p in nbr[f]:
                if flip[g] < 0:
                    flip[g] = flip[f] ^ p
                    component[g] = comp_id
                    queue.append(g)
        comp_id += 1
    out = faces.copy()
    flipped = flip == 1
    out[flipped] = out[flipped][:, [0, 2, 1]]
    # Each piece outward: a negative-volume piece is turned inside out.
    tris = points[out]
    per_face = np.einsum('ij,ij->i', tris[:, 0], np.cross(tris[:, 1], tris[:, 2]))
    piece_volume = np.bincount(component, weights=per_face, minlength=comp_id)
    inward = piece_volume[component] < 0
    out[inward] = out[inward][:, [0, 2, 1]]
    reoriented = int((flipped ^ inward).sum())
    return out, reoriented


# ---------------------------------------------------------------- boundary loops

def boundary_loops(faces, n_points):
    """[{'ring': [...], 'branched': bool}] for every closed loop of open edges."""
    if not len(faces):
        return []
    directed = _directed_edges(faces)
    n = np.int64(n_points)
    code = directed[:, 0] * n + directed[:, 1]
    reverse = directed[:, 1] * n + directed[:, 0]
    uniq, counts = np.unique(code, return_counts=True)
    once = np.isin(code, uniq[counts == 1])
    lo, hi = directed.min(axis=1), directed.max(axis=1)
    und = lo * n + hi
    uu, ucount = np.unique(und, return_counts=True)
    single = np.isin(und, uu[ucount == 1])
    open_edges = directed[once & single & ~np.isin(reverse, code)]
    # A hole rim walks the reverse of the faces' open edges.
    out = {}
    for a, b in open_edges[:, ::-1].tolist():
        out.setdefault(a, []).append(b)
    used = set()
    loops = []
    for start in list(out):
        for first in list(out[start]):
            if (start, first) in used:
                continue
            ring = [start]
            where = {start: 0}
            used.add((start, first))
            cur = first
            ok = True
            while cur != start:
                if cur in where:
                    # The walk came back through a pinch vertex: the part since
                    # its first visit is a simple cycle of its own. Split it off,
                    # so a rim through a branch becomes simple loops, never a
                    # figure eight fanned as one (#978 review).
                    k = where[cur]
                    sub = ring[k:]
                    for v in sub[1:]:
                        where.pop(v, None)
                    del ring[k + 1:]
                    if len(sub) >= 3:
                        loops.append({'ring': sub, 'branched': False})
                else:
                    where[cur] = len(ring)
                    ring.append(cur)
                nxt = [v for v in out.get(cur, []) if (cur, v) not in used]
                if not nxt or len(used) > len(open_edges) + 1:
                    ok = False
                    break
                used.add((cur, nxt[0]))
                cur = nxt[0]
            if len(ring) >= 3:
                # An unclosed walk (a rim that dead-ends) cannot be fanned.
                loops.append({'ring': ring, 'branched': not ok})
    return loops


def _loop_metrics(points, ring):
    pts = points[ring]
    centroid = pts.mean(axis=0)
    nxt = np.roll(pts, -1, axis=0)
    perimeter = float(np.linalg.norm(nxt - pts, axis=1).sum())
    area = float(np.linalg.norm(np.cross(pts - centroid, nxt - centroid), axis=1).sum() / 2.0)
    centred = pts - centroid
    try:
        normal = np.linalg.svd(centred, full_matrices=False)[2][-1]
        flatness = float(np.abs(centred @ normal).max())
    except np.linalg.LinAlgError:
        flatness = float('inf')
    width = float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0)))
    return {'perimeter_mm': round(perimeter, 2), 'area_mm2': round(area, 2),
            'flatness_mm': round(flatness, 4), 'width_mm': round(width, 2),
            'centroid': centroid}


def _is_opening(metrics, surface_area):
    big = metrics['area_mm2'] >= max(OPENING_MIN_AREA_MM2, OPENING_MIN_SURFACE_SHARE * surface_area)
    flat = metrics['flatness_mm'] <= max(OPENING_PLANAR_MM, 0.01 * metrics['width_mm'])
    return big and flat


# ---------------------------------------------------------------- repair

def _surface_area(tris):
    if not len(tris):
        return 0.0
    return float(np.linalg.norm(np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]),
                                axis=1).sum() / 2.0)


def repair(source_path, dest_path, *, fill_openings=True) -> dict:
    """Repair one mesh file into ``dest_path`` (a binary STL copy); see the module doc."""
    tris = load_triangles(source_path)
    if len(tris) > MAX_REPAIR_TRIANGLES:
        raise FixError(f'this mesh has {len(tris)} triangles, more than the '
                       f'{MAX_REPAIR_TRIANGLES} a mesh repair handles; nothing was changed')
    before = facts_for(tris)
    points, faces = weld(tris)
    if len(faces):
        a, b, c = faces[:, 0], faces[:, 1], faces[:, 2]
        normals = np.cross(points[b] - points[a], points[c] - points[a])
        good = (a != b) & (b != c) & (a != c) & (np.einsum('ij,ij->i', normals, normals) > 0.0)
    else:
        good = np.zeros(0, dtype=bool)
    dropped_degenerate = int((~good).sum())
    faces = faces[good]
    dropped_duplicates = 0
    if len(faces):
        _uf, first_idx = np.unique(np.sort(faces, axis=1), axis=0, return_index=True)
        dropped_duplicates = int(len(faces) - len(first_idx))
        faces = faces[np.sort(first_idx)]
    _b, _nm, inconsistent = edge_stats(faces, len(points))
    reoriented = 0
    if inconsistent:
        faces, reoriented = orient_consistently(points, faces)
    surface = _surface_area(points[faces]) if len(faces) else 0.0
    volume_unfilled = signed_volume(points[faces]) if len(faces) else 0.0
    damage_fans, opening_fans = [], []
    holes_filled = 0
    openings = []
    branched_skipped = 0
    for loop in boundary_loops(faces, len(points)):
        if loop['branched']:
            branched_skipped += 1
            continue
        ring = loop['ring']
        metrics = _loop_metrics(points, ring)
        opening = _is_opening(metrics, surface)
        if opening:
            openings.append({k: metrics[k] for k in ('perimeter_mm', 'area_mm2', 'width_mm')}
                            | {'closed': bool(fill_openings)})
            if not fill_openings:
                continue
        centroid_idx = len(points)
        points = np.vstack([points, metrics['centroid']])
        fan = [(centroid_idx, ring[i], ring[(i + 1) % len(ring)]) for i in range(len(ring))]
        (opening_fans if opening else damage_fans).extend(fan)
        holes_filled += 0 if opening else 1
    volume_change_pct = None
    if damage_fans:
        with_damage = np.vstack([faces, np.array(damage_fans, dtype=np.int64)])
        volume_damage = signed_volume(points[with_damage])
        base = max(abs(volume_damage), abs(volume_unfilled), 1e-9)
        change = abs(volume_damage - volume_unfilled) / base
        volume_change_pct = round(change * 100.0, 1)
        if change > DAMAGE_FILL_MAX_VOLUME_CHANGE:
            raise FixError(f'filling the holes would change the volume by about '
                           f'{volume_change_pct:g}%, which is not a small repair; nothing was '
                           'changed. The holes are probably part of the design or the mesh '
                           'needs a full remesh')
        faces = with_damage
    if opening_fans:
        faces = np.vstack([faces, np.array(opening_fans, dtype=np.int64)])
    out_tris = points[faces] if len(faces) else np.zeros((0, 3, 3))
    flipped = False
    if len(out_tris) and signed_volume(out_tris) < 0:
        out_tris = out_tris[:, [0, 2, 1]]
        flipped = True
    openings_closed = sum(1 for o in openings if o['closed'])
    if not (dropped_degenerate or dropped_duplicates or reoriented or holes_filled
            or openings_closed or flipped):
        if openings:
            raise FixError(f'the only holes are {len(openings)} opening(s) that look designed '
                           '(large and flat) and were kept open, so there is nothing to repair; '
                           'nothing was changed')
        if branched_skipped:
            raise FixError(f'the only holes are {branched_skipped} branched rim(s) that cannot be '
                           'filled safely without a full remesh, so nothing was changed')
        raise FixError('the mesh repair found nothing it can fix (no broken or duplicate '
                       'triangles, no fillable holes, no wrong-way faces), so nothing was changed')
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    support_clearance._write_binary_stl(dest_path, out_tris)
    after = facts_for(out_tris)
    if after['nonmanifold_edges']:
        dest_path.unlink(missing_ok=True)
        raise FixError(f"the repaired mesh would still have {after['nonmanifold_edges']} "
                       'non-manifold edge(s) (three or more faces on one edge); that needs a '
                       'full remesh, so nothing was changed')
    return {'before': before, 'after': after,
            'dropped_degenerate': dropped_degenerate,
            'dropped_duplicates': dropped_duplicates,
            'faces_reoriented': reoriented,
            'holes_filled': holes_filled,
            'openings': openings,
            'openings_closed': openings_closed,
            'branched_loops_skipped': branched_skipped,
            'damage_fill_volume_change_pct': volume_change_pct,
            'flipped_inverted': flipped,
            'output_path': str(dest_path)}


# ---------------------------------------------------------------- wall thickness

def _first_hit(origin, direction, v0, e1, e2, eps=1e-9):
    """Distance to the nearest triangle hit along one ray (Moller-Trumbore), or None."""
    p = np.cross(direction, e2)
    det = np.einsum('ij,ij->i', e1, p)
    ok = np.abs(det) > eps
    inv = np.zeros_like(det)
    inv[ok] = 1.0 / det[ok]
    s = origin - v0
    u = np.einsum('ij,ij->i', s, p) * inv
    q = np.cross(s, e1)
    v = (q @ direction) * inv
    t = np.einsum('ij,ij->i', e2, q) * inv
    hit = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 1e-4)
    return float(t[hit].min()) if hit.any() else None


def wall_thickness(path, *, samples=256, threshold_mm=THIN_WALL_MM, seed=978) -> dict:
    """How thick the walls are, from rays cast inward at sampled faces.

    Returns {'measured': True, 'samples', 'min_mm', 'p10_mm', 'median_mm',
    'thin_fraction', 'threshold_mm'} or {'measured': False, 'reason'}.
    Sampling is area-weighted and seeded, so the same file gives the same
    numbers. Rays that leave an open mesh without a hit are skipped.
    """
    try:
        tris = load_triangles(path)
    except FixError as exc:
        return {'measured': False, 'reason': str(exc)}
    if not len(tris):
        return {'measured': False, 'reason': 'no triangles'}
    if len(tris) > MAX_WALL_TRIANGLES:
        return {'measured': False,
                'reason': f'{len(tris)} triangles is more than the {MAX_WALL_TRIANGLES} measured'}
    if signed_volume(tris) < 0:
        tris = tris[:, [0, 2, 1]]
    v0 = tris[:, 0]
    e1 = tris[:, 1] - v0
    e2 = tris[:, 2] - v0
    normals = np.cross(e1, e2)
    area2 = np.linalg.norm(normals, axis=1)
    usable = area2 > 0
    if not usable.any():
        return {'measured': False, 'reason': 'no faces with an area'}
    rng = np.random.default_rng(seed)
    weights = np.where(usable, area2, 0.0)
    picks = rng.choice(len(tris), size=min(samples, int(usable.sum())), replace=False,
                       p=weights / weights.sum())
    distances = []
    for f in picks.tolist():
        inward = -normals[f] / area2[f]
        origin = v0[f] + (e1[f] + e2[f]) / 3.0 + inward * 1e-3
        t = _first_hit(origin, inward, v0, e1, e2)
        if t is not None:
            distances.append(t + 1e-3)
    if not distances:
        return {'measured': False, 'reason': 'no inward ray met the other side of a wall'}
    d = np.array(distances)
    return {'measured': True, 'samples': int(len(d)),
            'min_mm': round(float(d.min()), 2),
            'p10_mm': round(float(np.percentile(d, 10)), 2),
            'median_mm': round(float(np.median(d)), 2),
            'thin_fraction': round(float((d < threshold_mm).mean()), 3),
            'threshold_mm': threshold_mm}


def wall_note(wall) -> str | None:
    """One chat line naming thin walls, or None when walls are fine or unmeasured."""
    if not wall or not wall.get('measured') or not wall.get('thin_fraction'):
        return None
    return (f"thin walls: {wall['thin_fraction'] * 100:.0f}% of sampled walls are under "
            f"{wall['threshold_mm']:g} mm (thinnest {wall['min_mm']:g} mm); thin walls may not "
            'print, and no automatic wall-thickening exists yet')
