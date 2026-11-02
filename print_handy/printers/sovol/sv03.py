"""Sovol SV03 (Klipper) on stock Klipper.

Build volume is sourced from Marlin firmware printer table (Sovol SV-03); OrcaSlicer has no SV03 profile. The numbers are the printable area
and height, not a marketing claim. Templates are Print Handy's own generic
set with that volume substituted. Upstream Orca profile text is not copied.
Not checked on a live printer.
"""
from ..base import PrinterModel
from . import VENDOR
from ..klipper.adapter import KlipperAdapter

MODEL = PrinterModel(
    key="sovol.sv03",
    label="Sovol SV03 (Klipper)",
    adapter=KlipperAdapter,
    templates="sovol_sv03",
    baseline="protected-settings-baseline.sovol.sv03.json",
    vendor=VENDOR,
    supported_nozzle_sizes=(0.2, 0.4, 0.6, 0.8),
    supported_nozzle_types=("brass", "hardened_steel", "ruby", "tungsten_carbide", "diamond"),
    settings={
        "printer": {
            "name": "Sovol SV03 (Klipper)",
            "bed_mm": [360, 360],
            "max_print_height_mm": 410,
            "nozzle_diameter_mm": 0.4,
            "filament_slots": 1,
        },
    },
)
