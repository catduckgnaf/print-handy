"""Bambu Lab A1 with the real Orca CLI: profiles generated from the installed OrcaSlicer, the production slice path,
the A1 guards on real output, the baseline proposal.

Skipped unless PRINT_HANDY_REAL_SLICER=1; CI's Slicer test jobs run tests/slicer with the pinned
OrcaSlicer v2.5.0-beta bundle (#356: the shipped slicer line). The A1 templates are generated
(profiles.install, production identities only) from that OrcaSlicer's own resources folder:
PRINT_HANDY_ORCA_RESOURCES, else the Flatpak's install location. OrcaSlicer 2.5 packs its vendor profiles
into a preset cache (BBL.opc); print_handy reads the cache directly (preset_cache), which
test_generation_from_the_installed_tree records. Locally: PRINT_HANDY_REAL_SLICER=1 python3 -m
pytest -s tests/slicer/test_orca_real_bambu_a1.py with a supported OrcaSlicer as the configured
[orca] command (PRINT_HANDY_ORCA_CMD overrides it). Printed lines record the slicer version, profile
identity, effective settings and exact toolpath bounds for the review log.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from print_handy import printers, settings
from print_handy.core import slice_guard as sg
from print_handy.printers.bambulab import facts, guard, profiles

from .test_orca_real import WORK, _orca_command, write_cube

REAL = os.environ.get("PRINT_HANDY_REAL_SLICER") == "1"
pytestmark = pytest.mark.skipif(not REAL, reason="set PRINT_HANDY_REAL_SLICER=1 to slice with the real Orca CLI")
ROOT = Path(__file__).resolve().parents[2]
PROPOSAL = ROOT / "config" / "protected-settings-baseline.bambulab.a1.proposed.json"


def installed_resources():
    """The OrcaSlicer resources folder of the slicer under test: PRINT_HANDY_ORCA_RESOURCES, else the Flatpak's."""
    if os.environ.get("PRINT_HANDY_ORCA_RESOURCES"):
        return Path(os.environ["PRINT_HANDY_ORCA_RESOURCES"])
    for scope in ("--user", "--system"):
        if not shutil.which("flatpak"):
            break
        found = subprocess.run(["flatpak", "info", scope, "--show-location", "com.orcaslicer.OrcaSlicer"],
                               capture_output=True, text=True)
        files = Path(found.stdout.strip()) / "files"
        if found.returncode == 0 and files.is_dir():
            for marker in ("BBL.json", "BBL.opc"):
                hit = next(files.glob(f"**/profiles/{marker}"), None)
                if hit:
                    return hit.parent.parent
            listing = sorted(str(p.relative_to(files)) for p in files.glob("share/*/*"))[:40]
            raise AssertionError(f"no profiles/BBL.json or BBL.opc under {files}; share/*/*: {listing}")
    raise AssertionError("no OrcaSlicer resources folder: set PRINT_HANDY_ORCA_RESOURCES or install the Flatpak")


def packed():
    return (installed_resources() / "profiles" / "BBL.opc").is_file()


def adapter_for(key, hermes=None):
    env = {"PRINT_HANDY__PRINTER__MODEL": key}
    if hermes is not None:
        env["PRINT_HANDY__PATHS__STATE_HOME"] = str(hermes)
    return printers.get(key, settings.load(local=False, environ=env))


@pytest.fixture
def a1(work):
    """The A1 adapter with templates generated from the installed OrcaSlicer (production identities only)."""
    adapter = adapter_for("bambulab.a1", work / "hermes")
    known = profiles.install(adapter, installed_resources())
    print(f"\nA1 profiles: {known['label']} from {installed_resources()}")
    return adapter


@pytest.fixture
def work():
    WORK.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as tmp:
        yield Path(tmp)


