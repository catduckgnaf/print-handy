"""Flashforge Creator 5 on OpenCreator firmware: 4 independent toolheads, 256 mm cube.

The same hardware as flashforge.creator5, on the other custom firmware:
OpenCreator (Kalico plus upstream Moonraker) instead of ZMod. The two
firmwares are mutually incompatible (different print-start contract,
different tool objects), so this is its own model under its own vendor,
on the flashforge_stock precedent; the key names the firmware variant
after the model, the registry's existing variant shape. Tool facts as
for the ZMod model: the toolheads are a tool changer (T0-T3, Klipper
extruder..extruder3), not an IFS feeder, so filament_slots stays 1 and
tool_count names the heads. Templates:
templates/orca/flashforge_creator5_opencreator, the Creator pair's Orca
templates with the OpenCreator deltas (docs/OPENCREATOR.md). Every
OpenCreator fact here is documented and config verified, not hardware
verified (#1490); see facts.py.
"""
from ..base import PrinterModel
from . import VENDOR
from .adapter import OpenCreatorAdapter

MODEL = PrinterModel(
    key="flashforge_opencreator.creator5",
    label="Flashforge Creator 5 (OpenCreator)",
    adapter=OpenCreatorAdapter,
    templates="flashforge_creator5_opencreator",
    baseline="protected-settings-baseline.flashforge_opencreator.creator5.json",
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
