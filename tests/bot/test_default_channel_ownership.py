"""Job ownership is enforced in the default printer's channel too (P1-77).

``_routed_printer_id`` normalizes the default printer to None (its control
paths stay the legacy ones), and the print-next / print-again ownership
checks were guarded by ``if pid`` — so in the default printer's own
channel, a job belonging to a second printer was offered behind a bed-clear
button and started. Ownership compares the job against the channel's
actual printer, default included; a shared channel stays undecided and
resolves from the job, exactly as #536 built it.
"""
import asyncio
import copy
import json
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy.bot import runtime
from print_handy.bot.router import load_plugin
from print_handy.core import prompts
from print_handy.hermes import _config, print_next

OPERATOR = "200000000000000002"
HOME = "100000000000000001"     # the default printer's own channel
OTHER = "100000000000000099"    # the second printer's own channel
GARAGE = "flashforge-ad5x"      # the default printer (first roster entry)
WORKSHOP = "workshop-ad5x"      # the second printer


@pytest.fixture
def two_printer_cfg(monkeypatch):
    data = copy.deepcopy(_config.cfg())
    base = copy.deepcopy(data["printers"][0])
    data["printers"] = [
        {**base, "id": GARAGE, "name": "Garage AD5X", "chat_id": HOME},
        {**base, "id": WORKSHOP, "name": "Workshop AD5X", "chat_id": OTHER},
    ]
    monkeypatch.setattr(_config, "_override", data)
    return data


class DedicatedGateway:
    """Each channel routes to exactly one printer; nothing is shared."""

    def __init__(self, adapter, routed):
        self._adapter = adapter
        self._routed = routed

    def _is_user_authorized_for_source(self, source):
        return True

    def _delivery_adapter_for(self, source):
        return self._adapter

    def _session_key_for_source(self, source):
        return f"{source.chat_id}:{source.user_id}"

    def printer_id_for(self, source):
        return self._routed.get(str(source.chat_id))

    def shared_printer_ids_for(self, source):
        return None


class PickingAdapter:
    """Records questions and answers each with its first choice (Start)."""

    def __init__(self):
        self.sent = []
        self.questions = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(str(content))
        return S(success=True)

    async def send_clarify(self, chat_id, question, choices, clarify_id=None, session_key=None,
                           metadata=None):
        self.questions.append((question, list(choices)))
        prompts.resolve_gateway_clarify(clarify_id, list(choices)[0])
        return S(success=True)


def run(coro):
    return asyncio.run(coro)


def event(text, chat_id=HOME):
    source = runtime.make_source(OPERATOR, chat_id, platform_name="discord")
    return runtime.make_event(source, text, "m1")


def make_job(tmp_path, printer_id):
    folder = tmp_path / "archive" / "ghost--abc"
    sliced = folder / "sliced" / "orca" / "run1"
    sliced.mkdir(parents=True)
    rows = []
    for n in (1, 2):
        g = sliced / f"plate_{n}.gcode"
        g.write_text(f"; plate {n}\n")
        rows.append({"slot": n, "gcode": str(g)})
    job = {"title": "Ghost", "printer_id": printer_id, "gcode": rows[0]["gcode"],
           "derived_plates": rows}
    (folder / "JOB.json").write_text(json.dumps(job))
    plan = {"plan_id": "ghost", "parts": [
        {"id": "body", "label": "Body", "status": "completed", "quantity": 1,
         "completed_quantity": 1, "run_id": 1, "plate_id": 1, "gcode_file": "plate_1.gcode"},
        {"id": "tail", "label": "Tail", "quantity": 1, "completed_quantity": 0,
         "run_id": 2, "plate_id": 2, "gcode_file": "plate_2.gcode",
         "job_json": str(folder / "JOB.json"), "gcode_path": rows[1]["gcode"]},
    ]}
    plans = tmp_path / "Plans"
    plans.mkdir(exist_ok=True)
    (plans / "ghost.json").write_text(json.dumps(plan))
    return folder, plans, rows


