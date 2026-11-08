"""Bambu Lab H3C initial support: facts recorded, model deliberately not registered.

The H3C (Bambu's nine-tool Swaptek changer) arrived with Bambu Studio 02.09.01.70.
Its facts were read from that pinned Studio commit's own files (the device
definition resources/printers/N10.json and the H3C machine profiles — see the H3C
block in facts.py). The pinned OrcaSlicer v2.5.0 carries no H3C profiles, so no
checked Orca identity exists and the model is not registered: these tests pin the
recorded facts AND the deliberate absences, so the gap stays visible and the
registration, when an Orca release brings H3C profiles, is a conscious #356-style
landing rather than a silent drift.
"""
import re

from print_handy import printers
from print_handy.printers.bambulab import facts


def test_h3c_facts_carry_only_known_source_labels():
    for name, fact in facts.H3C.items():
        assert fact.source in ("ORCA", "BAMBU", "DERIVED"), name


def test_h3c_identity_and_geometry_facts():
    f = facts.H3C
    assert f["printer_model"].value == "Bambu Lab H3C"
    # printable_area / printable_height in the pinned Studio machine profile;
    # the same 330 x 320 x 325 mm volume Bambu publishes.
    assert f["bed_mm"].value == (330.0, 320.0)
    assert f["max_print_height_mm"].value == 325.0
    assert f["bed_exclude_area"].value == ()
    assert f["default_plate"].value == "Textured PEI Plate"
    assert f["nozzle_sizes_mm"].value == (0.2, 0.4, 0.6, 0.8)
    assert f["stock_nozzle"].value == (0.4, "hardened_steel")
    # N10.json's temperature ranges at the pinned Studio commit.
    assert f["max_hotend_c"].value == 350
    assert f["max_bed_c"].value == 120


def test_h3c_serial_prefix_is_32x():
    # resources/printers/N10.json at the pinned Bambu Studio commit pairs
    # display_name "Bambu Lab H3C" with sn_prefix "32X": the first three
    # characters of an H3C serial, and the HMS per-model prefix (see
    # test_bambu_hms.py for the resolution path pinned against a synthetic
    # 32X table, ahead of the regenerated table landing).
    assert facts.H3C["serial_prefix"].value == "32X"
    assert re.fullmatch(r"[0-9A-Z]{3}", facts.H3C["serial_prefix"].value)


def test_h3c_tools_are_not_extruders():
    # Nine PrintTools share the toolhead's single FlexFeed extruder. The
    # "extruders" fact (a profile's nozzle_diameter entry count, consumed by
    # the extruder-aware identity/guard checks) must not be claimed for it.
    assert facts.H3C["tools"].value == 9
    assert "extruders" not in facts.H3C
    assert facts.H3C["ams"].value == ("AMS 3H", 9)


def test_h3c_has_no_orca_identity_yet():
    # The blocker, pinned: the pinned OrcaSlicer v2.5.0-rc BBL index and machine
    # folder contain no H3C profiles, so no profile names or identities can be
    # recorded honestly. When a pinned Orca release brings them, this test is
    # the one to retire as the model registers the #356 way.
    for key in ("orca_profiles", "orca_filaments", "orca_identity"):
        assert key not in facts.H3C


def test_h3c_is_not_a_registered_model_yet():
    # Registration without a checked identity is not a supported shape (the
    # profile checks index the identity fact directly). Until an OrcaSlicer
    # release carrying H3C profiles is pinned, bambulab.h3c stays unselectable.
    assert "bambulab.h3c" not in printers.MODELS
