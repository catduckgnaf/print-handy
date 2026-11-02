"""Plan one-bed merging of every object in a multi-plate 3MF. Disk only.

The planner never slices and never contacts the printer. It answers one
question: can every source object share one AD5X bed with safe spacing, and
if not, why. A split is always reported with a reason.

When rectangle packing fails and at least one object is genuinely
non-rectangular, :func:`pack_polygon` retries with the objects' actual
convex hulls (translation only, authored yaw; see #221 P1). That fallback
is additive only: rectangle verdicts and placements never change, and a
polygon placement flows into the same per-object shifts the exporters
already realize.

When rectangle and polygon packing both fail, one last fallback packs the
hulls with per-object yaw (#221 R2b): an object that fits the usable bed
only when turned in the bed plane is packed by its hull rotated to the
yaw the shared per-object sweep records for it — the split path's
``yaw_by_object`` semantics — while objects that fit as authored keep
their authored hull. A turned object's shift is measured from its
rotated bounds, like the split path's shared-yaw shifts, and slicing
realizes each recorded turn through the exporters' per-object fit
channels composed with those shifts (see ``model_job._slice_merged``),
re-proving the landed placement before accepting the merged output.
"""
import math
import re
import zipfile
import xml.etree.ElementTree as ET

from .. import settings

_S = settings.current()

MAX_MERGED_S = _S['layout']['max_merged_hours'] * 3600


class SplitNeeded(RuntimeError):
    """A merged slice turned out unsuitable; carry the reason to the caller."""

EDGE_MARGIN_MM = _S['layout']['merge_edge_margin_mm']
SPACING_MM = _S['layout']['merge_spacing_mm']
TOWER_MM = _S['prime_tower']['size_mm']
TOWER_CLEARANCE_MM = _S['prime_tower']['part_clearance_mm']
TOWER_SLACK_MM = _S['prime_tower']['merge_slack_mm']
FILAMENT_SLOTS = _S['printer']['filament_slots']

SEPARATE_RE = re.compile(
    r"(print(?:ed)?\s+(?:\w+\s+){0,3}(?:separately|individually|one\s+(?:plate\s+)?at\s+a\s+time|one\s+by\s+one)"
    r"|(?:separate|individual)\s+(?:print|job|plate)s?"
    r"|do\s+not\s+(?:print|merge|combine)\s+(?:\w+\s+){0,3}together"
    r"|don'?t\s+(?:print|merge|combine)\s+(?:\w+\s+){0,3}together"
    r"|one\s+plate\s+at\s+a\s+time)", re.I)


def author_text(source, job=None):
    """Free text an author or the job may use to ask for separate printing."""
    chunks = []
    with zipfile.ZipFile(source) as archive:
        if '3D/3dmodel.model' in archive.namelist():
            root = ET.fromstring(archive.read('3D/3dmodel.model'))
            for node in root.iter():
                if node.tag.split('}')[-1] == 'metadata' and node.get('name') in (
                        'Description', 'Title', 'Designer', 'CopyRight', 'License', 'Notes', 'Summary'):
                    chunks.append(node.text or '')
    for key in ('description', 'notes', 'summary'):
        if isinstance((job or {}).get(key), str):
            chunks.append(job[key])
    return '\n'.join(chunks)


def separate_hint(source, job=None):
    match = SEPARATE_RE.search(author_text(source, job))
    return match.group(0) if match else None


def pack(extents, bed, spacing=None, edge=None, reserve_y=0.0):
    """Shelf packing of axis-aligned (w, d) footprints. Orientation is kept.

    Returns {key: (x, y)} lower-left corners, or None when it does not fit.
    Both row directions are tried; the first that fits wins. reserve_y keeps a
    strip along the high Y edge free (prime tower).
    """
    spacing = SPACING_MM if spacing is None else spacing
    edge = EDGE_MARGIN_MM if edge is None else edge
    width = bed - 2 * edge
    depth = bed - 2 * edge - reserve_y
    if not extents:
        return {}
    for swap in (False, True):
        items = sorted(extents.items(), key=lambda kv: -(kv[1][0] if swap else kv[1][1]))
        placed, x, y, row = {}, 0.0, 0.0, 0.0
        ok = True
        for key, (w, d) in items:
            a, b = (d, w) if swap else (w, d)       # a along the row, b across rows
            lim_a, lim_b = (depth, width) if swap else (width, depth)
            if a > lim_a + 1e-9 or b > lim_b + 1e-9:
                ok = False
                break
            if x + a > lim_a + 1e-9:
                x, y, row = 0.0, y + row + spacing, 0.0
            if y + b > lim_b + 1e-9:
                ok = False
                break
            px, py = (y, x) if swap else (x, y)
            placed[key] = (edge + px, edge + py)
            x += a + spacing
            row = max(row, b)
        if ok:
            return placed
    return None


# Node budget for one polygon-pack search (#221 P1). Exhaustion returns None
# — exactly today's rectangle verdict — rather than stalling intake.
POLYGON_MAX_NODES = 4000


def _polygon_area(points):
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2)
                   in zip(points, points[1:] + points[:1]))) / 2.0


