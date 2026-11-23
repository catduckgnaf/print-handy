"""Whole-array twin of the placement report's per-point loops (#1790).

``orca_adapter.placement_candidates`` rotates every unique vertex of the model
into each of five orientations, takes the XY convex hull of each, and then
measures that hull at a few hundred bed yaws. On the 1.1 M triangle tongs case
(560 K unique points) the scalar loops cost about 15 s on the reporting host:
2.8 M ``_rotate`` calls, a monotone chain fed some 240 K points per orientation
(the octagon prune in ``_drop_interior`` keeps half the points of a long,
curved part), and per-yaw ``min``/``max`` over the hull.

This module does the same arithmetic on arrays and hands the scalar code only
what it needs:

- ``rotate``: each rotated component is ``m[0] * x + m[1] * y + m[2] * z``,
  evaluated left to right in float64 exactly as ``orca_adapter._rotate`` does,
  so every rotated point is bit-identical.
- ``hull``: ``orca_adapter._hull`` on a pruned point set. The prune drops only
  points strictly inside (by the same ``1e-9 * span**2`` margin
  ``_drop_interior`` uses) a convex polygon whose corners are input points.
  Such a point can never be a hull vertex, so the chain returns the same
  vertices in the same order. The octagon step is ``_drop_interior`` itself;
  finer polygons (more directions) then shed the band between the octagon and
  the hull, which leaves the chain a few thousand points.
- ``yaw_extents``: the hull's X and Y extent after each bed yaw, with the
  ``ym[0] * x + ym[1] * y`` arithmetic of the scalar loop.

The scalar code in ``orca_adapter`` stays the oracle and still runs for small
meshes; ``tests/pipeline/test_placement_numpy.py`` pins this module's output
to it, report row for report row.
"""
import math

import numpy as np

# Directions of the successive pruning polygons. The octagon (8) is
# orca_adapter._drop_interior; each later stage runs only on the survivors.
PRUNE_DIRECTIONS = (64, 512)
_CHUNK = 32768


def unique_rows(rows):
    """``np.unique(rows, axis=0)`` as float64, by a lexsort instead of a structured sort.

    For finite rows without a negative zero both give the distinct rows in
    (x, y, z) order, with equal rows equal bit for bit. numpy's structured sort
    is several times slower on a million rows. A negative zero equals a
    positive one, and which of the two ``np.unique`` keeps depends on its
    unstable sort, so such input (and non-finite input) goes to ``np.unique``.
    """
    rows = np.asarray(rows)
    if (len(rows) == 0 or not np.isfinite(rows).all()
            or bool(np.any((rows == 0) & np.signbit(rows)))):
        return np.ascontiguousarray(np.unique(rows, axis=0), dtype=np.float64)
    ordered = rows[np.lexsort((rows[:, 2], rows[:, 1], rows[:, 0]))]
    keep = np.ones(len(ordered), dtype=bool)
    keep[1:] = np.any(ordered[1:] != ordered[:-1], axis=1)
    return np.ascontiguousarray(ordered[keep], dtype=np.float64)


def points_array(points):
    """``points`` (a list of 3-tuples) as an (N, 3) float64 array with the same values and order."""
    return np.asarray(points, dtype=np.float64).reshape((-1, 3))


def rotate(points, m):
    """Rotate an (N, 3) array by the row-major matrix ``m``; bit-identical to ``_rotate`` per point."""
    x, y, z = points[:, 0], points[:, 1], points[:, 2]
    return np.stack((m[0] * x + m[1] * y + m[2] * z,
                     m[3] * x + m[4] * y + m[5] * z,
                     m[6] * x + m[7] * y + m[8] * z), axis=1)


def _corners(arr, directions):
    """Input points extreme in ``directions`` evenly spaced directions, in angular order, without repeats."""
    corners = []
    for k in range(directions):
        angle = math.radians(360.0 * k / directions)
        corner = arr[int(np.argmax(arr[:, 0] * math.cos(angle) + arr[:, 1] * math.sin(angle)))]
        if not corners or not np.array_equal(corners[-1], corner):
            corners.append(corner)
    while len(corners) > 1 and np.array_equal(corners[0], corners[-1]):
        corners.pop()
    return np.array(corners)


def _strictly_inside(arr, corners, tolerance):
    """Mask of points strictly inside the counterclockwise polygon ``corners`` by more than ``tolerance``."""
    ax, ay = corners[:, 0], corners[:, 1]
    rolled = np.roll(corners, -1, axis=0)
    dx, dy = rolled[:, 0] - ax, rolled[:, 1] - ay
    inside = np.empty(len(arr), dtype=bool)
    for start in range(0, len(arr), _CHUNK):
        block = arr[start:start + _CHUNK]
        edge = dx * (block[:, 1:2] - ay) - dy * (block[:, 0:1] - ax)
        inside[start:start + _CHUNK] = np.all(edge > tolerance, axis=1)
    return inside


def _prune(arr, span):
    """Survivors of the finer pruning polygons, in input order."""
    tolerance = 1e-9 * span * span
    for directions in PRUNE_DIRECTIONS:
        if len(arr) <= 4096:
            break
        corners = _corners(arr, directions)
        if len(corners) < 3:
            break
        inside = _strictly_inside(arr, corners, tolerance)
        keep = arr[~inside]
        # As in _drop_interior: trust the prune only when it removed points and
        # left a real polygon behind (corners may wind either way when degenerate).
        if not 3 <= len(keep) < len(arr):
            break
        arr = keep
    return arr


def hull(xy):
    """``orca_adapter._hull`` of the rows of the (N, 2) array ``xy``, in the scalar function's order."""
    from . import orca_adapter
    points = [tuple(row) for row in xy.tolist()] if len(xy) <= 4096 else None
    if points is not None:
        return orca_adapter._hull(points)
    # _drop_interior (the octagon) as _hull runs it, then the finer polygons.
    # When the octagon declines (it does on some long parts), the finer
    # polygons still run on every point.
    octagon = orca_adapter._drop_interior(xy)
    arr = xy if octagon is xy else np.asarray(octagon, dtype=np.float64).reshape((-1, 2))
    span = float(np.ptp(xy)) or 1.0
    arr = _prune(arr, span)
    return orca_adapter._hull([tuple(row) for row in arr.tolist()])


def orientation(points, m):
    """(XY hull, Z extent) of an (N, 3) point array rotated by ``m``: the scalar loop's values."""
    rotated = rotate(points, m)
    zs = rotated[:, 2]
    z_extent = float(zs.max()) - float(zs.min())
    return hull(rotated[:, :2]), z_extent


def yaw_extents(hull_points, yaws):
    """[(X extent, Y extent)] of ``hull_points`` turned by each yaw (degrees), as the scalar loop computes them."""
    from . import orca_adapter
    if not yaws:
        return []
    hx = np.array([p[0] for p in hull_points], dtype=np.float64)
    hy = np.array([p[1] for p in hull_points], dtype=np.float64)
    mats = [orca_adapter._yaw(yaw) for yaw in yaws]
    m0 = np.array([float(m[0]) for m in mats])[:, None]
    m1 = np.array([float(m[1]) for m in mats])[:, None]
    m3 = np.array([float(m[3]) for m in mats])[:, None]
    m4 = np.array([float(m[4]) for m in mats])[:, None]
    mx = m0 * hx + m1 * hy
    my = m3 * hx + m4 * hy
    w = mx.max(axis=1) - mx.min(axis=1)
    d = my.max(axis=1) - my.min(axis=1)
    return list(zip(w.tolist(), d.tolist(), strict=True))
