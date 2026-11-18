"""Hardware facts for the Flashforge Creator 5 and Creator 5 Pro on Reforge.

Reforge keeps the stock hardware and MCU firmware, so the hardware facts
are the Creator pair's: the 256 mm cube the Creator Orca templates print
in, four toolheads, and FlashForge's own heater ratings (specifications
table, read 2026-11-14 for #1490: extruder 320 C, bed 120 C, the Pro's
active chamber 65 C). None of it is measured on a Reforge printer.

The chamber warning from OpenCreator applies here too: Reforge's Pro
config declares ``[heater_generic chamber_heater]`` with ``max_temp: 80``
(``pkgs/klipper-config/payload/config/chamber/Creator5Pro.cfg``), 15 C
above the rating, so Print Handy's 65 C ceiling is the only thing
holding a chamber target to the rated maximum. The base Creator 5 has
no chamber heater (``chamber/Creator5.cfg`` declares only a
``temperature_sensor chamber``) and records no chamber ceiling.
"""
from __future__ import annotations

from ..flashforge_opencreator.facts import Fact

CREATOR5 = {
    "bed_mm": Fact((256.0, 256.0), "CREATOR_TEMPLATES", False),
    "max_print_height_mm": Fact(256.0, "CREATOR_TEMPLATES", False),
    "tools": Fact(4, "REFORGE", False),
    "kinematics": Fact("CoreXY", "REFORGE", False),
    "nozzle_sizes": Fact((0.25, 0.4, 0.6, 0.8), "CREATOR_TEMPLATES", False),
    "max_hotend_c": Fact(320, "FLASHFORGE", False),
    "max_bed_c": Fact(120, "FLASHFORGE", False),
}

CREATOR5_PRO = {
    **CREATOR5,
    "max_chamber_c": Fact(65, "FLASHFORGE", False),
}
