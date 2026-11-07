"""Fake-G-code factory contract tests (testing-hardening pass, E3).

The factory (``tests/gcode_factory.py``) is the shared source of
slicer-shaped fakes. These tests pin the contract its consumers rely on
— chiefly foreign-G-code detection in ``ai_fix`` — including the two
detection gaps the #978 deep dive confirmed, pinned as labeled
characterizations so a fix (or a regression) changes them deliberately:

* a Cura file names no ``printer_model`` anywhere Cura actually writes
  one, so detection sees no target model and the file is not flagged
  foreign;
* a multi-plate ``.gcode.3mf`` is judged by its FIRST plate only — a
  foreign model on plate 2 is invisible today.

The pre-existing foreign-G-code tests in ``tests/test_ai_fix_978.py``
are deliberately NOT rewritten onto the factory in this PR: that file
is the #978 lane's surface and its remediation PR is in flight. The
factory is adopted here and by future tests; migrating that file is a
follow-up for after the #978 PR lands.
"""
import gcode_factory as factory

from print_handy.core import ai_fix

OWN = ["AD5X", "Flashforge Adventurer 5M"]


def test_factory_shapes_carry_the_contract_markers():
    for family in ("orca", "bambu", "prusa"):
        text = factory.gcode_text(family, "AD5X")
        assert "M109 S210" in text and "G1 X10.0 Y10.0 E0.50" in text
        # The model comment lives in the FOOTER config block, after the
        # toolpath — the placement real slicers use and detection's
        # head+tail windows exist for.
        assert text.index("; printer_model = AD5X") > text.index("G1 X20.0 Y20.0")
        assert "; nozzle_diameter = 0.4" in text
    cura = factory.gcode_text("cura", "AD5X")
    assert "Cura_SteamEngine" in cura and ";LAYER:0" in cura
    assert "printer_model" not in cura  # Cura writes none — realism


def test_foreign_detection_across_families(tmp_path):
    for family in ("orca", "bambu", "prusa"):
        foreign = factory.write_gcode(tmp_path / f"{family}-foreign.gcode",
                                      family, "Prusa MK4")
        assert ai_fix.gcode_target_model(foreign) == "Prusa MK4", family
        assert ai_fix.is_foreign_gcode(foreign, OWN) == "Prusa MK4", family
        own = factory.write_gcode(tmp_path / f"{family}-own.gcode", family, "AD5X")
        assert ai_fix.is_foreign_gcode(own, OWN) is None, family


def test_footer_beyond_the_head_window_is_still_detected(tmp_path):
    # ~100 KB of toolpath between header and footer config block: the
    # tail window must still find the model (the #925 head+tail shape).
    body = "G1 X1 Y1 E0.1\n" * 8000
    path = factory.write_gcode(tmp_path / "deep.gcode", "prusa", "MK4", body=body)
    assert ai_fix.gcode_target_model(path) == "MK4"
    assert ai_fix.is_foreign_gcode(path, OWN) == "MK4"


def test_cura_file_is_not_flagged_foreign_characterization(tmp_path):
    """KNOWN GAP (#978 dive): Cura headers carry no printer_model
    comment, so a Cura-sliced file is indistinguishable from a file with
    no header — detection returns no model and nothing is flagged. The
    approver-only as-is question is the backstop, not detection."""
    path = factory.write_gcode(tmp_path / "cura.gcode", "cura", "Ender-3")
    assert ai_fix.gcode_target_model(path) is None
    assert ai_fix.is_foreign_gcode(path, OWN) is None


def test_multi_plate_package_is_judged_by_its_first_plate(tmp_path):
    own_first = factory.write_gcode_3mf(tmp_path / "own-first.gcode.3mf", {
        1: factory.gcode_text("orca", "AD5X"),
        2: factory.gcode_text("bambu", "Bambu A1"),
    })
    assert ai_fix.gcode_target_model(own_first) == "AD5X"
    # KNOWN GAP (#978 dive): plate 2's foreign model is invisible today.
    assert ai_fix.is_foreign_gcode(own_first, OWN) is None

    foreign_first = factory.write_gcode_3mf(tmp_path / "foreign-first.gcode.3mf", {
        1: factory.gcode_text("bambu", "Bambu A1"),
        2: factory.gcode_text("orca", "AD5X"),
    })
    assert ai_fix.is_foreign_gcode(foreign_first, OWN) == "Bambu A1"
