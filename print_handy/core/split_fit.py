"""Offline full-size packing plan for splitting a 3MF into printable plates.

Margin semantics (see #76): two different clearances are used here, and each
is applied exactly once.

* ``edge`` (``layout.merge_edge_margin_mm``) is bed-edge clearance. It shrinks
  the usable area once per axis: ``usable = bed - 2 * edge``. A part placed on
  a plate of its own is checked against that usable area only.
* ``spacing`` (``layout.merge_spacing_mm``) is an object-to-object packing
  gap. It is added only between objects that share a plate; it never changes
  an individual part's footprint or the machine's printable area, so reducing
  it cannot make an individually oversized part fit.

One generated feature IS part of this geometry check when the caller
supplies the job's brim policy (#221 stage 3): the brim budget computed by
``orca_adapter.required_feature_edge`` (the explicit brim width plus the
resolved travel sweep) is added to the bed-edge clearance, so the usable
area shrinks by that budget exactly once and a part that fits only without
its brim no longer counts as fitting — its blocking reason names the brim.
With no brim policy supplied, the budget is zero and verdicts are exactly
as before. The remaining generated features (skirt, supports, prime tower
and purge structures) are slice-dependent: their extents stay with the
slicer capacity checks and the G-code out-of-bounds guard downstream, and
this check makes no claim about them. A plan returned here is therefore an
offer to slice at 100%, never a print-ready assurance, and separate plates
are never offered as a remedy for a part that does not fit a plate on its
own.

A part that fits a plate only when turned in the bed plane has its turn
recorded as ``yaw_by_object`` (object id to degrees). A turned part alone
on a plate is realized by the exporter's whole-plate fit; a turned part
may also share a plate when the shared packing is proved over the hulls
rotated to their recorded turns (#221 R2, below), realized by the
exporter's per-object fit channel composed with the plate's recorded
``shifts_by_plate``. Slicing applies exactly the recorded turn (see
``model_job._slice_full_size_split``) and refuses a plan that records a
turn for a shared plate without the shifts that realize it, instead of
slicing it as authored.

Fit is evaluated from the part's actual mesh vertices, not its bounding
rectangle (#221, following #76): at each checked yaw the envelope is the
axis-aligned envelope of the rotated mesh vertices (the true in-plane mesh
extrema), so a non-rectangular part is judged by the size it really occupies
when turned. For a rectangular part this is identical to the rectangle
check. When the selected printer model declares a printable-area exclusion,
a part counts as fitting only if a placement of that envelope exists inside
the usable area that avoids the exclusion; the plan records that placement
as ``placement_by_object`` and the part gets a plate of its own, because the
split exporter applies one recorded placement to a whole derived plate (see
``yaw_by_object``). When the avoiding placement was proved only at a turn,
the plan records that turn in ``yaw_by_object`` too, even when the authored
footprint fits the usable bed (P2-78), and the exporter composes the two for
the solo plate. Slicing applies exactly the recorded placement to that
plate alone and refuses a placement recorded for a shared plate, an unknown
object or an out-of-range position instead of slicing the plate centered
into the exclusion.

Shared plates are packed by their bounding rectangles first, exactly as
before (#221 P2). Only when rectangle packing cannot bin a set of objects
together does the planner retry that set by the objects' actual convex
hulls (``plate_merge.pack_polygon``, translation only at authored yaw):
the proved per-object translations are recorded as ``shifts_by_plate``
(parallel to ``plates``; ``None`` for a plate sliced as authored) and
slicing hands them to the exporter as ``item_shifts`` after fail-closed
validation, then re-reads the derived plate to prove it landed at the
recorded shifts. The brim/feature budget is charged exactly once in the
polygon branch — as hull clearances (``spacing + 2 * feature`` between
hulls, ``edge + feature`` to the bed edge, ``feature`` to an exclusion) —
replacing, never stacking with, the rectangle branch's footprint growth.

One deliberate change on exclusion beds (#221 P2): an object Stage 2 would
have solo-binned for its recorded placement may instead share a polygon
plate, but only when the proved polygon placement for that plate also
avoids every exclusion under the same once-only brim budget. A shared
polygon placement supersedes the solo placement for its members: a plate
never carries both, and its members lose their ``placement_by_object``
entries. When no such shared placement is proved, Stage 2 behaviour is
unchanged.

Shared plates may also carry turned parts (#221 R2, the deferred polygon
P3): after the authored packing above, a part that fits only at its
recorded yaw is tried, in order, into an existing open bin and into a new
bin shared with other turned parts — in both cases by ``pack_polygon``
over the hulls rotated to each member's recorded yaw (authored members
keep their authored hulls), with the same exclusion checks and the same
once-only brim budget charged as hull clearances. Only when no shared
placement is proved does the turned part fall back to its solo turned
plate, exactly as before. Trying shared placements first is the fit
ranking — a proved shared plate fits more parts per plate than solo
plates — and it never displaces a proved authored plan: authored bins are
formed first and a turned part joins the layout they could not fill. The
proved corners are recorded as ``shifts_by_plate`` like any polygon
plate, measured from each member's rotated bounds for turned members, and
a turned member of a shared plate keeps its ``yaw_by_object`` entry (the
plate's shifts alone do not describe its orientation) while losing any
``placement_by_object`` entry, as for any polygon member.

Reorientation is the last fit preference (#221 R1), evaluated only for a
part that fits neither as authored nor at any bed-plane yaw: one face
change from ``orca_adapter.UP_AXES`` (a 90-degree turn about X or Y that
lands the part flat on another authored face), composed with a bed
turn of the rotated footprint — a cardinal turn (0 or 90 degrees) when
one fits, else the best diagonal turn of the same sweep (#221 diagonal,
below). The plan records the full rotation in ``reorientation_by_object``; slicing applies
exactly that rotation (see ``model_job._slice_full_size_split``) and
passes the record's measured ``supports`` decision to the slicer for that
plate. The recorded ``brim_recommended`` is advisory only: it never
changes the job's brim policy. No offer is made at all when the per-part
supports/brim measurement (``orca_adapter._surface_stats``, the same
heuristics ``rotation_alternatives`` uses) cannot be performed. Arbitrary
tilts, upside-down flips and poses that do not land flat are never
candidates. Among the proved candidates for a part, cardinal-turn
candidates rank ahead of diagonal-turn ones, and each class is ranked by
fit quality — most bed margin, then no-supports over supports, then
larger measured bed contact (see
:func:`_ranked_reorientation_candidates`): the goal is always to fit as
much as possible, but ranking only ever orders proved candidates; it
never overrides a proof, a guard or the once-only budgets above.

A reoriented part alone on its plate is realized through the exporter's
whole-plate fit channel, as in R1. Beyond solo plates (#221 R3), a
reoriented part may also share a split plate: after the authored packing
and the shared-yaw attempts, it is tried into an existing open bin and
into a new bin shared with other reoriented parts (or with a turned part
still on its solo plate), in both cases by
``pack_polygon`` over each member's footprint in its recorded orientation
(the reoriented member's hull is its mesh hull after the recorded
rotation), with the same exclusion checks and the same once-only brim
budget charged as hull clearances. The proved corners are recorded as
``shifts_by_plate`` measured from the member's reoriented bounds, and
slicing realizes the member through the exporter's per-object fit channel
composed with those shifts — the same channel R2 built for shared yaw,
which accepts any rigid rotation, not only bed-plane turns. Only when no
shared placement is proved does the reoriented part fall back to its solo
plate. On an exclusion bed a reorientation candidate is proved only when
a placement of the reoriented footprint (grown by the brim budget,
exactly as for a yawed footprint) also avoids every exclusion; the winner
then carries that placement in ``placement_by_object`` like any placed
part, and a shared polygon placement supersedes it exactly as for yawed
members. The reorientation proof itself is per build item (#221 R3): the
one recorded rotation is applied rigidly to the whole object, flat
landing is proved per item (every item's rotated Z minimum within the
exporter's 0.05 mm lift tolerance of the object's), and the supports
measurement is over the object's own triangles across all its items. Two
bounds are inherited, not new: the shared geometry reader reads exactly
one build item per object id, so a source that places one object as
several build items is refused for every fit verdict in this module, not
just reorientation; and an object whose items could be read separately
would still have no single hull here, so it could be reoriented solo but
never join a hull-packed shared plate.

Diagonal bed turns (#221, final stage). Every yaw in this module comes
from one bounded sweep: :func:`mesh_yaw_candidates` checks 0–90 degrees
in 1-degree steps with envelopes computed from the hull vertices — 91
angles per footprint, deterministic, the same budget on every path.
In-plane yaw has recorded sweep angles, diagonals included, since the
mesh-hull evaluation (a 45-degree turn is the canonical shared-yaw case);
what this stage adds is the bed turn composed into a reorientation. A
reorientation candidate first tries the cardinal turns and takes a
diagonal turn only when neither cardinal fits, and among a part's proved
candidates a cardinal-turn candidate always outranks a diagonal-turn one
(see :func:`_ranked_reorientation_candidates`), so no verdict a cardinal
plan already achieved ever changes — diagonal turns only add fits. This
deliberately revisits the pinned #76 Stand verdict: the retained Stand
(130 x 230 x 30) fits at no in-plane yaw and at no cardinal reorientation
(about X it would stand 230 mm tall, over a 220 mm limit; about Y its
30 x 230 end footprint fits at neither cardinal turn), but stood on that
end at a 45-degree bed turn its envelope is 183.8 x 183.8 mm, inside the
204 mm usable bed — so it is now offered reoriented about Y at 45
degrees, its proof pinned in tests. Slicing validates a reorientation
record's yaw as any finite angle in [0, 90] whose composed matrix
matches the record exactly (see ``model_job._slice_full_size_split``).
"""
import math
import zipfile

