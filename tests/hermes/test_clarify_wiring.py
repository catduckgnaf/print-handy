"""The S5 switch itself (#524 stage 2): register() activation + hook swap,
the alert_button_handlers install, and the _adapter_for delivery
substitution.

Everything is gated on Hermes host + [discord] clarify_ui on, and every
test pins one side of the gate: with the switch off (the default) the
plugin behaves byte-for-byte as before the switch existed; with it on,
handler questions register into Print Handy's own registry and render
through HermesClarifyAdapter while plain posts keep travelling through
the host adapter.
"""
import asyncio
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

from print_handy.core import prompts
from print_handy.hermes import _config
from print_handy.hermes.clarify_ui import HermesClarifyAdapter

ROOT = Path(__file__).resolve().parents[2]


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "print_feedback_wiring", ROOT / "plugins/print-feedback/__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def hermes_ctx():
    """A register() context offering the Hermes-only APIs (auxiliary task + llm lane)."""
    ctx = mock.Mock()
    ctx.llm = object()
    return ctx


def switch_on(monkeypatch, plugin):
    monkeypatch.setattr(plugin, "_HOST_HERMES", True)
    monkeypatch.setattr(_config, "clarify_ui_enabled", lambda: True)
    monkeypatch.setattr(_config, "discord_token", lambda: "tok")


# ---------------------------------------------------------------------- register()


def test_register_with_switch_on_activates_the_own_registry_and_swaps_the_hook(monkeypatch):
    plugin = load_plugin()
    activated = []
    monkeypatch.setattr(prompts, "activate", lambda on=True: activated.append(on))
    # The registry activates only when the renderer can be installed,
    # which takes a resolvable token (P2-208); the no-token stand-down is
    # pinned in test_clarify_retire.py.
    monkeypatch.setattr(_config, "discord_token", lambda: "tok")
    previous = copy.deepcopy(_config.cfg())  # the session override (tests/conftest) must survive
    data = copy.deepcopy(previous)
    data.setdefault("discord", {})["clarify_ui"] = True
    _config.set_override(data)
    try:
        ctx = hermes_ctx()
        plugin.register(ctx)
    finally:
        _config.set_override(previous)
    assert activated == [True]  # design §3.1: registry flips with the rest of the switch
    assert plugin._HOST_HERMES is True
    hooks = [item.args for item in ctx.register_hook.call_args_list]
    dispatch = [fn for name, fn in hooks if name == "pre_gateway_dispatch"]
    assert plugin.typed_answer_dispatch in dispatch
    assert plugin.typed_quantity_dispatch not in dispatch


def test_register_with_switch_off_changes_nothing(monkeypatch):
    plugin = load_plugin()
    activated = []
    monkeypatch.setattr(prompts, "activate", lambda on=True: activated.append(on))
    previous = copy.deepcopy(_config.cfg())
    data = copy.deepcopy(previous)
    data.setdefault("discord", {}).pop("clarify_ui", None)
    _config.set_override(data)
    try:
        ctx = hermes_ctx()
        plugin.register(ctx)
    finally:
        _config.set_override(previous)
    assert activated == []  # Hermes' registry keeps the questions
    assert plugin._HOST_HERMES is True  # the host record is kept either way
    hooks = [item.args for item in ctx.register_hook.call_args_list]
    dispatch = [fn for name, fn in hooks if name == "pre_gateway_dispatch"]
    assert plugin.typed_quantity_dispatch in dispatch
    assert plugin.typed_answer_dispatch not in dispatch


def test_register_under_standalone_never_activates(monkeypatch):
    # The Router offers neither Hermes-only API: even with the setting on,
    # the switch cannot engage (standalone's adapter already renders ours).
    plugin = load_plugin()
    activated = []
    monkeypatch.setattr(prompts, "activate", lambda on=True: activated.append(on))
    monkeypatch.setattr(_config, "clarify_ui_enabled", lambda: True)
    ctx = mock.Mock(spec=["register_hook", "register_platform_handler", "register_locale"])
    plugin.register(ctx)
    assert plugin._HOST_HERMES is False
    assert activated == []


# ---------------------------------------------------------------------- the install


def attach(plugin):
    handlers = {}
    bot = S(add_listener=lambda fn, name: handlers.update({name: fn}))
    host = S(_is_allowed_user=lambda uid, user=None, guild=None, channel_ids=None: True)
    plugin.alert_button_handlers(bot, host)
    return handlers


def test_attach_installs_the_clarify_adapter_under_hermes_with_the_switch_on(monkeypatch):
    plugin = load_plugin()
    monkeypatch.setattr(plugin, "_CLARIFY_UI", None)
    switch_on(monkeypatch, plugin)
    attach(plugin)
    assert isinstance(plugin._CLARIFY_UI, HermesClarifyAdapter)


def test_attach_installs_nothing_without_a_token(monkeypatch):
    plugin = load_plugin()
    monkeypatch.setattr(plugin, "_CLARIFY_UI", None)
    monkeypatch.setattr(plugin, "_HOST_HERMES", True)
    monkeypatch.setattr(_config, "clarify_ui_enabled", lambda: True)
    monkeypatch.setattr(_config, "discord_token", lambda: "")
    attach(plugin)
    assert plugin._CLARIFY_UI is None  # no token: host adapter fallback, no new failure mode


