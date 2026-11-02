"""Stage 2 plugin wiring (#524): the pbq: branch in on_interaction and the
typed_answer_dispatch hook.

The branch and the hook are live exactly when the S5 switch installs
plugin._CLARIFY_UI (Hermes host + [discord] clarify_ui on) and register()
swaps the hook in. These tests install a real HermesClarifyAdapter by
hand — the posture the switch creates — and pin both sides: with no
adapter installed nothing changes, with one installed clicks and typed
answers resolve Print Handy's own registry. The switch itself (register,
install, delivery substitution) is pinned in test_clarify_wiring.py.
"""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

from print_handy.core import prompts
from print_handy.hermes.clarify_ui import HermesClarifyAdapter

ROOT = Path(__file__).resolve().parents[2]
CID = "clarifyplugin1"
KEY = "live:chan1:user1"
QUESTION = "Pick a color?"
CHOICES = ["Red", "Blue", "Cancel"]


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "print_feedback_clarify", ROOT / "plugins/print-feedback/__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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

    def patches(self):
        return [r for r in self.requests if r[0] == "PATCH"]


class Interaction:
    def __init__(self, custom_id, channel_id="chan1"):
        self.data = {"custom_id": custom_id}
        self.user = S(id=1, bot=False, display_name="op", name="op")
        self.guild = None
        self.channel_id = channel_id
        self.message = S(id="m1", content=QUESTION)
        self.out = []
        interaction = self

        class Response:
            async def send_message(self, content, ephemeral=False, **kw):
                interaction.out.append(("send", content, ephemeral))

            async def edit_message(self, content=None, view=None, **kw):
                interaction.out.append(("edit", content, view))

            async def defer(self):
                interaction.out.append(("defer",))

        self.response = Response()
        self.followup = S(send=self._followup)

    async def _followup(self, content, view=None):
        self.out.append(("followup", content))


def wire(plugin, adapter):
    handlers = {}
    bot = S(add_listener=lambda fn, name: handlers.update({name: fn}))
    plugin.alert_button_handlers(bot, adapter)
    return handlers["on_interaction"]


def host_adapter(allowed=True):
    return S(_is_allowed_user=lambda uid, user=None, guild=None, channel_ids=None: allowed)


def posted_question(opener):
    """The handler pattern: register in the own registry, render through the adapter."""
    prompts.register(CID, KEY, QUESTION, CHOICES)
    ui = HermesClarifyAdapter(token="tok", opener=opener)
    asyncio.run(ui.send_clarify("chan1", QUESTION, CHOICES, CID, KEY))
    return ui


def close(cid=CID):
    prompts.resolve_gateway_clarify(cid, prompts.CANCELLED)
    prompts.wait_for_response(cid, 0.01)


# ---------------------------------------------------------------------- clicks (S3)


def test_pbq_click_resolves_through_the_plugin_listener(monkeypatch):
    plugin = load_plugin()
    opener = FakeOpener()
    ui = posted_question(opener)
    monkeypatch.setattr(plugin, "_CLARIFY_UI", ui)
    on_interaction = wire(plugin, host_adapter())
    interaction = Interaction(f"pbq:{CID}:1")
    asyncio.run(on_interaction(interaction))
    # P2-207: the click is acknowledged with discord.py's defer() — the
    # fake response offers nothing else, so a defer_update() call would
    # raise inside the suppress and this record would be missing.
    assert ("defer",) in interaction.out
    assert prompts.wait_for_response(CID, 1) == "Blue"
    patches = opener.patches()
    assert len(patches) == 1 and patches[0][2]["content"] == f"{QUESTION}\n> op chose **Blue**"
    assert patches[0][2]["components"] == []


def test_pbq_click_is_inert_until_the_adapter_is_installed():
    plugin = load_plugin()
    assert plugin._CLARIFY_UI is None  # the S5 switch has not run
    on_interaction = wire(plugin, host_adapter())
    prompts.register(CID, KEY, QUESTION, CHOICES)
    try:
        interaction = Interaction(f"pbq:{CID}:0")
        asyncio.run(on_interaction(interaction))
        assert interaction.out == []  # silently not ours, as for any unknown family
        assert prompts.is_open(CID)
    finally:
        close()


def test_pbq_click_unauthorized_gets_the_alert_families_refusal(monkeypatch):
    plugin = load_plugin()
    opener = FakeOpener()
    ui = posted_question(opener)
    monkeypatch.setattr(plugin, "_CLARIFY_UI", ui)
    on_interaction = wire(plugin, host_adapter(allowed=False))
    try:
        interaction = Interaction(f"pbq:{CID}:0")
        asyncio.run(on_interaction(interaction))
        assert interaction.out == [("send", "Not allowed. Nothing changed.", True)]
        assert prompts.is_open(CID) and opener.patches() == []
    finally:
        close()


