"""Flashforge AD5M Pro on ZMod Klipper: single extruder, no IFS, 220 mm cube.

Same print geometry and filament model as the AD5M; the Pro adds the enclosure, camera and air
filtration, none of which change slicing geometry; the thermal facts are
recorded separately in the printer registry thermal record. Quirks and templates as for the AD5M (see ad5m.py);
templates: templates/orca/flashforge_ad5m_pro, derived from OrcaSlicer's upstream Flashforge
Adventurer 5M Pro profile.
"""
from ..base import PrinterModel
from . import VENDOR
from .adapter import ZmodSingleExtruderAdapter

MODEL = PrinterModel(
    key="flashforge.ad5m_pro",
    label="Flashforge AD5M Pro (ZMod)",
    adapter=ZmodSingleExtruderAdapter,
    templates="flashforge_ad5m_pro",
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
            # The base template's nozzle; other supported sizes use the tuned profiles.
            "nozzle_diameter_mm": 0.4,
            # One extruder, no IFS: a single filament slot.
            "filament_slots": 1,
        },
    },
)
