"""Sovol SV06 Plus (Marlin/OctoPrint) on stock OctoPrint.

Build volume is sourced from OrcaSlicer Sovol SV06 Plus 0.4 nozzle printable area. The numbers are the printable area
and height, not a marketing claim. Templates are Print Handy's own generic
set with that volume substituted. Upstream Orca profile text is not copied.
Not checked on a live printer.
"""
from ..base import PrinterModel
from . import VENDOR
from ..octoprint.adapter import OctoPrintAdapter

MODEL = PrinterModel(
    key="sovol_marlin.sv06_plus",
    label="Sovol SV06 Plus (Marlin/OctoPrint)",
    adapter=OctoPrintAdapter,
    templates="sovol_marlin_sv06_plus",
    baseline="protected-settings-baseline.sovol_marlin.sv06_plus.json",
    vendor=VENDOR,
    supported_nozzle_sizes=(0.2, 0.4, 0.6, 0.8),
    supported_nozzle_types=("brass", "hardened_steel", "ruby", "tungsten_carbide", "diamond"),
    settings={
        "printer": {
            "name": "Sovol SV06 Plus (Marlin/OctoPrint)",
            "bed_mm": [300, 300],
            "max_print_height_mm": 340,
            "nozzle_diameter_mm": 0.4,
            "filament_slots": 1,
        },
    },
)
