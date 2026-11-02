"""Stock Adventurer 5X: the AD5X bed on stock firmware, one tool, four IFS bays.

The ZMod AD5X (flashforge.ad5x) is a different printer profile. This one never uses
Moonraker or START_PRINT. It reads IFS occupancy from the stock /detail API and
starts T0 only. That occupancy read is not verified on a live printer.
"""
from ..base import PrinterModel
from . import VENDOR
from .adapter import StockFlashforgeAdapter

MODEL = PrinterModel(
    key="flashforge_stock.ad5x",
    label="Flashforge Adventurer 5X (stock)",
    adapter=StockFlashforgeAdapter,
    # Its own template folder, not the ZMod AD5X's: the stock guard forbids the
    # START_PRINT/END_PRINT/SET_PRINT_STATS_INFO macros in flashforge_ad5x, so the
    # stock template carries plain slicer-owned start/end/layer-change sequences.
    templates="flashforge_ad5x_stock",
    vendor=VENDOR,
    color_system="multi_slot",
    supported_nozzle_sizes=(0.25, 0.4, 0.6, 0.8),
    settings={"printer": {
        "name": "Adventurer 5X",
        "bed_mm": [220.0, 220.0],
        "max_print_height_mm": 220.0,
        "nozzle_diameter_mm": 0.4,
        # Physical IFS bays. Print start uses slot occupancy, not a second tool.
        "filament_slots": 4,
    }},
)
