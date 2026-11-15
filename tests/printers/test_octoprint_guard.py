"""OctoPrint family G-code guard (#1469): the Marlin models get the Prusa-style checks.

OctoPrintAdapter used to inherit KlipperAdapter.gcode_problems, whose first move is
``if not self.shared_tool: return []`` — the Bondtech INDX short-circuit. No OctoPrint
model is an INDX host, so every OctoPrint/Marlin model accepted any file unchecked.
The guard (print_handy/printers/octoprint/guard.py) is driven by the facts the models
already declare: bed and height from the model settings, the expected printer_model
from the model's pinned Orca template, the nozzle sizes from the model record.

Temperature limits are the one fact no OctoPrint model declares. The guard therefore
skips the temperature check for the stock models (an undeclared limit stays unknown;
none is invented), and the temperature refusals below declare limits on the model's
own config to pin that a declared limit is enforced for a Creality and an Anycubic
model alike.
"""
from print_handy import printers, settings

CREALITY = "octoprint.ender3"   # template printer_model: Creality Ender-3
ANYCUBIC = "octoprint.kobra"    # template printer_model: Anycubic Kobra
MODELS = (CREALITY, ANYCUBIC)
PRINTER_MODELS = {CREALITY: "Creality Ender-3", ANYCUBIC: "Anycubic Kobra"}


def config_for(key, **printer):
    cfg = settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": key})
    cfg["printer"].update(printer)
    return cfg


def adapter_for(key, **printer):
    return printers.get(key, config_for(key, **printer))


def good_gcode(key, extra_start=""):
    """A clean Marlin file for the model, in the shape Orca writes: config header,
    plain Marlin heat/home start, a locatable toolpath, a Custom end marker."""
    return (f"; printer_model = {PRINTER_MODELS[key]}\n; nozzle_diameter = 0.4\n"
            f"{extra_start}"
            "M140 S60\nM104 S215\nG90\nM83\nG28\nG92 E0\n; CHANGE_LAYER\nG1 Z0.2\n"
            "G1 X10 Y10 E1\n; FEATURE: Custom\nM104 S0\nM140 S0\n")


# --------------------------------------------------------------------------- acceptance

def test_clean_marlin_file_passes_for_creality_and_anycubic():
    for key in MODELS:
        assert adapter_for(key).gcode_problems(good_gcode(key)) == [], key


def test_hand_written_marlin_start_code_passes():
    # The false-positive risk the issue names: a hand-sliced file whose custom
    # start code Marlin CAN execute (homing, probing, waits, a purge line) must
    # not be refused just because it is not the slicer's own sequence. A comment
    # naming the macro it replaced is not an executable line either.
    for key in MODELS:
        text = good_gcode(key, extra_start=(
            "; START_PRINT replaced by the hand-written Marlin sequence below\n"
            "G29 ; probe the bed\nM190 S60\nM109 S215\n"
            "G1 X0.4 Y20 Z0.3 ; move to the purge line start\n"))
        assert adapter_for(key).gcode_problems(text) == [], key


# --------------------------------------------------------------------------- refusals

def test_foreign_macro_start_code_is_refused_for_creality_and_anycubic():
    for key in MODELS:
        a = adapter_for(key)
        for marker in ("START_PRINT BED_TEMP=60 EXTRUDER_TEMP=215", "PRINT_START BED=60 HOTEND=215",
                       "BED_MESH_CALIBRATE", "CHANGE_ZCOLOR"):
            problems = a.gcode_problems(marker + "\n" + good_gcode(key))
            assert any("another vendor's start code" in p for p in problems), (key, marker)


def test_mismatched_printer_model_is_refused_for_creality_and_anycubic():
    for key, other in ((CREALITY, ANYCUBIC), (ANYCUBIC, CREALITY)):
        a = adapter_for(key)
        text = good_gcode(key).replace(PRINTER_MODELS[key], PRINTER_MODELS[other])
        assert any("sliced for" in p for p in a.gcode_problems(text)), key


def test_tool_change_above_t0_is_refused_for_creality_and_anycubic():
    for key in MODELS:
        a = adapter_for(key)
        assert any("single-tool" in p for p in a.gcode_problems(good_gcode(key) + "T1\n")), key
        assert a.gcode_problems(good_gcode(key) + "T0\n") == [], key


def test_declared_temperature_limits_are_refused_for_creality_and_anycubic():
    for key in MODELS:
        a = adapter_for(key, max_hotend_c=260, max_bed_c=110)
        good = good_gcode(key)
        assert a.gcode_problems(good) == [], key
        for command in ("M104 S300", "M109 S261", "M140 S120", "M190 S111"):
            problems = a.gcode_problems(good + command + "\n")
            assert any("exceeds" in p and command.split()[0] in p for p in problems), (key, command)


def test_undeclared_temperature_limits_skip_instead_of_inventing_one():
    # No OctoPrint model declares temperature limits today, so the stock
    # adapters must not refuse a temperature on a limit nobody declared.
    for key in MODELS:
        a = adapter_for(key)
        assert a.gcode_problems(good_gcode(key) + "M104 S300\nM140 S120\n") == [], key


def test_toolpath_outside_the_bed_or_height_is_refused_for_creality_and_anycubic():
    for key in MODELS:
        a = adapter_for(key)
        off_bed = good_gcode(key).replace("G1 X10 Y10 E1", "G1 X500 Y500 E1")
        assert a.gcode_problems(off_bed) != [], key
        too_tall = good_gcode(key).replace("G1 Z0.2", "G1 Z300")
        assert any("Z300" in p for p in a.gcode_problems(too_tall)), key


def test_a_file_whose_toolpath_cannot_be_located_is_refused():
    # Bounds cannot be checked, so the file is refused rather than waved
    # through — the same posture as the Prusa guard.
    for key in MODELS:
        raw = f"; printer_model = {PRINTER_MODELS[key]}\n; nozzle_diameter = 0.4\nG1 X10 Y10 E1\n"
        assert adapter_for(key).gcode_problems(raw) != [], key


def test_unsupported_nozzle_is_refused_where_the_model_declares_sizes():
    # The Kobra preset accepts only the 0.4 mm nozzle upstream profiles.
    a = adapter_for(ANYCUBIC)
    text = good_gcode(ANYCUBIC).replace("= 0.4", "= 0.6")
    assert any("nozzle" in p for p in a.gcode_problems(text))


# --------------------------------------------------------------------------- neighbors

def test_every_octoprint_model_uses_the_guard():
    # All 13 OctoPrint/Marlin models ride the one adapter; each must refuse a
    # Klipper macro file instead of inheriting the INDX empty list.
    for key, model in printers.MODELS.items():
        if not key.startswith("octoprint."):
            continue
        a = printers.get(key, settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": key}))
        problems = a.gcode_problems("START_PRINT BED_TEMP=60 EXTRUDER_TEMP=215\nG28\n")
        assert any("another vendor's start code" in p for p in problems), key


def test_generic_klipper_keeps_its_indx_only_behavior():
    # The decision recorded on #1469: generic Klipper gets no family guard in
    # this change (PRINT_START is its own start macro and it declares no
    # printer_model identity or temperature limits to check against), so its
    # gcode_problems stays the INDX-only check it has always been.
    a = printers.get("klipper.generic",
                     settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": "klipper.generic"}))
    assert a.gcode_problems("PRINT_START BED=60 HOTEND=215\nG28\n") == []
