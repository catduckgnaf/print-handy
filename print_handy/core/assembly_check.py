"""Deterministic engineering checks for multi-part design projects (#998).

The multi-part design feature must never take a model's word that a part
fits, is watertight or mates with another: these checks measure the
exported geometry itself and judge it against stated standards and the
real printer. They are offline, stdlib + the repo's own STL reader, and
make no claim beyond what they measure — a passing review says nothing
about load capacity, creep, heat or print success.

What is checked (each finding carries the measured evidence):

* **Per part** — readable, non-empty, watertight, manifold, consistent
  winding, one shell, positive volume; measured extents.
* **Bed fit** — the part's footprint on the actual bed in either flat
  orientation (90° about Z allowed), the height against the build height,
  and the edge margin. A margin under ``min_margin_mm`` is a warning, not
  a pass: a 3 mm edge is unproven until a real slice shows skirts, brims,
  purge and exclusion zones fit.
* **Rack ears** — a 19-inch rack unit's *pitch* (44.45 mm per U) is not
  the height a panel may occupy: EIA-310 panels are 1/32 in (0.794 mm)
  shorter per installation, so a 2U ear may be at most 88.106 mm, not
  88.9 mm. An ear above the panel clearance fails even though it is
  below the pitch.
* **Mirrored pairs** — left/right parts must agree in extents and volume.
* **Project shape** — a "two or more pieces" request is not satisfied by
  one part, coupons, or a reference model: at least two distinct
  structural printable parts must exist, and reference geometry named as
  not-for-printing is never counted or offered as printable.
* **Declared roles** (#1017) — a part may *declare* its role on its
  project manifest (``project_v1``) instead of having it inferred from
  the file name: a declared role wins, is reported as ``declared``, and a
  part declared a ``rack_ear`` earns the panel-clearance check by
  declaration rather than by the height heuristic that undeclared parts
  still get. A declared ear with no rack size to check against is a
  warning, never a silent pass.
* **Assembly transforms and collisions** (#1017) — when parts declare an
  ``assembly_transform`` placement (again on the manifest), placed parts
  are checked against each other: disjoint placed bounds prove no
  collision; overlapping bounds get an exact interpenetration test (a
  vertex strictly inside the other solid, or an edge piercing a face
  with its interior inside the other solid — flush contact is *not* a
  collision). Parts without a declared placement are named as unchecked;
  a review where nothing is placed still states transforms and
  collisions as not checked.

Not covered here (stated, not implied): fastener reach and clearances,
load and thermal behavior, and slicer toolpath fit.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

RACK_UNIT_MM = 44.45
EIA_PANEL_ALLOWANCE_MM = 0.79375  # 1/32 inch per panel (EIA-310)
RACK_FLANGE_WIDTH_MM = 482.6
DEFAULT_MIN_MARGIN_MM = 5.0
MIRROR_TOLERANCE_MM = 0.05
MIRROR_VOLUME_TOLERANCE = 0.001  # relative
NOT_FOR_PRINTING_MARKERS = ("NOT_FOR_PRINTING", "REFERENCE_", "_REFERENCE")
COUPON_MARKERS = ("_TEST_", "TEST_", "COUPON")
CONTACT_EPS_MM = 0.01  # within this of a surface is contact, not penetration


@dataclass
class Finding:
    level: str  # "fail" | "warn" | "info"
    code: str
    message: str
    part: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _rotation_rows(rotate_deg) -> tuple[tuple[float, float, float], ...]:
    """Rotation matrix rows for degrees about X, then Y, then Z (Rz·Ry·Rx)."""
    ax, ay, az = (math.radians(float(v)) for v in rotate_deg)
    cx, sx, cy, sy, cz, sz = (math.cos(ax), math.sin(ax), math.cos(ay),
                              math.sin(ay), math.cos(az), math.sin(az))
    return (
        (cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx),
        (sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx),
        (-sy, cy * sx, cy * cx),
    )


def apply_transform(transform: dict, point) -> tuple[float, float, float]:
    """One local point in assembly coordinates under a declared transform."""
    rows = _rotation_rows(transform.get("rotate_deg") or (0.0, 0.0, 0.0))
    tx, ty, tz = (float(v) for v in transform.get("translate_mm") or (0.0, 0.0, 0.0))
    x, y, z = (float(v) for v in point)
    return (rows[0][0] * x + rows[0][1] * y + rows[0][2] * z + tx,
            rows[1][0] * x + rows[1][1] * y + rows[1][2] * z + ty,
            rows[2][0] * x + rows[2][1] * y + rows[2][2] * z + tz)


@dataclass
class PartReport:
    name: str
    role: str  # "structural" | "coupon" | "reference" (declared "rack_ear" counts as structural)
    info: dict[str, Any]
    role_source: str = "inferred"  # "declared" | "inferred"
    declared_role: str | None = None
    transform: dict[str, Any] | None = None  # normalized assembly_transform, or None
    path: str = ""

    @property
    def dims(self) -> tuple[float, float, float]:
        return tuple(float(v) for v in self.info.get("dimensions_mm") or (0.0, 0.0, 0.0))

    @property
    def volume(self) -> float | None:
        value = self.info.get("abs_volume_mm3")
        return float(value) if value is not None else None

    @property
    def placed_bounds(self) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        """(min, max) in assembly coordinates: the local bounds under the
        declared transform, or the local bounds themselves when unplaced."""
        lo = tuple(float(v) for v in self.info.get("bounds_min_mm") or (0.0, 0.0, 0.0))
        hi = tuple(float(v) for v in self.info.get("bounds_max_mm") or (0.0, 0.0, 0.0))
        if not self.transform:
            return lo, hi
        corners = [apply_transform(self.transform, (x, y, z))
                   for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]
        return (tuple(min(c[i] for c in corners) for i in range(3)),
                tuple(max(c[i] for c in corners) for i in range(3)))


def panel_clearance_mm(units: int) -> float:
    """The height a panel of ``units`` rack units may occupy (EIA-310)."""
    return round(units * RACK_UNIT_MM - EIA_PANEL_ALLOWANCE_MM, 3)


def part_role(name: str) -> str:
    upper = Path(name).name.upper()
    if any(marker in upper for marker in NOT_FOR_PRINTING_MARKERS):
        return "reference"
    if any(marker in upper for marker in COUPON_MARKERS):
        return "coupon"
    return "structural"


def _validate_declaration(declaration, name: str) -> tuple[str | None, dict | None]:
    """(declared role, normalized transform) from one declaration dict.

    Validation is the manifest's own (``core.projects``), so a declaration
    means the same thing on the manifest and in a review; an invalid one
    raises instead of being silently treated as undeclared.
    """
    if declaration is None:
        return None, None
    if not isinstance(declaration, dict):
        raise ValueError(f"a declaration for {name} must be an object; got {declaration!r}")
    from . import projects

    role = None
    if declaration.get("role") is not None:
        role = projects.validate_component_role(declaration["role"])
    transform = None
    if declaration.get("assembly_transform") is not None:
        transform = projects.validate_assembly_transform(declaration["assembly_transform"])
    return role, transform


def describe_part(path: Path | str, role: str | None = None, *,
                  declaration: dict | None = None) -> PartReport:
    """Measure one STL: extents, topology and volume.

    ``declaration`` is the part's manifest entry (``role`` /
    ``assembly_transform``): a declared role beats the file-name
    inference and is labeled ``declared``; ``rack_ear`` counts as
    structural here and is kept on ``declared_role`` for the ear check.
    """
    from .geometry_layout import describe_triangles
    from .orca_adapter import read_stl_triangles

    path = Path(path)
    info = describe_triangles(read_stl_triangles(path))
    declared_role, transform = _validate_declaration(declaration, path.name)
    if declared_role is not None:
        coarse = "structural" if declared_role == "rack_ear" else declared_role
        return PartReport(path.name, coarse, info, role_source="declared",
                          declared_role=declared_role, transform=transform, path=str(path))
    return PartReport(path.name, role or part_role(path.name), info,
                      transform=transform, path=str(path))


def declarations_from_project(project: dict, stl_paths) -> dict[str, dict]:
    """Declarations keyed by STL path, matched from a project_v1 manifest.

    Components match reviewed files by name, in order — two components
    sharing one file name consume successive components, which is the
    manifest's own ordering. A component with no declared role or
    transform contributes no declaration.
    """
    components = list((project or {}).get("components") or [])
    used: set[int] = set()
    out: dict[str, dict] = {}
    for raw in stl_paths:
        path = Path(raw)
        for idx, component in enumerate(components):
            if idx in used or component.get("name") != path.name:
                continue
            used.add(idx)
            declaration = {key: component[key] for key in ("role", "assembly_transform")
                           if component.get(key) is not None}
            if declaration:
                out[str(path)] = declaration
            break
    return out


# ---------------------------------------------------------------- checks

def check_part(part: PartReport) -> list[Finding]:
    info = part.info
    out: list[Finding] = []
    if not info.get("faces"):
        return [Finding("fail", "empty_mesh", "the part has no triangles", part.name)]
    problems = {
        "not watertight": not info.get("watertight"),
        "non-manifold edges": bool(info.get("non_manifold_edges")),
        "inconsistent winding": not info.get("winding_consistent", True),
        "more than one shell": int(info.get("shell_count") or 1) > 1,
        "degenerate triangles": bool(info.get("degenerate_triangles")),
    }
    bad = [name for name, failed in problems.items() if failed]
    evidence = {k: info.get(k) for k in ("watertight", "boundary_edges", "non_manifold_edges",
                                         "winding_inconsistencies", "shell_count",
                                         "degenerate_triangles", "faces")}
    if bad:
        level = "fail" if ("not watertight" in bad or "non-manifold edges" in bad) else "warn"
        out.append(Finding(level, "mesh_topology", "mesh problems: " + ", ".join(bad), part.name, evidence))
    if part.volume is not None and part.volume <= 0:
        out.append(Finding("fail", "no_volume", "the mesh encloses no volume", part.name,
                           {"abs_volume_mm3": part.volume}))
    out.append(Finding("info", "measured", "measured extents and volume", part.name,
                       {"dimensions_mm": list(part.dims), "abs_volume_mm3": part.volume}))
    return out


def check_bed_fit(part: PartReport, bed_mm: Iterable[float],
                  min_margin_mm: float = DEFAULT_MIN_MARGIN_MM) -> list[Finding]:
    """The part flat on the bed, in its best 90°-about-Z orientation."""
    bx, by, bz = (float(v) for v in bed_mm)
    dx, dy, dz = part.dims
    options = [(dx, dy), (dy, dx)]
    fitting = [(x, y) for x, y in options if x <= bx and y <= by]
    evidence = {"part_mm": [dx, dy, dz], "bed_mm": [bx, by, bz]}
    if dz > bz:
        return [Finding("fail", "too_tall", f"{dz:g} mm tall on a {bz:g} mm build height", part.name, evidence)]
    if not fitting:
        return [Finding("fail", "exceeds_bed", f"{dx:g} x {dy:g} mm does not fit a {bx:g} x {by:g} mm bed "
                        "in either flat orientation", part.name, evidence)]
    x, y = max(fitting, key=lambda xy: min(bx - xy[0], by - xy[1]))
    margin = round(min(bx - x, by - y) / 2.0, 3)
    evidence["edge_margin_mm"] = margin
    if margin < min_margin_mm:
        return [Finding("warn", "bed_margin_unproven",
                        f"fits with only {margin:g} mm per side; unproven until a real slice shows "
                        "skirt, brim, purge and bed exclusion zones fit", part.name, evidence)]
    return [Finding("info", "bed_fit", f"fits with {margin:g} mm per side", part.name, evidence)]


def check_rack_ear(part: PartReport, units: int) -> list[Finding]:
    """A rack-mounted part's height against the panel clearance, not the pitch."""
    height = part.dims[2]
    pitch = round(units * RACK_UNIT_MM, 3)
    clearance = panel_clearance_mm(units)
    evidence = {"height_mm": height, "units": units, "unit_pitch_mm": pitch,
                "panel_clearance_mm": clearance}
    if height > pitch:
        return [Finding("fail", "rack_over_pitch", f"{height:g} mm is taller than {units}U ({pitch:g} mm)",
                        part.name, evidence)]
    if height > clearance:
        return [Finding("fail", "rack_panel_clearance",
                        f"{height:g} mm is under the {units}U pitch ({pitch:g} mm) but over the "
                        f"{clearance:g} mm a {units}U panel may occupy; it can foul the device above "
                        "or below. Reduce the ear height or measure the rack", part.name, evidence)]
    return [Finding("info", "rack_clearance_ok", f"{height:g} mm is within the {clearance:g} mm "
                    f"{units}U panel clearance", part.name, evidence)]