from .. import settings


def _bed_xy_height(bed, height):
    """Normalize bed/height arguments to (bed_x, bed_y, height_limit).

    ``bed`` may be a scalar (square bed, the legacy calling convention), an
    (x, y) pair, or an (x, y, z) triple. The Z limit is the printer's
    ``max_print_height_mm`` — never the XY bed size — unless the caller
    passes an explicit ``height`` or a 3-element bed.
    """
    printer = settings.current()['printer']
    if bed is None:
        bed_xy = [float(v) for v in printer['bed_mm'][:2]]
        bed_z = float(printer['bed_mm'][2]) if len(printer['bed_mm']) > 2 else None
    elif isinstance(bed, (list, tuple)):
        bed_xy = [float(v) for v in bed[:2]]
        bed_z = float(bed[2]) if len(bed) > 2 else None
    else:
        bed_xy = [float(bed), float(bed)]
        bed_z = None
    if height is not None:
        limit = float(height)
    elif bed_z is not None:
        limit = bed_z
    else:
        limit = float(printer.get('max_print_height_mm') or bed_xy[0])
    return bed_xy[0], bed_xy[1], limit


def yaw_envelope(width, depth, degrees):
    """XY envelope (w, d) of a width x depth rectangle yawed by ``degrees``.

    Dimension-preserving: yaw rotates the part in the bed plane, so its size
    and height are unchanged; only the axis-aligned envelope grows/shrinks.
    """
    rad = math.radians(float(degrees))
    c, s = math.cos(rad), math.sin(rad)
    return (width * c + depth * s, width * s + depth * c)


def best_yaw(width, depth, usable_x, usable_y):
    """Best in-plane yaw for one rectangular footprint against a usable area.

    Sweeps yaw 0-90 degrees in 1-degree steps (the same dense-sweep approach
    as the offline analysis on #76; a sweep is numerical evidence, not a
    continuous-angle proof). Returns (degrees, (w, d)) minimizing the worst
    usable-area ratio, preferring the smallest rotation on ties.
    """
    best = None
    for degrees in range(0, 91):
        envelope = yaw_envelope(width, depth, degrees)
        ratio = max(envelope[0] / usable_x, envelope[1] / usable_y)
        key = (round(ratio, 9), degrees)
        if best is None or key < best[0]:
            best = (key, float(degrees), envelope)
    return best[1], best[2]


def _exclusion_rectangles(polygon):
    """Rectangles (x0, y0, x1, y1) covering exactly the area an exclusion polygon covers.

    A model's ``bed_exclude_area`` is one polygon chain, but a chain can trace
    several disjoint zones: the Qidi chains walk two separate strips in one
    list, joined by zero-area runs along the bed edges. Collapsing such a
    chain to its bounding rectangle would exclude the whole bed (the X-Plus 4
    chain spans (0, 0) to (305, 305)), so the polygon is decomposed instead:
    the elementary grid cells between consecutive vertex coordinates lie
    wholly inside or wholly outside an axis-aligned polygon, tested at each
    cell's center, and the inside cells — merged along each row band — are
    the zones. A polygon with a diagonal edge cannot be decomposed this way
    and keeps the conservative bounding rectangle.
    """
    from . import slice_guard
    points = [(float(point[0]), float(point[1])) for point in polygon]
    if len(points) < 3:
        return []
    edges = list(zip(points, points[1:] + points[:1]))
    if any(x0 != x1 and y0 != y1 for (x0, y0), (x1, y1) in edges):
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        return [(min(xs), min(ys), max(xs), max(ys))]
    xs = sorted({point[0] for point in points})
    ys = sorted({point[1] for point in points})
    rects = []
    for y0, y1 in zip(ys, ys[1:]):
        run_start = None
        for x0, x1 in zip(xs, xs[1:]):
            inside = slice_guard._point_in_polygon((x0 + x1) / 2, (y0 + y1) / 2, points)
            if inside and run_start is None:
                run_start = x0
            elif not inside and run_start is not None:
                rects.append((run_start, y0, x0, y1))
                run_start = None
        if run_start is not None:
            rects.append((run_start, y0, xs[-1], y1))
    return rects


