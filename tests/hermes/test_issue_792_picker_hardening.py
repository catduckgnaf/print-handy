"""Issue #792 part 2: normalized picker labels, parsed-color sanity, and
bind confirmations that name the exact spool.

Part 1 (plain-language intent) and part 3 (US spelling) shipped in #796;
this file pins only the part 2 surfaces: the one picker label shape
(color · material · #id · grams left, unknowns said in place), the
confirm-or-correct step for a color that parses to multiple colors or a
fragment, and the Discord bind confirmation that stands between a spool
picker selection and any bind.
"""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy.core import filament_color
from print_handy.runtime import print_alert_buttons as pab
from print_handy.runtime import spool_sync
from print_handy.runtime.discord_print_summary import spool_picker_label

SPEC = importlib.util.spec_from_file_location(
    "pf_issue_792", Path(__file__).parents[2] / "plugins/print-feedback/__init__.py")
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


# ---------------------------------------------------------------- label shape

@pytest.mark.parametrize("name,material,spool_id,grams,expected", [
    ("Coffee PLA+", "PLA+", 21, 812.0, "Coffee · PLA+ · #21 · 812 g left"),
    ("Matte White", "PLA+", 7, 400.0, "Matte White · PLA+ · #7 · 400 g left"),
    ("Black", "PLA", 9, None, "Black · PLA · #9 · grams unknown"),
    (None, None, 5, None, "color unknown · material unknown · #5 · grams unknown"),
    ("PLA", "PLA", 3, 250.0, "color unknown · PLA · #3 · 250 g left"),
    ("Spool #7", "", 7, 10.0, "color unknown · material unknown · #7 · 10 g left"),
])
def test_spool_picker_label_shape(name, material, spool_id, grams, expected):
    assert spool_picker_label(name, material, spool_id, grams) == expected


def test_insertion_options_use_the_picker_shape_and_say_unknown_grams():
    options = pab.insertion_select_options([
        {"id": 18, "remaining_weight": None,
         "filament": {"name": "Rose Red", "material": "Silk PLA"}},
        {"id": 19, "remaining_weight": 812.0,
         "filament": {"name": "Black", "material": "PLA"}},
    ])
    labels = {o["value"]: o["label"] for o in options}
    assert labels["18"] == "Rose Red · Silk PLA · #18 · grams unknown"
    assert labels["19"] == "Black · PLA · #19 · 812 g left"


def test_color_picker_options_all_carry_color_and_material():
    options = pab.intake_color_options(
        [{"brand": "Sunlu", "material": "PLA", "color": "Coffee", "hex": None}], "PLA")
    by_value = {o["value"]: o["label"] for o in options}
    assert by_value["Coffee"] == "Coffee · PLA"      # known for this material
    assert by_value["Black"] == "Black · PLA"        # common color, same shape
    assert by_value[pab.INTAKE_OTHER] == "Other (type it exactly)…"


# ---------------------------------------------------------------- color sanity

@pytest.mark.parametrize("color,material", [
    ("Matte White", "PLA+"),
    ("Black", "PLA"),
    ("Rose Red", "Silk PLA"),       # one compound color, not several
    ("Galaxy", "PLA"),              # unreadable name: the swatch step's job, not sanity
    ("Nebula", "PLA"),
])
def test_color_sanity_leaves_single_colors_alone(color, material):
    assert filament_color.color_sanity(color, material) is None
    assert filament_color.color_sanity_text(color, material) is None


@pytest.mark.parametrize("color,material,fragments", [
    ("Rose red blue green", "PLA", ("rose red", "blue", "green")),
    ("Rose Red Blue Green", "Silk PLA", ("rose red", "blue", "green")),
    ("term NoPLA+ Natural", "PLA+", ("natural",)),
    ("term No...", "PLA+", ()),
])
def test_color_sanity_flags_multi_and_fragment_parses(color, material, fragments):
    sanity = filament_color.color_sanity(color, material)
    assert sanity is not None
    for fragment in fragments:
        assert fragment in sanity["understood"]
    text = filament_color.color_sanity_text(color, material)
    assert text.startswith(f"I read the color as “{color}”")


