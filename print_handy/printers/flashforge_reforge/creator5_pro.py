"""Flashforge Creator 5 Pro on Reforge firmware: 4 independent toolheads, 256 mm cube.

Same toolhead model and print geometry as the Creator 5 on Reforge (see
creator5.py); the Pro adds the enclosed frame and active chamber heating.
Reforge's Pro config declares ``[heater_generic chamber_heater]`` with
``max_temp: 80`` (``chamber/Creator5Pro.cfg``); Print Handy's chamber
ceiling is FlashForge's 65 C rating instead (see facts.py). Templates:
templates/orca/flashforge_creator5_pro_reforge.
"""
from ..base import PrinterModel
from . import VENDOR
from .adapter import ReforgeAdapter

MODEL = PrinterModel(
    key="flashforge_reforge.creator5_pro",
    label="Flashforge Creator 5 Pro (Reforge)",
    adapter=ReforgeAdapter,
    templates="flashforge_creator5_pro_reforge",
    baseline="protected-settings-baseline.flashforge_reforge.creator5_pro.json",
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
