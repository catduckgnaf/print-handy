"""Remap integrity checks for cross-slicer 3MF work (#1525, tranche C).

One validation pass over a source and a proposed filament remap, built
on the existing inventory (`source_colors`) and package rules
(`package_remap`) rather than a second reader. It answers, before any
byte is rewritten:

* roles survive: a modifier is never a printable part, a negative
  volume stays a subtraction, support blockers and enforcers are named;
* painted identity is present or absent as a fact, never assumed;
* per-filament arrays are handled atomically: an array the remapper
  does not know is named as an unknown passthrough, and a palette whose
  color and material lists disagree in length is a problem, not a guess;
* plate selection names only plates the source actually has;
* logical filament, material, physical slot and toolhead are tracked
  as four separate facts — a five-color source on four tools is a
  finding, not a silent collapse;
* unknown references (a decision or mapping entry naming a filament
  the source does not have) are rejected, never adopted.
"""
from __future__ import annotations

import json
import logging
import zipfile
from pathlib import Path

from . import package_remap, source_colors, source_descriptor

logger = logging.getLogger(__name__)

SUPPORT_SUBTYPES = ("support_blocker", "support_enforcer")
NON_PRINTING_SUBTYPES = ("modifier_part", "negative_part") + SUPPORT_SUBTYPES


def plate_ids(manifest: dict) -> list:
    """Plate ids a manifest knows, from parts or per-plate usage."""
    found = []
    for part in manifest.get("parts") or ():
        plate = part.get("plate_id") or part.get("plate")
        if plate is not None and plate not in found:
            found.append(plate)
    for plate in (manifest.get("plate_usage") or {}):
        if plate not in found:
            found.append(plate)
    return found


def tracking(manifest: dict, decisions: dict | None = None,
             tool_count: int | None = None) -> list[dict]:
    """Logical filament / material / physical slot / toolhead, per filament.

    ``decisions`` maps a source filament number to a mapping with a
    ``slot`` (physical slot) when the operator has chosen one. A slot is
    never invented here: unchosen stays None. The toolhead is the slot's
    head only when the printer model says slots and heads coincide
    (``tool_count`` given and equal to the slot count the caller passes
    via decisions); otherwise it stays None rather than pretending a
    feeder slot is an independent head.
    """
    decisions = decisions or {}
    rows = []
    for filament in manifest.get("filaments") or ():
        number = filament.get("source_extruder")
        choice = decisions.get(number) or decisions.get(str(number)) or {}
        slot = choice.get("slot") if isinstance(choice, dict) else choice
        rows.append({
            "logical_filament": number,
            "material": filament.get("material"),
            "color": filament.get("color"),
            "physical_slot": slot,
            "toolhead": None,  # filled only by a printer model that proves it
        })
    return rows


