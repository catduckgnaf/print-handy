"""Canonical spool records (spool_canon) and their use at every Spoolman write."""
import pytest

from print_handy.core import spool_canon as c


@pytest.mark.parametrize("raw,canon", [
    ("pla", "PLA"), ("PLA+", "PLA+"), ("pla plus", "PLA+"), ("PLA-Pro", "PLA+"), ("Pro PLA", "PLA+"),
    ("silk", "Silk PLA"), ("PLA Silk", "Silk PLA"), ("petg", "PETG"), ("PET-G", "PETG"),
    ("nylon", "PA"), ("", None), ("unobtanium", None),
])
def test_canonical_material(raw, canon):
    assert c.canonical_material(raw) == canon


def test_pla_and_pla_plus_share_a_swap_group_but_finishes_do_not():
    assert c.swap_group("PLA") == c.swap_group("pla plus") == "PLA"
    assert c.swap_group("Silk PLA") != c.swap_group("PLA")
    assert c.swap_group("PETG") == "PETG"


@pytest.mark.parametrize("color,material,brand,canon", [
    ("Black PLA", "PLA", None, "Black"),
    ("black", "PLA+", None, "Black"),
    ("Matte White PLA+", "PLA+", None, "Matte White"),
    ("rose red blue green", "Silk PLA", None, "Rose Red Blue Green"),
    ("LOCYFENS PLA Shiny Silk Rainbow", "Silk PLA", "LOCYFENS", "Shiny Rainbow"),
    ("Black PLA", None, None, "Black"),
    ("PLA", "PLA", None, "PLA"),
])
def test_canonical_color(color, material, brand, canon):
    assert c.canonical_color(color, material, brand) == canon


def test_canonical_brand():
    assert c.canonical_brand("sunlu") == "SUNLU"
    assert c.canonical_brand("Unknown") is None
    assert c.canonical_brand("generic") is None
    assert c.canonical_brand("some shop") == "Some Shop"
    assert c.canonical_brand("eSUN") == "eSUN"


def test_canonical_spec_refuses_free_text_slips():
    with pytest.raises(c.SpecError):
        c.canonical_spec({"material": "plastic", "color": "Black"})
    with pytest.raises(c.SpecError):
        c.canonical_spec({"material": "PLA", "color": " -- "})
    spec = c.canonical_spec({"material": "pla plus", "color": "black pla+", "brand": "sunlu", "weight": 1000})
    assert spec == {"material": "PLA+", "color": "Black", "brand": "SUNLU", "weight": 1000}


FILAMENTS = [
    {"id": 8, "material": "PLA", "name": "Black PLA", "vendor": {"name": "SUNLU"}, "color_hex": "000000"},
    {"id": 13, "material": "PLA+", "name": "Black", "vendor": {"name": "SUNLU"}, "color_hex": "161616"},
    {"id": 2, "material": "PLA+", "name": "Wood PLA+", "vendor": {"name": "Unknown"}, "color_hex": "B08A5F"},
    {"id": 3, "material": "PLA+", "name": "Wood PLA+", "vendor": {"name": "Unknown"}, "color_hex": "B08A5F"},
]


def test_find_filament_reuses_canonical_match_and_keeps_pla_plus_distinct():
    pick = lambda **spec: (c.find_filament(FILAMENTS, c.canonical_spec(spec)) or {}).get("id")
    assert pick(material="pla+", color="black", brand="sunlu") == 13
    assert pick(material="PLA", color="Black", brand="SUNLU") == 8      # "Black PLA" record
    assert pick(material="PLA+", color="wood", brand="unknown") == 2     # lowest of the duplicates
    assert pick(material="PETG", color="Black", brand="SUNLU") is None


def test_changeover_compatible():
    assert c.changeover_compatible("PLA+", "Black", "161616", "PLA", "Black PLA", "000000")
    assert c.changeover_compatible("PLA", "Jet", "000000", "PLA+", "Black", "1A1A1A")   # same to the eye
    assert not c.changeover_compatible("PLA", "Black", "000000", "Silk PLA", "Black", "000000")
    assert c.changeover_compatible("PLA", "White", "FFFFFF", "PLA+", "Snow", "FAFAFA")
    assert not c.changeover_compatible("PLA", "Red", "E53935", "PLA", "Blue", "1E88E5")


def test_audit_flags_duplicates_noise_and_unused_types():
    notes = c.audit_filaments(FILAMENTS, [{"filament": {"id": 13}}, {"filament": {"id": 2}}])
    joined = "\n".join(notes)
    assert "#8: name 'Black PLA' -> 'Black'" in joined
    assert "duplicate filament types [2, 3]" in joined and "unused copies: [3]" in joined
    assert "#2: vendor 'Unknown' is a placeholder" in joined
    assert "#8: no spools use this type" in joined


