"""Flashforge Creator 5 on ZMod Klipper: 4 independent toolheads, 256 mm cube.

Quirks: the toolheads are a tool changer (T0-T3, Klipper extruder..extruder3), not an
IFS feeder — the model records tool_count, filament_slots stays 1, and the adapter is the
multi-tool one (tools, not slots; see adapter.ZmodMultiToolAdapter). START_PRINT takes
MESH like its siblings (the C5Pro build's macro reads params.MESH; #301 evidence
addendum), so the start/end G-code is the shared ZMod form. ZMod documents no Klipper
13 support on the Creator models. Templates: templates/orca/flashforge_creator5,
derived from OrcaSlicer's upstream Flashforge Creator 5 profile the same way the
AD5M templates were derived from theirs.
"""
from ..base import PrinterModel
from . import VENDOR
from .adapter import ZmodMultiToolAdapter

MODEL = PrinterModel(
    key="flashforge.creator5",
    label="Flashforge Creator 5 (ZMod)",
    adapter=ZmodMultiToolAdapter,
    templates="flashforge_creator5",
    baseline="protected-settings-baseline.flashforge.creator5.json",
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
            # so no IFS behaviour in core switches on; tool_count names the heads.
            "filament_slots": 1,
            "tool_count": 4,
        },
    },
)
