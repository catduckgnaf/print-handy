"""Sovol M1D (Klipper) on stock Klipper.

Build volume is the single-toolhead size from Sovol's M1D specification
(300 x 300 x 350 mm). Seven-tool printing is listed as 240 x 300 x 350 mm
and is not this entry.
OrcaSlicer has no M1D profile. Templates are Print Handy's own generic
set with the single-toolhead volume substituted. This is not a multi-tool
profile. Upstream Orca profile text is not copied. Not checked on a live printer.
"""
from ..base import PrinterModel
from . import VENDOR
from ..klipper.adapter import KlipperAdapter

MODEL = PrinterModel(
    key="sovol.m1d",
    label="Sovol M1D (Klipper)",
    adapter=KlipperAdapter,
    templates="sovol_m1d",
    baseline="protected-settings-baseline.sovol.m1d.json",
    vendor=VENDOR,
    supported_nozzle_sizes=(0.2, 0.4, 0.6, 0.8),
    supported_nozzle_types=("brass", "hardened_steel", "ruby", "tungsten_carbide", "diamond"),
    settings={
        "printer": {
            "name": "Sovol M1D (Klipper)",
            "bed_mm": [300, 300],
            "max_print_height_mm": 350,
            "nozzle_diameter_mm": 0.4,
            "filament_slots": 1,
        },
    },
)
