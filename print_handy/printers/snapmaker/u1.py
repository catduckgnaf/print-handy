"""Snapmaker U1 (inherits snapmaker VENDOR): 270 mm cube, four toolheads, Klipper-based firmware.

Sourced values: facts.py (U1), the manufacturer's published specification.
Every fact is unverified. Templates: templates/orca/snapmaker_u1, written
for this project. Snapmaker and OrcaSlicer profiles are not shipped.
"""
from ..base import PrinterModel
from . import VENDOR
from .adapter import SnapmakerAdapter


MODEL = PrinterModel(
    key="snapmaker.u1",
    label="Snapmaker U1",
    adapter=SnapmakerAdapter,
    templates="snapmaker_u1",
    baseline="protected-settings-baseline.snapmaker.u1.json",
    vendor=VENDOR,
    color_system="multi_tool",
    supported_nozzle_sizes=(0.4,),
    supported_nozzle_types=("stainless_steel", "hardened_steel"),
    settings={
        "printer": {
            "name": "Snapmaker U1",
            "bed_mm": [270.0, 270.0],
            "max_print_height_mm": 270.0,
            "nozzle_diameter_mm": 0.4,
            "nozzle_type": "stainless_steel",
            "filament_slots": 1,
            "tool_count": 4,
        },
    },
)
