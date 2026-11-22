"""Filament family + flags API and the one spool label (#1788 phase B, design sections 1, 2, 4)."""
from __future__ import annotations

import pytest

from print_handy.core import filament_type as ft


def fil(name, material, **extra):
    return {"name": name, "material": material, **extra}


# ------------------------------------------------------------------ the four design labels

@pytest.mark.parametrize("filament,expected", [
    (fil("High Speed Matte Black PETG", "PETG"), "Black Matte, PETG HS"),
    (fil("Black Matte", "PETG", extra={"finish": '"matte"', "hs": "x"}), "Black Matte, PETG"),
    (fil("Matte White PLA", "PLA"), "White Matte, PLA"),
    (fil("Matte White", "PLA"), "White Matte, PLA"),
    (fil("Bronze PLA+", "PLA+"), "Bronze, PLA+"),
    (fil("Bronze", "PLA+"), "Bronze, PLA+"),
    (fil("Dark Green PLA", "PLA"), "Dark Green, PLA"),
    (fil("Dark Green", "PLA"), "Dark Green, PLA"),
])
def test_design_labels(filament, expected):
    assert ft.spool_label(filament) == expected


def test_label_ignores_free_text_order_when_fields_are_structured():
    a = ft.spool_label(fil("HS Matte Black", "PETG"))
    b = ft.spool_label(fil("Black Matte HS", "PETG"))
    assert a == b == "Black Matte, PETG HS"


def test_unknown_parts_are_said_in_place():
    assert ft.spool_label(fil("Grey", ""), color_fallback="color unknown",
                          material_fallback="material unknown") == "Grey, material unknown"
    assert ft.spool_label(fil("Spool #7", ""), color_fallback="color unknown",
                          material_fallback="material unknown") == "color unknown, material unknown"


def test_unknown_family_keeps_the_whole_name_and_raw_material():
    # No family means no name word is known to be a type word: nothing is stripped or guessed.
    assert ft.spool_label(fil("Matte Mystery", "Weird")) == "Matte Mystery, Weird"
    assert ft.spool_label(fil("Red", "PLA/PHA")) == "Red, PLA/PHA"


# ------------------------------------------------------------------ mappings per source

def test_spoolman_material_and_name_flags():
    t = ft.from_spoolman_filament(fil("High Speed Matte Black PETG", "PETG"))
    assert (t.family, t.flags) == ("PETG", frozenset({"high_speed", "matte"}))


@pytest.mark.parametrize("word", ["HS", "HF", "High Speed", "High Flow"])
def test_hs_and_hf_both_map_to_high_speed(word):
    assert ft.from_spoolman_filament(fil(f"{word} Black", "PETG")).flags == {"high_speed"}


def test_spoolman_structured_fields_and_json_encoded_extras():
    t = ft.from_spoolman_filament(fil("Clear", "PETG", extra={"finish": '"matte"', "translucent": "true",
                                                              "glow": "false", "pattern": '"marble"'}))
    assert t.flags == {"matte", "translucent", "imitates_marble"}
    assert t.sources["matte"] == "finish"


def test_structured_finish_replaces_a_finish_read_from_the_name():
    t = ft.from_spoolman_filament(fil("Silk Gold", "PLA", extra={"finish": '"matte"'}))
    assert t.flags == {"matte"}


def test_name_never_changes_the_family():
    t = ft.from_spoolman_filament(fil("PETG Blue", "PLA"))
    assert t.family == "PLA"
    assert ft.from_spoolman_filament(fil("PETG Blue", "")).family is None


def test_name_does_not_set_effect_flags():
    # "Wood" or "Marble" in a color name is a color, not a fill claim.
    assert ft.from_spoolman_filament(fil("Wood Brown", "PLA")).flags == frozenset()


def test_reinforced_family_stays_in_the_family():
    t = ft.from_spoolman_filament(fil("Carbon", "PETG-CF"))
    assert (t.family, t.flags) == ("PETG-CF", frozenset())
    assert ft.from_text("Carbon Fiber PETG").family == "PETG-CF"


def test_unknown_words_are_dropped_not_guessed():
    t = ft.from_text("PLA Sparkly")
    assert t.family == "PLA" and t.flags == frozenset() and t.unmapped == ("SPARKLY",)


def test_blends_and_vendor_lines_have_no_family():
    t = ft.from_text("PLA/PHA")
    assert t.family is None and t.raw_material == "PLA/PHA"
    assert ft.from_text("Mystery").family is None


