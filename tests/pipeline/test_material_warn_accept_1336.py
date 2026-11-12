"""#1336: material mismatches warn and can be accepted at print start, never hard-block.

Owner ruling (2.2.9, #1190/#1213, restated 2026-11-11): no filament or material
condition hard-blocks a print. Each test drives the real entry point
(``model_job.main(["print-now", ...])``, the CLI and the chat's print-now run,
or ``print_next.start``'s argv and the chat flows) through the refusal-turned-
warning case and the accepted case.

Regression tests seen failing on base 1d452a58: on that commit the sliced vs
loaded refusal carried no accept token or prefix (``material_accept`` did not
exist) and print-now had no ``--accept-material-mismatch`` flag, so every test
here that asserts a warning or an accepted start failed there.
"""
import asyncio
import json
from pathlib import Path

import pytest

from print_handy import printers, settings
from print_handy.core import material_accept, model_job, printer_state, spoolman_client
from print_handy.hermes import print_next

PRUSA = "prusa.mk4s"


def _adapter(model=PRUSA):
    return printers.get(model, settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": model}))


def _state(material="PLA", nozzle_type="hardened_steel", **over):
    state = {"nozzle": {"diameter_mm": 0.4, "type": nozzle_type},
             "spool": {"spoolman_id": 7, "material": material},
             "temps_c": {"nozzle": 215, "bed": 60}}
    state.update(over)
    return state


def _gcode(material="PLA", hotend=215, bed=60):
    lines = [f"; filament_type = {material}", "; nozzle_diameter = 0.4",
             f"; nozzle_temperature = {hotend}", f"; nozzle_temperature_initial_layer = {hotend}",
             f"; hot_plate_temp_initial_layer = {bed}", f"M140 S{bed}", f"M104 S{hotend}", f"M190 S{bed}",
             f"M109 S{hotend}", "G90", "M83", "; CHANGE_LAYER", "G1 Z0.2", "G1 X10 Y10 E1",
             "; FEATURE: Custom", "M104 S0", "M140 S0"]
    return "\n".join(lines) + "\n"


@pytest.fixture
def prusa_start(tmp_path, monkeypatch):
    """A Prusa (offline-verified transport) with printer delivery off: a start that passes every
    check ends at the 'delivery is not enabled ... passed offline checks' refusal, nothing sent."""
    adapter = _adapter()
    state = {"value": _state()}
    monkeypatch.setattr(model_job.printers, "current", lambda config=None: adapter)
    monkeypatch.setattr(model_job.printer_state, "load", lambda *a, **k: dict(state["value"]))
    monkeypatch.setattr(spoolman_client, "get_spool", lambda sid, **k: None)
    monkeypatch.setattr(model_job, "_print_now_control_lock", lambda target: _NoLock())
    from print_handy.core import delivery
    monkeypatch.setattr(delivery, "methods", lambda *a, **k: ["download"])

    def run(text, *flags, material="PLA", nozzle_type="hardened_steel"):
        state["value"] = _state(material=material, nozzle_type=nozzle_type)
        gcode = tmp_path / "plate.gcode"
        gcode.write_text(text)
        monkeypatch.setattr("sys.argv", ["model_job", "print-now", str(gcode), "--bed-clear-confirmed", *flags])
        with pytest.raises(SystemExit) as out:
            model_job.main()
        return str(out.value.code), gcode
    return run


class _NoLock:
    def acquire(self, blocking=True):
        return True

    def release(self):
        pass


def _passed(detail):
    return "passed offline checks" in detail


# ---- site 1: sliced vs loaded material -------------------------------------------------------

def test_sliced_vs_loaded_warns_with_an_accept_path(prusa_start):
    detail, _ = prusa_start(_gcode("PETG", hotend=240, bed=80))
    assert detail.startswith("REFUSED: " + material_accept.PREFIX)
    warnings = material_accept.pending(detail)
    assert [w["key"] for w in warnings] == ["material|PETG|PLA"]
    # The question offers Reslice for the loaded material first, Print as sliced second.
    assert material_accept.choices(warnings) == ["Reslice for PLA", "Print as sliced", "Cancel"]
    assert "own temperatures" in material_accept.question(warnings)


def test_sliced_vs_loaded_accepted_prints_as_sliced_at_its_own_temperatures(prusa_start, capsys):
    detail, _ = prusa_start(_gcode("PETG", hotend=240, bed=80), "--accept-material-mismatch")
    assert _passed(detail), detail
    out = capsys.readouterr().out
    assert "MATERIAL WARNING ACCEPTED" in out and "PETG" in out
    # The accepted start re-ran the nozzle check against the loaded material and showed it.
    assert "NOZZLE CHECK: the loaded PLA suits the fitted nozzle." in out


def test_accept_by_key_bound_to_this_gcode_sha(prusa_start):
    text = _gcode("PETG", hotend=240, bed=80)
    _, gcode = prusa_start(text)
    sha = model_job.file_sha256(gcode)
    detail, _ = prusa_start(text, "--accept-material", "material|PETG|PLA", "--accept-material-sha256", sha)
    assert _passed(detail), detail


def test_an_acceptance_does_not_carry_to_a_different_gcode(prusa_start, capsys):
    """The answer was given for one G-code; another file (even the same mismatch) asks again."""
    detail, _ = prusa_start(_gcode("PETG", hotend=240, bed=80), "--accept-material", "material|PETG|PLA",
                            "--accept-material-sha256", "0" * 64)
    assert detail.startswith("REFUSED: " + material_accept.PREFIX), detail
    assert "were for another G-code" in capsys.readouterr().out


def test_an_acceptance_names_its_materials(prusa_start):
    """Accepting PETG-on-PLA does not accept ABS-on-PLA."""
    text = _gcode("ABS", hotend=250, bed=100)
    _, gcode = prusa_start(text)
    detail, _ = prusa_start(text, "--accept-material", "material|PETG|PLA",
                            "--accept-material-sha256", model_job.file_sha256(gcode))
    assert [w["key"] for w in material_accept.pending(detail)] == ["material|ABS|PLA"]


def test_heater_limits_still_refuse_when_accepted(prusa_start):
    """Mechanical heater limits are never waived: an accepted mismatch over the maximum refuses."""
    detail, _ = prusa_start(_gcode("PC", hotend=400, bed=80), "--accept-material-mismatch")
    assert detail.startswith("REFUSED:") and not _passed(detail)
    assert material_accept.PREFIX not in detail  # a real limit, not an acceptable warning


def test_a_mixed_refusal_offers_no_accept(prusa_start):
    """A material warning beside a real fault: the fault is fixed first, no Print as sliced."""
    text = _gcode("PETG", hotend=240, bed=80).replace("; nozzle_diameter = 0.4", "; nozzle_diameter = 0.6")
    detail, _ = prusa_start(text)
    assert material_accept.PREFIX not in detail and material_accept.pending(detail) == []


def test_accepted_mismatch_rechecks_the_loaded_material_against_a_brass_nozzle(prusa_start):
    """Sliced PETG, abrasive Wood PLA loaded, brass nozzle: accepting the mismatch shows the abrasive warning."""
    text = _gcode("PETG", hotend=240, bed=80)
    detail, _ = prusa_start(text, "--accept-material", "material|PETG|Wood PLA",
                            material="Wood PLA", nozzle_type="brass")
    keys = [w["key"] for w in material_accept.pending(detail)]
    assert keys == ["abrasive|Wood PLA|brass"], detail
    assert "this filament will wear out the brass nozzle" in detail
    detail, _ = prusa_start(text, "--accept-material-mismatch", material="Wood PLA", nozzle_type="brass")
    assert _passed(detail), detail


def test_own_temperatures_are_the_gcodes_not_the_loaded_spools():
    state = _state("PLA")
    temps = model_job._mismatch_own_temperatures(state, _gcode("PETG", hotend=245, bed=85), ["PETG"])
    from print_handy.core.nozzle import get_nozzle_type_adjustments
    offset = get_nozzle_type_adjustments("hardened_steel")["temp_offset_c"]
    assert temps == {"nozzle": 245 - offset, "bed": 85.0}
    assert model_job._mismatch_own_temperatures(state, _gcode("PLA"), ["PLA"]) is None


# ---- Bambu: abrasive on a soft nozzle and the not-recommended list ---------------------------

# (the Bambu abrasive and not-recommended cases live in tests/printers/test_bambulab_a1.py, beside
# the A1 profile fixture they need: test_bambu_material_warnings_can_be_accepted_1336)


# ---- site 3: Bambu AMS tray holding another material -----------------------------------------

def test_ams_tray_of_another_material_warns_naming_the_tray_then_accepts():
    from print_handy.printers.bambulab import ams_mapping
    reqs = [{"material": "PETG", "extruder": None}, {"material": "PLA", "extruder": None}]
    trays = [{"tray": 0, "material": "PLA", "extruder": None, "external": False},
             {"tray": 1, "material": "ABS", "extruder": None, "external": False}]
    with pytest.raises(ams_mapping.MappingError) as refused:
        ams_mapping.resolve(reqs, trays)
    text = "REFUSED: " + str(refused.value) + ". Nothing uploaded or started."
    warnings = material_accept.pending(text)
    assert [w["kind"] for w in warnings] == ["tray"]
    assert "tray 0 holds PLA and would be used" in warnings[0]["text"]
    with material_accept.accepting([warnings[0]["key"]]):
        assert ams_mapping.resolve(reqs[:1], trays[:1]) == [0]


# ---- site 4: Spoolman material differs from the slot record: a plain warning ------------------

def test_spoolman_material_differing_from_the_slot_record_only_warns(monkeypatch, capsys, tmp_path):
    st = {"filament_slots": {"1": {"spoolman_id": 5, "material": "PLA", "name": "Red"}},
          "spool": {"spoolman_id": 4, "material": "PLA"}, "moonraker_url": "http://printer"}
    calls = []

    def mr(state, path, data=None):
        calls.append((path, data))
        return {"result": {"spool_id": 4 if data is None and len(calls) == 1 else 5}}
    monkeypatch.setattr(model_job, "_mr", mr)
    monkeypatch.setattr(spoolman_client, "get_spool",
                        lambda sid, **k: {"archived": False, "filament": {"material": "PETG"}})
    saved = {}
    monkeypatch.setattr(printer_state, "load", lambda *a, **k: {})
    monkeypatch.setattr(printer_state, "save", lambda value, *a: saved.update(value))
    result = model_job._sync_active_spool(st, 1)
    assert result["changed"] is True
    assert "WARNING: Spoolman spool #5 is PETG, but slot 1 records PLA" in capsys.readouterr().out
    assert saved["spool"]["material"] == "PLA"  # the slot record is kept


# ---- site 6: AD5X screen label on unmapped G-code: family match plus accept --------------------

def _readiness(label, wanted):
    status = {"filament_switch_sensor head_switch_sensor": {"enabled": True, "filament_detected": True},
              "print_stats": {"state": "standby"},
              "zmod_color": {"slots": [{"ID": 1, "hasFilament": True, "Material": label, "Color": "#FF0000"}]}}
    selection = {"slot": 1, "spoolman_id": 5, "material": wanted, "color": "#FF0000"}
    return model_job.check_filament_readiness({}, status, selection, observed_at=100.0, now=100.0)


def test_screen_label_family_matches_and_a_real_difference_is_acceptable():
    assert _readiness("PLA", "PLA+") == []          # same family: no problem at all
    problems = _readiness("PETG", "PLA")
    assert len(problems) == 1 and "[accept label|1|PETG|PLA]" in problems[0]
    with material_accept.accepting(["label|1|PETG|PLA"]):
        assert _readiness("PETG", "PLA") == []


# ---- chat: print_next.start carries the answer, bound to the exact G-code ------------------------

def test_print_next_start_passes_the_answer_bound_to_the_gcode_sha(tmp_path, monkeypatch):
    gcode = tmp_path / "plate.gcode"
    gcode.write_text(_gcode())
    sha = print_next._sha256(gcode)
    seen = {}

    class Proc:
        returncode = 0

        async def communicate(self):
            return b"START REQUEST ACCEPTED\n", b""

    async def spawn(*argv, **kw):
        seen["argv"] = argv
        return Proc()
    monkeypatch.setattr(print_next.asyncio, "create_subprocess_exec", spawn)
    target = {"plan_id": "p", "part_id": "a", "copy": 1, "gcode": str(gcode), "gcode_sha256": sha,
              "accept_material": ["material|PETG|PLA"]}
    ok, _ = asyncio.run(print_next.start(target, idle_check=lambda: None, claims_dir=tmp_path / "claims"))
    assert ok
    argv = list(seen["argv"])
    assert argv[argv.index("--accept-material") + 1] == "material|PETG|PLA"
    assert argv[argv.index("--accept-material-sha256") + 1] == sha


# ---- site 5: remap to a slot holding another material ------------------------------------------

def _remap_env(tmp_path, monkeypatch):
    slots = {"1": {"spoolman_id": 10, "material": "PLA", "color": "white", "physical_color_verified": True},
             "2": {"spoolman_id": 11, "material": "PETG", "color": "black", "physical_color_verified": True}}
    state = {"filament_slots": slots, "spool": {"spoolman_id": 10, "material": "PLA"},
             "nozzle": {"diameter_mm": 0.4, "type": "hardened_steel"}}
    monkeypatch.setattr(model_job.printer_state, "load", lambda *a, **k: state)
    gcode = tmp_path / "plate.gcode"
    gcode.write_text("; filament_type = PLA\nT0\nG1 X1 Y1 E1\n")
    job = {"gcode": str(gcode), "filament_selection": {"slot": 1, "spoolman_id": 10, "material": "PLA",
                                                      "color": "white"}}
    (tmp_path / "JOB.json").write_text(json.dumps(job))
    resliced = []
    monkeypatch.setattr(model_job, "slice_job", lambda path: resliced.append(
        json.loads(Path(path).read_text())["filament_selection"]) or gcode)
    return tmp_path / "JOB.json", resliced


def _remap_cli(monkeypatch, job_json, *flags):
    monkeypatch.setattr("sys.argv", ["model_job", "remap-slot", str(job_json), "--slot", "2", *flags])
    try:
        model_job.main()
    except SystemExit as exc:
        return str(exc.code)
    return "OK"


def test_remap_to_another_material_warns_then_accepting_reslices_for_that_slot(tmp_path, monkeypatch):
    job_json, resliced = _remap_env(tmp_path, monkeypatch)
    out = _remap_cli(monkeypatch, job_json)
    assert material_accept.PREFIX in out and "[accept remap|0|2|PLA|PETG]" in out, out
    assert resliced == []  # nothing re-finalized or resliced without the answer
    assert _remap_cli(monkeypatch, job_json, "--accept-material-mismatch") == "OK"
    assert [s["slot"] for s in resliced] == [2] and resliced[0]["material"] == "PETG"