def _non_rectangular(hull, width, depth):
    """True when the hull is genuinely smaller than its AABB footprint."""
    aabb = width * depth
    return aabb - _polygon_area(hull) > 1e-6 * max(1.0, aabb)


def _seg_distance(p1, p2, p3, p4):
    def point_seg(p, a, b):
        vx, vy = b[0] - a[0], b[1] - a[1]
        wx, wy = p[0] - a[0], p[1] - a[1]
        denom = vx * vx + vy * vy
        t = 0.0 if denom == 0 else max(0.0, min(1.0, (wx * vx + wy * vy) / denom))
        return math.hypot(p[0] - (a[0] + t * vx), p[1] - (a[1] + t * vy))

    def orient(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def on_seg(a, p, b):
        return (min(a[0], b[0]) - 1e-12 <= p[0] <= max(a[0], b[0]) + 1e-12
                and min(a[1], b[1]) - 1e-12 <= p[1] <= max(a[1], b[1]) + 1e-12)

    d1, d2 = orient(p3, p4, p1), orient(p3, p4, p2)
    d3, d4 = orient(p1, p2, p3), orient(p1, p2, p4)
    if ((d1 > 0) != (d2 > 0) and d1 and d2 and (d3 > 0) != (d4 > 0) and d3 and d4) \
            or (d1 == 0 and on_seg(p3, p1, p4)) or (d2 == 0 and on_seg(p3, p2, p4)) \
            or (d3 == 0 and on_seg(p1, p3, p2)) or (d4 == 0 and on_seg(p1, p4, p2)):
        return 0.0
    return min(point_seg(p1, p3, p4), point_seg(p2, p3, p4),
               point_seg(p3, p1, p2), point_seg(p4, p1, p2))


def _polygons_intersect(a, b):
    """Separating-axis test for two convex polygons (touching counts)."""
    for poly in (a, b):
        for i in range(len(poly)):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % len(poly)]
            nx, ny = -(y2 - y1), x2 - x1
            if nx == 0 and ny == 0:
                continue
            proj_a = [x * nx + y * ny for x, y in a]
            proj_b = [x * nx + y * ny for x, y in b]
            if max(proj_a) < min(proj_b) - 1e-12 or max(proj_b) < min(proj_a) - 1e-12:
                return False
    return True


def _polygons_overlap(a, b):
    """True when two convex polygons overlap with positive area (touching is not)."""
    for poly in (a, b):
        for i in range(len(poly)):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % len(poly)]
            nx, ny = -(y2 - y1), x2 - x1
            if nx == 0 and ny == 0:
                continue
            proj_a = [x * nx + y * ny for x, y in a]
            proj_b = [x * nx + y * ny for x, y in b]
            if max(proj_a) <= min(proj_b) + 1e-12 or max(proj_b) <= min(proj_a) + 1e-12:
                return False
    return True


def hull_distance(a, b):
    """Exact minimum distance between two convex polygons (0 if they touch)."""
    if _polygons_intersect(a, b):
        return 0.0
    return min(_seg_distance(a[i], a[(i + 1) % len(a)], b[j], b[(j + 1) % len(b)])
               for i in range(len(a)) for j in range(len(b)))


# Dense mesh hulls (hundreds of vertices) made the polygon packer's exact
# distance checks O(n*m) per pair and took minutes (a 9-part toy model: 400 s).
# Above this many vertices a hull is packed by an outer K-DOP: the
# intersection of its supporting half-planes at OUTER_DOP_K evenly spaced
# directions. It contains the hull (so any clearance it proves holds for the
# hull too), keeps the hull's exact AABB (axis directions are included, so
# corners and shifts are unchanged), and is at most 1/cos(pi/K) - 1 (0.5% for
# K = 32) larger across any width.
OUTER_DOP_VERTICES = 48
OUTER_DOP_K = 32


def _outer_polygon(points, k=OUTER_DOP_K):
    """Convex polygon containing ``points``, bounded by k supporting lines."""
    dirs = [(math.cos(2 * math.pi * i / k), math.sin(2 * math.pi * i / k)) for i in range(k)]
    support = [max(x * ux + y * uy for x, y in points) for ux, uy in dirs]
    out = []
    for i in range(k):
        (ax, ay), ha = dirs[i], support[i]
        (bx, by), hb = dirs[(i + 1) % k], support[(i + 1) % k]
        det = ax * by - ay * bx
        out.append(((ha * by - hb * ay) / det, (ax * hb - bx * ha) / det))
    # Snap the axis-aligned extremes exactly onto the points' AABB.
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    lo_x, hi_x, lo_y, hi_y = min(xs), max(xs), min(ys), max(ys)
    return [(min(max(x, lo_x), hi_x), min(max(y, lo_y), hi_y)) for x, y in out]


def _aabb_gap(a, b):
    """Euclidean gap between two point sets' AABBs: a lower bound on their distance."""
    ax0, ax1 = min(p[0] for p in a), max(p[0] for p in a)
    ay0, ay1 = min(p[1] for p in a), max(p[1] for p in a)
    bx0, bx1 = min(p[0] for p in b), max(p[0] for p in b)
    by0, by1 = min(p[1] for p in b), max(p[1] for p in b)
    dx = max(0.0, bx0 - ax1, ax0 - bx1)
    dy = max(0.0, by0 - ay1, ay0 - by1)
    return math.hypot(dx, dy)


