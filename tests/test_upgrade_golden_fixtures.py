"""Upgrade-path golden fixtures: today's readers over frozen old shapes.

The fixtures under ``tests/fixtures/legacy/`` freeze state as older
releases wrote it — a pre-#1029 JOB.json (no ``started_by`` stamp), a
pre-#672 hardware record (``ifs_slots``), a pre-#979/#988 print record
(no ``record_id``), a pre-#392 Markdown record note, and a settings file
carrying every retired key at once. Provenance for each fixture —
recorded vs inferred — lives in that directory's README.md; nothing
there is presented as a recorded shape unless the README says so.

Readers exercised, at the seams the existing suites use:
print-again planning (``print_control.printed_jobs`` /
``_target_from_record`` / ``again_question``, as in
``tests/hermes/test_print_control.py``), the shared terminal renderer
(``discord_print_summary.format_event``), Wrapped aggregates
(``print_stats.load_records`` / ``aggregate``, as in
``tests/test_wrapped_records_from_writers.py``), settings loading with
the retired keys, the per-printer state loader, and the doctor's
hardware / settings checks over an old install layout.
"""
import json
import shutil
from pathlib import Path

import pytest

from print_handy import doctor, settings
from print_handy.core import printer_state, print_stats
from print_handy.runtime import discord_print_summary as summary
from print_handy.runtime import print_control as pc

LEGACY = Path(__file__).parent / "fixtures" / "legacy"


def _fixture(name):
    return json.loads((LEGACY / name).read_text())


def _materialize_job(tmp_path):
    """The frozen pre-#1029 job as an archive entry with real G-code files.

    The fixture stores bare file names; the materialized archive gives
    plate_1 a launch record (the shape ``printed_jobs`` scans for), so
    the old job participates in print-again planning exactly as an
    archived job from that era does on an upgraded install.
    """
    archive = tmp_path / "archive"
    run = archive / "legacy-widget--abc" / "sliced" / "orca" / "run1"
    run.mkdir(parents=True)
    for name in ("plate_1.gcode", "plate_2.gcode"):
        (run / name).write_text(
            "; estimated printing time (normal mode) = 1h 0m\n"
            "; total filament used [g] = 12\nG1 X1\n")
    job = _fixture("job_pre_starter_stamp.json")
    job["gcode"] = str(run / "plate_1.gcode")
    job["derived_plates"][0]["gcode"] = str(run / "plate_1.gcode")
    job["derived_plates"][1]["gcode"] = str(run / "plate_2.gcode")
    job_path = run.parents[2] / "JOB.json"
    job_path.write_text(json.dumps(job))
    (run / "plate_1.launch.json").write_text(json.dumps(
        {"file": "plate_1.gcode", "artifact": str(run / "plate_1.gcode")}))
    return archive, job_path


# ------------------------------------------------------------- the old JOB.json

def test_the_frozen_job_really_has_no_starter_stamp_or_printer_id():
    job = _fixture("job_pre_starter_stamp.json")
    assert "started_by" not in job and "printer_id" not in job
    assert summary.starter_user_id(job) == ""


def test_print_again_planning_resolves_the_old_job(tmp_path):
    archive, job_path = _materialize_job(tmp_path)
    rows = pc.printed_jobs(archive, numbers_path=tmp_path / "numbers.json")
    assert [row["title"] for row in rows] == ["Legacy Widget"]
    assert rows[0]["index"] == 1 and rows[0]["job_json"] == job_path
    target = pc._target_from_record(rows[0]["record"], archive)
    assert target["title"] == "Legacy Widget"
    assert target["gcode"] == str((archive / "legacy-widget--abc" / "sliced"
                                    / "orca" / "run1" / "plate_1.gcode").resolve())
    question = pc.again_question(target)
    assert "Legacy Widget" in question and "Confirm the bed is clear" in question


def test_terminal_render_of_the_old_job_mentions_nobody(tmp_path, monkeypatch):
    archive, _job_path = _materialize_job(tmp_path)
    monkeypatch.setattr(summary, "ARCHIVE", archive)
    monkeypatch.setattr(summary, "CONTEXT_PATH", tmp_path / "no-context.json")
    for kind, verb in (("complete", "Print complete"), ("cancelled", "Print cancelled"),
                       ("error", "Print stopped with an error")):
        text = summary.format_event(kind, "plate_1.gcode", archive=archive, emit=False)
        assert text.startswith(f"{verb}: Legacy Widget"), text
        # Pre-#1029 jobs have no stamp and behave as before: no mention.
        assert "<@" not in text


# ------------------------------------------------------------- old print records