def production_flow(work, stl):
    """tests.printers.a1_flow with the real configured Orca: slice_job -> verification -> bambu_handoff -> print_now."""
    # Bambu Studio is the Bambu Lab default; this file checks the OrcaSlicer path ([printer] slicer: orca).
    env = {**os.environ, "PRINT_HANDY__PRINTER__MODEL": "bambulab.a1", "PRINT_HANDY__PRINTER__SLICER": "orca",
           "PRINT_HANDY__PATHS__STATE_HOME": str(work / "flow-hermes")}
    out = subprocess.run([sys.executable, "-m", "tests.printers.a1_flow", str(work / "flow"), "--stl", str(stl),
                          "--resources", str(installed_resources())],
                         cwd=ROOT, capture_output=True, text=True, env=env, timeout=900)
    line = next((l for l in out.stdout.splitlines() if l.startswith("A1_FLOW ")), None)
    assert line, out.stdout[-3000:] + out.stderr[-3000:]
    result = json.loads(line[len("A1_FLOW "):])
    print(f"\nA1 production flow: {json.dumps(result, indent=1)}")
    return result


def toolpath_bounds(text):
    """Exact XYZ bounds of the extruding toolpath moves, for the log."""
    motion, bounds = sg.Motion(), {a: [None, None] for a in "XYZ"}
    prelude, body, _ = sg.split_toolpath(text)
    for line in prelude.splitlines():
        motion.step(line)
    motion.forget("XYZ")
    for line in body.splitlines():
        op, words, target, delta = motion.step(line)
        if target is not None and delta is not None and delta > 0 and all(target[a] is not None for a in "XYZ"):
            for a in "XYZ":
                lo, hi = bounds[a]
                bounds[a] = [target[a] if lo is None else min(lo, target[a]), target[a] if hi is None else max(hi, target[a])]
    return bounds


def textured_plate_patterns(start):
    """Patterns for the lines the vendor start runs only for the Textured PEI plate, read from the generated profile."""
    branch = re.search(r'\{if curr_bed_type\s*==\s*"Textured PEI Plate"\}(.*?)\{(?:else|elsif|endif)', start, re.S)
    assert branch, "the installed A1 start G-code has no Textured PEI branch"
    return guard.template_patterns(branch.group(1))


def test_generation_from_the_installed_tree(work):
    """#356: generation works from whichever layout the installed OrcaSlicer ships: the JSON
    vendor tree, or (OrcaSlicer 2.5+) the packed preset cache — the recorded identity says which."""
    adapter = adapter_for("bambulab.a1", work / "hermes")
    known = profiles.install(adapter, installed_resources())
    print(f"\nA1 generated from {installed_resources()}: {known['label']}")
    assert ("packed cache" in known["label"]) == packed()


def test_motion_envelope_covers_every_literal_vendor_move(a1):
    machine = json.loads((a1.pinned_templates() / "machine.json").read_text())
    xmin, ymin, xmax, ymax = facts.A1["motion_envelope_mm"].value
    for key in ("machine_start_gcode", "machine_end_gcode", "change_filament_gcode", "time_lapse_gcode"):
        xs = [float(v) for v in re.findall(r"^\s*G[0-3]\b[^;\n]*?X(-?\d+\.?\d*)", machine[key], re.M)]
        ys = [float(v) for v in re.findall(r"^\s*G[0-3]\b[^;\n]*?Y(-?\d+\.?\d*)", machine[key], re.M)]
        assert all(xmin <= x <= xmax for x in xs) and all(ymin <= y <= ymax for y in ys), key


