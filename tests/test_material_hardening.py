"""Material and nozzle hardening (2026-10-31 audit): variant stacking, abrasive nozzle handling, the
seeded "stock" nozzle, Spoolman zero temperatures and the Spoolman material warning."""
import sys

import pytest

from print_handy.core import nozzle, printer_state, slice_guard, spoolman_client
from print_handy.core.nozzle import (AbrasiveNozzleError, NozzleError, NozzleTemperatureError,
                                     check_nozzle_material_safety, check_nozzle_temperature_safety,
                                     normalize_nozzle_type, validate_nozzle_setting)

CFG = {"material_temps": {}}


# ---------------------------------------------------------------- item 4: variant stacking

@pytest.mark.parametrize("label, variants, nozzle_delta, wall_factor", [
    ("Silk PLA", ("SILK PLA",), 10, 0.7),
    ("PLA+ Silk", ("SILK PLA", "PLA+"), 10, 0.7),
    ("Silk PLA+", ("SILK PLA", "PLA+"), 10, 0.7),
    ("Matte PLA+", ("MATTE PLA", "PLA+"), 5, 0.9),
    ("Silk PLA-CF", ("SILK PLA", "PLA-CF"), 10, 0.7),
    ("Matte PLA-CF", ("MATTE PLA", "PLA-CF"), 5, 0.9),
    ("PLA+", ("PLA+",), 5, 1.0),
])
def test_stacked_variants_take_the_larger_delta_and_the_smaller_outer_wall_factor(label, variants, nozzle_delta,
                                                                                  wall_factor):
    adjustments = printer_state.material_adjustments(label)
    assert adjustments["variants"] == variants
    assert adjustments["nozzle_delta"] == nozzle_delta
    assert adjustments["outer_wall_speed_factor"] == wall_factor


def test_resolved_temperatures_follow_the_stacked_adjustment_and_say_so():
    _, temps, sources = printer_state.resolve_material_temperatures("PLA+ Silk", {}, CFG)
    assert temps == {"nozzle": 220, "bed": 60}
    assert sources["nozzle"] == "material default + SILK PLA + PLA+ adjustment"
    _, temps, _ = printer_state.resolve_material_temperatures("Matte PLA+", {}, CFG)
    assert temps["nozzle"] == 215


def test_a_finish_never_hides_the_carbon_fibre_flag():
    assert printer_state.material_adjustments("Silk PLA-CF")["abrasive"] is True
    assert printer_state.material_family("Silk PLA-CF") == "PLA-CF"
    assert printer_state.material_variant("Silk PLA-CF") == "SILK PLA"


# ---------------------------------------------------------------- item 3: abrasive nozzle handling

@pytest.mark.parametrize("label", ["Wood PLA", "Hardwood PLA", "Basalt PLA", "Ceramic PLA", "Copper PLA",
                                   "Bronze PLA", "Steel PLA", "Aluminum PLA", "Magnetic PLA", "Luminous PLA",
                                   "Kevlar Nylon", "Mica PLA", "PA6-CF", "PAHT-CF", "PET-CF", "PETGGF",
                                   "Silk PLA-CF", "Glow PLA", "Marble PLA", "Sparkle PLA", "Galaxy PLA"])
def test_a_brass_nozzle_refuses_every_abrasive_label(label):
    with pytest.raises(AbrasiveNozzleError):
        check_nozzle_material_safety("brass", label)
    check_nozzle_material_safety("hardened_steel", label)  # a hardened nozzle takes it
    result = slice_guard.pre_slice(nozzle_type="brass", materials=[label], nozzle_temp=210,
                                   bed=(256, 256), filament_slots=1)
    assert any("abrasive" in message for message in result.blocked)


@pytest.mark.parametrize("label", ["Silk PLA Metallic Gold", "Metallic PLA", "Pearl PLA", "PLA", "PETG", "PCTG"])
def test_a_brass_nozzle_takes_non_abrasive_labels(label):
    check_nozzle_material_safety("brass", label)


# ---------------------------------------------------------------- item 8: nozzle

def printer_model(monkeypatch, model, nozzle_type=None):
    from print_handy import settings
    printer = {"model": model, **({"nozzle_type": nozzle_type} if nozzle_type else {})}
    monkeypatch.setattr(settings, "current", lambda: {"printer": printer})


