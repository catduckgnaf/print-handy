"""The shipped Flashforge protected-settings baselines (#868) against what the real Orca CLI writes.

config/protected-settings-baseline.{flashforge.ad5m,ad5m_pro,creator5,creator5_pro,flashforge_stock.ad5x}.json were
approved from each model's slice header rebuilt offline (tests/printers/test_model_baselines.py). Here each model's
templates go through the real Orca with the start/end G-code slice_model hands it (the printer adapter's own
machine_start_gcode/machine_end_gcode), and the output must pass that model's shipped baseline.

Skipped unless PRINT_HANDY_REAL_SLICER=1 (CI's Slicer test jobs).
"""
import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from print_handy import printers, settings
from print_handy.core import slice_guard as sg

from .test_orca_real import WORK, _orca_command, write_cube

REAL = os.environ.get("PRINT_HANDY_REAL_SLICER") == "1"
pytestmark = pytest.mark.skipif(not REAL, reason="set PRINT_HANDY_REAL_SLICER=1 to slice with the real Orca CLI")
SHIPPED = ("flashforge.ad5m", "flashforge.ad5m_pro", "flashforge.creator5", "flashforge.creator5_pro",
           "flashforge_stock.ad5x")


@pytest.mark.parametrize("key", SHIPPED)
def test_real_output_passes_the_shipped_baseline(key):
    adapter = printers.get(key, settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": key}))
    templates = adapter.templates_dir()
    WORK.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as tmp:
        tmp = Path(tmp)
        machine = json.loads((templates / "machine.json").read_text())
        machine["machine_start_gcode"] = adapter.machine_start_gcode(machine["machine_start_gcode"], "MESH_DATA")
        machine["machine_end_gcode"] = adapter.machine_end_gcode(machine.get("machine_end_gcode", ""))
        (tmp / "machine.json").write_text(json.dumps(machine, indent=2))
        out = tmp / "out"
        out.mkdir()
        command = [*_orca_command(), "--load-settings", f"{tmp / 'machine.json'};{templates / 'process.json'}",
                   "--load-filaments", str(templates / "filament.json"), "--arrange", "1", "--orient", "0",
                   "--ensure-on-bed", "--allow-newer-file", "--slice", "0", "--outputdir", str(out),
                   str(write_cube(tmp / "cube.stl", 20))]
        result = subprocess.run(command, capture_output=True, text=True, timeout=600)
        print(f"\n$ {' '.join(command)}\nexit {result.returncode}\n{(result.stdout + result.stderr)[-2000:]}")
        assert result.returncode == 0
        gate = sg.enforce_approved_baseline(out / "plate_1.gcode", path=adapter.baseline_path)
        assert gate.ok, gate.as_dict()
