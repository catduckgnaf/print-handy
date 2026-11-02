"""The platform interface (#164 stage 1): a fake platform drives BotCore and the intake prompts.

The fake adapter is an in-memory ``BasePlatformAdapter``: no network, and it
never imports ``discord`` — its client is a plain stub, not a discord.py
client and not the corpus ``FakeDiscord``. Two drives:

* a full intake through the REAL plugin handlers and the real
  ``link_intake.ask`` prompt machinery (a model link, a button answer, an
  approval answer, completion), on a fake transport that presents as the
  Discord platform the handlers gate on today;
* the same core scoped to a differently-named platform (``fake``), proving
  session keys, sources and typed answers follow the platform name — the
  shape Telegram and Matrix plug into in later stages of #164.
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy import settings
from print_handy.bot import __main__ as bot_main
from print_handy.bot import platform, runtime
from print_handy.bot.platform import BasePlatformAdapter, SendResult
from print_handy.bot.rest import split_media
from print_handy.bot.router import Router
from print_handy.core import link_intake, prompts


class FakeClient:
    """A chat client stub: a bot user and a listener registry. No platform library."""

    def __init__(self):
        self.user = S(id=300000000000000003)
        self.listeners = {}

    def add_listener(self, func, name=None):
        self.listeners.setdefault(name or func.__name__, []).append(func)


class FakePlatformAdapter(BasePlatformAdapter):
    """In-memory transport: sends and prompts are recorded, presses are simulated."""

    name = "fake"
    message_limit = 100
    upload_max_bytes = 12345
    supports_buttons = True
    max_buttons = 10

    def __init__(self, allowed):
        super().__init__(allowed)
        self.sent = []       # {chat_id, content, files, reply_to, metadata}
        self.clarifies = []  # {clarify_id, question, choices}
        self.closed = []     # (clarify_id, note)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        body, files = split_media(content)
        self.sent.append({"chat_id": str(chat_id), "content": body,
                          "files": [str(p) for p in files], "reply_to": reply_to,
                          "metadata": metadata})
        return SendResult(True, message_id=str(len(self.sent)))

    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
        labels = [str(c) for c in choices][:self.max_buttons]
        self.clarifies.append({"clarify_id": str(clarify_id), "question": question, "choices": labels})
        self._record_prompt(clarify_id, {"choices": labels, "question": question})
        return SendResult(True, message_id=str(clarify_id))

    async def close_prompt(self, clarify_id, note):
        self._prompts.pop(str(clarify_id), None)
        self.closed.append((str(clarify_id), note))

    async def press(self, clarify_id, index, user_id):
        """A button press, the way a platform callback would deliver it."""
        label = await self.answer_choice(clarify_id, index, user_id=user_id)
        if label is not None:
            await self.close_prompt(clarify_id, f"chose **{label}**")
        return label


class FakeDiscordTransport(FakePlatformAdapter):
    """The fake transport presenting as Discord, the platform name the plugin handlers accept today."""

    name = "discord"
    message_limit = 2000


def run(coro):
    return asyncio.run(coro)


def message(text, user_id, chat_id):
    return S(author=S(id=int(user_id), bot=False), channel=S(id=int(chat_id)),
             content=text, id=42, attachments=[])


# ---------------------------------------------------------------------- capabilities and ids

def test_discord_adapter_reports_its_capabilities(cfg):
    from print_handy.bot.adapter import StandaloneAdapter
    adapter = StandaloneAdapter(FakeClient(), ["1"], upload_max_bytes=9500000)
    assert adapter.name == "discord" and adapter.message_limit == 2000
    assert adapter.supports_buttons is True and adapter.max_buttons == 25
    assert adapter.upload_max_bytes == 9500000
    assert isinstance(adapter, BasePlatformAdapter)


def test_live_stream_is_a_discord_only_capability():
    # #311: the live camera view is a Discord Activity. Discord opts in;
    # the base default and the Telegram/Matrix adapters stay False, so
    # those platforms expose no /print-live command or stub.
    from print_handy.bot import adapter as discord_adapter
    from print_handy.bot import matrix as matrix_adapter
    from print_handy.bot import telegram as telegram_adapter
    assert platform.BasePlatformAdapter.supports_live_stream is False
    assert discord_adapter.StandaloneAdapter.supports_live_stream is True
    assert telegram_adapter.StandaloneAdapter.supports_live_stream is False
    assert matrix_adapter.StandaloneAdapter.supports_live_stream is False


def test_serial_polling_is_a_telegram_and_matrix_trait():
    # P1-109: Telegram (getUpdates) and Matrix (/sync) drive messages from a
    # serial polling loop, so a wait inside message handling starves the very
    # loop that must deliver the answer; BotCore backgrounds the did-you-mean
    # wait for them. Discord dispatches each message independently.
    from print_handy.bot import adapter as discord_adapter
    from print_handy.bot import matrix as matrix_adapter
    from print_handy.bot import telegram as telegram_adapter
    assert platform.BasePlatformAdapter.polls_serially is False
    assert discord_adapter.StandaloneAdapter.polls_serially is False
    assert telegram_adapter.StandaloneAdapter.polls_serially is True
    assert matrix_adapter.StandaloneAdapter.polls_serially is True


def test_prompt_ids_round_trip_through_the_interface():
    cid = "ab" * 16
    assert platform.custom_id(cid, 3) == f"pbq:{cid}:3"
    assert platform.parse_custom_id(f"pbq:{cid}:3") == (cid, 3)
    assert platform.parse_custom_id("other:1") is None and platform.parse_custom_id("pbq:x:y") is None
    # The Discord adapter re-exports the same helpers the existing tests import.
    from print_handy.bot import adapter as discord_adapter
    assert discord_adapter.custom_id is platform.custom_id
    assert discord_adapter.parse_custom_id is platform.parse_custom_id
    assert discord_adapter.SendResult is platform.SendResult


def test_library_status_is_platform_reported():
    label, available, detail = platform.library_status("discord")
    assert label == "discord.py" and available is True and detail
    label, available, detail = platform.library_status("telegram")
    assert label == "telegram" and available is True and detail == "standard library client"
    label, available, detail = platform.library_status("matrix")
    assert label == "matrix" and available is True and detail == "standard library client"


# ---------------------------------------------------------------------- settings

def test_a_config_without_the_platform_key_still_yields_discord(tmp_path):
    home = Path(settings.HOME).read_text()
    legacy = tmp_path / "home.yaml"
    legacy.write_text(home.replace("  platform: discord\n", ""))
    data = settings.load(home=legacy, local=False, environ={})
    assert data["bot"]["platform"] == "discord"


def test_matrix_platform_validates_and_passes_the_platform_check(tmp_path):
    # Stage 1 pinned matrix as known-but-unimplemented; stage 3 (#164)
    # implements it, so the platform row now passes like telegram's.
    data = settings.load(local=False, environ={"PRINT_HANDY__BOT__PLATFORM": "matrix",
                                               "PRINT_HANDY__BOT__MODE": "standalone"})
    assert data["bot"]["platform"] == "matrix"
    rows = {label: (ok, detail) for label, ok, detail in bot_main.checks(data)}
    assert rows["platform"] == (True, "matrix")
    assert bot_main.check.__module__  # the check itself ran offline, above


def test_botcore_builds_the_matrix_adapter(cfg):
    cfg["bot"]["platform"] = "matrix"
    core = runtime.BotCore(FakeClient(), cfg, chat_id="!prints:example.org")
    assert core.adapter.name == "matrix" and core.adapter.uses_llm is False
    assert core.adapter.supports_buttons is False
    assert core.adapter.supports_live_stream is False


# ---------------------------------------------------------------------- full intake on a fake transport

def test_fake_transport_drives_a_full_intake_through_the_real_handlers(cfg):
    chat_id = cfg["discord"]["chat_id"]
    user_id = cfg["discord"]["allowed_user_ids"]
    adapter = FakeDiscordTransport([user_id])
    client = FakeClient()
    core = runtime.BotCore(client, cfg, adapter=adapter, router=Router())
    assert core.platform_name == "discord" and core.chat_id == chat_id
    seen = {}

    async def fake_prepare(event, adapter_arg, state, root, reuse=None, **kwargs):
        seen["adapter"] = adapter_arg
        source = event.source
        key = runtime.session_key(source)
        color = await link_intake.ask(adapter_arg, source, key, "Which color?", ["Black", "White"])
        await adapter_arg.send(chat_id=source.chat_id, content=f"Sliced in {['Black', 'White'][color]}.")
        start = await link_intake.ask(adapter_arg, source, key, "Start the print?", ["Print it"])
        seen["answers"] = (color, start)
        await adapter_arg.send(chat_id=source.chat_id, content="Started.")
        return {"action": "skip", "reason": "test intake"}

    async def flow():
        task = asyncio.create_task(core.handle_message(
            message("https://makerworld.com/en/models/931302-holder", user_id, chat_id)))
        for expected in ("Which color?", "Start the print?"):
            for _ in range(200):
                row = next((c for c in adapter.clarifies if c["question"].startswith(expected)), None)
                if row is not None:
                    break
                await asyncio.sleep(0.01)
            assert row is not None, f"no prompt {expected!r}: {adapter.clarifies}"
            if expected == "Which color?":
                # A stranger's press resolves nothing.
                assert await adapter.press(row["clarify_id"], 0, user_id="999") is None
                assert prompts.is_open(row["clarify_id"])
                assert await adapter.press(row["clarify_id"], 1, user_id=user_id) == "White"
            else:
                assert await adapter.press(row["clarify_id"], 0, user_id=user_id) == "Print it"
        return await task

    with mock.patch("print_handy.core.prepared_intake.prepare_link", fake_prepare), \
            mock.patch("print_handy.core.printer_state.load", return_value={}):
        out = run(flow())
    assert out == {"action": "skip", "reason": "test intake"}
    assert seen["adapter"] is adapter and seen["answers"] == (1, 0)
    texts = [row["content"] for row in adapter.sent]
    assert "Sliced in White." in texts and texts[-1] == "Started."
    assert [note for _, note in adapter.closed] == ["chose **White**", "chose **Print it**"]


def test_a_named_fake_platform_scopes_sessions_and_typed_answers(cfg):
    chat_id, user_id = "555", "777"
    adapter = FakePlatformAdapter([user_id])
    core = runtime.BotCore(FakeClient(), cfg, adapter=adapter, platform_name="fake",
                           chat_id=chat_id, router=_NullRouter())
    source = runtime.make_source(user_id, chat_id, platform_name="fake")
    assert runtime.platform_name_of(source) == "fake"
    assert runtime.session_key(source) == f"fake:{chat_id}::{user_id}"

    async def flow():
        cid = "typed-fake-1"
        prompts.register(cid, runtime.session_key(source), "How many copies?", ["1", "2"])
        await adapter.send_clarify(chat_id=chat_id, question="How many copies?", choices=["1", "2"],
                                   clarify_id=cid, session_key="flow")
        out = await core.process(runtime.make_event(source, "2", 7))
        assert out == {"action": "skip", "reason": "typed answer to an open question"}
        return await asyncio.to_thread(prompts.wait_for_response, cid, 1)

    assert run(flow()) == "2"
    assert ("typed-fake-1", "Answered by message: 2") in adapter.closed


class _NullRouter:
    """No handler takes the message: BotCore falls through to the typed-answer path."""

    def attach_platform(self, platform_name, client, adapter):
        pass

    @property
    def dispatch_hooks(self):
        return []

    def handler_names(self):
        return []

    async def dispatch(self, event, gateway):
        return None
