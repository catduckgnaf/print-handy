"""Creality Ender-3 V3 SE through OctoPrint: despite the V3 name its stock firmware is Marlin-based.

OrcaSlicer's profile for this model declares gcode_flavor "marlin2" (unlike the Klipper
Ender-3 V3 and V3 KE), so it is an OctoPrint preset here, driven over OctoPrint's serial
connection like the other Marlin Enders — it has no Moonraker endpoint. Sourced values:
OrcaSlicer v2.5.0-alpha (commit 3880f2c58ec392ed2bd5938ab31c9c7c496c0eb2), Creality vendor
profiles 02.03.02.87 — printable area 220 x 220 mm, printable height 250 mm, 0.4 mm nozzle
machine profile (0.2/0.6/0.8 also exist upstream). Templates are Print Handy's own Marlin
template set with those facts substituted (Orca profile content is AGPL-3.0 and is not
republished here; see docs/OCTOPRINT-MARLIN.md).
"""
from ..base import PrinterModel
from . import VENDOR
from .adapter import OctoPrintAdapter

MODEL = PrinterModel(
    key="octoprint.ender3v3se",
    label="Creality Ender-3 V3 SE (OctoPrint)",
    adapter=OctoPrintAdapter,
    templates="octoprint_ender3v3se",
    baseline="protected-settings-baseline.octoprint.ender3v3se.json",
    vendor=VENDOR,
    settings={"printer": {
        "name": "Creality Ender-3 V3 SE (OctoPrint)", "bed_mm": [220.0, 220.0], "max_print_height_mm": 250.0,
        "nozzle_diameter_mm": 0.4, "filament_slots": 1,
    }},
)
