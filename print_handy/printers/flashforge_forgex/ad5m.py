"""Flashforge Adventurer 5M on Forge-X firmware: single extruder, 220 mm cube.

The same hardware as flashforge.ad5m, on the other custom firmware:
Forge-X (stock-generation Klipper patched in place plus Moonraker)
instead of ZMod. The two firmwares are mutually incompatible — most
sharply at print start, where both use a macro named START_PRINT with
different parameters, and Forge-X fatally rejects ZMod's deprecated
Z-offset parameters — so this is its own model under its own vendor,
on the flashforge_opencreator precedent. Templates:
templates/orca/flashforge_ad5m_forgex, the AD5M's Orca templates with
the Forge-X deltas (docs/FORGEX.md). Every Forge-X fact here is
documented and config verified, not hardware verified (#1506); see
facts.py.
"""
from ..base import PrinterModel
from . import VENDOR
from .adapter import ForgeXAdapter

MODEL = PrinterModel(
    key="flashforge_forgex.ad5m",
    label="Flashforge AD5M (Forge-X)",
    adapter=ForgeXAdapter,
    templates="flashforge_ad5m_forgex",
    baseline="protected-settings-baseline.flashforge_forgex.ad5m.json",
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
            # Orca templates are tuned for this nozzle. Slicing refuses any other size.
            "nozzle_diameter_mm": 0.4,
            # One extruder, no IFS: a single filament slot.
            "filament_slots": 1,
        },
    },
)
