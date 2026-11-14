"""Read nozzle, bed, and max Z from a Moonraker config, and compare them (#623).

Read-only. A failed read is a sentence, not a blocked add-printer. A mismatch
is a warning. Nothing here rewrites the profile.
"""
from __future__ import annotations


import logging

logger = logging.getLogger(__name__)
def _num(section, key):
    if not isinstance(section, dict):
        return None
    raw = section.get(key)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value


def _travel(section):
    """An axis' length: ``position_max`` less ``position_min`` (Klipper's default 0, never below it)."""
    top = _num(section, "position_max")
    if top is None:
        return None
    try:
        bottom = max(float(section.get("position_min", 0)), 0.0)
    except (TypeError, ValueError):
        bottom = 0.0
    return top - bottom if top > bottom else None


def from_configfile(payload) -> dict:
    """Pull nozzle, bed, and max Z out of a Moonraker configfile query.

    Returns ``{"error": ...}`` when none of the three can be read. Missing
    pieces are omitted rather than guessed.
    """
    result = (payload or {}).get("result") if isinstance(payload, dict) else None
    status = (result or {}).get("status") if isinstance(result, dict) else None
    configfile = (status or {}).get("configfile") if isinstance(status, dict) else None
    config = (configfile or {}).get("config") if isinstance(configfile, dict) else None
    if not isinstance(config, dict):
        config = (result or {}).get("config") if isinstance(result, dict) else None
    if not isinstance(config, dict):
        return {"error": "the printer config did not include nozzle, bed, or Z"}
    found = {}
    nozzle = _num(config.get("extruder"), "nozzle_diameter")
    if nozzle is not None:
        found["nozzle_mm"] = nozzle
    bed_x = _travel(config.get("stepper_x"))
    bed_y = _travel(config.get("stepper_y"))
    mesh = config.get("bed_mesh") if isinstance(config.get("bed_mesh"), dict) else None
    # The probed mesh is inset from the bed edges, so it only stands in when the steppers are missing.
    if not (bed_x and bed_y) and mesh and isinstance(mesh.get("mesh_max"), str) \
            and isinstance(mesh.get("mesh_min"), str):
        try:
            max_x, max_y = (float(part) for part in mesh["mesh_max"].split(","))
            min_x, min_y = (float(part) for part in mesh["mesh_min"].split(","))
            if max_x > min_x and max_y > min_y:
                bed_x, bed_y = max_x - min_x, max_y - min_y
        except ValueError:
            logger.debug("Best-effort recovery from ValueError", exc_info=True)
    if bed_x and bed_y:
        found["bed_mm"] = [bed_x, bed_y]
    height = _num(config.get("stepper_z"), "position_max")
    if height is not None:
        found["max_z_mm"] = height
    if not found:
        return {"error": "the printer config did not include nozzle, bed, or Z"}
    return found


def _facts(model):
    """Nozzle set, bed, and max Z from a registry model.

    Registry models keep bed and height under ``settings["printer"]``. A
    test double may set the same names as attributes. Either shape works.
    """
    nested = {}
    settings = getattr(model, "settings", None)
    if isinstance(settings, dict) and isinstance(settings.get("printer"), dict):
        nested = settings["printer"]
    bed = getattr(model, "bed_mm", None)
    if not bed:
        bed = nested.get("bed_mm")
    height = getattr(model, "max_print_height_mm", None)
    if height is None:
        height = nested.get("max_print_height_mm")
    nozzles = getattr(model, "supported_nozzle_sizes", ()) or ()
    return nozzles, bed, height


def warning_for(model, geometry) -> str:
    """A mismatch sentence, or "" when the reported numbers fit the picked model.

    The generic Klipper profile is offered in the sentence. The picked model
    is not changed.
    """
    if not geometry or geometry.get("error"):
        return ""
    nozzles, model_bed, model_z = _facts(model)
    bits = []
    nozzle = geometry.get("nozzle_mm")
    if nozzle is not None and nozzles and not any(abs(float(nozzle) - float(size)) < 0.02 for size in nozzles):
        bits.append(f"nozzle {float(nozzle):g} mm is not in this profile")
    bed = geometry.get("bed_mm")
    if bed and model_bed and (float(bed[0]) + 1 < float(model_bed[0]) or float(bed[1]) + 1 < float(model_bed[1])):
        bits.append("the bed is smaller than this profile")
    height = geometry.get("max_z_mm")
    if height is not None and model_z and float(height) + 1 < float(model_z):
        bits.append("max Z is lower than this profile")
    if not bits:
        return ""
    return ("; ".join(bits) + ". Apply still uses the model you picked. "
            "The generic Klipper profile can hold these numbers instead; go back and pick it if you want that. "
            "The numbers are what the printer reported, not a tuned profile.")
