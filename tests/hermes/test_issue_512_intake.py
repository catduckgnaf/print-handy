"""Conversational new-spool intake walkthrough (#512).

The data layer in print_alert_buttons replaces the five-field text modal:
material / brand (opt-in only) / colour / weight / replaced-spool
disposition / confirm, with the new spool's number previewed at the first
step. These tests pin the step machine, the component shapes, the 0 g
disposition recommendation, and the new bind_slot 'delete' disposition.
"""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy.hermes import print_alert_buttons as pab
from print_handy.hermes import spool_sync

SPEC = importlib.util.spec_from_file_location(
    "pf_issue_512", Path(__file__).parents[2] / "plugins/print-feedback/__init__.py")
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


def test_intake_parse_and_action_maps():
    assert pab.parse_intake_custom_id("pbintake:mat_1:deadbeef") == ("mat_1", "deadbeef")
    assert pab.parse_intake_custom_id("pbintake:confirm:deadbeef") == ("confirm", "deadbeef")
    assert pab.parse_intake_custom_id("pbintake:explode:deadbeef") is None
    assert pab.parse_intake_custom_id("pbinsert:new:deadbeef") is None
    assert pab.intake_material_for_action("mat_1") == "Silk PLA"
    assert pab.intake_material_for_action("mat_0") == "PLA"
    assert pab.intake_weight_for_action("w_500") == 500
    assert pab.intake_disposition_for_action("disp_delete") == "delete"


def test_intake_material_components_cover_issue_quick_select():
    labels = [b["label"] for row in pab.intake_material_components("aid") for b in row["components"]]
    for wanted in ("PLA", "Silk PLA", "PLA+", "PETG", "TPU", "ABS"):
        assert wanted in labels
    assert labels[-1] == "Cancel"


def test_intake_preview_names_the_spool_number_immediately():
    text = pab.intake_preview_text(18, 2)
    assert "Spool #18" in text and "Slot 2" in text
    state = {"step": "material", "slot": 2, "predicted_id": 18}
    assert pab.intake_step_text(state).startswith("Preparing new **Spool #18**")


def test_intake_step_order_brand_off_by_default():
    state = {"step": "start", "origin": "insertion", "old_spool_id": 13, "prompt_brand": False}
    order = []
    while state["step"] != "confirm":
        state = {**state, "step": pab.intake_next_step(state)}
        order.append(state["step"])
    assert order == ["material", "color", "weight", "disposition", "confirm"]


def test_intake_step_order_brand_on_and_empty_slot_skips_disposition():
    state = {"step": "start", "origin": "insertion", "old_spool_id": None, "prompt_brand": True}
    order = []
    while state["step"] != "confirm":
        state = {**state, "step": pab.intake_next_step(state)}
        order.append(state["step"])
    assert order == ["material", "brand", "color", "weight", "confirm"]


def test_intake_spool_question_origin_never_asks_disposition():
    state = {"step": "weight", "origin": "spool-question", "old_spool_id": 13}
    assert pab.intake_next_step(state) == "confirm"


def test_intake_disposition_components_recommend_depleted_at_zero_grams():
    labels = [b["label"] for b in pab.intake_disposition_components("aid", 0.0)[0]["components"]]
    assert labels[0] == "Archive as depleted (recommended)"
    assert "Delete" in labels and "Move to shelf" in labels
    text = pab.intake_step_text({"step": "disposition", "slot": 1, "old_spool_id": 13, "old_remaining": 0.0})
    assert "#13" in text and "recommended" in text


def test_intake_disposition_components_default_order_has_shelf_first():
    labels = [b["label"] for b in pab.intake_disposition_components("aid", 250.0)[0]["components"]]
    assert labels[0] == "Move to shelf"
    assert labels[-2] == "Delete" and labels[-1] == "Cancel"


def test_intake_color_options_known_first_then_common_then_other():
    types = [{"brand": "Sunlu", "material": "Silk PLA", "color": "Rose Red Blue Green", "hex": None},
             {"brand": "Sunlu", "material": "PETG", "color": "Teal", "hex": None}]
    options = pab.intake_color_options(types, "Silk PLA")
    assert options[0]["value"] == "Rose Red Blue Green"
    assert options[-1]["value"] == pab.INTAKE_OTHER
    assert "Teal" not in [o["value"] for o in options]


