"""Terminal posts mention the starter on every platform, and carry one joke at most (#1332).

#1029 left two gaps: Telegram and Matrix terminal posts never mentioned
the starter (only the Discord send path took ``mention_user_id``), and the
Discord mention sat mid-post after the status roast. The second was fixed
by #1095 (the rendered text never carries a mention; ``rest.send`` puts
the one ``<@id>`` at the front), and is pinned here through the listener.
#1017's failed, cancelled and spaghetti roast banks are wired into the
Moonraker listener's posts (#1023); #1326 gave the family listeners'
plain terminal posts one status quip instead, so no post carries both.
"""
import json

import pytest

from print_handy.bot import matrix_rest, rest, telegram_rest
from print_handy.hermes import _config, starter_pings
from print_handy.hermes import discord_print_summary as summary
from print_handy.hermes import moonraker_event_listener as mel

FILE = "widget.gcode"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(starter_pings, "STATE", tmp_path / "starter_pings.json")
    monkeypatch.setattr(mel, "ROAST_STATE", tmp_path / "roast.json")
    monkeypatch.setitem(_config.cfg()["discord"], "mention_starter", True)


def _job(platform, user_id):
    return {"title": "Widget", "gcode": "/archive/widget/plate.gcode",
            "started_by": {"platform": platform, "user_id": user_id}}


def _platform(monkeypatch, name, job):
    monkeypatch.setitem(_config.cfg()["bot"], "platform", name)
    monkeypatch.setattr(mel, "_platform_name", lambda: name)
    monkeypatch.setattr(summary, "find_job", lambda *a, **k: (job, {}))


def test_1332_a_telegram_cancel_post_mentions_its_starter(monkeypatch):
    _platform(monkeypatch, "telegram", _job("telegram", "424242"))
    monkeypatch.setattr(mel, "_telegram_chat_id", lambda: "-100777")
    monkeypatch.setattr(mel, "_telegram_token", lambda: "123456789:AA" + "x" * 33)
    sent = []
    monkeypatch.setattr(telegram_rest, "call",
                        lambda token, method, payload=None, files=(), **kw: sent.append((method, payload))
                        or {"message_id": 5})
    assert mel.post("Print cancelled: Widget", ping_kind="cancelled", ping_job_key=FILE)
    (method, payload), = sent
    assert method == "sendMessage"
    assert payload["text"].startswith("Starter: Print cancelled")
    assert payload["entities"] == [{"type": "text_mention", "offset": 0, "length": 7,
                                    "user": {"id": 424242, "is_bot": False, "first_name": "Starter"}}]


def test_1332_a_matrix_error_post_mentions_its_starter(monkeypatch):
    _platform(monkeypatch, "matrix", _job("matrix", "@sam:example.org"))
    monkeypatch.setattr(mel, "_matrix_room_id", lambda: "!room:example.org")
    monkeypatch.setattr(mel, "_matrix_homeserver", lambda: "https://matrix.example.org")
    monkeypatch.setattr(mel, "_matrix_token", lambda: "syt_test")
    events = []
    monkeypatch.setattr(matrix_rest, "send_event",
                        lambda hs, token, room, kind, content, *a, **k: events.append(content)
                        or {"event_id": "$e"})
    assert mel.post("Print failed: Widget", ping_kind="error", ping_job_key=FILE)
    assert events[0]["body"].startswith("@sam:example.org Print failed")
    assert events[0]["m.mentions"] == {"user_ids": ["@sam:example.org"]}


def test_1332_a_starter_from_another_platform_is_never_mentioned(monkeypatch):
    # A Discord starter on a Telegram install names nobody there, and so on.
    _platform(monkeypatch, "telegram", _job("discord", "111222333444555666"))
    assert starter_pings.ping_for("complete", job_key=FILE) == ""
    assert summary.starter_user_id(_job("matrix", "not-an-id"), "matrix") == ""
    assert summary.starter_user_id(_job("telegram", "12ab"), "telegram") == ""


def test_1332_the_setting_still_turns_every_platform_off(monkeypatch):
    _platform(monkeypatch, "matrix", _job("matrix", "@sam:example.org"))
    monkeypatch.setitem(_config.cfg()["discord"], "mention_starter", False)
    assert starter_pings.ping_for("complete", job_key=FILE) == ""


def test_1332_the_discord_mention_leads_the_cancel_post_even_with_its_roast(monkeypatch):
    # #1029's mid-post mention: the roast is the last line, the mention the first.
    _platform(monkeypatch, "discord", _job("discord", "111222333444555666"))
    monkeypatch.setattr(mel, "generate_roast", lambda *a, **k: "ROAST LINE")
    monkeypatch.setattr(mel, "capture_stop", lambda *a, **k: "")
    posted = []
    monkeypatch.setattr(rest, "send", lambda token, chat, text, **kw: posted.append(
        rest.starter_mention(text, kw.get("mention_user_id"))[0]) or (True, "1"))
    mel.post_cancel_report(FILE)
    lines = posted[0].splitlines()
    assert lines[0].startswith("<@111222333444555666> ")
    assert lines[-1] == "ROAST LINE"
    assert sum("<@" in line for line in lines) == 1


def test_1332_no_terminal_post_carries_both_a_roast_and_a_status_quip(monkeypatch):
    from print_handy.core import status_voice
    from print_handy.hermes import family_control

    quips, roasts = [], []
    monkeypatch.setattr(status_voice, "voiced", lambda text, *a, **k: quips.append(text) or text + "\nQUIP")
    monkeypatch.setattr(mel, "generate_roast", lambda *a, **k: roasts.append(a) or "ROAST")
    monkeypatch.setattr(mel, "capture_stop", lambda *a, **k: "")
    posts = []
    monkeypatch.setattr(mel, "post", lambda text, **kw: posts.append(text) or True)
    # The Moonraker listener's cancel post: its bank roast, no quip.
    mel.post_cancel_report(FILE)
    assert roasts and not quips
    assert "ROAST" in posts[-1] and "QUIP" not in posts[-1]
    roasts.clear()
    # A family listener's error post: one quip, no roast.
    monkeypatch.setattr(family_control, "_mel", lambda: mel)
    family_control.post_family_event("error", FILE)
    assert quips and not roasts
    assert "QUIP" in posts[-1] and "ROAST" not in posts[-1]
    json.dumps(posts)  # plain text only
