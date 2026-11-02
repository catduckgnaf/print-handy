"""Marlin support uses OctoPrint's serial connection, not direct USB/serial."""
from ..base import PrinterModel
from . import VENDOR
from .adapter import OctoPrintAdapter

MODEL = PrinterModel(
    key='octoprint.marlin', label='Marlin via OctoPrint', adapter=OctoPrintAdapter,
    templates='octoprint_marlin',
    baseline='protected-settings-baseline.octoprint.marlin.json',
    vendor=VENDOR,
    # Printer-specific defaults; firmware, margins, prime tower and cold pull come from VENDOR.
    settings={'printer': {
        'name': 'Marlin via OctoPrint', 'bed_mm': [220.0, 220.0], 'max_print_height_mm': 220.0,
        'nozzle_diameter_mm': 0.4, 'filament_slots': 1,
    }},
)
