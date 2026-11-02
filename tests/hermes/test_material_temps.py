import pytest

from print_handy.core import printer_state


@pytest.mark.parametrize("material,expected", [
    ("PLA", {"nozzle": 210, "bed": 60}),
    ("PLA Tough", {"nozzle": 210, "bed": 60}),
    ("PETG", {"nozzle": 240, "bed": 85}),
    ("ABS", {"nozzle": 245, "bed": 100}),
    ("ASA", {"nozzle": 260, "bed": 105}),
    ("TPU", {"nozzle": 230, "bed": 50}),
    ("PA", {"nozzle": 260, "bed": 90}),
    ("PC", {"nozzle": 275, "bed": 110}),
])
def test_material_fallbacks(material, expected):
    family, temps, sources = printer_state.resolve_material_temperatures(material, {}, config={})
    assert temps == expected
    assert set(sources.values()) == {"material default"}


@pytest.mark.parametrize("material,expected,variant", [
    ("Silk PLA", {"nozzle": 220, "bed": 60}, "SILK PLA"),
    ("PLA+", {"nozzle": 215, "bed": 60}, "PLA+"),
    ("PLA Pro", {"nozzle": 215, "bed": 60}, "PLA+"),
    ("Matte PLA", {"nozzle": 215, "bed": 60}, "MATTE PLA"),
    ("PLA-CF", {"nozzle": 215, "bed": 60}, "PLA-CF"),
    ("PETG-CF", {"nozzle": 245, "bed": 85}, "PETG-CF"),
    ("PA-CF", {"nozzle": 270, "bed": 90}, "PA-CF"),
    ("PA-GF", {"nozzle": 265, "bed": 90}, "REINFORCED"),
])
def test_variant_adjustments_apply_to_defaults_for_all_filaments(material, expected, variant):
    """#512: variant adjustments cover every filament family, not just Silk PLA."""
    family, temps, sources = printer_state.resolve_material_temperatures(material, {}, config={})
    assert temps == expected
    assert sources["nozzle"] == f"material default + {variant} adjustment"
    assert sources["bed"] == "material default"


def test_variant_adjustment_never_overrides_spoolman_temperatures():
    family, temps, sources = printer_state.resolve_material_temperatures(
        "Silk PLA", {"settings_extruder_temp": 215, "settings_bed_temp": 55}, config={})
    assert temps == {"nozzle": 215, "bed": 55}
    assert set(sources.values()) == {"Spoolman filament"}


def test_variant_outer_wall_speed_factors():
    assert printer_state.material_adjustments("Silk PLA")["outer_wall_speed_factor"] == 0.7
    assert printer_state.material_adjustments("Matte PLA")["outer_wall_speed_factor"] == 0.9
    assert printer_state.material_adjustments("PETG")["outer_wall_speed_factor"] == 1.0


def test_variant_outer_wall_caps_and_layer_time():
    silk = printer_state.material_adjustments("Silk PLA")
    assert (silk["outer_wall_speed_max"], silk["slow_down_layer_time_min"]) == (60, 8)
    plain = printer_state.material_adjustments("PLA")
    assert (plain["outer_wall_speed_max"], plain["slow_down_layer_time_min"]) == (None, None)
    matte = printer_state.material_adjustments("Matte PLA")
    assert (matte["outer_wall_speed_max"], matte["slow_down_layer_time_min"]) == (None, None)
    stacked = printer_state.material_adjustments("Silk Matte PLA")
    assert stacked["outer_wall_speed_max"] == 60 and stacked["outer_wall_speed_factor"] == 0.7
    assert (silk["max_volumetric_speed_max"], plain["max_volumetric_speed_max"]) == (12, None)
    assert printer_state.material_adjustments("PETG")["variant"] is None


def test_spoolman_temperatures_override_defaults_independently():
    family, temps, sources = printer_state.resolve_material_temperatures(
        "PETG", {"settings_extruder_temp": 235, "settings_bed_temp": None}, config={})
    assert temps == {"nozzle": 235, "bed": 85}
    assert sources == {"nozzle": "Spoolman filament", "bed": "material default"}


def test_unknown_and_missing_material_fail_closed():
    for material in ("mystery blend", None):
        with pytest.raises(ValueError, match="no PLA fallback"):
            printer_state.resolve_material_temperatures(material, {}, config={})


def test_invalid_spoolman_target_rejected():
    # 0 is Spoolman's "not set" (see tests/test_material_hardening.py); a negative or absurd value is invalid.
    with pytest.raises(ValueError, match="Invalid nozzle temperature"):
        printer_state.resolve_material_temperatures("PLA", {"settings_extruder_temp": -1}, config={})


@pytest.mark.parametrize("material, family, variant, nozzle", [
    # Real-world labels with extra finish or speed words (2026-10-30 audit).
    ("Shiny Silk PLA", "PLA", "SILK PLA", 220),
    ("PLA (Silk)", "PLA", "SILK PLA", 220),
    ("PLA Basic", "PLA", None, 210),
    # Labels the original rules already resolved keep their exact result.
    ("Silk PLA", "PLA", "SILK PLA", 220),
    ("PLA+", "PLA", "PLA+", 215),
    ("Matte PLA", "PLA", "MATTE PLA", 215),
    ("PLA-CF", "PLA-CF", "PLA-CF", 215),
])
def test_finish_words_resolve_to_the_family_with_its_variant_temperature(material, family, variant, nozzle):
    resolved, temps, _ = printer_state.resolve_material_temperatures(material, {}, {"material_temps": {}})
    assert resolved == family and temps["nozzle"] == nozzle
    assert printer_state.material_variant(material) == variant


@pytest.mark.parametrize("material", ["PLA/PHA", "NoPLA+", "Wood", "High Speed PLA", "PETG HF"])
def test_blends_abrasives_and_unknown_names_stay_refused(material):
    with pytest.raises(ValueError, match="Unsupported filament material"):
        printer_state.resolve_material_temperatures(material, {}, {"material_temps": {}})
