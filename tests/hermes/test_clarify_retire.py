"""S6 retirements on the clarify-UI path (#524 stage 2).

With the S5 switch on, two Hermes workarounds stand down for the install:
register() no longer patches Hermes' clarify locale (handler questions
never render through Hermes anymore), and alert_button_handlers no longer
installs the Stop-view substitution into prepared_intake — the clarify
path completes its standing prompt in place instead (retire on success,
guidance-plus-Retry by id after a safe refusal). With the switch off,
both install exactly as before; those sides are pinned too.
"""
import asyncio
import copy
import importlib.util
import json
import urllib.request
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

from print_handy.bot.platform import SendResult
from print_handy.core import prepared_intake, prompts
from print_handy.hermes import _config, print_alert_buttons as pab
from print_handy.hermes.clarify_ui import HermesClarifyAdapter

ROOT = Path(__file__).resolve().parents[2]


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "print_feedback_retire", ROOT / "plugins/print-feedback/__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def with_config(monkeypatch, **discord):
    """Install a session-config copy with the given [discord] keys; returns a restore callable."""
    previous = copy.deepcopy(_config.cfg())
    data = copy.deepcopy(previous)
    data.setdefault("discord", {}).update(discord)
    _config.set_override(data)
    return lambda: _config.set_override(previous)


# ---------------------------------------------------------------------- the locale patch


def test_register_skips_the_locale_patch_with_the_switch_on(monkeypatch):
    plugin = load_plugin()
    restore = with_config(monkeypatch, clarify_ui=True)
    monkeypatch.setattr(prompts, "activate", lambda on=True: None)
    monkeypatch.setattr(_config, "discord_token", lambda: "tok")
    ctx = mock.Mock()
    ctx.llm = object()
    try:
        plugin.register(ctx)
    finally:
        restore()
    assert ctx.register_locale.call_args_list == []


def test_register_without_a_token_keeps_the_own_registry_off(monkeypatch):
    """Switch on but no resolvable bot token: the clarify adapter can
    never be installed, so the own prompt registry must NOT activate —
    questions registered there would render nowhere and sit unanswered
    until timeout (P2-208). The locale patch and today's typed hook stay."""
    plugin = load_plugin()
    restore = with_config(monkeypatch, clarify_ui=True)
    activated = []
    monkeypatch.setattr(prompts, "activate", lambda on=True: activated.append(on))
    monkeypatch.setattr(_config, "discord_token", lambda: "")
    ctx = mock.Mock()
    ctx.llm = object()
    try:
        plugin.register(ctx)
    finally:
        restore()
    assert activated == []
    assert len(ctx.register_locale.call_args_list) == 1
    hooks = [item.args for item in ctx.register_hook.call_args_list]
    dispatch = [fn for name, fn in hooks if name == "pre_gateway_dispatch"]
    assert plugin.typed_answer_dispatch not in dispatch


def test_register_keeps_the_locale_patch_with_the_switch_off(monkeypatch):
    plugin = load_plugin()
    restore = with_config(monkeypatch, clarify_ui=False)
    ctx = mock.Mock()
    ctx.llm = object()
    try:
        plugin.register(ctx)
    finally:
        restore()
    assert len(ctx.register_locale.call_args_list) == 1


# ---------------------------------------------------------------------- the Stop substitution


def attach(plugin):
    handlers = {}
    bot = S(add_listener=lambda fn, name: handlers.update({name: fn}))
    host = S(_is_allowed_user=lambda uid, user=None, guild=None, channel_ids=None: True)
    plugin.alert_button_handlers(bot, host)
    return handlers


def switch_on(monkeypatch, plugin):
    monkeypatch.setattr(plugin, "_CLARIFY_UI", None)
    monkeypatch.setattr(plugin, "_HOST_HERMES", True)
    monkeypatch.setattr(_config, "clarify_ui_enabled", lambda: True)
    monkeypatch.setattr(_config, "discord_token", lambda: "tok")


def test_attach_with_the_switch_on_stands_the_stop_substitution_down(monkeypatch):
    plugin = load_plugin()
    switch_on(monkeypatch, plugin)
    monkeypatch.setattr(prepared_intake, "STOP_SENDER", object())  # sentinel: must be cleared
    monkeypatch.setattr(prepared_intake, "REFUSAL_EDITOR_BY_ID", None)
    attach(plugin)
    assert isinstance(plugin._CLARIFY_UI, HermesClarifyAdapter)
    assert prepared_intake.STOP_SENDER is None
    assert prepared_intake.REFUSAL_EDITOR_BY_ID is not None


