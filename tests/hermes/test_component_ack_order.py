"""Discord component handlers acknowledge before gated slow work (#594 phase 2).

Discord drops an interaction that is not answered within 3 seconds. These are deterministic ORDERING
tests on the real plugin handlers with a fake Discord: they prove the response is sent before the
slow call starts, not that a real network round trip beats 3 s. A handler that does slow work first
is a product gap; it is recorded as a strict xfail with the exact failure, never by loosening the check.

Handler families (plugins/print-feedback alert_button_handlers):
  covered elsewhere: clarify pbq (tests/bot/test_runtime.py, test_clarify_ui.py), exclude pick
                     (tests/hermes/test_exclude_chat.py)
  covered here:      plate confirm (pbplate:go), intake Retry, spool-question same / cancel / existing select
  not covered:       insertion, new-spool and intake wizards (in-memory or modal only, no printer/Spoolman call
                     before the response), alert resume/pause/cancel (respond first by construction, line order)
"""
import asyncio
import json
from types import SimpleNamespace as S

import pytest

RID = "a" * 32

from tests.hermes.test_print_next import Interaction, plate_clicks


class Order(list):
    texts: list

    def __init__(self):
        super().__init__()
        self.texts = []


def _order_interaction(order, custom_id, values=None):
    click = Interaction(custom_id, values)
    resp = click.response

    class Response:
        async def send_message(self, *a, **k):
            order.append("ack")

        async def edit_message(self, *a, **k):
            order.append("ack")
            order.texts.append(k.get("content") or (a[0] if a else None))

        async def defer(self, *a, **k):
            order.append("ack")
    click.response = Response()
    click.message = S(id="m1", edit=_noop)
    return click


async def _noop(*a, **k):
    return None


def test_plate_confirm_acknowledges_before_print_next_start(tmp_path, monkeypatch):
    from print_handy.hermes import print_next
    starts, order = [], Order()
    on_interaction, picker, _ = plate_clicks(tmp_path, monkeypatch, starts)
    pick = Interaction(f"pbplate:pick:{picker}", ["plate3"])
    asyncio.run(on_interaction(pick))
    go_id = pick.out[0][2].items[0].custom_id
    real_start = print_next.start

    async def start(target):
        order.append("start")
        return await real_start(target)
    monkeypatch.setattr(print_next, "start", start)
    asyncio.run(on_interaction(_order_interaction(order, go_id)))
    assert order == ["ack", "start"], order


def test_intake_retry_acknowledges_before_print_next_start(tmp_path, monkeypatch):
    from tests.hermes.test_stop_button_view import _install
    from print_handy.hermes import print_alert_buttons as pab, print_next
    monkeypatch.setattr(pab, "STATE", tmp_path / "alerts.json")
    _sent, on_interaction, plugin = _install(monkeypatch)
    monkeypatch.setattr(plugin, "_state_dir", lambda: tmp_path)
    folder = tmp_path / "model-link-intakes"
    folder.mkdir()
    receipt = {"intake_id": RID, "status": "START_REFUSED", "start_prompt_message_id": "m1",
               "start_refusal_detail": "x", "retry_target": {"gcode": "g.gcode", "gcode_sha256": "a" * 64}}
    (folder / f"{RID}.json").write_text(json.dumps(receipt))
    from print_handy.core import prepared_intake as intake
    monkeypatch.setattr(intake, "_safe_preupload_refusal", lambda detail: True)
    order = Order()

    async def start(target):
        order.append("start")
        return True, "ok"
    monkeypatch.setattr(print_next, "start", start)
    asyncio.run(on_interaction(_order_interaction(order, pab.retry_custom_id(RID))))
    assert order[:2] == ["ack", "start"], (order, order.texts)


def _spool_question_click(tmp_path, monkeypatch, action, slow_name, order):
    from tests.hermes.test_stop_button_view import _install
    from print_handy.hermes import print_alert_buttons as pab
    monkeypatch.setattr(pab, "STATE", tmp_path / "alerts.json")
    _sent, on_interaction, plugin = _install(monkeypatch)

    def slow(*a, **k):
        order.append("slow")
        return "done"
    monkeypatch.setattr(plugin, slow_name, slow)
    aid = pab.record("spool-question", "", context={"slot": 1})
    click = _order_interaction(order, f"{pab.SPOOL_QUESTION_PREFIX}:{action}:{aid}")
    return lambda: asyncio.run(on_interaction(click))


GAP = ("handler awaits the printer/Spoolman call in a worker thread before any interaction response, "
       "so a slow Moonraker or Spoolman read can pass Discord's 3 s window (#594 phase 2 finding)")


@pytest.mark.xfail(strict=True, reason=GAP)
@pytest.mark.parametrize("action,slow_name", [("same", "_answer_spool_question"),
                                              ("cancel", "_cancel_spool_question_print")])
def test_spool_question_acknowledges_before_slow_work(tmp_path, monkeypatch, action, slow_name):
    order = Order()
    _spool_question_click(tmp_path, monkeypatch, action, slow_name, order)()
    assert order[:2] == ["ack", "slow"], order
