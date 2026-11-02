"""Anycubic Kobra Neo through OctoPrint: a Marlin machine on OctoPrint's serial connection.

OrcaSlicer's profile for this model declares gcode_flavor "marlin2", so it is an OctoPrint
preset here, driven over OctoPrint's serial connection like the other Marlin presets — it
has no Moonraker endpoint. Sourced values: OrcaSlicer v2.5.0-alpha (commit
3880f2c58ec392ed2bd5938ab31c9c7c496c0eb2), Anycubic vendor profiles 02.04.00.09 —
printable area 220 x 220 mm, printable height 250 mm, 0.4 mm nozzle machine profile
(the only nozzle size upstream ships for this model). Templates are Print Handy's own
Marlin template set with those facts substituted (Orca profile content is AGPL-3.0 and is
not republished here; see docs/OCTOPRINT-MARLIN.md).
"""
from ..base import PrinterModel
from . import VENDOR
from .adapter import OctoPrintAdapter

MODEL = PrinterModel(
    key="octoprint.kobraneo",
    label="Anycubic Kobra Neo (OctoPrint)",
    adapter=OctoPrintAdapter,
    templates="octoprint_kobraneo",
    baseline="protected-settings-baseline.octoprint.kobraneo.json",
    vendor=VENDOR,
    supported_nozzle_sizes=(0.4,),
    settings={"printer": {
        "name": "Anycubic Kobra Neo (OctoPrint)", "bed_mm": [220.0, 220.0], "max_print_height_mm": 250.0,
        "nozzle_diameter_mm": 0.4, "filament_slots": 1,
    }},
)
