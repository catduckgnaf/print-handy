"""Snapmaker U1 multicolor feasibility report (#1525, tranche I).

Read-only. For a project whose logical filaments exceed the U1's four
independent toolheads, this report says what the source itself proves:
how many logical filaments each plate uses, whether painting or height
changes put several filaments into the same plate's layers, and whether
a support or interface filament adds to the count. It plans no swaps,
collapses no colors, and claims nothing about hardware: no U1 has
printed through Print Handy, and this report is not a print plan.

Same-layer analysis basis, stated honestly: the inventory's per-plate
usage classes (painting, layer changes, height ranges) prove several
filaments are used *within* a plate's layers; they do not prove which
exact layer pairs coincide. The report therefore says "same plate,
interleaved layers" where that is what the evidence shows, and never
presents a layer-exact toolpath analysis it did not run.
"""
from __future__ import annotations

from pathlib import Path

from . import source_colors

U1_TOOL_COUNT = 4


def feasibility(source, *, tool_count=U1_TOOL_COUNT) -> dict:
    """The feasibility report for one source on a tool-count-limited head.

    ``tool_count`` is the destination's physical tool count (4 for the
    U1). The report is feasible only when every plate's logical
    filament count fits the tools; support and interface designations
    count toward a plate's total when they name a filament no part
    prints with.
    """
    manifest = source_colors.inventory(Path(source))
    palette = [f.get("source_extruder") for f in manifest.get("filaments") or ()]
    plates = []
    problems = []
    for plate_id, usage in sorted((manifest.get("plate_usage") or {}).items()):
        used = list(usage.get("source_filaments") or [])
        classes = usage.get("classes") or {}
        interleaved = any(
            (classes.get(name) or {}).get("source_filaments")
            for name in ("painting", "layer_changes", "height_ranges"))
        support = [f for f in ((manifest.get("support_filaments") or {}).values())
                   if f and f not in used]
        total = len(used) + len(support)
        row = {"plate": plate_id, "logical_filaments": used,
               "support_only_filaments": support,
               "filament_count": total, "fits_tools": total <= tool_count,
               "interleaved_layers": bool(interleaved),
               "usage_known": bool(usage.get("usage_known"))}
        plates.append(row)
        if total > tool_count:
            problems.append(
                f"plate {plate_id} uses {total} logical filaments "
                f"({len(used)} printing, {len(support)} support-only) on "
                f"{tool_count} physical tools"
                + ("; several filaments interleave within the plate's layers, "
                   "so a layer-boundary swap cannot separate them"
                   if interleaved else ""))
        if not usage.get("usage_known"):
            problems.append(
                f"plate {plate_id} filament usage is not fully known from "
                "the source; feasibility for it cannot be proven")
    return {"schema_version": 1, "destination": "snapmaker.u1",
            "tool_count": tool_count, "palette_size": len(palette),
            "plates": plates, "problems": problems,
            "feasible": not problems,
            "hardware_status": "unverified — no U1 has printed through "
                               "Print Handy; this report is not a print plan",
            "note": "No colors were collapsed and no swap was planned. "
                    "A plate over the tool count is reported, not fixed."}


def render_text(report: dict) -> str:
    lines = [f"U1 feasibility: {'feasible on paper' if report['feasible'] else 'not feasible as authored'} "
             f"({report['palette_size']} logical filaments, {report['tool_count']} tools)"]
    for row in report["plates"]:
        lines.append(
            f"- plate {row['plate']}: {row['filament_count']} filaments "
            f"({', '.join(str(f) for f in row['logical_filaments']) or 'none'}"
            + (f"; support-only {row['support_only_filaments']}" if row["support_only_filaments"] else "")
            + (", interleaved layers" if row["interleaved_layers"] else "")
            + ("" if row["fits_tools"] else " — exceeds the tool count"))
    for problem in report["problems"]:
        lines.append(f"Problem: {problem}")
    lines.append(report["hardware_status"] + ".")
    return "\n".join(lines) + "\n"
