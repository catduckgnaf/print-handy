"""Guided /spool with buttons + deterministic natural-language routing (#475).

The dispatch harness follows test_print_stop.py: a FakeAdapter records the
clarify questions, and the clarify gateway's wait_for_response is patched
with a queue of answers (button labels or typed replies). The Spoolman side
is a fake client behind spool_sync's own functions, with a write log so
cancel tests can prove zero Spoolman writes happened.
"""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("pf_spool_guided", HERE.parents[1] / "plugins/print-feedback/__init__.py")
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)

from print_handy.hermes import spool_sync


# ---------------------------------------------------------------- fake Spoolman

FILAMENTS = [
    {"id": 3, "name": "Matte White", "material": "PLA+", "color_hex": "FFFFFF",
     "vendor": {"name": "Sunlu"}},
    {"id": 4, "name": "Black", "material": "PLA", "color_hex": "000000",
     "vendor": {"name": "eSun"}},
    {"id": 3, "name": "Matte White", "material": "PLA+", "color_hex": "FFFFFF",
     "vendor": {"name": "Sunlu"}},  # duplicate row: dedupe target
]
SPOOLS = [
    {"id": 7, "remaining_weight": 400.0, "location": "Shelf",
     "filament": {"name": "Matte White", "material": "PLA+"}},
    {"id": 9, "remaining_weight": 800.0, "location": "Shelf",
     "filament": {"name": "Black", "material": "PLA"}},
    {"id": 12, "remaining_weight": 1000.0, "location": "IFS Slot 2",
     "filament": {"name": "Red", "material": "PETG"}},
]
STATE = {"filament_slots": {"2": {"spoolman_id": 12, "name": "Red PETG"}}}


class FakeSpoolman:
    """The spoolman callable shape (method, path, body) with a write log."""

    def __init__(self):
        self.writes = []
        self.next_id = 20

    def __call__(self, method, path, body=None):
        if method == "GET" and path == "/api/v1/filament":
            return [dict(f) for f in FILAMENTS]
        if method == "GET" and path == "/api/v1/spool?allow_archived=true":
            return [{"id": s["id"]} for s in SPOOLS] + [{"id": 19, "archived": True}]
        if method == "GET" and path == "/api/v1/spool?allow_archived=false":
            return [dict(s) for s in SPOOLS]
        if method == "GET" and path == "/api/v1/vendor":
            return [{"id": 1, "name": "Sunlu"}, {"id": 2, "name": "eSun"}]
        self.writes.append((method, path, body))
        if method == "POST" and path == "/api/v1/spool":
            made = {"id": self.next_id}
            self.next_id += 1
            return made
        if method == "POST" and path == "/api/v1/filament":
            return {"id": 30}
        if method == "POST" and path == "/api/v1/vendor":
            return {"id": 5}
        return {}


# ---------------------------------------------------------------- spool_sync units

def test_filament_types_dedupes_and_carries_db_colour_and_hex():
    types = spool_sync.filament_types(client=FakeSpoolman())
    # Sorted by brand: eSun before Sunlu; the duplicate filament row collapsed.
    assert [(t["brand"], t["material"], t["color"], t["hex"]) for t in types] == [
        ("eSun", "PLA", "Black", "000000"),
        ("Sunlu", "PLA+", "Matte White", "FFFFFF"),
    ]


def test_next_spool_id_counts_archived():
    assert spool_sync.next_spool_id(client=FakeSpoolman()) == 20
    empty = lambda method, path, body=None: []
    assert spool_sync.next_spool_id(client=empty) == 1


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


def test_bind_slot_archive_disposition(tmp_path):
    state, client, printer, calls = _bind_harness(tmp_path)
    result = spool_sync.bind_slot(2, 7, client=client, mr=printer, state_path=state,
                                  push_screen_update=False, prior_disposition="archive")
    patches = [c for c in calls if c[0] == "PATCH" and c[1] == "/api/v1/spool/3"]
    assert patches == [("PATCH", "/api/v1/spool/3", {"archived": True})]
    assert result["prior_disposition"] == "archive"
    assert "Spool #3 was archived." in spool_sync.bind_text(result)


