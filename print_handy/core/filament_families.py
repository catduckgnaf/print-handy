"""Per-family filament settings for the slicer, for printers with one PLA template and no vendor profiles.

Every in-repo and generated Orca template ships a single PLA filament profile. ``apply_family_overrides``
puts the job's material family (print_handy/data/filament_families.toml, from OrcaSlicer's Generic
profiles) on top of it: part-cooling fan, overhang fan, maximum volumetric speed, filament retraction,
the slicer's temperature range and the glass-transition temperature. Print Handy's own per-spool values
(temperatures, flow, density, colour) are applied after this and always win.

A template that already is the job's family keeps its tuned values; the family row only fills what it
lacks. Bambu Lab never reaches this: its adapter selects a checked vendor profile per material.
"""
from __future__ import annotations

import copy
import tomllib
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "filament_families.toml"
_REINFORCED = ("-CF", "-GF", "-CARBONFIBER", "-GLASSFIBER")
_RANGE_LOW, _RANGE_HIGH = "nozzle_temperature_range_low", "nozzle_temperature_range_high"


class FilamentFamilyError(RuntimeError):
    """filament_families.toml is missing, unreadable or not a table of scalar settings."""


@lru_cache(maxsize=1)
def _families() -> dict[str, dict[str, list[str]]]:
    try:
        with DATA.open("rb") as source:
            rows = tomllib.load(source)["families"]
        result = {}
        for family, row in rows.items():
            if not row or any(isinstance(v, (bool, list, dict)) for v in row.values()):
                raise ValueError(f"{family}: expected a table of scalar settings")
            result[family] = {key: [value if isinstance(value, str) else format(value, "g")]
                              for key, value in row.items()}
        return result
    except (OSError, KeyError, TypeError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise FilamentFamilyError(f"invalid filament family configuration in {DATA}: {exc}") from exc


def family_row(family: str) -> dict[str, list[str]] | None:
    """The Orca settings for a material family (PLA, PETG-CF, ...), or None when there is no row.

    A reinforced family without its own row (ABS-CF, PA-GF, ...) uses its base polymer's row.
    """
    rows, family = _families(), str(family or "")
    if family in rows:
        return copy.deepcopy(rows[family])
    for suffix in _REINFORCED:
        if family.endswith(suffix) and family[:-len(suffix)] in rows:
            return copy.deepcopy(rows[family[:-len(suffix)]])
    return None


def _number(value):
    try:
        return float(value[0] if isinstance(value, list) else value)
    except (TypeError, ValueError, IndexError):
        return None


def apply_family_overrides(template: dict, material: str, nozzle_temp=None) -> tuple[dict, str]:
    """(filament profile for material, note) from a printer template's filament profile.

    The template is not modified. ``nozzle_temp`` is the nozzle target the job will print at; the
    profile's range is widened to include it. The note says what happened, for the slice log.
    """
    from .printer_state import material_family
    family = material_family(material)
    own = material_family((template.get("filament_type") or [""])[0])
    row = family_row(family)
    data = copy.deepcopy(template)
    if row is None:
        note = f"no filament family row for {material!r}; the printer template's {own or 'own'} settings are used"
    elif family == own:
        for key, value in row.items():
            data.setdefault(key, value)
        note = f"{family} matches the printer template; its tuned settings are kept"
    else:
        data.update(row)
        note = f"{family} settings applied over the printer's {own or 'template'} filament profile"
    include_nozzle_temperature(data, nozzle_temp)
    return data, note


def include_nozzle_temperature(data: dict, nozzle_temp) -> None:
    """Widen an existing slicer range to include the integer target sent to Orca."""
    if nozzle_temp is None:
        return
    target = int(nozzle_temp)
    low, high = _number(data.get(_RANGE_LOW)), _number(data.get(_RANGE_HIGH))
    if low is not None and target < low:
        data[_RANGE_LOW] = [str(target)]
    if high is not None and target > high:
        data[_RANGE_HIGH] = [str(target)]
