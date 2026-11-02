"""Issue #722: source-profile compatibility warnings — surface only, never mutate."""
from pathlib import Path

from print_handy import settings
from print_handy.core.source_profile_warnings import (machine_identity, machine_limits,
                                                      source_printer_label, source_profile_warnings)

AD5X_TEMPLATES = settings.ROOT / "print_handy" / "templates" / "orca" / "flashforge_ad5x"
AD5X_LABEL = "Flashforge AD5X (ZMod)"
LIMITS = machine_limits(AD5X_TEMPLATES)


def warn(author, target=AD5X_LABEL):
    return source_profile_warnings(author, source_label=source_printer_label(author),
                                   target_label=target, limits=LIMITS)


def test_ad5x_machine_limits_come_from_its_template():
    # Verified target facts: the AD5X machine template's own axis limits.
    assert LIMITS == {"max_axis_speed": 600.0, "max_axis_acceleration": 20000.0}


def test_foreign_out_of_range_value_produces_exactly_one_warning():
    author = {"printer_model_id": "Bambu Lab X1 Carbon", "outer_wall_speed": "700"}
    warnings = warn(author)
    assert len(warnings) == 1
    text = warnings[0]
    assert "outer_wall_speed" in text and "700" in text and "600" in text
    assert "Bambu Lab X1 Carbon" in text  # provenance named
    assert "not changed" in text  # and the no-mutation promise


def test_foreign_acceleration_conflict_is_named():
    author = {"printer_settings_id": "0.20mm Standard @BBL X1C", "default_acceleration": "25000"}
    warnings = warn(author)
    assert len(warnings) == 1
    assert "default_acceleration" in warnings[0] and "20000" in warnings[0]


def test_foreign_fan_above_100_percent_is_named():
    author = {"printer_model_id": "Some Other Printer", "fan_max_speed": "120"}
    warnings = warn(author)
    assert len(warnings) == 1
    assert "fan_max_speed" in warnings[0] and "0-100%" in warnings[0]


def test_foreign_in_range_profile_produces_no_warning():
    # The #715 trigger shape itself: 2 walls, 120 mm/s outer wall — in range, no warning.
    author = {"printer_model_id": "Bambu Lab X1 Carbon", "outer_wall_speed": "120",
              "wall_loops": "2", "bridge_speed": "50", "brim_width": "0",
              "default_acceleration": "10000", "fan_max_speed": "100"}
    assert warn(author) == []


def test_native_profile_produces_no_warning():
    author = {"printer_model_id": AD5X_LABEL, "outer_wall_speed": "700"}
    assert warn(author) == []


def test_unidentified_source_produces_no_warning():
    # No printer identity in the source settings: foreignness is not demonstrated.
    assert warn({"outer_wall_speed": "700"}) == []
    assert warn({}) == []


def test_values_without_a_verified_target_range_are_not_warned():
    # Wall count and brim have no verified target range in the machine template, so a
    # foreign value — however different — is not a demonstrated conflict (#722 scope).
    author = {"printer_model_id": "Bambu Lab X1 Carbon", "wall_loops": "6", "brim_width": "20"}
    assert warn(author) == []


def test_percentage_and_list_forms_are_read():
    author = {"printer_model_id": ["Other Printer"], "outer_wall_speed": ["650"]}
    warnings = warn(author)
    assert len(warnings) == 1 and "650" in warnings[0]


def test_missing_machine_template_gives_no_limits(tmp_path: Path):
    assert machine_limits(tmp_path) == {}
    author = {"printer_model_id": "Other Printer", "outer_wall_speed": "700"}
    assert source_profile_warnings(author, source_label="Other Printer",
                                   target_label=AD5X_LABEL, limits={}) == []


AD5X_ALIASES = machine_identity(AD5X_TEMPLATES)


def test_native_fallback_identity_from_the_target_template_is_not_foreign():
    # The AD5X template's own printer_settings_id names a nozzle and differs from the
    # adapter label "(ZMod)"; it is still this printer, so no warnings.
    author = {"printer_settings_id": "Flashforge AD5X 0.4 nozzle", "outer_wall_speed": "700"}
    assert source_profile_warnings(author, source_label=source_printer_label(author),
                                   target_label=AD5X_LABEL, limits=LIMITS,
                                   aliases=AD5X_ALIASES) == []


def test_distinct_variant_is_not_treated_as_native_by_substring():
    author = {"printer_model_id": "Anycubic Kobra 2 Pro", "outer_wall_speed": "700"}
    warnings = source_profile_warnings(author, source_label=source_printer_label(author),
                                       target_label="Anycubic Kobra 2", limits=LIMITS)
    assert len(warnings) == 1 and "Kobra 2 Pro" in warnings[0]


def test_every_filament_fan_value_is_checked():
    author = {"printer_model_id": "Bambu Lab X1 Carbon", "fan_max_speed": ["100", "120"]}
    warnings = warn(author)
    assert len(warnings) == 1
    assert "fan_max_speed (filament 2)" in warnings[0] and "120" in warnings[0]