def test_bind_slot_depleted_disposition(tmp_path):
    state, client, printer, calls = _bind_harness(tmp_path)
    result = spool_sync.bind_slot(2, 7, client=client, mr=printer, state_path=state,
                                  push_screen_update=False, prior_disposition="depleted")
    patches = [c for c in calls if c[0] == "PATCH" and c[1] == "/api/v1/spool/3"]
    assert patches == [("PATCH", "/api/v1/spool/3",
                        {"archived": True, "remaining_weight": 0, "location": "depleted"})]
    assert "Spool #3 was marked as depleted (0 g left)." in spool_sync.bind_text(result)


def test_bind_slot_shelf_wording_unchanged(tmp_path):
    state, client, printer, calls = _bind_harness(tmp_path)
    result = spool_sync.bind_slot(2, 7, client=client, mr=printer, state_path=state,
                                  push_screen_update=False)
    assert "Spool #3 went back to the shelf." in spool_sync.bind_text(result)


def test_bind_slot_refuses_unknown_disposition(tmp_path):
    state, client, printer, calls = _bind_harness(tmp_path)
    with pytest.raises(spool_sync.SpoolError, match="disposition"):
        spool_sync.bind_slot(2, 7, client=client, mr=printer, state_path=state,
                             push_screen_update=False, prior_disposition="bin it")
    assert calls == []


# ---------------------------------------------------------------- dispatch harness

class FakeAdapter:
    def __init__(self):
        self.sent = []
        self.clarifies = []

    async def send(self, chat_id=None, content=None, metadata=None):
        self.sent.append(content)

    async def send_clarify(self, **kw):
        self.clarifies.append(kw)
        return S(success=True)


def _event(text):
    src = S(platform=S(value="discord"), profile="3dprinting", is_bot=False, profile_route_rejected=False,
            user_id="u", chat_id="c", thread_id=None)
    return S(source=src, text=text, internal=False)


def _gateway(adapter):
    return S(_is_user_authorized_for_source=lambda s: True,
             _delivery_adapter_for=lambda s: adapter,
             _session_key_for_source=lambda s: "live-session")


def _fake_bind(slot, spool_id, **kw):
    return {"slot": slot, "spool_id": spool_id, "prior_spool_id": 12,
            "record": {"name": "Matte White PLA+", "remaining_grams": 1000.0},
            "screen": "pushed", "usage": "unchanged",
            "prior_disposition": kw.get("prior_disposition", "shelf")}


def _run(text, answers, adapter=None, fake=None, bind=None):
    """Run spool_dispatch with the clarify answers queued and spool_sync faked."""
    adapter = adapter or FakeAdapter()
    fake = fake or FakeSpoolman()
    queue = list(answers)
    from tools import clarify_gateway as cm

    def wait(cid, timeout=None):
        return queue.pop(0) if queue else None

    real_types, real_next, real_create = spool_sync.filament_types, spool_sync.next_spool_id, spool_sync.create_spool
    shelf = [{"spool_id": 7, "name": "Matte White", "material": "PLA+", "remaining_grams": 400.0},
             {"spool_id": 9, "name": "Black", "material": "PLA", "remaining_grams": 800.0}]
    patches = [
        mock.patch.object(cm, "wait_for_response", side_effect=wait),
        mock.patch.object(spool_sync, "filament_types", side_effect=lambda: real_types(client=fake)),
        mock.patch.object(spool_sync, "next_spool_id", side_effect=lambda: real_next(client=fake)),
        mock.patch.object(spool_sync, "create_spool", side_effect=lambda spec: real_create(spec, client=fake)),
        mock.patch.object(spool_sync, "load_state", return_value=json.loads(json.dumps(STATE))),
        mock.patch.object(spool_sync, "shelf_spools", side_effect=lambda **kw: [dict(s) for s in shelf]),
        mock.patch.object(spool_sync, "list_text", return_value="Slot 1: empty\nSlot 2: #12 Red PETG"),
    ]
    if bind is not None:
        patches.append(mock.patch.object(spool_sync, "bind_slot", side_effect=bind))
    for patch in patches:
        patch.start()
    try:
        out = asyncio.run(plugin.spool_dispatch(_event(text), _gateway(adapter)))
    finally:
        for patch in patches:
            patch.stop()
    return out, adapter, fake