def test_create_spool_canonicalizes_and_reuses(monkeypatch):
    from print_handy.runtime import spool_sync
    calls = []

    def client(method, path, body):
        calls.append((method, path, body))
        if method == "GET" and path == "/api/v1/filament":
            return FILAMENTS
        if method == "POST" and path == "/api/v1/spool":
            return {"id": 99, **(body or {})}
        if method == "GET":
            return []
        return {"id": 77, **(body or {})}

    # create_spool takes a client parameter (spool_sync.create_spool); the
    # TypeError-to-skip guard that used to wrap this call predated that
    # parameter and was removed in the 2026-11-07 test-suite cleanup.
    try:
        spool_sync.create_spool({"material": "pla plus", "color": "black", "brand": "sunlu", "grams": 1000},
                                client=client)
    except spool_sync.SpoolError:
        pass
    assert not [c for c in calls if c[0] == "POST" and c[1] == "/api/v1/filament"]


def test_create_spool_refuses_unknown_material():
    from print_handy.runtime import spool_sync
    with pytest.raises(spool_sync.SpoolError):
        spool_sync.create_spool({"material": "mystery goo", "color": "Black"},
                                client=lambda *a: pytest.fail("Spoolman must not be called"))


def test_vendor_blacks_and_whites_match_but_greys_navy_and_cream_do_not():
    assert c.changeover_compatible("PLA", "Jet", "000000", "PLA+", "Onyx", "161616")
    assert c.changeover_compatible("PLA", "Ink", "1A1A1A", "PLA+", "Onyx", "000000")
    assert c.changeover_compatible("PLA", "Snow", "FFFFFF", "PLA+", "Paper", "F0F0F0")
    assert not c.changeover_compatible("PLA", "Grey", "808080", "PLA", "Slate", "6E6E6E")
    assert not c.changeover_compatible("PLA", "Jet", "000000", "PLA", "Navy", "1A1A3A")
    assert not c.changeover_compatible("PLA", "Snow", "FFFFFF", "PLA", "Cream", "F5F0E1")


def test_walkthrough_typed_answers_are_canonical():
    from print_handy.runtime import print_alert_buttons as pab
    assert pab.intake_clean_typed("material", "pla plus", {}) == "PLA+"
    with pytest.raises(ValueError):
        pab.intake_clean_typed("material", "plastic", {})
    assert pab.intake_clean_typed("brand", "unknown", {}) is None
    assert pab.intake_clean_typed("brand", "sunlu", {}) == "SUNLU"
    assert pab.intake_clean_typed("color", "black pla", {"material": "PLA"}) == "Black"
    with pytest.raises(ValueError):
        pab.intake_clean_typed("color", "--", {"material": "PLA"})


def test_walkthrough_material_buttons_fit_discord_rows_and_keep_old_indexes():
    from print_handy.runtime import print_alert_buttons as pab
    assert pab.INTAKE_MATERIALS[:6] == ("PLA", "Silk PLA", "PLA+", "PETG", "TPU", "ABS")
    assert all(c.canonical_material(m) == m for m in pab.INTAKE_MATERIALS)
    # Every legacy button material is reachable through primary -> subtype.
    for m in pab.INTAKE_MATERIALS:
        assert m in c.material_subtypes(c.primary_material(m))


def test_material_tree_covers_every_canonical_material_once():
    flat = [m for subtypes in c.MATERIAL_TREE.values() for m in subtypes]
    assert sorted(flat) == sorted(c.MATERIALS) and len(flat) == len(set(flat))
    assert c.primary_material("pla plus") == "PLA"
    assert c.primary_material("silk") == "PLA"
    assert c.primary_material("PET-G") == "PETG"
    assert c.material_subtypes("pla") == ("PLA", "PLA+", "Silk PLA", "Matte PLA")
    assert c.material_subtypes("nope") == ()


def test_typed_brand_or_color_not_in_picker_needs_explicit_yes():
    from print_handy.runtime import print_alert_buttons as pab
    state = {"material": "PLA", "types": [{"brand": "Sunlu", "material": "PLA", "color": "Black"}]}
    assert not pab.intake_typed_is_new("brand", "sunlu", state)
    assert pab.intake_typed_is_new("brand", "Polymaker", state)
    assert not pab.intake_typed_is_new("color", "black", state)
    assert pab.intake_typed_is_new("color", "Galaxy Teal", state)
    comps = pab.intake_step_components("a1", {**state, "step": "newcheck", "new_field": "color",
                                              "new_value": "Galaxy Teal"})
    ids = [b["custom_id"] for r in comps for b in r["components"]]
    assert "pbintake:new_ok:a1" in ids and "pbintake:new_fix:a1" in ids


def test_brand_picker_skips_placeholders_and_case_duplicates():
    from print_handy.runtime import print_alert_buttons as pab
    opts = pab.intake_brand_options([{"brand": "Unknown", "material": "PLA"}, {"brand": "Generic", "material": "PLA"},
                                     {"brand": "SUNLU", "material": "PLA"}, {"brand": "sunlu", "material": "PLA+"}],
                                    "PLA")
    assert [o["label"] for o in opts][:-2] == ["SUNLU"]  # stored spelling kept, case twin dropped