def test_intake_brand_options_known_first_with_skip_and_type():
    types = [{"brand": "Sunlu", "material": "PLA", "color": "Black", "hex": None},
             {"brand": "eSun", "material": "PETG", "color": "Teal", "hex": None}]
    options = pab.intake_brand_options(types, "PLA")
    assert options[0]["value"] == "Sunlu"
    assert options[-2]["value"] == pab.INTAKE_OTHER
    assert options[-1]["value"] == pab.INTAKE_SKIP


def test_intake_confirm_text_names_fate_and_delete_is_loud():
    state = {"step": "confirm", "slot": 1, "predicted_id": 18, "brand": None,
             "material": "Silk PLA", "color": "Rose Red", "grams": 500,
             "old_spool_id": 13, "disposition": "delete"}
    text = pab.intake_step_text(state)
    assert "Spool #18" in text and "DELETED" in text and "Slot 1" in text


def test_create_and_bind_insertion_passes_disposition_and_names_it():
    calls = []

    class Sync:
        @staticmethod
        def parse_new(text):
            return {"material": "PLA", "color": "Black", "grams": 1000}

        @staticmethod
        def create_spool(spec):
            return {"spool_id": 18}

        @staticmethod
        def bind_slot(slot, spool_id, **kwargs):
            calls.append(kwargs)
            return {"record": {"name": "Black PLA"}, "prior_spool_id": 13}

    out = pab.create_and_bind_insertion(1, {"brand": "", "material": "PLA", "color": "Black",
                                            "grams": "1000", "lot": ""}, Sync,
                                        prior_disposition="depleted")
    assert calls[0]["prior_disposition"] == "depleted"
    assert "Spool #13 was archived as depleted" in out


def _bind_harness(tmp_path):
    state = tmp_path / "hw.json"
    state.write_text(json.dumps({
        "spool": {"spoolman_id": 3, "name": "Pink PLA", "material": "PLA", "density": 1.24, "diameter_mm": 1.75},
        "filament_slots": {"2": {"spoolman_id": 3, "material": "PLA", "color": "pink"}}}))
    spools = {
        3: {"id": 3, "location": "IFS Slot 2", "filament": {"name": "Pink PLA", "material": "PLA", "color_hex": "F95D73"}},
        7: {"id": 7, "location": "Shelf", "filament": {"name": "Coffee PLA+", "material": "PLA+", "color_hex": "6F4E37"}},
    }
    calls = []

    def client(method, path, body=None):
        calls.append((method, path, body))
        if method == "GET":
            return spools[int(path.rsplit("/", 1)[1])]
        return {}

    def printer(method, path, body=None):
        return {"result": {"spool_id": None}}

    return state, client, printer, calls


def test_bind_slot_delete_disposition_removes_prior_record(tmp_path):
    state, client, printer, calls = _bind_harness(tmp_path)
    result = spool_sync.bind_slot(2, 7, client=client, mr=printer, state_path=state,
                                  push_screen_update=False, prior_disposition="delete")
    assert ("DELETE", "/api/v1/spool/3", None) in calls
    assert not any(c[0] == "PATCH" and c[1] == "/api/v1/spool/3" for c in calls)
    assert "deleted from Spoolman" in spool_sync.bind_text(result)


def test_new_spool_prompt_parser_accepts_delete():
    assert pab.parse_new_spool_custom_id("pbnewspool:delete:deadbeef") == ("delete", "deadbeef")


# --------------------------------------- #515 review fixes: identity + claim

def test_create_and_bind_insertion_passes_expected_prior_spool():
    calls = []

    class Sync:
        @staticmethod
        def parse_new(text):
            return {"material": "PLA", "color": "Black", "grams": 1000}

        @staticmethod
        def load_state():
            return {"filament_slots": {"1": {"spoolman_id": 13}}}

        @staticmethod
        def create_spool(spec):
            return {"spool_id": 18}

        @staticmethod
        def bind_slot(slot, spool_id, **kwargs):
            calls.append(kwargs)
            return {"record": {"name": "Black PLA"}, "prior_spool_id": 13}

    pab.create_and_bind_insertion(1, {"brand": "", "material": "PLA", "color": "Black",
                                      "grams": "1000", "lot": ""}, Sync,
                                  prior_disposition="delete", expected_prior_spool_id=13)
    assert calls[0]["expected_prior_spool_id"] == 13