def _questions(adapter):
    return [c["question"] for c in adapter.clarifies]


# ---------------------------------------------------------------- guided new

def test_bare_spool_menu_lists():
    out, adapter, fake = _run("/spool", ["List spools"])
    assert out["reason"] == "spool command handled"
    assert "What would you like to do" in _questions(adapter)[0]
    assert adapter.clarifies[0]["choices"] == ["Add a new spool", "Change a spool", "List spools", "Cancel"]
    assert "Slot 2: #12" in adapter.sent[-1]
    assert fake.writes == []


def test_bare_spool_menu_enters_new_flow():
    answers = ["Add a new spool", "Matte White · PLA+ · Sunlu", "1000", "Create spool", "Leave it on the shelf"]
    out, adapter, fake = _run("/spool", answers)
    assert any(m == "POST" and p == "/api/v1/spool" for m, p, _b in fake.writes)
    assert "Created spool #20" in adapter.sent[-1]


def test_guided_new_known_type_asks_no_colour_or_hex():
    # Types list sorted by brand: 1 = eSun Black PLA, 2 = Sunlu Matte White PLA+.
    answers = ["2", "1000", "Create spool", "Leave it on the shelf"]
    out, adapter, fake = _run("/spool new", answers)
    questions = _questions(adapter)
    assert len(questions) == 4  # type, grams, confirm, load offer
    assert "Matte White" in questions[0] and "Sunlu" in questions[0]
    assert not any("colour" in q.lower() or "hex" in q.lower() for q in questions)
    # The create carried the database spec: matching filament reused, no new filament POST.
    assert not any(m == "POST" and p == "/api/v1/filament" for m, p, _b in fake.writes)
    spool_posts = [b for m, p, b in fake.writes if m == "POST" and p == "/api/v1/spool"]
    assert spool_posts and spool_posts[0]["filament_id"] == 3
    # No number in the confirm question (#1063: never predicted); the
    # actual number is announced in the result, once creation assigned it.
    assert "This creates a new spool" in questions[2]
    assert "#20" not in questions[2]
    assert adapter.clarifies[2]["choices"][0] == "Create spool"
    assert "Created spool #20 on the shelf" in adapter.sent[-1]


def test_guided_new_typed_number_and_grams():
    answers = ["1", "750", "Create spool", "Leave it on the shelf"]
    out, adapter, fake = _run("/spool new", answers)
    spool_posts = [b for m, p, b in fake.writes if m == "POST" and p == "/api/v1/spool"]
    assert spool_posts[0]["filament_id"] == 4 and spool_posts[0]["initial_weight"] == 750.0
    assert "750 g" in _questions(adapter)[2]


def test_guided_new_new_type_never_asks_hex():
    # Brand is not asked by default (#512: spool.prompt_brand is off), so the
    # answers go straight to material.
    answers = ["New filament type", "PETG", "Sky Blue", "500", "Create spool",
               "Leave it on the shelf"]
    out, adapter, fake = _run("/spool add", answers)
    questions = _questions(adapter)
    assert not any("hex" in q.lower() for q in questions)
    assert not any("brand" in q.lower() for q in questions)
    filament_posts = [b for m, p, b in fake.writes if m == "POST" and p == "/api/v1/filament"]
    # The color code is derived from the name, never asked for.
    assert filament_posts and filament_posts[0]["color_hex"] == "64B5F6"
    assert filament_posts[0]["name"] == "Sky Blue" and filament_posts[0]["material"] == "PETG"
    assert "Created spool #20" in adapter.sent[-1]


