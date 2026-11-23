"""Generic single extruder Klipper printer: stock Moonraker, PRINT_START/PRINT_END macros.

Quirks: multi-color and multi-plate sources are refused. Copy this file for a Klipper printer that needs
only different numbers, or override a key in config/local.yaml. Templates: templates/orca/klipper_generic.
"""
from ..base import PrinterModel
from . import VENDOR
from .adapter import KlipperAdapter

MODEL = PrinterModel(
    key="klipper.generic",
    label="Generic Klipper",
    adapter=KlipperAdapter,
    templates="klipper_generic",
    baseline="protected-settings-baseline.klipper.generic.json",
    supported_nozzle_sizes=(0.2, 0.25, 0.4, 0.6, 0.8),
    supported_nozzle_types=("brass", "hardened_steel", "stainless_steel", "ruby", "tungsten_carbide", "diamond"),
    vendor=VENDOR,
    # Printer-specific defaults; firmware, macros, margins, prime tower and cold pull come from VENDOR.
    settings={
        "printer": {
            "name": "Generic Klipper",
            "bed_mm": [220.0, 220.0],
            "max_print_height_mm": 250.0,
            "nozzle_diameter_mm": 0.4,
            "filament_slots": 1,
        },
    },
)
