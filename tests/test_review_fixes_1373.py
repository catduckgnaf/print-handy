"""Findings from the #1373 pre-release review of the chat process, each pinned."""
import json
from pathlib import Path
from types import SimpleNamespace as S

from print_handy import instance
from print_handy.bot import runtime
from print_handy.bot.platform import BasePlatformAdapter
from print_handy.core import nozzle, prepared_intake
from print_handy.runtime import klipper_report


def test_print_anyway_is_labeled_by_whether_printing_is_on():
    assert "(bed is clear)" in prepared_intake.PRINT_ANYWAY_RISK_LABEL
    assert prepared_intake.SLICE_ANYWAY_RISK_LABEL.startswith("Slice anyway")


def test_with_no_approvers_another_member_cannot_answer_someone_elses_prompt():
    adapter = BasePlatformAdapter.__new__(BasePlatformAdapter)
    adapter.denied, adapter.approvers = set(), set()
    adapter.registry = S(session_key=lambda cid: "discord:chat::111", choices=lambda cid: ["Black", "White"])
    assert adapter.may_answer_prompt("111", "c") is True       # the person asked
    assert adapter.may_answer_prompt("222", "c") is False      # anyone else, no approvers configured
    adapter.approvers = {"333"}
    assert adapter.may_answer_prompt("333", "c") is True       # a configured approver
    adapter.registry = S(session_key=lambda cid: "flow", choices=lambda cid: [])  # older flows name nobody
    assert adapter.may_answer_prompt("222", "c") is True


def test_a_reply_naming_a_longer_command_is_not_the_command_itself():
    assert runtime._names_command("Use `/print-stop` to stop.", "/print-stop") is True
    assert runtime._names_command("Try /print-stop or /print-next.", "/print") is False
    assert runtime._names_command("Use /spool list", "/spool list") is True


def test_a_designer_zero_support_gap_is_kept_in_any_spelling():
    for raw in ("0", "0.00", ["0"], 0):
        gaps, reasons = nozzle.resolve_support_gaps(0.2, "designer", {"support_top_z_distance": raw})
        assert gaps["support_top_z_distance"] == 0 and reasons["support_top_z_distance"] == "designer", raw


def test_estimate_text_accepts_a_single_path_result(tmp_path):
    gcode = tmp_path / "plate_1.gcode"
    gcode.write_text("; estimated printing time (normal mode) = 1h 2m 0s\n")
    # A bare Path used to raise TypeError ('PosixPath' object is not subscriptable).
    text = prepared_intake._variant_estimate_str({"title": "x"}, gcode)
    assert isinstance(text, str) and text


def test_the_bot_cancel_marker_belongs_to_the_printer_it_stopped(tmp_path, monkeypatch):
    marker = tmp_path / "last_bot_cancel.json"
    monkeypatch.setattr(klipper_report, "BOT_CANCEL", marker)
    klipper_report.mark_bot_cancel(printer_id="printer-a")
    assert json.loads(marker.read_text())["printer_id"] == "printer-a"
    instance.set_printer_id("printer-b")
    try:
        assert klipper_report.cancelled_by_bot() is False   # printer B's screen cancel is not the bot's
        instance.set_printer_id("printer-a")
        assert klipper_report.cancelled_by_bot() is True
        instance.set_printer_id(None)
        assert klipper_report.cancelled_by_bot() is True     # single-printer process: unnamed counts
    finally:
        instance.set_printer_id(None)
