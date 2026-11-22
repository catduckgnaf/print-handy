"""The build plate on a printer, and the material/plate pairs that need a warning (#1788 phase C).

Print Handy did not know which plate is on the printer: Orca's bed type stayed
at its default and nothing warned about a material/plate pair that damages a
plate. On 2026-11-21 a PETG print at 75 °C on a smooth PEI plate fused to it
and tore the coating, a well-known PETG/PEI interaction.

``[printer] build_plate`` names the plate (one of ``PLATES``). Unset means
unknown, and unknown never warns: a warning that fires on a guess teaches
people to ignore it. A warning never blocks a print (#1336); it is one line in
the preparation message.
"""
from __future__ import annotations

import re
from typing import Iterable

#: The plates a printer can carry, by setting value, with the name chat shows.
PLATES = {
    "smooth_pei": "smooth PEI",
    "textured_pei": "textured PEI",
    "smooth_pc": "smooth PC",
    "cool_plate": "cool plate",
}

#: Orca's ``curr_bed_type`` value for each plate, and the filament key that carries that plate's bed
#: temperature. Orca 2.4.2 writes "High Temp Plate" for the smooth PEI plate (its UI label "Smooth PEI
#: Plate / High Temp Plate" is not a value: Orca falls back to Cool Plate on it) and "Engineering Plate"
#: for the smooth PC plate; each bed type reads its own ``*_plate_temp`` key.
ORCA_BED_TYPES = {
    "smooth_pei": ("High Temp Plate", "hot_plate_temp"),
    "textured_pei": ("Textured PEI Plate", "textured_plate_temp"),
    "smooth_pc": ("Engineering Plate", "eng_plate_temp"),
    "cool_plate": ("Cool Plate", "cool_plate_temp"),
}


def orca_bed_type(plate: str | None) -> tuple[str, str] | None:
    """(curr_bed_type, temperature key) for a configured plate; None when unset (Orca's default stands)."""
    return ORCA_BED_TYPES.get(plate or "")


def plate_from_config(config) -> str | None:
    """The configured plate in a printer adapter's settings mapping, None when unset or unreadable."""
    try:
        return normalize(((config or {}).get("printer") or {}).get("build_plate"))
    except BuildPlateError:
        return None


# Common ways people and vendor listings name the plates (Flashforge sells the
# textured one as "PEI Powder-Coated").
_ALIASES = {
    "smooth pei": "smooth_pei", "pei smooth": "smooth_pei", "smooth": "smooth_pei",
    "high temp plate": "smooth_pei", "high temp": "smooth_pei",
    "textured pei": "textured_pei", "pei textured": "textured_pei", "textured": "textured_pei",
    "powder coated": "textured_pei", "pei powder coated": "textured_pei",
    "powder coated pei": "textured_pei",
    "smooth pc": "smooth_pc", "pc": "smooth_pc", "pc plate": "smooth_pc",
    "cool plate": "cool_plate", "cool": "cool_plate",
}


class BuildPlateError(ValueError):
    """A build plate name that is not one of PLATES or its aliases."""


def normalize(value) -> str | None:
    """The setting value for a plate name, None for unset; raises BuildPlateError for anything else."""
    if value is None:
        return None
    text = re.sub(r"[\s_\-]+", " ", str(value).strip().casefold()).replace(" plate", "").strip()
    if not text:
        return None
    key = text.replace(" ", "_")
    if key in PLATES:
        return key
    if text in _ALIASES:
        return _ALIASES[text]
    if f"{text} plate" in _ALIASES:
        return _ALIASES[f"{text} plate"]
    choices = ", ".join(sorted(PLATES))
    raise BuildPlateError(f"unknown build plate {value!r} (choose one of: {choices})")


def label(plate: str | None) -> str:
    """The plate's name in chat."""
    return PLATES.get(plate or "", "unknown plate")


# Material families that bond to a plate hard enough to damage it, and what to
# do instead. Each rule names its evidence. PETG fusing to smooth PEI is the
# common, documented case (Prusa's knowledge base and Flashforge's plate
# listings say to use a release agent or the textured sheet); PCTG behaves
# the same way.
_RULES = (
    ({"PETG", "PCTG"}, "smooth_pei",
     "{material} can fuse to smooth PEI and tear the coating. Use the textured plate, "
     "or a thin layer of glue stick on this one."),
)


def warnings(materials: Iterable[str], plate: str | None) -> list[str]:
    """One line per material/plate pair that needs a warning, in material order, without repeats."""
    from .printer_state import material_family

    if plate is None:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for material in materials or ():
        family = material_family(material) if material else ""
        if not family or family in seen:
            continue
        seen.add(family)
        for families, rule_plate, text in _RULES:
            if plate == rule_plate and family in families:
                out.append(text.format(material=family))
    return out


def plate_for(printer_id: str | None = None) -> str | None:
    """The configured build plate for a printer, or None when unset or unreadable."""
    from .. import settings

    try:
        view = settings.view_for(printer_id) if printer_id else settings.current()
        return normalize((view.get("printer") or {}).get("build_plate"))
    except Exception:
        import logging
        logging.getLogger(__name__).debug("build plate unreadable for %r", printer_id, exc_info=True)
        return None