def _closer_than(a, b, limit):
    """True when convex polygons ``a`` and ``b`` touch, overlap or are nearer than ``limit``.

    Same verdict as ``hull_distance(a, b) < limit`` (limit > 0), computed
    with pruning: separated convex polygons are nearest at a vertex of one
    and an edge of the other, and only vertices inside the other polygon's
    AABB grown by ``limit`` (against edges whose grown AABB holds them) can
    be nearer than ``limit``.
    """
    def box(poly):
        return (min(p[0] for p in poly), max(p[0] for p in poly),
                min(p[1] for p in poly), max(p[1] for p in poly))

    ba, bb = box(a), box(b)
    boxes_touch = not (ba[1] < bb[0] or bb[1] < ba[0] or ba[3] < bb[2] or bb[3] < ba[2])
    if boxes_touch and _polygons_intersect(a, b):
        return True

    def near(points, poly, pbox):
        x0, x1, y0, y1 = pbox[0] - limit, pbox[1] + limit, pbox[2] - limit, pbox[3] + limit
        edges = None
        for px, py in points:
            if not (x0 <= px <= x1 and y0 <= py <= y1):
                continue
            if edges is None:
                edges = [(poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly))]
            for (ax, ay), (bx, by) in edges:
                if (px < min(ax, bx) - limit or px > max(ax, bx) + limit
                        or py < min(ay, by) - limit or py > max(ay, by) + limit):
                    continue
                vx, vy = bx - ax, by - ay
                denom = vx * vx + vy * vy
                t = 0.0 if denom == 0 else max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / denom))
                if math.hypot(px - (ax + t * vx), py - (ay + t * vy)) < limit:
                    return True
        return False

    return near(a, b, bb) or near(b, a, ba)


def _rect_polygon(rect):
    x0, y0, x1, y1 = rect
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def pack_polygon(hulls, bed, spacing=None, edge=None, reserve_y=0.0,
                 exclusions=(), feature=0.0, max_nodes=POLYGON_MAX_NODES):
    """Pack convex hulls onto one bed by translation only, at authored yaw.

    ``hulls`` maps key -> [(x, y), ...] convex hull in any absolute position;
    each hull is normalized to its own AABB minimum internally. Returns
    {key: (x, y)} AABB lower-left corners in bed coordinates — the same shape
    :func:`pack` returns, so callers convert corners to shifts identically —
    or None when no valid placement is found within the search budget.

    Clearances are measured between the hulls themselves, exactly once:
    hull-to-hull >= ``spacing + 2 * feature``, hull-to-bed-edge (and to the
    ``reserve_y`` strip) >= ``edge + feature``, hull-to-exclusion >=
    ``feature``. ``feature`` is a brim/feature budget in the #226 sense; with
    the default 0 every threshold reduces to the plain spacing/edge meaning.
    The search is a deterministic backtracking first-fit over a bounded
    candidate set (usable corners, vertex-to-vertex alignments and
    edge-normal offsets from already-placed hulls and exclusion edges);
    every returned placement is distance-validated exactly, so an incomplete
    candidate set can only cause a conservative miss (None), never an
    invalid placement. Degenerate hulls (< 3 points) are packed by their
    AABB rectangle.
    """
    spacing = SPACING_MM if spacing is None else spacing
    edge = EDGE_MARGIN_MM if edge is None else edge
    if not hulls:
        return {}
    feature = float(feature)
    clearance = float(spacing) + 2 * feature
    edge_clear = float(edge) + feature
    lo = edge_clear
    hi_x, hi_y = float(bed) - edge_clear, float(bed) - edge_clear - float(reserve_y)
    shapes = {}
    for key, hull in hulls.items():
        pts = [(float(x), float(y)) for x, y in hull]
        if len(pts) < 3:
            xs = [p[0] for p in pts] or [0.0]
            ys = [p[1] for p in pts] or [0.0]
            pts = _rect_polygon((min(xs), min(ys), max(xs), max(ys)))
        elif len(pts) > OUTER_DOP_VERTICES:
            pts = _outer_polygon(pts)
        min_x, min_y = min(p[0] for p in pts), min(p[1] for p in pts)
        local = [(x - min_x, y - min_y) for x, y in pts]
        shapes[key] = {'local': local,
                       'w': max(p[0] for p in local), 'd': max(p[1] for p in local),
                       'area': _polygon_area(local)}
    order = sorted(shapes, key=lambda k: (-shapes[k]['area'], str(k)))
    exclusion_polys = [_rect_polygon(tuple(float(v) for v in rect)) for rect in exclusions]
    nodes = [0]

    def valid(local, corner):
        placed = [(x + corner[0], y + corner[1]) for x, y in local]
        if any(x < lo - 1e-9 or x > hi_x + 1e-9 or y < lo - 1e-9 or y > hi_y + 1e-9
               for x, y in placed):
            return None
        return placed

    def candidates(shape, placed):
        w, d = shape['w'], shape['d']
        found = {(lo, lo), (lo, hi_y - d), (hi_x - w, lo), (hi_x - w, hi_y - d)}
        offsets = ((0.0, 0.0), (clearance, 0.0), (-clearance, 0.0),
                   (0.0, clearance), (0.0, -clearance))
        for other, other_corner in placed:
            for px, py in other:
                for vx, vy in shape['local']:
                    base = (px - vx, py - vy)
                    for dx, dy in offsets:
                        found.add((base[0] + dx, base[1] + dy))
            centroid = (sum(p[0] for p in other) / len(other),
                        sum(p[1] for p in other) / len(other))
            for i in range(len(other)):
                ax, ay = other[i]
                bx, by = other[(i + 1) % len(other)]
                ex, ey = bx - ax, by - ay
                length = math.hypot(ex, ey)
                if length == 0:
                    continue
                nx, ny = -ey / length, ex / length
                if (centroid[0] - (ax + bx) / 2) * nx + (centroid[1] - (ay + by) / 2) * ny > 0:
                    nx, ny = -nx, -ny
                for anchor in (other[i], other[(i + 1) % len(other)]):
                    for vx, vy in shape['local']:
                        found.add((anchor[0] + nx * clearance - vx,
                                   anchor[1] + ny * clearance - vy))
        for rect in exclusion_polys:
            xs = [p[0] for p in rect]
            ys = [p[1] for p in rect]
            for cx in (max(xs) + feature, min(xs) - w - feature, lo, hi_x - w):
                for cy in (max(ys) + feature, min(ys) - d - feature, lo, hi_y - d):
                    found.add((cx, cy))
        return sorted((c for c in found
                       if lo - 1e-9 <= c[0] <= hi_x - w + 1e-9
                       and lo - 1e-9 <= c[1] <= hi_y - d + 1e-9),
                      key=lambda c: (round(c[1], 9), round(c[0], 9)))

    def search(index, placed):
        if index == len(order):
            return {}
        if nodes[0] > max_nodes:
            return None
        key = order[index]
        shape = shapes[key]
        for corner in candidates(shape, placed):
            nodes[0] += 1
            if nodes[0] > max_nodes:
                return None
            poly = valid(shape['local'], corner)
            if poly is None:
                continue
            # The AABB gap is a lower bound on the hull distance: a pair whose
            # boxes already clear needs no exact check (same verdict, faster).
            if any(_aabb_gap(poly, other) < clearance - 1e-9
                   and (_polygons_overlap(poly, other)
                        or (_closer_than(poly, other, clearance - 1e-9) if clearance > 1e-9
                            else hull_distance(poly, other) < clearance - 1e-9))
                   for other, _c in placed):
                continue
            if any(_polygons_overlap(poly, rect)
                   or hull_distance(poly, rect) < feature - 1e-9 for rect in exclusion_polys):
                continue
            rest = search(index + 1, placed + [(poly, corner)])
            if rest is not None:
                return {key: corner, **rest}
        return None

    return search(0, [])


