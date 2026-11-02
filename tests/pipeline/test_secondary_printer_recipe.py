"""The recorded slice recipe must be recomputed against the job's own printer.

slice_job records the recipe from printer_state.load(job["printer_id"]),
so a secondary printer whose nozzle or filament slots differ from the
default printer's must not read as a changed recipe at launch or during
slicing (Codex P1 on #786): an unchanged secondary-printer job has to
launch, and a real change on that printer still has to be refused.
"""

import copy

import pytest

from print_handy import settings
from print_handy.core import model_job as mj
from print_handy.core import slice_recipe as recipe_module

from .test_slice_recipe import _job

SECONDARY = 'workshop-ad5x'

TWO_PRINTERS = """
discord:
  chat_id: "111"
printers:
  - id: garage-ad5x
    name: Garage AD5X
    model: flashforge.ad5x
    moonraker_url: http://192.168.1.60:7125
  - id: workshop-ad5x
    name: Workshop AD5X
    model: flashforge.ad5x
    chat_id: "222"
    moonraker_url: http://192.168.1.61:7125
"""


@pytest.fixture
def two_printers(tmp_path, monkeypatch):
    local = tmp_path / "local.yaml"
    local.write_text(TWO_PRINTERS)
    data = settings.load(home=settings.HOME, local=local, environ={})
    assert [entry["id"] for entry in data["printers"]] == ["garage-ad5x", SECONDARY]
    monkeypatch.setattr(settings, "current", lambda: data)
    return data


def _two_records(monkeypatch):
    """printer_state.load serving two printers whose records differ."""
    default = {'nozzle': {'diameter_mm': 0.6, 'type': 'brass'},
               'filament_slots': {'1': {'color': 'white', 'material': 'PLA', 'hex': 'FFFFFF'}}}
    secondary = {'nozzle': {'diameter_mm': 0.4, 'type': 'hardened steel'},
                 'filament_slots': {'1': {'color': 'black', 'material': 'PETG', 'hex': '000000'},
                                    '2': {'color': 'red', 'material': 'PETG', 'hex': 'FF0000'}}}
    records = {None: default, SECONDARY: secondary}
    monkeypatch.setattr(mj.printer_state, 'load',
                        lambda printer_id=None: copy.deepcopy(records.get(printer_id, default)))
    return records


def _recorded(job, state, source_sha256):
    job['slice_recipe'] = recipe_module.record(
        recipe_module.slice_recipe(job, source_sha256, state))
    return job


def test_recipe_changed_recomputes_against_the_jobs_own_printer(two_printers, monkeypatch):
    records = _two_records(monkeypatch)
    job = _recorded(_job(printer_id=SECONDARY), records[SECONDARY], 'source-a')
    assert mj._recipe_changed(job, job) is False
    assert mj._recipe_changed(job, job, SECONDARY) is False


def test_recipe_changed_still_detects_a_change_on_the_jobs_own_printer(two_printers, monkeypatch):
    records = _two_records(monkeypatch)
    job = _recorded(_job(printer_id=SECONDARY), records[SECONDARY], 'source-a')
    assert mj._recipe_changed(job, job) is False
    records[SECONDARY]['nozzle']['diameter_mm'] = 0.8  # the secondary printer's nozzle changes after slicing
    assert mj._recipe_changed(job, job) is True


def test_recipe_changed_keeps_the_legacy_load_for_jobs_without_a_printer(two_printers, monkeypatch):
    records = _two_records(monkeypatch)
    job = _recorded(_job(), records[None], 'source-a')
    assert mj._recipe_changed(job, job) is False


def test_launch_accepts_an_unchanged_secondary_printer_job(two_printers, tmp_path, monkeypatch):
    records = _two_records(monkeypatch)
    src = tmp_path / 'model.stl'
    src.write_bytes(b'solid secondary\n')
    gcode = tmp_path / 'plate_1.gcode'
    gcode.write_text('G1 X0\n')
    job = _recorded(_job(printer_id=SECONDARY, downloaded_file=str(src),
                         gcode=str(gcode), gcode_verified=True),
                    records[SECONDARY], mj.file_sha256(src))
    job['artifact_binding'] = {'source_sha256': mj.file_sha256(src),
                               'decisions_sha256': mj.decisions_digest(job.get('color_decisions')),
                               'gcode_sha256': mj.file_sha256(gcode),
                               'recipe_version': job['slice_recipe']['version'],
                               'recipe_sha256': job['slice_recipe']['sha256']}
    mj.require_current_artifact(job, gcode)  # must not raise: nothing changed since slicing
