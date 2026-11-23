"""Legacy hardware-record regression: listener and cross-printer slot readers (#1817)."""
import json

from print_handy.runtime import moonraker_event_listener as listener
from print_handy.runtime import spool_sync


def test_listener_slot_readers_retain_pre_rename_slot_assignments(tmp_path, monkeypatch):
    path = tmp_path / "printer_hardware.json"
    path.write_text(json.dumps({"active_slot": 2,
                                "ifs_slots": {"2": {"spoolman_id": 12, "color": "Purple"}}}))
    monkeypatch.setattr(listener, "_hardware_file", lambda: path)
    assert listener._slot_record(2)["spoolman_id"] == 12
    assert listener._mapped_spool(2) == 12
    assert listener.recovery_slot("old.gcode") == (2, 12)
    # Only reads are normalized; the original record is never rewritten here.
    assert "ifs_slots" in json.loads(path.read_text())


def test_loaded_elsewhere_checks_pre_rename_hardware_state(tmp_path):
    root = tmp_path / "state" / "printers"
    other = root / "printer-b" / "printer_hardware.json"
    other.parent.mkdir(parents=True)
    other.write_text(json.dumps({"ifs_slots": {"3": {"spoolman_id": 22}}}))
    own = root / "printer-a" / "printer_hardware.json"
    assert spool_sync.spool_loaded_elsewhere(22, state_path=own, printer_id="printer-a") == "printer-b"
