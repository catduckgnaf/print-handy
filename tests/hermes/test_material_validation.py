"""Material target safety checks at slicing and print-start boundaries."""
from copy import deepcopy

import pytest

from print_handy.core import model_job, orca_adapter, printer_state


@pytest.mark.parametrize("material,match", [("mystery blend", "no PLA fallback"), ("", "no material")])
def test_slice_refuses_unknown_material_before_creating_slicer_configs(monkeypatch, tmp_path, material, match):
    state = {"spool": {"spoolman_id": 1, "material": material},
             "nozzle": {"diameter_mm": 0.4, "type": "brass"},
             "temps_c": {"nozzle": 210, "bed": 60}}
    monkeypatch.setattr(printer_state.settings, "current", lambda: {"material_temps": printer_state.DEFAULT_MATERIAL_TEMPS})
    with pytest.raises(RuntimeError, match=match):
        printer_state._material_temperature_contract(state)


def test_slice_refuses_mismatched_temperature_provenance():
    state = {"spool": {"material": "PETG"}, "temperature_resolution": {"material_family": "PLA"}}
    with pytest.raises(RuntimeError, match="does not match selected spool"):
        printer_state._material_temperature_contract(state)


def test_legacy_known_material_explicit_targets_remain_allowed(monkeypatch):
    state = {"spool": {"material": "PETG"}, "temps_c": {"nozzle": 235, "bed": 80}}
    monkeypatch.setattr(printer_state.settings, "current", lambda: {"material_temps": printer_state.DEFAULT_MATERIAL_TEMPS})
    assert printer_state._material_temperature_contract(state) is None


@pytest.mark.parametrize("source", ["spool changed by slot sync; resolved per slice",
                                    "spool changed by launch sync; resolved per slice"])
def test_resolved_per_slice_marker_does_not_fail_verification(monkeypatch, source):
    """spool_sync/launch sync write a resolution with no material_family; 2.2.3 compared
    None to the spool's family and refused every slice ("stored temperature material
    does not match selected spool")."""
    state = {"spool": {"material": "Silk PLA"}, "temperature_resolution": {"source": source}}
    monkeypatch.setattr(printer_state.settings, "current", lambda: {"material_temps": printer_state.DEFAULT_MATERIAL_TEMPS})
    assert printer_state._material_temperature_contract(state) is None


def test_resolved_per_slice_marker_still_rejects_unknown_material(monkeypatch):
    state = {"spool": {"material": "mystery blend"}, "temperature_resolution": {"source": "resolved per slice"}}
    monkeypatch.setattr(printer_state.settings, "current", lambda: {"material_temps": printer_state.DEFAULT_MATERIAL_TEMPS})
    with pytest.raises(RuntimeError, match="no PLA fallback"):
        printer_state._material_temperature_contract(state)