def test_wrapped_aggregates_over_pre_record_id_records(tmp_path):
    vault = tmp_path / "vault"
    prints = vault / "Prints"
    prints.mkdir(parents=True)
    first = _fixture("print_record_pre_record_id.json")
    (prints / "print-0007.json").write_text(json.dumps(first))
    # A reprint recorded under the SAME number, as pre-#988 writers did —
    # told apart only by its start time (print_stats.record_key's fallback).
    second = {**first, "status": "failed", "success": False,
              "started_at": "2026-05-02T09:00:00Z",
              "completed_at": "2026-05-02T09:20:00Z",
              "print_duration_seconds": 1200.0, "filament_used_g": 4.0}
    (prints / "print-0007-reprint.json").write_text(json.dumps(second))
    # The pre-#392 Markdown note rides along in the same folder.
    shutil.copy(LEGACY / "print_record_obsidian_note.md", prints / "print-0006.md")

    loaded, skipped = print_stats.load_records(vault)
    assert skipped == [] and len(loaded) == 2  # the note is not a JSON record
    keys = {print_stats.record_key(record) for record in loaded}
    assert keys == {"#7@2026-03-04T15:00:00Z", "#7@2026-05-02T09:00:00Z"}
    agg = print_stats.aggregate(loaded, year=2026)
    assert agg["total_prints"] == 2
    assert agg["completed"] == 1 and agg["failed"] == 1
    assert agg["success_rate"] == 0.5
    assert agg["monthly"] == {"2026-03": 1, "2026-05": 1}
    assert agg["breakdowns"]["by_material"]["PLA"]["count"] == 2
    assert agg["breakdowns"]["by_color"]["Black"]["count"] == 2


# ------------------------------------------------------------- old hardware record

def test_state_loader_adopts_the_ifs_slots_record(tmp_path, monkeypatch):
    record = tmp_path / "printer_hardware.json"
    record.write_text((LEGACY / "hardware_ifs_slots.json").read_text())
    monkeypatch.setattr(printer_state, "STATE", str(record))
    state = printer_state.load()
    assert "ifs_slots" not in state
    assert state["filament_slots"]["1"]["spoolman_id"] == 7
    assert state["filament_slots"]["2"]["spoolman_id"] == 9
    assert state["spool"]["spoolman_id"] == 7


def test_doctor_reads_the_old_install_layout(tmp_path, monkeypatch):
    """Doctor's own checks over an old layout: the ifs_slots hardware
    record in the state folder of a data set loaded from defaults."""
    local = tmp_path / "local.toml"
    local.write_text(f'[paths]\nstate_home = "{tmp_path}/hermes"\n')
    data = settings.load(local=local, environ={})
    state_dir = settings.state_dir(data)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "printer_hardware.json").write_text(
        (LEGACY / "hardware_ifs_slots.json").read_text())
    # Settings row: the record's era does not disturb the settings facts.
    assert "nozzle" in doctor.check_settings(data)
    # Hardware row: doctor reads the raw file (it does not adopt the
    # renamed key — it checks spool/temps/nozzle, which the old record
    # carries under unchanged names) and names the nozzle it found.
    nozzle = data["printer"]["nozzle_diameter_mm"]
    assert doctor.check_hardware(data) == f"printer_hardware.json, {nozzle:g} mm nozzle"


# ------------------------------------------------------------- retired settings keys

def test_settings_load_with_every_retired_key_at_once():
    data = settings.load(local=LEGACY / "settings_all_retired_keys.toml", environ={})
    # Both aliases fold onto their current names; the old spellings are gone.
    assert data["printer"]["filament_slots"] == 3
    assert "ifs_slots" not in data["printer"]
    assert data["webui"]["cookie_secure"] is True
    assert "secure_cookie" not in data["webui"]
    # The retired service key is dropped, not carried and not an error.
    assert "openviking_url" not in data["services"]


def test_settings_retired_keys_in_the_environment_fold_the_same_way():
    data = settings.load(local=False, environ={
        "PRINT_HANDY__PRINTER__IFS_SLOTS": "3",
        "PRINT_HANDY__WEBUI__SECURE_COOKIE": "true",
        "PRINT_HANDY__SERVICES__OPENVIKING_URL": "http://old.invalid",
    })
    assert data["printer"]["filament_slots"] == 3
    assert data["webui"]["cookie_secure"] is True
    assert "openviking_url" not in data["services"]


def test_printer_family_is_a_named_refusal_not_an_alias(tmp_path):
    """``[printer] family`` was replaced by ``model``, not renamed: the
    loader refuses it and names the replacement, so an old install gets
    instructions, not a silent drop or a bare 'unknown setting'."""
    local = tmp_path / "local.toml"
    local.write_text('[printer]\nfamily = "sovol_klipper"\n')
    with pytest.raises(settings.SettingsError, match=r"family was replaced by model"):
        settings.load(local=local, environ={})
