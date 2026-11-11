"""Flashforge Creator 5 Pro on ZMod Klipper: 4 independent toolheads, 256 mm cube.

Same toolhead model and print geometry as the Creator 5; the Pro adds the enclosed
frame, active chamber heating and air filtration, which the Orca template records
(support_chamber_temp_control / support_air_filtration) without changing the tool
model. On the Creator models ZMod 1.7.6 runs its stock-Klipper configuration;
its one documented Klipper 13 piece there is the opt-in KLIPPER_C5 parameter
(SAVE_ZMOD_DATA KLIPPER_C5=1, default 0), which uses the Klipper 13 input
shapers. Print Handy does not detect it. Quirks and
templates as for the Creator 5 (see creator5.py); templates:
templates/orca/flashforge_creator5_pro, derived from OrcaSlicer's upstream
Flashforge Creator 5 Pro profile.
"""
from ..base import PrinterModel
from . import VENDOR
from .adapter import ZmodMultiToolAdapter

MODEL = PrinterModel(
    key="flashforge.creator5_pro",
    label="Flashforge Creator 5 Pro (ZMod)",
    adapter=ZmodMultiToolAdapter,
    templates="flashforge_creator5_pro",
    baseline="protected-settings-baseline.flashforge.creator5_pro.json",
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
            # so no IFS behaviour in core switches on; tool_count names the heads.
            "filament_slots": 1,
            "tool_count": 4,
        },
    },
)
