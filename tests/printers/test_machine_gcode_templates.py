"""SAFE-9: shipped machine templates must agree with the verifiers that gate their jobs.

OctoPrint/Marlin (P1-49, P1-100): verify_gcode requires M104 S0 and M140 S0 in the
slicer-owned end sequence, so every octoprint_* template must carry the repo's Marlin
shutdown (the sovol_marlin_* templates' "M104 S0 / M140 S0 / M84") instead of an empty
machine_end_gcode the adapter passes through unchanged.

Stock Flashforge AD5X (P1-59): the stock guard forbids Klipper/ZMod macros outright, so
the stock model needs its own template whose start, end, layer-change and pause sequences
are plain G/M/T code the guard accepts. The ZMod AD5X keeps the macro template.
"""
import json

import pytest

from print_handy import printers, settings
from print_handy.printers.flashforge_stock.guard import stock_gcode_problems

TEMPLATES = settings.ROOT / "print_handy" / "templates" / "orca"


def machine(folder):
    return json.loads((TEMPLATES / folder / "machine.json").read_text())


def octoprint_folders():
    return sorted(p.name for p in TEMPLATES.iterdir() if p.name.startswith("octoprint_"))


@pytest.mark.parametrize("folder", octoprint_folders())
def test_octoprint_template_end_gcode_turns_off_both_heaters(folder):
    end = machine(folder)["machine_end_gcode"]
    lines = [line.strip() for line in end.splitlines()]
    assert "M104 S0" in lines, folder
    assert "M140 S0" in lines, folder


def test_octoprint_template_end_gcode_matches_the_repo_marlin_shutdown():
    # The sovol_marlin_* templates are the repo's existing Marlin shutdown convention.
    assert machine("sovol_marlin_sv01")["machine_end_gcode"] == "M104 S0\nM140 S0\nM84"
    for folder in octoprint_folders():
        assert machine(folder)["machine_end_gcode"] == "M104 S0\nM140 S0\nM84", folder


def marlin_folders():
    folders = []
    for path in sorted(TEMPLATES.iterdir()):
        if not (path / "machine.json").is_file():
            continue
        template = machine(path.name)
        if template.get("gcode_flavor") in ("marlin", "marlin2"):
            folders.append(path.name)
    return folders


@pytest.mark.parametrize("folder", marlin_folders())
def test_marlin_template_layer_change_resets_relative_extruder(folder):
    # Bonus (not in the audit): real Orca 2.5.0-alpha refuses every shipped Marlin
    # template whose layer change omits G92 E0 while use_relative_e_distances is on
    # ("Relative extruder addressing requires resetting the extruder position at each
    # layer... Add G92 E0 to layer_gcode", return -51). Klipper-flavor templates are
    # exempt: Orca does not demand the reset for them (verified by real slice).
    template = machine(folder)
    assert str(template.get("use_relative_e_distances")) == "1", folder
    assert "G92 E0" in template["layer_change_gcode"].splitlines(), folder


def test_stock_ad5x_has_its_own_template_not_the_zmod_one():
    stock = printers.MODELS["flashforge_stock.ad5x"]
    zmod = printers.MODELS["flashforge.ad5x"]
    assert stock.templates != zmod.templates
    assert machine(zmod.templates)["machine_start_gcode"].startswith("START_PRINT")
    assert machine(zmod.templates)["machine_end_gcode"] == "END_PRINT"


def test_stock_ad5x_template_sequences_pass_the_stock_guard():
    stock = printers.MODELS["flashforge_stock.ad5x"]
    template = machine(stock.templates)
    for field in ("machine_start_gcode", "machine_end_gcode", "layer_change_gcode",
                  "before_layer_change_gcode", "machine_pause_gcode"):
        assert stock_gcode_problems(template[field]) == [], (field, template[field])


def test_stock_ad5x_template_heats_waits_homes_and_shuts_down():
    stock = printers.MODELS["flashforge_stock.ad5x"]
    template = machine(stock.templates)
    start = template["machine_start_gcode"]
    assert "M190 S[bed_temperature_initial_layer_single]" in start
    assert "M109 S[nozzle_temperature_initial_layer]" in start
    assert "G28" in start.splitlines()
    end = [line.strip() for line in template["machine_end_gcode"].splitlines()]
    assert "M104 S0" in end and "M140 S0" in end
    # No Klipper statistics or timelapse macros ride along on layer changes.
    assert "SET_PRINT_STATS_INFO" not in template["layer_change_gcode"]
    assert "TIMELAPSE_TAKE_FRAME" not in template["layer_change_gcode"]
    # Real-Orca evidence (2.5.0-alpha, this lane): with the klipper flavor Orca emits
    # SET_VELOCITY_LIMIT lines the stock guard forbids; with marlin2 it requires the
    # per-layer G92 E0 extruder reset for relative E. A full real slice of this
    # template passes stock_gcode_problems with no problems.
    assert template["gcode_flavor"] == "marlin2"
    assert "G92 E0" in template["layer_change_gcode"].splitlines()