def test_color_sanity_names_the_unknown_fragment_words():
    sanity = filament_color.color_sanity("term NoPLA+ Natural", "PLA+")
    assert sanity["fragment"] and set(sanity["unknown"]) == {"term", "nopla"}
    text = filament_color.color_sanity_text("term NoPLA+ Natural", "PLA+")
    assert "term" in text and "nopla" in text and "Natural" in text


def test_intake_colorcheck_step_gates_a_multi_color_parse():
    state = {"step": "color", "material": "Silk PLA", "color": "Rose Red Blue Green",
             "grams": None, "origin": "insertion", "old_spool_id": None}
    assert pab.intake_next_step(state) == "colorcheck"
    text = pab.intake_step_text({**state, "step": "colorcheck"})
    assert "more than one color" in text and "Rose Red Blue Green" in text
    labels = [b["label"] for row in pab.intake_step_components("aid", {**state, "step": "colorcheck"})
              for b in row["components"]]
    assert labels == ["That is exactly as labeled", "Type the color again", "Cancel"]
    # Once confirmed, the same color is never asked about twice.
    assert pab.intake_next_step({**state, "color_confirmed": True}) == "weight"


def test_intake_clean_color_skips_the_colorcheck():
    state = {"step": "color", "material": "PLA", "color": "Black",
             "grams": None, "origin": "insertion", "old_spool_id": None}
    assert pab.intake_next_step(state) == "weight"


def test_bind_confirm_components_parse():
    components = pab.bind_confirm_components("deadbeef")
    ids = [b["custom_id"] for b in components[0]["components"]]
    assert pab.parse_bind_confirm_custom_id(ids[0]) == ("confirm", "deadbeef")
    assert pab.parse_bind_confirm_custom_id(ids[1]) == ("cancel", "deadbeef")
    assert pab.parse_bind_confirm_custom_id("pbbindconfirm:explode:deadbeef") is None
    assert pab.parse_bind_confirm_custom_id("pbintake:confirm:deadbeef") is None


# ------------------------------------------------- spool-question minting gate

def test_spool_question_text_answer_with_garbled_color_mints_nothing():
    created = []
    sync = S(SpoolError=spool_sync.SpoolError,
             load_state=lambda: {"filament_slots": {}},
             create_spool=lambda spec: created.append(spec) or {"spool_id": 30},
             screen_hex=spool_sync.screen_hex)
    listener = S(load_runout_pause=lambda: {"slot": 2, "prior_spool_id": 8, "cause": "swap"},
                 clear_runout_pause=mock.Mock())
    with pytest.raises(spool_sync.SpoolError) as excinfo:
        plugin._answer_spool_question(
            listener, sync, {"slot": 2, "prior_spool_id": 8, "cause": "swap"},
            {"status": "new", "brand": None, "material": "PLA",
             "color": "Rose Red Blue Green", "grams": 1000})
    assert "more than one color" in str(excinfo.value)
    assert getattr(excinfo.value, "color_sanity", None) is True
    assert created == []


# ------------------------------------------- Discord: selection != bind, confirm binds

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


def _insertion_rig(tmp_path, monkeypatch):
    monkeypatch.setattr(pab, "STATE", tmp_path / "alerts.json")
    monkeypatch.setattr(spool_sync, "load_state", lambda: {
        "filament_slots": {"2": {"spoolman_id": 12, "name": "Red PETG",
                                 "color": "red", "material": "PETG", "remaining_grams": 90.0}}})
    spools = [{"id": 7, "remaining_weight": 400.0,
               "filament": {"name": "Matte White", "material": "PLA+"}}]

    def fake_spoolman(method, path, body=None):
        if path.startswith("/api/v1/spool/"):
            return dict(spools[0])
        return [dict(s) for s in spools]

    monkeypatch.setattr(spool_sync, "spoolman", fake_spoolman)
    binds = []
    monkeypatch.setattr(pab, "bind_insertion_existing",
                        lambda slot, spool_id, sync, **kw: binds.append(
                            (slot, spool_id, kw.get("expected_prior_spool_id")))
                        or f"Slot {slot} → spool #{spool_id}. Inventory updated; printer untouched.")
    bot = S(listeners={}, add_listener=lambda fn, name: bot.listeners.__setitem__(name, fn))
    plugin.alert_button_handlers(bot, S())
    return bot.listeners["on_interaction"], binds


