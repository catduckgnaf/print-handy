"""Hardware facts for the Flashforge Creator 5 and Creator 5 Pro on OpenCreator.

Geometry and tool facts come from the OpenCreator documentation site and
the shipped opencreator-fs config tree (checked 2026-11-14 for issue
#1490), and are unverified: Print Handy has not measured a Creator 5 or
Creator 5 Pro running OpenCreator, and no OpenCreator printer has
printed through Print Handy. Templates are the Creator pair's Orca
templates with the OpenCreator deltas recorded in the template folders,
not copies of any OpenCreator or OrcaSlicer profile shipped by upstream.

Temperature ceilings are a separate, settled question (#1490, Kalico
research pass): FlashForge's own specifications table for the Creator 5
Pro (flashforge.com, read 2026-11-14) rates the extruder at 320 C, the
bed at 120 C and the active chamber at 65 C maximum, and the base
Creator 5 table carries the same extruder and bed figures with no
chamber heating at all. Those manufacturer ratings are what is recorded
here, so the shared heater-ceiling machinery uses them. Two warnings
travel with them: OpenCreator's shipped Pro config sets the chamber
heater max_temp to 80 C, 15 C above the manufacturer rating, and the
fork clamps a job's chamber target to that config value at print start,
so Print Handy's 65 C ceiling is the only thing holding a chamber target
to the rated maximum. The base model records no max_chamber_c at all:
it has no chamber heater, and no chamber target is offered for it.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Fact:
    value: object
    source: str
    verified: bool


CREATOR5 = {
    "bed_mm": Fact((256.0, 256.0), "OPENCREATOR", False),
    "max_print_height_mm": Fact(256.0, "OPENCREATOR", False),
    "tools": Fact(4, "OPENCREATOR", False),
    "kinematics": Fact("CoreXY", "OPENCREATOR", False),
    "nozzle_sizes": Fact((0.25, 0.4, 0.6, 0.8), "OPENCREATOR", False),
    # Manufacturer ceilings (FlashForge specifications table, read
    # 2026-11-14): the shared heater-ceiling machinery reads these.
    "max_hotend_c": Fact(320, "FLASHFORGE", False),
    "max_bed_c": Fact(120, "FLASHFORGE", False),
}

CREATOR5_PRO = {
    **CREATOR5,
    # Pro only: the base printer has no chamber heater, so it records
    # no chamber ceiling and no chamber target is offered for it. The
    # shipped OpenCreator config's 80 C max_temp is above this rating
    # and is never the ceiling Print Handy uses.
    "max_chamber_c": Fact(65, "FLASHFORGE", False),
}
