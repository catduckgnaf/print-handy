"""Anycubic vendor: the Klipper-based Kobra 2 family on Moonraker.

Everything is offline: profiles run against invented resources (anycubic_fixture) and no
test touches a network or a printer. Live support is unclaimed — there is no Anycubic
hardware to test against, and the family's stock firmware exposes no Moonraker endpoint:
this vendor covers installs where Moonraker is exposed (community firmware such as
Rinkhals). Anycubic's Marlin printers keep the OctoPrint preset route
(test_octoprint_anycubic_presets.py), and the Kobra 3 / S1 generation is deliberately not
covered — its profiles open with the proprietary G9111 start command, which the guard
below refuses outright.
"""
import re

import pytest

from print_handy import printers, settings
from print_handy.printers.anycubic import VENDOR as ANYCUBIC_VENDOR
from print_handy.printers.anycubic import facts as anycubic_facts
from print_handy.printers.anycubic import profiles

from . import anycubic_fixture as fixture

MODELS = {
    "anycubic.kobra2": "KOBRA2",
    "anycubic.kobra2neo": "KOBRA2NEO",
    "anycubic.kobra2pro": "KOBRA2PRO",
    "anycubic.kobra2plus": "KOBRA2PLUS",
    "anycubic.kobra2max": "KOBRA2MAX",
}


def config_for(key, hermes=fixture.HERMES, environ=None):
    env = {"PRINT_HANDY__PRINTER__MODEL": key, "PRINT_HANDY__PATHS__STATE_HOME": str(hermes),
           **(environ or {})}
    return settings.load(local=False, environ=env)


def adapter_for(key, cfg=None):
    return printers.get(key, cfg or config_for(key))


# --------------------------------------------------------------------------- models + settings

@pytest.mark.parametrize("key", sorted(MODELS))
def test_anycubic_registration_and_defaults(key):
    model = printers.MODELS[key]
    assert model.vendor is ANYCUBIC_VENDOR is printers.VENDORS["anycubic"]
    assert model.generated_templates is True
    a = adapter_for(key)
    f = getattr(anycubic_facts, MODELS[key])
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
def test_anycubic_identity_is_a_real_pin(key):
    f = getattr(anycubic_facts, MODELS[key])
    entries = f["orca_identity"].value
    # One pin per checked packaging: 2.4.2's tree, then 2.5.0-alpha's and 2.5.0-beta's tree and packed cache.
    assert [known["vendor_version"] for known in entries] == ["02.04.00.02", "02.04.00.09", "02.04.00.09", "02.04.00.10", "02.04.00.10"]
    for known in entries:
        assert all(re.fullmatch(r"[0-9a-f]{64}", d) for d in known["sha256"].values())
    assert all(fixture.identity(f)["sha256"] != known["sha256"] for known in entries)


@pytest.mark.parametrize("key", sorted(MODELS))
def test_anycubic_fixture_profile_install(key, monkeypatch):
    f = getattr(anycubic_facts, MODELS[key])
    a = adapter_for(key)
    fixture.trust(type(a), f, monkeypatch.setattr)
    fixture.install(a, f)
    assert profiles.problems(a) == []
    assert a.start_plan(60, 215)[0].startswith("G90")  # the fixture's inline start, as the templates carry it


def test_anycubic_profiles_refuse_unrecorded_identity(tmp_path):
    a = adapter_for("anycubic.kobra2")
    root = fixture.resources(tmp_path / "resources", anycubic_facts.KOBRA2)
    with pytest.raises(profiles.ProfileError, match="not a version Print Handy has checked"):
        profiles.install(a, root)


# --------------------------------------------------------------------------- guard

@pytest.mark.parametrize("key", sorted(MODELS))
def test_anycubic_guard(key):
    a = adapter_for(key)
    f = getattr(anycubic_facts, MODELS[key])
    model = f["printer_model"].value
    good = (f"; printer_model = {model}\n; nozzle_diameter = 0.4\n"
            "G90 ; use absolute coordinates\nM83 ; extruder relative mode\nM140 S60\nM104 S215\nG28\nG1 X10 Y10 E1\n")
    assert a.gcode_problems(good) == []  # the family's own start is fully inline; no macro is demanded
    assert any("another printer ecosystem" in p for p in a.gcode_problems("PRINT_START\n" + good))
    assert any("another printer ecosystem" in p for p in a.gcode_problems("START_PRINT\n" + good))
    assert any("another printer ecosystem" in p for p in a.gcode_problems("G9111 bedTemp=60 extruderTemp=215\n" + good))
    assert any("sliced for" in p for p in a.gcode_problems(good.replace(model, "Anycubic Kobra 2 Max")
                                                          if model != "Anycubic Kobra 2 Max"
                                                          else good.replace(model, "Anycubic Kobra 2")))
    over = f["max_hotend_c"].value + 50
    assert any("exceeds" in p for p in a.gcode_problems(good + f"M104 S{over:g}\n"))
