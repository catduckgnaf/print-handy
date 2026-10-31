"""The start claim and recorded start message match the printer's 3MF path (Print #64 duplicate)."""
import json
import time

from print_handy.hermes import moonraker_event_listener as mel
from print_handy.hermes import print_next

UPLOADED = "Slim_plate_1_ab.gcode.3mf"


def test_claim_matches_zmod_3mf_unpack_path(tmp_path, monkeypatch):
    claims = tmp_path / "claims.json"
    claims.write_text(json.dumps({UPLOADED: time.time()}))
    monkeypatch.setattr(mel, "START_CLAIMS", claims)
    assert mel.start_claimed("3mf_print/Slim_plate_1_ab.gcode")
    assert mel.start_claimed(UPLOADED)
    assert not mel.start_claimed("3mf_print/Other_plate_1.gcode")


def test_start_message_found_by_printer_path(tmp_path, monkeypatch):
    monkeypatch.setattr(print_next, "START_MESSAGES", tmp_path / "messages.json")
    print_next.record_start_message(UPLOADED, "123")
    assert print_next.take_start_message("3mf_print/Slim_plate_1_ab.gcode") == "123"
    assert print_next.take_start_message("3mf_print/Slim_plate_1_ab.gcode") is None
