"""Flashforge vendor profile for OpenCreator firmware: Kalico behind Moonraker, .gcode only.

This is a separate vendor from flashforge (ZMod over Moonraker), on the
flashforge_stock precedent: a vendor has one start-code family, and
OpenCreator's is not ZMod's. OpenCreator is the FlashForge-C5-Modding-Group
custom firmware for the Creator 5 and Creator 5 Pro: Kalico (a Klipper fork)
plus upstream Moonraker, with a firmware-owned print start (C5_PRINT_START,
synthesized by the firmware dispatcher from the file; the adapter never
writes it into a file) and a firmware-owned print end (C5_PRINT_STOP).
Delivery is plain .gcode only: the ZMod .gcode.3mf package and its
zmod_delivery_format setting do not apply to this vendor at all.
See docs/OPENCREATOR.md.
"""
from ..base import SHARED_SETTINGS, VendorProfile, merge_settings

VENDOR = VendorProfile(
    key="flashforge_opencreator",
    label="Flashforge OpenCreator",
    connection="moonraker",
    start_code="klipper_macro",
    slicers=("orca",),
    file_formats=(".gcode",),
    settings=merge_settings(SHARED_SETTINGS, {
        "printer": {
            "firmware": "OpenCreator Kalico",
            # The firmware's own start and end macros. They name what the
            # firmware runs, not lines Print Handy writes: the documented
            # file start form carries no macro line at all (adapter.py).
            "start_macro": "C5_PRINT_START",
            "end_macro": "C5_PRINT_STOP",
        },
    }),
)
