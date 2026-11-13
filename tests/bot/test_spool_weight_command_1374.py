"""#1374: /spool weight corrects a spool's grams left from chat.

Driven through BotCore.handle_message on the fake Discord client (the
production chat entry), with a fake Spoolman behind the one transport
spool_sync uses. Nothing reaches a printer.
"""
import asyncio
from unittest import mock

from corpus import fake_discord
from print_handy.bot import runtime
from print_handy.bot.router import Router
from print_handy.core import spoolman_client


class FakeSpoolman:
    def __init__(self):
        self.spool = {"id": 19, "initial_weight": 1000.0, "remaining_weight": 0.0,
                      "used_weight": 1053.88, "archived": False,
                      "filament": {"name": "Bronze", "material": "PLA+"}}
        self.patches = []

    def __call__(self, method, path, body=None, **kw):
        if method == "GET" and path == "/api/v1/spool/19":
            return dict(self.spool)
        if method == "GET" and path.startswith("/api/v1/spool/"):
            return {}
        if method == "PATCH" and path == "/api/v1/spool/19":
            self.patches.append(body)
            self.spool["remaining_weight"] = body["remaining_weight"]
            return {}
        raise AssertionError(f"unexpected {method} {path}")


def _send(cfg, text):
    discord = fake_discord.FakeDiscord(auto=False, chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(cfg["discord"]["allowed_user_ids"]))
    core = runtime.BotCore(discord.client, cfg, router=Router())
    fake = FakeSpoolman()
    with mock.patch.object(spoolman_client, "request", fake):
        asyncio.run(core.handle_message(discord.message(text)))
    return discord.sent, fake


def test_spool_weight_sets_grams_left(cfg):
    sent, fake = _send(cfg, "/spool weight 19 932.4")
    assert fake.patches == [{"remaining_weight": 932.4}]
    assert sent == ["Spool #19 (Bronze PLA+) now has 932.4 g left (was 0 g)."]


def test_spool_weight_refuses_more_than_the_spool_started_with(cfg):
    sent, fake = _send(cfg, "/spool weight 19 1200")
    assert fake.patches == []
    assert "started at 1000 g" in sent[0] and "Nothing changed" in sent[0]


def test_spool_weight_unknown_spool_and_bad_form(cfg):
    sent, fake = _send(cfg, "/spool weight 404 10")
    assert fake.patches == [] and "no spool #404" in sent[0]
    sent, fake = _send(cfg, "/spool weight nineteen")
    assert fake.patches == [] and "/spool weight <spool#> <grams left>" in sent[0]


def test_plain_language_suggests_spool_weight():
    assert runtime.command_suggestion("fix the spool weight") == "/spool weight"
    assert runtime.command_suggestion("the filament weight is wrong") == "/spool weight"
