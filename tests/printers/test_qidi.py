"""Qidi vendor: the Klipper-based Qidi line on Moonraker (Q1 Pro, X-Plus 3, X-Plus 4, X-Max 3, X-Smart 3).

Everything is offline: profiles run against invented resources (qidi_fixture) and no test
touches a network or a printer. Live support is unclaimed — there is no Qidi hardware to
test against. Bed sizes are the pinned Orca profiles' printable areas, not the marketed
build volumes (the X-Smart 3 is marketed 180 x 180 mm; its Orca printable area is
175 x 180 mm and is what is recorded).
"""
import re

import pytest

from print_handy import printers, settings
from print_handy.printers.qidi import VENDOR as QIDI_VENDOR
from print_handy.printers.qidi import facts as qidi_facts
from print_handy.printers.qidi import profiles

from . import qidi_fixture as fixture

MODELS = {
    "qidi.q1pro": "Q1PRO",
    "qidi.xplus3": "XPLUS3",
    "qidi.xplus4": "XPLUS4",
    "qidi.xmax3": "XMAX3",
    "qidi.xsmart3": "XSMART3",
}


def config_for(key, hermes=fixture.HERMES, environ=None):
    env = {"PRINT_HANDY__PRINTER__MODEL": key, "PRINT_HANDY__PATHS__HERMES_HOME": str(hermes),
           **(environ or {})}
    return settings.load(local=False, environ=env)


def adapter_for(key, cfg=None):
    return printers.get(key, cfg or config_for(key))


# --------------------------------------------------------------------------- models + settings

@pytest.mark.parametrize("key", sorted(MODELS))
def test_qidi_registration_and_defaults(key):
    model = printers.MODELS[key]
    assert model.vendor is QIDI_VENDOR is printers.VENDORS["qidi"]
    assert model.generated_templates is True
    a = adapter_for(key)
    f = getattr(qidi_facts, MODELS[key])
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
def test_qidi_identity_is_a_real_pin(key):
    f = getattr(qidi_facts, MODELS[key])
    entries = f["orca_identity"].value
    # One pin per checked packaging: 2.4.2's tree, then 2.5.0-alpha's, 2.5.0-beta's and 2.5.0-rc's tree and packed cache.
    assert [known["vendor_version"] for known in entries] == ["02.04.00.06", "02.04.00.17", "02.04.00.17", "02.04.00.19", "02.04.00.19", "02.04.00.20", "02.04.00.20"]
    for known in entries:
        assert all(re.fullmatch(r"[0-9a-f]{64}", d) for d in known["sha256"].values())
    assert all(fixture.identity(f)["sha256"] != known["sha256"] for known in entries)


@pytest.mark.parametrize("key", sorted(MODELS))
def test_qidi_fixture_profile_install(key, monkeypatch):
    f = getattr(qidi_facts, MODELS[key])
    a = adapter_for(key)
    fixture.trust(type(a), f, monkeypatch.setattr)
    fixture.install(a, f)
    assert profiles.problems(a) == []
    assert a.start_plan(60, 215)[0].startswith("PRINT_START")  # Qidi's own macro leads the template start


def test_qidi_profiles_refuse_unrecorded_identity(tmp_path):
    a = adapter_for("qidi.q1pro")
    root = fixture.resources(tmp_path / "resources", qidi_facts.Q1PRO)
    with pytest.raises(profiles.ProfileError, match="not a version Print Handy has checked"):
        profiles.install(a, root)


# --------------------------------------------------------------------------- guard

@pytest.mark.parametrize("key", sorted(MODELS))
def test_qidi_guard(key):
    a = adapter_for(key)
    f = getattr(qidi_facts, MODELS[key])
    model = f["printer_model"].value
    good = (f"; printer_model = {model}\n; nozzle_diameter = 0.4\n"
            "PRINT_START BED=60 HOTEND=215\nM140 S60\nM104 S215\nG28\nG1 X10 Y10 E1\n")
    assert a.gcode_problems(good) == []  # PRINT_START is Qidi's own macro, never foreign
    assert any("another printer ecosystem" in p for p in a.gcode_problems("START_PRINT\n" + good))
    assert any("sliced for" in p for p in a.gcode_problems(good.replace(model, "Qidi X-Max 3")
                                                          if model != "Qidi X-Max 3"
                                                          else good.replace(model, "Qidi Q1 Pro")))
    over = f["max_hotend_c"].value + 50
    assert any("exceeds" in p for p in a.gcode_problems(good + f"M104 S{over:g}\n"))
