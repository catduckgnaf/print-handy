"""A pasted Discord message link re-runs that message's model link or file.

Seen live on 2.3.2: pasting the link to an earlier lantern message got "I didn't
understand that". The linked message's link or file now runs again, as the person
who pasted it, and only links into this server's print channels are followed.
"""
import asyncio
from types import SimpleNamespace as S
from unittest import mock

from corpus import fake_discord
from print_handy.bot import runtime

GUILD = 400000000000000004
MODEL = "https://makerworld.com/en/models/3483992-pinecone-cottage-lantern#profileId-3978285"


def run(coro):
    return asyncio.run(coro)


def setup(cfg):
    cfg["discord"]["allowed_user_ids"] = ""
    discord = fake_discord.FakeDiscord(auto=False, chat_id=int(cfg["discord"]["chat_id"]))
    store = {}

    async def fetch_message(message_id):
        if int(message_id) not in store:
            raise LookupError("Unknown Message")
        return store[int(message_id)]

    discord.channel.fetch_message = fetch_message
    core = runtime.BotCore(discord.client, cfg)

    def post(content, author=None):
        message = discord.message(content, author=author)
        message.guild = S(id=GUILD)
        store[message.id] = message
        return message

    return discord, core, post


def link(channel_id, message_id, guild=GUILD):
    return f"https://discord.com/channels/{guild}/{channel_id}/{message_id}"


def intake(core, message):
    seen = []

    async def fake_prepare(event, adapter, state, root, reuse=None, **kwargs):
        seen.append((event.text, event.source.user_id, event.message_id))
        return {"action": "skip", "reason": "test intake"}

    with mock.patch("print_handy.core.prepared_intake.prepare_link", fake_prepare), \
            mock.patch("print_handy.core.printer_state.load", return_value={}):
        run(core.handle_message(message))
    return seen


def test_parse_message_link_takes_only_a_bare_message_link():
    assert runtime.parse_message_link(" https://discord.com/channels/1/2/3 ") == ("1", "2", "3")
    assert runtime.parse_message_link("<https://ptb.discord.com/channels/1/2/3>") == ("1", "2", "3")
    assert runtime.parse_message_link("https://discordapp.com/channels/1/2/3/") == ("1", "2", "3")
    assert runtime.parse_message_link("print https://discord.com/channels/1/2/3") is None
    assert runtime.parse_message_link("https://discord.com/channels/1/2") is None
    assert runtime.parse_message_link(MODEL) is None


def test_a_link_to_an_earlier_model_message_runs_it_again_as_the_paster(cfg):
    discord, core, post = setup(cfg)
    first = post(MODEL)
    paster = fake_discord.FakeUser(200000000000000777, "member")
    again = post(link(discord.channel.id, first.id), author=paster)
    seen = intake(core, again)
    assert seen == [(MODEL, str(paster.id), str(again.id))]
    assert not any("didn't understand" in text for text in discord.sent)


def test_a_link_to_a_bot_post_says_so_and_runs_nothing(cfg):
    discord, core, post = setup(cfg)
    reply = post("Inspected 4 parts", author=discord.bot_user)
    seen = intake(core, post(link(discord.channel.id, reply.id)))
    assert seen == []
    assert any("one of my own posts" in text for text in discord.sent)


def test_a_link_into_another_server_or_channel_is_not_followed(cfg):
    discord, core, post = setup(cfg)
    first = post(MODEL)
    assert intake(core, post(link(discord.channel.id, first.id, guild=999))) == []
    assert any("outside this server" in text for text in discord.sent)
    assert intake(core, post(link(555000000000000555, first.id))) == []
    assert any("channel I do not print from" in text for text in discord.sent)


def test_a_deleted_message_says_it_could_not_be_opened(cfg):
    discord, core, post = setup(cfg)
    assert intake(core, post(link(discord.channel.id, 123456789))) == []
    assert any("could not open that message" in text for text in discord.sent)
