"""Fuzzy extension for the loaded-filament view (#744 follow-up).

Three layers, all measured against the shared scorer's own policy
(#468): British color spellings fold in canonical(); plain-text near
misses of a loaded ask earn a did-you-mean prompt and nothing more;
and the new command's own typed near misses resolve through the
existing command-suggestion machinery.
"""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("pf_loaded_fuzzy", HERE.parents[1] / "plugins/print-feedback/__init__.py")
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)

from print_handy.core import fuzzy
from print_handy.hermes import spool_sync


# ---------------------------------------------------------------- canonical spellings

def test_color_spellings_are_one_word_to_the_scorer():
    assert fuzzy.canonical("colour") == fuzzy.canonical("color") == "color"
    assert fuzzy.canonical("colours") == fuzzy.canonical("colors") == "color"
    assert fuzzy.canonical("Silk Grey") == fuzzy.canonical("silk gray")
    assert fuzzy.fuzzy_score("colour", "color") == 99  # equated, still capped below exact's 100 tier
    # Unrelated look-alikes are not merged by the new forms.
    assert fuzzy.canonical("colour") != fuzzy.canonical("collar")
    assert fuzzy.canonical("grey") != fuzzy.canonical("green")


# ---------------------------------------------------------------- fuzzy phrase policy

@pytest.mark.parametrize("text", [
    "what colours are loded",
    "currant spools",
    "wat colors are loaded",
    "loaded filamnet",
    "how much fillament is left",
    "print colurs",
    "current spoolz",
    "show me the loaded spools",
    "what colour is loaded",
    "which spools are loded",
])
def test_fuzzy_loaded_asks_are_clear(text):
    assert plugin._loaded_spools_fuzzy(text) is True


@pytest.mark.parametrize("text", [
    "load the filament",               # close to a rival action phrase: no clear margin
    "the loaded filament is tangled",  # discussion, scores 0
    "what colors are available",       # a shelf question: grey zone, not a loaded ask
    "change the filament",             # the rival intent itself
    "add new filamnet",                # a typo of the rival intent stays the rival's
    "swap the filamnet",
    "what filament should I buy",
    "current events",
    "the current spool is almost empty",
    "is the printer loaded",
    "/print-spools",                   # command-shaped: the command machinery's job
    "",
])
def test_fuzzy_loaded_asks_never_claim_other_text(text):
    assert plugin._loaded_spools_fuzzy(text) is False


def test_how_much_filament_is_left_is_also_an_exact_ask():
    assert plugin._loaded_spools_intent("how much filament is left") == "loaded"
    assert plugin._loaded_spools_intent("How much filament is left on the spools?") == "loaded"


# ---------------------------------------------------------------- dispatch: the prompt

class FakeAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id=None, content=None, metadata=None):
        self.sent.append(content)


def _event(text):
    src = S(platform=S(value="discord"), profile="3dprinting", is_bot=False,
            profile_route_rejected=False, user_id="u", chat_id="c", thread_id=None)
    return S(source=src, text=text, internal=False)


def _gateway(adapter):
    return S(_is_user_authorized_for_source=lambda s: True,
             _delivery_adapter_for=lambda s: adapter,
             _session_key_for_source=lambda s: "live-session")


def _run_fuzzy_dispatch(text, answer):
    adapter = FakeAdapter()
    confirm = mock.AsyncMock(return_value=answer)
    with mock.patch.object(plugin, "_confirm", confirm), \
            mock.patch.object(spool_sync, "loaded_text", return_value="loaded view body") as loaded:
        out = asyncio.run(plugin.loaded_spools_dispatch(_event(text), _gateway(adapter)))
    return out, adapter, confirm, loaded


def test_fuzzy_ask_prompts_and_a_yes_shows_the_view():
    out, adapter, confirm, loaded = _run_fuzzy_dispatch("currant spools", plugin._LOADED_SUGGEST_YES)
    assert out == {"action": "skip", "reason": "loaded spools view handled"}
    question = confirm.call_args.args[4]
    assert 'Did you mean "what colors are loaded"?' in question
    assert confirm.call_args.args[6] == 60  # the stage-3 suggestion expiry, not the spool-flow timeout
    loaded.assert_called_once_with()
    assert adapter.sent == ["loaded view body"]


def test_fuzzy_ask_declined_or_timed_out_shows_nothing():
    for answer in (plugin._LOADED_SUGGEST_NO, None):
        out, adapter, confirm, loaded = _run_fuzzy_dispatch("currant spools", answer)
        assert out == {"action": "skip", "reason": "loaded spools suggestion declined"}
        loaded.assert_not_called()
        assert adapter.sent == ["Okay — nothing was shown."]


def test_exact_ask_never_prompts():
    adapter = FakeAdapter()
    confirm = mock.AsyncMock(side_effect=AssertionError("exact asks must not prompt"))
    with mock.patch.object(plugin, "_confirm", confirm), \
            mock.patch.object(spool_sync, "loaded_text", return_value="loaded view body"):
        out = asyncio.run(plugin.loaded_spools_dispatch(_event("What colors are loaded"), _gateway(adapter)))
    assert out["reason"] == "loaded spools view handled"
    assert adapter.sent == ["loaded view body"]


# ---------------------------------------------------------------- command near misses

@pytest.mark.parametrize("text", [
    "/print-spol", "/print-spols", "/prnt-spools", "/print-spool",
    "/print-spoools", "/prnit-spools", "/print-spoolsx",
])
def test_print_spools_command_typos_suggest_the_command(text):
    from print_handy.bot import runtime
    assert runtime.command_suggestion(text) == "/print-spools"
    assert runtime.command_suggestion_candidates(text) == []
