"""Sovol SV04 (Klipper) on stock Klipper.

Build volume is the single-mode size from the SV04 firmware source specifications
(300 x 300 x 400 mm). Copy mode is about 150 x 300 x 400 and mirror mode about
115 x 300 x 400; those are not this entry. OrcaSlicer has no SV04 profile.
Templates are Print Handy's own generic set with that volume substituted.
This is not an IDEX or dual-tool profile. Upstream Orca profile text is not copied.
Not checked on a live printer.
"""
from ..base import PrinterModel
from . import VENDOR
from ..klipper.adapter import KlipperAdapter

MODEL = PrinterModel(
    key="sovol.sv04",
    label="Sovol SV04 (Klipper)",
    adapter=KlipperAdapter,
    templates="sovol_sv04",
    baseline="protected-settings-baseline.sovol.sv04.json",
    vendor=VENDOR,
    supported_nozzle_sizes=(0.2, 0.4, 0.6, 0.8),
    supported_nozzle_types=("brass", "hardened_steel", "ruby", "tungsten_carbide", "diamond"),
    settings={
        "printer": {
            "name": "Sovol SV04 (Klipper)",
            "bed_mm": [300, 300],
            "max_print_height_mm": 400,
            "nozzle_diameter_mm": 0.4,
            "filament_slots": 1,
        },
    },
)
