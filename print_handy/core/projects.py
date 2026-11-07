"""Source-neutral multi-part project identity and safe, readable plate references.

A project describes source components separately from printable *plates*: several
parts may share one G-code, and one source part may require several plates.
Do not use the human-readable 82A code as an internal primary key. Keep the
existing integer print_number registry intact for older records and commands.
"""
from __future__ import annotations

import re
from pathlib import Path

SCHEMA = "project_v1"
_SAFE_ID = re.compile(r"[^a-zA-Z0-9_-]+")


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
        components.append({
            "component_id": f"source{idx}",
            "name": name,
            "required": True,
            "quantity_required": 1,
            "source_file": str(row.get("downloaded_file") or row.get("path") or ""),
        })
    return {
        "schema": SCHEMA,
        "project_id": pid,
        "title": str(title or "Multi-part project"),
        "source_kind": source_kind,
        "components": components,
    }


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
