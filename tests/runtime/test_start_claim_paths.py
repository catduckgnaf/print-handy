"""The start claim and recorded start message match the printer's 3MF path (Print #64 duplicate)."""
import json
import time

from print_handy.runtime import moonraker_event_listener as mel
from print_handy.runtime import print_next

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


def test_start_message_records_originating_channel(tmp_path, monkeypatch):
    """P2-11: the destination the start post went to is recorded with the id,
    so the warmup edit targets the originating channel, not the configured one."""
    monkeypatch.setattr(print_next, "START_MESSAGES", tmp_path / "messages.json")
    print_next.record_start_message(UPLOADED, "123", chat_id="chan9")
    assert print_next.take_start_message_target("3mf_print/Slim_plate_1_ab.gcode") == ("123", "chan9")
    assert print_next.take_start_message_target("3mf_print/Slim_plate_1_ab.gcode") == (None, None)


def test_start_message_records_originating_thread_over_channel(tmp_path, monkeypatch):
    """A start posted into a Discord thread lives in the thread channel."""
    monkeypatch.setattr(print_next, "START_MESSAGES", tmp_path / "messages.json")
    print_next.record_start_message(UPLOADED, "124", chat_id="chan9", thread_id="thread7")
    assert print_next.take_start_message_target(UPLOADED) == ("124", "thread7")


def test_start_message_without_destination_records_none(tmp_path, monkeypatch):
    monkeypatch.setattr(print_next, "START_MESSAGES", tmp_path / "messages.json")
    print_next.record_start_message(UPLOADED, "125")
    assert print_next.take_start_message_target(UPLOADED) == ("125", None)