@pytest.mark.parametrize("model, expected", [("sovol.sv06", "brass"), ("creality.k1", "brass"),
                                             ("anycubic.kobra2", "brass"), ("flashforge.ad5x", "stainless_steel"),
                                             ("bambulab.a1", "stainless_steel"), ("no.such.model", "brass")])
def test_stock_resolves_per_model_and_is_never_a_blanket_stainless(monkeypatch, model, expected):
    printer_model(monkeypatch, model)
    assert normalize_nozzle_type("stock") == expected


def test_stock_on_a_brass_stock_printer_refuses_abrasive_filament(monkeypatch):
    printer_model(monkeypatch, "sovol.sv06")
    with pytest.raises(AbrasiveNozzleError):
        check_nozzle_material_safety("stock", "PLA-CF")
    printer_model(monkeypatch, "sovol.sv06", nozzle_type="hardened_steel")  # the operator says so
    check_nozzle_material_safety("stock", "PLA-CF")


def test_stock_with_unreadable_settings_reads_as_brass(monkeypatch):
    from print_handy import settings

    def broken():
        raise RuntimeError("no settings")
    monkeypatch.setattr(settings, "current", broken)
    assert normalize_nozzle_type("stock") == "brass"


def test_stock_resolves_to_the_model_own_type_when_one_is_known():
    assert nozzle.recorded_nozzle_type("stock", "stainless_steel") == "stainless_steel"
    assert nozzle.recorded_nozzle_type("hardened_steel", "brass") == "hardened_steel"
    assert nozzle.recorded_nozzle_type(None, None) == nozzle.DEFAULT_NOZZLE_TYPE == "hardened_steel"
    assert nozzle.recorded_nozzle_type("stock", None) == "stock"  # normalizes per model


@pytest.mark.parametrize("model, expected", [("sovol.sv06", "brass"), ("creality.k1", "brass"),
                                             ("anycubic.kobra2", "brass"), ("bambulab.a1", "stainless_steel"),
                                             ("qidi.q1pro", "hardened_steel")])