def _declared_exclusions():
    """Bed exclusion rectangles (x0, y0, x1, y1) declared by the current printer model.

    Read from the model adapter's sourced facts (``bed_exclude_area``, a
    polygon in bed coordinates) and decomposed into the disjoint zones the
    polygon actually covers (see :func:`_exclusion_rectangles`). Models that
    declare none — and any lookup problem — mean no exclusions.
    """
    try:
        from .. import printers
        key = settings.current()['printer'].get('model')
        model = printers.MODELS.get(key) if key else None
        facts = getattr(getattr(model, 'adapter', None), 'facts', None) or {}
        polygon = list(getattr(facts.get('bed_exclude_area'), 'value', None) or ())
        if not polygon:
            return []
        return _exclusion_rectangles(polygon)
    except Exception:
        return []


def _placement(envelope, bed_x, bed_y, edge, exclusions):
    """A lower-left bed position for one envelope avoiding every exclusion, or None.

    The envelope (w, d) must sit inside the usable area (bed minus the edge
    clearance, applied once). Candidate positions come from the usable-area
    edges and from each exclusion's edges, which is exact for placing one
    rectangle against axis-aligned rectangles: if any valid position exists,
    one exists at a candidate.
    """
    w, d = float(envelope[0]), float(envelope[1])
    max_x, max_y = bed_x - edge - w, bed_y - edge - d
    if max_x < edge - 1e-9 or max_y < edge - 1e-9:
        return None
    cand_x, cand_y = {edge, max_x}, {edge, max_y}
    for x0, y0, x1, y1 in exclusions:
        cand_x.update(v for v in (x1, x0 - w) if edge - 1e-9 <= v <= max_x + 1e-9)
        cand_y.update(v for v in (y1, y0 - d) if edge - 1e-9 <= v <= max_y + 1e-9)

    def clamped(values, high):
        return sorted({min(max(v, edge), high) for v in values})

    for y in clamped(cand_y, max_y):
        for x in clamped(cand_x, max_x):
            if all(x + w <= x0 + 1e-9 or x >= x1 - 1e-9 or y + d <= y0 + 1e-9 or y >= y1 - 1e-9
                   for x0, y0, x1, y1 in exclusions):
                return (round(x, 3), round(y, 3))
    return None


def _rotate_hull_xy(hull, degrees):
    """Hull points turned in the bed plane by ``degrees``.

    The same rotation convention as :func:`mesh_yaw_candidates` and the
    slicing fit matrix: (x cos - y sin, x sin + y cos). Turning about any
    centre differs only by a translation, which the packer's corner
    normalization absorbs.
    """
    rad = math.radians(float(degrees))
    c, s = math.cos(rad), math.sin(rad)
    return [(x * c - y * s, x * s + y * c) for x, y in hull]


def mesh_yaw_candidates(hull_xy, usable_x, usable_y):
    """Checked yaws for one mesh footprint, best first: (ratio, degrees, (w, d)).

    ``hull_xy`` is the convex hull of the part's mesh vertices in the bed
    plane (a rotated point set's envelope is set by its hull vertices). Each
    envelope is computed from the actual vertices — the mesh's true in-plane
    extrema — not from rotating its bounding rectangle. The sweep is yaw
    0–90 degrees in 1-degree steps, the same dense-sweep approach as the
    offline analysis on #76: numerical evidence, not a continuous-angle proof.
    Ordering minimizes the worst usable-area ratio, smallest yaw on ties.
    """
    candidates = []
    for degrees in range(0, 91):
        rad = math.radians(degrees)
        c, s = math.cos(rad), math.sin(rad)
        xs = [x * c - y * s for x, y in hull_xy]
        ys = [x * s + y * c for x, y in hull_xy]
        envelope = (max(xs) - min(xs), max(ys) - min(ys))
        ratio = max(envelope[0] / usable_x, envelope[1] / usable_y)
        candidates.append((round(ratio, 9), float(degrees), envelope))
    candidates.sort(key=lambda row: (row[0], row[1]))
    return candidates


def _rank_reorientation_candidates(candidates):
    """Deterministic fit-quality order for proved reorientation candidates.

    The steering for all fit work — fit as much as possible — applied among
    proved candidates only: the most bed margin first, then a candidate
    that needs no supports over one that does, then the larger measured bed
    contact; the axis name breaks any remaining tie, so the order is total
    and a given part always records the same winner. Ranking never creates
    a candidate: every entry has already passed the flat-landing, footprint
    and height proofs in :func:`_reorientation_candidates`.
    """
    return sorted(candidates, key=lambda c: (-c['margin_mm'], bool(c['supports']),
                                             -c['bed_contact_mm2'], c['axis']))


def _ranked_reorientation_candidates(candidates):
    """Proved reorientation candidates in offer order (#221 diagonal).

    Candidates whose bed turn is cardinal (0 or 90 degrees) rank ahead of
    candidates that need a diagonal turn — each class internally in the
    fit-quality order of :func:`_rank_reorientation_candidates`. A diagonal
    turn is an additional candidate class, never a replacement: when any
    cardinal-turn candidate is proved, the winner is exactly the one R1
    would have recorded, and diagonal candidates only ever add fits for
    parts no cardinal reorientation could fit.
    """
    cardinal = [c for c in candidates if c['yaw_deg'] in (0.0, 90.0)]
    diagonal = [c for c in candidates if c['yaw_deg'] not in (0.0, 90.0)]
    return (_rank_reorientation_candidates(cardinal)
            + _rank_reorientation_candidates(diagonal))