def test_attach_with_the_switch_off_installs_the_stop_substitution_as_before(monkeypatch):
    plugin = load_plugin()
    monkeypatch.setattr(prepared_intake, "STOP_SENDER", None)
    monkeypatch.setattr(prepared_intake, "REFUSAL_EDITOR_BY_ID", None)
    attach(plugin)
    assert prepared_intake.STOP_SENDER is not None
    assert prepared_intake.REFUSAL_EDITOR_BY_ID is None


def test_edit_refusal_by_id_mirrors_the_view_outcome(monkeypatch):
    plugin = load_plugin()
    switch_on(monkeypatch, plugin)
    monkeypatch.setattr(prepared_intake, "STOP_SENDER", object())
    monkeypatch.setattr(prepared_intake, "REFUSAL_EDITOR_BY_ID", None)
    attach(plugin)
    editor = prepared_intake.REFUSAL_EDITOR_BY_ID
    calls = []
    monkeypatch.setattr(pab, "edit_message", lambda *a: calls.append(a) or True)
    receipt_id = "a" * 32
    ok = asyncio.run(editor(S(), "chan1", "m9", "guidance text",
                            {"receipt": {"intake_id": receipt_id}}, "raw detail"))
    assert ok is True
    token, channel_id, message_id, text, components = calls[0]
    assert (token, channel_id, message_id, text) == ("tok", "chan1", "m9", "guidance text")
    button = components[0]["components"][0]
    assert button["label"] == "Retry"
    assert button["custom_id"] == pab.retry_custom_id(receipt_id)
    # An unusable receipt id: bare guidance, no Retry, reported unhandled.
    ok = asyncio.run(editor(S(), "chan1", "m9", "guidance text", {"receipt": {}}, "raw"))
    assert ok is False
    assert calls[1][4] == []


# ---------------------------------------------------------------------- the clarify path in prepared_intake


class ClarifyAdapter:
    """The message-id-carrying adapter shape the clarify path completes."""

    def __init__(self):
        self.retired = []
        self.clarified = []

    async def send(self, **kw):
        raise AssertionError("the clarify path never posts a second message")

    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
        self.clarified.append(clarify_id)
        return SendResult(True, message_id="m9")

    async def retire_prompt(self, clarify_id):
        self.retired.append(clarify_id)
        return True


SOURCE = S(chat_id="chan1", thread_id=None)
KEY = "live:chan1:user1"


def run_stop(adapter, work, refusal_context=None, monkeypatch=None):
    monkeypatch.setattr(prepared_intake, "STOP_SENDER", None)
    return asyncio.run(prepared_intake._with_stop(
        adapter, SOURCE, KEY, None, "Working…", work,
        allow_stop=True, refusal_context=refusal_context))


def test_clarify_path_completion_retires_the_question(monkeypatch):
    adapter = ClarifyAdapter()

    async def work():
        return "done"

    assert run_stop(adapter, work(), monkeypatch=monkeypatch) == "done"
    assert adapter.retired == adapter.clarified  # the standing question was retired in place


def test_clarify_path_refusal_edits_by_id_and_skips_retire(monkeypatch):
    adapter = ClarifyAdapter()
    edits = []

    async def editor(source, channel_id, message_id, text, context, raw):
        edits.append((channel_id, message_id, text))
        return True

    monkeypatch.setattr(prepared_intake, "REFUSAL_EDITOR_BY_ID", editor)
    context = {"receipt": {"intake_id": "b" * 32}}

    async def work():
        return False, "REFUSED: printer is still busy. Nothing uploaded or started."

    result = run_stop(adapter, work(), refusal_context=context, monkeypatch=monkeypatch)
    assert result[0] is False
    assert len(edits) == 1 and edits[0][0] == "chan1" and edits[0][1] == "m9"
    assert context["prompt_handled"] is True
    assert context["start_prompt_message_id"] == "m9"
    assert adapter.retired == []  # the refusal edit replaced the retire


# ---------------------------------------------------------------------- pab.edit_message components


def test_edit_message_omits_components_unless_given(monkeypatch):
    seen = []

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        seen.append(json.loads(req.data.decode()))
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert pab.edit_message("tok", "chan1", "m9", "hello") is True
    assert "components" not in seen[0]  # pre-existing callers: payload unchanged
    components = [{"type": 1, "components": [{"type": 2, "style": 1, "label": "Retry",
                                              "custom_id": "pbretry:x"}]}]
    assert pab.edit_message("tok", "chan1", "m9", "hello", components) is True
    assert seen[1]["components"] == components
    assert pab.edit_message("tok", "chan1", "m9", "hello", []) is True
    assert seen[2]["components"] == []  # explicit empty list strips the buttons
