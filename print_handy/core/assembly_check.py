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

Not covered here (stated, not implied): assembly transforms and
part-to-part collisions (they need the assembly's placement data, not
loose part meshes), fastener reach and clearances, load and thermal
behavior, and slicer toolpath fit.
"""
from __future__ import annotations

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


@dataclass
class Finding:
    level: str  # "fail" | "warn" | "info"
    code: str
    message: str
    part: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PartReport:
    name: str
    role: str  # "structural" | "coupon" | "reference"
    info: dict[str, Any]

    @property
    def dims(self) -> tuple[float, float, float]:
        return tuple(float(v) for v in self.info.get("dimensions_mm") or (0.0, 0.0, 0.0))

    @property
    def volume(self) -> float | None:
        value = self.info.get("abs_volume_mm3")
        return float(value) if value is not None else None


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


def describe_part(path: Path | str, role: str | None = None) -> PartReport:
    """Measure one STL: extents, topology and volume."""
    from .geometry_layout import describe_triangles
    from .orca_adapter import read_stl_triangles

    path = Path(path)
    info = describe_triangles(read_stl_triangles(path))
    return PartReport(path.name, role or part_role(path.name), info)


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
                "reference": [p.name for p in parts if p.role == "reference"]}
    if len(structural) < min_structural:
        return [Finding("fail", "too_few_parts", f"{len(structural)} structural printable part(s); "
                        f"the request needs at least {min_structural}", None, evidence)]
    return [Finding("info", "parts", f"{len(structural)} structural printable parts", None, evidence)]


def _mirror_key(name: str) -> str | None:
    upper = name.upper()
    for side in ("LEFT", "RIGHT"):
        if side in upper:
            stem = Path(upper).stem.replace(side, "#")
            return "".join(ch for ch in stem if not ch.isdigit())
    return None


def review(stl_paths: Iterable[Path | str], *, bed_mm: Iterable[float], rack_units: int | None = None,
           min_margin_mm: float = DEFAULT_MIN_MARGIN_MM, min_structural: int = 2) -> list[Finding]:
    """Every check over a project's exported STLs, fails first."""
    bed = tuple(bed_mm)
    parts = [describe_part(p) for p in stl_paths]
    findings: list[Finding] = []
    for part in parts:
        findings += check_part(part)
        if part.role != "reference":
            findings += check_bed_fit(part, bed, min_margin_mm)
        if rack_units and part.role == "structural" and part.dims[2] >= rack_units * RACK_UNIT_MM * 0.5:
            # Parts that span most of the rack height are the ears/brackets.
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
    lines.append(f"{fails} failure(s), {warns} warning(s). Not checked: assembly transforms and "
                 "collisions, fastener fit, load, heat, slicer toolpaths.")
    return "\n".join(lines)


__all__ = ["Finding", "PartReport", "RACK_UNIT_MM", "check_bed_fit", "check_mirrored_pair",
           "check_part", "check_project_shape", "check_rack_ear", "describe_part",
           "panel_clearance_mm", "part_role", "review", "summary_text"]