def _object_hulls(source, object_ids):
    """Authored-orientation hull per object for the polygon fallback, or None.

    Eligible only when every object is a single build item — a hull per
    object id is not a faithful footprint for grouped or multi-item objects
    (painted sources, cutter carriers) — and at least one hull is genuinely
    non-rectangular. Rectangle-only sources return None here, so their
    verdicts stay today's by construction, not just by test.
    """
    from . import color_geometry
    hulls = {}
    non_rectangular = False
    try:
        with zipfile.ZipFile(source) as archive:
            for oid in object_ids:
                items = color_geometry._world_hull_xy(archive, {oid})
                if len(items) != 1:
                    return None
                hull, bounds = items[0]
                if _non_rectangular(hull, bounds[0][1] - bounds[0][0],
                                     bounds[1][1] - bounds[1][0]):
                    non_rectangular = True
                hulls[oid] = hull
    except RuntimeError:
        return None
    return hulls if non_rectangular else None


def _object_hulls_all(source, object_ids):
    """Authored hull per object for the yawed fallback, or None.

    The same single-build-item eligibility as :func:`_object_hulls` — a
    hull per object id is not a faithful footprint for grouped or
    multi-item objects — but without the non-rectangular gate: a turned
    rectangle is the canonical yawed-merge case, so rectangle hulls are
    kept. Any ineligible object makes the whole attempt unavailable,
    because the merged bed is one packing problem over every object.
    """
    from . import color_geometry
    hulls = {}
    try:
        with zipfile.ZipFile(source) as archive:
            for oid in object_ids:
                items = color_geometry._world_hull_xy(archive, {oid})
                if len(items) != 1:
                    return None
                hulls[oid] = items[0][0]
    except RuntimeError:
        return None
    return hulls


