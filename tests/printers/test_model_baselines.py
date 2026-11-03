"""Per-model protected-settings baselines (SAFE-10: P1-48, P2-174).

Every model except the AD5X (flashforge.ad5x, whose approved output the shared
config/protected-settings-baseline.json records) must declare its own baseline
file. Without one, orca_adapter enforces the AD5X's approved start/end G-code
against the model's output and blocks ordinary slices, and re-approving the
shared file for one model silently changes the AD5X's approval.

Most baseline files are not shipped: approval is a separate, deliberate
operator step from a real slice of that model
(python3 -m print_handy.core.approve_baseline approve-baseline <artifact>
--baseline config/<file>). Until a model's file exists the guard only warns,
as it already does for the other per-model baselines (e.g. bambulab.a1).

The other five Flashforge profiles ship theirs (#868). Each was approved with
that same command from the model's slice header rebuilt offline
(model_artifact(key, adapter=True)), the method that reproduces the AD5X's
real-slice approval byte for byte (test_offline_artifact_reproduces_the_real_ad5x_approval).
tests/slicer/test_flashforge_baselines_real.py checks each against real Orca output.
"""
import contextlib
import io
import json
from pathlib import Path

import pytest

from print_handy import printers, settings
from print_handy.core import slice_guard as sg

ROOT = Path(__file__).resolve().parents[2]
SHARED_OWNER = "flashforge.ad5x"
AD5X_ARTIFACT = ROOT / "tests/fixtures/printers/ad5x_zmod/silk_pla_slot2_sliced.gcode.txt"
# The protected keys a real Orca artifact header carries (see the AD5X fixture).
ARTIFACT_KEYS = ("machine_start_gcode", "machine_end_gcode", "post_process",
                 "host_type", "printer_technology")
# Models whose baseline ships, approved from model_artifact(key, adapter=True) (#868).
SHIPPED = ("flashforge.ad5m", "flashforge.ad5m_pro", "flashforge.creator5", "flashforge.creator5_pro",
           "flashforge_stock.ad5x")


def config_for(key):
    return settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": key})


def model_artifact(key, tmp_path, adapter=False):
    """A >1000-byte G-code artifact shaped like a slice of `key`: the model's own
    template protected values in Orca header form (newlines escaped, as Orca
    writes them; post_process is the Orca default the templates leave unset).

    adapter=True writes the start/end G-code as slice_model hands it to Orca (the
    printer adapter's machine_start_gcode/machine_end_gcode around the template
    text): what a real slice's header carries. A host_type the template does not set
    is left out, as the real Orca header leaves it out (AD5M, AD5M Pro)."""
    model = printers.MODELS[key]
    machine = json.loads((ROOT / "print_handy/templates/orca" / model.templates
                          / "machine.json").read_text())
    if adapter:
        a = printers.get(key, config_for(key))
        machine = {**machine,
                   "machine_start_gcode": a.machine_start_gcode(machine["machine_start_gcode"], "MESH_DATA"),
                   "machine_end_gcode": a.machine_end_gcode(machine.get("machine_end_gcode", ""))}
    lines = []
    for k in ARTIFACT_KEYS:
        if adapter and k == "host_type" and k not in machine:
            continue
        v = machine.get(k, "")
        if isinstance(v, str):
            v = v.replace("\n", "\\n")
        lines.append(f"; {k} = {v}")
    path = tmp_path / f"{key}.gcode"
    path.write_text("\n".join(lines) + "\n" + "; filler comment line\n" * 80
                    + "G28\nG1 X10 Y10 E1\n")
    assert path.stat().st_size > 1000  # the guard only enforces above 1000 bytes
    return path


def test_every_model_but_the_ad5x_declares_its_own_baseline():
    for key, model in printers.MODELS.items():
        if key == SHARED_OWNER:
            assert model.baseline is None, key  # the shared file is the AD5X's own approval
            continue
        assert model.baseline == f"protected-settings-baseline.{key}.json", key
        path = settings.ROOT / "config" / model.baseline
        assert path != sg.BASELINE_PATH, key
        # Approval is a separate operator step, not shipped, except the Flashforge profiles (#868).
        assert path.exists() == (key in SHIPPED), key