def validate(source, mapping=None, selected_plate_ids=None,
             decisions=None) -> dict:
    """Validate a proposed remap/selection against a source 3MF.

    Returns {"ok", "problems", "warnings", "roles", "tracking",
    "descriptor"}. ``ok`` is False only for a named problem; warnings
    never block (filament conditions are warnings the operator can
    accept, per the standing ruling).
    """
    source = Path(source)
    problems: list[str] = []
    warnings: list[str] = []
    descriptor = source_descriptor.describe(source)
    if descriptor["user_class"] == "unsupported-corrupt":
        problems.append("the source is not a readable, usable 3MF: "
                        + "; ".join(descriptor["problems"] or ["unknown damage"]))
    try:
        manifest = source_colors.inventory(source)
    except Exception as exc:  # inventory refuses damaged packages by name
        problems.append(f"source inventory failed: {exc}")
        manifest = {"parts": [], "filaments": [], "painted": False,
                    "plate_usage": {}}
    parts = list(manifest.get("parts") or [])
    # The inventory separates non-printing volumes into `cutters`: a
    # modifier, negative volume or support blocker/enforcer is never in
    # `parts`, by design. Roles are read across both lists.
    all_volumes = parts + list(manifest.get("cutters") or [])
    roles = {
        "modifiers": [p["key"] for p in all_volumes if p.get("subtype") == "modifier_part"],
        "negative_parts": [p["key"] for p in all_volumes if p.get("subtype") == "negative_part"],
        "support_blockers": [p["key"] for p in all_volumes if p.get("subtype") == "support_blocker"],
        "support_enforcers": [p["key"] for p in all_volumes if p.get("subtype") == "support_enforcer"],
        "printable_parts": [p["key"] for p in parts
                            if p.get("subtype", "normal_part") == "normal_part"],
    }
    # A modifier or negative volume can never be a printable part: the
    # inventory keeps subtypes distinct, and this pass fails loudly if a
    # future reader ever merges them.
    overlap = set(roles["printable_parts"]) & (
        set(roles["modifiers"]) | set(roles["negative_parts"]))
    if overlap:
        problems.append(f"non-printing volumes listed as printable: {sorted(overlap)}")
    try:
        with zipfile.ZipFile(source) as archive:
            negatives = source_colors.negative_volumes(archive)
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        # An unreadable archive here only loses the negative-volume
        # warning; the descriptor pass already refuses damaged archives.
        logger.info("negative-volume scan skipped for %s: %s", Path(source).name, exc)
        negatives = []
    if negatives and not roles["negative_parts"]:
        warnings.append(
            f"{len(negatives)} negative volume(s) ride as model volumes; they "
            "stay subtractions in any remap or the remap refuses by name")
    if manifest.get("painted"):
        warnings.append("the source carries painted color identity; a remap must "
                        "translate paint states, never carry them verbatim")
    palette = [f.get("source_extruder") for f in manifest.get("filaments") or ()]
    # Per-filament arrays: schema-verified, atomic.
    if source.suffix.lower() == ".3mf" and zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            if "Metadata/project_settings.config" in archive.namelist():
                try:
                    settings = json.loads(archive.read("Metadata/project_settings.config"))
                except ValueError:
                    settings = None
                    problems.append("project_settings.config does not parse as JSON")
                if isinstance(settings, dict):
                    colors = settings.get("filament_colour")
                    types = settings.get("filament_type")
                    if (isinstance(colors, list) and isinstance(types, list)
                            and len(colors) != len(types)):
                        problems.append(
                            "filament_colour and filament_type disagree in length "
                            f"({len(colors)} vs {len(types)}); the palette is not "
                            "schema-verified, so no remap may proceed on it")
                    for key in package_remap.unknown_palette_arrays(
                            settings, len(palette)):
                        warnings.append(
                            f"per-filament-shaped array {key!r} is not a known "
                            "filament array; it passes through as authored and is "
                            "never permuted on a length guess")
    if mapping is not None:
        try:
            normalized = package_remap.validate_mapping(mapping)
        except RuntimeError as exc:
            problems.append(str(exc))
            normalized = {}
        for number in normalized:
            if palette and number not in palette:
                problems.append(
                    f"the mapping names source filament {number}, which the "
                    "source palette does not have; unknown references are "
                    "rejected, not adopted")
    if decisions:
        for number in decisions:
            try:
                known = int(number) in palette
            except (TypeError, ValueError):
                known = False
            if palette and not known:
                problems.append(
                    f"a decision names source filament {number!r}, which the "
                    "source palette does not have; it is rejected, not adopted")
    known_plates = plate_ids(manifest)
    if selected_plate_ids is not None:
        for plate in selected_plate_ids:
            if known_plates and plate not in known_plates:
                problems.append(
                    f"selected plate {plate!r} is not a plate of this source "
                    f"(plates: {known_plates}); the selection is rejected, "
                    "not adopted")
    rows = tracking(manifest, decisions)
    used_slots = [r["physical_slot"] for r in rows if r["physical_slot"] is not None]
    if len(set(used_slots)) != len(used_slots):
        warnings.append("two logical filaments share one physical slot; that is "
                        "a warning to accept, never a silent merge")
    return {"ok": not problems, "problems": problems, "warnings": warnings,
            "roles": roles, "tracking": rows, "descriptor": descriptor}
