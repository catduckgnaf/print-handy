"""#1816: a non-PLA spool slices with the Orca vendor profile for its sub-type, never the printer's PLA profile.

These slices go through slice_model with the real AD5X templates and the committed Orca profile database; only Orca
itself is stubbed (the slice_inputs fixture captures the filament JSON it receives). The original 2026-11-21 PETG
artifact is documented by its archived job evidence: it did use the PLA template as source, then its tungsten-carbide nozzle's 1.05 multiplier yielded the emitted 1.05. The regression below pins the cause and the corrected path.
"""
import pytest

from print_handy.core import filament_type
from print_handy.core import nozzle as nozzle_mod
from print_handy.core import orca_adapter
from print_handy.core import orca_profile_db
from print_handy.core import printer_state
from print_handy.gcode import verify as gcode_verify

from .test_slice_temperatures import slice_inputs, stale_state  # noqa: F401

AD5X = "flashforge.ad5x"


def _flow(state, vendor_flow):
    """The flow Orca receives: the vendor's ratio times the fitted nozzle type's multiplier."""
    multiplier = nozzle_mod.get_nozzle_type_adjustments((state.get("nozzle") or {}).get("type"))["flow_multiplier"]
    return [str(round(float(vendor_flow) * multiplier, 4))]


def _vendor(name):
    return next(r for r in orca_profile_db.vendor_filaments("Flashforge") if r["name"] == name)["values"]


def test_high_speed_petg_slices_with_the_vendor_hs_petg_profile(slice_inputs):
    state = stale_state("PETG")
    slice_inputs.spools[17] = {"id": 17, "filament": {"material": "PETG", "name": "High Speed Matte Black PETG",
                                                      "settings_extruder_temp": None, "settings_bed_temp": None}}
    _nozzle, _bed, filament = slice_inputs.slice(AD5X, state)
    vendor = _vendor("Flashforge HS PETG @FF AD5X")
    assert filament["nozzle_temperature"] == ["235"]
    assert filament["nozzle_temperature_initial_layer"] == ["240"]
    assert filament["hot_plate_temp"] == ["75"]
    assert filament["hot_plate_temp_initial_layer"] == ["75"]
    assert filament["name"] == "Flashforge HS PETG @FF AD5X"
    assert filament["filament_type"] == ["PETG"]
    assert filament["filament_flow_ratio"] == _flow(state, vendor["filament_flow_ratio"][0])
    assert filament["filament_max_volumetric_speed"] == vendor["filament_max_volumetric_speed"] == ["12"]
    assert filament["fan_min_speed"] == vendor["fan_min_speed"]
    assert filament["filament_z_hop_types"] == ["Normal Lift"]
    assert filament["enable_pressure_advance"] == ["0"]


def test_vendor_temperature_resolution_retains_both_layer_targets():
    state = stale_state("PETG")
    state["printer_model"] = AD5X
    hs_petg = {"material": "PETG", "name": "High Speed Matte Black PETG"}
    temps, resolution = printer_state.slice_temperatures(state, filament=hs_petg, stored_ok=False)
    layers = resolution.get('layer_temperatures') or {}
    expected = {"nozzle": 235, "bed": 75, "nozzle_initial_layer": 240, "bed_initial_layer": 75}
    assert {**temps, "nozzle_initial_layer": layers.get("nozzle", temps["nozzle"]),
            "bed_initial_layer": layers.get("bed", temps["bed"])} == expected
    assert resolution["layer_temperatures"] == {"nozzle": 240, "bed": 75}
    state["printer_model"] = AD5X
    state["temperature_resolution"] = resolution
    state["temps_c"] = {**temps, "nozzle_initial_layer": 240, "bed_initial_layer": 75}
    assert printer_state.gcode_temperatures(
        state, ["PETG"], fetch_filament=lambda _sid: None,
        text='; filament_type = PETG\n; nozzle_temperature = 235\n; nozzle_temperature_initial_layer = 240\n',
    ) == expected


def test_plain_petg_slices_with_the_vendor_generic_and_never_the_pla_name(slice_inputs):
    state = stale_state("PETG")
    slice_inputs.spools[17] = {"id": 17, "filament": {"material": "PETG", "name": "Black PETG",
                                                      "settings_extruder_temp": None, "settings_bed_temp": None}}
    _nozzle, _bed, filament = slice_inputs.slice(AD5X, state)
    vendor = _vendor("Generic PETG @Flashforge")
    assert filament["filament_settings_id"] == ["Generic PETG @Flashforge"]
    assert "PLA" not in filament["filament_settings_id"][0]
    assert filament["filament_flow_ratio"] == _flow(state, vendor["filament_flow_ratio"][0])


def _capture_slice(monkeypatch):
    captured = {}
    real = orca_adapter.slice_model

    def wrapped(*args, **kwargs):
        result = real(*args, **kwargs)
        captured["meta"] = result[1]
        return result

    monkeypatch.setattr(orca_adapter, "slice_model", wrapped)
    return captured


