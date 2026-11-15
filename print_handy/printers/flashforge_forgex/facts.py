"""Hardware facts for the Flashforge Adventurer 5M and 5M Pro on Forge-X.

Every value comes from the Forge-X (DrA1ex/ff5m) documentation and the
shipped config tree of the 1.4.2 beta branch (checked 2026-11-15 for
issue #1506), and every fact is unverified: Print Handy has not
measured an Adventurer 5M or 5M Pro running Forge-X, and no Forge-X
printer has printed through Print Handy. Templates are the AD5M pair's
Orca templates with the Forge-X deltas recorded in the template
folders, not copies of any Forge-X or OrcaSlicer profile shipped by
upstream.

These models support the 1.4.2 beta only (#1506); stable 1.4.1 is
not a supported line for them. Temperature ceilings: the extruder
maximum (350 C) and the bed maximum (130 C) are both sourced from
the beta branch's shipped default printer config. Deliberately absent: any chamber fact.
Neither model has a chamber heater or a chamber temperature sensor on
Forge-X (the Pro enclosure is passive, with circulation fans and a
servo), so no max_chamber_c fact is recorded here and the shared
heater-ceiling machinery keeps its unknown fallback for chamber.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Fact:
    value: object
    source: str
    verified: bool


AD5M = {
    "bed_mm": Fact((220.0, 220.0), "FORGEX", False),
    "max_print_height_mm": Fact(220.0, "FORGEX", False),
    "tools": Fact(1, "FORGEX", False),
    "kinematics": Fact("CoreXY", "FORGEX", False),
    "nozzle_sizes": Fact((0.25, 0.4, 0.6, 0.8), "FORGEX", False),
    # Sourced from the beta config (see the module docstring).
    "max_hotend_c": Fact(350.0, "FORGEX", False),
    "max_bed_c": Fact(130.0, "FORGEX", False),
}
