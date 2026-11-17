"""Machine-readable cross-slicer capability manifest for issue #1525.

The manifest (`source_capabilities.json`, beside this module) is the one
place that records, per source/destination cell, what Print Handy claims,
which fixture backs the claim, which module owns the behavior, what
evidence level has actually been executed, and what gap remains. Docs and
reports read it instead of restating capability claims in prose, so the
claims cannot drift apart.

Evidence levels, weakest to strongest: ``unverified``, ``mocked-parse``,
``spec-synthetic``, ``real-destination-slice``, ``genuine-producer``.
A level is only honest when an executed test proves it; this module never
upgrades a level, it only reports what the manifest records.
"""
from __future__ import annotations

import json
from pathlib import Path

MANIFEST_PATH = Path(__file__).with_name("source_capabilities.json")

EVIDENCE_LEVELS = ("unverified", "mocked-parse", "spec-synthetic",
                   "real-destination-slice", "genuine-producer")

SOURCE_KINDS = ("bambu", "orca", "prusa", "cura", "anycubic",
                "generic-core", "gcode-only", "unknown")


def load_manifest() -> dict:
    """The parsed capability manifest; raises on a missing or broken file."""
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def cells() -> tuple:
    return tuple(load_manifest().get("cells") or ())


def gaps() -> tuple:
    return tuple(load_manifest().get("gaps") or ())


def cell_for(source: str, destination: str | None = None) -> dict | None:
    """The manifest cell for a source kind (and destination, when given)."""
    for cell in cells():
        if cell.get("source") != source:
            continue
        if destination is not None and cell.get("destination") != destination:
            continue
        return cell
    return None


def validate_manifest(manifest: dict | None = None) -> list[str]:
    """Structural problems with the manifest; [] when it is well formed.

    Every cell needs an id, a known source kind, an evidence level from
    EVIDENCE_LEVELS, an owner module and a fixture reference. A cell that
    claims genuine-producer evidence must name its gap as None only when
    the fixture provenance manifest backs it; that cross-check lives in
    the tests, which can read the corpus.
    """
    manifest = load_manifest() if manifest is None else manifest
    problems = []
    if manifest.get("schema_version") != 1:
        problems.append("manifest schema_version must be 1")
    seen = set()
    for cell in manifest.get("cells") or ():
        cid = cell.get("id")
        if not cid or cid in seen:
            problems.append(f"cell id missing or duplicated: {cid!r}")
        seen.add(cid)
        if cell.get("source") not in SOURCE_KINDS + ("any",):
            problems.append(f"cell {cid}: unknown source kind {cell.get('source')!r}")
        if cell.get("evidence") not in EVIDENCE_LEVELS:
            problems.append(f"cell {cid}: unknown evidence level {cell.get('evidence')!r}")
        for field in ("owner", "fixture", "feature", "destination"):
            if not cell.get(field):
                problems.append(f"cell {cid}: missing {field}")
    for gap in manifest.get("gaps") or ():
        for field in ("id", "cell", "gap", "owner"):
            if not gap.get(field):
                problems.append(f"gap {gap.get('id')!r}: missing {field}")
    return problems


def gap_table() -> list[dict]:
    """The explicit gap table: every recorded gap, with its owning cell."""
    by_id = {cell.get("id"): cell for cell in cells()}
    rows = []
    for gap in gaps():
        cell = by_id.get(gap.get("cell")) or {}
        rows.append({"gap_id": gap.get("id"), "cell": gap.get("cell"),
                     "source": cell.get("source"), "destination": cell.get("destination"),
                     "evidence": cell.get("evidence"), "gap": gap.get("gap"),
                     "owner": gap.get("owner")})
    # Cells that carry an inline gap but no gap-table row still surface.
    covered = {row["cell"] for row in rows}
    for cell in cells():
        if cell.get("gap") and cell.get("id") not in covered:
            rows.append({"gap_id": None, "cell": cell.get("id"),
                         "source": cell.get("source"),
                         "destination": cell.get("destination"),
                         "evidence": cell.get("evidence"), "gap": cell.get("gap"),
                         "owner": cell.get("owner")})
    return rows
