"""Offline contract tests for timelapse delivery through the bot's REST boundary (#524).

Hermes mode used to post through the `hermes send` CLI subprocess; the
listener now hands every Discord post to print_handy.bot.rest.send with
the bot token, in both modes. These tests pin that boundary: the exact
arguments rest.send receives, failure propagation, and honest refusal
(no token / no chat id) with no subprocess fallback.
"""
from print_handy.runtime import moonraker_event_listener as listener


def _no_subprocess(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("a subprocess was invoked for a listener post")

    monkeypatch.setattr(listener.subprocess, "run", boom)


def _wire_rest(monkeypatch, result):
    sent = []
    monkeypatch.setattr(listener.announcer, "DISCORD_CHAT_ID", "channel-42")
    monkeypatch.setattr(listener, "_bot_token", lambda: "tok-1")
    monkeypatch.setattr(listener, "log", lambda _line: None)

    def fake_send(token, chat, text):
        sent.append((token, chat, text))
        return result

    monkeypatch.setattr("print_handy.bot.rest.send", fake_send)
    return sent


def test_post_hands_media_marker_and_configured_target_to_rest(monkeypatch):
    sent = _wire_rest(monkeypatch, (True, "msg-1"))
    _no_subprocess(monkeypatch)
    message = "Print complete: Stand\nMEDIA:/cache/stand.discord.mp4"

    assert listener.post(message) is True
    assert sent == [("tok-1", "channel-42", message)]


def test_post_reports_rest_rejection_as_delivery_failure(monkeypatch):
    _wire_rest(monkeypatch, (False, "upload rejected"))
    _no_subprocess(monkeypatch)

    assert listener.post("Print complete: Stand\nMEDIA:/cache/stand.mp4") is False


def test_post_without_a_bot_token_fails_honestly_and_never_falls_back(monkeypatch):
    # The real rest.send refuses before any network call when the token is
    # empty; post() must surface that failure, not retry through a CLI.
    logs = []
    monkeypatch.setattr(listener.announcer, "DISCORD_CHAT_ID", "channel-42")
    monkeypatch.setattr(listener, "_bot_token", lambda: "")
    monkeypatch.setattr(listener, "log", logs.append)
    _no_subprocess(monkeypatch)

    assert listener.post("Print complete: Stand") is False
    assert any("no Discord bot token" in line for line in logs)


def test_post_without_a_chat_id_fails_honestly_and_never_falls_back(monkeypatch):
    logs = []
    monkeypatch.setattr(listener.announcer, "DISCORD_CHAT_ID", "")
    monkeypatch.setattr(listener, "_bot_token", lambda: "tok-1")
    monkeypatch.setattr(listener, "log", logs.append)
    _no_subprocess(monkeypatch)

    assert listener.post("Print complete: Stand") is False
    assert any("discord.chat_id is not configured" in line for line in logs)
