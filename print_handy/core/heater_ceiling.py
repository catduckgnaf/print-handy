"""The one heater ceiling every Print Handy temperature path asks (#1147).

A target above the ceiling is refused, never clamped: the G-code
mechanical-limit check at print-now, the panic purge, the OctoPrint
preheat and a macro run with temperature arguments all call ``ceiling``
(or ``refusal``) here, so a printer has one answer to "how hot may this
heater go" whichever path sends the number.

The ceiling for each heater comes from the printer profile:

- ``nozzle``: the fitted nozzle type's safe maximum (``data/nozzles.toml``),
  lowered to the model's ``max_hotend_c`` fact when the profile has one.
  The type is the hardware record's, else the configured one, else the
  default (hardened steel); ``stock`` resolves per model (nozzle.py) and an
  unrecognized type counts as brass, the softest.
- ``bed``: the model's ``max_bed_c`` fact, else the hottest bed any
  configured material asks for.
- ``chamber``: the model's ``max_chamber_c`` fact, else
  ``UNKNOWN_HEATER_CEILING_C``.
- anything else: ``UNKNOWN_HEATER_CEILING_C``.

The firmware's own ``max_temp`` stays the real hardware limit; this is the
ceiling Print Handy refuses to send above.
"""
from __future__ import annotations

from .. import settings

HEATERS = ("nozzle", "bed", "chamber")
# A named heater the profile says nothing about: conservative, warm-only.
UNKNOWN_HEATER_CEILING_C = 60.0

# Macro argument names that carry a heater target. Shared with the G-code
# scan in model_job (START_PRINT/PRINT_START arguments) and the macro run
# guard (print_handy.macros), so both read the same words the same way.
MACRO_NOZZLE_ARGS = frozenset({"EXTRUDER_TEMP", "HOTEND", "HOTEND_TEMP", "EXTRUDER", "NOZZLE", "NOZZLE_TEMP",
                               "TEMP", "TOOL_TEMP", "PRINT_TEMP", "EXTRUDER_TEMPERATURE", "NOZZLE_TEMPERATURE"})
MACRO_BED_ARGS = frozenset({"BED_TEMP", "BED", "BED_TEMPERATURE", "HEATER_BED", "PLATE_TEMP"})
MACRO_CHAMBER_ARGS = frozenset({"CHAMBER_TEMP", "CHAMBER", "CHAMBER_TEMPERATURE"})


class HeaterCeilingError(ValueError):
    """A requested heater target above this printer's ceiling."""


def _fact(adapter, name):
    fact = getattr(adapter, "fact", None)
    if not callable(fact):
        return None
    try:
        value = fact(name)
    except Exception:  # a model without that fact has no such limit
        return None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _view(adapter, view):
    if view is not None:
        return view
    config = getattr(adapter, "config", None)
    return config if isinstance(config, dict) else settings.current()


def nozzle_type(state=None, view=None):
    """The fitted nozzle type a ceiling applies to: the hardware record's, else the configured one."""
    recorded = ((state or {}).get("nozzle") or {}).get("type") if isinstance(state, dict) else None
    if isinstance(recorded, str) and recorded.strip():
        return recorded
    from .nozzle import DEFAULT_NOZZLE_TYPE
    return ((view or {}).get("printer") or {}).get("nozzle_type") or DEFAULT_NOZZLE_TYPE


def ceiling(adapter, heater, *, state=None, view=None):
    """The highest target (°C) this printer's profile allows for ``heater``."""
    view = _view(adapter, view)
    if heater == "nozzle":
        from . import nozzle as _nozzle
        try:
            canonical = _nozzle.normalize_nozzle_type(nozzle_type(state, view))
        except _nozzle.NozzleError:
            canonical = _nozzle.normalize_nozzle_type("brass")
        limit = float(_nozzle._nozzle_type_defaults()[canonical]["max_temperature_c"])
        model = _fact(adapter, "max_hotend_c")
        return min(limit, model) if model is not None else limit
    if heater == "bed":
        bed = _fact(adapter, "max_bed_c")
        if bed is not None:
            return bed
        temps = {**settings.DEFAULT_MATERIAL_TEMPS, **((view or {}).get("material_temps") or {})}
        return float(max(row["bed"] for row in temps.values()
                         if isinstance(row, dict) and isinstance(row.get("bed"), (int, float))))
    if heater == "chamber":
        chamber = _fact(adapter, "max_chamber_c")
        return chamber if chamber is not None else UNKNOWN_HEATER_CEILING_C
    return UNKNOWN_HEATER_CEILING_C


def refusal(adapter, heater, target, *, state=None, view=None):
    """The refusal text for a target above the ceiling, or None when it is allowed."""
    try:
        value = float(target)
    except (TypeError, ValueError):
        return f"the {heater} target {target!r} is not a number"
    limit = ceiling(adapter, heater, state=state, view=view)
    if not value <= limit:  # NaN is refused too
        return f"the {heater} target {value:g} C is above this printer's {limit:g} C {heater} ceiling"
    return None


def require(adapter, heater, target, *, state=None, view=None):
    """Raise HeaterCeilingError when ``target`` is above the ceiling."""
    problem = refusal(adapter, heater, target, state=state, view=view)
    if problem:
        raise HeaterCeilingError(problem)


def macro_arg_heater(key):
    """The heater a macro argument sets ("nozzle", "bed", "chamber"), or None."""
    name = str(key or "").strip().upper()
    if name in MACRO_NOZZLE_ARGS:
        return "nozzle"
    if name in MACRO_BED_ARGS:
        return "bed"
    if name in MACRO_CHAMBER_ARGS:
        return "chamber"
    return None
