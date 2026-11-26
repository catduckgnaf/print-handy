"""#623: show Moonraker nozzle, bed, and Z, and warn when they disagree."""
from types import SimpleNamespace

from print_handy.core.printer_geometry import from_configfile, warning_for

PAYLOAD = {"result": {"status": {"configfile": {"config": {
    "extruder": {"nozzle_diameter": "0.4"},
    "stepper_x": {"position_max": "220"},
    "stepper_y": {"position_max": "220"},
    "stepper_z": {"position_max": "250"},
}}}}}


def test_a_known_config_is_shown_and_a_useless_payload_is_named():
    found = from_configfile(PAYLOAD)
    assert found["nozzle_mm"] == 0.4
    assert found["bed_mm"] == [220.0, 220.0]
    assert found["max_z_mm"] == 250.0
    assert "error" in from_configfile({"result": {"hostname": "ad5x"}})


def test_a_mismatch_warns_and_does_not_rewrite():
    model = SimpleNamespace(bed_mm=(300, 300), max_print_height_mm=300,
                            supported_nozzle_sizes=(0.4,))
    text = warning_for(model, {"nozzle_mm": 0.6, "bed_mm": [220, 220], "max_z_mm": 250})
    assert "nozzle 0.6" in text and "generic Klipper" in text and "Apply still uses" in text
    assert warning_for(model, {"nozzle_mm": 0.4, "bed_mm": [300, 300], "max_z_mm": 300}) == ""
    # Registry models keep bed and height under settings["printer"], not attributes.
    nested = SimpleNamespace(supported_nozzle_sizes=(0.4, 0.6),
                             settings={"printer": {"bed_mm": [220, 220], "max_print_height_mm": 220}})
    nested_text = warning_for(nested, {"nozzle_mm": 0.4, "bed_mm": [100, 100], "max_z_mm": 80})
    assert "bed is smaller" in nested_text and "max Z is lower" in nested_text
    assert "nozzle" not in nested_text.split(".", 1)[0]
    assert warning_for(nested, {"nozzle_mm": 0.6, "bed_mm": [220, 220], "max_z_mm": 220}) == ""


def _config(**sections):
    return {"result": {"status": {"configfile": {"config": sections}}}}


def test_the_inset_bed_mesh_does_not_shrink_the_bed():
    # K1-like: the probed mesh covers 5..215 of a 230 mm bed.
    found = from_configfile(_config(
        stepper_x={"position_max": "230", "position_min": "0"},
        stepper_y={"position_max": "230"},
        bed_mesh={"mesh_min": "5, 5", "mesh_max": "215, 215"}))
    assert found["bed_mm"] == [230.0, 230.0]
    model = SimpleNamespace(bed_mm=(220, 220), max_print_height_mm=250, supported_nozzle_sizes=(0.4,))
    assert warning_for(model, found) == ""


def test_the_axis_length_is_position_max_less_position_min_and_the_mesh_is_the_fallback():
    assert from_configfile(_config(stepper_x={"position_max": "245", "position_min": "-5"},
                                   stepper_y={"position_max": "300", "position_min": "20"}))["bed_mm"] == [250.0, 280.0]
    assert from_configfile(_config(bed_mesh={"mesh_min": "5, 5", "mesh_max": "215, 215"}))["bed_mm"] == [210.0, 210.0]


def test_klipper_014_dynamic_coordinate_fixture_has_more_than_four_axes():
    import json
    from pathlib import Path

    fixture = Path(__file__).parents[1] / "fixtures" / "klipper" / "v0.14.0-dynamic-coordinates.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    position = payload["objects"]["toolhead"]["position"]
    axis_map = payload["objects"]["gcode_move"]["axis_map"]
    assert len(position) == 5
    assert position[axis_map["E"]] == 7.0
    assert position[2] == 5.25