def _solo_yaw(hull, usable):
    """The yaw one object's hull needs to fit the usable bed area, or None.

    0.0 when the authored envelope already fits — authored orientation is
    always preferred, exactly as on the split path. Otherwise the first
    fitting candidate of the shared 1-degree sweep
    (``split_fit.mesh_yaw_candidates``: best worst-ratio order, smallest
    angle on ties), which is the choice ``split_fit.per_part_fit`` records
    for a part that fits only turned. None when no checked yaw fits: such
    an object cannot share the merged bed turned either, because any
    packing must still fit its rotated envelope inside the same usable
    area. The sweep is numerical evidence, not a continuous-angle proof.
    """
    from .split_fit import mesh_yaw_candidates
    xs = [p[0] for p in hull]
    ys = [p[1] for p in hull]
    if max(xs) - min(xs) <= usable + 1e-9 and max(ys) - min(ys) <= usable + 1e-9:
        return 0.0
    for _ratio, degrees, envelope in mesh_yaw_candidates(hull, usable, usable):
        if degrees > 0 and envelope[0] <= usable + 1e-9 and envelope[1] <= usable + 1e-9:
            return float(degrees)
    return None


def _yawed_merge_attempt(source, object_ids, extents, bed, spacing, reserve, edge=None, tower_mm=None):
    """Proved one-bed packing with solo-needing objects turned, or None.

    Returns (corners, tower_xy, yaw_by_object, trial_hulls): corners in
    the :func:`pack_polygon` shape (placed hull AABB lower-lefts),
    ``yaw_by_object`` naming exactly the turned objects, and the hulls as
    packed (rotated for turned objects) so the caller measures their
    shifts and placed bounds from the rotated bounds. The tower rules are
    the authored polygon fallback's: the reserved strip is tried first,
    and a placement that wins only by using it needs the same
    ``prime_tower_xy`` proof on the placed AABBs (rotated AABBs for
    turned objects — conservative supersets of the rotated hulls).
    """
    from . import color_geometry
    from .split_fit import _rotate_hull_xy
    hulls = _object_hulls_all(source, object_ids)
    if hulls is None:
        return None
    edge = EDGE_MARGIN_MM if edge is None else edge
    usable = float(bed) - 2 * edge
    yaw_by_object, trial = {}, {}
    for oid in object_ids:
        degrees = _solo_yaw(hulls[oid], usable)
        if degrees is None:
            return None
        if degrees > 0:
            yaw_by_object[oid] = degrees
            trial[oid] = _rotate_hull_xy(hulls[oid], degrees)
        else:
            trial[oid] = hulls[oid]
    if not yaw_by_object:
        # Nothing needs a turn: the authored verdicts above stand.
        return None
    placed = pack_polygon(trial, bed, spacing, edge=edge, reserve_y=reserve)
    if placed is not None:
        return placed, None, yaw_by_object, trial
    if reserve:
        loose = pack_polygon(trial, bed, spacing, edge=edge)
        if loose is not None:
            loose_bounds = []
            for oid, (px, py) in loose.items():
                if oid in yaw_by_object:
                    hull = trial[oid]
                    extent = (max(p[0] for p in hull) - min(p[0] for p in hull),
                              max(p[1] for p in hull) - min(p[1] for p in hull))
                else:
                    extent = extents[oid]
                loose_bounds.append(((px, px + extent[0]), (py, py + extent[1]), (0.0, 0.0)))
            try:
                tower_xy = color_geometry.prime_tower_xy(loose_bounds, bed=bed, tower=tower_mm)
            except RuntimeError:
                tower_xy = None
            else:
                return loose, tower_xy, yaw_by_object, trial
    return None


# Node budget for one turned-rectangle search. Exhaustion is a "no" (split
# stays the verdict), never a stall.
TURNED_MAX_NODES = 20000


def rearrange_requested(job):
    """True when the operator (or intake's auto-apply) chose a 100% rearrangement."""
    return bool(isinstance(job, dict) and job.get('full_size_rearrange_plan'))


def pack_turned(footprints, width, depth, spacing=None, edge=None,
                max_nodes=TURNED_MAX_NODES):
    """Pack (w, d) rectangles with each one either as authored or turned 90 degrees.

    ``footprints`` maps key -> (w, d). The usable area is
    ``width - 2 * edge`` by ``depth - 2 * edge`` starting at (edge, edge);
    rectangles keep ``spacing`` between each other. Returns
    {key: (x, y, turned)} lower-left corners, or None when no placement is
    found within the search budget. Deterministic: largest first, authored
    orientation before the turn, bottom-left candidate corners.
    """
    spacing = SPACING_MM if spacing is None else spacing
    edge = EDGE_MARGIN_MM if edge is None else edge
    if not footprints:
        return {}
    keys = sorted(footprints, key=lambda k: (-(footprints[k][0] * footprints[k][1]), str(k)))
    hi_x, hi_y = width - edge, depth - edge
    options = {}
    for key in keys:
        w, d = (float(v) for v in footprints[key])
        variants = [(w, d, False)] + ([(d, w, True)] if abs(w - d) > 1e-6 else [])
        variants = [v for v in variants if edge + v[0] <= hi_x + 1e-7 and edge + v[1] <= hi_y + 1e-7]
        if not variants:
            return None
        options[key] = variants
    placed, nodes = [], 0

    def clash(x, y, w, d):
        for _k, px, py, pw, pd, _t in placed:
            if not (x + w + spacing <= px + 1e-7 or px + pw + spacing <= x + 1e-7
                    or y + d + spacing <= py + 1e-7 or py + pd + spacing <= y + 1e-7):
                return True
        return False

    def search(i):
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes:
            return False
        if i == len(keys):
            return True
        xs = sorted({edge} | {p[1] + p[3] + spacing for p in placed})
        ys = sorted({edge} | {p[2] + p[4] + spacing for p in placed})
        for w, d, turned in options[keys[i]]:
            for y in ys:
                for x in xs:
                    if x + w > hi_x + 1e-7 or y + d > hi_y + 1e-7 or clash(x, y, w, d):
                        continue
                    placed.append((keys[i], x, y, w, d, turned))
                    if search(i + 1):
                        return True
                    placed.pop()
        return False

    if not search(0):
        return None
    return {key: (x, y, turned) for key, x, y, _w, _d, turned in placed}


