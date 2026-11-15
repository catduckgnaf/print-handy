"""Flashforge Adventurer 5M Pro on Forge-X firmware: single extruder, 220 mm cube.

Same print geometry and filament model as the AD5M on Forge-X (see
ad5m.py); the Pro adds the enclosure, camera and air filtration, none
of which change slicing geometry. The Pro has no chamber heater and
no chamber temperature sensor on Forge-X either — the enclosure is
passive, with circulation fans and a servo — so no chamber fact is
recorded for this model; the thermal facts live in the printer
registry thermal record. Templates:
templates/orca/flashforge_ad5m_pro_forgex.
"""
from ..base import PrinterModel
from . import VENDOR
from .adapter import ForgeXAdapter

MODEL = PrinterModel(
    key="flashforge_forgex.ad5m_pro",
    label="Flashforge AD5M Pro (Forge-X)",
    adapter=ForgeXAdapter,
    templates="flashforge_ad5m_pro_forgex",
    baseline="protected-settings-baseline.flashforge_forgex.ad5m_pro.json",
    supported_nozzle_sizes=(0.25, 0.4, 0.6, 0.8),
    supported_nozzle_types=("stainless_steel", "hardened_steel"),
    vendor=VENDOR,
    # Printer-specific defaults; firmware, macro names, margins, prime tower and cold pull come from VENDOR.
    settings={
        "printer": {
            "name": "Flashforge AD5M Pro",
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