def check_mirrored_pair(a: PartReport, b: PartReport) -> list[Finding]:
    da, db = a.dims, b.dims
    deltas = [round(abs(x - y), 4) for x, y in zip(da, db)]
    evidence = {"a_mm": list(da), "b_mm": list(db), "deltas_mm": deltas,
                "a_volume": a.volume, "b_volume": b.volume}
    if max(deltas) > MIRROR_TOLERANCE_MM:
        return [Finding("fail", "mirror_mismatch", f"{a.name} and {b.name} differ in extents",
                        f"{a.name}|{b.name}", evidence)]
    if a.volume and b.volume and abs(a.volume - b.volume) / max(a.volume, b.volume) > MIRROR_VOLUME_TOLERANCE:
        return [Finding("fail", "mirror_mismatch", f"{a.name} and {b.name} differ in volume",
                        f"{a.name}|{b.name}", evidence)]
    return [Finding("info", "mirror_ok", f"{a.name} and {b.name} match as a mirrored pair",
                    f"{a.name}|{b.name}", evidence)]


def check_project_shape(parts: list[PartReport], min_structural: int = 2) -> list[Finding]:
    structural = [p.name for p in parts if p.role == "structural"]
    evidence = {"structural": structural,
                "coupons": [p.name for p in parts if p.role == "coupon"],
                "reference": [p.name for p in parts if p.role == "reference"],
                "role_source": {p.name: p.role_source for p in parts}}
    if len(structural) < min_structural:
        return [Finding("fail", "too_few_parts", f"{len(structural)} structural printable part(s); "
                        f"the request needs at least {min_structural}", None, evidence)]
    return [Finding("info", "parts", f"{len(structural)} structural printable parts", None, evidence)]


