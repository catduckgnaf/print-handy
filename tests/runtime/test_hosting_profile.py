"""#1817 M9: the Hermes hosting-profile gate is gone.

The handlers used to answer only sources stamped with the Hermes
hosting profile (``source.profile == "3dprinting"``, plus a
``profile_route_rejected`` flag). The bot is its own host, so the gate
now checks only that the source is a bot platform's human message: a
source carries no profile at all, and one that still has a profile
attribute is answered the same way.
"""
from pathlib import Path
from types import SimpleNamespace as S

from print_handy.bot import runtime
from print_handy.runtime import _config


def load_plugin():
    from print_handy.bot.handlers import common
    return common


def _event(source, internal=False):
    return S(source=source, internal=internal, text="hi")


def test_the_profile_names_are_gone():
    plugin = load_plugin()
    assert not hasattr(_config, "DEFAULT_PROFILE")
    assert not hasattr(_config, "hosting_profile")
    assert not hasattr(runtime, "PROFILE")
    assert not hasattr(plugin, "_is_hosting_profile")
    source = runtime.make_source("u", "c")
    assert not hasattr(source, "profile")
    assert not hasattr(source, "profile_route_rejected")


def test_gate_answers_bot_platform_human_messages_without_a_profile():
    plugin = load_plugin()
    assert plugin._is_print_discord_event(_event(runtime.make_source("u", "c")))
    for name in ("discord", "telegram", "matrix", "webui"):
        assert plugin._is_print_discord_event(_event(runtime.make_source("u", "c", platform_name=name)))
    stale = S(platform=S(value="discord"), profile="printshop", is_bot=False, user_id="u", chat_id="c")
    assert plugin._is_print_discord_event(_event(stale))


def test_gate_still_refuses_bots_internal_events_and_other_platforms():
    plugin = load_plugin()
    assert not plugin._is_print_discord_event(_event(runtime.make_source("u", "c", is_bot=True)))
    assert not plugin._is_print_discord_event(_event(runtime.make_source("u", "c"), internal=True))
    assert not plugin._is_print_discord_event(_event(runtime.make_source("u", "c", platform_name="slack")))
    assert not plugin._is_print_discord_event(S(source=None, internal=False, text="hi"))
