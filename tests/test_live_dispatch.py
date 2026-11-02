"""Offline tests for the /print-live handler (issue #311, stage 3).

No Discord and no network: the dispatch runs against a fake adapter that
records sends and a fake gateway, in the style of the /print-macros tests.
The handler must be Discord-only (Telegram and Matrix sources fall
through untouched), must refuse to start when [live_stream] says so,
and must never attempt a voice connection — start posts the Activity
pointer for the configured channel and drives the session machine.
"""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from print_handy import live_stream
from print_handy.hermes import _config

ROOT = Path(__file__).parents[1]
PLUGIN_PATH = ROOT / "plugins/print-feedback/__init__.py"
spec = importlib.util.spec_from_file_location("print_feedback_live", PLUGIN_PATH)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

ENABLED = {"enabled": True, "voice_channel_id": "123456789012345678",
           "auto_start_on_print": False, "max_height": 720, "fps": 15,
           "max_bitrate_kbps": 2500, "encoder": "auto", "empty_timeout_s": 300,
           "activity_origin": ""}


class FakeAdapter:
    """Records everything the dispatch tries to show the user."""

    def __init__(self):
        self.sent = []

    async def send(self, chat_id=None, content=None, metadata=None):
        self.sent.append(content)


def _event(text, platform="discord", user_id="42"):
    source = S(platform=S(value=platform), profile="3dprinting", is_bot=False,
               profile_route_rejected=False, user_id=user_id, chat_id="c", thread_id=None)
    return S(source=source, text=text, internal=False)


def _gateway(adapter, authorized=True):
    return S(_is_user_authorized_for_source=lambda s: authorized,
             _delivery_adapter_for=lambda s: adapter)


@pytest.fixture(autouse=True)
def _live_config():
    previous = _config.cfg()  # the session test settings from tests/conftest.py; restore, never clear
    _config.set_override({"live_stream": dict(ENABLED)})
    plugin._LIVE_SESSION = live_stream.LiveStreamSession()
    yield
    plugin._LIVE_SESSION = live_stream.LiveStreamSession()
    _config.set_override(previous)


def _run(text, adapter=None, platform="discord", authorized=True):
    adapter = adapter or FakeAdapter()
    out = asyncio.run(plugin.live_dispatch(
        _event(text, platform=platform), _gateway(adapter, authorized)))
    return out, adapter


def test_start_posts_the_activity_pointer_and_goes_live():
    out, adapter = _run("/print-live start")
    assert out == {"action": "skip", "reason": "print live started"}
    message = adapter.sent[-1]
    assert "<#123456789012345678>" in message and "activity" in message.lower()
    session = plugin._LIVE_SESSION
    assert session.is_live and session.started_by == "42"


def test_second_start_reports_the_running_session():
    _run("/print-live start")
    out, adapter = _run("/print-live start")
    assert out["reason"] == "print live already live"
    assert "is on" in adapter.sent[-1]


def test_stop_and_status_follow_the_session():
    out, adapter = _run("/print-live stop")
    assert out["reason"] == "print live not running"
    _run("/print-live start")
    out, adapter = _run("/print-live status")
    assert out["reason"] == "print live status shown" and "is on" in adapter.sent[-1]
    out, adapter = _run("/print-live stop")
    assert out["reason"] == "print live stopped"
    assert not plugin._LIVE_SESSION.is_live
    out, adapter = _run("/print-live")
    assert out["reason"] == "print live status shown"
    assert "ready but not running" in adapter.sent[-1]


def test_unknown_verb_shows_usage_and_changes_nothing():
    out, adapter = _run("/print-live dance")
    assert out["reason"] == "print live usage shown"
    assert not plugin._LIVE_SESSION.is_live


def test_start_refuses_when_disabled_or_unconfigured():
    _config.set_override({"live_stream": dict(ENABLED, enabled=False)})
    out, adapter = _run("/print-live start")
    assert out["reason"] == "print live not configured"
    assert "off" in adapter.sent[-1]
    assert not plugin._LIVE_SESSION.is_live
    _config.set_override({"live_stream": dict(ENABLED, voice_channel_id="")})
    out, adapter = _run("/print-live start")
    assert out["reason"] == "print live not configured"
    assert "no voice channel" in adapter.sent[-1]
    out, adapter = _run("/print-live status")
    assert "no voice channel" in adapter.sent[-1]


@pytest.mark.parametrize("platform", ["telegram", "matrix"])
def test_other_platforms_have_no_live_command(platform):
    adapter = FakeAdapter()
    out, _ = _run("/print-live start", adapter=adapter, platform=platform)
    assert out is None and adapter.sent == []
    assert not plugin._LIVE_SESSION.is_live


def test_unauthorised_users_and_other_text_fall_through():
    adapter = FakeAdapter()
    out, _ = _run("/print-live start", adapter=adapter, authorized=False)
    assert out is None and adapter.sent == []
    out, _ = _run("/print-livex", adapter=adapter)
    assert out is None and adapter.sent == []
    out, _ = _run("print-live start", adapter=adapter)
    assert out is None and adapter.sent == []