def _turned_attempt(hulls, bed, spacing, reserve, edge=None, tower_mm=None):
    """One-bed placement where any object may turn 90 degrees, or None.

    Returns (corners, tower_xy, yaw_by_object, trial_hulls) in the
    :func:`_yawed_merge_attempt` shape. Footprints are the hulls' bounding
    rectangles (a conservative superset of each hull). The reserved tower
    strip is tried first; a placement that needs the whole bed must also
    leave a ``prime_tower_xy`` spot clear of every placed rectangle.
    """
    from . import color_geometry
    from .split_fit import _rotate_hull_xy
    footprints = {oid: (max(p[0] for p in h) - min(p[0] for p in h), max(p[1] for p in h) - min(p[1] for p in h))
                  for oid, h in hulls.items()}
    tower_key = object()
    # The tower packed as one more rectangle, grown so any spot the packer
    # gives it still clears parts by the tower clearance and the bed edge by
    # the tower edge margin, plus one 5 mm step of the tower search grid.
    edge = EDGE_MARGIN_MM if edge is None else edge
    grow = max(0.0, TOWER_CLEARANCE_MM - spacing, float(color_geometry.TOWER_EDGE_MM) - edge)
    tower_side = (TOWER_MM if tower_mm is None else tower_mm) + 2 * grow + 5.0
    # Reserved strip first (the planner's usual fast path), then the tower as
    # a packed item, then the whole bed with a tower spot searched afterwards.
    attempts = (((bed - reserve, False, False), (bed, True, True), (bed, True, False)) if reserve
                else ((bed, False, False),))
    for depth, needs_tower, tower_item in attempts:
        items = {**footprints, tower_key: (tower_side, tower_side)} if tower_item else footprints
        placed = pack_turned(items, bed, depth, spacing, edge=edge)
        if placed is None:
            continue
        placed.pop(tower_key, None)
        tower_xy = None
        if needs_tower:
            bounds = []
            for oid, (x, y, turned) in placed.items():
                w, d = footprints[oid][::-1] if turned else footprints[oid]
                bounds.append(((x, x + w), (y, y + d), (0.0, 0.0)))
            try:
                tower_xy = color_geometry.prime_tower_xy(bounds, bed=bed, tower=tower_mm)
            except RuntimeError:
                continue
        yaw_by_object = {oid: 90.0 for oid, (_x, _y, turned) in placed.items() if turned}
        trial = {oid: (_rotate_hull_xy(hulls[oid], 90.0) if oid in yaw_by_object else hulls[oid]) for oid in hulls}
        corners = {oid: (x, y) for oid, (x, y, _t) in placed.items()}
        return corners, tower_xy, yaw_by_object, trial
    return None


def _shifts(placed, boxes, extents, yaw_by_object, yaw_hulls):
    """Per-object shifts and placed bounds from packed lower-left corners."""
    shifts, bounds = {}, {}
    for oid, (px, py) in placed.items():
        box = boxes[oid]
        if oid in yaw_by_object:
            hull = yaw_hulls[oid]
            base = (min(p[0] for p in hull), min(p[1] for p in hull))
            extent = (max(p[0] for p in hull) - base[0], max(p[1] for p in hull) - base[1])
        else:
            base = (box[0][0], box[1][0])
            extent = extents[oid]
        shifts[oid] = [px - base[0], py - base[1], -box[2][0]]
        bounds[oid] = [(px, px + extent[0]), (py, py + extent[1]),
                       (0.0, box[2][1] - box[2][0])]
    return shifts, bounds


def arrange_group(source, object_ids, bed, spacing=None, edge=None):
    """Full-size placement of one single-color plate's objects, turning any by 90 degrees.

    Used when a rearranged per-part job splits by slot: each slot plate is
    packed on its own bed instead of keeping the author's layout (which is
    what overflowed the bed in the first place). Returns
    {'shifts', 'yaw_by_object', 'placed_bounds_mm'} or None when the group
    does not fit one bed at 100%.
    """
    spacing = SPACING_MM if spacing is None else spacing
    from . import color_geometry
    object_ids = sorted({str(o) for o in object_ids}, key=lambda o: (len(o), o))
    hulls = _object_hulls_all(source, object_ids)
    if hulls is None:
        return None
    with zipfile.ZipFile(source) as archive:
        boxes = {oid: color_geometry._world_bounds(archive, {oid})[0] for oid in object_ids}
    extents = {oid: (box[0][1] - box[0][0], box[1][1] - box[1][0]) for oid, box in boxes.items()}
    attempt = _turned_attempt(hulls, float(bed), spacing, 0.0, edge=edge)
    if attempt is None:
        return None
    placed, _tower, yaw_by_object, trial = attempt
    shifts, bounds = _shifts(placed, boxes, extents, yaw_by_object, trial)
    return {'shifts': shifts, 'yaw_by_object': yaw_by_object, 'placed_bounds_mm': bounds}


