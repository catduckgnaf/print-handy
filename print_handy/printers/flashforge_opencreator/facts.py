"""Hardware facts for the Flashforge Creator 5 and Creator 5 Pro on OpenCreator.

Every value comes from the OpenCreator documentation site and the shipped
opencreator-fs config tree (checked 2026-11-14 for issue #1490), and every
fact is unverified: Print Handy has not measured a Creator 5 or Creator 5
Pro running OpenCreator, and no OpenCreator printer has printed through
Print Handy. Templates are the Creator pair's Orca templates with the
OpenCreator deltas recorded in the template folders, not copies of any
OpenCreator or OrcaSlicer profile shipped by upstream.

Deliberately absent: any temperature ceiling. The docs hardware table and
the shipped Pro config disagree about the chamber maximum (65 C against
80 C), and the nozzle figure is questioned in the docs table itself, so no
max_hotend_c, max_bed_c or max_chamber_c fact is recorded here; the shared
heater-ceiling machinery keeps its unknown fallback for this family.
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
}
