"""Loaded-filament view (#744): /print-spools, its trigger phrases, and the
loaded-only rendering in spool_sync.loaded_text.

The view is read-only and deterministic: it must answer with no LLM
configured (the Telegram pure-bot path included), show what is loaded,
where, and how much is left — and never bury that answer in the shelf.
"""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("pf_loaded_spools", HERE.parents[1] / "plugins/print-feedback/__init__.py")
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)

from print_handy.hermes import spool_sync


SPOOLS = [
    {"id": 12, "remaining_weight": 640.0, "initial_weight": 1000.0,
     "filament": {"name": "Red", "material": "PETG", "color_hex": "FF0000",
                  "vendor": {"name": "Sunlu"}}},
    {"id": 7, "remaining_weight": 400.0, "location": "Shelf",
     "filament": {"name": "Matte White", "material": "PLA+", "vendor": {"name": "Sunlu"}}},
]

STATE = {"filament_slots": {
    "2": {"spoolman_id": 12, "name": "Red PETG", "material": "PETG", "color": "red", "hex": "FF0000"},
    "3": {"name": "Galaxy PLA", "material": "PLA", "color": "galaxy"},
}}


def _client(spools=SPOOLS):
    return lambda method, path, body=None: [dict(s) for s in spools]


def _state_file(tmp_path, state):
    path = tmp_path / "printer_hardware.json"
    path.write_text(json.dumps(state))
    return path


# ---------------------------------------------------------------- loaded_text

def test_loaded_text_shows_loaded_only_with_amounts_and_percent(tmp_path):
    text = spool_sync.loaded_text(client=_client(), state_path=_state_file(tmp_path, STATE))
    assert text.startswith("**Loaded filament**")
    assert "Slot 2: #12 Sunlu Red PETG (#FF0000) — 640 g left (64% of 1000 g)" in text
    assert "Slot 1: empty" in text and "Slot 4: empty" in text
    # The shelf spool is inventory, not an answer to "what is loaded".
    assert "#7" not in text and "Matte White" not in text and "**Shelf**" not in text
    assert "`/spool list`" in text  # the pointer to the full inventory


def test_loaded_text_names_unbound_recorded_filament(tmp_path):
    text = spool_sync.loaded_text(client=_client(), state_path=_state_file(tmp_path, STATE))
    assert "Slot 3: Galaxy PLA is recorded here, but no spool is bound to it" in text
    assert "`/spool change 3 <spool#>`" in text


def test_loaded_text_reports_an_unbound_single_spool(tmp_path):
    # #746 review: a single-spool record with filament details but no Spoolman id (the A1 fixture's shape)
    state = {"spool": {"spoolman_id": None, "name": "Dark Green PLA", "material": "PLA"}}
    text = spool_sync.loaded_text(client=_client(), state_path=_state_file(tmp_path, state))
    assert "Nothing is recorded as loaded" not in text
    assert "Spool: Dark Green PLA is recorded here, but no spool is bound to it" in text
    assert "record it with `/spool`" in text


def test_loaded_text_single_spool_printer_and_unknown_percent(tmp_path):
    state = {"spool": {"spoolman_id": 7, "name": "Matte White PLA+", "material": "PLA+"}}
    text = spool_sync.loaded_text(client=_client(), state_path=_state_file(tmp_path, state))
    assert "Spool: #7 Sunlu Matte White PLA+ — 400 g left" in text
    assert "percent unknown" in text  # no initial_weight on the fake spool
    assert "Slot 1" not in text  # no phantom IFS slots for a one-spool printer


def test_loaded_text_nothing_recorded(tmp_path):
    text = spool_sync.loaded_text(client=_client(), state_path=_state_file(tmp_path, {}))
    assert "Nothing is recorded as loaded on this printer." in text


def test_loaded_text_spool_missing_from_spoolman_uses_the_record(tmp_path):
    state = {"filament_slots": {"1": {"spoolman_id": 99, "name": "Pink PLA", "material": "PLA",
                                      "color": "pink", "hex": "F95D73"}}}
    text = spool_sync.loaded_text(client=_client(), state_path=_state_file(tmp_path, state))
    assert "Slot 1: #99 Pink PLA (#F95D73) — amount unknown" in text


def test_loaded_text_spoolman_down_still_answers_without_amounts(tmp_path):
    def down(method, path, body=None):
        raise spool_sync.SpoolError("spoolman unavailable")

    text = spool_sync.loaded_text(client=down, state_path=_state_file(tmp_path, STATE))
    assert "Slot 2: #12 Red PETG (#FF0000) — amount unavailable (Spoolman did not answer)" in text
    assert "640" not in text  # a stale snapshot is never dressed up as current


# ---------------------------------------------------------------- intent phrases

