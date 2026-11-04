"""Height-based color changes with the real Orca CLI (#870).

A ChromaCanvas-style tower (one part, color changes by layer height in
Metadata/custom_gcode_per_layer.xml) slices through slice_model:

- on the AD5X (IFS slots) and the Creator 5 (toolheads) the G-code must
  change tool at the recorded heights, to the tools the picks mapped to;
- on the AD5M (one extruder) with the pause choice recorded, the G-code
  must pause at those heights instead and never change tool.

Skipped unless PRINT_HANDY_REAL_SLICER=1 (CI's Slicer test jobs).
"""
import os
import re
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from print_handy import printers, settings
from print_handy.core import orca_adapter, source_colors, spoolman_client
from tests.pipeline.test_forgebridge_edge_cases import layered
from tests.pipeline.test_multi_tool_intake import creator_config, tool_state

from .test_orca_real import WORK

REAL = os.environ.get("PRINT_HANDY_REAL_SLICER") == "1"
pytestmark = pytest.mark.skipif(not REAL, reason="set PRINT_HANDY_REAL_SLICER=1 to slice with the real Orca CLI")

AD5X_STATE = {
    "moonraker_url": "http://127.0.0.1:7125",
    "spoolman_url": "http://127.0.0.1:7912",
    "nozzle": {"diameter_mm": 0.4, "type": "hardened_steel"},
    "spool": {"spoolman_id": 10, "material": "PLA", "density": 1.24},
    "temps_c": {"nozzle": 220, "bed": 60},
    "calibration_baseline": {"native_mesh": "auto"},
    "filament_slots": {
        "1": {"slot": 1, "spoolman_id": 10, "color": "white", "material": "PLA",
              "physical_color_verified": True, "hex": "FFFFFF"},
        "2": {"slot": 2, "spoolman_id": 11, "color": "black", "material": "PLA",
              "physical_color_verified": True, "hex": "101010"},
        "3": {"slot": 3, "spoolman_id": 12, "color": "red", "material": "PLA",
              "physical_color_verified": True, "hex": "FF0000"}},
}


def _decisions(source, picks):
    manifest = source_colors.inventory(source)
    state_rows = {1: ("white", 10), 2: ("black", 11), 3: ("red", 12)}
    choices = {}
    for row, slot in zip(source_colors.canonical_prompts(manifest), picks):
        color, spool = state_rows[slot]
        choices[row["key"]] = {"slot": slot, "color": color, "material": "PLA", "spoolman_id": spool}
    return {"schema": "part_slots_v1", "mode": "per_part",
            "source_sha256": manifest["source_sha256"], "choices": choices}


def _tool_at_heights(text):
    """{tool: Z of the layer where that tool is first selected} from Orca G-code.

    Orca marks each layer with ';Z:<height>' before printing it; a tool
    command belongs to the most recent marker above it.
    """
    first = {}
    z = None
    for line in text.splitlines():
        if line.startswith(";Z:"):
            try:
                z = float(line[3:])
            except ValueError:
                pass
        match = re.match(r"\s*T(\d+)\s*(?:;.*)?$", line)
        if match and z is not None:
            first.setdefault(int(match.group(1)), z)
    return first


def _slice(tmp, source, adapter, state, decisions):
    with mock.patch.object(spoolman_client, "get_spool", return_value=None):
        out, _provenance, _configs = orca_adapter.slice_model(
            source, tmp / "slice", state, adapter=adapter, color_decisions=decisions)
    return Path(out).read_text(errors="replace")


def test_ad5x_height_colors_change_tool_at_their_heights():
    WORK.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as tmp:
        tmp = Path(tmp)
        source = layered(tmp / "chroma.3mf")
        adapter = printers.get("flashforge.ad5x",
                               settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": "flashforge.ad5x"}))
        # Tower -> slot 1, the 4 mm color -> slot 2, the 8 mm color -> slot 3:
        # tools T0, T1, T2 in pick order.
        text = _slice(tmp, source, adapter, dict(AD5X_STATE), _decisions(source, [1, 2, 3]))
        first = _tool_at_heights(text)
        print(f"\nAD5X first tool selections by layer Z: {first}")
        assert 3.6 <= first[1] <= 4.6, first
        assert 7.6 <= first[2] <= 8.6, first


def test_creator_height_colors_change_tool_at_their_heights():
    WORK.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as tmp:
        tmp = Path(tmp)
        source = layered(tmp / "chroma.3mf")
        adapter = printers.get("flashforge.creator5", creator_config("flashforge.creator5"))
        # Toolheads are identity tools: picks 1, 3, 4 -> T0, T2, T3.
        manifest = source_colors.inventory(source)
        rows = source_colors.canonical_prompts(manifest)
        state = tool_state()
        choices = {}
        for row, slot in zip(rows, [1, 3, 4]):
            record = state["filament_slots"][str(slot)]
            choices[row["key"]] = {"slot": slot, "color": record["color"], "material": "PLA",
                                   "spoolman_id": record["spoolman_id"]}
        decisions = {"schema": "part_slots_v1", "mode": "per_part",
                     "source_sha256": manifest["source_sha256"], "choices": choices}
        text = _slice(tmp, source, adapter, state, decisions)
        first = _tool_at_heights(text)
        print(f"\nCreator 5 first tool selections by layer Z: {first}")
        assert 3.6 <= first[2] <= 4.6, first   # the 4 mm color went to toolhead 3 (T2)
        assert 7.6 <= first[3] <= 8.6, first   # the 8 mm color went to toolhead 4 (T3)


def test_ad5m_pause_choice_pauses_at_the_heights_and_never_changes_tool():
    WORK.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as tmp:
        tmp = Path(tmp)
        source = layered(tmp / "chroma.3mf")
        adapter = printers.get("flashforge.ad5m",
                               settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": "flashforge.ad5m"}))
        manifest = source_colors.inventory(source)
        decisions = {"schema": "part_slots_v1", "mode": "single_color",
                     "source_sha256": manifest["source_sha256"], "height_colors": "pause",
                     "selection": {"slot": 1, "color": "white", "material": "PLA", "spoolman_id": 10}}
        state = {"nozzle": {"diameter_mm": 0.4, "type": "hardened_steel"},
                 "temps_c": {"nozzle": 220, "bed": 60},
                 "spool": {"material": "PLA", "spoolman_id": 10, "name": "white", "color": "white",
                           "density": 1.24, "diameter_mm": 1.75},
                 "filament_slots": {"1": {"color": "white", "material": "PLA", "spoolman_id": 10,
                                          "hex": "FFFFFF", "physical_color_verified": True}}}
        text = _slice(tmp, source, adapter, state, decisions)
        pauses = []
        z = None
        for line in text.splitlines():
            if line.startswith(";Z:"):
                try:
                    z = float(line[3:])
                except ValueError:
                    pass
            if line.strip() == "PAUSE" and z is not None:
                pauses.append(z)
        print(f"\nAD5M pauses by layer Z: {pauses}")
        assert any(3.6 <= p <= 4.6 for p in pauses), pauses
        assert any(7.6 <= p <= 8.6 for p in pauses), pauses
        assert not re.search(r"^\s*T[1-9]\s*$", text, re.M), "a one-extruder pause print changed tool"
