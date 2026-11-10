"""The typing / working indicator: shown for an accepted message, never for an ignored one.

It is cosmetic. It must never block the handler and never raise into it.
"""
import asyncio
from types import SimpleNamespace as S
from unittest import mock

from corpus import fake_discord
from print_handy.bot import matrix, matrix_rest, runtime, telegram
from print_handy.webui import chat

OPERATOR = "200000000000000002"
STRANGER = "200000000000000777"


def _core(cfg, allowed=OPERATOR):
    cfg["discord"]["allowed_user_ids"] = allowed
    discord = fake_discord.FakeDiscord(auto=False, chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(OPERATOR))
    return discord, runtime.BotCore(discord.client, cfg)


def _handle(core, message):
    async def scenario():
        out = await core.handle_message(message)
        if core._signal_tasks:
            await asyncio.gather(*core._signal_tasks)
        return out
    return asyncio.run(scenario())


def _record_typing(discord):
    typed = []

    def typing():
        async def once():
            typed.append(discord.channel.id)
        return once()

    discord.channel.typing = typing
    return typed


def test_an_accepted_message_shows_discord_typing_and_the_reply_is_unchanged(cfg):
    discord, core = _core(cfg)
    typed = _record_typing(discord)
    out = _handle(core, discord.message("/print-help"))
    assert out == {"action": "skip", "reason": "help"}
    assert typed == [discord.channel.id]
    assert discord.sent and discord.sent[-1].startswith("**Print Handy**")


def test_ignored_messages_never_show_typing(cfg):
    discord, core = _core(cfg)
    typed = _record_typing(discord)
    # Another bot, the bot itself, an unrouted channel, a refused user, an empty message.
    _handle(core, discord.message("/print-help", author=fake_discord.FakeUser(9, "other", bot=True)))
    _handle(core, discord.message("/print-help", author=discord.bot_user))
    elsewhere = discord.add_channel(555)
    elsewhere.typing = discord.channel.typing
    _handle(core, discord.message("/print-help", channel=elsewhere))
    assert _handle(core, discord.message("/print-help",
                                         author=fake_discord.FakeUser(int(STRANGER), "x"))) is None
    _handle(core, discord.message(""))
    assert typed == []


def test_a_failing_or_hanging_indicator_never_blocks_or_breaks_the_handler(cfg, monkeypatch):
    discord, core = _core(cfg)

    def broken():
        raise RuntimeError("discord down")

    discord.channel.typing = broken
    assert _handle(core, discord.message("/print-help")) == {"action": "skip", "reason": "help"}

    never = asyncio.Event

    def hangs():
        async def wait():
            await never().wait()
        return wait()

    discord.channel.typing = hangs
    monkeypatch.setattr(runtime, "WORKING_SIGNAL_TIMEOUT_S", 0.05)

    async def scenario():
        out = await asyncio.wait_for(core.handle_message(discord.message("/print-help")), 2)
        await asyncio.gather(*core._signal_tasks)  # times out quietly, never raises
        return out

    assert asyncio.run(scenario()) == {"action": "skip", "reason": "help"}

    core.adapter = object()  # an adapter with no hook at all
    core._signal_working(S(chat_id="1", thread_id=None))  # a no-op, not an error
    # Called with no running loop (task creation fails): still never raises.
    core.adapter = S(signal_working=lambda *a, **k: None)
    core._signal_working(S(chat_id="1", thread_id=None))


def test_telegram_sends_a_typing_chat_action_in_the_thread():
    adapter = telegram.StandaloneAdapter(None, ["1"], token="t")
    calls = []
    adapter._call = lambda method, payload=None, files=(): calls.append((method, payload))
    asyncio.run(adapter.signal_working("-100", metadata={"thread_id": "7"}))
    assert calls == [("sendChatAction", {"chat_id": "-100", "action": "typing", "message_thread_id": "7"})]


def test_matrix_sends_a_typing_notification_for_the_bot_account():
    client = S(bot_user_id="@bot:example.org", user=None)
    adapter = matrix.StandaloneAdapter(client, ["@a:example.org"], homeserver="https://hs", token="tok")
    seen = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def opener(req, timeout=None):
        seen.append((req.get_method(), req.full_url, req.data))
        return Resp()

    adapter.opener = opener
    asyncio.run(adapter.signal_working("!room:example.org"))
    method, url, data = seen[0]
    assert method == "PUT"
    assert url == ("https://hs/_matrix/client/v3/rooms/%21room%3Aexample.org/typing/"
                   "%40bot%3Aexample.org")
    assert data == b'{"typing": true, "timeout": 10000}'
    # Without a known bot account nothing is sent.
    seen.clear()
    adapter.client = S(bot_user_id="", user=None)
    asyncio.run(adapter.signal_working("!room:example.org"))
    assert seen == []
    assert matrix_rest.typing  # the REST helper is public


def test_web_chat_page_shows_working_while_a_turn_runs_and_not_after():
    class Service:
        def __init__(self, busy, prompt=None):
            self._busy, self._prompt = busy, prompt

        def busy(self):
            return self._busy

        def open_prompt(self):
            return self._prompt

        def entries(self):
            return [{"seq": 1, "kind": "operator", "content": "<b>hi</b>"}]

    page = chat.render_chat_page(Service(True), form_token="t")
    assert chat.WORKING_TEXT in page and "&lt;b&gt;hi&lt;/b&gt;" in page
    assert chat.WORKING_TEXT not in chat.render_chat_page(Service(False), form_token="t")
    waiting = {"clarify_id": "c", "question": "Pick", "choices": ["A"]}
    assert chat.WORKING_TEXT not in chat.render_chat_page(Service(True, waiting), form_token="t")
