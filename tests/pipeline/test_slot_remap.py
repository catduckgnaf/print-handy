"""Tests for slot remapping without physical slot changes (Part B)."""
import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from print_handy.core import model_job as job
from print_handy.printers.flashforge.zmod import multicolor_contract as contract


def _state():
    return {
        "moonraker_url": "http://192.168.45.50:7125",
        "display_off": 0,
        "nozzle": {"diameter_mm": 0.4},
        "spool": {"spoolman_id": 10, "material": "PLA", "name": "White PLA"},
        "filament_slots": {
            "1": {"spoolman_id": 10, "material": "PLA", "color": "White PLA", "hex": "#FFFFFF", "physical_color_verified": True},
            "2": {"spoolman_id": 11, "material": "PLA", "color": "Pink PLA", "hex": "#F95D73", "physical_color_verified": True},
            "3": {"spoolman_id": 8, "material": "PLA", "color": "Black PLA", "hex": "#161616", "physical_color_verified": True},
            "4": {"spoolman_id": 12, "material": "PLA", "color": "Dark Green PLA", "hex": "#067749", "physical_color_verified": True},
        },
    }


def _artifact():
    return (
        "; HEADER_BLOCK_END\n"
        "M190 S60\n"
        "M104 S210 T0\n"
        "START_PRINT BED_TEMP=60 EXTRUDER_TEMP=210 MESH=MESH_DATA\n"
        "G90\nM83\n"
        "T0\n"
        "G1 X10 Y10 Z0.24 E1\n"
        ";LAYER_CHANGE\n;WIDTH:0.5\n;LAYER_CHANGE\n;WIDTH:0.44\nEND_PRINT\n"
        "; nozzle_diameter = 0.4\n; first_layer_height = 0.24\n; filament_type = PLA\n"
    )


def test_remap_slot_on_gcode(tmp_path):
    st = _state()
    gcode = tmp_path / "plate_1.gcode"
    gcode.write_text(_artifact())

    # Initially finalize for slot 1
    contract.finalize(gcode, None, st, {"0": 1})
    assert contract.embedded_mapping(gcode.read_text(), st) == {"0": 1}

    # Remap to slot 2
    res = job.remap_slot(gcode, 2, tool="0", state=st)
    assert res["ok"] is True
    assert res["slot"] == 2
    assert res["mapping"] == {"0": 2}

    # Verify G-code embedded metadata and checksum
    embedded = contract.embedded_mapping(gcode.read_text(), st)
    assert embedded == {"0": 2}
    assert "; zmod_color_data = 0|#F95D73|PLA" in gcode.read_text()


def test_remap_slot_on_job_json(tmp_path):
    st = _state()
    gcode = tmp_path / "plate_1.gcode"
    gcode.write_text(_artifact())
    contract.finalize(gcode, None, st, {"0": 1})

    job_data = {
        "model_id": "test-model-1",
        "gcode": str(gcode),
        "filament_selection": {
            "slot": 1,
            "spoolman_id": 10,
            "color": "White PLA",
            "material": "PLA",
        },
        "artifact_binding": {
            "source_sha256": "fake_source",
            "decisions_sha256": "fake_decisions",
            "gcode_sha256": job.file_sha256(gcode),
        },
    }
    job_file = tmp_path / "JOB.json"
    job_file.write_text(json.dumps(job_data, indent=2))

    # Remap job to slot 4 (Dark Green PLA)
    res = job.remap_slot(job_file, 4, state=st)
    assert res["ok"] is True
    assert res["slot"] == 4

    # Verify job file updated
    updated_job = json.loads(job_file.read_text())
    assert updated_job["filament_selection"]["slot"] == 4
    assert updated_job["filament_selection"]["color"] == "Dark Green PLA"
    assert updated_job["filament_selection"]["spoolman_id"] == 12
    assert updated_job["gcode_sha256"] == res["gcode_sha256"]

    # Verify G-code updated
    assert contract.embedded_mapping(gcode.read_text(), st) == {"0": 4}
    assert "; zmod_color_data = 0|#067749|PLA" in gcode.read_text()


def test_print_now_with_remap_slot_arg(tmp_path):
    st = _state()
    gcode = tmp_path / "plate_1.gcode"
    gcode.write_text(_artifact())
    contract.finalize(gcode, None, st, {"0": 1})

    job_data = {
        "model_id": "test-model-2",
        "gcode": str(gcode),
        "filament_selection": {
            "slot": 1,
            "spoolman_id": 10,
            "color": "White PLA",
            "material": "PLA",
        },
    }
    job_file = tmp_path / "JOB.json"
    job_file.write_text(json.dumps(job_data, indent=2))

    mock_print_mapped = MagicMock(return_value={"accepted": True, "started": True})

    with patch.object(job.printer_state, "load", return_value=st), \
         patch.object(job, "print_mapped_native", mock_print_mapped), \
         patch.object(job, "verify_gcode"), \
         patch.object(job, "_printer_phase", return_value=("standby", "x.gcode", "Ready")), \
         patch.object(job, "mesh_readiness", return_value={"display_off": 0, "profile": "MESH_DATA", "problem": None}):

        job.print_now(job_file, remap_slot_num=3)

        assert mock_print_mapped.called
        # Mapping passed to print_mapped_native must be {"0": 3}
        called_mapping = mock_print_mapped.call_args[0][3]
        assert called_mapping == {"0": 3}


def test_cli_remap_slot_subcommand(tmp_path):
    st = _state()
    gcode = tmp_path / "test.gcode"
    gcode.write_text(_artifact())
    contract.finalize(gcode, None, st, {"0": 1})

    test_config = tmp_path / "config.toml"
    test_config.write_text(
        "[paths]\nhermes_home = " + json.dumps(str(tmp_path / "hermes")) + "\n"
    )
    state_path = tmp_path / "hermes" / "state" / "printer_hardware.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps(st))

    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "print_handy.core.model_job",
            "remap-slot",
            str(gcode),
            "--slot",
            "2",
        ],
        cwd=str(Path(__file__).resolve().parents[2]),
        capture_output=True,
        text=True,
        env={
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "PRINT_HANDY_CONFIG": str(test_config),
            # the conftest pin for fixtures with literal temperatures (see tests/conftest.py)
            **{k: v for k, v in os.environ.items() if k.startswith("PRINT_HANDY__NOZZLE_TEMP_OFFSETS__")},
        },
    )
    assert run.returncode == 0
    assert "SLOT REMAPPED" in run.stdout
    assert '"slot": 2' in run.stdout
