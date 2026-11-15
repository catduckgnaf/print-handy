"""Display-only summaries and sanity warnings for AFC backup lanes.

This module never constructs or sends printer commands. Hardware semantics
remain unverified; callers should present these as advisory warnings only.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from . import afc


def _material(value: Any) -> str:
    return " ".join(str(value or "").split())


def _family(value: str) -> str:
    # Reuse the application's established material vocabulary so variants
    # such as PLA-CF compare consistently with the file material.
    from ...core.printer_state import material_family
    return material_family(value) if value else ""


def _known_material(value: str) -> bool:
    family = _family(value)
    return family in {"PLA", "PETG", "PCTG", "ABS", "ASA", "TPU", "PA", "PC",
                      "PLA-CF", "PLA-GF", "PETG-CF", "PETG-GF", "PA-CF", "PA-GF",
                      "PC-CF", "PC-GF", "SUPPORT-PLA", "SUPPORT-PA"}


def _has_filament(lane: Mapping[str, Any]) -> bool:
    """AFC's reported load switch; material metadata is not filament evidence."""
    return lane.get("load") is True


def summarize_backup_lanes(status: Any, used_tools: Iterable[int],
                           tool_materials: Mapping[int, str] | None = None) -> dict[str, Any]:
    """Return display rows and advisory warnings for used tools.

    Lane and tool identities come only from ``afc.resolve_tool_map`` and
    ``afc.parse_lanes``. Missing or ambiguous maps are reported, never
    guessed. ``tool_materials`` maps file tool numbers to sliced material
    labels; comparison is by the shared material-family normalizer.
    """
    materials = tool_materials if isinstance(tool_materials, Mapping) else {}
    resolved, map_problems = afc.resolve_tool_map(status)
    lanes = afc.parse_lanes(status)
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    if map_problems:
        warnings.extend(f"Backup-lane map unavailable: {problem}" for problem in map_problems)
    for tool in sorted({int(t) for t in used_tools}):
        mapping = resolved.get(tool) if resolved else None
        source_name = mapping.get("lane") if mapping else None
        source_lane = lanes.get(source_name) if lanes and source_name else None
        target = _material(source_lane.get("runout_lane")) if source_lane else ""
        file_material = _material(materials.get(tool)) or (_material(source_lane.get("material")) if source_lane else "")
        row = {"tool": tool, "lane": source_name, "runout_lane": target or None,
               "material": _material(source_lane.get("material")) if source_lane else "",
               "backup_material": ""}
        target_lane = lanes.get(target) if lanes and target else None
        if not target:
            warnings.append(f"T{tool}: no runout_lane target is configured")
        elif target == source_name:
            warnings.append(f"T{tool}: runout_lane points to the same lane ({target})")
        elif target_lane is None:
            warnings.append(f"T{tool}: runout_lane target {target} is not in AFC's lane list or has no status")
        else:
            row["backup_material"] = _material(target_lane.get("material"))
            if not _has_filament(target_lane):
                warnings.append(f"T{tool}: backup lane {target} has no filament confirmed (AFC load is false or unknown)")
            backup_material = row["backup_material"]
            if not backup_material:
                warnings.append(f"T{tool}: backup lane {target} material is unknown")
            if backup_material and not _known_material(backup_material):
                warnings.append(f"T{tool}: backup lane {target} has unknown material {backup_material!r}")
            if file_material and not _known_material(file_material):
                warnings.append(f"T{tool}: file material {file_material!r} is unknown")
            if file_material and backup_material and _known_material(file_material) and _known_material(backup_material):
                if _family(file_material) != _family(backup_material):
                    warnings.append(f"T{tool}: backup lane {target} material {backup_material!r} "
                                    f"does not match file material {file_material!r}")
        rows.append(row)
    return {"rows": rows, "warnings": warnings, "display_only": True,
            "hardware_verified": False}
