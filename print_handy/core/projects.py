"""Source-neutral multi-part project identity and safe, readable plate references.

A project describes source components separately from printable *plates*: several
parts may share one G-code, and one source part may require several plates.
Do not use the human-readable 82A code as an internal primary key. Keep the
existing integer print_number registry intact for older records and commands.

A component may also *declare* assembly data (#1017 item 3): its ``role``
in the assembled project and its ``assembly_transform`` placement. The
manifest is the only declaration surface — ``assembly_check`` consumes
these declarations instead of inferring roles from file names or skipping
transform and collision checks for want of placement data.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

SCHEMA = "project_v1"
_SAFE_ID = re.compile(r"[^a-zA-Z0-9_-]+")

# Roles a source may declare for a component (#1017 item 3). The first three
# are the coarse roles assembly_check used to infer from file names alone;
# "rack_ear" declares the part a rack ear (it counts as structural, and the
# declaration — not a height guess — is what earns it the panel-clearance
# check). Anything else is rejected, never silently dropped.
COMPONENT_ROLES = ("structural", "coupon", "reference", "rack_ear")


def validate_component_role(value) -> str:
    """A declared component role, or ValueError naming the bad value."""
    if not isinstance(value, str) or value not in COMPONENT_ROLES:
        raise ValueError(f"a component role must be one of {', '.join(COMPONENT_ROLES)}; "
                         f"got {value!r}")
    return value


def _vec3(value, field):
    if (not isinstance(value, (list, tuple)) or len(value) != 3
            or any(isinstance(v, bool) or not isinstance(v, (int, float))
                   or not math.isfinite(v) for v in value)):
        raise ValueError(f"assembly_transform {field} must be three finite numbers; "
                         f"got {value!r}")
    return [float(v) for v in value]


def validate_assembly_transform(value) -> dict:
    """A declared assembly placement, normalized, or ValueError.

    The placement is the part's local frame in the assembled project:
    ``rotate_deg`` (degrees about X, then Y, then Z) and ``translate_mm``.
    Only those two keys exist — a scale or a raw matrix is a different
    claim and is rejected rather than half-applied. A missing axis set
    normalizes to zeros, so a stored transform is always complete.
    """
    if not isinstance(value, dict):
        raise ValueError(f"an assembly_transform must be an object with translate_mm "
                         f"and/or rotate_deg; got {value!r}")
    unknown = sorted(set(value) - {"translate_mm", "rotate_deg"})
    if unknown:
        raise ValueError(f"assembly_transform keys must be translate_mm and/or rotate_deg; "
                         f"got {', '.join(unknown)}")
    return {
        "translate_mm": _vec3(value.get("translate_mm", (0, 0, 0)), "translate_mm"),
        "rotate_deg": _vec3(value.get("rotate_deg", (0, 0, 0)), "rotate_deg"),
    }


def plate_suffix(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA. Never wrap or reuse suffixes."""
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValueError("plate index must be a nonnegative integer")
    output = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        output = chr(65 + rem) + output
    return output


def project_ref(number: int | None, suffix: str | None) -> str | None:
    """Stable display alias; unknown project numbers have no invented alias."""
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        return None
    if not isinstance(suffix, str) or not re.fullmatch(r"[A-Z]+", suffix):
        return None
    return f"{number}{suffix}"


def project_id_for_intake(intake_id: str) -> str:
    """Deterministic identity for a user's one accepted intake event."""
    token = _SAFE_ID.sub("-", str(intake_id or "")).strip("-")
    if not token or len(token) > 100:
        raise ValueError("a valid intake id is required for a project")
    return f"project-{token}"


def from_files(intake_id: str, source_kind: str, sources: list[dict],
               title: str | None = None) -> dict:
    """One durable manifest, with each selected source kept as one component.

    A component is *not* a plate; the slicer may combine several into one
    plate or spread them across many. A filename alone is not a stable id.
    Stable identities here are scoped to the immutable intake and position.
    """
    if not sources:
        raise ValueError("a project requires at least one selected source file")
    if not isinstance(source_kind, str) or not source_kind:
        raise ValueError("a project source kind is required")
    pid = project_id_for_intake(intake_id)
    components = []
    for idx, row in enumerate(sources, 1):
        name = str(row.get("name") or Path(str(row.get("downloaded_file") or "")).name)
        if not name:
            raise ValueError("every project source needs a name")
        component = {
            "component_id": f"source{idx}",
            "name": name,
            "required": True,
            "quantity_required": 1,
            "source_file": str(row.get("downloaded_file") or row.get("path") or ""),
        }
        # A part that came out of a ZIP keeps its path inside the archive
        # (#1204 item 3), so "arms/left.stl" and "spare/left.stl" stay told apart.
        if row.get("archive_path"):
            component["archive_path"] = str(row["archive_path"])
        # Declared assembly data (#1017 item 3): a source that knows a
        # part's role or its placement in the assembly declares it here,
        # validated — an invalid declaration fails the manifest build
        # instead of being silently dropped. Undeclared stays absent.
        if row.get("role") is not None:
            component["role"] = validate_component_role(row["role"])
        if row.get("assembly_transform") is not None:
            component["assembly_transform"] = validate_assembly_transform(
                row["assembly_transform"])
        components.append(component)
    return {
        "schema": SCHEMA,
        "project_id": pid,
        "title": str(title or "Multi-part project"),
        "source_kind": source_kind,
        "components": components,
    }


DESIGN_SOURCE_KIND = "ai_design"


def from_design(intake_id: str, sources: list[dict], design: dict,
                title: str | None = None) -> dict:
    """Entry point for a multi-part AI design's handoff (#1204 item 4).

    The same manifest :func:`from_files` builds for uploads and links, so a
    design's parts get the same components, plates and next-plate flow. The
    design adds only where they came from: its session and turn, and each
    component's declared part name. Provenance, never an approval.
    """
    if not isinstance(design, dict) or not design.get("session_id"):
        raise ValueError("a design project needs the design session it came from")
    project = from_files(intake_id, DESIGN_SOURCE_KIND, sources,
                         title=title or design.get("title"))
    declared = {str(row.get("file")): str(row.get("part"))
                for row in design.get("parts") or []
                if isinstance(row, dict) and row.get("file") and row.get("part")}
    for component in project["components"]:
        if component["name"] in declared:
            component["design_part"] = declared[component["name"]]
    project["design"] = {"session_id": str(design["session_id"]), "turn": design.get("turn")}
    return project


def plate_info(index: int, part_keys=None) -> dict:
    """Execution-plan fields kept independent of a not-yet-allocated number."""
    return {
        "project_plate_id": f"plate{index + 1}",
        "project_plate_suffix": plate_suffix(index),
        "component_keys": [str(k) for k in (part_keys or ())],
    }


def scope_choices(is_project: bool, has_remaining: bool = False) -> tuple[str, ...]:
    """Choice semantics for reprint UX, never an authorization to start."""
    if not is_project:
        return ("This print only", "Cancel")
    out = ["This plate only", "Entire project", "Choose a plate"]
    if has_remaining:
        out.append("Remaining unprinted plates")
    out.append("Cancel")
    return tuple(out)
