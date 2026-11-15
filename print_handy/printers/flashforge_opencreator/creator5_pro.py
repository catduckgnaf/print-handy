"""Flashforge Creator 5 Pro on OpenCreator firmware: 4 independent toolheads, 256 mm cube.

Same toolhead model and print geometry as the Creator 5 on OpenCreator
(see creator5.py); the Pro adds the enclosed frame, active chamber
heating and air filtration, which the Orca template records
(support_chamber_temp_control / support_air_filtration) without changing
the tool model. The chamber ceiling is the manufacturer rating, 65 C
(FlashForge's specifications table, read 2026-11-14 for #1490); the
shipped OpenCreator config sets the chamber heater max_temp to 80 C,
15 C above that rating, and Print Handy never uses the config value as
its ceiling (see facts.py). Templates:
templates/orca/flashforge_creator5_pro_opencreator.
"""
from ..base import PrinterModel
from . import VENDOR
from .adapter import OpenCreatorAdapter

MODEL = PrinterModel(
    key="flashforge_opencreator.creator5_pro",
    label="Flashforge Creator 5 Pro (OpenCreator)",
    adapter=OpenCreatorAdapter,
    templates="flashforge_creator5_pro_opencreator",
    baseline="protected-settings-baseline.flashforge_opencreator.creator5_pro.json",
    supported_nozzle_sizes=(0.25, 0.4, 0.6, 0.8),
    supported_nozzle_types=("stainless_steel", "hardened_steel"),
    vendor=VENDOR,
    color_system="multi_tool",
    # Printer-specific defaults; firmware, macro names, margins, prime tower and cold pull come from VENDOR.
    settings={
        "printer": {
            "name": "Flashforge Creator 5 Pro",
            # Usable print area, matching printable_area in the template machine.json.
            "bed_mm": [256.0, 256.0],
            "max_print_height_mm": 256.0,
            # Orca templates are tuned for this nozzle. Slicing refuses any other size.
            "nozzle_diameter_mm": 0.4,
            # Four independent toolheads (tools, not IFS slots): filament_slots stays 1
            # so no IFS behavior in core switches on; tool_count names the heads.
            "filament_slots": 1,
            "tool_count": 4,
        },
    },
)
