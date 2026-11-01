"""Source-profile compatibility warnings (issue #722, follow-up 3 from #715).

When a model's project settings were authored for a different printer, some of its
values can sit outside what the target printer is verified to handle. This module
compares the authored values against the target printer's own verified facts — the
machine limits in its Orca machine template (`machine_max_speed_x/y`,
`machine_max_acceleration_x/y`) and the 0-100 percent fan scale — and returns
plain-language warnings for demonstrated conflicts only.

Warnings are visibility, never changes: nothing here mutates a setting, and the
existing precedence (template/vendor profile, filament family, material variant,
operator/spool values) is untouched. Values with no verified target range — wall
count and brim width among the #722 candidates — are checked and deliberately not
warned about, because there is no target fact to conflict with.
"""
import json
import re
from pathlib import Path

# Authored keys checked against the target machine template's axis limits.
_SPEED_KEYS = ("outer_wall_speed", "bridge_speed", "internal_bridge_speed", "travel_speed")
_ACCEL_KEYS = ("default_acceleration", "outer_wall_acceleration", "travel_acceleration",
               "inner_wall_acceleration")
# Fan speeds in Orca filament/profile settings are percentages of the fan's range.
_FAN_KEYS = ("fan_max_speed", "fan_min_speed", "overhang_fan_speed")


def _number(value):
    """First numeric value of an Orca setting (scalar or single-element list), else None."""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if isinstance(value, str):
        value = value.strip().rstrip("%")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _limit(machine, *keys):
    values = [v for key in keys if (v := _number(machine.get(key))) is not None]
    return min(values) if values else None


def machine_limits(templates_dir):
    """Verified target limits from the printer's own machine template, or {} if unreadable."""
    try:
        machine = json.loads((Path(templates_dir) / "machine.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    limits = {}
    speed = _limit(machine, "machine_max_speed_x", "machine_max_speed_y")
    if speed is not None:
        limits["max_axis_speed"] = speed
    accel = _limit(machine, "machine_max_acceleration_x", "machine_max_acceleration_y")
    if accel is not None:
        limits["max_axis_acceleration"] = accel
    return limits


def source_printer_label(author):
    """The printer the source profile names, from the identity Orca/Bambu writes into
    project_settings.config (`printer_model_id`, else `printer_settings_id`), else None."""
    for key in ("printer_model_id", "printer_settings_id"):
        value = author.get(key)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _norm(label):
    text = " ".join(str(label).lower().replace("_", " ").replace("-", " ").split())
    # A nozzle suffix ("... 0.4 nozzle") names a nozzle, not a different printer.
    return re.sub(r"\s+\d+(?:\.\d+)?\s*(?:mm\s*)?nozzle$", "", text).strip()


def machine_identity(templates_dir):
    """The names the target machine template itself declares (`printer_model`,
    `printer_settings_id`), normalized; empty when the template is unreadable."""
    try:
        machine = json.loads((Path(templates_dir) / "machine.json").read_text())
    except (OSError, json.JSONDecodeError):
        return frozenset()
    names = set()
    for key in ("printer_model", "printer_settings_id"):
        value = machine.get(key)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        if isinstance(value, str) and value.strip():
            names.add(_norm(value))
    return frozenset(names)


def is_foreign(source_label, target_label, aliases=()):
    """True only when the source names a printer that is demonstrably not the target.

    Identity is exact (after normalizing case, separators and a nozzle suffix) against
    the target label and the names its own machine template declares. No substring
    matching: "Kobra 2 Pro" is not "Kobra 2". When either side is missing, foreignness
    is not demonstrated and no warnings are produced.
    """
    if not source_label or not (target_label or aliases):
        return False
    known = {_norm(target_label)} if target_label else set()
    known.update(aliases)
    return _norm(source_label) not in known


def source_profile_warnings(author, *, source_label, target_label, limits, aliases=()):
    """One warning per authored value that demonstrably conflicts with a target fact.

    Returns [] for native profiles, unidentified sources, in-range values, and values
    with no verified target range. Each warning names the value, where it came from,
    and the target fact it conflicts with. Nothing is changed.
    """
    if not author or not is_foreign(source_label, target_label, aliases):
        return []
    warnings = []
    origin = f"the source profile for {source_label}"
    checks = []
    if limits.get("max_axis_speed") is not None:
        checks += [(key, limits["max_axis_speed"], "mm/s", "maximum axis speed") for key in _SPEED_KEYS]
    if limits.get("max_axis_acceleration") is not None:
        checks += [(key, limits["max_axis_acceleration"], "mm/s^2", "maximum axis acceleration")
                   for key in _ACCEL_KEYS]
    for key, limit, unit, fact in checks:
        value = _number(author.get(key))
        if value is not None and value > limit:
            warnings.append(
                f"Source profile warning: {key} is {value:g} {unit} in {origin}, above the "
                f"{target_label} limit of {limit:g} {unit} (its machine template {fact}). "
                "Print Handy has not changed this setting.")
    for key in _FAN_KEYS:
        raw = author.get(key)
        entries = list(raw) if isinstance(raw, (list, tuple)) else [raw]
        for index, entry in enumerate(entries):
            value = _number(entry)
            if value is None or value <= 100:
                continue
            which = f" (filament {index + 1})" if len(entries) > 1 else ""
            warnings.append(
                f"Source profile warning: {key}{which} is {value:g}% in {origin}, above the "
                f"{target_label} fan range of 0-100%. Print Handy has not changed this setting.")
    return warnings
