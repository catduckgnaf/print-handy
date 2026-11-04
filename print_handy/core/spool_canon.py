"""Canonical spool records: one spelling per material, color and brand.

Spoolman is the record. Print Handy only writes into it through these rules so
two entry paths (Discord walkthrough, ``/spool new``, a runout replacement)
never mint "PLA+" next to "pla plus", "Black PLA" next to "Black", or a second
copy of a filament type that already exists under a different case.

Nothing here talks to Spoolman. ``canonical_spec`` cleans a new-spool spec;
``find_filament`` picks the existing filament type it should reuse;
``audit_filaments`` lists the cleanup a person should review in Spoolman.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from . import filament_color

# The material menu every picker offers, in order. "Other" stays available but
# must still resolve to a known family (printer_state.material_family).
MATERIALS = ("PLA", "PLA+", "Silk PLA", "Matte PLA", "PETG", "PCTG", "TPU", "ABS", "ASA", "PA", "PC")

# Pickers ask the primary type first, then its subtypes (PLA -> PLA+, Silk PLA,
# Matte PLA). A primary with one subtype skips the second question. Every
# subtype is a MATERIALS entry, so a picker can never mint a new material.
MATERIAL_TREE = {
    "PLA": ("PLA", "PLA+", "Silk PLA", "Matte PLA"),
    "PETG": ("PETG",),
    "PCTG": ("PCTG",),
    "TPU": ("TPU",),
    "ABS": ("ABS",),
    "ASA": ("ASA",),
    "PA": ("PA",),
    "PC": ("PC",),
}
PRIMARY_LABELS = {"PA": "PA (Nylon)"}


def primary_material(value: Any) -> str | None:
    """The primary type a material belongs to (\"Silk PLA\" -> \"PLA\"), or None."""
    canon = canonical_material(value)
    for primary, subtypes in MATERIAL_TREE.items():
        if canon in subtypes:
            return primary
    return None


def material_subtypes(primary: Any) -> tuple[str, ...]:
    """The subtypes offered under one primary type (empty for an unknown one)."""
    return MATERIAL_TREE.get(str(primary or "").strip().upper(), ())


_MATERIAL_ALIASES = {
    "PLA": "PLA", "PLA+": "PLA+", "PLAPLUS": "PLA+", "PLAPRO": "PLA+", "PRO PLA": "PLA+", "PLA PRO": "PLA+",
    "PLA PLUS": "PLA+", "SILK": "Silk PLA", "SILK PLA": "Silk PLA", "PLA SILK": "Silk PLA",
    "SILK PLA+": "Silk PLA", "MATTE PLA": "Matte PLA", "PLA MATTE": "Matte PLA", "MATTE PLA+": "Matte PLA",
    "PETG": "PETG", "PET-G": "PETG", "PCTG": "PCTG", "TPU": "TPU", "TPU 95A": "TPU", "ABS": "ABS",
    "ASA": "ASA", "PA": "PA", "NYLON": "PA", "PC": "PC",
}

# PLA and PLA+ swap freely at changeover and failover; finishes do not (a silk
# spool looks different), so Silk/Matte stay their own groups.
_SWAP_GROUP = {"PLA": "PLA", "PLA+": "PLA"}

_BRAND_ALIASES = {"SUNLU": "SUNLU", "ELEGOO": "ELEGOO", "ESUN": "eSUN", "POLYMAKER": "Polymaker",
                  "BAMBU": "Bambu Lab", "BAMBU LAB": "Bambu Lab", "OVERTURE": "Overture",
                  "HATCHBOX": "Hatchbox", "AIROK": "Airok", "LOCYFENS": "LOCYFENS", "GEEETECH": "Geeetech",
                  "JAYO": "JAYO", "KINGROON": "Kingroon", "ANYCUBIC": "Anycubic", "CREALITY": "Creality",
                  "FLASHFORGE": "Flashforge", "PRUSAMENT": "Prusament"}
_NO_BRAND = {"", "UNKNOWN", "GENERIC", "NONE", "N/A", "NA", "-", "NO BRAND", "OTHER"}

_SPACE = re.compile(r"\s+")


def _squash(value: Any) -> str:
    return _SPACE.sub(" ", str(value or "").strip())


def canonical_material(value: Any) -> str | None:
    """The menu spelling of a material label, or None when it is not one we know."""
    text = _squash(value).upper().replace("_", " ")
    if not text:
        return None
    if text in _MATERIAL_ALIASES:
        return _MATERIAL_ALIASES[text]
    compact = text.replace(" ", "").replace("-", "")
    for key, canon in _MATERIAL_ALIASES.items():
        if compact == key.replace(" ", "").replace("-", ""):
            return canon
    return None


def swap_group(material: Any) -> str | None:
    """The changeover group: PLA and PLA+ share one, every other material is its own."""
    canon = canonical_material(material)
    return _SWAP_GROUP.get(canon, canon) if canon else None


def canonical_brand(value: Any) -> str | None:
    """Known brands in their own spelling; unknown brands title-cased; no-brand words become None."""
    text = _squash(value)
    if text.upper() in _NO_BRAND:
        return None
    return _BRAND_ALIASES.get(text.upper(), text if any(c.isupper() for c in text[1:]) else text.title())


def canonical_color(color: Any, material: Any = None, brand: Any = None) -> str:
    """The color/finish name with the material words and brand noise taken out.

    "Black PLA" -> "Black", "rose red blue green" -> "Rose Red Blue Green",
    "PLA+ Matte White" -> "Matte White". A name that is only a material keeps it.
    """
    text = _squash(color)
    canon = canonical_material(material) or ""
    words = text.split(" ")
    drop = {w.upper() for w in canon.split(" ")} | {"PLA", "PLA+", "PETG", "PCTG", "TPU", "ABS", "ASA", "FILAMENT"}
    drop |= {w.upper() for w in _squash(brand).split(" ") if w}
    kept = [w for w in words if w.upper().strip("()") not in drop]
    if not kept:
        kept = words
    out = " ".join(kept)
    return " ".join(w if (w.isupper() and len(w) > 1) else w.capitalize() for w in out.split(" "))


class SpecError(ValueError):
    pass


def canonical_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """A new-spool spec with material, color and brand in their canonical spelling.

    Refuses an unknown material and a color that is empty or only punctuation, so a
    free-text slip never becomes a Spoolman filament type.
    """
    material = canonical_material(spec.get("material"))
    if material is None:
        raise SpecError(f"unknown material {spec.get('material')!r}; pick one of: {', '.join(MATERIALS)}")
    color = canonical_color(spec.get("color"), material, spec.get("brand"))
    if not re.search(r"[A-Za-z]", color):
        raise SpecError("a color or finish name is required (e.g. Black, Matte White)")
    out = dict(spec)
    out.update(material=material, color=color, brand=canonical_brand(spec.get("brand")))
    return out


def _filament_key(row: Mapping[str, Any]) -> tuple[str, str, str] | None:
    material = canonical_material(row.get("material"))
    if material is None:
        return None
    brand = canonical_brand((row.get("vendor") or {}).get("name")) or ""
    return brand.casefold(), material.casefold(), canonical_color(row.get("name"), material, (row.get("vendor") or {}).get("name")).casefold()


def find_filament(filaments: Iterable[Mapping[str, Any]], spec: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The existing filament type a canonical spec should reuse, lowest id first.

    Compares canonical forms, so "SUNLU / PLA+ / black" reuses "SUNLU / PLA+ / Black".
    PLA and PLA+ are deliberately NOT merged here: they are different products on
    the record even though they swap at changeover.
    """
    want = ((spec.get("brand") or "").casefold(), str(spec["material"]).casefold(), str(spec["color"]).casefold())
    rows = sorted((r for r in filaments or [] if isinstance(r, Mapping) and r.get("id") is not None),
                  key=lambda r: int(r["id"]))
    return next((r for r in rows if _filament_key(r) == want), None)


def changeover_compatible(material_a: Any, color_a: Any, hex_a: Any,
                          material_b: Any, color_b: Any, hex_b: Any) -> bool:
    """Whether spool B may stand in for spool A at a runout or failover.

    Same swap group (PLA == PLA+), and the same color: the canonical color names
    match, or both hexes are the same to the eye (CIE76 within SAME_DELTA_E, which
    treats 000000 / 161616 / 1A1A1A black as one).
    """
    group_a, group_b = swap_group(material_a), swap_group(material_b)
    if group_a is None or group_a != group_b:
        return False
    name_a, name_b = canonical_color(color_a, material_a), canonical_color(color_b, material_b)
    if name_a and name_a.casefold() == name_b.casefold():
        return True
    if filament_color.same_color(hex_a, hex_b) is True:
        return True
    tone_a, tone_b = _neutral_extreme(hex_a), _neutral_extreme(hex_b)
    return tone_a is not None and tone_a == tone_b


def _neutral_extreme(value: Any) -> str | None:
    """"black" or "white" for a hex every vendor would sell under that name, else None.

    Vendors' black runs 000000..242424 and white E0E0E0..FFFFFF; both sit outside
    SAME_DELTA_E of each other (000000 vs 161616 is 7.2) yet print the same. Only
    near-neutral extremes qualify, so navy or cream never pass as black or white.
    """
    hexed = filament_color.normalize_hex(value)
    if not hexed:
        return None
    r, g, b = (int(hexed[i:i + 2], 16) for i in (0, 2, 4))
    if max(r, g, b) - min(r, g, b) > 0x10:
        return None
    if max(r, g, b) <= 0x24:
        return "black"
    if min(r, g, b) >= 0xE0:
        return "white"
    return None


def audit_filaments(filaments: Iterable[Mapping[str, Any]], spools: Iterable[Mapping[str, Any]] = ()) -> list[str]:
    """Human-readable cleanup findings for the Spoolman filament list. Read only."""
    rows = [r for r in filaments or [] if isinstance(r, Mapping) and r.get("id") is not None]
    used = {int(((s.get("filament") or {}).get("id")) or 0) for s in spools or [] if isinstance(s, Mapping)}
    notes = []
    groups: dict[tuple[str, str, str], list[int]] = {}
    for r in rows:
        fid = int(r["id"])
        material = canonical_material(r.get("material"))
        if material is None:
            notes.append(f"#{fid}: material {r.get('material')!r} is not on the menu")
            continue
        if material != r.get("material"):
            notes.append(f"#{fid}: material {r.get('material')!r} -> {material!r}")
        color = canonical_color(r.get("name"), material, (r.get("vendor") or {}).get("name"))
        if color != r.get("name"):
            notes.append(f"#{fid}: name {r.get('name')!r} -> {color!r}")
        brand = canonical_brand((r.get("vendor") or {}).get("name"))
        if (r.get("vendor") or {}).get("name") and brand is None:
            notes.append(f"#{fid}: vendor {(r.get('vendor') or {}).get('name')!r} is a placeholder")
        if not filament_color.normalize_hex(r.get("color_hex")):
            notes.append(f"#{fid}: no color hex")
        key = _filament_key(r)
        if key is not None:
            groups.setdefault(key, []).append(fid)
    for key, ids in groups.items():
        if len(ids) > 1:
            spare = [i for i in ids[1:] if i not in used]
            notes.append(f"duplicate filament types {ids} ({key[1]} {key[2]}); unused copies: {spare or 'none'}")
    for r in rows:
        if int(r["id"]) not in used and rows:
            notes.append(f"#{int(r['id'])}: no spools use this type")
    return notes
