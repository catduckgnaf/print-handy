"""Surface the structured thermal facts from the printer registry (issue #720).

Read-only presentation helpers shared by guided setup and the slice
summary. They report what the registry knows, spell unknown out as
unknown, and never change a setting, a fan, a temperature, or a chamber
command. Nothing is inferred from "enclosed": a fact the registry does
not carry as known stays unknown here too.

Per-install facts (#741): a printer's hardware record may carry facts the
operator confirmed from a read-only probe of that printer
(``core.thermal_probe``). Passed as ``install``, they override the model
value for that printer only, and every line they touch names them as
``operator-confirmed`` with where they came from. The registry is unchanged.
"""
from __future__ import annotations

import dataclasses

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


_QUESTIONS = dict(_SETUP_QUESTIONS)
_ROLES = ("part_cooling", "auxiliary", "exhaust", "filtration")


def question_for(field):
    """The guided-setup question for one thermal field."""
    return _QUESTIONS.get(field, field)


def _thermal_of(entry_or_thermal, install=None):
    """Accept a PrinterEntry or a ThermalCapabilities and return the thermal
    record, with any confirmed per-install facts applied over it."""
    thermal = getattr(entry_or_thermal, "thermal", entry_or_thermal)
    changes = {field: fact["value"] for field, fact in (install or {}).items()
               if isinstance(fact, dict) and fact.get("value")
               and field in getattr(thermal, "__dataclass_fields__", {})}
    if not changes:
        return thermal
    for role in _ROLES:
        # A fan this printer does not have has no mapping, whatever the model's says.
        if changes.get(f"fan_{role}_present") == "absent" and f"fan_{role}_mapping" not in changes:
            changes[f"fan_{role}_mapping"] = "unknown"
    return dataclasses.replace(thermal, **changes)


def _provenance(model_provenance, install, group):
    """A line's provenance: the model's, plus this printer's confirmations for that line."""
    facts = [fact for field, fact in (install or {}).items()
             if field.startswith(group) and isinstance(fact, dict)]
    if not facts:
        return model_provenance
    sources = sorted({str(fact.get("source") or "") for fact in facts} - {""})
    return (f"{model_provenance}; this printer: operator-confirmed"
            + (f", {', '.join(sources)}" if sources else ""))


def unknown_thermal_fields(entry_or_thermal, install=None):
    """The thermal field names whose recorded value is explicitly unknown."""
    thermal = _thermal_of(entry_or_thermal, install)
    return tuple(name for name, _question in _SETUP_QUESTIONS
                 if getattr(thermal, name, "unknown") == "unknown")


def thermal_setup_questions(entry_or_thermal, install=None):
    """Guided-setup questions for the facts that are unknown for this model.

    Returns a list of {"field", "question"} dicts, one per unknown fact.
    Known facts are never asked about, so a fully recorded model yields
    only the questions it is genuinely missing. Facts confirmed for this
    printer (``install``) count as known.
    """
    thermal = _thermal_of(entry_or_thermal, install)
    return [{"field": name, "question": question}
            for name, question in _SETUP_QUESTIONS
            if getattr(thermal, name, "unknown") == "unknown"]


def thermal_assumptions_lines(entry_or_thermal, install=None):
    """The slice-summary lines describing the thermal assumptions in play.

    Three lines: enclosure, chamber, and fans. Every value is the
    registry value, with unknown shown as unknown, plus the provenance
    recorded for each fact group. Facts confirmed for this printer
    (``install``) replace the model value and are named in that line's
    provenance. No plan or command is implied.
    """
    model = _thermal_of(entry_or_thermal)
    thermal = _thermal_of(entry_or_thermal, install)
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
        f"Thermal: enclosure {thermal.enclosure} "
        f"(provenance: {_provenance(model.enclosure_provenance, install, 'door_')}); "
        f"door/lid state knowable: {thermal.door_lid_state_knowable}",
        f"Thermal: chamber heater {heater_text} "
        f"(provenance: {_provenance(model.chamber_provenance, install, 'chamber_')}); "
        f"chamber sensor {thermal.chamber_sensor_present}, "
        f"fresh reading at slice time: {thermal.chamber_sensor_fresh_reading}",
        f"Thermal: fans {fans} (provenance: {_provenance(model.fan_provenance, install, 'fan_')})",
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


def thermal_lines_for_model(model_key, registry=None, printer_id=None):
    """Slice-summary thermal lines for a model key; [] when the model is unknown.

    With ``printer_id`` the facts confirmed for that printer (#741) apply.
    """
    entry = thermal_entry_for_model(model_key, registry=registry)
    if entry is None:
        return []
    install = {}
    if printer_id:
        from ..core.thermal_probe import install_facts
        install = install_facts(printer_id, model_key)
    return thermal_assumptions_lines(entry, install)