def test_guided_new_prompt_brand_on_asks_brand_first():
    answers = ["New filament type", "sunlu", "PETG", "Sky Blue", "500", "Create spool",
               "Leave it on the shelf"]
    with mock.patch.object(plugin, "_prompt_brand", return_value=True):
        out, adapter, fake = _run("/spool add", answers)
    questions = _questions(adapter)
    assert any("brand" in q.lower() for q in questions)
    vendor_posts = [b for m, p, b in fake.writes if m == "POST" and p == "/api/v1/vendor"]
    assert vendor_posts == []  # "sunlu" reuses the existing Sunlu vendor, no near-duplicate
    filament_posts = [b for m, p, b in fake.writes if m == "POST" and p == "/api/v1/filament"]
    assert filament_posts and filament_posts[0]["vendor_id"] == 1
    assert "Created spool #20" in adapter.sent[-1]


def test_guided_new_with_no_saved_types_skips_type_question():
    adapter = FakeAdapter()
    queue = ["PLA", "Green", "1000", "Create spool", "Leave it on the shelf"]
    from tools import clarify_gateway as cm

    def wait(cid, timeout=None):
        return queue.pop(0) if queue else None

    fake = FakeSpoolman()
    real_create = spool_sync.create_spool
    with mock.patch.object(cm, "wait_for_response", side_effect=wait), \
            mock.patch.object(spool_sync, "filament_types", return_value=[]), \
            mock.patch.object(spool_sync, "next_spool_id", return_value=1), \
            mock.patch.object(spool_sync, "create_spool",
                              side_effect=lambda spec: real_create(spec, client=fake)):
        asyncio.run(plugin.spool_dispatch(_event("/spool new"), _gateway(adapter)))
    # The first question promises no spool number (#1063: a predicted id
    # can be wrong by creation time) and, with brand off by default,
    # asks the material directly.
    assert _questions(adapter)[0].startswith("Preparing a new spool")
    assert "#" not in _questions(adapter)[0]
    assert "What material" in _questions(adapter)[0]
    assert "Created spool #20" in adapter.sent[-1]


def test_guided_new_cancel_at_confirm_writes_nothing():
    answers = ["1", "1000", "Cancel"]
    out, adapter, fake = _run("/spool new", answers)
    assert adapter.sent[-1] == "Cancelled. Nothing changed."
    assert fake.writes == []


def test_guided_new_typed_multi_color_gets_confirm_or_correct():
    # A typed color that parses to several colors is confirmed as labeled
    # before anything is created (#792 part 2), naming what was understood.
    answers = ["New filament type", "PLA", "Rose Red Blue Green", "That is exactly as labeled",
               "1000", "Create spool", "Leave it on the shelf"]
    out, adapter, fake = _run("/spool new", answers)
    questions = _questions(adapter)
    assert any("more than one color" in q and "Rose Red Blue Green" in q for q in questions)
    filament_posts = [b for m, p, b in fake.writes if m == "POST" and p == "/api/v1/filament"]
    assert filament_posts and filament_posts[0]["name"] == "Rose Red Blue Green"
    assert "Created spool #20" in adapter.sent[-1]


def test_guided_new_typed_fragment_color_can_be_corrected():
    answers = ["New filament type", "PLA+", "term No...", "Type it again", "Black",
               "1000", "Create spool", "Leave it on the shelf"]
    out, adapter, fake = _run("/spool new", answers)
    questions = _questions(adapter)
    assert any("term No" in q for q in questions)
    filament_posts = [b for m, p, b in fake.writes if m == "POST" and p == "/api/v1/filament"]
    assert filament_posts and filament_posts[0]["name"] == "Black"
    assert "Created spool #20" in adapter.sent[-1]