def test_bind_slot_delete_refuses_when_slot_mapping_changed(tmp_path):
    """The confirmation named spool #99; slot 2 now holds #3. Abort, no writes."""
    state, client, printer, calls = _bind_harness(tmp_path)
    with pytest.raises(spool_sync.SpoolError) as excinfo:
        spool_sync.bind_slot(2, 7, client=client, mr=printer, state_path=state,
                             push_screen_update=False, prior_disposition="delete",
                             expected_prior_spool_id=99)
    message = str(excinfo.value)
    assert "#99" in message and "#3" in message
    assert not any(c[0] in ("PATCH", "DELETE") for c in calls)


def test_bind_slot_mismatch_guard_covers_every_disposition(tmp_path):
    state, client, printer, calls = _bind_harness(tmp_path)
    with pytest.raises(spool_sync.SpoolError):
        spool_sync.bind_slot(2, 7, client=client, mr=printer, state_path=state,
                             push_screen_update=False, prior_disposition="archive",
                             expected_prior_spool_id=99)
    assert not any(c[0] in ("PATCH", "DELETE") for c in calls)


def test_bind_slot_delete_with_matching_expected_id_deletes_that_spool(tmp_path):
    state, client, printer, calls = _bind_harness(tmp_path)
    result = spool_sync.bind_slot(2, 7, client=client, mr=printer, state_path=state,
                                  push_screen_update=False, prior_disposition="delete",
                                  expected_prior_spool_id=3)
    assert ("DELETE", "/api/v1/spool/3", None) in calls
    assert not any(c[0] == "PATCH" and c[1] == "/api/v1/spool/3" for c in calls)
    assert result["prior_spool_id"] == 3


# ------------------------------------------------- end-to-end click walkthrough

class _FakeResponse:
    def __init__(self, sink):
        self.sink = sink

    async def send_message(self, content=None, view=None, ephemeral=False, **kw):
        self.sink.append(("send", content, view))

    async def edit_message(self, content=None, view=None, **kw):
        self.sink.append(("edit", content, view))

    async def defer(self, **kw):
        self.sink.append(("defer", None, None))

    async def send_modal(self, modal):
        self.sink.append(("modal", modal))


class _FakeMessage:
    async def edit(self, view=None, **kw):
        return None


def _interaction(sink, custom_id, values=None):
    interaction = S(user=S(id="42", bot=False, display_name="catduck"),
                    data={"custom_id": custom_id, "values": values or []},
                    response=_FakeResponse(sink), message=_FakeMessage(),
                    channel_id="99", guild=None)

    async def edit_original_response(content=None, view=None, **kw):
        sink.append(("edit", content, view))

    async def followup_send(content, **kw):
        sink.append(("send", content, None))

    interaction.edit_original_response = edit_original_response
    interaction.followup = S(send=followup_send)
    return interaction


def _custom_ids(view):
    return [item.custom_id for item in (view.children if view else [])]


