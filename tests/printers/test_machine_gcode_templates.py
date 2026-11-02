"""Shipped machine templates must agree with the slicer that consumes them.

Marlin templates (#763): real Orca refuses every shipped marlin/marlin2 template
whose layer_change_gcode omits G92 E0 while use_relative_e_distances is on, so
every such template must carry the per-layer extruder reset.
"""
import json

import pytest

from print_handy import settings

TEMPLATES = settings.ROOT / "print_handy" / "templates" / "orca"


def machine(folder):
    return json.loads((TEMPLATES / folder / "machine.json").read_text())


def marlin_folders():
    folders = []
    for path in sorted(TEMPLATES.iterdir()):
        if not (path / "machine.json").is_file():
            continue
        template = machine(path.name)
        if template.get("gcode_flavor") in ("marlin", "marlin2"):
            folders.append(path.name)
    return folders


@pytest.mark.parametrize("folder", marlin_folders())
def test_marlin_template_layer_change_resets_relative_extruder(folder):
    # Bonus (not in the audit): real Orca 2.5.0-alpha refuses every shipped Marlin
    # template whose layer change omits G92 E0 while use_relative_e_distances is on
    # ("Relative extruder addressing requires resetting the extruder position at each
    # layer... Add G92 E0 to layer_gcode", return -51). Klipper-flavor templates are
    # exempt: Orca does not demand the reset for them (verified by real slice).
    template = machine(folder)
    assert str(template.get("use_relative_e_distances")) == "1", folder
    assert "G92 E0" in template["layer_change_gcode"].splitlines(), folder
