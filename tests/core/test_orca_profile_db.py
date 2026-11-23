"""The committed OrcaSlicer profile database and its resolver (#1816; design record section 3 and 6)."""
from __future__ import annotations

import importlib.util
import json
import tomllib
from pathlib import Path

import pytest

from print_handy.core import filament_type as ft
from print_handy.core import orca_profile_db as db

ROOT = Path(__file__).resolve().parents[2]


def _script():
    spec = importlib.util.spec_from_file_location("build_orca_profile_db", ROOT / "scripts" / "build_orca_profile_db.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- the committed data -------------------------------------------------------------------------------------

def test_database_matches_the_shipped_orca_pin():
    version, revision = _script().pinned_orca()
    meta = db.meta()
    assert (meta["orca_version"], meta["orca_revision"]) == (version, revision), (
        "print_handy/data/orca_profiles was generated from a different OrcaSlicer than docker/orca-prebuilt.sh "
        "pins; regenerate with python3 scripts/build_orca_profile_db.py")


def test_every_vendor_file_listed_in_the_index_exists_and_parses():
    meta = db.meta()
    files = {p.stem for p in db.DATA_DIR.glob("*.jsonl")}
    assert files == set(meta["vendors"])
    assert sum(len(db.vendor_filaments(v)) for v in meta["vendors"]) == meta["filament_presets"]
    assert len(db.index()["printers"]) == meta["printer_presets"]


def test_the_wheel_ships_the_database():
    patterns = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"][
        "package-data"]["print_handy"]
    assert "data/orca_profiles/*.json" in patterns and "data/orca_profiles/*.jsonl" in patterns


def test_notice_credits_orcaslicer_for_the_profiles():
    assert "print_handy/data/orca_profiles" in (ROOT / "NOTICE").read_text(encoding="utf-8")


# --- resolving the AD5X (design record section 3 acceptance examples) --------------------------------------

AD5X = "Flashforge AD5X"


@pytest.mark.parametrize("nozzle, preset", [(0.25, "Flashforge AD5X 0.25 nozzle"), (0.4, "Flashforge AD5X 0.4 nozzle"),
                                            (0.6, "Flashforge AD5X 0.6 nozzle"), (0.8, "Flashforge AD5X 0.8 nozzle")])
def test_printer_preset_by_model_and_nozzle(nozzle, preset):
    assert db.printer_preset(AD5X, nozzle) == preset


def test_printer_preset_takes_preferred_and_folder_by_keyword_only(tmp_path):
    """duckcatgnaf (3fae76f5): the third parameter used to be ``folder``; a folder passed positionally must
    fail loudly instead of being read as ``preferred`` and resolving against the committed database."""
    with pytest.raises(TypeError):
        db.printer_preset(AD5X, 0.4, tmp_path)
    assert db.printer_preset(AD5X, 0.4, preferred="Flashforge AD5X 0.4 nozzle", folder=db.DATA_DIR) \
        == "Flashforge AD5X 0.4 nozzle"


def test_unknown_printer_or_nozzle_has_no_preset():
    assert db.printer_preset(AD5X, 1.2) is None
    assert db.printer_preset("Not A Printer", 0.4) is None


def _resolve(material, name, nozzle=0.4):
    return db.resolve(db.printer_preset(AD5X, nozzle), ft.from_text(material, name))


def test_high_speed_petg_takes_the_vendor_hs_profile():
    r = _resolve("PETG", "High Speed Matte Black PETG")
    assert (r.name, r.filament_id, r.level) == ("Flashforge HS PETG @FF AD5X", "OFDxxTpW", "vendor sub-type")
    assert (r.get("filament_flow_ratio"), r.get("nozzle_temperature"), r.get("nozzle_temperature_initial_layer"),
            r.get("filament_max_volumetric_speed"), r.get("hot_plate_temp")) == ("0.98", "235", "240", "12", "75")
    assert r.get("filament_type") == "PETG"


def test_plain_petg_takes_the_vendor_generic_never_the_hs_profile():
    r = _resolve("PETG", "Black PETG")
    assert (r.name, r.filament_id, r.level) == ("Generic PETG @Flashforge", "OFYPdQJh", "vendor generic")
    assert (r.get("filament_flow_ratio"), r.get("nozzle_temperature")) == ("1", "255")


def test_petg_never_resolves_to_a_pla_profile():
    for name in ("Black PETG", "High Speed PETG", "PETG"):
        assert _resolve("PETG", name).get("filament_type") == "PETG"


