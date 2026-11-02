"""Anycubic i3 Mega S through OctoPrint: a Marlin machine on OctoPrint's serial connection.

OrcaSlicer's profile for this model declares gcode_flavor "marlin", so it is an OctoPrint
preset here, driven over OctoPrint's serial connection like the other Marlin presets — it
has no Moonraker endpoint. Sourced values: OrcaSlicer v2.5.0-alpha (commit
3880f2c58ec392ed2bd5938ab31c9c7c496c0eb2), Anycubic vendor profiles 02.04.00.09 —
printable area 210 x 210 mm, printable height 205 mm, 0.4 mm nozzle machine profile
(the only nozzle size upstream ships for this model). Templates are Print Handy's own
Marlin template set with those facts substituted (Orca profile content is AGPL-3.0 and is
not republished here; see docs/OCTOPRINT-MARLIN.md).
"""
from ..base import PrinterModel
from . import VENDOR
from .adapter import OctoPrintAdapter

MODEL = PrinterModel(
    key="octoprint.i3megas",
    label="Anycubic i3 Mega S (OctoPrint)",
    adapter=OctoPrintAdapter,
    templates="octoprint_i3megas",
    baseline="protected-settings-baseline.octoprint.i3megas.json",
    vendor=VENDOR,
    supported_nozzle_sizes=(0.4,),
    settings={"printer": {
        "name": "Anycubic i3 Mega S (OctoPrint)", "bed_mm": [210.0, 210.0], "max_print_height_mm": 205.0,
        "nozzle_diameter_mm": 0.4, "filament_slots": 1,
    }},
)
