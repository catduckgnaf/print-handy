"""Qidi X-Plus 4 (inherits qidi VENDOR): stock Klipper + Moonraker, 305 x 305 mm bed, 280 mm height, single extruder, enclosed CoreXY.

Sourced values: facts.py (XPLUS4); docs/QIDI.md. Templates: generated on the operator's
machine from their own OrcaSlicer into <state>/orca-profiles/qidi_xplus4 (profiles.py); none are shipped.
"""
from ..base import PrinterModel
from . import VENDOR, facts
from .adapter import QidiAdapter


class XPlus4Adapter(QidiAdapter):
    facts = facts.XPLUS4
    # The 2.1.1 contract forbids Fluidd filament management. Until version
    # detection is verified across QIDI builds, fail closed for this model:
    # keep local Spoolman bookkeeping but do not publish printer-side writes.
    lane_data_write_refusal = (
        "not written: QIDI X-Plus 4 firmware does not support Fluidd filament "
        "management; configure BOX filament settings in QIDI Studio")

    def screen_command(self, slot, material, hex_code):
        return None  # QIDI's vendor contract forbids Fluidd filament management


MODEL = PrinterModel(
    key="qidi.xplus4",
    label="Qidi X-Plus 4",
    adapter=XPlus4Adapter,
    templates="qidi_xplus4",
    generated_templates=True,
    vendor=VENDOR,
    baseline="protected-settings-baseline.qidi.xplus4.json",
    supported_nozzle_sizes=(0.4,),
    supported_nozzle_types=("hardened_steel", "stainless_steel"),
    settings={
        "printer": {
            "name": "Qidi X-Plus 4",
            "bed_mm": list(facts.XPLUS4["bed_mm"].value),
            "max_print_height_mm": facts.XPLUS4["max_print_height_mm"].value,
            "nozzle_diameter_mm": 0.4,
            "nozzle_type": "hardened_steel",
            "filament_slots": 1,
        },
    },
)
