"""Sovol SV01 Pro (Marlin/OctoPrint) on stock OctoPrint.

Build volume is sourced from OrcaSlicer Sovol SV01 Pro 0.4 nozzle printable area. The numbers are the printable area
and height, not a marketing claim. Templates are Print Handy's own generic
set with that volume substituted. Upstream Orca profile text is not copied.
Not checked on a live printer.
"""
from ..base import PrinterModel
from . import VENDOR
from ..octoprint.adapter import OctoPrintAdapter

MODEL = PrinterModel(
    key="sovol_marlin.sv01_pro",
    label="Sovol SV01 Pro (Marlin/OctoPrint)",
    adapter=OctoPrintAdapter,
    templates="sovol_marlin_sv01_pro",
    baseline="protected-settings-baseline.sovol_marlin.sv01_pro.json",
    vendor=VENDOR,
    supported_nozzle_sizes=(0.2, 0.4, 0.6, 0.8),
    supported_nozzle_types=("brass", "hardened_steel", "ruby", "tungsten_carbide", "diamond"),
    settings={
        "printer": {
            "name": "Sovol SV01 Pro (Marlin/OctoPrint)",
            "bed_mm": [280, 240],
            "max_print_height_mm": 300,
            "nozzle_diameter_mm": 0.4,
            "filament_slots": 1,
        },
    },
)