def full_size_split_possible(source, manifest, bed=None, edge=None, brim_policy=None, state=None):
    """Whether source-assigned plate groups fit individually at authored size.

    This is an offline geometry check for the explicit full-size split option.
    It does not imply Orca acceptance; every derived plate is still sliced and
    validated by the normal offline G-code gates.

    ``brim_policy``/``state`` carry the job's brim the way split_fit's check
    takes them (P2-79): when a policy applies, the feature edge budget (the
    brim width plus the resolved travel sweep) is added to the edge margin,
    so a split approved here is one whose brims stay on the bed — the plain
    edge-only verdict offered splits the brim-aware check had refused.
    """
    edge = EDGE_MARGIN_MM if edge is None else edge
    if brim_policy is not None or (state and state.get('brim_policy')):
        from .orca_adapter import required_feature_edge
        edge = float(edge) + float(required_feature_edge(state or {}, brim_policy))
    from . import color_geometry
    bed = min(_S['printer']['bed_mm']) if bed is None else bed
    try:
        with zipfile.ZipFile(source) as archive:
            settings = ET.fromstring(archive.read('Metadata/model_settings.config'))
            groups = []
            for plate in settings.iter('plate'):
                ids = [m.get('value') for instance in plate.findall('model_instance')
                       for m in instance.findall('metadata') if m.get('key') == 'object_id']
                if not ids:
                    return False
                groups.append(ids)
            if len(groups) < 2:
                return False
            for ids in groups:
                boxes = color_geometry._world_bounds(archive, set(ids))
                bounds = [(min(box[a][0] for box in boxes), max(box[a][1] for box in boxes))
                          for a in range(3)]
                usable = float(bed) - 2 * float(edge)
                if (max(bounds[a][1] - bounds[a][0] for a in (0, 1)) > usable
                        or bounds[2][1] - bounds[2][0] > _S['printer']['max_print_height_mm']):
                    return False
    except (RuntimeError, KeyError, zipfile.BadZipFile, ET.ParseError, ValueError):
        return False
    return True