def test_walkthrough_clicks_create_and_bind_with_disposition(tmp_path, monkeypatch):
    monkeypatch.setattr(pab, "STATE", tmp_path / "alerts.json")
    monkeypatch.setattr(plugin, "_prompt_brand", lambda: False)
    monkeypatch.setattr(spool_sync, "next_spool_id", lambda: 18)
    monkeypatch.setattr(spool_sync, "filament_types", lambda: [
        {"filament_id": 5, "brand": "Sunlu", "material": "Silk PLA",
         "color": "Rose Red Blue Green", "hex": None}])
    monkeypatch.setattr(spool_sync, "load_state", lambda: {
        "filament_slots": {"2": {"spoolman_id": 13, "name": "Matte White PLA+", "remaining_grams": 0.0}}})
    monkeypatch.setattr(spool_sync, "create_spool", lambda spec: {"spool_id": 18})
    binds = []

    def fake_bind(slot, spool_id, **kwargs):
        binds.append((slot, spool_id, kwargs))
        return {"record": {"name": "Rose Red Blue Green Silk PLA"}, "prior_spool_id": 13}

    monkeypatch.setattr(spool_sync, "bind_slot", fake_bind)

    bot = S(listeners={}, add_listener=lambda fn, name: bot.listeners.__setitem__(name, fn))
    plugin.alert_button_handlers(bot, S())
    on_interaction = bot.listeners["on_interaction"]

    sink = []
    insertion_aid = pab.record("insertion", "", slot=2)
    asyncio.run(on_interaction(_interaction(sink, f"pbinsert:new:{insertion_aid}")))
    kind, content, view = sink[-1]
    assert "Spool #18" in content and "write #18 on the spool" in content
    mat = next(cid for cid in _custom_ids(view) if ":mat_1:" in cid)

    sink.clear()
    asyncio.run(on_interaction(_interaction(sink, mat)))
    kind, content, view = sink[-1]
    select_id = _custom_ids(view)[0]
    assert select_id.startswith("pbintakeselect:")

    sink.clear()
    asyncio.run(on_interaction(_interaction(sink, select_id, values=["Rose Red Blue Green"])))
    kind, content, view = sink[-1]
    weight = next(cid for cid in _custom_ids(view) if ":w_500:" in cid)

    sink.clear()
    asyncio.run(on_interaction(_interaction(sink, weight)))
    kind, content, view = sink[-1]
    assert "#13" in content and "recommended" in content
    depleted = next(cid for cid in _custom_ids(view) if ":disp_depleted:" in cid)

    sink.clear()
    asyncio.run(on_interaction(_interaction(sink, depleted)))
    kind, content, view = sink[-1]
    assert "Create" in content and "archived as depleted" in content
    confirm = next(cid for cid in _custom_ids(view) if ":confirm:" in cid)

    sink.clear()
    asyncio.run(on_interaction(_interaction(sink, confirm)))
    kind, content, view = sink[-1]
    assert "Created spool #18" in content and "archived as depleted" in content
    assert binds and binds[0][2].get("prior_disposition") == "depleted"
    assert binds[0][2].get("expected_prior_spool_id") == 13


def _walk_to_confirm(tmp_path, monkeypatch):
    """Drive the click walkthrough to its confirmation step; return the rig."""
    monkeypatch.setattr(pab, "STATE", tmp_path / "alerts.json")
    monkeypatch.setattr(plugin, "_prompt_brand", lambda: False)
    monkeypatch.setattr(spool_sync, "next_spool_id", lambda: 18)
    monkeypatch.setattr(spool_sync, "filament_types", lambda: [
        {"filament_id": 5, "brand": "Sunlu", "material": "Silk PLA",
         "color": "Rose Red Blue Green", "hex": None}])
    monkeypatch.setattr(spool_sync, "load_state", lambda: {
        "filament_slots": {"2": {"spoolman_id": 13, "name": "Matte White PLA+", "remaining_grams": 0.0}}})
    created = []

    def fake_create(spec):
        created.append(spec)
        return {"spool_id": 18}

    monkeypatch.setattr(spool_sync, "create_spool", fake_create)
    binds = []

    def fake_bind(slot, spool_id, **kwargs):
        binds.append((slot, spool_id, kwargs))
        return {"record": {"name": "Rose Red Blue Green Silk PLA"}, "prior_spool_id": 13}

    monkeypatch.setattr(spool_sync, "bind_slot", fake_bind)
    bot = S(listeners={}, add_listener=lambda fn, name: bot.listeners.__setitem__(name, fn))
    plugin.alert_button_handlers(bot, S())
    on_interaction = bot.listeners["on_interaction"]
    sink = []
    insertion_aid = pab.record("insertion", "", slot=2)
    asyncio.run(on_interaction(_interaction(sink, f"pbinsert:new:{insertion_aid}")))
    mat = next(cid for cid in _custom_ids(sink[-1][2]) if ":mat_1:" in cid)
    asyncio.run(on_interaction(_interaction(sink, mat)))
    select_id = _custom_ids(sink[-1][2])[0]
    asyncio.run(on_interaction(_interaction(sink, select_id, values=["Rose Red Blue Green"])))
    weight = next(cid for cid in _custom_ids(sink[-1][2]) if ":w_500:" in cid)
    asyncio.run(on_interaction(_interaction(sink, weight)))
    depleted = next(cid for cid in _custom_ids(sink[-1][2]) if ":disp_depleted:" in cid)
    asyncio.run(on_interaction(_interaction(sink, depleted)))
    confirm = next(cid for cid in _custom_ids(sink[-1][2]) if ":confirm:" in cid)
    return on_interaction, sink, confirm, created, binds