@pytest.mark.parametrize("text", [
    "What colors are loaded",
    "what colors are loaded?",
    "What colours are loaded",
    "which spools are loaded",
    "What filament is loaded",
    "what's loaded",
    "What is loaded?",
    "Current Spools",
    "current spools",
    "loaded filament",
    "show me the loaded spools",
    "list the current filament",
    "print-colors",
    "print colors",
])
def test_loaded_intent_phrases(text):
    assert plugin._loaded_spools_intent(text) == "loaded"


@pytest.mark.parametrize("text", [
    "the loaded filament is tangled",
    "what filament should I buy?",
    "current events in the channel",
    "the current spool is almost empty",
    "add new filament",
    "swap the filament",
    "/print-spools",
    "print 57",
    "",
])
def test_loaded_intent_discussion_never_matches(text):
    assert plugin._loaded_spools_intent(text) is None


# ---------------------------------------------------------------- dispatch

class FakeAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id=None, content=None, metadata=None):
        self.sent.append(content)


def _event(text, **source_kw):
    src = S(platform=S(value="discord"), profile="3dprinting", is_bot=False,
            profile_route_rejected=False, user_id="u", chat_id="c", thread_id=None, **source_kw)
    return S(source=src, text=text, internal=False)


def _gateway(adapter):
    return S(_is_user_authorized_for_source=lambda s: True,
             _delivery_adapter_for=lambda s: adapter,
             _session_key_for_source=lambda s: "live-session")


def _run_loaded(text, **source_kw):
    adapter = FakeAdapter()
    seen = []

    def fake_loaded(**kw):
        seen.append(kw)
        return "loaded view body"

    with mock.patch.object(spool_sync, "loaded_text", side_effect=fake_loaded):
        out = asyncio.run(plugin.loaded_spools_dispatch(_event(text, **source_kw), _gateway(adapter)))
    return out, adapter, seen


@pytest.mark.parametrize("text", [
    "/print-spools", "/spool loaded", "/print-colors",
    "What colors are loaded", "Current Spools", "print-colors",
])
def test_loaded_dispatch_triggers(text):
    out, adapter, seen = _run_loaded(text)
    assert out == {"action": "skip", "reason": "loaded spools view handled"}
    assert adapter.sent == ["loaded view body"]
    assert seen == [{}]  # unrouted: the legacy single-printer call shape


@pytest.mark.parametrize("text", [
    "/print-colors /archive/JOB.json",  # the archived-job form keeps its owner
    "/spool list",
    "/spool",
    "the loaded filament is tangled",
    "hello",
])
def test_loaded_dispatch_leaves_other_text_alone(text):
    out, adapter, seen = _run_loaded(text)
    assert out is None and adapter.sent == [] and seen == []


def test_loaded_dispatch_shared_channel_sections_each_printer():
    adapter = FakeAdapter()
    seen = []

    def fake_loaded(**kw):
        seen.append(kw.get("printer_id"))
        return f"loaded for {kw.get('printer_id')}"

    with mock.patch.object(spool_sync, "loaded_text", side_effect=fake_loaded), \
            mock.patch.object(plugin, "_printer_label", side_effect=lambda pid: f"Printer {pid}"):
        out = asyncio.run(plugin.loaded_spools_dispatch(
            _event("/print-spools", shared_printer_ids=["garage", "workshop"]), _gateway(adapter)))
    assert out["reason"] == "loaded spools view handled"
    assert sorted(seen) == ["garage", "workshop"]
    assert "Printer garage" in adapter.sent[-1] and "loaded for workshop" in adapter.sent[-1]


def test_loaded_dispatch_failure_is_a_plain_answer_not_a_crash():
    adapter = FakeAdapter()
    with mock.patch.object(spool_sync, "loaded_text", side_effect=RuntimeError("boom")):
        out = asyncio.run(plugin.loaded_spools_dispatch(_event("/print-spools"), _gateway(adapter)))
    assert out["reason"] == "loaded spools view handled"
    assert "cannot say what is loaded" in adapter.sent[-1]


# ---------------------------------------------------------------- registry / LLM list

def test_adding_print_spools_does_not_degrade_the_spool_near_miss():
    # "/spol" ties on windowed score between "spool" (whole name) and the
    # "spools" window of "print-spools"; the whole-name tie-break keeps the
    # long-standing single suggestion instead of a pick-list.
    from print_handy.bot import runtime
    assert runtime.command_suggestion("/spol list") == "/spool list"
    assert runtime.command_suggestion_candidates("/spol list") == []
    # And the new command's own near miss resolves to it.
    assert runtime.command_suggestion("/print-spol") == "/print-spools"


def test_print_spools_is_registered_and_the_llm_may_name_it():
    from print_handy.bot import commands, llm
    cmd = commands.by_name("print-spools")
    assert cmd is not None and cmd.usage == "/print-spools"
    assert "/print-spools" in commands.help_text()
    assert len(commands.help_text()) <= 2000
    assert llm.allowed_command("/print-spools") == "/print-spools"