def _reorientation_candidates(source, oid, item_points, usable, height_limit):
    """Proved face-change candidates for one object (#221 R1), best pair first.

    One candidate per ``orca_adapter.UP_AXES`` axis pair, mirroring
    ``rotation_alternatives``: within a pair (+Y/-Y, +X/-X) the face is
    chosen by measured bed contact, then least overhang, from
    ``orca_adapter._surface_stats`` over the object's own triangles. A
    candidate is proved only when the rotated part lands flat (every build
    item's rotated Z minimum within the exporter's 0.05 mm lift tolerance
    of the object's), its rotated height is within the limit, and its
    rotated footprint fits the usable area at a bed turn: a cardinal turn
    (0 or 90 degrees) when one fits, else the best fitting diagonal turn
    of the same bounded 1-degree sweep (#221 diagonal).
    The composed matrix is yaw-after-face-change, ``_matmul(_yaw, up)``,
    exactly as the single-plate path records it. ``supports`` and
    ``brim_recommended`` reuse ``rotation_alternatives``' heuristics
    against the authored overhang. The proof is per build item, so a
    multi-item object is measured exactly like a single-item one
    (#221 R3): the rotation is one rigid transform of the whole object,
    the flat-landing check below is per item (every item's rotated Z
    minimum within 0.05 mm of the object's), and the surface measurement
    is over the object's own triangles across all its items, so no item's
    relationship to the others is ever altered. (Whether a multi-item
    object's points can be read at all is the geometry reader's contract,
    shared by every fit verdict; this function simply no longer refuses
    a point set for having more than one item.)
    """
    from .orca_adapter import (IDENTITY_ROT, UP_AXES, _hull, _matmul,
                               _object_triangles, _rotate, _surface_stats, _yaw)
    tris = _object_triangles(source, oid)
    if not tris:
        return []
    _contact, authored_overhang = _surface_stats(tris, IDENTITY_ROT)
    proved = []
    for pair in (('+Y', '-Y'), ('+X', '-X')):
        options = []
        for axis in pair:
            _name, up = UP_AXES[axis]
            contact, overhang = _surface_stats(tris, up)
            options.append((contact, -overhang, axis, up, overhang))
        contact, _neg_overhang, axis, up, overhang = max(
            options, key=lambda option: (option[0], option[1], option[2]))
        rotated_items = [[_rotate(p, up) for p in item] for item in item_points]
        floor = min(p[2] for item in rotated_items for p in item)
        if any(min(p[2] for p in item) - floor > 0.05 for item in rotated_items):
            continue  # some item would hover: not a flat landing, no candidate
        rotated = [p for item in rotated_items for p in item]
        height = max(p[2] for p in rotated) - floor
        if height > height_limit + 1e-9:
            continue
        hull = _hull([(p[0], p[1]) for p in rotated])
        # Bed turn for the reoriented footprint: a cardinal turn (0 or 90
        # degrees) is always preferred — the first fitting cardinal in the
        # sweep's fit-quality order is exactly the R1 choice. Only when
        # neither cardinal turn fits does the best fitting diagonal turn of
        # the same bounded sweep become this pair's candidate (#221
        # diagonal): a diagonal turn is an additional candidate class, so
        # it can make a part fit that no cardinal reorientation could, and
        # it never displaces a proved cardinal candidate.
        sweep = mesh_yaw_candidates(hull, usable[0], usable[1])
        chosen = None
        for _ratio, degrees, envelope in sweep:
            if degrees not in (0.0, 90.0):
                continue
            if envelope[0] <= usable[0] + 1e-9 and envelope[1] <= usable[1] + 1e-9:
                chosen = (degrees, envelope)
                break
        if chosen is None:
            for _ratio, degrees, envelope in sweep:
                if envelope[0] <= usable[0] + 1e-9 and envelope[1] <= usable[1] + 1e-9:
                    chosen = (degrees, envelope)
                    break
        if chosen is None:
            continue
        degrees, envelope = chosen
        footprint = envelope[0] * envelope[1]
        proved.append({
            'axis': axis,
            'rotation': [round(v, 9) for v in _matmul(_yaw(degrees), up)],
            'yaw_deg': float(degrees),
            'dims_mm': [round(envelope[0], 3), round(envelope[1], 3), round(height, 3)],
            'supports': bool(overhang > authored_overhang * 1.5 + 100.0),
            'brim_recommended': bool(contact < 400 or contact < 0.05 * footprint),
            'bed_contact_mm2': round(contact),
            'overhang_mm2': round(overhang),
            'margin_mm': round(min(usable[0] - envelope[0], usable[1] - envelope[1]), 3),
        })
    return proved


def _feature_edge_mm(brim_policy=None, state=None):
    """Brim/feature edge budget in mm for a supplied brim policy, else 0.

    The budget is ``orca_adapter.required_feature_edge`` (explicit brim
    width plus the resolved travel sweep) for the job's brim policy. No
    policy — neither passed directly nor carried on ``state`` — means no
    budget, so policy-off jobs keep the pre-stage-3 verdicts exactly.
    """
    if brim_policy is None and not (state and state.get('brim_policy')):
        return 0.0
    from .orca_adapter import required_feature_edge
    return float(required_feature_edge(state or {}, brim_policy))


