"""Discord component handlers acknowledge before gated slow work (#594 phase 2).

Discord drops an interaction that is not answered within 3 seconds. These are deterministic ORDERING
tests on the real plugin handlers with a fake Discord: they prove the response is sent before the
slow call starts, not that a real network round trip beats 3 s. A handler that does slow work first
is a product gap; it is recorded as a strict xfail with the exact failure, never by loosening the check.

Handler families (plugins/print-feedback alert_button_handlers):
  covered elsewhere: clarify pbq (tests/bot/test_runtime.py, test_clarify_ui.py), exclude pick
                     (tests/hermes/test_exclude_chat.py)
  covered here:      plate confirm (pbplate:go), intake Retry, spool-question same / cancel / existing select
                     insertion existing / new, spool-question existing / new, and the picker selection that
                     re-reads Spoolman for the bind confirmation
  not covered:       new-spool slot and disposition steps and intake wizard steps (in-memory only, no
                     printer/Spoolman call before the response), alert resume/pause/cancel (respond first
                     by construction, line order)
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


def test_an_unexpected_retry_error_is_uncertain_and_offers_no_retry(tmp_path, monkeypatch):
    # Codex on #182: an exception out of print_next.start may follow an upload or start, so it must not
    # be dressed up as "REFUSED: ... Nothing uploaded or started" with a fresh Retry button.
    from tests.hermes.test_stop_button_view import _install
    from print_handy.hermes import print_alert_buttons as pab, print_next
    monkeypatch.setattr(pab, "STATE", tmp_path / "alerts.json")
    _sent, on_interaction, plugin = _install(monkeypatch)
    monkeypatch.setattr(plugin, "_state_dir", lambda: tmp_path)
    folder = tmp_path / "model-link-intakes"
    folder.mkdir()
    saved = "REFUSED: printer is busy. Nothing uploaded or started."
    receipt = {"intake_id": RID, "status": "START_REFUSED", "start_prompt_message_id": "m1",
               "start_refusal_detail": saved, "retry_target": {"gcode": "g.gcode", "gcode_sha256": "a" * 64}}
    (folder / f"{RID}.json").write_text(json.dumps(receipt))

    async def start(target):
        raise ConnectionResetError("printer closed the connection mid-upload")
    monkeypatch.setattr(print_next, "start", start)
    edits = []
    click = _order_interaction(Order(), pab.retry_custom_id(RID))

    async def edit(**kwargs):
        edits.append(kwargs)
    click.message = S(id="m1", edit=edit)
    asyncio.run(on_interaction(click))
    after = json.loads((folder / f"{RID}.json").read_text())
    assert after["status"] == "START_UNCERTAIN" and after["started"] is None
    assert after["start_refusal_detail"].startswith("UNCERTAIN:")
    assert edits and edits[-1]["view"] is None and "Do not retry" in edits[-1]["content"]


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


@pytest.mark.parametrize("action,slow_name", [("same", "_answer_spool_question"),
                                              ("cancel", "_cancel_spool_question_print")])
def test_spool_question_acknowledges_before_slow_work(tmp_path, monkeypatch, action, slow_name):
    """Was a strict xfail for the #594 phase 2 gap (the handler awaited the
    printer/Spoolman call before any interaction response, past Discord's
    3 s window); the handlers now acknowledge first (P2-164)."""
    order = Order()
    _spool_question_click(tmp_path, monkeypatch, action, slow_name, order)()
    assert order[:2] == ["ack", "slow"], order


def _bind_click(tmp_path, monkeypatch, order, custom_id):
    click = _order_interaction(order, custom_id)

    async def edit_original(content=None, view=None, **kw):
        order.append("edit")
        order.texts.append(content)
    click.edit_original_response = edit_original
    return click


def test_intake_confirm_acknowledges_before_the_bind(tmp_path, monkeypatch):
    """The walkthrough confirmation creates and binds through Spoolman and
    Moonraker calls that can outlast Discord's 3 s window; the interaction
    must be deferred before the bind starts (P2-191)."""
    from tests.hermes.test_stop_button_view import _install
    from print_handy.hermes import print_alert_buttons as pab
    monkeypatch.setattr(pab, "STATE", tmp_path / "alerts.json")
    _sent, on_interaction, plugin = _install(monkeypatch)
    order = Order()

    def slow_bind(slot, values, spool_sync, disposition, expected_prior_spool_id=None):
        order.append("slow")
        return "Spool #20 created and loaded into slot 1."
    monkeypatch.setattr(pab, "create_and_bind_insertion", slow_bind)
    aid = pab.record("intake", "", origin="insertion", slot=1, step="confirm",
                     brand="Sunlu", material="PLA", color="Black", grams="1000",
                     disposition="shelf", old_spool_id=None)
    click = _bind_click(tmp_path, monkeypatch, order, f"{pab.INTAKE_PREFIX}:confirm:{aid}")
    asyncio.run(on_interaction(click))
    assert order[:2] == ["ack", "slow"], order
    assert order[-1] == "edit" and "loaded into slot 1" in (order.texts[-1] or "")


def test_new_spool_confirm_acknowledges_before_the_bind(tmp_path, monkeypatch):
    """The #483 prompt's Confirm load binds through spool_sync.bind_slot;
    same 3 s rule as the walkthrough confirmation (P2-191)."""
    from tests.hermes.test_stop_button_view import _install
    from print_handy.hermes import print_alert_buttons as pab, spool_sync
    monkeypatch.setattr(pab, "STATE", tmp_path / "alerts.json")
    _sent, on_interaction, plugin = _install(monkeypatch)
    order = Order()

    def slow_bind(slot, spool_id, **kw):
        order.append("slow")
        return {"slot": slot, "spool_id": spool_id, "prior_spool_id": None,
                "record": {"name": "Matte White PLA+", "remaining_grams": 1000.0},
                "screen": "pushed", "usage": "unchanged", "prior_disposition": "shelf"}
    monkeypatch.setattr(spool_sync, "bind_slot", slow_bind)
    aid = pab.record("spoolman-new-confirm", "", spool_id=20, label="#20 Matte White",
                     slot=1, disposition="shelf", old_spool_id=None)
    click = _bind_click(tmp_path, monkeypatch, order, f"{pab.NEW_SPOOL_PREFIX}:confirm:{aid}")
    asyncio.run(on_interaction(click))
    assert order[:2] == ["ack", "slow"], order
    assert order[-1] == "edit"


def _spoolman_rig(tmp_path, monkeypatch, order):
    """Plugin handlers plus a Spoolman whose every call records "slow"."""
    import importlib.util
    from pathlib import Path
    from print_handy.hermes import print_alert_buttons as pab, spool_sync
    monkeypatch.setattr(pab, "STATE", tmp_path / "alerts.json")
    # Real discord.ui (the pickers build Select/View); the shared _install fake has none.
    spec = importlib.util.spec_from_file_location(
        "pf_ack_order", Path(__file__).parents[2] / "plugins/print-feedback/__init__.py")
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    listeners = {}
    plugin.alert_button_handlers(S(add_listener=lambda fn, name: listeners.__setitem__(name, fn)), S())
    on_interaction = listeners["on_interaction"]

    def slow(value):
        def call(*a, **k):
            order.append("slow")
            return value
        return call
    state = {"filament_slots": {"2": {"spoolman_id": 12, "name": "Red PETG", "remaining_grams": 90.0}}}
    spools = [{"id": 7, "remaining_weight": 400.0, "filament": {"name": "Matte White", "material": "PLA+"}}]
    monkeypatch.setattr(spool_sync, "load_state", slow(state))
    monkeypatch.setattr(spool_sync, "spoolman", slow(spools))
    monkeypatch.setattr(spool_sync, "next_spool_id", slow(20))
    monkeypatch.setattr(spool_sync, "filament_types", slow([]))
    return on_interaction, pab


@pytest.mark.parametrize("make_id", [
    lambda pab: f"pbinsert:existing:{pab.record('insertion', '', slot=2)}",
    lambda pab: f"pbinsert:new:{pab.record('insertion', '', slot=2)}",
    lambda pab: f"pbspool:existing:{pab.record('spool-question', '', context={'slot': 2})}",
    lambda pab: f"pbspool:new:{pab.record('spool-question', '', context={'slot': 2})}",
], ids=["insertion-existing", "insertion-new", "spool-question-existing", "spool-question-new"])
def test_spool_pickers_acknowledge_before_spoolman_reads(tmp_path, monkeypatch, make_id):
    """Opening a shelf picker or the new-spool walkthrough reads Spoolman first;
    the interaction must be deferred before any of those reads (3 s window)."""
    order = Order()
    on_interaction, pab = _spoolman_rig(tmp_path, monkeypatch, order)
    asyncio.run(on_interaction(_bind_click(tmp_path, monkeypatch, order, make_id(pab))))
    assert "slow" in order and order[0] == "ack", order
    assert order.count("ack") == 1, order


def test_picker_selection_acknowledges_before_the_bind_confirmation_reread(tmp_path, monkeypatch):
    """Choosing a spool re-reads it from Spoolman for the confirmation text."""
    order = Order()
    on_interaction, pab = _spoolman_rig(tmp_path, monkeypatch, order)
    aid = pab.record("insertion-select", "", slot=2)
    click = _bind_click(tmp_path, monkeypatch, order, f"pbinsertselect:{aid}")
    click.data["values"] = ["7"]
    asyncio.run(on_interaction(click))
    assert "slow" in order and order[0] == "ack", order
    assert order[-1] == "edit" and "Bind spool" in (order.texts[-1] or ""), (order, order.texts)
