"""First-step refactor helpers for #1590: behavior-preserving extractions.

Each test pins one new helper through the production entry point its
callers use: the shared send/read-back contract, the frozen launch
target, the delivery refusal wording, the press authorization, the
question view, the listener job table and the Web UI body reader.
"""
import io
import json
from dataclasses import FrozenInstanceError
from types import SimpleNamespace as S

import pytest

from print_handy.launch import print_now
from print_handy.runtime import print_stop


# -- item 2: send_once_and_read_back -----------------------------------------

def test_send_once_and_read_back_defines_took_as_reaching_the_wanted_state():
    sends = []
    reads = iter([{"state": "printing"}, {"state": "paused"}])
    took, after = print_stop.send_once_and_read_back(
        lambda: sends.append("pause") or True, lambda: next(reads), "paused",
        wait_s=5, poll_s=0)
    assert sends == ["pause"]  # sent exactly once, never retried
    assert took is True and after == {"state": "paused"}


def test_send_once_and_read_back_reports_not_took_when_the_state_never_lands():
    took, after = print_stop.send_once_and_read_back(
        lambda: True, lambda: {"state": "printing"}, "paused", wait_s=0.05, poll_s=0)
    assert took is False and after == {"state": "printing"}


def test_send_once_and_read_back_accepts_a_state_predicate():
    reads = iter([{"state": "printing"}, {"state": "standby"}])
    took, after = print_stop.send_once_and_read_back(
        lambda: True, lambda: next(reads), lambda st: st["state"] not in print_stop.ACTIVE,
        wait_s=5, poll_s=0)
    assert took is True and after["state"] == "standby"


# -- items 1 and 3: launch target and the delivery refusal --------------------

def test_launch_target_is_frozen_and_built_once_from_a_job(tmp_path):
    gcode = tmp_path / "plate_1.gcode"
    gcode.write_text("G1 X0\n")
    job_path = tmp_path / "JOB.json"
    job_path.write_text(json.dumps({"gcode": str(gcode), "printer_id": "p1"}))
    target = print_now.build_launch_target(job_path)
    assert target.gcode == gcode.resolve()
    assert target.job_path == job_path.resolve()
    assert target.printer_id == "p1"
    assert target.sha is not None and len(target.sha) == 64
    with pytest.raises(FrozenInstanceError):
        target.printer_id = "other"


def test_delivery_not_enabled_refusal_keeps_the_transport_wording(monkeypatch):
    monkeypatch.setattr(print_now, "_verify_routed", lambda *a, **k: None)
    adapter = S(label="Prusa XL")
    with pytest.raises(SystemExit) as caught:
        print_now._refuse_delivery_not_enabled(
            adapter, S(), "/tmp/x.gcode", None, None,
            via="PrusaLink", status_note="the link status alone is",
            handoff="move the file yourself")
    assert str(caught.value) == (
        "REFUSED: Prusa XL print delivery is not enabled in Print Handy (add "
        "'printer' to [delivery] methods to print over PrusaLink; the link status "
        "alone is read-only). /tmp/x.gcode passed offline checks; move the file "
        "yourself (not yet tested on a printer). Nothing uploaded or started.")


# -- item 5: authorize_press ---------------------------------------------------

def test_authorize_press_answers_allowed_and_approver_kinds():
    from print_handy.bot.platform import BasePlatformAdapter
    adapter = BasePlatformAdapter(["1"], approver=["2"])
    press = lambda uid, bot=False: S(user=S(id=uid, bot=bot), guild=None, channel_id="9")
    assert adapter.authorize_press(press("1"), "allowed") is True
    assert adapter.authorize_press(press("1"), "approver") is False
    assert adapter.authorize_press(press("2"), "approver") is True
    assert adapter.authorize_press(press("3"), "allowed") is False
    assert adapter.authorize_press(press("1", bot=True), "allowed") is False


# -- item 6: _question_view ------------------------------------------------------

def test_question_view_carries_the_question_timeout():
    pytest.importorskip("discord")
    from print_handy.bot.handlers import common
    view = common._question_view()
    assert view.timeout == common._question_timeout()


# -- item 7: the listener job table ----------------------------------------------

def test_run_due_jobs_runs_due_jobs_once_and_warns_per_failure(monkeypatch):
    from print_handy.runtime import moonraker_event_listener as mel
    warnings = []
    monkeypatch.setattr(mel, "warn", warnings.append)
    listener = mel.Listener.__new__(mel.Listener)
    listener._job_last_run = {}
    ran = []

    async def good():
        ran.append("good")

    async def bad():
        raise RuntimeError("boom")

    import asyncio
    asyncio.run(listener._run_due_jobs(
        [("good job", 60, good), ("bad job", 60, bad), ("later job", 10 ** 9, good)],
        now=1000.0))
    assert ran == ["good"]
    assert warnings == ["bad job failed: RuntimeError('boom')"]
    # A second pass inside the interval runs nothing again.
    asyncio.run(listener._run_due_jobs([("good job", 60, good)], now=1001.0))
    assert ran == ["good"]


# -- item 8: Web UI body reading ---------------------------------------------------

def _fake_handler(raw, headers):
    refused = []
    handler = S(headers=headers, rfile=io.BytesIO(raw),
                _refuse_unread_body=lambda: refused.append(True))
    return handler, refused


def test_body_read_json_parses_an_object_and_refuses_the_rest():
    from print_handy.webui import body
    handler, _refused = _fake_handler(b'{"confirm": true}',
                                      {"Content-Type": "application/json", "Content-Length": "17"})
    parsed, error = body.read_json(handler, limit=4096, noun="control", tail="Nothing sent.")
    assert error is None and parsed == {"confirm": True}

    handler, refused = _fake_handler(b"x" * 5000,
                                     {"Content-Type": "application/json", "Content-Length": "5000"})
    parsed, error = body.read_json(handler, limit=4096, noun="control", tail="Nothing sent.")
    assert parsed is None and error == (400, "bad or oversized body. Nothing sent.")
    assert refused == [True]

    handler, _refused = _fake_handler(b"[1]",
                                      {"Content-Type": "application/json", "Content-Length": "3"})
    parsed, error = body.read_json(handler, limit=4096, noun="spool", tail="Nothing changed.")
    assert parsed is None and error == (400, "body must be a JSON object. Nothing changed.")


def test_body_read_form_parses_urlencoded_and_caps_the_size():
    from print_handy.webui import body
    handler, _refused = _fake_handler(
        b"a=1&b=two", {"Content-Type": "application/x-www-form-urlencoded", "Content-Length": "9"})
    form, error = body.read_form(handler)
    assert error is None and form == {"a": ["1"], "b": ["two"]}

    handler, refused = _fake_handler(
        b"", {"Content-Type": "application/x-www-form-urlencoded", "Content-Length": "70000"})
    form, error = body.read_form(handler)
    assert form is None and error == (413, "form body too large") and refused == [True]