def test_shore_hardness_is_not_a_flag_or_unmapped_word():
    t = ft.from_text("TPU 95A")
    assert (t.family, t.flags, t.unmapped) == ("TPU", frozenset(), ())


def test_helix_v2_material_wins_family_and_precise_type_adds_flags():
    t = ft.from_helix_lane({"material": "PLA", "filament_type": "PLA Silk"})
    assert (t.family, t.flags) == ("PLA", frozenset({"silk"}))
    t = ft.from_helix_lane({"material": "PETG", "filament_type": "PETG HF"})
    assert t.flags == {"high_speed"}


def test_helix_family_disagreement_does_not_trust_the_precise_flags():
    t = ft.from_helix_lane({"material": "PLA", "filament_type": "PETG HF"})
    assert (t.family, t.flags) == ("PLA", frozenset())


def test_helix_legacy_field_is_read_only_when_v2_fields_are_empty():
    assert ft.from_helix_lane({"helix_material": "PLA+"}).flags == {"plus"}
    t = ft.from_helix_lane({"material": "PETG", "helix_material": "PLA+"})
    assert (t.family, t.flags) == ("PETG", frozenset())
    assert ft.from_helix_lane({"type": "PLA"}).family == "PLA"


def test_helix_filament_id_does_not_set_identity():
    assert ft.from_helix_lane({"filament_id": "GFG02"}) == ft.UNKNOWN


def test_modifiers_map_and_basic_sets_nothing():
    t = ft.from_modifiers("PETG", ["HF", "Basic"])
    assert (t.family, t.flags) == ("PETG", frozenset({"high_speed"}))
    t = ft.from_modifiers("PLA", ["Silk", "Pro", "Matte"])
    assert t.flags == {"silk", "plus", "matte"}
    t = ft.from_modifiers("PLA", ["Unheard"])
    assert t.flags == frozenset() and t.unmapped == ("UNHEARD",)


def test_modifiers_need_a_family_first():
    assert ft.from_modifiers("", ["HF"]) == ft.UNKNOWN


# ------------------------------------------------------------------ source precedence

def test_spoolman_and_lane_read_the_same_spool_to_the_same_type():
    spool = ft.from_spoolman_filament(fil("High Speed Matte Black PETG", "PETG"))
    lane = ft.from_helix_lane({"material": "PETG", "filament_type": "PETG HS Matte"})
    assert (spool.family, spool.flags) == (lane.family, lane.flags)


def test_every_flag_in_the_table_is_known_to_the_code():
    assert set(ft.known_flags()) >= {"high_speed", "plus", "matte", "silk", "translucent"}


def test_the_readers_never_return_a_flag_outside_the_table():
    t = ft.from_spoolman_filament(fil("HS Matte Silk Pro", "PLA+"))
    assert t.flags <= set(ft.known_flags())


@pytest.mark.parametrize("text, family", [("Support PLA", "SUPPORT-PLA"), ("SUPPORT-PA", "SUPPORT-PA"), ("PLA", "PLA")])
def test_support_families_are_recognized(text, family):
    assert ft.from_text(text).family == family


def test_color_name_keeps_type_words_that_were_not_parsed_as_flags():
    plain = ft.from_text("PLA")
    assert ft.color_name("Plus Blue", plain) == "Plus Blue"
    assert ft.color_name("Matte White", ft.from_text("PLA Matte")) == "White"


def test_color_name_keeps_plus_word_when_type_has_no_plus_flag():
    assert ft.color_name("Red PLA+", ft.from_text("PLA")) == "Red PLA+"
    assert ft.color_name("Red PLA+", ft.from_text("PLA+")) == "Red"


@pytest.mark.parametrize("material, expected", [
    ("PLA", "Red PET"),
    ("PCTG", "Red PET"),
    ("PETG", "Red"),
    ("PETG-CF", "Red"),
])
def test_color_name_only_strips_pet_for_the_petg_family(material, expected):
    assert ft.color_name("Red PET", ft.from_text(material)) == expected


def test_paht_resolves_to_the_pa_family():
    # PAHT is normalized by material_family before _VALID_FAMILY is checked.
    assert ft.from_text("PAHT").family == "PA"
    assert ft.from_text("PAHT-CF").family == "PA-CF"
    assert ft.from_text("Polymaker PAHT CF").family == "PA-CF"
