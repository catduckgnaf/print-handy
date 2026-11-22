"""Plate -> Orca bed type mapping and the Web UI printer editor field (#1788)."""

from print_handy.core import build_plate
from print_handy.webui import printer_settings


def test_every_plate_has_an_orca_bed_type_and_its_own_temperature_key():
    assert set(build_plate.ORCA_BED_TYPES) == set(build_plate.PLATES)
    keys = [key for _bed, key in build_plate.ORCA_BED_TYPES.values()]
    assert len(set(keys)) == len(keys)
    assert build_plate.orca_bed_type("smooth_pei") == ("High Temp Plate", "hot_plate_temp")
    assert build_plate.orca_bed_type("textured_pei")[0] == "Textured PEI Plate"


def test_unset_or_unknown_maps_to_nothing():
    assert build_plate.orca_bed_type(None) is None
    assert build_plate.orca_bed_type("") is None
    assert build_plate.plate_from_config({}) is None
    assert build_plate.plate_from_config({"printer": {"build_plate": ""}}) is None
    assert build_plate.plate_from_config({"printer": {"build_plate": "nonsense"}}) is None
    assert build_plate.plate_from_config({"printer": {"build_plate": "Textured"}}) == "textured_pei"


def test_editor_offers_unknown_and_every_plate():
    assert "build_plate" in __import__("print_handy.webui.config", fromlist=["x"]).ENTRY_EDITABLE_KEYS
    row = printer_settings._entry_row.__globals__["Row"]
    from print_handy.webui import catalog
    field = catalog.field_by_id("printer.build_plate")
    assert field.kind == "picker" and field.choices[0] == ""
    assert set(field.choices[1:]) == set(build_plate.PLATES)
    assert printer_settings._choice_label(row("entry.build_plate", "Build plate", "picker"), "smooth_pei") == "smooth PEI"
    assert printer_settings._choice_label(row("entry.build_plate", "Build plate", "picker"), "") == "Unknown (not set)"