# ---------------------------------------------------------------------- typed answers (S4)


def make_event(text):
    source = S(platform=S(value="discord"), profile="3dprinting", user_id="1", chat_id="chan1",
               thread_id=None, is_bot=False)
    return S(source=source, text=text, internal=False)


class FakeGateway:
    def _is_user_authorized_for_source(self, source):
        return True

    def _session_key_for_source(self, source):
        return KEY

    def _delivery_adapter_for(self, source):
        return None


def test_typed_answer_dispatch_claims_the_live_key_prompt(monkeypatch):
    plugin = load_plugin()
    opener = FakeOpener()
    ui = posted_question(opener)
    monkeypatch.setattr(plugin, "_CLARIFY_UI", ui)
    result = asyncio.run(plugin.typed_answer_dispatch(make_event("3 copies"), FakeGateway()))
    assert result == {"action": "skip", "reason": "typed clarify answer handled"}
    assert prompts.wait_for_response(CID, 1) == "3 copies"
    patches = opener.patches()
    assert len(patches) == 1
    assert patches[0][2]["content"] == f"{QUESTION}\n> Answered by message: 3 copies"


def test_typed_answer_dispatch_falls_through_without_the_adapter():
    plugin = load_plugin()
    prompts.register(CID, KEY, QUESTION, CHOICES)
    try:
        # No parser prompt and no adapter installed: exactly today's
        # typed_quantity_dispatch outcome — the text is not claimed.
        result = asyncio.run(plugin.typed_answer_dispatch(make_event("3 copies"), FakeGateway()))
        assert result is None
        assert prompts.is_open(CID)
    finally:
        close()


def test_typed_answer_dispatch_falls_through_when_nothing_is_open(monkeypatch):
    plugin = load_plugin()
    opener = FakeOpener()
    monkeypatch.setattr(plugin, "_CLARIFY_UI", HermesClarifyAdapter(token="tok", opener=opener))
    result = asyncio.run(plugin.typed_answer_dispatch(make_event("hello"), FakeGateway()))
    assert result is None and opener.requests == []


def test_typed_answer_dispatch_is_not_registered_while_the_switch_is_off():
    # The S5 switch defaults off ([discord] clarify_ui unset): register()
    # keeps today's hook list byte-for-byte. The on-state swap is pinned in
    # test_clarify_wiring.py.
    plugin = load_plugin()
    ctx = mock.Mock()
    plugin.register(ctx)
    hooks = [item.args for item in ctx.register_hook.call_args_list]
    dispatch = [fn for name, fn in hooks if name == "pre_gateway_dispatch"]
    assert plugin.typed_quantity_dispatch in dispatch  # today's hook keeps its place
    assert plugin.typed_answer_dispatch not in dispatch  # the swap is the switch's, not the default


def test_typed_answer_dispatch_retires_a_parser_prompts_message(monkeypatch):
    """A parser-backed prompt (a quantity question) resolved by a typed
    reply must also retire its rendered question (P2-209): the registry
    resolution alone left the message's buttons standing, inviting a
    second answer into a prompt that no longer exists."""
    from print_handy.core import link_intake
    plugin = load_plugin()
    opener = FakeOpener()
    ui = posted_question(opener)
    monkeypatch.setattr(plugin, "_CLARIFY_UI", ui)
    # The switch's register() half: the own registry is the gateway, so a
    # parser prompt registered here is the one typed_reply resolves.
    monkeypatch.setattr(prompts, "_active", True)
    scope = link_intake._scope(make_event("3").source)
    monkeypatch.setattr(link_intake, "_typed_prompts",
                        {scope + (KEY,): (CID, lambda text: int(str(text).strip()))})
    result = asyncio.run(plugin.typed_answer_dispatch(make_event("3"), FakeGateway()))
    assert result == {"action": "skip", "reason": "typed print quantity handled"}
    assert prompts.wait_for_response(CID, 1) == "3"
    patches = opener.patches()
    assert len(patches) == 1
    assert patches[0][2]["content"] == f"{QUESTION}\n> Answered by message: 3"
    assert patches[0][2]["components"] == []
    # The resolved id was consumed by the dispatch, not left for a later reply.
    assert link_intake.take_resolved_prompt_id() is None