def test_a_pla_spool_keeps_the_tuned_pla_template_and_its_name(slice_inputs, monkeypatch):
    state = stale_state("PLA")
    slice_inputs.spools[17] = {"id": 17, "filament": {"material": "PLA", "name": "White PLA",
                                                      "settings_extruder_temp": None, "settings_bed_temp": None}}
    captured = _capture_slice(monkeypatch)
    _nozzle, _bed, filament = slice_inputs.slice(AD5X, state)
    assert filament["filament_settings_id"] == ["Flashforge Generic PLA"]
    assert filament["filament_flow_ratio"] == ["0.98"]  # the shipped [material_tuning] PLA recipe (#783)
    # Provenance is the flow Orca actually receives, after the tuning override (#1832).
    # slice_model copies state before job_filament, so the record is on the slice result.
    assert captured["meta"]["flow_provenance"]["PLA"]["emitted_flow_ratio"] == 0.98
    assert captured["meta"]["flow_provenance"]["PLA"]["material_tuning_flow_ratio"] == 0.98


def test_nonnumeric_vendor_flow_does_not_abort_the_slice(slice_inputs, monkeypatch):
    """A profile flow that is not a number is skipped, not a failed slice (#1832)."""
    real = orca_profile_db.vendor_layer

    def layer(template, resolution):
        out = real(template, resolution)
        out["filament_flow_ratio"] = ["not-a-number"]
        return out

    monkeypatch.setattr(orca_profile_db, "vendor_layer", layer)
    state = stale_state("PETG")
    slice_inputs.spools[17] = {"id": 17, "filament": {"material": "PETG", "name": "Black PETG",
                                                      "settings_extruder_temp": None, "settings_bed_temp": None}}
    captured = _capture_slice(monkeypatch)
    _nozzle, _bed, filament = slice_inputs.slice(AD5X, state)
    prov = captured["meta"]["flow_provenance"]["PETG"]
    assert prov["base_flow_ratio"] is None
    # Invalid vendor values must never leak into Orca's emitted JSON.
    emitted = filament.get("filament_flow_ratio")
    assert emitted is None or (len(emitted) == 1 and float(emitted[0]) > 0)
    assert prov["emitted_flow_ratio"] == (float(emitted[0]) if emitted else None)


def test_a_material_orca_has_no_vendor_profile_for_names_the_library_generic(slice_inputs):
    state = stale_state("PC")
    slice_inputs.spools[17] = {"id": 17, "filament": {"material": "PC", "name": "Clear PC",
                                                      "settings_extruder_temp": None, "settings_bed_temp": None}}
    _nozzle, _bed, filament = slice_inputs.slice(AD5X, state)
    expected = orca_profile_db.profile_name(
        orca_profile_db.resolve(orca_profile_db.printer_preset("Flashforge AD5X", 0.4,
                                                               preferred="Flashforge AD5X 0.4 nozzle"),
                                filament_type.from_text("PC")), "PC")
    assert filament["filament_settings_id"] == [expected]
    assert "Flashforge Generic PLA" not in expected



@pytest.mark.parametrize("explicit, expected", [
    ({}, (235, 75)),
    ({"settings_extruder_temp": 250}, (250, 75)),
    ({"settings_bed_temp": 80}, (235, 80)),
    ({"settings_extruder_temp": 250, "settings_bed_temp": 80}, (250, 80)),
])
def test_ad5x_hs_petg_slice_and_verification_share_temperatures(slice_inputs, explicit, expected):
    state = stale_state("PETG")
    state["printer_model"] = AD5X
    state["nozzle"]["type"] = "brass"
    slice_inputs.spools[17] = {
        "id": 17,
        "filament": {"material": "PETG", "name": "High Speed Matte Black PETG", **explicit},
    }
    nozzle, bed, filament = slice_inputs.slice(AD5X, state)
    assert filament["nozzle_temperature"] == [str(expected[0])]
    vendor_nozzle_initial = 240 if expected[0] == 235 and "settings_extruder_temp" not in explicit else expected[0]
    assert filament["nozzle_temperature_initial_layer"] == [str(vendor_nozzle_initial)]
    assert filament["hot_plate_temp"] == [str(expected[1])]
    vendor_bed_initial = 75 if expected[1] == 75 and "settings_bed_temp" not in explicit else expected[1]
    assert filament["hot_plate_temp_initial_layer"] == [str(vendor_bed_initial)]
    assert (nozzle, bed) == expected
    assert filament["filament_settings_id"] == ["Flashforge HS PETG @FF AD5X"]
    # Re-resolve independently through the verification entry point, with no
    # recorded-temperature shortcut, so a mismatch cannot pass this test.
    text = '; filament_type = "PETG"\n'
    text += f"; nozzle_temperature_initial_layer = {nozzle}\n; hot_plate_temp_initial_layer = {bed}\n"
    verified = gcode_verify._with_gcode_temperatures(state, text)
    assert verified["temps_c"] == {"nozzle": expected[0], "bed": expected[1],
                                    "nozzle_initial_layer": vendor_nozzle_initial,
                                    "bed_initial_layer": vendor_bed_initial}


