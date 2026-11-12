"""Checks of a sliced G-code against the hardware record, shared by the vendor guards.

The Prusa, Qidi, Elegoo SDCP and Snapmaker guards refuse another ecosystem's start code, a file for
another model and temperatures over the model's limits. Those are properties of the file alone. This
module adds the properties that need the record of what is loaded and installed, the same three the
Bambu Lab guard checks: the material, the nozzle diameter and the heater targets. A guard calls
``recorded_job_problems`` with the state it was given and gets [] when there is none (call sites that
only have the file, such as a pre-queue scan).

The state's ``temps_c`` is the job's own: orca_adapter sets it from the sliced spool and
model_job._with_gcode_temperatures from the G-code's spool (printer_state.gcode_temperatures), so a
slot job sliced at its spool's temperature is checked against that.
"""
from __future__ import annotations

import math
import re

_NUMBER = r"(-?\d+(?:\.\d+)?)"
# START macros that carry the heater targets as arguments instead of (or before) M104/M190:
# Qidi's PRINT_START BED= HOTEND=, Creality/ZMod's START_PRINT BED_TEMP= EXTRUDER_TEMP=.
_NOZZLE_ARGS = ("EXTRUDER_TEMP", "NOZZLE_TEMP", "HOTEND_TEMP", "HOTEND", "EXTRUDER")
_BED_ARGS = ("BED_TEMP", "BED_TEMPERATURE", "BED")
_PLATE_KEYS = ("cool_plate_temp_initial_layer", "eng_plate_temp_initial_layer",
               "hot_plate_temp_initial_layer", "textured_plate_temp_initial_layer")


def _config(text, key):
    match = re.search(rf"^; {re.escape(key)} = (.*)$", text, re.M)
    return match.group(1).strip() if match else None


def _config_numbers(text, key):
    """The comma-joined numbers of an Orca config line, or [] when absent or not numeric."""
    raw = _config(text, key)
    try:
        return [float(piece) for piece in raw.split(",")] if raw else []
    except ValueError:
        return []


def _commands(text, command):
    return [float(v) for v in re.findall(rf"^[ \t]*{command}\b[^;\n]*?\bS{_NUMBER}", text, re.M)]


def _macro_args(text, names):
    pattern = rf"^[ \t]*[A-Z][A-Z0-9]*_[A-Z0-9_]+\b[^;\n]*?\b(?:{'|'.join(names)})={_NUMBER}"
    return [float(v) for v in re.findall(pattern, text, re.M)]


def embedded_materials(text):
    """The filament_type per filament, unquoted (Orca quotes names with spaces), or []."""
    from ..core.orca_config import split_config_list
    raw = _config(text, "filament_type")
    try:
        return [m.strip() for m in split_config_list(raw) if m.strip()] if raw else []
    except ValueError:
        return []


def nozzle_problems(text, state):
    """The embedded nozzle diameter(s) must be present and equal the installed nozzle."""
    recorded = (state.get("nozzle") or {}).get("diameter_mm")
    if recorded is None:
        return []
    raw = _config(text, "nozzle_diameter")
    if raw is None:
        return [f"G-code carries no nozzle_diameter to check against the installed {float(recorded):g} mm nozzle"]
    try:
        sizes = [float(piece) for piece in raw.strip('"').split(",") if piece.strip()]
    except ValueError:
        return [f"invalid embedded nozzle_diameter {raw!r}"]
    if not sizes:
        return [f"invalid embedded nozzle_diameter {raw!r}"]
    bad = next((s for s in sizes if not math.isclose(s, float(recorded), abs_tol=1e-3)), None)
    return [] if bad is None else [f"G-code nozzle {bad:g} mm != installed {float(recorded):g} mm"]


def material_problems(text, state):
    """The embedded filament_type must be the loaded material (any loaded spool's, for a multi-filament file)."""
    spool = (state.get("spool") or {}).get("material")
    if not spool:
        return []
    from ..core import material_accept
    from ..core.printer_state import same_material
    materials = embedded_materials(text)
    if not materials:
        return [f"G-code must name a filament_type, got {_config(text, 'filament_type')!r}"]
    if len(materials) == 1:
        # A warning the operator may accept with Print as sliced, never a hard block (#1336).
        return [] if same_material(materials[0], spool) else material_accept.material_problem(materials[0], spool)
    # A multi-filament file is checked against every spool the record holds; which tray feeds which
    # tool is the print path's live decision.
    loaded = [spool] + [rec.get("material") for rec in (state.get("filament_slots") or {}).values()
                        if isinstance(rec, dict)]
    names = ', '.join(sorted({str(x) for x in loaded if x}))
    return [p for m in dict.fromkeys(materials) if not any(same_material(m, x) for x in loaded if x)
            for p in material_accept.problem(
                "material", f"G-code filament_type {m!r} is not a loaded material ({names})", m, names)]


def temperature_problems(text, state, materials=None):
    """The heater targets must be the recorded ones: in the file's own config block and in a heater command.

    Nozzle: the effective target (the recorded target plus the nozzle-type offset). The first filament's
    config entry (any entry, for a multi-filament file whose start tool the record does not name) and at
    least one M104/M109 or START-macro argument must equal it. Bed: the same for M140/M190 and the plate
    temperatures. A file with neither the config entry nor a heater command carrying a target is refused.
    """
    temps = state.get("temps_c") or {}
    if "nozzle" not in temps or "bed" not in temps:
        return []
    from ..core.nozzle import NozzleError, effective_nozzle_temperature
    from ..core.printer_state import recorded_nozzle_type
    try:
        nozzle = effective_nozzle_temperature(recorded_nozzle_type(state), float(temps["nozzle"]))
    except NozzleError as exc:
        return [f"recorded nozzle temperature is not usable: {exc}"]
    bed = float(temps["bed"])
    multi = len(embedded_materials(text)) > 1
    problems = []
    for label, wanted, config_values, commands, args in (
            ("nozzle", nozzle,
             _config_numbers(text, "nozzle_temperature_initial_layer") or _config_numbers(text, "nozzle_temperature"),
             _commands(text, "M104") + _commands(text, "M109"), _macro_args(text, _NOZZLE_ARGS)),
            ("bed", bed,
             [v for key in _PLATE_KEYS for v in (_config_numbers(text, key)[:None if multi else 1])],
             _commands(text, "M140") + _commands(text, "M190"), _macro_args(text, _BED_ARGS))):
        commanded = [v for v in commands + args if v > 0]
        matches = lambda v: math.isclose(v, wanted, abs_tol=1e-3)  # noqa: E731
        if config_values and not (any(map(matches, config_values)) if multi else all(map(matches, config_values))):
            problems.append(f"G-code {label} temperature {config_values[0]:g} C != recorded {wanted:g} C")
        elif not multi and not any(map(matches, commanded)) and (commanded or not config_values):
            # (a multi-filament file's heater commands follow each tool's own target: the config block decides)
            problems.append(f"no heater command sets the recorded {label} temperature {wanted:g} C")
    return problems


def recorded_job_problems(text, adapter, state):
    """Material, nozzle diameter and heater problems of text against state; [] when there is no state."""
    if not state:
        return []
    del adapter  # the checks are the same on every printer; the argument keeps the guards' call shape
    return nozzle_problems(text, state) + material_problems(text, state) + temperature_problems(text, state)
