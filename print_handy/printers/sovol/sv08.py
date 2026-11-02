"""Sovol SV08 (Klipper) on stock Klipper.

Build volume is sourced from OrcaSlicer Sovol SV08 0.4 nozzle printable area. The numbers are the printable area
and height, not a marketing claim. Templates are Print Handy's own generic
set with that volume substituted. Upstream Orca profile text is not copied.
Not checked on a live printer.
"""
from ..base import PrinterModel
from . import VENDOR
from ..klipper.adapter import KlipperAdapter

MODEL = PrinterModel(
    key="sovol.sv08",
    label="Sovol SV08 (Klipper)",
    adapter=KlipperAdapter,
    templates="sovol_sv08",
    baseline="protected-settings-baseline.sovol.sv08.json",
    vendor=VENDOR,
    supported_nozzle_sizes=(0.2, 0.4, 0.6, 0.8),
    supported_nozzle_types=("brass", "hardened_steel", "ruby", "tungsten_carbide", "diamond"),
    settings={
        "printer": {
            "name": "Sovol SV08 (Klipper)",
            "bed_mm": [350, 350],
            "max_print_height_mm": 345,
            "nozzle_diameter_mm": 0.4,
            "filament_slots": 1,
        },
    },
)