def test_attach_installs_nothing_in_standalone(monkeypatch):
    plugin = load_plugin()
    monkeypatch.setattr(plugin, "_CLARIFY_UI", None)
    monkeypatch.setattr(plugin, "_HOST_HERMES", False)
    monkeypatch.setattr(_config, "clarify_ui_enabled", lambda: True)
    monkeypatch.setattr(_config, "discord_token", lambda: "tok")
    attach(plugin)
    assert plugin._CLARIFY_UI is None


# ---------------------------------------------------------------------- delivery substitution


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()
        self.status = 200

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self):
        self.requests = []
        self._seq = 0

    def __call__(self, req, timeout=None):
        payload = json.loads(req.data.decode()) if req.data else {}
        self.requests.append((req.method, req.full_url, payload))
        if req.method == "POST":
            self._seq += 1
            return FakeResponse({"id": f"m{self._seq}"})
        return FakeResponse({})


class RecordingHost:
    """The host (Hermes) adapter stand-in: records what reaches it."""

    name = "discord"
    supports_buttons = True

    def __init__(self):
        self.sent = []
        self.clarified = []

    async def send(self, chat_id, content, metadata=None):
        self.sent.append((chat_id, content))
        return S(success=True, message_id="host1")

    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
        self.clarified.append((chat_id, question))
        return S(success=True, message_id="host2")


class FakeGateway:
    def __init__(self, host):
        self.host = host

    def _delivery_adapter_for(self, source):
        return self.host


def discord_source():
    return S(platform=S(value="discord"), profile="3dprinting", user_id="1",
             chat_id="chan1", thread_id=None, is_bot=False)


def install_ui(monkeypatch, plugin):
    opener = FakeOpener()
    ui = HermesClarifyAdapter(token="tok", opener=opener)
    monkeypatch.setattr(plugin, "_CLARIFY_UI", ui)
    monkeypatch.setattr(_config, "clarify_ui_enabled", lambda: True)
    return ui, opener


def test_adapter_for_substitutes_send_clarify_only(monkeypatch):
    plugin = load_plugin()
    ui, opener = install_ui(monkeypatch, plugin)
    host = RecordingHost()
    adapter = plugin._adapter_for(FakeGateway(host), discord_source())
    assert adapter is not host and adapter is not ui
    result = asyncio.run(adapter.send_clarify("chan1", "Pick?", ["A", "B"], "cid1", "key"))
    assert result.success and result.message_id == "m1"
    assert [r[0] for r in opener.requests] == ["POST"]  # the UI posted the question
    assert host.clarified == []  # the host adapter never saw it
    asyncio.run(adapter.send(chat_id="chan1", content="plain post"))
    assert host.sent == [("chan1", "plain post")]  # plain posts still travel via the host
    assert adapter.supports_buttons is True and adapter.name == "discord"  # reads as the host


def test_adapter_for_falls_back_for_other_platforms_and_when_uninstalled(monkeypatch):
    plugin = load_plugin()
    ui, _opener = install_ui(monkeypatch, plugin)
    host = RecordingHost()
    telegram = S(platform=S(value="telegram"), chat_id="chan1", thread_id=None)
    assert plugin._adapter_for(FakeGateway(host), telegram) is host
    monkeypatch.setattr(plugin, "_CLARIFY_UI", None)
    assert plugin._adapter_for(FakeGateway(host), discord_source()) is host


def test_adapter_for_keeps_the_none_adapter_contract(monkeypatch):
    plugin = load_plugin()
    install_ui(monkeypatch, plugin)
    assert plugin._adapter_for(FakeGateway(None), discord_source()) is None


def test_confirm_renders_through_the_ui_and_resolves_by_click(monkeypatch):
    """The whole seam end to end: a handler confirm posts via REST through
    the substituted adapter and a pbq: click resolves the wait."""
    plugin = load_plugin()
    ui, opener = install_ui(monkeypatch, plugin)
    host = RecordingHost()
    adapter = plugin._adapter_for(FakeGateway(host), discord_source())
    source = discord_source()

    async def run():
        task = asyncio.ensure_future(
            plugin._confirm(adapter, source, None, "test", "Proceed?", ["Yes", "Cancel"], 5))
        for _ in range(200):
            if opener.requests:
                break
            await asyncio.sleep(0.01)
        assert opener.requests, "the question was never posted"
        _method, _url, payload = opener.requests[0]
        custom_id = payload["components"][0]["components"][0]["custom_id"]
        cid = custom_id.split(":")[1]
        interaction = S(channel_id="chan1", user=S(display_name="op", name="op"),
                        message=S(id="m1", content="Proceed?"),
                        response=S(defer=lambda: asyncio.sleep(0),
                                   send_message=lambda *a, **k: asyncio.sleep(0)))
        await ui.handle_click(interaction, cid, 0)
        return await task

    # register() activated the own registry in this posture: without it the
    # test session's tools.clarify_gateway stand-in (tests/hermes/conftest)
    # would be the handlers' registry while the UI resolves into our own.
    prompts.activate(True)
    try:
        result = asyncio.run(run())
    finally:
        prompts.activate(False)
    assert result == "Yes"
    assert host.clarified == [] and host.sent == []
