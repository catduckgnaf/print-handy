"""The build plate on a printer and its material warnings (#1788 phase C).

2026-11-21: a PETG print on a smooth PEI plate fused to it and tore the
coating. Print Handy knew the material but had no idea which plate was on the
printer, so nothing warned.
"""

import pytest

from print_handy import settings
from print_handy.core import build_plate
from print_handy.runtime import discord_print_summary as summary


# ------------------------------------------------------------------ names

@pytest.mark.parametrize("text, expected", [
    ("smooth_pei", "smooth_pei"), ("Smooth PEI", "smooth_pei"), ("smooth pei plate", "smooth_pei"),
    ("High Temp Plate", "smooth_pei"),
    ("textured_pei", "textured_pei"), ("Textured", "textured_pei"),
    ("PEI Powder-Coated", "textured_pei"), ("powder coated plate", "textured_pei"),
    ("Smooth PC", "smooth_pc"), ("cool plate", "cool_plate"),
    (None, None), ("", None), ("  ", None),
])
def test_plate_names_normalize(text, expected):
    assert build_plate.normalize(text) == expected


def test_an_unknown_plate_name_is_refused():
    with pytest.raises(build_plate.BuildPlateError, match="smooth_pei"):
        build_plate.normalize("granite slab")


# ------------------------------------------------------------------ warnings

def test_petg_on_smooth_pei_warns_once():
    lines = build_plate.warnings(["PETG", "PETG"], "smooth_pei")
    assert lines == ["PETG can fuse to smooth PEI and tear the coating. Use the textured plate, "
                     "or a thin layer of glue stick on this one."]


@pytest.mark.parametrize("materials, plate", [
    (["PETG"], "textured_pei"),   # the right plate for PETG
    (["PLA", "PLA+"], "smooth_pei"),  # PLA on smooth PEI is fine
    (["PETG"], None),             # unknown plate never warns
    ([], "smooth_pei"),
])
def test_no_warning_when_the_pair_is_fine_or_the_plate_is_unknown(materials, plate):
    assert build_plate.warnings(materials, plate) == []


def test_pctg_warns_like_petg():
    assert build_plate.warnings(["PCTG"], "smooth_pei")[0].startswith("PCTG can fuse to smooth PEI")


# ------------------------------------------------------------------ settings

def _load(tmp_path, text, environ=None):
    path = tmp_path / "local.yaml"
    path.write_text(text)
    return settings.load(local=path, environ=environ or {})


def test_the_setting_loads_and_normalizes(tmp_path):
    data = _load(tmp_path, "printer:\n  build_plate: PEI Powder-Coated\n")
    assert data["printer"]["build_plate"] == "textured_pei"
    assert "build_plate" not in (settings.unknown_settings_warning() or "")


def test_the_setting_is_optional(tmp_path):
    data = _load(tmp_path, "printer:\n  filament_slots: 4\n")
    assert data["printer"].get("build_plate") is None


def test_an_unknown_plate_in_the_config_is_a_named_error(tmp_path):
    with pytest.raises(settings.SettingsError, match=r"\[printer\] build_plate"):
        _load(tmp_path, "printer:\n  build_plate: granite slab\n")


def test_the_environment_can_set_it(tmp_path):
    data = _load(tmp_path, "printer:\n  filament_slots: 4\n",
                 environ={"PRINT_HANDY__PRINTER__BUILD_PLATE": "smooth pei"})
    assert data["printer"]["build_plate"] == "smooth_pei"


# ------------------------------------------------------------------ the preparation message

def _prep(monkeypatch, plate, row):
    monkeypatch.setattr(build_plate, "plate_for", lambda printer_id=None: plate)
    job = {"title": "Snacking Tongs", "printer_id": "flashforge-ad5x"}
    return summary.format_preparation(job, {"slot": 3, "color": "Black", "seconds": 1200, **row})


def test_the_preparation_message_carries_the_plate_warning(monkeypatch):
    text = _prep(monkeypatch, "smooth_pei", {"material": "PETG"})
    assert text.endswith("**Plate:** PETG can fuse to smooth PEI and tear the coating. Use the textured "
                         "plate, or a thin layer of glue stick on this one.")


def test_a_multi_slot_plate_warns_for_its_petg_slot(monkeypatch):
    text = _prep(monkeypatch, "smooth_pei", {"filaments": [
        {"slot": 1, "color": "White", "material": "PLA"},
        {"slot": 3, "color": "Black", "material": "PETG"}]})
    assert text.count("**Plate:**") == 1 and "PETG can fuse" in text


def test_no_plate_line_without_a_warning(monkeypatch):
    assert "**Plate:**" not in _prep(monkeypatch, "smooth_pei", {"material": "PLA"})
    assert "**Plate:**" not in _prep(monkeypatch, None, {"material": "PETG"})