def plan(source, manifest, plates, slots, state, bed=None, spacing=None, job=None, edge=None,
         capacity=None, multi_tool=False):
    """Return a merge plan or a split decision with a human reason.

    manifest: source_colors.inventory(source); plates: model_job.plate_manifest(source)['plates'];
    slots: {part_key: slot}. Result keys: mode ('merge'|'split'), reason, shifts, ...;
    a merge proved only with turned objects also carries yaw_by_object (#221 R2b).
    ``edge`` is the bed-edge margin parts keep (default the merge edge
    margin); callers add the job's brim so a brim never crosses the edge.
    ``capacity`` is the number of distinct filament positions the JOB's
    printer offers, as its adapter counts them; the module-level
    FILAMENT_SLOTS is only the configured printer's count, so a caller
    slicing for a multi_tool model (whose filament_slots stays 1 by design)
    must pass the job printer's count or every multi-toolhead merge is
    refused in a process configured for that model. With ``multi_tool``
    the positions are toolheads: each carries its own filament and heater,
    so mixed materials across positions are allowed and a capacity refusal
    names toolheads instead of IFS slots.
    """
    spacing = SPACING_MM if spacing is None else spacing
    edge = EDGE_MARGIN_MM if edge is None else edge
    from . import color_geometry
    # Reserve and check room for the tower the slice will actually emit:
    # the configured size raised to the fitted nozzle's tuned width (P2-34).
    tower_mm = color_geometry.effective_tower_mm((state.get('nozzle') or {}).get('diameter_mm'))
    bed = int(bed or state['bed_mm'][0])
    out = {'mode': 'split', 'reason': None, 'plates_in': len(plates),
           'objects_in': sorted({p['object_id'] for p in manifest['parts']}, key=str),
           'parts_in': sorted(p['key'] for p in manifest['parts']), 'bed_mm': bed, 'spacing_mm': spacing}
    hint = separate_hint(source, job)
    if hint:
        out['reason'] = f'author notes ask for separate printing ("{hint}")'
        return out
    used = sorted(set(slots.values()))
    limit = FILAMENT_SLOTS if capacity is None else int(capacity)
    if len(used) > limit:
        out['reason'] = (f'needs {len(used)} toolheads; this printer has {limit}' if multi_tool
                         else f'needs {len(used)} filament slots; one IFS job has {limit}')
        return out
    materials = {(state.get('filament_slots') or {}).get(str(s), {}).get('material') for s in used}
    if not multi_tool and len(used) > 1 and len(materials) > 1:
        out['reason'] = 'mixed materials across slots cannot share one IFS job'
        return out
    object_ids = sorted({p['object_id'] for p in manifest['parts']}, key=int)
    try:
        with zipfile.ZipFile(source) as archive:
            boxes = {oid: color_geometry._world_bounds(archive, {oid})[0] for oid in object_ids}
    except RuntimeError as exc:
        # The merge exporter cannot place this geometry; say why instead of failing the slice.
        out['reason'] = f'the merge exporter cannot place this geometry ({exc})'
        return out
    extents, tall = {}, []
    for oid, box in boxes.items():
        extents[oid] = (box[0][1] - box[0][0], box[1][1] - box[1][0])
        if box[2][1] - box[2][0] > float(state['max_print_height_mm']):
            tall.append(oid)
    if tall:
        out['reason'] = f'object(s) {",".join(tall)} exceed the printer height'
        return out
    reserve = (tower_mm + TOWER_CLEARANCE_MM + TOWER_SLACK_MM + edge) if len(used) > 1 else 0.0
    placed = pack(extents, bed, spacing, edge=edge, reserve_y=reserve)
    tower_xy = None
    packing = 'rectangle'
    if placed is None and reserve:
        # The reserved strip is a fast path, not the rule. Pack without it and
        # accept only when the exporter's own tower search clears every part.
        loose = pack(extents, bed, spacing, edge=edge)
        if loose is not None:
            loose_bounds = [((px, px + extents[oid][0]), (py, py + extents[oid][1]), (0.0, 0.0))
                            for oid, (px, py) in loose.items()]
            try:
                tower_xy = color_geometry.prime_tower_xy(loose_bounds, bed=bed, tower=tower_mm)
            except RuntimeError:
                tower_xy = None
            else:
                placed = loose
    yaw_by_object, yaw_hulls = {}, None
    if placed is None and rearrange_requested(job):
        # A chosen 100% rearrangement, tried before the (much slower) hull
        # searches below: any object may turn 90 degrees in the bed plane,
        # not only the ones that need a turn to fit alone. Same
        # spacing, edge and tower rules; the turns flow into the same
        # yaw_by_object + shifts channel as R2b, so slicing re-proves them.
        hulls_all = _object_hulls_all(source, object_ids)
        attempt = _turned_attempt(hulls_all, float(bed), spacing, reserve, edge=edge, tower_mm=tower_mm) if hulls_all else None
        if attempt is not None:
            placed, tower_xy, yaw_by_object, yaw_hulls = attempt
            packing = 'turned'
    if placed is None:
        # Polygon fallback (#221 P1): rectangle packing failed and at least
        # one object is genuinely non-rectangular, so judge the actual hulls.
        # Translation only, authored yaw; the positions flow into the same
        # realizable shifts as rectangle placements. The tower rules are
        # unchanged: the reserved strip is tried first, and a placement that
        # wins only by using it needs the same prime_tower_xy proof on the
        # placed hull AABBs (conservative supersets of the hulls).
        hulls = _object_hulls(source, object_ids)
        if hulls is not None:
            placed = pack_polygon(hulls, bed, spacing, edge=edge, reserve_y=reserve)
            if placed is not None:
                packing = 'polygon'
            elif reserve:
                loose = pack_polygon(hulls, bed, spacing, edge=edge)
                if loose is not None:
                    loose_bounds = [((px, px + extents[oid][0]), (py, py + extents[oid][1]), (0.0, 0.0))
                                    for oid, (px, py) in loose.items()]
                    try:
                        tower_xy = color_geometry.prime_tower_xy(loose_bounds, bed=bed, tower=tower_mm)
                    except RuntimeError:
                        tower_xy = None
                    else:
                        placed, packing = loose, 'polygon'
    if placed is None:
        # Yawed merge fallback (#221 R2b): rectangle and authored hull
        # packing both failed. Pack the hulls with each solo-needing
        # object rotated to its recorded yaw (see _yawed_merge_attempt);
        # the proved corners flow into the same realizable shifts, with a
        # turned object's shift measured from its rotated bounds.
        attempt = _yawed_merge_attempt(source, object_ids, extents, bed, spacing, reserve, edge=edge, tower_mm=tower_mm)
        if attempt is not None:
            placed, tower_xy, yaw_by_object, yaw_hulls = attempt
            packing = 'polygon'
    if placed is None:
        out['reason'] = (f'{len(extents)} parts do not fit one {bed}x{bed} mm bed with {spacing:g} mm spacing'
                         + (' and a prime tower' if reserve else '')
                         + (', even rearranged' if rearrange_requested(job) else ''))
        return out
    shifts, bounds = _shifts(placed, boxes, extents, yaw_by_object, yaw_hulls)
    out.update({'mode': 'merge', 'shifts': shifts, 'placed_bounds_mm': bounds, 'slots': used,
                'packing': packing, 'objects_out': sorted(shifts, key=str)})
    if yaw_by_object:
        out['yaw_by_object'] = yaw_by_object
    if tower_xy is not None:
        out['prime_tower_xy_mm'] = list(tower_xy)
    return out
