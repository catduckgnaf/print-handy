"""Starter mentions: a terminal post @mentions only who started the print.

The job carries an intake-stamped ``started_by`` (platform + user ID). The
shared format_event renderer appends a ``<@id>`` line to terminal kinds
(complete / cancelled / error) only, the listener's Discord REST sender
allows exactly that user to ping, and every other post — start, pause,
another person's print, a print started outside the bot — mentions nobody.
"""
import copy
import io
import json

import pytest

from print_handy import settings
from print_handy.bot import rest
from print_handy.hermes import _config
from print_handy.hermes import discord_print_summary as summary
from print_handy.hermes import moonraker_event_listener as mel
from print_handy.hermes import print_records

STARTER = "111222333444555666"


@pytest.fixture
def archive(tmp_path, monkeypatch):
    """An archive holding one sliced job, stamped with a Discord starter."""
    folder = tmp_path / "archive" / "widget--abc"
    folder.mkdir(parents=True)
    gcode = folder / "plate.gcode"
    gcode.write_text("; estimated printing time (normal mode) = 1h 0m\n"
                     "; total filament used [g] = 12\n")
    job = {"title": "Widget", "gcode": str(gcode),
           "started_by": {"platform": "discord", "user_id": STARTER}}
    (folder / "JOB.json").write_text(json.dumps(job))
    monkeypatch.setattr(summary, "ARCHIVE", tmp_path / "archive")
    monkeypatch.setattr(summary, "CONTEXT_PATH", tmp_path / "no-context.json")
    return tmp_path / "archive"


def _with_config(**sections):
    """Install a settings copy with the given section keys; returns a restore callable."""
    previous = copy.deepcopy(_config.cfg())
    data = copy.deepcopy(previous)
    for section, values in sections.items():
        data.setdefault(section, {}).update(values)
    _config.set_override(data)
    return lambda: _config.set_override(previous)


def _event(archive, kind):
    return summary.format_event(kind, "plate.gcode", archive=archive, emit=False)


# ---------------------------------------------------------- the intake stamp

def test_stamp_started_by_records_the_requester():
    from types import SimpleNamespace as S

    from print_handy.core import prepared_intake

    job = {}
    prepared_intake.stamp_started_by(
        job, S(user_id=STARTER, platform=S(value="discord")))
    assert job["started_by"] == {"platform": "discord", "user_id": STARTER}
    # A re-intake by someone else re-stamps: they started the new print.
    prepared_intake.stamp_started_by(
        job, S(user_id="999888777666555444", platform=S(value="discord")))
    assert job["started_by"]["user_id"] == "999888777666555444"
    # No user id: no stamp, and an existing stamp is left alone.
    job2 = {}
    prepared_intake.stamp_started_by(job2, S(user_id="", platform=S(value="discord")))
    assert "started_by" not in job2
    prepared_intake.stamp_started_by(job, S(user_id=None, platform=None))
    assert job["started_by"]["user_id"] == "999888777666555444"


# ------------------------------------------------------------ the renderer

@pytest.mark.parametrize("kind", ["complete", "cancelled", "error"])
def test_terminal_posts_mention_only_the_starter(archive, kind):
    text = _event(archive, kind)
    assert text.splitlines()[-1] == f"<@{STARTER}>"
    assert text.count("<@") == 1


@pytest.mark.parametrize("kind", ["started", "paused", "slice"])
def test_non_terminal_posts_mention_nobody(archive, kind):
    assert "<@" not in _event(archive, kind)


def test_no_starter_stamp_mentions_nobody(archive):
    job_path = archive / "widget--abc" / "JOB.json"
    job = json.loads(job_path.read_text())
    del job["started_by"]
    job_path.write_text(json.dumps(job))
    assert "<@" not in _event(archive, "complete")


def test_starter_from_another_platform_mentions_nobody(archive):
    job_path = archive / "widget--abc" / "JOB.json"
    job = json.loads(job_path.read_text())
    job["started_by"] = {"platform": "telegram", "user_id": STARTER}
    job_path.write_text(json.dumps(job))
    assert "<@" not in _event(archive, "complete")


def test_non_numeric_starter_id_mentions_nobody(archive):
    job_path = archive / "widget--abc" / "JOB.json"
    job = json.loads(job_path.read_text())
    job["started_by"] = {"platform": "discord", "user_id": "not-a-user-id"}
    job_path.write_text(json.dumps(job))
    assert "<@" not in _event(archive, "complete")


def test_setting_off_mentions_nobody(archive):
    restore = _with_config(discord={"mention_starter_on_complete": False})
    try:
        assert "<@" not in _event(archive, "complete")
    finally:
        restore()


def test_telegram_install_mentions_nobody(archive):
    restore = _with_config(bot={"platform": "telegram"})
    try:
        assert "<@" not in _event(archive, "complete")
    finally:
        restore()


def test_unknown_job_mentions_nobody(archive):
    assert "<@" not in summary.format_event("complete", "nope.gcode", archive=archive, emit=False)


# ------------------------------------------------------- the mention reader

def test_mention_user_id_reads_only_a_standalone_mention_line():
    assert summary.mention_user_id(f"Print complete: Widget\n<@{STARTER}>") == STARTER
    assert summary.mention_user_id("Print complete: Widget") is None
    # A mention buried in prose (a model title, a report line) is not a ping.
    assert summary.mention_user_id(f"Print complete: <@{STARTER}> fan mount") is None
    assert summary.mention_user_id(f"see <@{STARTER}>\nPrint complete") is None
    assert summary.mention_user_id("<@123>") is None  # too short to be a user ID


