"""Runout "same" answer: a fresh roll of the same filament, never the empty spool."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy.hermes import print_alert_buttons as pab
from print_handy.hermes import spool_sync

SPEC = importlib.util.spec_from_file_location(
    "print_feedback_same_filament", Path(__file__).parents[2] / "plugins/print-feedback/__init__.py")
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)

RUNOUT = {"filename": "a.gcode", "slot": 3, "prior_spool_id": 20, "cause": "runout",
          "recorded_at": 1.0, "usage_suspended": True, "suspended_spool_id": 20}


def _listener(record):
    return S(load_runout_pause=lambda: record, clear_runout_pause=mock.Mock())


def _sync(**overrides):
    base = dict(SpoolError=spool_sync.SpoolError,
                fresh_spool_like=mock.Mock(return_value={"spool_id": 31, "filament_id": 13, "grams": 1000.0}),
                bind_slot=mock.Mock(return_value={"slot": 3, "spool_id": 31, "prior_spool_id": 20}),
                bind_text=lambda r: "Spool #31 is in slot 3; spool #20 was marked as depleted.",
                resume_usage=mock.Mock())
    base.update(overrides)
    return S(**base)


def test_runout_same_clones_binds_depletes_and_charges_the_new_roll():
    lis, sync = _listener(dict(RUNOUT)), _sync()
    text = plugin._answer_spool_question(lis, sync, dict(RUNOUT), {"status": "same"})
    sync.fresh_spool_like.assert_called_once_with(20)
    assert sync.bind_slot.call_args.args == (3, 31)
    assert sync.bind_slot.call_args.kwargs["prior_disposition"] == "depleted"
    sync.resume_usage.assert_called_once_with(31)
    lis.clear_runout_pause.assert_called_once()
    assert "Created spool #31" in text and "like #20" in text and "#31" in text


def test_swap_same_still_keeps_the_spool():
    swap = {**RUNOUT, "cause": "swap"}
    lis, sync = _listener(dict(swap)), _sync()
    text = plugin._answer_spool_question(lis, sync, dict(swap), {"status": "same"})
    sync.fresh_spool_like.assert_not_called()
    sync.bind_slot.assert_not_called()
    sync.resume_usage.assert_called_once_with(20)
    assert "same spool #20" in text


def test_clone_bind_failure_names_the_new_spool_so_retry_does_not_duplicate():
    lis = _listener(dict(RUNOUT))
    sync = _sync(bind_slot=mock.Mock(side_effect=spool_sync.SpoolError("slot is busy")))
    with pytest.raises(spool_sync.SpoolError) as err:
        plugin._answer_spool_question(lis, sync, dict(RUNOUT), {"status": "same"})
    assert err.value.spool_created == 31
    sync.resume_usage.assert_not_called()


def test_fresh_spool_like_copies_type_weight_and_price():
    calls = []

    def client(method, path, body):
        calls.append((method, path, body))
        if method == "GET":
            return {"id": 20, "initial_weight": 1000, "price": 13.99, "filament": {"id": 13, "weight": 1000}}
        return {"id": 31}

    made = spool_sync.fresh_spool_like(20, client=client)
    assert made == {"spool_id": 31, "filament_id": 13, "grams": 1000.0}
    post = calls[-1]
    assert post[:2] == ("POST", "/api/v1/spool")
    assert post[2]["filament_id"] == 13 and post[2]["remaining_weight"] == 1000.0 and post[2]["price"] == 13.99
    assert not [c for c in calls if c[0] == "PATCH"]   # the old spool is left to bind_slot


def test_fresh_spool_like_refuses_without_type_or_weight():
    with pytest.raises(spool_sync.SpoolError):
        spool_sync.fresh_spool_like(20, client=lambda *a: {"id": 20, "filament": {}})
    with pytest.raises(spool_sync.SpoolError):
        spool_sync.fresh_spool_like(20, client=lambda *a: {"id": 20, "filament": {"id": 13}})


def test_runout_button_says_new_roll():
    labels = lambda cause: [b["label"] for b in pab.spool_question_components("x", 20, cause)[0]["components"]]
    assert labels("runout")[0] == "New roll, same filament"
    assert labels("swap")[0] == "Same spool"
    assert labels(None)[0] == "Same spool"
