"""Flashforge vendor profile for Reforge firmware: Klipper behind Moonraker, .gcode only.

A separate vendor from flashforge (ZMod) and flashforge_opencreator, on
the flashforge_stock precedent: a vendor has one start-code family, and
Reforge's is its own. Reforge (Klipper4FlashForge/firmware) replaces the
Creator 5 application layer with a Klipper fork, Moonraker, Mainsail and
Fluidd, and keeps the stock MCU firmware. Its ``[ff_print]`` extra runs
``FF_BEFORE_PRINT_START`` (and through it ``START_PRINT``) before the file
starts, from values it reads out of the file, and ``FF_AFTER_PRINT_END``
(``END_PRINT``) once the job is over. The adapter never writes either
macro into a file. See docs/REFORGE.md.
"""
from ..base import SHARED_SETTINGS, VendorProfile, merge_settings

VENDOR = VendorProfile(
    key="flashforge_reforge",
    label="Flashforge Reforge",
    connection="moonraker",
    start_code="klipper_macro",
    slicers=("orca",),
    file_formats=(".gcode",),
    settings=merge_settings(SHARED_SETTINGS, {
        "printer": {
            "firmware": "Reforge Klipper",
            # The firmware's own start and end entry points. They name what
            # the firmware runs, not lines Print Handy writes: the start form
            # carries no macro line at all (adapter.py).
            "start_macro": "FF_BEFORE_PRINT_START",
            "end_macro": "FF_AFTER_PRINT_END",
        },
    }),
)