def test_nozzle_selects_the_vendors_per_nozzle_profile():
    assert _resolve("PETG", "HS PETG", 0.25).name == "Flashforge HS PETG @FF AD5X 0.25 nozzle"
    assert _resolve("PETG", "HS PETG", 0.6).name == "Flashforge HS PETG @FF AD5X 0.6 nozzle"
    # Generic PETG @Flashforge is not listed for the 0.25 nozzle; the vendor's 0.25 generic serves it.
    assert _resolve("PETG", "PETG", 0.25).name == "Generic PETG @FF AD5M 0.25 Nozzle"


@pytest.mark.parametrize("material, name, profile", [
    ("PLA+", "Bronze PLA+", "Flashforge PLA Pro @FF AD5X"),
    ("PLA", "Silk Gold PLA", "Flashforge PLA Silk @FF AD5X"),
    ("PLA", "Matte White PLA", "Flashforge PLA Matte @FF AD5X"),
])
def test_pla_sub_types(material, name, profile):
    assert _resolve(material, name).name == profile


def test_vendor_brand_differs_from_the_folder_name():
    brands = db.meta()["own_brands"]
    assert (brands["Anker"], brands["BBL"], brands["Prusa"], brands["Flashforge"]) == (
        "AnkerMake", "Bambu Lab", "Prusa Polymers", "Flashforge")
    assert "Sovol" not in brands  # only third-party profiles in that folder
    anker = db.printer_preset("Anker M5", 0.4)
    assert db.resolve(anker, ft.from_text("PLA", "Silk PLA+")).name == "AnkerMake PLA+ Silk @Anker"


def test_spool_brand_selects_that_brands_profile_only_for_that_brand():
    preset = db.printer_preset(AD5X, 0.4)
    assert db.resolve(preset, ft.from_text("PETG", "PETG"), brand="SUNLU").name == "SUNLU PETG @FF AD5M"
    assert db.resolve(preset, ft.from_text("PETG", "PETG"), brand="Polymaker").name == "Generic PETG @Flashforge"
    assert db.resolve(preset, ft.from_text("PETG", "PETG")).name == "Generic PETG @Flashforge"
    # SUNLU's Marble and Wood profiles never serve a plain SUNLU PLA spool.
    assert db.resolve(preset, ft.from_text("PLA", "PLA"), brand="SUNLU").name == "Generic PLA @Flashforge"
    assert db.resolve(preset, ft.from_text("PLA+", "PLA+"), brand="SUNLU").name == "SUNLU PLA+ @FF AD5M"



def test_a_spoolman_vendor_object_is_read_as_the_brand():
    """#1832: Spoolman's filament vendor is {"id", "name"}; it selects like the name and never raises."""
    preset = db.printer_preset(AD5X, 0.4)
    petg = ft.from_text("PETG", "PETG")
    assert db.resolve(preset, petg, brand={"id": 3, "name": "SUNLU"}).name == "SUNLU PETG @FF AD5M"
    assert db.resolve(preset, petg, brand={"id": 4, "name": "Polymaker"}).name == "Generic PETG @Flashforge"
    for unknown in ({"id": 5}, {"id": 6, "name": None}, {"id": 7, "name": "  "}, 42, "", None):
        assert db.resolve(preset, petg, brand=unknown).name == "Generic PETG @Flashforge"

def test_unknown_family_or_no_vendor_profile_falls_back_to_the_library():
    assert db.resolve("Flashforge AD5X 0.4 nozzle", ft.UNKNOWN) is None
    assert db.resolve("No Such Preset", ft.from_text("PLA")) is None


def test_nil_means_inherit_not_zero():
    assert db.value({"filament_retraction_length": ["nil"]}, "filament_retraction_length") is None
    assert db.value({"x": ["0"]}, "x") == "0"


# --- building, writing and comparing (a small synthetic Orca tree) ------------------------------------------