@pytest.mark.parametrize("label", ["operator setting", "Spoolman filament"])
def test_ad5x_hs_petg_vendor_never_replaces_a_non_default_source(slice_inputs, monkeypatch, label):
    # Only a material default may be raised to the vendor target; any other
    # resolved source (an attested operator value under any label) stands.
    real = printer_state.resolve_material_temperatures

    def resolved(material, filament):
        family, targets, sources = real(material, filament)
        return family, {"nozzle": 244, "bed": 81}, {"nozzle": label, "bed": label}

    monkeypatch.setattr(printer_state, "resolve_material_temperatures", resolved)
    state = stale_state("PETG")
    state["printer_model"] = AD5X
    state["nozzle"]["type"] = "brass"
    hs_petg = {"material": "PETG", "name": "High Speed Matte Black PETG"}
    temps, resolution = printer_state.slice_temperatures(state, filament=hs_petg, stored_ok=False)
    assert temps == {"nozzle": 244, "bed": 81}
    assert resolution["sources"] == {"nozzle": label, "bed": label}


def test_hs_petg_flow_is_reconstructable_from_slice_provenance(slice_inputs, monkeypatch):
    state = stale_state("PETG")
    state["nozzle"]["type"] = "brass"
    slice_inputs.spools[17] = {
        "id": 17, "filament": {"material": "PETG", "name": "High Speed Black PETG"},
    }
    captured = _capture_slice(monkeypatch)
    _, _, filament = slice_inputs.slice(AD5X, state)
    record = captured["meta"]["flow_provenance"]["PETG"]
    assert record["profile_name"] == "Flashforge HS PETG @FF AD5X"
    assert record["nozzle_type"] == "brass"
    assert record["base_flow_ratio"] == 0.98
    assert record["flow_multiplier"] == nozzle_mod.get_nozzle_type_adjustments("brass")["flow_multiplier"]
    assert record["material_tuning_flow_ratio"] is None
    rebuilt = round(record["base_flow_ratio"] * record["flow_multiplier"], 4)
    assert rebuilt == record["emitted_flow_ratio"] == float(filament["filament_flow_ratio"][0])


def test_archived_petg_105_flow_source_is_the_nozzle_multiplier():
    """Reconstruct #123: the archived PLA template supplied 1.0; tungsten carbide supplied 1.05."""
    nozzle = nozzle_mod.get_nozzle_type_adjustments("DUROZZLE tungsten carbide 0.4 (nickel-plated copper)")
    assert nozzle["flow_multiplier"] == 1.05
    base_template_flow = 1.0
    emitted_archived_flow = round(base_template_flow * nozzle["flow_multiplier"], 4)
    assert emitted_archived_flow == 1.05


def test_non_pla_does_not_emit_the_archived_pla_template_flow(slice_inputs):
    state = stale_state("PETG")
    state["nozzle"]["type"] = "tungsten_carbide"
    slice_inputs.spools[17] = {"id": 17, "filament": {"material": "PETG", "name": "High Speed Matte Black PETG"}}
    _, _, filament = slice_inputs.slice(AD5X, state)
    vendor = _vendor("Flashforge HS PETG @FF AD5X")
    expected = _flow(state, vendor["filament_flow_ratio"][0])
    assert filament["filament_settings_id"] == ["Flashforge HS PETG @FF AD5X"]
    assert filament["filament_flow_ratio"] == expected
    assert filament["filament_flow_ratio"] != ["1.05"]


@pytest.mark.parametrize("vendor, expected", [
    ({"id": 1, "name": "SUNLU"}, "SUNLU PETG @FF AD5M"),
    ({"id": 2, "name": "Polymaker"}, "Generic PETG @Flashforge"),
    ({"id": 3}, "Generic PETG @Flashforge"),
    (None, "Generic PETG @Flashforge"),
])
def test_spoolman_vendor_selects_the_sliced_profile_or_falls_back(slice_inputs, vendor, expected):
    state = stale_state("PETG")
    slice_inputs.spools[17] = {
        "id": 17, "filament": {"material": "PETG", "name": "Black PETG", "vendor": vendor},
    }
    _, _, filament = slice_inputs.slice(AD5X, state)
    assert filament["filament_settings_id"] == [expected]


@pytest.mark.parametrize("material, expected", [
    ("Silk PLA", 220),
    ("PLA+", 215),
    ("Matte PLA", 215),
])
def test_pla_subtypes_keep_the_tuned_template_and_variant_temperature(slice_inputs, material, expected):
    state = stale_state(material)
    state["nozzle"]["type"] = "brass"
    slice_inputs.spools[17] = {"id": 17, "filament": {"material": material, "name": material}}
    nozzle, bed, filament = slice_inputs.slice(AD5X, state)
    assert (nozzle, bed) == (expected, 60)
    assert filament["filament_settings_id"] == ["Flashforge Generic PLA"]
    assert filament["filament_flow_ratio"] == ["0.98"]
    assert filament["enable_pressure_advance"] == ["0"]