def test_walkthrough_second_confirm_click_creates_and_binds_nothing(tmp_path, monkeypatch):
    on_interaction, sink, confirm, created, binds = _walk_to_confirm(tmp_path, monkeypatch)
    asyncio.run(on_interaction(_interaction(sink, confirm)))
    assert len(created) == 1 and len(binds) == 1
    sink.clear()
    asyncio.run(on_interaction(_interaction(sink, confirm)))
    kind, content, view = sink[-1]
    assert kind == "send" and "already answered" in content
    assert len(created) == 1 and len(binds) == 1


def test_walkthrough_confirm_that_loses_the_claim_race_creates_nothing(tmp_path, monkeypatch):
    """Both clicks peeked the same alert; the loser's take() returns None.

    Regression for #515 finding 1: the confirm path used to ignore take()'s
    result and run intake_finish anyway, creating and binding a second spool.
    """
    on_interaction, sink, confirm, created, binds = _walk_to_confirm(tmp_path, monkeypatch)
    monkeypatch.setattr(pab, "take", lambda aid: None)  # a parallel click claimed it first
    sink.clear()
    asyncio.run(on_interaction(_interaction(sink, confirm)))
    kind, content, view = sink[-1]
    assert kind == "send" and "already answered" in content
    assert created == [] and binds == []


def test_edit_details_clears_photo_extracted_temperatures(tmp_path, monkeypatch):
    """Edit details switches to manual entry, which has no temperature
    step: the photo-extracted nozzle/bed temperatures must be dropped, not
    silently saved onto the new filament over its material defaults (P2-198)."""
    monkeypatch.setattr(pab, "STATE", tmp_path / "alerts.json")
    monkeypatch.setattr(plugin, "_prompt_brand", lambda: False)
    bot = S(listeners={}, add_listener=lambda fn, name: bot.listeners.__setitem__(name, fn))
    plugin.alert_button_handlers(bot, S())
    on_interaction = bot.listeners["on_interaction"]
    aid = pab.record("intake", "", origin="insertion", slot=2, step="confirm",
                     brand="Sunlu", material="Silk PLA", color="Rose Red", grams=500,
                     from_photo=True, photo_route="vision", nozzle_temp=215, bed_temp=65,
                     disposition="shelf", old_spool_id=None)
    sink = []
    asyncio.run(on_interaction(_interaction(sink, f"pbintake:edit:{aid}")))
    rows = [r for r in pab._load().values() if isinstance(r, dict) and r.get("kind") == "intake"]
    assert rows, "the re-entered walkthrough step was not recorded"
    latest = max(rows, key=lambda r: r.get("at", 0))
    assert latest["nozzle_temp"] is None and latest["bed_temp"] is None


def test_typed_color_in_the_modal_clears_the_picked_swatch(tmp_path, monkeypatch):
    """Typing a color in the edit modal supersedes a swatch picked earlier:
    the stale hex must be cleared, or create_spool would record the old
    swatch for the new color name (P2-233). The select path's color branch
    already clears it; the modal path must match."""
    monkeypatch.setattr(pab, "STATE", tmp_path / "alerts.json")
    monkeypatch.setattr(plugin, "_prompt_brand", lambda: False)
    bot = S(listeners={}, add_listener=lambda fn, name: bot.listeners.__setitem__(name, fn))
    plugin.alert_button_handlers(bot, S())
    on_interaction = bot.listeners["on_interaction"]
    aid = pab.record("intake", "", origin="insertion", slot=2, step="color",
                     brand="Sunlu", material="PLA", color="Red", hex="#FF0000",
                     grams=1000, disposition="shelf", old_spool_id=None)
    sink = []
    click = _interaction(sink, f"{pab.INTAKE_SELECT_PREFIX}:{aid}", values=[pab.INTAKE_OTHER])
    asyncio.run(on_interaction(click))
    modal = next(entry[1] for entry in sink if entry[0] == "modal")
    modal.item._value = "Teal"
    asyncio.run(modal.on_submit(S(response=_FakeResponse(sink))))
    rows = [r for r in pab._load().values() if isinstance(r, dict) and r.get("kind") == "intake"]
    latest = max(rows, key=lambda r: r.get("at", 0))
    assert latest["color"] == "Teal"
    assert latest["hex"] is None