def test_a1_production_slice_verifies_packages_and_never_starts(work, a1):
    result = production_flow(work, write_cube(work / "cube.stl", 20))
    assert "error" not in result, result
    assert result["printer_model"] == "bambulab.a1" and result["gcode_verified"] is True
    assert result["generator"].startswith("OrcaSlicer ")
    assert result["packages"] and result["print_now"].startswith("REFUSED: Bambu Lab A1")
    assert result["job_unchanged_by_print_now"] is True
    assert all("/api/v1/spool/" in url for url in result["contacted"])
    assert result["templates"].endswith("flow-hermes/state/orca-profiles/bambulab_a1")
    text = Path(result["sliced"]).read_text(errors="replace")
    assert a1.gcode_problems(text) == []
    # The plate is explicit, and the vendor start ran its Textured PEI branch.
    start = json.loads((a1.pinned_templates() / "machine.json").read_text())["machine_start_gcode"]
    plate_lines = textured_plate_patterns(start)
    executed = [guard._code(line) for line in text.splitlines()]
    assert "; curr_bed_type = Textured PEI Plate" in text and plate_lines
    for code, pattern, _ in plate_lines:
        assert any(pattern.fullmatch(line) for line in executed), code
    bounds = toolpath_bounds(text)
    print(f"A1 extruding toolpath bounds (mm): {bounds}")
    assert 0 <= bounds["X"][0] and bounds["X"][1] <= 256 and 0 <= bounds["Y"][0] and bounds["Y"][1] <= 256
    assert 0 < bounds["Z"][0] and bounds["Z"][1] <= 20.5
    effective = {k: v for k, v in (l[2:].split(" = ", 1) for l in text.splitlines()
                                   if l.startswith("; ") and " = " in l)
                 if k in ("printer_model", "printer_settings_id", "print_settings_id", "filament_settings_id",
                          "curr_bed_type", "nozzle_diameter", "nozzle_type", "z_hop_types", "enable_arc_fitting",
                          "printable_area", "printable_height", "bed_exclude_area")}
    print(f"A1 effective settings: {json.dumps(effective, indent=1)}")
    assert effective["printer_model"] == "Bambu Lab A1" and effective["curr_bed_type"] == "Textured PEI Plate"


def test_a1_output_matches_the_proposed_baseline(work, a1):
    """The proposal (not an approval) must describe what the supported slicer actually writes."""
    result = production_flow(work, write_cube(work / "cube.stl", 20))
    assert "error" not in result, result
    gate = sg.enforce_approved_baseline(Path(result["sliced"]), path=PROPOSAL)
    assert gate.ok, gate.as_dict()


def orca_cli(templates, stl, out):
    """Plain Orca CLI with a template folder, the flags the adapter uses for a single-color STL."""
    out.mkdir()
    command = [*_orca_command(), "--load-settings", f"{templates / 'machine.json'};{templates / 'process.json'}",
               "--load-filaments", str(templates / "filament.json"), "--arrange", "1", "--orient", "0",
               "--ensure-on-bed", "--allow-newer-file", "--slice", "0", "--outputdir", str(out), str(stl)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    print(f"\n$ {' '.join(command)}\nexit {result.returncode}\n{(result.stdout + result.stderr)[-2000:]}")
    return result, out / "plate_1.gcode"


def test_a1_oversized_model_is_never_accepted(work, a1):
    bed = facts.A1["bed_mm"].value
    result, gcode = orca_cli(a1.templates_dir(), write_cube(work / "huge.stl", bed[0] + 10), work / "out")
    accepted = (result.returncode == 0 and gcode.is_file()
                and sg.post_slice(gcode, tool_slots={0: 1}, filament_slots=1, bed=bed, templates=a1.templates_dir(),
                                  envelope=a1.motion_envelope_mm).status != "blocked"
                and not a1.gcode_problems(gcode.read_text(errors="replace")))
    assert not accepted, "an oversized model produced A1 G-code the guards accepted"


def test_ad5x_output_is_refused_by_the_a1_guard(work, a1):
    ad5x = adapter_for("flashforge.ad5x")
    result, gcode = orca_cli(ad5x.templates_dir(), write_cube(work / "cube.stl", 20), work / "out")
    assert result.returncode == 0
    found = " ".join(a1.gcode_problems(gcode.read_text(errors="replace")))
    assert "Klipper/ZMod code (START_PRINT" in found and "not 'Bambu Lab A1'" in found