def _drive_to_bind_confirm(on_interaction, sink):
    insertion_aid = pab.record("insertion", "", slot=2)
    asyncio.run(on_interaction(_interaction(sink, f"pbinsert:existing:{insertion_aid}")))
    select_id = next(cid for cid in _custom_ids(sink[-1][2]) if cid.startswith("pbinsertselect:"))
    sink.clear()
    asyncio.run(on_interaction(_interaction(sink, select_id, values=["7"])))
    return sink[-1]


def test_insertion_select_names_the_exact_spool_and_binds_only_on_confirm(tmp_path, monkeypatch):
    on_interaction, binds = _insertion_rig(tmp_path, monkeypatch)
    sink = []
    kind, content, view = _drive_to_bind_confirm(on_interaction, sink)
    assert "Bind spool Matte White · PLA+ · #7 · 400 g left to slot 2?" in content
    assert "Spool #12 goes back on the shelf." in content  # shelf by default, named
    assert "archived" not in content
    assert binds == []  # the picker selection alone bound nothing
    confirm = next(cid for cid in _custom_ids(view) if cid.startswith("pbbindconfirm:confirm:"))
    sink.clear()
    asyncio.run(on_interaction(_interaction(sink, confirm)))
    # The confirmation named spool #12 as displaced, so the bind carries it
    # as the expected prior: a rebind since then would refuse, not shelf #12.
    assert binds == [(2, 7, 12)]
    assert "Slot 2 → spool #7" in sink[-1][1]


def test_insertion_bind_confirm_cancel_binds_nothing(tmp_path, monkeypatch):
    on_interaction, binds = _insertion_rig(tmp_path, monkeypatch)
    sink = []
    kind, content, view = _drive_to_bind_confirm(on_interaction, sink)
    cancel = next(cid for cid in _custom_ids(view) if cid.startswith("pbbindconfirm:cancel:"))
    sink.clear()
    asyncio.run(on_interaction(_interaction(sink, cancel)))
    assert sink[-1][1] == "Bind cancelled. Nothing changed."
    assert binds == []


def test_bind_insertion_existing_passes_the_expected_prior_to_bind_slot():
    calls = []

    class FakeSync:
        @staticmethod
        def bind_slot(slot, spool_id, **kw):
            calls.append((slot, spool_id, kw))
            return {"record": {"name": "Matte White PLA+", "remaining_grams": 400.0}}

    text = pab.bind_insertion_existing(2, 7, FakeSync, expected_prior_spool_id=12)
    assert calls[0][2]["expected_prior_spool_id"] == 12
    assert calls[0][2]["push_screen_update"] is False
    assert "spool #7" in text


def test_bind_insertion_existing_stale_confirmation_is_refused_by_bind_slot(tmp_path):
    # The confirmation named spool #12 as the slot's spool; the slot was
    # rebound to #9 meanwhile, so the guard refuses and #9 is not shelved.
    state = {"spool": {}, "filament_slots": {"2": {"spoolman_id": 9, "name": "Black PLA",
                                                   "material": "PLA", "color": "black",
                                                   "hex": "161616"}}}
    path = tmp_path / "printer_hardware.json"
    path.write_text(json.dumps(state))
    spools = {7: {"id": 7, "location": "Shelf", "archived": False, "remaining_weight": 400.0,
                  "filament": {"name": "Matte White", "material": "PLA+", "color_hex": "FFFFFF"}},
              9: {"id": 9, "location": "IFS Slot 2", "archived": False, "remaining_weight": 90.0,
                  "filament": {"name": "Black", "material": "PLA", "color_hex": "161616"}}}

    def client(method, path_, body=None):
        if method == "GET" and path_.startswith("/api/v1/spool/"):
            return dict(spools[int(path_.rsplit("/", 1)[1])])
        return {}

    with pytest.raises(spool_sync.SpoolError, match="was expected to hold spool #12"):
        _bind_with_client(client, path)
    saved = json.loads(path.read_text())
    assert saved["filament_slots"]["2"]["spoolman_id"] == 9


def _bind_with_client(client, path):
    return spool_sync.bind_slot(2, 7, client=client, mr=lambda m, p, b=None: {"result": "ok"},
                                state_path=path, expected_prior_spool_id=12)