def test_one_shot_new_with_multi_color_creates_nothing():
    # The one-shot form has no confirmation step, so a suspicious parse is
    # refused with what was understood and the guided form named instead.
    out, adapter, fake = _run("/spool new Sunlu, PLA+, Rose Red Blue Green, 1000", [])
    assert adapter.clarifies == []
    assert "more than one color" in adapter.sent[-1]
    assert "Nothing was created" in adapter.sent[-1] and "`/spool new`" in adapter.sent[-1]
    assert fake.writes == []


def test_guided_new_expire_at_first_question_writes_nothing():
    out, adapter, fake = _run("/spool new", [])
    assert adapter.sent[-1] == "Cancelled. Nothing changed."
    assert fake.writes == []


def test_guided_new_then_load_into_empty_slot():
    answers = ["1", "1000", "Create spool", "Load it into a slot", "Slot 1", "Load spool #20"]
    bind = mock.Mock(side_effect=_fake_bind)
    out, adapter, fake = _run("/spool new", answers, bind=bind)
    bind.assert_called_once()
    args, kw = bind.call_args
    assert args == (1, 20) and kw.get("prior_disposition") == "shelf"
    assert kw.get("expected_prior_spool_id") is None  # slot 1 held no spool to name
    assert "Created spool #20" in adapter.sent[-1] and "Slot 1" in adapter.sent[-1]


def test_guided_new_then_load_with_depleted_old_spool():
    answers = ["1", "1000", "Create spool", "Load it into a slot", "Slot 2", "Mark it as depleted", "Load spool #20"]
    bind = mock.Mock(side_effect=_fake_bind)
    out, adapter, fake = _run("/spool new", answers, bind=bind)
    args, kw = bind.call_args
    assert args == (2, 20) and kw.get("prior_disposition") == "depleted"
    assert kw.get("expected_prior_spool_id") == 12  # the spool the fate question named (#515)
    assert "marked as depleted" in adapter.sent[-1]


# ---------------------------------------------------------------- guided change

def test_guided_change_archive():
    answers = ["Slot 2", "#7 Matte White", "Archive it", "Change slot 2"]
    bind = mock.Mock(side_effect=_fake_bind)
    out, adapter, fake = _run("/spool change", answers, bind=bind)
    args, kw = bind.call_args
    assert args == (2, 7) and kw.get("prior_disposition") == "archive"
    assert kw.get("expected_prior_spool_id") == 12  # the spool the confirmation named (#515)
    questions = _questions(adapter)
    assert "Slot 2: #12 Red PETG" in questions[0]
    assert "Matte White · PLA+ · #7 · 400 g left" in questions[1]
    assert "will be archived" in questions[3]
    assert "Matte White · PLA+ · #7 · 400 g left" in questions[3]  # the exact spool, named
    assert "Spool #12 was archived." in adapter.sent[-1]


def test_guided_change_depleted_via_typed_list_number():
    answers = ["2", "1", "Mark it as depleted", "Change slot 2"]
    bind = mock.Mock(side_effect=_fake_bind)
    out, adapter, fake = _run("/spool change", answers, bind=bind)
    args, kw = bind.call_args
    assert args == (2, 7) and kw.get("prior_disposition") == "depleted"
    assert "marked as depleted (0 g left)" in adapter.sent[-1]


def test_guided_change_cancel_at_confirm_binds_nothing():
    answers = ["Slot 2", "#7 Matte White", "Back on the shelf", "Cancel"]
    bind = mock.Mock(side_effect=_fake_bind)
    out, adapter, fake = _run("/spool change", answers, bind=bind)
    bind.assert_not_called()
    assert adapter.sent[-1] == "Cancelled. Nothing changed."
    assert fake.writes == []


