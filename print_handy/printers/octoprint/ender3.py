"""Creality Ender-3 (Marlin) through OctoPrint: the octoprint.marlin preset with Ender-3 facts.

Sourced values: OrcaSlicer v2.5.0-alpha (commit 3880f2c58ec392ed2bd5938ab31c9c7c496c0eb2),
Creality vendor profiles 02.03.02.87 — Marlin flavour, printable area 220 x 220 mm,
printable height 250 mm, 0.4 mm nozzle machine profile (0.2/0.6/0.8 also exist upstream).
Templates are Print Handy's own Marlin template set with those facts substituted (Orca
profile content is AGPL-3.0 and is not republished here; see docs/OCTOPRINT-MARLIN.md).
"""
from ..base import PrinterModel
from . import VENDOR
from .adapter import OctoPrintAdapter

MODEL = PrinterModel(
    key="octoprint.ender3",
    label="Creality Ender-3 (OctoPrint)",
    adapter=OctoPrintAdapter,
    templates="octoprint_ender3",
    baseline="protected-settings-baseline.octoprint.ender3.json",
    vendor=VENDOR,
    settings={"printer": {
        "name": "Creality Ender-3 (OctoPrint)", "bed_mm": [220.0, 220.0], "max_print_height_mm": 250.0,
        "nozzle_diameter_mm": 0.4, "filament_slots": 1,
    }},
)