# ------------------------------------------------- transforms and collisions

def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


class _Solid:
    """One part's triangles in assembly coordinates, with the indexes the
    interpenetration test needs: a YZ grid for ray casting and a uniform
    3D grid for near-surface and segment queries."""

    def __init__(self, triangles):
        self.triangles = [[tuple(float(v) for v in point) for point in tri]
                          for tri in triangles]
        lo = tuple(min(p[i] for tri in self.triangles for p in tri) for i in range(3))
        hi = tuple(max(p[i] for tri in self.triangles for p in tri) for i in range(3))
        self.bounds = (lo, hi)
        span = max(hi[i] - lo[i] for i in range(3)) or 1.0
        self.cell = max(span / 24.0, 0.05)
        self.yz: dict[tuple[int, int], list[int]] = {}
        self.grid: dict[tuple[int, int, int], list[int]] = {}
        for idx, tri in enumerate(self.triangles):
            tlo = tuple(min(p[i] for p in tri) for i in range(3))
            thi = tuple(max(p[i] for p in tri) for i in range(3))
            for j in range(int(tlo[1] // self.cell), int(thi[1] // self.cell) + 1):
                for k in range(int(tlo[2] // self.cell), int(thi[2] // self.cell) + 1):
                    self.yz.setdefault((j, k), []).append(idx)
            for i in range(int(tlo[0] // self.cell), int(thi[0] // self.cell) + 1):
                for j in range(int(tlo[1] // self.cell), int(thi[1] // self.cell) + 1):
                    for k in range(int(tlo[2] // self.cell), int(thi[2] // self.cell) + 1):
                        self.grid.setdefault((i, j, k), []).append(idx)
        self.vertices = sorted({p for tri in self.triangles for p in tri})
        edges = set()
        for tri in self.triangles:
            for a, b in ((0, 1), (1, 2), (2, 0)):
                edges.add((min(tri[a], tri[b]), max(tri[a], tri[b])))
        self.edges = sorted(edges)

    def _candidates_near(self, point):
        i, j, k = (int(point[axis] // self.cell) for axis in range(3))
        out = set()
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for dk in (-1, 0, 1):
                    out.update(self.grid.get((i + di, j + dj, k + dk), ()))
        return out

    def distance_to_surface(self, point) -> float:
        """Exact distance to the nearest candidate triangle (grid-narrowed)."""
        best = math.inf
        for idx in self._candidates_near(point):
            best = min(best, _point_triangle_distance(point, self.triangles[idx]))
        return best

    def strictly_inside(self, point) -> bool:
        """Inside the solid, and not merely touching its surface.

        A +X ray's crossings decide inside/outside (crossings closer than
        1e-7 mm are one crossing — a ray through a shared edge counts
        once); a point within CONTACT_EPS_MM of the surface is contact.
        """
        lo, hi = self.bounds
        if any(point[i] <= lo[i] or point[i] >= hi[i] for i in range(3)):
            return False
        key = (int(point[1] // self.cell), int(point[2] // self.cell))
        hits = []
        for idx in self.yz.get(key, ()):
            t = _ray_triangle_t(point, (1.0, 0.0, 0.0), self.triangles[idx])
            if t is not None and t > 1e-9:
                hits.append(t)
        crossings = 0
        last = None
        for t in sorted(hits):
            if last is None or t - last > 1e-7:
                crossings += 1
            last = t
        if crossings % 2 == 0:
            return False
        return self.distance_to_surface(point) > CONTACT_EPS_MM

    def segment_crossing_samples(self, a, b):
        """Points just either side of where segment a→b pierces this
        solid's faces, sampled 0.05 mm along the segment from each
        transversal crossing (coplanar contact yields no samples)."""
        lo = tuple(min(a[i], b[i]) for i in range(3))
        hi = tuple(max(a[i], b[i]) for i in range(3))
        candidates = set()
        for i in range(int(lo[0] // self.cell), int(hi[0] // self.cell) + 1):
            for j in range(int(lo[1] // self.cell), int(hi[1] // self.cell) + 1):
                for k in range(int(lo[2] // self.cell), int(hi[2] // self.cell) + 1):
                    candidates.update(self.grid.get((i, j, k), ()))
        direction = _sub(b, a)
        length = math.sqrt(_dot(direction, direction))
        if length <= 0:
            return []
        unit = tuple(v / length for v in direction)
        samples = []
        for idx in candidates:
            t = _segment_triangle_t(a, direction, self.triangles[idx])
            if t is None or t <= 1e-9 or t >= 1.0 - 1e-9:
                continue
            hit = tuple(a[i] + direction[i] * t for i in range(3))
            axis = max(range(3), key=lambda i: abs(direction[i]))
            for side in (-0.05, 0.05):
                point = tuple(hit[i] + unit[i] * side for i in range(3))
                if 0.0 < (point[axis] - a[axis]) / direction[axis] < 1.0:
                    samples.append(point)
        return samples


def _point_triangle_distance(point, tri) -> float:
    """Distance from a point to a triangle (Ericson's closest-point test)."""
    a, b, c = tri
    ab, ac, ap = _sub(b, a), _sub(c, a), _sub(point, a)
    d1, d2 = _dot(ab, ap), _dot(ac, ap)
    if d1 <= 0 and d2 <= 0:
        closest = a
    else:
        bp = _sub(point, b)
        d3, d4 = _dot(ab, bp), _dot(ac, bp)
        if d3 >= 0 and d4 <= d3:
            closest = b
        else:
            vc = d1 * d4 - d3 * d2
            if vc <= 0 and d1 >= 0 and d3 <= 0:
                closest = tuple(a[i] + ab[i] * (d1 / (d1 - d3)) for i in range(3))
            else:
                cp = _sub(point, c)
                d5, d6 = _dot(ab, cp), _dot(ac, cp)
                if d6 >= 0 and d5 <= d6:
                    closest = c
                else:
                    vb = d5 * d2 - d1 * d6
                    if vb <= 0 and d2 >= 0 and d6 <= 0:
                        closest = tuple(a[i] + ac[i] * (d2 / (d2 - d6)) for i in range(3))
                    else:
                        va = d3 * d6 - d5 * d4
                        if va <= 0 and (d4 - d3) >= 0 and (d5 - d6) >= 0:
                            w = (d4 - d3) / ((d4 - d3) + (d5 - d6))
                            closest = tuple(b[i] + _sub(c, b)[i] * w for i in range(3))
                        else:
                            denom = 1.0 / (va + vb + vc)
                            closest = tuple(a[i] + ab[i] * (vb * denom)
                                            + ac[i] * (vc * denom) for i in range(3))
    delta = _sub(point, closest)
    return math.sqrt(_dot(delta, delta))


def _ray_triangle_t(origin, direction, tri) -> float | None:
    """Möller–Trumbore: the ray parameter of the hit, or None."""
    a, b, c = tri
    e1, e2 = _sub(b, a), _sub(c, a)
    p = _cross(direction, e2)
    det = _dot(e1, p)
    if abs(det) < 1e-12:
        return None
    inv = 1.0 / det
    s = _sub(origin, a)
    u = _dot(s, p) * inv
    if u < -1e-9 or u > 1.0 + 1e-9:
        return None
    q = _cross(s, e1)
    v = _dot(direction, q) * inv
    if v < -1e-9 or u + v > 1.0 + 1e-9:
        return None
    t = _dot(e2, q) * inv
    return t if t > 0 else None


def _segment_triangle_t(a, direction, tri) -> float | None:
    """The segment parameter (0..1 along a→a+direction) of a transversal
    pierce, or None — a coplanar segment does not pierce."""
    t = _ray_triangle_t(a, direction, tri)
    if t is None or t <= 0.0 or t >= 1.0:
        return None
    normal = _cross(_sub(tri[1], tri[0]), _sub(tri[2], tri[0]))
    if abs(_dot(normal, direction)) < 1e-9 * max(1.0, math.sqrt(_dot(normal, normal))):
        return None
    return t


def _placed_solid(part: PartReport) -> _Solid:
    from .orca_adapter import read_stl_triangles

    triangles = read_stl_triangles(Path(part.path))
    if part.transform:
        triangles = [[apply_transform(part.transform, point) for point in tri]
                     for tri in triangles]
    return _Solid(triangles)


def _interpenetration(a: _Solid, b: _Solid):
    """A point where the two solids interpenetrate, or None.

    Two closed solids overlap in volume exactly when a vertex of one is
    strictly inside the other, or an edge of one pierces a face of the
    other with the edge's interior inside it (the crossing-slabs case,
    where no vertex is inside). Flush contact satisfies neither.
    """
    for solid, other in ((a, b), (b, a)):
        for vertex in solid.vertices:
            if other.strictly_inside(vertex):
                return vertex
        for start, end in solid.edges:
            for sample in other.segment_crossing_samples(start, end):
                if other.strictly_inside(sample):
                    return sample
    return None


def check_assembly_collision(a: PartReport, b: PartReport) -> list[Finding]:
    """Collision between two placed parts, from their placed geometry."""
    pair = f"{a.name}|{b.name}"
    alo, ahi = a.placed_bounds
    blo, bhi = b.placed_bounds
    overlap = [round(min(ahi[i], bhi[i]) - max(alo[i], blo[i]), 4) for i in range(3)]
    evidence = {"a_bounds_mm": [list(alo), list(ahi)], "b_bounds_mm": [list(blo), list(bhi)],
                "bounds_overlap_mm": overlap if all(v > 0 for v in overlap) else None}
    if any(v <= 0 for v in overlap):
        return [Finding("info", "assembly_no_collision",
                        "placed bounding boxes do not overlap; the placed parts cannot collide",
                        pair, evidence)]
    point = _interpenetration(_placed_solid(a), _placed_solid(b))
    if point is None:
        return [Finding("info", "assembly_no_collision",
                        "placed bounding boxes overlap but the placed solids do not "
                        "interpenetrate (contact or clearance only)", pair, evidence)]
    evidence["interpenetration_at_mm"] = [round(float(v), 4) for v in point]
    evidence["overlap_mm"] = overlap
    return [Finding("fail", "assembly_collision",
                    "the placed parts interpenetrate: their placed volumes overlap",
                    pair, evidence)]


def _mirror_key(name: str) -> str | None:
    upper = name.upper()
    for side in ("LEFT", "RIGHT"):
        if side in upper:
            stem = Path(upper).stem.replace(side, "#")
            return "".join(ch for ch in stem if not ch.isdigit())
    return None


def review(stl_paths: Iterable[Path | str], *, bed_mm: Iterable[float], rack_units: int | None = None,
           min_margin_mm: float = DEFAULT_MIN_MARGIN_MM, min_structural: int = 2,
           declarations: dict | None = None, project: dict | None = None) -> list[Finding]:
    """Every check over a project's exported STLs, fails first.

    ``declarations`` maps a file name (or an exact path) to that part's
    manifest declaration (``role`` / ``assembly_transform``); ``project``
    is a whole project_v1 manifest, matched to the files by
    :func:`declarations_from_project`. An explicit ``declarations`` entry
    wins over the manifest for the same part.
    """
    bed = tuple(bed_mm)
    paths = [Path(p) for p in stl_paths]
    declared: dict[str, dict] = {}
    if project is not None:
        declared.update(declarations_from_project(project, paths))
    for key, declaration in (declarations or {}).items():
        match = [p for p in paths if str(p) == str(key) or p.name == str(key)]
        for p in match:
            declared[str(p)] = declaration
    parts = [describe_part(p, declaration=declared.get(str(p))) for p in paths]
    findings: list[Finding] = []
    for part in parts:
        findings += check_part(part)
        if part.role != "reference":
            findings += check_bed_fit(part, bed, min_margin_mm)
        if part.declared_role == "rack_ear":
            # Declared ears are checked because they were declared, at any height.
            if rack_units:
                findings += check_rack_ear(part, rack_units)
            else:
                findings.append(Finding(
                    "warn", "rack_ear_unchecked",
                    "declared a rack ear, but no rack size was supplied, so its panel "
                    "clearance was not checked", part.name,
                    {"declared_role": "rack_ear"}))
        elif (part.role_source == "inferred" and rack_units and part.role == "structural"
              and part.dims[2] >= rack_units * RACK_UNIT_MM * 0.5):
            # Undeclared parts that span most of the rack height are
            # still presumed ears/brackets, as before declarations existed.
            findings += check_rack_ear(part, rack_units)
        if part.role == "reference":
            findings.append(Finding("info", "reference_not_printable",
                                    "reference geometry: never offered for printing", part.name))
    pairs: dict[str, list[PartReport]] = {}
    for part in parts:
        key = _mirror_key(part.name)
        if key and part.role == "structural":
            pairs.setdefault(key, []).append(part)
    for group in pairs.values():
        if len(group) == 2:
            findings += check_mirrored_pair(*group)
    findings += check_project_shape(parts, min_structural)
    placed = [p for p in parts if p.transform is not None]
    unplaced = [p for p in parts if p.transform is None]
    for part in placed:
        lo, hi = part.placed_bounds
        findings.append(Finding("info", "assembly_placement",
                                "placed in the assembly by its declared transform", part.name,
                                {"placed_bounds_mm": [[round(float(v), 4) for v in lo],
                                                      [round(float(v), 4) for v in hi]],
                                 "transform": part.transform}))
    if placed and unplaced:
        findings.append(Finding("warn", "assembly_placement_partial",
                                "only some parts declare an assembly placement; transforms "
                                "and collisions were checked only among the placed parts",
                                None, {"placed": [p.name for p in placed],
                                       "unplaced": [p.name for p in unplaced]}))
    for i, a in enumerate(placed):
        for b in placed[i + 1:]:
            findings += check_assembly_collision(a, b)
    order = {"fail": 0, "warn": 1, "info": 2}
    return sorted(findings, key=lambda f: (order[f.level], f.part or "", f.code))


def summary_text(findings: list[Finding]) -> str:
    """Plain text for chat: failures and warnings with their evidence, then a count."""
    lines = []
    for f in findings:
        if f.level == "info":
            continue
        where = f" [{f.part}]" if f.part else ""
        lines.append(f"{f.level.upper()}{where}: {f.message}")
    fails = sum(f.level == "fail" for f in findings)
    warns = sum(f.level == "warn" for f in findings)
    codes = {f.code for f in findings}
    if "assembly_placement_partial" in codes:
        tail = ("Assembly transforms and collisions were checked only for the placed "
                "parts; the rest have no declared placement. Not checked: fastener fit, "
                "load, heat, slicer toolpaths.")
    elif codes & {"assembly_collision", "assembly_no_collision"}:
        tail = "Not checked: fastener fit, load, heat, slicer toolpaths."
    else:
        tail = ("Not checked: assembly transforms and collisions (no part declares an "
                "assembly placement), fastener fit, load, heat, slicer toolpaths.")
    lines.append(f"{fails} failure(s), {warns} warning(s). {tail}")
    return "\n".join(lines)


__all__ = ["Finding", "PartReport", "RACK_UNIT_MM", "apply_transform", "check_assembly_collision",
           "check_bed_fit", "check_mirrored_pair", "check_part", "check_project_shape",
           "check_rack_ear", "declarations_from_project", "describe_part",
           "panel_clearance_mm", "part_role", "review", "summary_text"]