def run_print_next(tmp_path, job_printer, chat_id=HOME):
    folder, plans, rows = make_job(tmp_path, job_printer)
    plugin = load_plugin()
    adapter = PickingAdapter()
    gateway = DedicatedGateway(adapter, {HOME: GARAGE, OTHER: WORKSHOP})
    started = []

    async def fake_start(target):
        started.append(target["gcode"])
        return True, "START REQUEST ACCEPTED"

    real_resolve, real_load = print_next.resolve_next, print_next.load_plan
    with mock.patch.object(print_next, "load_plan", lambda pid: real_load(pid, plans)), \
            mock.patch.object(print_next, "start", fake_start), \
            mock.patch.object(print_next, "resolve_next",
                              lambda plan, part_id=None: real_resolve(plan, tmp_path / "archive",
                                                                      part_id=part_id)), \
            mock.patch("print_handy.hermes.discord_print_summary.find_job", return_value=(None, None)):
        result = run(plugin.print_next_dispatch(event("/print-next ghost", chat_id), gateway))
    return result, adapter, started, rows


# ---------------------------------------------------------------------- print-next

def test_print_next_refuses_a_second_printers_job_in_the_default_channel(two_printer_cfg, tmp_path):
    result, adapter, started, _rows = run_print_next(tmp_path, WORKSHOP)
    assert started == [] and adapter.questions == []  # no bed-clear prompt, no start
    assert any("belongs to Workshop AD5X" in text and "not this channel's printer" in text
               for text in adapter.sent)


def test_print_next_still_starts_the_default_printers_own_job(two_printer_cfg, tmp_path):
    result, adapter, started, rows = run_print_next(tmp_path, GARAGE)
    assert started == [rows[1]["gcode"]]
    assert adapter.questions and adapter.questions[0][1][0] == print_next.START_LABEL


def test_print_next_still_refuses_in_the_second_printers_channel(two_printer_cfg, tmp_path):
    result, adapter, started, _rows = run_print_next(tmp_path, GARAGE, chat_id=OTHER)
    assert started == [] and adapter.questions == []
    assert any("belongs to Garage AD5X" in text for text in adapter.sent)


# ---------------------------------------------------------------------- print-again

def run_print_again(tmp_path, job_printer, chat_id=HOME):
    folder, _plans, rows = make_job(tmp_path, job_printer)
    plugin = load_plugin()
    adapter = PickingAdapter()
    gateway = DedicatedGateway(adapter, {HOME: GARAGE, OTHER: WORKSHOP})
    target = {"job_json": str(folder / "JOB.json"), "gcode": rows[0]["gcode"], "title": "Ghost"}
    started, queued = [], []

    async def fake_start(given):
        started.append(given["gcode"])
        return True, "START REQUEST ACCEPTED"

    from print_handy.hermes import print_control as pc
    from print_handy.hermes import discord_print_summary as summary
    with mock.patch.object(pc, "resolve_print", lambda query: dict(target)), \
            mock.patch.object(pc, "again_question", lambda given: "Print Ghost again?"), \
            mock.patch.object(pc, "queue_copies", lambda given: queued.append(given)), \
            mock.patch.object(pc, "start_estimate_text", lambda *a, **k: "estimate"), \
            mock.patch.object(summary, "gcode_metadata", lambda path: (60, 1.0)), \
            mock.patch.object(summary, "_print_number", lambda given: 7), \
            mock.patch.object(print_next, "start", fake_start):
        result = run(plugin.print_again_dispatch(event("/print-again ghost", chat_id), gateway))
    return result, adapter, started


def test_print_again_refuses_a_second_printers_job_in_the_default_channel(two_printer_cfg, tmp_path):
    result, adapter, started = run_print_again(tmp_path, WORKSHOP)
    assert started == [] and adapter.questions == []  # never offered for confirmation
    assert any("belongs to Workshop AD5X" in text and "not this channel's printer" in text
               for text in adapter.sent)


def test_print_again_still_starts_the_default_printers_own_job(two_printer_cfg, tmp_path):
    result, adapter, started = run_print_again(tmp_path, GARAGE)
    assert len(started) == 1 and adapter.questions