def per_part_fit(source, bed=None, height=None, exclusions=None, brim_policy=None, state=None):
    """Evaluate every authored object of a 3MF at 100% scale, one row per object.

    Each row reports the authored extent, the usable XY area (bed minus the
    edge clearance, applied once), the Z limit, the best dimension-preserving
    yaw for the part's actual mesh footprint (see :func:`mesh_yaw_candidates`),
    and whether the part fits a plate on its own at full size in any checked
    yaw. When the printer model declares bed exclusions (``exclusions`` may
    override them, as (x0, y0, x1, y1) rectangles in bed coordinates), the row
    also reports an avoiding ``placement_mm``; a part whose footprint fits
    only where every placement overlaps an exclusion does not count as
    fitting, and its blocking reason names the exclusion. When the caller
    supplies the job's brim policy (``brim_policy``, with the printer
    ``state`` it was resolved against), the brim/feature budget from
    :func:`_feature_edge_mm` shrinks the usable area exactly once, the row
    reports it as ``feature_edge_mm``, placements keep the brimmed footprint
    clear of exclusions as well as the bed edge, and a part that fits only
    without its brim does not count as fitting — its blocking reason names
    the brim budget. As a last preference, a part that fits in no
    orientation above is evaluated for a face change (see
    :func:`_reorientation_candidates`): when a proved candidate exists the
    row reports it as ``reorientation`` and counts as fitting, with the
    candidate's measured supports/brim record attached; on an exclusion bed
    the candidate is proved only with an avoiding placement of the
    reoriented footprint, which the row then reports as ``placement_mm``
    like any placed part. Parts are evaluated
    as authored: grouping, part colors and relationships are read, never
    altered, and a continuous part is never silently split.
    ``fits_full_size`` is False for a part that no separate-plate
    assignment can cure.
    """
    from . import color_geometry
    from . import plate_merge
    from .source_colors import inventory

    bed_x, bed_y, height_limit = _bed_xy_height(bed, height)
    edge = float(plate_merge.EDGE_MARGIN_MM)
    feature = _feature_edge_mm(brim_policy, state)
    # The brim/feature budget is added to the edge clearance once: the usable
    # area is bed - 2 * (edge + feature) per axis, never shrunk twice.
    usable = (bed_x - 2 * (edge + feature), bed_y - 2 * (edge + feature))
    usable_no_brim = (bed_x - 2 * edge, bed_y - 2 * edge)
    if exclusions is None:
        exclusions = _declared_exclusions()
    exclusions = [tuple(float(v) for v in rect) for rect in exclusions]
    manifest = inventory(source)
    names = {}
    keys = {}
    for part in manifest.get('parts') or []:
        oid = str(part['object_id'])
        names.setdefault(oid, part.get('name') or f'Object {oid}')
        keys.setdefault(oid, []).append(part['key'])
    rows = []
    if not keys:
        return rows
    with zipfile.ZipFile(source) as archive:
        for oid in sorted(keys, key=str):
            item_points = color_geometry._world_item_points(archive, {oid})
            points = [point for item in item_points for point in item]
            extent = [round(max(p[a] for p in points) - min(p[a] for p in points), 3)
                      for a in range(3)]
            hull = color_geometry._world_hull_xy(archive, {oid})[0][0]
            candidates = mesh_yaw_candidates(hull, usable[0], usable[1])
            _ratio, best_deg, best_envelope = candidates[0]
            fits_authored = (extent[0] <= usable[0] + 1e-9 and extent[1] <= usable[1] + 1e-9
                             and extent[2] <= height_limit + 1e-9)
            chosen, placement = None, None
            fits_without_brim = False
            if extent[2] <= height_limit + 1e-9:
                for _r, degrees, envelope in candidates:
                    if envelope[0] > usable[0] + 1e-9 or envelope[1] > usable[1] + 1e-9:
                        continue
                    if exclusions:
                        if feature:
                            # Place the brimmed footprint (envelope grown by
                            # the budget on every side) against the original
                            # edge clearance, so the brim itself also avoids
                            # the exclusion and the bed edge; the recorded
                            # placement is the part's own lower-left corner.
                            grown = (envelope[0] + 2 * feature, envelope[1] + 2 * feature)
                            spot = _placement(grown, bed_x, bed_y, edge, exclusions)
                            if spot is None:
                                continue
                            placement = (round(spot[0] + feature, 3), round(spot[1] + feature, 3))
                        else:
                            spot = _placement(envelope, bed_x, bed_y, edge, exclusions)
                            if spot is None:
                                continue
                            placement = spot
                    chosen = (degrees, envelope)
                    break
                if feature:
                    # Shadow verdict at the no-brim usable area, used only to
                    # name the brim as the blocker when it alone is decisive.
                    for _r, _degrees, envelope in candidates:
                        if (envelope[0] <= usable_no_brim[0] + 1e-9
                                and envelope[1] <= usable_no_brim[1] + 1e-9
                                and (not exclusions
                                     or _placement(envelope, bed_x, bed_y, edge, exclusions) is not None)):
                            fits_without_brim = True
                            break
            fits = chosen is not None
            reorientation = None
            reorientation_unplaced = False
            if not fits:
                # Last preference (#221 R1, extended by R3): a face change is
                # evaluated only when neither the authored orientation nor
                # any bed-plane yaw fits, so no existing verdict ever becomes
                # a reoriented one. On an exclusion bed a candidate counts as
                # proved only when a placement of its reoriented footprint —
                # grown by the brim budget exactly as a yawed footprint is,
                # so the budget is still charged once — also avoids every
                # exclusion; candidates are tried in the ranked order and the
                # winner's placement is recorded like any placed part's. And
                # if the per-part supports/brim measurement cannot be
                # performed there is no offer at all — never a silent default
                # either way.
                try:
                    proved = _reorientation_candidates(source, oid, item_points,
                                                       usable, height_limit)
                except Exception:
                    proved = []
                if proved and exclusions:
                    winner = None
                    for candidate in _ranked_reorientation_candidates(proved):
                        envelope = candidate['dims_mm'][:2]
                        if feature:
                            grown = (envelope[0] + 2 * feature, envelope[1] + 2 * feature)
                            spot = _placement(grown, bed_x, bed_y, edge, exclusions)
                            spot = (round(spot[0] + feature, 3),
                                    round(spot[1] + feature, 3)) if spot else None
                        else:
                            spot = _placement(envelope, bed_x, bed_y, edge, exclusions)
                        if spot is not None:
                            winner, placement = candidate, spot
                            break
                    if winner is None:
                        reorientation_unplaced = True
                        proved = []
                    else:
                        proved = [winner]
                if proved:
                    winner = _ranked_reorientation_candidates(proved)[0]
                    reorientation = {key: winner[key] for key in
                                     ('axis', 'rotation', 'yaw_deg', 'dims_mm', 'supports',
                                      'brim_recommended', 'bed_contact_mm2', 'overhang_mm2')}
                    fits = True
            yaw, envelope = chosen if chosen else (best_deg, best_envelope)
            reason = None
            if not fits:
                if extent[2] > height_limit + 1e-9:
                    reason = (f'height {extent[2]:g} mm exceeds the {height_limit:g} mm '
                              'print height')
                elif feature and fits_without_brim:
                    reason = (f'footprint {extent[0]:g} x {extent[1]:g} mm fits the usable '
                              f'{usable_no_brim[0]:g} x {usable_no_brim[1]:g} mm bed without a brim, '
                              f'but not with the {feature:g} mm brim/feature edge budget applied '
                              f'once (usable {usable[0]:g} x {usable[1]:g} mm with the brim)')
                elif exclusions and any(env[0] <= usable[0] + 1e-9 and env[1] <= usable[1] + 1e-9
                                        for _r, _d, env in candidates):
                    x0, y0, x1, y1 = exclusions[0]
                    reason = (f'footprint {extent[0]:g} x {extent[1]:g} mm fits the usable '
                              f'{usable[0]:g} x {usable[1]:g} mm bed only where it would overlap '
                              f'the declared bed exclusion ({x0:g}, {y0:g}) to ({x1:g}, {y1:g}) mm; '
                              'no placement at a checked yaw avoids it')
                elif exclusions and reorientation_unplaced:
                    x0, y0, x1, y1 = exclusions[0]
                    reason = (f'footprint {extent[0]:g} x {extent[1]:g} mm fits the usable '
                              f'{usable[0]:g} x {usable[1]:g} mm bed only reoriented, but the '
                              f'reoriented footprint cannot be placed clear of the declared '
                              f'bed exclusion ({x0:g}, {y0:g}) to ({x1:g}, {y1:g}) mm')
                else:
                    reason = (f'footprint {extent[0]:g} x {extent[1]:g} mm exceeds the usable '
                              f'{usable[0]:g} x {usable[1]:g} mm bed at every checked yaw '
                              f'(best {envelope[0]:.1f} x {envelope[1]:.1f} mm at {yaw:g} degrees)')
            rows.append({'object_id': oid, 'name': names[oid], 'keys': keys[oid],
                         'extent_mm': extent, 'usable_mm': [round(usable[0], 3), round(usable[1], 3)],
                         'height_limit_mm': height_limit,
                         'best_yaw_deg': yaw,
                         'best_extent_mm': [round(envelope[0], 3), round(envelope[1], 3), extent[2]],
                         'fits_authored': bool(fits_authored),
                         'feature_edge_mm': round(feature, 3),
                         'fits_full_size': bool(fits),
                         'reorientation': reorientation,
                         'placement_mm': list(placement) if placement else None,
                         'exclusions_mm': [[round(v, 3) for v in rect] for rect in exclusions],
                         'blocking_reason': reason})
    return rows


