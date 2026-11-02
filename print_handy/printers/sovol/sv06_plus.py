"""Sovol SV06 Plus (Klipper) on stock Klipper.

Build volume is sourced from OrcaSlicer Sovol SV06 Plus 0.4 nozzle printable area. The numbers are the printable area
and height, not a marketing claim. Templates are Print Handy's own generic
set with that volume substituted. Upstream Orca profile text is not copied.
Not checked on a live printer.
"""
from ..base import PrinterModel
from . import VENDOR
from ..klipper.adapter import KlipperAdapter

MODEL = PrinterModel(
    key="sovol.sv06_plus",
    label="Sovol SV06 Plus (Klipper)",
    adapter=KlipperAdapter,
    templates="sovol_sv06_plus",
    baseline="protected-settings-baseline.sovol.sv06_plus.json",
    vendor=VENDOR,
    supported_nozzle_sizes=(0.2, 0.4, 0.6, 0.8),
    supported_nozzle_types=("brass", "hardened_steel", "ruby", "tungsten_carbide", "diamond"),
    settings={
        "printer": {
            "name": "Sovol SV06 Plus (Klipper)",
            "bed_mm": [300, 300],
            "max_print_height_mm": 340,
            "nozzle_diameter_mm": 0.4,
            "filament_slots": 1,
        },
    },
)