def test_guided_change_add_new_first():
    answers = ["Slot 2", "Add a new spool", "1", "1000", "Create spool",
               "Back on the shelf", "Change slot 2"]
    bind = mock.Mock(side_effect=_fake_bind)
    out, adapter, fake = _run("/spool change", answers, bind=bind)
    args, kw = bind.call_args
    assert args == (2, 20)
    assert "Created spool #20" in adapter.sent[-1]


# ---------------------------------------------------------------- natural language

@pytest.mark.parametrize("text", [
    "We need to add new filament",
    "we need to add new filament",
    "add new filament",
    "Add a new spool",
    "adding a spool",
    "new filament",
    "new spool",
    "I need a new spool",
    "please add a new filament",
])
def test_nl_new_phrases_enter_guided_new(text):
    answers = ["1", "1000", "Create spool", "Leave it on the shelf"]
    out, adapter, fake = _run(text, answers)
    assert out["reason"] == "spool NL intent handled"
    assert "Created spool #20" in adapter.sent[-1]


@pytest.mark.parametrize("text", [
    "swap the filament",
    "change the filament",
    "replace the spool",
    "change filament in slot 2",
    "we need to swap the filament",
    "I need to change a filament roll",
    "change a filament roll",
    "swap roll",
    "please swap the roll",
    "replace a spool",
    "I want to change the filament",
])
def test_nl_change_phrases_enter_guided_change(text):
    answers = ["Slot 2", "#7 Matte White", "Back on the shelf", "Change slot 2"]
    bind = mock.Mock(side_effect=_fake_bind)
    out, adapter, fake = _run(text, answers, bind=bind)
    assert out["reason"] == "spool NL intent handled"
    bind.assert_called_once()


@pytest.mark.parametrize("text,expected", [
    ("We need to add new filament", "new"),
    ("add a new spool", "new"),
    ("swap the filament", "change"),
    ("I need to change a filament roll", "change"),
    ("change a filament roll", "change"),
    ("please swap the roll", "change"),
    ("replace a spool", "change"),
    ("I want to change the filament", "change"),
    ("the filament is tangled", None),
    ("add new filament to my shopping list", None),
    ("what filament should I buy?", None),
    ("the new filament prints great", None),
    ("I changed the filament yesterday", None),
    ("spool", None),
    ("print 57", None),
    ("/spool new", None),
    ("", None),
])
def test_spool_nl_intent_table(text, expected):
    assert plugin._spool_nl_intent(text) == expected


def test_nl_discussion_is_ignored_by_dispatch():
    out, adapter, fake = _run("the filament is tangled", [])
    assert out is None
    assert adapter.sent == [] and fake.writes == []


def test_help_text_stays_one_message_and_names_the_spool_phrases():
    # The help text is sent as one message on button platforms (2000 chars);
    # the spool phrases line must not push it over the split limit.
    from print_handy.bot import commands
    text = commands.help_text()
    assert len(text) <= 2000
    assert "add new filament" in text and "guided /spool flow" in text


# ---------------------------------------------------------------- one-shot forms unchanged

def test_one_shot_new_complete_spec_unchanged():
    out, adapter, fake = _run("/spool new Sunlu, PLA+, Matte White, 1000, FFFFFF", [])
    assert adapter.clarifies == []
    assert "Created spool #20 on the shelf" in adapter.sent[-1]
    assert "Load it with `/spool change <slot> 20`" in adapter.sent[-1]


def test_one_shot_change_complete_unchanged():
    bind = mock.Mock(side_effect=_fake_bind)
    out, adapter, fake = _run("/spool change 2 7", [], bind=bind)
    assert adapter.clarifies == []
    bind.assert_called_once_with(2, 7)
    assert "Slot 2" in adapter.sent[-1]


def test_one_shot_list_unchanged():
    out, adapter, fake = _run("/spool list", [])
    assert adapter.clarifies == []
    assert "Slot 2: #12" in adapter.sent[-1]


# ------------------------------------------------------- numbering (P2-182) / labels (P2-183)