def test_a_seeded_record_carries_the_models_own_nozzle_type(tmp_path, monkeypatch, model, expected):
    from print_handy import settings
    data = settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": model})
    view = {"printer": {"name": "Bench", "model": model, "bed_mm": [256, 256], "max_print_height_mm": 256,
                        "nozzle_diameter_mm": 0.4, **{k: v for k, v in data["printer"].items() if k == "nozzle_type"}},
            "services": {"moonraker_url": ""}, "material_temps": {"PLA": {"nozzle": 210, "bed": 60}}}
    monkeypatch.setattr(printer_state, "STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("PRINTER_HARDWARE_STATE", raising=False)
    assert printer_state.seed_hardware_record("bench", view) is True
    recorded = printer_state.load("bench")["nozzle"]["type"]
    assert recorded != "stock" and normalize_nozzle_type(recorded) == expected


@pytest.mark.parametrize("raw, mm", [(0.4, 0.4), ("0.4", 0.4), ("0.40mm", 0.4), ("0,4", 0.4), (" 0.6 mm ", 0.6),
                                     ("0.4000001", 0.4), (0.404, 0.4), ("0,25", 0.25)])
def test_nozzle_diameter_accepts_units_commas_and_a_small_tolerance(raw, mm):
    assert validate_nozzle_setting(raw, "brass")[0] == mm


@pytest.mark.parametrize("raw", ["abc", "0.4.5", "", None, True, 0.3, "0.45mm", -0.4])
def test_nozzle_diameter_still_rejects_nonsense_and_unsupported_sizes(raw):
    with pytest.raises(NozzleError):
        validate_nozzle_setting(raw, "brass")


def test_nozzle_diameter_matches_the_given_supported_sizes_with_tolerance():
    assert validate_nozzle_setting("0,4", "brass", supported_sizes=(0.2, 0.4, 0.6))[0] == 0.4
    with pytest.raises(NozzleError, match="not supported"):
        validate_nozzle_setting("0.8mm", "brass", supported_sizes=(0.2, 0.4, 0.6))


def test_the_temperature_message_names_the_canonical_nozzle_type(monkeypatch):
    printer_model(monkeypatch, "sovol.sv06")
    with pytest.raises(NozzleTemperatureError, match="for brass nozzle"):
        check_nozzle_temperature_safety("stock", 999)
    with pytest.raises(NozzleTemperatureError, match="for hardened steel nozzle"):
        check_nozzle_temperature_safety("Hardened Steel", 999)


def test_the_cli_nozzle_flag_takes_units_and_commas():
    assert printer_state._nozzle_arg("0,4") == 0.4
    assert printer_state._nozzle_arg("0.40mm") == 0.4
    with pytest.raises(Exception):
        printer_state._nozzle_arg("wide")


# ---------------------------------------------------------------- item 9: Spoolman zero temperatures

@pytest.mark.parametrize("filament", [{"settings_extruder_temp": 0, "settings_bed_temp": 0},
                                      {"settings_extruder_temp": 0.0, "settings_bed_temp": None},
                                      {"settings_extruder_temp": None, "settings_bed_temp": 0}])
def test_zero_spoolman_temperatures_mean_not_set(filament):
    _, temps, sources = printer_state.resolve_material_temperatures("PETG", filament, CFG)
    assert temps == {"nozzle": 240, "bed": 85}
    assert sources == {"nozzle": "material default", "bed": "material default"}


def test_a_real_spoolman_temperature_still_wins_and_a_bad_one_still_raises():
    _, temps, sources = printer_state.resolve_material_temperatures(
        "PETG", {"settings_extruder_temp": 0, "settings_bed_temp": 70}, CFG)
    assert temps == {"nozzle": 240, "bed": 70} and sources["bed"] == "Spoolman filament"
    for bad in (-5, 400, True):
        with pytest.raises(ValueError, match="Invalid nozzle temperature"):
            printer_state.resolve_material_temperatures("PETG", {"settings_extruder_temp": bad}, CFG)


def test_set_spool_with_zero_spoolman_temperatures_records_the_material_defaults(tmp_path, monkeypatch):
    saved = {}
    st = {"moonraker_url": "http://printer.test:7125", "nozzle": {"diameter_mm": 0.4, "type": "hardened_steel"},
          "spool": {}, "temps_c": {"nozzle": 210, "bed": 60}}
    sp = {"filament": {"name": "Zero PETG", "material": "PETG", "settings_extruder_temp": 0,
                       "settings_bed_temp": 0}}
    monkeypatch.setattr(printer_state, "load", lambda printer_id=None: st)
    monkeypatch.setattr(printer_state, "save", lambda record, printer_id=None: saved.update(record))
    monkeypatch.setattr(printer_state, "fetch_spoolman_spool", lambda spool_id, state=None: sp)
    monkeypatch.setattr(printer_state, "_get", lambda *a, **k: {})
    monkeypatch.setattr(sys, "argv", ["printer_state", "set", "--spool", "9", "--no-sync-klipper"])
    printer_state.main()
    assert saved["temps_c"] == {"nozzle": 240, "bed": 85}
    assert saved["temperature_resolution"]["sources"] == {"nozzle": "material default", "bed": "material default"}


# ---------------------------------------------------------------- item 10: Spoolman material warning

def _check_with_spoolman_material(monkeypatch, recorded, spoolman):
    st = {"moonraker_url": "x", "spoolman_url": "http://spoolman.test",
          "spool": {"spoolman_id": 7, "name": "Black", "material": recorded}}
    reply = {"id": 7, "filament": {"name": "Black", "material": spoolman}, "remaining_weight": 500}
    monkeypatch.setattr(spoolman_client, "request", lambda *a, **k: reply)
    monkeypatch.setattr(printer_state, "spoolman_url", lambda st=None: "http://spoolman.test")
    warnings, facts = [], {}
    printer_state._check_spoolman(st, warnings, facts)
    return warnings


def test_spoolman_material_that_differs_from_the_recorded_spool_warns(monkeypatch):
    warnings = _check_with_spoolman_material(monkeypatch, "PLA", "PETG")
    assert len(warnings) == 1
    assert "'PETG'" in warnings[0] and "'PLA'" in warnings[0] and "material" in warnings[0]


@pytest.mark.parametrize("recorded, spoolman", [("PLA", "PLA+"), ("Silk PLA", "PLA"), ("PET", "PETG"),
                                                ("PA12", "PA6"), ("PLA", None), (None, "PETG")])
def test_the_same_family_or_a_missing_label_does_not_warn(monkeypatch, recorded, spoolman):
    assert _check_with_spoolman_material(monkeypatch, recorded, spoolman) == []


def test_the_spoolman_material_warning_stays_advisory(monkeypatch):
    # check() never turns a Spoolman outcome into a problem (#235): the warning is not a problem.
    warnings = _check_with_spoolman_material(monkeypatch, "PLA", "ABS")
    assert warnings and not any("could not be reached" in w for w in warnings)
