"""Flashforge vendor profile for Forge-X firmware: stock-generation Klipper behind Moonraker, .gcode only.

This is a separate vendor from flashforge (ZMod over Moonraker), on the
flashforge_stock / flashforge_opencreator precedent: a vendor has one
start-code family, and Forge-X's is not ZMod's, even though both use a
macro named START_PRINT (Forge-X's parameters and fatal deprecated
parameters differ; see adapter.py). Forge-X (DrA1ex/ff5m) is the
unofficial firmware mod for the Adventurer 5M and Adventurer 5M Pro:
the stock Klipper patched in place plus Moonraker, with Fluidd or
Mainsail and a choice of screen modes. These profiles support the
1.4.2 beta only (#1506): stable 1.4.1 is not supported by these
models, as docs/FORGEX.md records. Delivery is plain .gcode only in
this tranche: the beta's .gcode.3mf acceptance of Print Handy's
ZMod-shaped package is not proven, so the ZMod .gcode.3mf package and
its zmod_delivery_format setting do not apply to this vendor at all.
See docs/FORGEX.md.
"""
from ..base import SHARED_SETTINGS, VendorProfile, merge_settings

VENDOR = VendorProfile(
    key="flashforge_forgex",
    label="Flashforge Forge-X",
    connection="moonraker",
    start_code="klipper_macro",
    slicers=("orca",),
    file_formats=(".gcode",),
    settings=merge_settings(SHARED_SETTINGS, {
        "printer": {
            "firmware": "Forge-X",
            "start_macro": "START_PRINT",
            "end_macro": "END_PRINT",
        },
    }),
)