def test_offline_artifact_reproduces_the_real_ad5x_approval(tmp_path):
    """The method the shipped Flashforge baselines were approved with gives the AD5X's own
    real-slice approval (config/protected-settings-baseline.json) byte for byte."""
    artifact = model_artifact(SHARED_OWNER, tmp_path, adapter=True)
    with contextlib.redirect_stdout(io.StringIO()):
        approved = sg.approve_baseline(artifact, path=tmp_path / "ad5x.json")
    assert approved == json.loads(sg.BASELINE_PATH.read_text())


@pytest.mark.parametrize("key", SHIPPED)
def test_shipped_flashforge_baseline_is_the_template_output(key, tmp_path):
    """Each shipped file is what approve-baseline writes from that model's slice header, so a
    template change to the start/end G-code fails here until it is re-approved."""
    artifact = model_artifact(key, tmp_path, adapter=True)
    shipped = settings.ROOT / "config" / printers.MODELS[key].baseline
    with contextlib.redirect_stdout(io.StringIO()):
        approved = sg.approve_baseline(artifact, path=tmp_path / "approved.json")
    assert approved == json.loads(shipped.read_text())
    assert sg.enforce_approved_baseline(artifact, path=shipped).ok


@pytest.mark.parametrize("key", SHIPPED)
def test_shipped_flashforge_baseline_blocks_a_foreign_start_gcode(key, tmp_path):
    """These printers now block a changed start G-code instead of only warning (#868)."""
    artifact = model_artifact(key, tmp_path, adapter=True)
    artifact.write_text(artifact.read_text().replace(
        "; machine_start_gcode = ", "; machine_start_gcode = M1002 judge_flag g29_before_print_flag\\n", 1))
    shipped = settings.ROOT / "config" / printers.MODELS[key].baseline
    gate = sg.enforce_approved_baseline(artifact, path=shipped)
    assert gate.status == "blocked", gate.as_dict()
    assert any("machine_start_gcode" in b for b in gate.blocked), gate.blocked


def test_shared_baseline_still_approves_the_ad5x_output():
    assert sg.enforce_approved_baseline(AD5X_ARTIFACT).ok


@pytest.mark.parametrize("key", ["octoprint.ender3", "octoprint.marlin", "sovol.sv08",
                                 "sovol_marlin.sv01", "snapmaker.u1", "flashforge.ad5m",
                                 "flashforge_stock.ad5x"])
def test_adapter_resolves_its_own_baseline_path(key):
    a = printers.get(key, config_for(key))
    assert a.baseline_path is not None, key
    assert a.baseline_path.name == f"protected-settings-baseline.{key}.json", key
    assert a.baseline_path != sg.BASELINE_PATH, key


@pytest.mark.parametrize("key", ["octoprint.marlin", "octoprint.ender3",
                                 "octoprint.ender3v2", "octoprint.ender3v3se"])
def test_model_output_is_blocked_by_the_shared_baseline_but_not_its_own(key, tmp_path):
    artifact = model_artifact(key, tmp_path)
    shared_before = sg.BASELINE_PATH.read_bytes()
    blocked = sg.enforce_approved_baseline(artifact)  # the shared AD5X baseline
    assert blocked.status == "blocked", key
    assert any("machine_start_gcode" in b or "machine_end_gcode" in b
               for b in blocked.blocked), key
    own = tmp_path / printers.MODELS[key].baseline
    with contextlib.redirect_stdout(io.StringIO()):  # approve_baseline prints the diff
        sg.approve_baseline(artifact, path=own)
    assert sg.enforce_approved_baseline(artifact, path=own).ok, key
    # Approving this model never touches the shared file or the AD5X's approval.
    assert sg.BASELINE_PATH.read_bytes() == shared_before
    assert sg.enforce_approved_baseline(AD5X_ARTIFACT).ok


def test_unapproved_model_baseline_warns_instead_of_blocking(tmp_path):
    key = "octoprint.ender3"
    artifact = model_artifact(key, tmp_path)
    missing = tmp_path / printers.MODELS[key].baseline
    r = sg.enforce_approved_baseline(artifact, path=missing)
    assert r.status == "warnings" and not r.blocked
    assert f"--baseline config/{missing.name}" in r.warnings[0]
