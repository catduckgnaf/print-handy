"""#478 through the bot: /print-handy debug on|off|status over messages.

The replies run the real plugin closure (guided_dispatch -> guided
handle_text -> print_handy.debug_mode) against a fake Discord, with the
settings pointed at a temp profile so no real state folder is touched.
"""
import asyncio
import re
from pathlib import Path

import pytest

from print_handy import debug_mode
from print_handy.bot import guided, runtime
from print_handy.runtime import _config

from corpus import fake_discord


@pytest.fixture(autouse=True)
def _clean_guided_store():
    path = _config.state_dir() / guided.STORE_NAME
    path.unlink(missing_ok=True)
    yield
    path.unlink(missing_ok=True)


@pytest.fixture
def core(cfg, monkeypatch, tmp_path):
    cfg["paths"]["state_home"] = tmp_path / "profile"
    monkeypatch.setattr(_config, "cfg", lambda: cfg)
    monkeypatch.setattr(_config, "discord_token", lambda: "")
    discord = fake_discord.FakeDiscord(chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(cfg["discord"]["allowed_user_ids"]))
    return discord, runtime.BotCore(discord.client, cfg), cfg


def send(discord, core, text):
    before = len(discord.sent)
    out = asyncio.run(core.handle_message(discord.message(text)))
    return out, discord.sent[before:]


def test_debug_on_status_off_delivers_the_export_in_chat(core):
    discord, bot, cfg = core
    out, sent = send(discord, bot, "/print-handy debug on")
    assert out == {"action": "skip", "reason": "guided setup/debugging handled"}
    assert "Debug mode ON" in sent[-1] and "PH_DEBUG_CONFIGURATION" in sent[-1]
    assert debug_mode.status(cfg) is not None

    _, sent = send(discord, bot, "/print-handy debug status")
    assert "Debug mode is ON" in sent[-1]

    _, sent = send(discord, bot, "/print-handy debug off")
    reply = sent[-1]
    assert "Debug mode OFF" in reply
    assert "Sanitization report:" in reply  # the report is shown first
    assert "MEDIA:" not in reply  # the delivery layer turned it into a file
    attached = [f for message in discord.messages for f in message.files]
    assert len(attached) == 1, attached
    name = attached[0] if isinstance(attached[0], str) else attached[0].filename
    assert re.match(r"print-handy-debug-\d{4}-\d{2}-\d{2}-.+\.md$", name)
    from print_handy import settings as _settings
    target = _settings.state_dir(cfg) / "debug-exports" / name
    assert target.is_file()
    assert "DEBUG_SESSION_START" in target.read_text()
    assert debug_mode.status(cfg) is None  # and the mode is off afterwards


def test_debug_off_when_never_on_is_plain(core):
    discord, bot, _cfg = core
    _, sent = send(discord, bot, "/print-handy debug off")
    assert "already off" in sent[-1]


def test_failed_export_posts_a_pb_marker_and_keeps_the_mode_on(core, monkeypatch):
    discord, bot, cfg = core
    send(discord, bot, "/print-handy debug on")

    def boom(*args, **kwargs):
        raise RuntimeError("disk exploded at 192.168.1.9")

    monkeypatch.setattr(debug_mode, "off_and_export", boom)
    _, sent = send(discord, bot, "/print-handy debug off")
    assert "export failed" in sent[-1]
    assert "PH_DEBUG_UNKNOWN" in sent[-1]  # the operator-message marker
    assert "192.168.1.9" not in sent[-1]  # the reply itself is redacted
    assert debug_mode.status(cfg) is not None  # retry stays possible


def test_debug_bad_minutes_is_a_plain_refusal(core):
    discord, bot, cfg = core
    _, sent = send(discord, bot, "/print-handy debug on soon")
    assert "not a number of minutes" in sent[-1]
    assert debug_mode.status(cfg) is None


def test_bundle_command_uploads_the_bundle_in_chat(core):
    # #1020 C1: /print-handy bundle delivers the file in chat (the
    # delivery layer turns the reply's MEDIA: marker into an attachment),
    # not only a path in the state folder.
    discord, bot, cfg = core
    out, sent = send(discord, bot, "/print-handy bundle")
    assert out == {"action": "skip", "reason": "guided setup/debugging handled"}
    reply = sent[-1]
    assert "Diagnostics bundle" in reply
    assert "MEDIA:" not in reply  # consumed by the delivery layer
    attached = [f for message in discord.messages for f in message.files]
    assert len(attached) == 1, attached
    name = attached[0] if isinstance(attached[0], str) else attached[0].filename
    assert name == "diagnostics-bundle.md"
    from print_handy import settings as _settings
    target = _settings.state_dir(cfg) / "diagnostics-bundle.md"
    assert target.is_file()
    text = target.read_text()
    assert "## Runtime status" in text
    assert "## Listener stack dumps" in text
