"""#1816: a non-PLA spool slices with the Orca vendor profile for its sub-type, never the printer's PLA profile.

The 2026-11-21 PETG spool on the AD5X was sliced with the PLA template's values and its G-code said "Flashforge
Generic PLA" (#123). These slices go through slice_model with the real AD5X templates and the committed Orca
profile database; only Orca itself is stubbed (the slice_inputs fixture captures the filament JSON it receives).
"""
from print_handy.core import filament_type
from print_handy.core import nozzle as nozzle_mod
from print_handy.core import orca_adapter
from print_handy.core import orca_profile_db

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
    assert filament["filament_settings_id"] == ["Flashforge HS PETG @FF AD5X"]
    assert filament["name"] == "Flashforge HS PETG @FF AD5X"
    assert filament["filament_type"] == ["PETG"]
    assert filament["filament_flow_ratio"] == _flow(state, vendor["filament_flow_ratio"][0])
    assert filament["filament_max_volumetric_speed"] == vendor["filament_max_volumetric_speed"] == ["12"]
    assert filament["fan_min_speed"] == vendor["fan_min_speed"]
    # What the vendor layer leaves to the printer and the job.
    assert filament["filament_z_hop_types"] == ["Normal Lift"]
    assert filament["enable_pressure_advance"] == ["0"]


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
    emitted = filament["filament_flow_ratio"][0]
    try:
        as_float = float(emitted)
    except ValueError:
        as_float = None
    assert prov["emitted_flow_ratio"] == as_float


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
