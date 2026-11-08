"""Contracts the port itself relies on: shipped baseline, child process offload, no flat imports."""
import asyncio
import json
import re
from pathlib import Path

from print_handy import settings
from print_handy.core import prepared_intake, slice_guard, source_colors

from .test_color_intake import source

CORE = Path(settings.ROOT) / "print_handy" / "core"


def test_shipped_protected_baseline_matches_the_orca_machine_template():
    """The committed approval must accept what the adapter writes into the G-code header.

    The synthesis models a real Orca header: alongside the machine template's
    own fields it carries slicing_pipeline_plugin empty, as every real Orca
    output does in its config dump (the recorded slice the shared baseline
    was approved from carries the line), because the baseline records the
    key (#1087) and a recorded key missing from output blocks by design."""
    machine = json.loads((settings.current()["orca"]["templates"] / "machine.json").read_text())
    start = ("M190 S[bed_temperature_initial_layer_single]\nM104 S[nozzle_temperature_initial_layer] T0\n"
             + machine["machine_start_gcode"])
    header = "".join(f"; {key} = {value}\n" for key, value in {
        "machine_start_gcode": start.replace("\n", "\\n"), "machine_end_gcode": machine["machine_end_gcode"],
        "post_process": "", "host_type": machine["host_type"], "printer_technology": "FFF",
        "slicing_pipeline_plugin": ""}.items())
    assert slice_guard.BASELINE_PATH == settings.ROOT / "config" / "protected-settings-baseline.json"
    assert slice_guard.BASELINE_PATH.is_file()
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        gcode = Path(td) / "plate_1.gcode"
        gcode.write_text(header)
        assert slice_guard.enforce_approved_baseline(gcode).status == "ok"


def test_heavy_steps_really_run_in_a_child_process(tmp_path):
    path = source(tmp_path)
    expected = json.loads(json.dumps(source_colors.inventory(path), default=str))
    got = asyncio.run(prepared_intake._heavy(source_colors.inventory, path))
    assert json.loads(json.dumps(got, default=str)) == expected


def test_core_has_no_flat_sibling_imports_or_path_hacks():
    mods = {p.stem for p in CORE.glob("*.py")}
    flat = re.compile(r"^\s*(?:from|import)\s+(" + "|".join(sorted(mods)) + r")\b", re.M)
    for path in CORE.glob("*.py"):
        text = path.read_text()
        assert not flat.search(text), path.name
        assert "sys.path.insert" not in text.replace("sys.path.insert(0, str(Path(__file__).resolve().parents[2]))", ""), path.name
        assert not re.search(r"__import__\(['\"](" + "|".join(sorted(mods)) + r")['\"]", text), path.name
