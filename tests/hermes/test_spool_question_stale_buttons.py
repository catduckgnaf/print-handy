"""Stale spool-question buttons and replacement ordering (SAFE-5: P1-96, P1-8).

P1-96: a spool-question alert's buttons stay clickable after the question is
answered in text (alerts are retained for hours). If a NEWER question has
since opened, a stale click must be refused against the clicked question's
own identity (recorded_at + filename) — never silently applied to the
current record. A stale Cancel must never cancel a newer print.

P1-8: answering a runout with an existing spool must validate/bind the
replacement BEFORE the old spool is marked depleted. bind_slot validates
first and runs its own 'depleted' disposition after validation, so the
plugin must not PATCH the old spool itself ahead of the bind.

Offline; Spoolman and the printer are fakes.
"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy.runtime import print_stop
from print_handy.runtime import spool_sync

SPEC = importlib.util.spec_from_file_location(
    "print_feedback_safe5", Path(__file__).parents[2] / "plugins/print-feedback/__init__.py")
assert SPEC is not None and SPEC.loader is not None
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)

# The question currently pending on disk: a newer print, a newer question.
CURRENT = {"filename": "newer.gcode", "slot": 2, "prior_spool_id": 8,
           "cause": "runout", "recorded_at": 2000.0,
           "usage_suspended": True, "suspended_spool_id": 8}
# The context a button on the OLDER question's message carries.
STALE_CLICK = {"filename": "older.gcode", "slot": 2, "prior_spool_id": 5,
               "cause": "runout", "recorded_at": 1000.0}


def _listener(record):
    return S(load_runout_pause=lambda: record, clear_runout_pause=mock.Mock())


def _sync(**overrides):
    base = dict(
        SpoolError=spool_sync.SpoolError,
        spoolman=mock.Mock(return_value={}),
        bind_slot=mock.Mock(return_value={"slot": 2, "spool_id": 12, "prior_spool_id": 8}),
        bind_text=lambda result: "Bound spool #12 into slot 2.",
        resume_usage=mock.Mock(),
        load_state=lambda: {"filament_slots": {}},
        create_spool=mock.Mock(),
        screen_hex=spool_sync.screen_hex,
    )
    base.update(overrides)
    return S(**base)


# ---------------------------------------------------------------- P1-96

def test_stale_cancel_click_refused_and_newer_print_untouched(monkeypatch):
    listener = _listener(dict(CURRENT))
    status = mock.Mock(return_value={"state": "paused", "filename": CURRENT["filename"]})
    cancel = mock.Mock(return_value=(True, "Stopped."))
    monkeypatch.setattr(print_stop, "status", status)
    monkeypatch.setattr(print_stop, "cancel", cancel)
    with pytest.raises(spool_sync.SpoolError):
        plugin._cancel_spool_question_print(listener, _sync(), dict(STALE_CLICK))
    cancel.assert_not_called()
    status.assert_not_called()
    listener.clear_runout_pause.assert_not_called()


def test_stale_same_spool_click_refused():
    listener = _listener(dict(CURRENT))
    sync = _sync()
    with pytest.raises(spool_sync.SpoolError):
        plugin._answer_spool_question(listener, sync, dict(STALE_CLICK), {"status": "same"})
    sync.resume_usage.assert_not_called()
    sync.bind_slot.assert_not_called()
    listener.clear_runout_pause.assert_not_called()


def test_stale_existing_spool_click_refused_before_any_write():
    listener = _listener(dict(CURRENT))
    sync = _sync()
    with pytest.raises(spool_sync.SpoolError):
        plugin._answer_spool_question(listener, sync, dict(STALE_CLICK),
                                      {"status": "existing", "spool_id": 12})
    sync.spoolman.assert_not_called()
    sync.bind_slot.assert_not_called()
    listener.clear_runout_pause.assert_not_called()


def test_filename_mismatch_without_timestamp_refused():
    listener = _listener(dict(CURRENT))
    sync = _sync()
    click = {"filename": "older.gcode", "slot": 2, "prior_spool_id": 5, "cause": "runout"}
    with pytest.raises(spool_sync.SpoolError):
        plugin._answer_spool_question(listener, sync, click, {"status": "same"})
    sync.resume_usage.assert_not_called()
    listener.clear_runout_pause.assert_not_called()


def test_click_for_the_current_question_still_cancels(monkeypatch):
    listener = _listener(dict(CURRENT))
    states = iter([{"state": "paused", "filename": CURRENT["filename"]},
                   {"state": "cancelled", "filename": ""}])
    monkeypatch.setattr(print_stop, "status", lambda: next(states))
    cancel = mock.Mock(return_value=(True, "Stopped. Printer now reports cancelled."))
    monkeypatch.setattr(print_stop, "cancel", cancel)
    message = plugin._cancel_spool_question_print(listener, _sync(), dict(CURRENT))
    cancel.assert_called_once_with(CURRENT["filename"])
    listener.clear_runout_pause.assert_called_once_with()
    assert "Print cancelled" in message


# ----------------------------------------------------------------- P1-8

def test_rejected_replacement_leaves_old_spool_untouched():
    record = {"filename": "job.gcode", "slot": 2, "prior_spool_id": 8,
              "cause": "runout", "recorded_at": 1500.0}
    listener = _listener(dict(record))
    sync = _sync(bind_slot=mock.Mock(
        side_effect=spool_sync.SpoolError("spool #12 is already mapped to slot 4. Nothing changed")))
    with pytest.raises(spool_sync.SpoolError):
        plugin._answer_spool_question(listener, sync, dict(record),
                                      {"status": "existing", "spool_id": 12})
    # The old spool must not have been depleted before the bind rejected
    # the replacement: no direct Spoolman write from the plugin at all.
    sync.spoolman.assert_not_called()
    sync.resume_usage.assert_not_called()
    listener.clear_runout_pause.assert_not_called()


def test_runout_answer_depletes_old_spool_through_the_bind():
    record = {"filename": "job.gcode", "slot": 2, "prior_spool_id": 8,
              "cause": "runout", "recorded_at": 1500.0}
    listener = _listener(dict(record))
    sync = _sync(bind_slot=mock.Mock(return_value={
        "slot": 2, "spool_id": 12, "prior_spool_id": 8, "record": {"name": "Sunlu PLA Black"},
        "screen": "pushed", "usage": "unchanged", "prior_disposition": "depleted"}),
        bind_text=spool_sync.bind_text)
    message = plugin._answer_spool_question(listener, sync, dict(record),
                                            {"status": "existing", "spool_id": 12})
    sync.bind_slot.assert_called_once()
    assert sync.bind_slot.call_args.kwargs["prior_disposition"] == "depleted"
    sync.spoolman.assert_not_called()
    sync.resume_usage.assert_called_once_with(12)
    listener.clear_runout_pause.assert_called_once_with()
    # The old spool's fate is stated once, by bind_text (#787).
    assert message.count("Spool #8") == 1
    assert "Spool #8 was marked as depleted (0 g left)." in message


def test_swap_answer_keeps_shelf_disposition_and_no_used_up_note():
    record = {"filename": "job.gcode", "slot": 2, "prior_spool_id": 8,
              "cause": "swap", "recorded_at": 1500.0}
    listener = _listener(dict(record))
    sync = _sync()
    message = plugin._answer_spool_question(listener, sync, dict(record),
                                            {"status": "existing", "spool_id": 12})
    assert sync.bind_slot.call_args.kwargs["prior_disposition"] == "shelf"
    sync.spoolman.assert_not_called()
    assert "marked used up" not in message


# ---------------------------------------------------------------- #787 (typed twin of P2-204)

def test_new_spool_bind_failure_names_the_created_spool():
    record = {"filename": "job.gcode", "slot": 2, "prior_spool_id": 8,
              "cause": "runout", "recorded_at": 1500.0}
    listener = _listener(dict(record))
    sync = _sync(create_spool=mock.Mock(return_value={"spool_id": 31}),
                 bind_slot=mock.Mock(side_effect=spool_sync.SpoolError("Spoolman refused the write")))
    with pytest.raises(spool_sync.SpoolError) as raised:
        plugin._answer_spool_question(listener, sync, dict(record),
                                      {"status": "new", "brand": "Sunlu", "material": "PLA",
                                       "color": "Black", "grams": "1000"})
    assert raised.value.spool_created == 31
    text = str(raised.value)
    assert "Spool #31 was created" in text and "(Spoolman refused the write)" in text
    assert "nothing was changed" not in text.casefold()
    sync.create_spool.assert_called_once()
    sync.resume_usage.assert_not_called()
    listener.clear_runout_pause.assert_not_called()


def test_new_spool_bind_crash_never_shows_the_raw_exception():
    record = {"filename": "job.gcode", "slot": 2, "prior_spool_id": 8,
              "cause": "runout", "recorded_at": 1500.0}
    sync = _sync(create_spool=mock.Mock(return_value={"spool_id": 31}),
                 bind_slot=mock.Mock(side_effect=KeyError("filament_slots")))
    with pytest.raises(spool_sync.SpoolError) as raised:
        plugin._answer_spool_question(_listener(dict(record)), sync, dict(record),
                                      {"status": "new", "brand": "Sunlu", "material": "PLA",
                                       "color": "Black", "grams": "1000"})
    assert raised.value.spool_created == 31
    assert "filament_slots" not in str(raised.value)


def test_existing_spool_bind_failure_is_not_marked_created():
    record = {"filename": "job.gcode", "slot": 2, "prior_spool_id": 8,
              "cause": "runout", "recorded_at": 1500.0}
    sync = _sync(bind_slot=mock.Mock(side_effect=spool_sync.SpoolError("mapped elsewhere")))
    with pytest.raises(spool_sync.SpoolError) as raised:
        plugin._answer_spool_question(_listener(dict(record)), sync, dict(record),
                                      {"status": "existing", "spool_id": 12})
    assert not getattr(raised.value, "spool_created", None)
    sync.create_spool.assert_not_called()
