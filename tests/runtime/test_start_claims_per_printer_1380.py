"""/print-stop on an idle printer names only that printer's start in flight (#1380).

The 2.3.2 review: start claims were not per printer, so Stop on idle printer B said
"<printer A's print> is starting" and offered to stop it. The claim now records the
job's printer, written by print-now's own claim and read by print_stop.
"""
from unittest import mock

import pytest

from print_handy.core import launch_state
from print_handy.launch import print_now
from print_handy.runtime import print_stop, start_claims

FILE = "Lantern_plate_1_" + "cd" * 32 + ".gcode.3mf"


@pytest.fixture(autouse=True)
def _claims(tmp_path, monkeypatch):
    path = tmp_path / "start_claims.json"
    monkeypatch.setattr(start_claims, "START_CLAIMS", path)
    monkeypatch.setattr(print_now, "START_CLAIMS", path)
    monkeypatch.setattr(launch_state, "in_flight", lambda *a, **k: None)
    return path


def _standby():
    return {"state": "standby", "filename": "", "layer": None, "total_layer": None,
            "progress": None, "print_duration": None}


def _cancel_on(printer_id):
    with mock.patch.object(print_stop, "_lan_adapter", return_value=object()), \
            mock.patch.object(print_stop, "status", return_value=_standby()), \
            mock.patch.object(print_stop, "_call") as call:
        sent, text = print_stop.cancel("", printer_id=printer_id)
    call.assert_not_called()
    assert not sent
    return text


def test_stop_on_another_idle_printer_does_not_name_this_printers_start():
    print_now.claim_start_announcement(FILE, printer_id="printer-a")
    assert "is starting" in _cancel_on("printer-a")
    text = _cancel_on("printer-b")
    assert "is starting" not in text and "Nothing to stop" in text


def test_an_unnamed_claim_still_counts_for_any_printer():
    print_now.claim_start_announcement(FILE)                  # single-printer install: no id
    assert "is starting" in _cancel_on("printer-b")
    assert "is starting" in _cancel_on(None)


def test_a_new_claim_keeps_the_other_printers_and_drops_stale_ones():
    print_now.claim_start_announcement("old.gcode", now=0, printer_id="printer-a")
    print_now.claim_start_announcement(FILE, printer_id="printer-b")
    assert start_claims.claim_printer(FILE) == "printer-b"
    assert start_claims.claim_printer("old.gcode") is None   # expired claim, its printer dropped
    assert start_claims.claimed_start_filenames() == [FILE]   # the printer map is not a claim
    assert start_claims.start_claimed(FILE)
