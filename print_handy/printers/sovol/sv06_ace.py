"""Sovol SV06 ACE (Klipper) on stock Klipper.

Build volume is sourced from OrcaSlicer Sovol SV06 ACE 0.4 nozzle printable area. The numbers are the printable area
and height, not a marketing claim. Templates are Print Handy's own generic
set with that volume substituted. Upstream Orca profile text is not copied.
Not checked on a live printer.
"""
from ..base import PrinterModel
from . import VENDOR
from ..klipper.adapter import KlipperAdapter

MODEL = PrinterModel(
    key="sovol.sv06_ace",
    label="Sovol SV06 ACE (Klipper)",
    adapter=KlipperAdapter,
    templates="sovol_sv06_ace",
    baseline="protected-settings-baseline.sovol.sv06_ace.json",
    vendor=VENDOR,
    supported_nozzle_sizes=(0.2, 0.4, 0.6, 0.8),
    supported_nozzle_types=("brass", "hardened_steel", "ruby", "tungsten_carbide", "diamond"),
    settings={
        "printer": {
            "name": "Sovol SV06 ACE (Klipper)",
            "bed_mm": [220, 220],
            "max_print_height_mm": 250,
            "nozzle_diameter_mm": 0.4,
            "filament_slots": 1,
        },
    },
)
