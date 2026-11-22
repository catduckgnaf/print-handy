"""The configured build plate reaches real Orca output (#1788): curr_bed_type and the bed heater follow it.

Job #123 selected PETG on a smooth PEI plate, but the G-code said ``curr_bed_type = Cool Plate``. Skipped
unless PRINT_HANDY_REAL_SLICER=1 (CI's Slicer test jobs). The offline half is tests/core/test_build_plate_orca_1788.py.
"""
import json
import os
import re
import tempfile
from pathlib import Path

import pytest

from print_handy import printers, settings
from print_handy.core import orca_adapter

REAL = os.environ.get("PRINT_HANDY_REAL_SLICER") == "1"
WORK = settings.ROOT / ".slicer-work"
STATE = Path(__file__).resolve().parents[1] / "pipeline" / "fixtures" / "printer_hardware.json"
pytestmark = pytest.mark.skipif(not REAL, reason="set PRINT_HANDY_REAL_SLICER=1 to slice with the real Orca CLI")

from .test_orca_edits_real import write_cube  # noqa: E402

KEY = printers.current().key  # the model the shared hardware fixture describes


def _slice(tmp, plate):
    env = {}
    if plate:
        env["PRINT_HANDY__PRINTER__BUILD_PLATE"] = plate
    adapter = printers.get(KEY, settings.load(environ=env))
    state = json.loads(STATE.read_text())
    out, _prov, _cfg = orca_adapter.slice_model(write_cube(Path(tmp) / "c.stl"), Path(tmp) / "slice", state,
                                                adapter=adapter)
    return Path(out).read_text(errors="replace")


def _bed_type(text):
    return re.search(r"^; curr_bed_type = (.*)$", text, re.M).group(1)


def _bed_heats(text):
    return {int(v) for v in re.findall(r"^M1[49]0 S(\d+)", text, re.M)} - {0}


@pytest.mark.parametrize("plate,bed_type", [
    (None, "Cool Plate"),
    ("smooth_pei", "High Temp Plate"),
    ("textured_pei", "Textured PEI Plate"),
    ("smooth_pc", "Engineering Plate"),
    ("cool_plate", "Cool Plate"),
])
def test_plate_names_the_bed_type_and_the_bed_heater_keeps_its_temperature(plate, bed_type):
    WORK.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as tmp:
        text = _slice(tmp, plate)
        assert _bed_type(text) == bed_type
        heats = _bed_heats(text)
        # Print Handy writes the job's bed temperature to every plate's key, so each plate heats to it.
        assert len(heats) == 1 and next(iter(heats)) > 0
        print(plate, bed_type, heats)
