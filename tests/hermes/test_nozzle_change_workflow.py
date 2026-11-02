"""Workflow-level tests for the guided /nozzle change flow (P1-105).

The regression: after ``heat_for_loosen`` succeeds, a chat delivery failure
(``adapter.send`` raising, or ``_confirm`` raising when button delivery
fails) exited the workflow without calling ``heater_off``, leaving the
hotend at the 250 °C loosen temperature. These tests use fakes only — no
live printer is ever touched.
"""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy.hermes import nozzle_change as nc
from print_handy.hermes import print_control as pc

SPEC = importlib.util.spec_from_file_location(
    "print_feedback_nozzle_workflow",
    Path(__file__).parents[2] / "plugins/print-feedback/__init__.py")
assert SPEC is not None and SPEC.loader is not None
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)

HEAT_DETAIL = "Hotend is at 250 °C (target 250 °C). Remove the old nozzle now."


class Adapter:
    """Fake chat adapter; ``fail_post_heat_send`` makes the send that carries
    the heat result raise, simulating a chat outage after heating."""

    def __init__(self, fail_post_heat_send=False):
        self.sent = []
        self.fail_post_heat_send = fail_post_heat_send

    async def send(self, **kw):
        content = kw.get("content")
        self.sent.append(content)
        if self.fail_post_heat_send and content == HEAT_DETAIL:
            raise RuntimeError("chat delivery failed")
        return S(success=True)


def _answers(**overrides):
    answers = {
        "nozzle-diameter": "0.4 mm",
        "nozzle-type": "Brass",
        "nozzle-heat": f"Confirm heat to {nc.LOOSEN_TEMP_C} °C",
        "nozzle-removed": nc.REMOVED_LABEL,
        "nozzle-torque": nc.TORQUED_LABEL,
        "nozzle-mesh": "Skip for now",
    }
    answers.update(overrides)

    async def confirm(adapter, source, metadata, prefix, question, choices,
                      timeout, live_key=None):
        answer = answers[prefix]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return confirm


def _run_workflow(adapter, confirm):
    source = S(chat_id="c", thread_id=None)
    fake_printer = S(connection="moonraker", supported_nozzle_sizes=None,
                     supported_nozzle_types=None)
    heater_off = mock.Mock(return_value=(True, "Heater turned off. Nothing was recorded."))
    with mock.patch.object(pc, "_adapter_for", return_value=fake_printer), \
            mock.patch.object(plugin, "_confirm", side_effect=confirm), \
            mock.patch.object(nc, "heat_for_loosen", return_value=(True, HEAT_DETAIL)), \
            mock.patch.object(nc, "heater_off", heater_off), \
            mock.patch.object(nc, "cooldown", return_value=(True, "Cooled down.")), \
            mock.patch.object(nc, "finish_change", return_value="Recorded the new nozzle."):
        result = asyncio.run(plugin._nozzle_change_workflow(adapter, source, None))
    return result, heater_off


def test_send_failure_after_heat_turns_heater_off():
    adapter = Adapter(fail_post_heat_send=True)
    source = S(chat_id="c", thread_id=None)
    fake_printer = S(connection="moonraker", supported_nozzle_sizes=None,
                     supported_nozzle_types=None)
    heater_off = mock.Mock(return_value=(True, "Heater turned off. Nothing was recorded."))
    with mock.patch.object(pc, "_adapter_for", return_value=fake_printer), \
            mock.patch.object(plugin, "_confirm", side_effect=_answers()), \
            mock.patch.object(nc, "heat_for_loosen", return_value=(True, HEAT_DETAIL)), \
            mock.patch.object(nc, "heater_off", heater_off):
        with pytest.raises(RuntimeError, match="chat delivery failed"):
            asyncio.run(plugin._nozzle_change_workflow(adapter, source, None))
    heater_off.assert_called_once_with()


def test_confirm_failure_after_heat_turns_heater_off():
    confirm = _answers(**{"nozzle-removed": RuntimeError("button delivery failed")})
    adapter = Adapter()
    source = S(chat_id="c", thread_id=None)
    fake_printer = S(connection="moonraker", supported_nozzle_sizes=None,
                     supported_nozzle_types=None)
    heater_off = mock.Mock(return_value=(True, "Heater turned off. Nothing was recorded."))
    with mock.patch.object(pc, "_adapter_for", return_value=fake_printer), \
            mock.patch.object(plugin, "_confirm", side_effect=confirm), \
            mock.patch.object(nc, "heat_for_loosen", return_value=(True, HEAT_DETAIL)), \
            mock.patch.object(nc, "heater_off", heater_off):
        with pytest.raises(RuntimeError, match="button delivery failed"):
            asyncio.run(plugin._nozzle_change_workflow(adapter, source, None))
    heater_off.assert_called_once_with()


def test_happy_path_records_without_extra_heater_off():
    result, heater_off = _run_workflow(Adapter(), _answers())
    assert result == "Recorded the new nozzle."
    heater_off.assert_not_called()


def test_cancel_after_heat_turns_heater_off_exactly_once():
    result, heater_off = _run_workflow(
        Adapter(), _answers(**{"nozzle-removed": "Cancel"}))
    assert result.startswith("Cancelled.")
    heater_off.assert_called_once_with()
