"""Flashforge Creator 5 on Reforge firmware: 4 independent toolheads, 256 mm cube.

The same hardware as flashforge.creator5 (ZMod) and
flashforge_opencreator.creator5, on a third custom firmware: Reforge
(Klipper4FlashForge/firmware), a Klipper fork with Moonraker that keeps
the stock MCU firmware. The firmwares are not interchangeable (different
print start, different tool objects), so this is its own model under its
own vendor. The toolheads are a tool changer (T0-T3, Klipper
extruder..extruder3), not an IFS feeder: filament_slots stays 1 and
tool_count names the heads. No chamber heater on the base model.
Templates: templates/orca/flashforge_creator5_reforge, the Creator
pair's Orca templates with the Reforge deltas (docs/REFORGE.md). Built
from Reforge's published source and documentation, not verified on
hardware.
"""
from ..base import PrinterModel
from . import VENDOR
from .adapter import ReforgeAdapter

MODEL = PrinterModel(
    key="flashforge_reforge.creator5",
    label="Flashforge Creator 5 (Reforge)",
    adapter=ReforgeAdapter,
    templates="flashforge_creator5_reforge",
    baseline="protected-settings-baseline.flashforge_reforge.creator5.json",
    supported_nozzle_sizes=(0.25, 0.4, 0.6, 0.8),
    supported_nozzle_types=("stainless_steel", "hardened_steel"),
    vendor=VENDOR,
    color_system="multi_tool",
    # Printer-specific defaults; firmware, macro names, margins, prime tower and cold pull come from VENDOR.
    settings={
        "printer": {
            "name": "Flashforge Creator 5",
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
