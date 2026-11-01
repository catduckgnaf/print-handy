"""Surface the structured thermal facts from the printer registry (issue #720).

Read-only presentation helpers shared by guided setup and the slice
summary. They report what the registry knows, spell unknown out as
unknown, and never change a setting, a fan, a temperature, or a chamber
command. Nothing is inferred from "enclosed": a fact the registry does
not carry as known stays unknown here too.
"""
from __future__ import annotations

# (thermal attribute, question for guided setup) in the order setup asks
# about them. A question is only returned while its fact is unknown.
_SETUP_QUESTIONS = (
    ("enclosure", "Is the printer enclosed or open-frame?"),
    ("door_lid_state_knowable", "Can the printer report its door or lid state?"),
    ("chamber_heater_present", "Does the printer have a chamber heater?"),
    ("chamber_heater_control", "Is active chamber temperature control supported by the printer firmware?"),
    ("chamber_sensor_present", "Does the printer have a chamber temperature sensor?"),
    ("chamber_sensor_fresh_reading",
     "Can the connected printer report a fresh chamber temperature reading at slice time?"),
    ("fan_part_cooling_present", "Does the printer have a part-cooling fan?"),
    ("fan_part_cooling_mapping", "What is the verified part-cooling fan index or macro name?"),
    ("fan_auxiliary_present", "Does the printer have an auxiliary fan?"),
    ("fan_auxiliary_mapping", "What is the verified auxiliary fan index or macro name?"),
    ("fan_exhaust_present", "Does the printer have an exhaust fan?"),
    ("fan_exhaust_mapping", "What is the verified exhaust fan index or macro name?"),
    ("fan_filtration_present", "Does the printer have a filtration fan?"),
    ("fan_filtration_mapping", "What is the verified filtration fan index or macro name?"),
)


def _thermal_of(entry_or_thermal):
    """Accept a PrinterEntry or a ThermalCapabilities and return the thermal record."""
    return getattr(entry_or_thermal, "thermal", entry_or_thermal)


def unknown_thermal_fields(entry_or_thermal):
    """The thermal field names whose recorded value is explicitly unknown."""
    thermal = _thermal_of(entry_or_thermal)
    return tuple(name for name, _question in _SETUP_QUESTIONS
                 if getattr(thermal, name, "unknown") == "unknown")


def thermal_setup_questions(entry_or_thermal):
    """Guided-setup questions for the facts that are unknown for this model.

    Returns a list of {"field", "question"} dicts, one per unknown fact.
    Known facts are never asked about, so a fully recorded model yields
    only the questions it is genuinely missing.
    """
    thermal = _thermal_of(entry_or_thermal)
    return [{"field": name, "question": question}
            for name, question in _SETUP_QUESTIONS
            if getattr(thermal, name, "unknown") == "unknown"]


def thermal_assumptions_lines(entry_or_thermal):
    """The slice-summary lines describing the thermal assumptions in play.

    Three lines: enclosure, chamber, and fans. Every value is the
    registry value, with unknown shown as unknown, plus the provenance
    recorded for each fact group. No plan or command is implied.
    """
    thermal = _thermal_of(entry_or_thermal)
    heater = thermal.chamber_heater_present
    if heater == "present":
        heater_text = f"present, active control {thermal.chamber_heater_control}"
        if thermal.chamber_heater_evidence:
            heater_text += f" (evidence: {thermal.chamber_heater_evidence})"
    elif heater == "absent":
        heater_text = "absent, active control not_supported"
    else:
        heater_text = "unknown, active control unknown"
    fans = ", ".join((
        f"part cooling {thermal.fan_part_cooling_present} (mapping {thermal.fan_part_cooling_mapping})",
        f"auxiliary {thermal.fan_auxiliary_present} (mapping {thermal.fan_auxiliary_mapping})",
        f"exhaust {thermal.fan_exhaust_present} (mapping {thermal.fan_exhaust_mapping})",
        f"filtration {thermal.fan_filtration_present} (mapping {thermal.fan_filtration_mapping})",
    ))
    return [
        f"Thermal: enclosure {thermal.enclosure} (provenance: {thermal.enclosure_provenance}); "
        f"door/lid state knowable: {thermal.door_lid_state_knowable}",
        f"Thermal: chamber heater {heater_text} (provenance: {thermal.chamber_provenance}); "
        f"chamber sensor {thermal.chamber_sensor_present}, "
        f"fresh reading at slice time: {thermal.chamber_sensor_fresh_reading}",
        f"Thermal: fans {fans} (provenance: {thermal.fan_provenance})",
    ]


def thermal_entry_for_model(model_key, registry=None):
    """The registry PrinterEntry for ``model_key``, or None when not registered."""
    if not model_key:
        return None
    if registry is None:
        from . import default_registry
        try:
            registry = default_registry()
        except Exception:
            return None
    for entry in registry.printers:
        if entry.key == model_key:
            return entry
    return None


def thermal_lines_for_model(model_key, registry=None):
    """Slice-summary thermal lines for a model key; [] when the model is unknown."""
    entry = thermal_entry_for_model(model_key, registry=registry)
    return thermal_assumptions_lines(entry) if entry is not None else []
