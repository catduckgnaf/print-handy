"""Flashforge AD5M on ZMod Klipper: single extruder, no IFS, 220 mm cube.

Quirks: ZMod's START_PRINT/END_PRINT come from the Orca template (through the single-extruder ZMod
adapter); there are no IFS slots to ask about and the AD5X PRINT_HANDY spool-match macro does not
apply. Templates: templates/orca/flashforge_ad5m, derived from OrcaSlicer's upstream Flashforge
Adventurer 5M profile the same way the AD5X templates were derived from its upstream profile.
"""
from ..base import PrinterModel
from . import VENDOR
from .adapter import ZmodSingleExtruderAdapter

MODEL = PrinterModel(
    key="flashforge.ad5m",
    label="Flashforge AD5M (ZMod)",
    adapter=ZmodSingleExtruderAdapter,
    templates="flashforge_ad5m",
    baseline="protected-settings-baseline.flashforge.ad5m.json",
    supported_nozzle_sizes=(0.25, 0.4, 0.6, 0.8),
    supported_nozzle_types=("stainless_steel", "hardened_steel"),
    vendor=VENDOR,
    # Printer-specific defaults; firmware, macro names, margins, prime tower and cold pull come from VENDOR.
    settings={
        "printer": {
            "name": "Flashforge AD5M",
            # Usable print area, matching printable_area in the template machine.json.
            "bed_mm": [220.0, 220.0],
            "max_print_height_mm": 220.0,
            # The base template's nozzle; other supported sizes use the tuned profiles.
            "nozzle_diameter_mm": 0.4,
            # One extruder, no IFS: a single filament slot.
            "filament_slots": 1,
        },
    },
)