def _tree(tmp_path, flow="0.98"):
    profiles = tmp_path / "profiles"

    def put(vendor, kind, data):
        folder = profiles / vendor / kind
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{data['name']}.json").write_text(json.dumps(data), encoding="utf-8")

    put("Acme", "machine", {"name": "fdm_machine", "instantiation": "false", "printer_model": "Acme One"})
    put("Acme", "machine", {"name": "Acme One 0.4 nozzle", "inherits": "fdm_machine", "instantiation": "true",
                            "nozzle_diameter": ["0.4"], "printer_variant": "0.4"})
    put("Acme", "machine", {"name": "Acme Duo 0.4 nozzle", "instantiation": "true", "printer_model": "Acme Duo",
                            "nozzle_diameter": ["0.4,0.4"], "printer_variant": "0.4"})
    put("OrcaFilamentLibrary", "filament", {"name": "fdm_filament_pet", "instantiation": "false",
                                            "filament_type": ["PETG"], "filament_flow_ratio": ["1"],
                                            "nozzle_temperature": ["240"], "filament_retraction_length": ["nil"]})
    put("Acme", "filament", {"name": "Generic PETG @Acme", "inherits": "fdm_filament_pet", "instantiation": "true",
                             "filament_id": "G1", "compatible_printers": ["Acme One 0.4 nozzle"]})
    put("Acme", "filament", {"name": "AcmeWorks HS PETG @Acme One", "inherits": "Generic PETG @Acme",
                             "instantiation": "true", "filament_id": "H1", "filament_vendor": ["AcmeWorks"],
                             "filament_flow_ratio": [flow], "compatible_printers": ["Acme One 0.4 nozzle"]})
    put("Acme", "filament", {"name": "Brandco HS PETG @Acme One", "inherits": "Generic PETG @Acme",
                             "instantiation": "true", "filament_vendor": ["Brandco"],
                             "compatible_printers": ["Acme One 0.4 nozzle"]})
    # The folder's own line is branded "AcmeWorks", not "Acme" (like Anker's AnkerMake): four of the five
    # branded profiles, so it is the own brand; Brandco is a third-party line in the folder.
    for name in ("AcmeWorks PETG Basic @Acme One", "AcmeWorks PETG Matte @Acme One", "AcmeWorks PETG Marble @Acme One"):
        put("Acme", "filament", {"name": name, "inherits": "Generic PETG @Acme", "instantiation": "true",
                                 "filament_vendor": ["AcmeWorks"], "compatible_printers": ["Acme One 0.4 nozzle"]})
    return profiles


def test_build_resolves_inherits_through_the_library(tmp_path):
    built = db.build(_tree(tmp_path), "9.9.9", "a" * 40)
    hs = next(r for r in built["filaments"]["Acme"] if r["name"] == "AcmeWorks HS PETG @Acme One")
    assert hs["inherits"] == ["Generic PETG @Acme", "fdm_filament_pet"]
    assert hs["values"]["nozzle_temperature"] == ["240"] and hs["values"]["filament_flow_ratio"] == ["0.98"]
    assert "compatible_printers" not in hs["values"] and hs["filament_id"] == "H1"
    assert built["printers"]["Acme Duo 0.4 nozzle"] == {"vendor": "Acme", "printer_model": "Acme Duo",
                                                        "variant": "0.4", "nozzle_diameter": 0.4, "extruders": 2}
    assert all(r["name"] != "fdm_filament_pet" for r in built["filaments"].get("OrcaFilamentLibrary", []))


def test_write_is_deterministic_and_loads_back(tmp_path):
    built = db.build(_tree(tmp_path), "9.9.9", "a" * 40)
    first, second = tmp_path / "one", tmp_path / "two"
    db.write(built, first)
    db.write(built, second)
    for path in first.iterdir():
        assert path.read_bytes() == (second / path.name).read_bytes()
    loaded = db.load(first)
    assert loaded["meta"] == built["meta"] and loaded["printers"] == built["printers"]
    assert loaded["filaments"]["Acme"] == built["filaments"]["Acme"]


def test_resolve_uses_the_folders_own_brand_and_skips_third_party_lines(tmp_path):
    out = tmp_path / "db"
    built = db.build(_tree(tmp_path), "9.9.9", "a" * 40)
    assert built["meta"]["own_brands"] == {"Acme": "AcmeWorks"}
    db.write(built, out)
    preset = "Acme One 0.4 nozzle"
    hs = db.resolve(preset, ft.from_text("PETG", "HS PETG"), folder=out)
    plain = db.resolve(preset, ft.from_text("PETG", "PETG"), folder=out)
    matte = db.resolve(preset, ft.from_text("PETG", "Matte PETG"), folder=out)
    assert (hs.name, hs.level) == ("AcmeWorks HS PETG @Acme One", "vendor sub-type")
    assert (plain.name, plain.level) == ("Generic PETG @Acme", "vendor generic")
    assert matte.name == "AcmeWorks PETG Matte @Acme One"
    # Brandco's HS profile serves only a Brandco spool, and then wins over the folder's own HS profile.
    assert db.resolve(preset, ft.from_text("PETG", "HS PETG"), brand="Brandco", folder=out).name ==         "Brandco HS PETG @Acme One"


