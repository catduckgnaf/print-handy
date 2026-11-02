"""P1-7: a fresh install must be able to provision the mandatory hardware record.

Nothing created state/printer_hardware.json on a clean install, and
printer_state.load() opens it unconditionally — even `printer_state set`
loaded before it could save, so a fresh installation could not initialize
the record without hand-writing the file. Provisioning seeds the same
minimal, truthful record guided onboarding writes for a new printer (#537),
and `set` bootstraps it instead of crashing.
"""
import sys
from types import SimpleNamespace

import pytest

from print_handy import settings
from print_handy.core import printer_state

VIEW = {"printer": {"name": "Bench P1", "bed_mm": [256, 256], "max_print_height_mm": 256,
                    "nozzle_diameter_mm": 0.4},
        "services": {"moonraker_url": ""},
        "material_temps": {"PLA": {"nozzle": 210, "bed": 60}}}


@pytest.fixture
def fresh_state(tmp_path, monkeypatch):
    state = tmp_path / "printer_hardware.json"
    monkeypatch.setattr(printer_state, "STATE", str(state))
    monkeypatch.setattr(settings, "default_printer_id", lambda data=None: "bench-p1")
    return state


def test_provision_creates_the_default_record_at_the_legacy_path(fresh_state):
    assert printer_state.provision_default_record(VIEW) is True
    record = printer_state.load()
    assert record["printer"] == "Bench P1"
    assert record["nozzle"]["diameter_mm"] == 0.4
    assert record["temps_c"] == {"nozzle": 210, "bed": 60}
    assert record["spool"] == {} and record["filament_slots"] == {}


def test_provision_never_overwrites_an_existing_record(fresh_state):
    assert printer_state.provision_default_record(VIEW) is True
    changed = {**VIEW, "printer": {**VIEW["printer"], "name": "Other"}}
    assert printer_state.provision_default_record(changed) is False
    assert printer_state.load()["printer"] == "Bench P1"


def test_provision_is_a_no_op_when_a_per_printer_record_exists(fresh_state, tmp_path, monkeypatch):
    per_printer = tmp_path / "printers" / "bench-p1" / "printer_hardware.json"
    per_printer.parent.mkdir(parents=True)
    per_printer.write_text("{}\n")
    assert printer_state.provision_default_record(VIEW) is False
    assert not fresh_state.exists()


def test_set_bootstraps_a_missing_record_instead_of_crashing(fresh_state, monkeypatch):
    monkeypatch.setattr(settings, "view_for", lambda pid, data=None: VIEW)
    monkeypatch.setattr(sys, "argv", ["printer_state", "set", "--nozzle", "0.6"])
    printer_state.main()
    record = printer_state.load()
    assert record["nozzle"]["diameter_mm"] == 0.6


def test_check_on_a_fresh_install_names_the_provisioning_step(fresh_state, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["printer_state", "check"])
    with pytest.raises(SystemExit) as excinfo:
        printer_state.main()
    assert excinfo.value.code == 2
    assert "provision" in capsys.readouterr().err


def test_provision_subcommand_creates_the_record(fresh_state, monkeypatch):
    monkeypatch.setattr(settings, "view_for", lambda pid, data=None: VIEW)
    monkeypatch.setattr(sys, "argv", ["printer_state", "provision"])
    printer_state.main()
    assert printer_state.load()["printer"] == "Bench P1"
    # And a second run is a quiet no-op.
    printer_state.main()
