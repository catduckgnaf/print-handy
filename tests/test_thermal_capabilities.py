"""Structured thermal capabilities (issue #720).

Every registered model carries an explicit [printer.thermal] record with
provenance, unknown spelled out as unknown, and no behavior change: these
tests pin representative models, the completeness of the record, the
guided-setup questions, and the slice-summary lines.
"""
import io
import tomllib
from pathlib import Path

import pytest

from print_handy import knowledge
from print_handy.knowledge import thermal as thermal_surface

ROOT = Path(__file__).parents[1]

THERMAL_KEYS = {
    "enclosure", "enclosure_provenance", "door_lid_state_knowable",
    "chamber_heater_present", "chamber_heater_control", "chamber_heater_evidence",
    "chamber_provenance", "chamber_sensor_present", "chamber_sensor_fresh_reading",
    "fan_part_cooling_present", "fan_part_cooling_mapping",
    "fan_auxiliary_present", "fan_auxiliary_mapping",
    "fan_exhaust_present", "fan_exhaust_mapping",
    "fan_filtration_present", "fan_filtration_mapping", "fan_provenance",
}


@pytest.fixture(scope="module")
def registry():
    return knowledge.load_registry()


def _entry(registry, key):
    return next(p for p in registry.printers if p.key == key)


def test_every_registered_model_has_an_explicit_thermal_record(registry):
    assert registry.printers
    for entry in registry.printers:
        thermal = entry.thermal
        assert thermal.enclosure in ("enclosed", "open", "unknown"), entry.key
        for provenance in (thermal.enclosure_provenance, thermal.chamber_provenance,
                           thermal.fan_provenance):
            assert provenance in ("vendor documentation", "verified profile",
                                  "operator-confirmed", "unknown"), entry.key
        # Supported control always carries verified command/macro evidence.
        if thermal.chamber_heater_control == "supported":
            assert thermal.chamber_heater_present == "present", entry.key
            assert thermal.chamber_heater_evidence not in ("", "unknown"), entry.key
        if thermal.chamber_heater_present == "absent":
            assert thermal.chamber_heater_control == "not_supported", entry.key
    # The raw TOML spells every field out for every model (no silent defaults).
    seen = 0
    for path in sorted((ROOT / "printers" / "registry").glob("*.toml")):
        with open(path, "rb") as handle:
            doc = tomllib.load(handle)
        for raw in doc.get("printer", []):
            seen += 1
            assert set(raw.get("thermal") or ()) == THERMAL_KEYS, raw.get("key")
    assert seen == len(registry.printers)


def test_enclosed_with_active_heating_creator5_pro(registry):
    thermal = _entry(registry, "flashforge.creator5_pro").thermal
    assert thermal.enclosure == "enclosed"
    assert thermal.chamber_heater_present == "present"
    # Firmware support is undocumented for the Creator models: present,
    # but control is NOT verified and must not read as supported.
    assert thermal.chamber_heater_control == "unknown"
    assert thermal.chamber_heater_evidence == ""
    assert thermal.fan_filtration_present == "present"


def test_enclosed_passive_models(registry):
    thermal = _entry(registry, "flashforge.ad5m_pro").thermal
    assert thermal.enclosure == "enclosed"
    for key in ("flashforge.ad5m_pro", "flashforge.ad5x", "flashforge_stock.ad5x"):
        thermal = _entry(registry, key).thermal
        assert thermal.chamber_heater_present == "absent", key
        assert thermal.chamber_heater_control == "not_supported", key
    assert _entry(registry, "flashforge.ad5m_pro").thermal.fan_filtration_present == "present"


def test_ad5x_enclosure_is_unknown_because_the_kit_is_optional(registry):
    # The AD5X ships open; Flashforge sells the enclosure as a separate kit, so
    # selecting the model does not establish that one is installed.
    for key in ("flashforge.ad5x", "flashforge_stock.ad5x"):
        thermal = _entry(registry, key).thermal
        assert thermal.enclosure == "unknown", key
        assert thermal.enclosure_provenance == "unknown", key


def test_open_frame_models(registry):
    for key in ("flashforge.ad5m", "octoprint.ender3", "bambulab.a1"):
        thermal = _entry(registry, key).thermal
        assert thermal.enclosure == "open", key
        assert thermal.chamber_heater_present == "absent", key
        assert thermal.chamber_heater_control == "not_supported", key