# ------------------------------------------------------------ the REST send

class _Opener:
    def __init__(self):
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        return io.BytesIO(json.dumps({"id": "1"}).encode())


def test_rest_send_allows_only_the_named_user_to_ping():
    opener = _Opener()
    ok, _ = rest.send("tok", "123", f"Done\n<@{STARTER}>", opener=opener,
                      mention_user_id=STARTER)
    assert ok
    payload = json.loads(opener.requests[0].data)
    assert payload["allowed_mentions"] == {"parse": [], "users": [STARTER]}


def test_rest_send_without_a_mention_pings_nobody():
    opener = _Opener()
    ok, _ = rest.send("tok", "123", "Done", opener=opener)
    assert ok
    payload = json.loads(opener.requests[0].data)
    assert payload["allowed_mentions"] == {"parse": []}


def test_rest_send_ignores_a_non_numeric_mention_user_id():
    opener = _Opener()
    ok, _ = rest.send("tok", "123", "Done", opener=opener, mention_user_id="garbage")
    assert ok
    payload = json.loads(opener.requests[0].data)
    assert payload["allowed_mentions"] == {"parse": []}


def test_listener_discord_post_threads_the_starter_mention(monkeypatch):
    calls = []

    def fake_send(token, channel_id, text, **kwargs):
        calls.append(kwargs)
        return True, "1"

    monkeypatch.setattr(rest, "send", fake_send)
    monkeypatch.setattr(mel, "_bot_token", lambda: "tok")
    assert mel._post_rest_discord(f"Print complete: Widget\n<@{STARTER}>")
    assert calls[-1] == {"mention_user_id": STARTER}
    # A post with no starter mention keeps the plain three-argument call.
    assert mel._post_rest_discord("Print started: Widget")
    assert calls[-1] == {}


# ------------------------------------------------------------- the record

def test_print_record_carries_the_starter_from_the_start_context():
    data = print_records.build_record_data(
        "plate.gcode", stats={"state": "complete"},
        context={"job_key": "plate.gcode", "title": "Widget",
                 "started_by": {"platform": "discord", "user_id": STARTER}},
        completed_at="2026-11-07T00:00:00Z")
    assert data["started_by"] == {"platform": "discord", "user_id": STARTER}
    record = print_records._record_json(data)
    assert record["started_by"] == {"platform": "discord", "user_id": STARTER}


# -------------------------------------------------------------- the setting

def test_mention_setting_defaults_on_and_validates(tmp_path):
    data = settings.load(local=False, environ={})
    assert data["discord"]["mention_starter_on_complete"] is True
    path = tmp_path / "local.yaml"
    path.write_text("discord:\n  mention_starter_on_complete: false\n")
    data = settings.load(local=path, environ={})
    assert data["discord"]["mention_starter_on_complete"] is False
    path.write_text("discord:\n  mention_starter_on_complete: maybe\n")
    with pytest.raises(settings.SettingsError, match="mention_starter_on_complete"):
        settings.load(local=path, environ={})
    data = settings.load(local=False,
                         environ={"PRINT_HANDY__DISCORD__MENTION_STARTER_ON_COMPLETE": "false"})
    assert data["discord"]["mention_starter_on_complete"] is False


# ------------------------------------------------- button posts (follow-up)

def test_button_post_threads_the_starter_mention(monkeypatch):
    """A terminal alert posted with buttons (the firmware-shutdown post
    carries the error event text) pings its starter like a plain post."""
    from print_handy.hermes import print_alert_buttons as pab
    calls = []

    def fake_buttons(token, channel_id, content, kind, filename, **kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(pab, "post_with_buttons", fake_buttons)
    monkeypatch.setattr(mel, "_bot_token", lambda: "tok")
    monkeypatch.setattr(mel, "_buttonless_standalone", lambda: False)
    monkeypatch.setattr(mel, "log", lambda line: None)
    assert mel.post_alert(f"**Printer firmware stopped.**\nPrint stopped with an error\n<@{STARTER}>",
                          "shutdown", "plate.gcode")
    assert calls[-1] == {"mention_user_id": STARTER}
    # A pause alert has no starter line and keeps the plain call shape.
    assert mel.post_alert("Print paused: Widget", "paused", "plate.gcode")
    assert calls[-1] == {}


def test_button_post_allows_only_the_named_user_to_ping(monkeypatch, tmp_path):
    from print_handy.hermes import print_alert_buttons as pab
    seen = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"{}"

    def fake_open(request, timeout):
        seen.append(json.loads(request.data))
        return Response()

    monkeypatch.setattr(pab, "STATE", tmp_path / "alerts.json")
    monkeypatch.setattr(pab.urllib.request, "urlopen", fake_open)
    assert pab.post_with_buttons("tok", "42", f"stopped\n<@{STARTER}>", "shutdown", "plate.gcode",
                                 mention_user_id=STARTER)
    assert seen[-1]["allowed_mentions"] == {"parse": [], "users": [STARTER]}
    assert pab.post_with_buttons("tok", "42", "paused", "paused", "plate.gcode")
    assert seen[-1]["allowed_mentions"] == {"parse": []}
    assert pab.post_with_buttons("tok", "42", "x", "shutdown", "plate.gcode", mention_user_id="<@1>")
    assert seen[-1]["allowed_mentions"] == {"parse": []}