def _guided_harness(monkeypatch, answers):
    """Patch plugin._confirm with a recorder; answers are callables per call
    receiving (question, choices) — what an adapter would hand back."""
    calls = []

    async def fake_confirm(adapter, source, metadata, prefix, question, choices, timeout,
                           live_key=None, approval=False):
        calls.append((question, list(choices)))
        return answers[len(calls) - 1](question, list(choices))

    monkeypatch.setattr(plugin, "_confirm", fake_confirm)
    source = S(chat_id="c", thread_id=None)
    gateway = S(_session_key_for_source=lambda s: "live-session")
    return calls, source, gateway


def test_guided_new_choices_match_the_numbered_type_list(monkeypatch):
    """The question numbers every listed type, so the registered choices
    must be the same entries in the same order (plus New filament type
    last): on Matrix a typed '4' is translated to choices[3] before the
    flow sees it, and with only the first three types registered that
    opened new-type entry instead of the displayed fourth type (P2-182)."""
    types = [{"brand": f"Brand{i}", "material": "PLA", "color": f"Color{i}", "hex": "000000"}
             for i in range(1, 6)]
    monkeypatch.setattr(spool_sync, "filament_types", lambda: [dict(t) for t in types])
    monkeypatch.setattr(spool_sync, "next_spool_id", lambda: 20)
    created = []
    monkeypatch.setattr(spool_sync, "create_spool",
                        lambda spec: created.append(dict(spec)) or {"spool_id": 20})
    calls, source, gateway = _guided_harness(monkeypatch, [
        lambda q, choices: choices[3],  # Matrix: typed '4' -> the 4th registered choice
        lambda q, choices: "1000",
        lambda q, choices: choices[0],  # Create spool
        lambda q, choices: "Leave it on the shelf",
    ])
    made_id, _message = asyncio.run(plugin._spool_guided_new(
        FakeAdapter(), source, None, gateway, spool_sync, None, {}))
    question, choices = calls[0]
    labels = [plugin._spool_type_label(t) for t in types]
    assert choices == labels + ["New filament type"]
    assert f"4. {labels[3]}" in question and "6. New filament type" in question
    assert made_id == 20
    assert created and created[0]["brand"] == "Brand4" and created[0]["color"] == "Color4"


def test_guided_change_long_shelf_name_resolves_by_full_label(monkeypatch):
    """A shelf label over 80 chars must be registered whole: adapters
    truncate only the displayed button text and resolve a click back to
    the full registered choice, so truncating at registration made every
    click end with 'That answer was not clear' (P2-183)."""
    long_name = "Matte White " + "x" * 90
    shelf = [{"spool_id": 7, "name": long_name, "material": "PLA+", "remaining_grams": 400.0}]
    state = {"filament_slots": {"2": {"spoolman_id": 12, "name": "Red PETG"}}}
    monkeypatch.setattr(spool_sync, "load_state", lambda **kw: json.loads(json.dumps(state)))
    monkeypatch.setattr(spool_sync, "shelf_spools", lambda **kw: [dict(s) for s in shelf])
    bound = []
    monkeypatch.setattr(spool_sync, "bind_slot",
                        lambda slot, spool_id, **kw: bound.append((slot, spool_id)) or _fake_bind(slot, spool_id))
    calls, source, gateway = _guided_harness(monkeypatch, [
        lambda q, choices: "Slot 2",
        lambda q, choices: choices[0],  # the click resolves to the registered label
        lambda q, choices: "Move to shelf" if "Move to shelf" in choices else choices[0],
        lambda q, choices: choices[0],  # Change slot 2
    ])
    result = asyncio.run(plugin._spool_guided_change(
        FakeAdapter(), source, None, gateway, spool_sync, None, {}))
    shelf_choices = calls[1][1]
    assert shelf_choices[0] == f"{long_name} · PLA+ · #7 · 400 g left"  # registered whole, not [:80]
    assert bound == [(2, 7)]
    assert "not clear" not in result