def test_supported_control_requires_evidence_q1_pro(registry):
    thermal = _entry(registry, "qidi.q1pro").thermal
    assert thermal.enclosure == "enclosed"
    assert thermal.chamber_heater_present == "present"
    assert thermal.chamber_heater_control == "supported"
    assert "CHAMBER=" in thermal.chamber_heater_evidence


def test_unknown_stays_unknown_for_generic_model(registry):
    thermal = _entry(registry, "klipper.generic").thermal
    assert thermal.enclosure == "unknown"
    assert thermal.chamber_heater_present == "unknown"
    assert thermal.chamber_sensor_present == "unknown"
    assert thermal.fan_part_cooling_mapping == "unknown"


def test_thermal_assumptions_lines_show_known_and_unknown(registry):
    entry = _entry(registry, "flashforge.creator5_pro")
    text = "\n".join(thermal_surface.thermal_assumptions_lines(entry))
    assert "enclosure enclosed" in text
    assert "chamber heater present, active control unknown" in text
    assert "chamber sensor unknown" in text
    assert "filtration present (mapping unknown)" in text
    assert "provenance: verified profile" in text


def test_slice_summary_includes_thermal_lines_for_registered_model(tmp_path):
    from print_handy.hermes import discord_print_summary as summary
    gcode = tmp_path / "plate_1.gcode"
    gcode.write_text("; estimated printing time (normal mode) = 1h 0m 0s\n"
                     "; total filament used [g] = 5\nG1 X1\n")
    job = {"printer_model": "flashforge.creator5_pro", "title": "Widget",
           "gcode": str(gcode)}
    text = summary.format_slice_summary(job)
    assert "Thermal: enclosure enclosed" in text
    assert "active control unknown" in text
    # A job naming no model keeps the summary it had (no thermal lines).
    job_without_model = {"title": "Widget", "gcode": str(gcode)}
    assert "Thermal:" not in summary.format_slice_summary(job_without_model)


def test_setup_questions_only_for_unknown_facts(registry):
    ad5x = _entry(registry, "flashforge.ad5x")
    fields = {q["field"] for q in thermal_surface.thermal_setup_questions(ad5x)}
    # Chamber heater presence is known for the AD5X; the enclosure kit is optional, so it is asked.
    assert "enclosure" in fields
    assert "chamber_heater_present" not in fields
    # Sensor and fan mapping facts are unknown, so they are asked about.
    assert "chamber_sensor_present" in fields
    assert "fan_part_cooling_mapping" in fields
    generic = _entry(registry, "klipper.generic")
    generic_fields = {q["field"] for q in thermal_surface.thermal_setup_questions(generic)}
    assert "enclosure" in generic_fields


def test_setup_wizard_surfaces_unknown_thermal_questions():
    from print_handy.setup_wizard import Wizard, build_plan
    answers = {
        "brand": "flashforge", "model": "flashforge.ad5x",
        "has_printer_software": "yes", "moonraker_url": "http://192.168.1.61:7125",
        "nozzle_diameter_mm": "0.4", "nozzle_type": "hardened_steel", "material": "PLA",
        "slicer": "orca", "install_slicer": "no",
        "spoolman": "skip", "camera": "no",
        "have_bot": "yes", "discord_app_id": "", "discord_chat_id": "100000000000000001",
        "discord_user_ids": "", "bot_name": "Print Handy",
        "discord_bot_token": "tok", "hermes": "no", "add_printer_2": "no",
    }
    wizard = Wizard(answers=answers, input_stream=io.StringIO(""), output=io.StringIO())
    plan = build_plan(wizard, hermes_found=False)
    notes = "\n".join(plan["notes"])
    assert "Thermal facts still unknown for Flashforge AD5X (ZMod)" in notes
    thermal_note = next(n for n in plan["notes"] if n.startswith("Thermal facts still unknown"))
    # The enclosure kit is optional on the AD5X, so setup asks; the known heater fact is not asked.
    assert "Is the printer enclosed or open-frame?" in thermal_note
    assert "chamber heater?" not in thermal_note
    assert "chamber temperature sensor" in thermal_note
