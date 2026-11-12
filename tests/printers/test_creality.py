"""Creality vendor: the Klipper-based Creality OS line on Moonraker (Ender-3 V3 series, K1 series).

Everything is offline: profiles run against invented resources (creality_fixture) and no
test touches a network or a printer. Live support is unclaimed — there is no Creality
hardware to test against. This vendor deliberately covers only the Klipper/Creality-OS
line; Creality's Marlin printers keep the octoprint.marlin route.
"""
import re

import pytest

from print_handy import printers, settings
from print_handy.printers.bambulab import profiles as _shared
from print_handy.printers.creality import VENDOR as CREALITY_VENDOR
from print_handy.printers.creality import facts as creality_facts
from print_handy.printers.creality import profiles

from . import creality_fixture as fixture

MODELS = {
    "creality.ender3v3": "ENDER3V3",
    "creality.ender3v3ke": "ENDER3V3KE",
    "creality.ender3v3plus": "ENDER3V3PLUS",
    "creality.k1": "K1",
    "creality.k1c": "K1C",
    "creality.k1max": "K1MAX",
}


def config_for(key, hermes=fixture.HERMES, environ=None):
    env = {"PRINT_HANDY__PRINTER__MODEL": key, "PRINT_HANDY__PATHS__STATE_HOME": str(hermes),
           **(environ or {})}
    return settings.load(local=False, environ=env)


def adapter_for(key, cfg=None):
    return printers.get(key, cfg or config_for(key))


# --------------------------------------------------------------------------- models + settings

@pytest.mark.parametrize("key", sorted(MODELS))
def test_creality_registration_and_defaults(key):
    model = printers.MODELS[key]
    assert model.vendor is CREALITY_VENDOR is printers.VENDORS["creality"]
    assert model.generated_templates is True
    a = adapter_for(key)
    f = getattr(creality_facts, MODELS[key])
    volume = a.build_volume()
    assert volume.bed_mm == tuple(float(v) for v in f["bed_mm"].value)
    assert volume.max_height_mm == float(f["max_print_height_mm"].value)
    cfg = config_for(key)
    assert cfg["printer"]["slicer"] == "orca" and cfg["printer"]["firmware"] == "Klipper"
    assert cfg["delivery"]["methods"] == ["printer"]  # Moonraker path: printer is the default
    with pytest.raises(settings.SettingsError, match="not supported by model"):
        config_for(key, environ={"PRINT_HANDY__PRINTER__NOZZLE_DIAMETER_MM": "0.6"})


# --------------------------------------------------------------------------- profiles

@pytest.mark.parametrize("key", sorted(MODELS))
def test_creality_identity_is_a_real_pin(key):
    f = getattr(creality_facts, MODELS[key])
    entries = f["orca_identity"].value
    # One pin per checked packaging: 2.4.2's tree, then 2.5.0-alpha's, 2.5.0-beta's and 2.5.0-rc's tree and packed cache (beta kept alpha's vendor version; rc bumped it).
    assert [known["vendor_version"] for known in entries] == ["02.03.02.75", "02.03.02.87", "02.03.02.87", "02.03.02.87", "02.03.02.87", "02.03.02.88", "02.03.02.88"]
    for known in entries:
        assert all(re.fullmatch(r"[0-9a-f]{64}", d) for d in known["sha256"].values())
    assert all(fixture.identity(f)["sha256"] != known["sha256"] for known in entries)


@pytest.mark.parametrize("key", sorted(MODELS))
def test_creality_fixture_profile_install(key, monkeypatch):
    f = getattr(creality_facts, MODELS[key])
    a = adapter_for(key)
    fixture.trust(type(a), f, monkeypatch.setattr)
    fixture.install(a, f)
    assert profiles.problems(a) == []
    assert a.start_plan(60, 215)[0].startswith("START_PRINT")  # the fixture start, as the templates carry it


def test_creality_profiles_refuse_unrecorded_identity(tmp_path):
    a = adapter_for("creality.k1")
    root = fixture.resources(tmp_path / "resources", creality_facts.K1)
    with pytest.raises(profiles.ProfileError, match="not a version Print Handy has checked"):
        profiles.install(a, root)


def test_creality_bare_string_bed_exclusion_reads_as_empty(tmp_path):
    """The K1 line stores bed_exclude_area as a bare "0x0" string; the checker treats it as empty."""
    a = adapter_for("creality.k1")
    root = fixture.resources(tmp_path / "resources", creality_facts.K1)
    upstream, _version = _shared.flatten(root, creality_facts.K1["orca_profiles"].value, vendor=profiles.VENDOR)
    upstream["machine"]["bed_exclude_area"] = "0x0"
    assert not any("bed_exclude_area" in p for p in profiles.describe_problems(a, upstream))


# --------------------------------------------------------------------------- guard

@pytest.mark.parametrize("key", sorted(MODELS))
def test_creality_guard(key):
    a = adapter_for(key)
    f = getattr(creality_facts, MODELS[key])
    model = f["printer_model"].value
    good = (f"; printer_model = {model}\n; nozzle_diameter = 0.4\n"
            "START_PRINT EXTRUDER_TEMP=215 BED_TEMP=60\nM140 S60\nM104 S215\nG28\nG1 X10 Y10 E1\n")
    assert a.gcode_problems(good) == []  # START_PRINT is Creality's own macro, never foreign
    assert any("another printer ecosystem" in p for p in a.gcode_problems("PRINT_START\n" + good))
    assert any("sliced for" in p for p in a.gcode_problems(good.replace(model, "Creality K1 Max")
                                                          if model != "Creality K1 Max"
                                                          else good.replace(model, "Creality K1")))
    over = f["max_hotend_c"].value + 50
    assert any("exceeds" in p for p in a.gcode_problems(good + f"M104 S{over:g}\n"))


def test_creality_guard_accepts_the_ke_fully_inline_start():
    """The Ender-3 V3 KE's real start code calls no macro at all; the guard must not demand one."""
    a = adapter_for("creality.ender3v3ke")
    inline = ("; printer_model = Creality Ender-3 V3 KE\n; nozzle_diameter = 0.4\n"
              "M140 S60\nM104 S215\nG28\nG1 X10 Y10 E1\n")
    assert a.gcode_problems(inline) == []