def test_an_effect_profile_never_serves_a_spool_without_the_effect(tmp_path):
    out = tmp_path / "db"
    db.write(db.build(_tree(tmp_path), "9.9.9", "a" * 40), out)
    names = [r["name"] for r in db.candidates("Acme One 0.4 nozzle", "PETG", folder=out)]
    assert "AcmeWorks PETG Marble @Acme One" in names
    assert db.resolve("Acme One 0.4 nozzle", ft.from_text("PETG", "PETG"), folder=out).name !=         "AcmeWorks PETG Marble @Acme One"


def test_own_brand_needs_a_clear_majority():
    def rec(name, brand):
        return {"name": name, "values": {"filament_vendor": [brand]}}
    assert db.own_brand([rec("a", "Polymaker"), rec("b", "SUNLU")]) is None  # Sovol's folder
    assert db.own_brand([rec(f"x{i}", "AnkerMake") for i in range(3)] + [rec("Generic PLA", "Generic")]) == "AnkerMake"
    assert db.own_brand([rec(f"x{i}", "Other") for i in range(5)]) is None


def test_a_vendor_preset_matched_only_by_condition_is_refused(tmp_path):
    """The resolver matches the explicit printer list; a condition-only preset must fail the build (the bump
    PR), not silently drop out of resolution."""
    profiles = _tree(tmp_path)
    path = profiles / "Acme" / "filament" / "cond.json"
    path.write_text(json.dumps({"name": "Generic PETG Cond @Acme", "inherits": "fdm_filament_pet",
                                "instantiation": "true",
                                "compatible_printers_condition": "printer_model==\"Acme One\""}))
    with pytest.raises(db.ProfileDatabaseError, match="compatible_printers_condition"):
        db.build(profiles, "9.9.9", "a" * 40)
    path.write_text(json.dumps({"name": "Generic PETG Cond @Acme", "inherits": "fdm_filament_pet",
                                "instantiation": "true"}))
    with pytest.raises(db.ProfileDatabaseError, match="no compatible_printers"):
        db.build(profiles, "9.9.9", "a" * 40)


def test_the_committed_vendor_presets_all_carry_an_explicit_printer_list():
    for vendor in db.meta()["vendors"]:
        if vendor == db.LIBRARY_VENDOR:
            continue
        for record in db.vendor_filaments(vendor):
            assert record["compatible_printers"] and not record["condition"], (vendor, record["name"])


def test_inherits_cycle_and_missing_parent_are_refused(tmp_path):
    profiles = tmp_path / "profiles" / "Acme" / "filament"
    profiles.mkdir(parents=True)
    (profiles / "a.json").write_text(json.dumps({"name": "a", "inherits": "b", "instantiation": "true"}))
    (profiles / "b.json").write_text(json.dumps({"name": "b", "inherits": "a"}))
    with pytest.raises(db.ProfileDatabaseError, match="cycle"):
        db.build(tmp_path / "profiles", "1", "b" * 40)
    (profiles / "b.json").write_text(json.dumps({"name": "b", "inherits": "gone"}))
    with pytest.raises(db.ProfileDatabaseError, match="missing"):
        db.build(tmp_path / "profiles", "1", "b" * 40)


def test_diff_reports_changed_values_for_the_bump_pr(tmp_path):
    old = db.build(_tree(tmp_path / "old"), "1.0", "a" * 40)
    new = db.build(_tree(tmp_path / "new", flow="0.96"), "1.1", "c" * 40)
    report = db.diff(old, new)
    assert report["filaments_changed"] == [{"vendor": "Acme", "name": "AcmeWorks HS PETG @Acme One",
                                            "values": {"filament_flow_ratio": ("0.98", "0.96")},
                                            "printers_added": [], "printers_removed": []}]
    text = db.diff_markdown(report)
    assert "1.0 -> 1.1" in text and "filament_flow_ratio 0.98 -> 0.96" in text


def test_pinned_orca_reads_the_stable_block(tmp_path):
    pin = tmp_path / "orca-prebuilt.sh"
    pin.write_text("case $1 in\n    stable)\n        version=2.5.1\n"
                   "        revision=eef7710cb102fdb51a9bcdbd9efd61047f44774a\n        ;;\nesac\n")
    assert _script().pinned_orca(pin) == ("2.5.1", "eef7710cb102fdb51a9bcdbd9efd61047f44774a")