def split_plan_100(source, bed=None, height=None, exclusions=None, brim_policy=None, state=None):
    """Return the fewest greedy full-size plates, or None if any object cannot fit.

    Source plate assignments are respected: objects from distinct authored plates
    are never combined. Every object is first evaluated on its own with
    :func:`per_part_fit` (per-part mesh footprint at dimension-preserving yaws,
    and the printer's real Z limit): a part that fits no plate alone has no
    split plan, because separate plates cannot cure an individually oversized
    part. An object that fits only at a turned yaw has the turn recorded in
    ``yaw_by_object`` and is planned alone on its own plate, unless a proved
    shared placement over the rotated hulls lets it share a plate instead
    (#221 R2; see the module docstring); objects that fit as authored keep
    their authored orientation and are packed together as before. When the bed has declared exclusions, every fitting object carries
    an avoiding placement from :func:`per_part_fit`: it is planned alone on
    its own plate and the placement is recorded in ``placement_by_object``,
    so slicing can land that plate exactly there (#221 stage 2) — unless a
    proved convex-hull placement lets it share a plate instead, in which
    case the shared placement in ``shifts_by_plate`` supersedes the solo
    placement for that object (see the module docstring, #221 P2). A part with
    no avoiding placement does not fit (its blocking reason names the
    exclusion), so no plan is offered for it. When the caller supplies the
    job's brim policy (``brim_policy`` / ``state``, #221 stage 3), the
    brim/feature budget is threaded into :func:`per_part_fit` — a part that
    fits only without its brim has no plan — and packing checks each plate
    with footprints grown by the budget on every side, so shared plates keep
    the brimmed parts clear of the bed edge and of each other; the budget is
    applied exactly once in each check, never stacked. A part that fits only
    reoriented (#221 R1) has its rotation record carried in
    ``reorientation_by_object`` and is planned alone on its own plate,
    unless a proved shared placement over the reoriented hulls lets it
    share a plate instead (#221 R3; see the module docstring) — the shared
    attempts run after the authored packing and the shared-yaw attempts,
    and the solo reoriented plate remains the fallback. An object appears in
    at most one of the plan's transform records (``yaw_by_object``,
    ``placement_by_object``, ``reorientation_by_object`` or a
    ``shifts_by_plate`` entry) — with one exception: a part whose avoiding
    placement was proved only at a turn appears in both ``yaw_by_object``
    and ``placement_by_object``, the two records the exporter composes for
    its solo plate (P2-78). Geometry checks and packing use the
    printer's edge and spacing margins (see the module docstring) and do
    not slice or contact the printer.
    """
    from . import color_geometry
    from . import model_job
    from . import plate_merge
    from .source_colors import inventory

    bed_x, bed_y, height_limit = _bed_xy_height(bed, height)
    bed = min(bed_x, bed_y)
    manifest = inventory(source)
    if not manifest.get('parts'):
        return None
    # Pass the normalized limit, not the raw argument: with a 3-element bed
    # and no explicit height, the raw argument is None and per_part_fit would
    # fall back to the configured printer height, discarding the caller's
    # lower Z limit (P2-69).
    report = per_part_fit(source, bed=(bed_x, bed_y), height=height_limit, exclusions=exclusions,
                          brim_policy=brim_policy, state=state)
    feature = _feature_edge_mm(brim_policy, state)
    if any(not row['fits_full_size'] for row in report):
        return None
    # A part whose authored footprint fits the usable bed but whose avoiding
    # placement was proved only at a turn carries that turn as well (P2-78):
    # without it the exporter would realize the placement with the unturned
    # footprint, outside the area the placement was proved against. For its
    # solo plate the exporter composes the recorded turn with the recorded
    # placement; a proved shared placement supersedes both with its shifts.
    yaw_by_object = {row['object_id']: row['best_yaw_deg'] for row in report
                     if row['best_yaw_deg'] > 0 and not row.get('reorientation')
                     and (not row['fits_authored'] or row.get('placement_mm'))}
    # A reoriented part carries its full rotation in its own record (the
    # matrix already contains its bed turn), never a yaw entry (#221 R1).
    reorientation_by_object = {row['object_id']: row['reorientation'] for row in report
                               if row.get('reorientation')}
    # On an exclusion bed every fitting row carries the avoiding placement
    # per_part_fit proved; a row without one would already have failed
    # fits_full_size above, so a missing placement here is impossible.
    placement_by_object = {row['object_id']: row['placement_mm'] for row in report
                           if row.get('placement_mm')}
    try:
        authored = model_job.plate_manifest(source)['plates']
    except (RuntimeError, zipfile.BadZipFile):
        authored = []
    object_to_plate = {str(object_id): plate['id'] for plate in authored
                       for object_id in plate['object_ids']}
    by_object = {}
    for part in manifest['parts']:
        by_object.setdefault(str(part['object_id']), []).append(part['key'])
    groups = {}
    for object_id, keys in by_object.items():
        groups.setdefault(object_to_plate.get(object_id, 1), {})[object_id] = keys

    if exclusions is None:
        exclusions_eff = _declared_exclusions()
    else:
        exclusions_eff = [tuple(float(v) for v in rect) for rect in exclusions]
    result = []
    shifts_by_plate = []
    with zipfile.ZipFile(source) as archive:
        for plate_id in sorted(groups, key=str):
            objects = groups[plate_id]
            boxes = {oid: color_geometry._world_bounds(archive, {oid})[0] for oid in objects}
            # Packing footprints carry the brim/feature budget grown on every
            # side (zero when no brim policy was supplied, so packing is then
            # exactly as before); pack() applies the bed-edge margin itself.
            extents = {oid: (box[0][1] - box[0][0] + 2 * feature, box[1][1] - box[1][0] + 2 * feature)
                       for oid, box in boxes.items()}
            if any(oid not in yaw_by_object and oid not in placement_by_object
                   and oid not in reorientation_by_object
                   and plate_merge.pack({oid: extent}, bed) is None
                   for oid, extent in extents.items()):
                return None
            # Hulls for the polygon fallback (#221 P2): one shared helper with
            # the merge path. None for rectangle-only sources, grouped or
            # multi-item objects — those keep today's packing by construction.
            hulls = plate_merge._object_hulls(source, list(objects))
            # Per-object hulls without that gate, for the shared-yaw attempt
            # (#221 R2): a turned rectangle is exactly what shared yaw packs,
            # so every single-item object's hull is read. Multi-item objects
            # get no entry and can never join a rotated packing.
            fit_hulls = {}
            for oid in objects:
                try:
                    items = color_geometry._world_hull_xy(archive, {oid})
                except RuntimeError:
                    items = []
                if len(items) == 1:
                    fit_hulls[oid] = items[0][0]
            # Reoriented footprints for the shared-reorientation attempts
            # (#221 R3): each reoriented object's mesh hull and bounds after
            # its recorded rotation, read through the same fit transform the
            # exporter composes. The single-item gate matches fit_hulls — a
            # multi-item object has no one hull here, so it can be reoriented
            # solo but never joins a hull-packed plate.
            reoriented_hulls = {}
            reoriented_bounds = {}
            for oid, record in reorientation_by_object.items():
                if oid not in objects:
                    continue
                fit = color_geometry.fit_transform({'rotation': record['rotation']})
                if fit is None:
                    continue
                try:
                    items = color_geometry._world_hull_xy(archive, {oid}, fit)
                    item_boxes = color_geometry._world_bounds(archive, {oid}, fit)
                except RuntimeError:
                    continue
                if len(items) == 1:
                    reoriented_hulls[oid] = items[0][0]
                    reoriented_bounds[oid] = [
                        (min(box[a][0] for box in item_boxes),
                         max(box[a][1] for box in item_boxes)) for a in range(3)]
            # Each bin: its objects, how it may still grow ('yaw' and solo
            # 'placement' bins are closed to rectangle packing; a placement
            # bin may still be superseded by a proved polygon placement),
            # and, for a polygon bin, the proved AABB corners per object.
            bins = []

            def polygon_corners(member_oids):
                if hulls is None:
                    return None
                return plate_merge.pack_polygon(
                    {oid: hulls[oid] for oid in member_oids}, bed,
                    exclusions=exclusions_eff, feature=feature)

            def yawed_corners(member_oids, yaws):
                """Proved corners for a plate whose turned members are rotated.

                ``yaws`` maps the turned members to their recorded yaw; every
                other member keeps its authored hull. Exclusion checks and
                the once-only brim budget are pack_polygon's, exactly as for
                the authored polygon fallback.
                """
                trial = {}
                for oid in member_oids:
                    hull = fit_hulls.get(oid)
                    if hull is None:
                        return None
                    trial[oid] = _rotate_hull_xy(hull, yaws[oid]) if oid in yaws else hull
                return plate_merge.pack_polygon(trial, bed,
                                                exclusions=exclusions_eff, feature=feature)

            def transformed_corners(member_oids, yaws, reoriented):
                """Proved corners for a plate with reoriented members (#221 R3).

                ``yaws`` maps turned members to their recorded yaw and
                ``reoriented`` names the members carrying a reorientation
                record; every member's hull is its footprint in its recorded
                orientation (authored hull, yaw-rotated hull, or the hull of
                the mesh after the recorded face change). Exclusion checks
                and the once-only brim budget are pack_polygon's, exactly as
                for the authored and yawed attempts.
                """
                trial = {}
                for oid in member_oids:
                    if oid in reoriented:
                        hull = reoriented_hulls.get(oid)
                    elif oid in yaws:
                        hull = fit_hulls.get(oid)
                        if hull is not None:
                            hull = _rotate_hull_xy(hull, yaws[oid])
                    else:
                        hull = fit_hulls.get(oid)
                    if hull is None:
                        return None
                    trial[oid] = hull
                return plate_merge.pack_polygon(trial, bed,
                                                exclusions=exclusions_eff, feature=feature)

            deferred_yaw = []
            deferred_reorientation = []
            for oid in sorted(extents, key=lambda key: -(extents[key][0] * extents[key][1])):
                if oid in yaw_by_object:
                    # Turned parts are binned after the authored packing:
                    # first the shared attempts (#221 R2), solo only as the
                    # fallback, exactly as this loop used to plan them.
                    deferred_yaw.append(oid)
                    continue
                if oid in reorientation_by_object:
                    # Reoriented parts are binned after the authored packing
                    # and the shared-yaw attempts: first the shared attempts
                    # (#221 R3), solo only as the fallback, exactly as R1
                    # planned them.
                    deferred_reorientation.append(oid)
                    continue
                placed = False
                for current in bins:
                    if current['kind'] in ('yaw', 'reorientation'):
                        continue
                    members = list(current['objects']) + [oid]
                    if current['kind'] == 'rect' and oid not in placement_by_object:
                        candidate = {**current['objects'], oid: extents[oid]}
                        if plate_merge.pack(candidate, bed) is not None:
                            current['objects'][oid] = extents[oid]
                            placed = True
                            break
                    # Polygon fallback: rectangle packing could not bin this
                    # set (or a member's solo placement forbids authored
                    # sharing). A proved hull placement supersedes any solo
                    # placement for its members (module docstring).
                    corners = polygon_corners(members)
                    if corners is not None:
                        current['objects'][oid] = extents[oid]
                        current['kind'] = 'polygon'
                        current['corners'] = corners
                        placed = True
                        break
                if not placed:
                    kind = 'placement' if oid in placement_by_object else 'rect'
                    bins.append({'objects': {oid: extents[oid]}, 'kind': kind, 'corners': None})
            # Shared-yaw attempts for the turned parts (#221 R2), in the
            # authored bins' order: join an open bin, then pair with other
            # turned parts, then the solo turned plate as before. A proved
            # shared plate is the better fit — more parts per plate — so it
            # is always tried before the solo fallback.
            settled_yaw = set()
            for oid in deferred_yaw:
                if oid in settled_yaw:
                    continue
                placed = False
                for current in bins:
                    if current['kind'] in ('yaw', 'reorientation'):
                        continue
                    members = list(current['objects']) + [oid]
                    trial_yaws = {**current.get('yaws', {}), oid: yaw_by_object[oid]}
                    corners = yawed_corners(members, trial_yaws)
                    if corners is not None:
                        current['objects'][oid] = extents[oid]
                        current['kind'] = 'polygon'
                        current['corners'] = corners
                        current['yaws'] = trial_yaws
                        placed = True
                        break
                if not placed:
                    members = [oid]
                    trial_yaws = {oid: yaw_by_object[oid]}
                    corners = None
                    for other in deferred_yaw:
                        if other == oid or other in settled_yaw:
                            continue
                        trial = {**trial_yaws, other: yaw_by_object[other]}
                        proved = yawed_corners(members + [other], trial)
                        if proved is not None:
                            members.append(other)
                            trial_yaws, corners = trial, proved
                    if len(members) > 1:
                        bins.append({'objects': {member: extents[member] for member in members},
                                     'kind': 'polygon', 'corners': corners, 'yaws': trial_yaws})
                        settled_yaw.update(members)
                        placed = True
                if placed:
                    settled_yaw.add(oid)
                else:
                    bins.append({'objects': {oid: extents[oid]}, 'kind': 'yaw',
                                 'corners': None, 'yaws': {}})
                    settled_yaw.add(oid)
            # Shared-reorientation attempts (#221 R3), after the authored
            # packing and the shared-yaw attempts, in the same shape as the
            # yaw pass: join an open bin, then pair with other reoriented
            # parts on a fresh plate, then the solo reoriented plate as
            # before. A proved shared plate fits more parts per plate, so it
            # is always tried before the solo fallback; it never displaces a
            # proved authored or shared-yaw plan, which were formed first.
            settled_reorientation = set()
            for oid in deferred_reorientation:
                if oid in settled_reorientation:
                    continue
                placed = False
                for current in bins:
                    if current['kind'] in ('yaw', 'reorientation'):
                        continue
                    members = list(current['objects']) + [oid]
                    trial_reoriented = {*current.get('reoriented', ()), oid}
                    corners = transformed_corners(members, current.get('yaws', {}),
                                                  trial_reoriented)
                    if corners is not None:
                        current['objects'][oid] = extents[oid]
                        current['kind'] = 'polygon'
                        current['corners'] = corners
                        current['reoriented'] = trial_reoriented
                        placed = True
                        break
                if not placed:
                    members = [oid]
                    trial_reoriented = {oid}
                    trial_yaws = {}
                    corners = None
                    # Pairing pool: other unsettled reoriented parts first,
                    # then turned parts that settled for a solo yaw plate —
                    # a mixed fresh plate is tried only after every
                    # shared-yaw attempt above has had its chance, and a
                    # proved pair dissolves the solo yaw bin it consumes.
                    partner_pool = [(other, None) for other in deferred_reorientation
                                    if other != oid and other not in settled_reorientation]
                    partner_pool += [(next(iter(current['objects'])),
                                      yaw_by_object[next(iter(current['objects']))])
                                     for current in bins if current['kind'] == 'yaw']
                    consumed_yaw = []
                    for other, other_yaw in partner_pool:
                        if other_yaw is None:
                            trial_r, trial_y = {*trial_reoriented, other}, trial_yaws
                        else:
                            trial_r, trial_y = trial_reoriented, {**trial_yaws, other: other_yaw}
                        proved = transformed_corners(members + [other], trial_y, trial_r)
                        if proved is not None:
                            members.append(other)
                            trial_reoriented, trial_yaws, corners = trial_r, trial_y, proved
                            if other_yaw is not None:
                                consumed_yaw.append(other)
                    if len(members) > 1:
                        if consumed_yaw:
                            bins[:] = [current for current in bins
                                       if not (current['kind'] == 'yaw'
                                               and next(iter(current['objects'])) in consumed_yaw)]
                        bins.append({'objects': {member: extents[member] for member in members},
                                     'kind': 'polygon', 'corners': corners, 'yaws': trial_yaws,
                                     'reoriented': trial_reoriented})
                        settled_reorientation.update(
                            member for member in members if member in reorientation_by_object)
                        placed = True
                if placed:
                    settled_reorientation.add(oid)
                else:
                    bins.append({'objects': {oid: extents[oid]}, 'kind': 'reorientation',
                                 'corners': None, 'yaws': {}})
                    settled_reorientation.add(oid)
            for current in bins:
                bin_objects = current['objects']
                result.append([key for oid in bin_objects for key in objects[oid]])
                if current['kind'] == 'polygon' and len(bin_objects) > 1:
                    corners = current['corners']
                    shifts = {}
                    for oid in bin_objects:
                        if oid in current.get('reoriented', ()):
                            # A reoriented member's shift is measured from its
                            # reoriented bounds — XY and Z alike, since the
                            # face change moves the part's floor — not its
                            # authored bounds (#221 R3).
                            base = (reoriented_bounds[oid][0][0], reoriented_bounds[oid][1][0])
                            floor = reoriented_bounds[oid][2][0]
                        elif oid in current.get('yaws', {}):
                            # A turned member's shift is measured from its
                            # rotated bounds (the corner the packer proved is
                            # the rotated hull's lower-left), not its authored
                            # bounds (#221 R2).
                            rotated = _rotate_hull_xy(fit_hulls[oid], current['yaws'][oid])
                            base = (min(p[0] for p in rotated), min(p[1] for p in rotated))
                            floor = boxes[oid][2][0]
                        else:
                            base = (boxes[oid][0][0], boxes[oid][1][0])
                            floor = boxes[oid][2][0]
                        shifts[oid] = [round(corners[oid][0] - base[0], 3) + 0.0,
                                       round(corners[oid][1] - base[1], 3) + 0.0,
                                       round(-floor, 3) + 0.0]
                    shifts_by_plate.append(shifts)
                    # The shared polygon placement supersedes Stage 2's solo
                    # placements for these objects: never both.
                    for oid in bin_objects:
                        placement_by_object.pop(oid, None)
                else:
                    shifts_by_plate.append(None)
    if len(result) <= 1:
        # A one-plate result is still a usable plan when it carries a
        # polygon placement (shifts) or a turn (yaw) the authored layout
        # does not have (P2-81, P2-83, P2-84): discarding it would drop the
        # only full-size offer for exactly the sources those proofs exist
        # for. A bare one-plate grouping adds nothing over the authored
        # layout, and a one-plate result whose fit rests on a reorientation
        # keeps the not-a-split rule the brim tests pin, so both stay
        # no-plan.
        carries_transform = (bool(yaw_by_object) or any(shifts_by_plate)) and not reorientation_by_object
        if len(result) == 0 or not carries_transform:
            return None
    plan = {'plates': result}
    if yaw_by_object:
        plan['yaw_by_object'] = yaw_by_object
    if reorientation_by_object:
        plan['reorientation_by_object'] = reorientation_by_object
    if placement_by_object:
        plan['placement_by_object'] = placement_by_object
    if any(shifts_by_plate):
        plan['shifts_by_plate'] = shifts_by_plate
    return plan
