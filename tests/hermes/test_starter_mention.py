"""Starter mentions: who started a print is the one person pinged (#1029, #1095).

The job carries an intake-stamped ``started_by`` (platform + user ID).
Since #1095 the rendered text of a post never carries a mention, on any
platform: the starter pings classifier (starter_pings) decides at the
send path, from the post kind and the job, whether the Discord send may
ping the starter — terminal posts and the alerts that need a person do,
everything else (start, slice, setup, choice prompts, progress, roasts)
does not, and an alert pings once per episode. This file covers the
stamp, the mention-free renderer, the send-path threading, and the
setting (including the pre-#1095 key spelling it replaced).
"""
import copy
import io
import json

import pytest

from print_handy import settings
from print_handy.bot import rest
from print_handy.runtime import _config
from print_handy.runtime import discord_print_summary as summary
from print_handy.runtime import moonraker_event_listener as mel
from print_handy.runtime import print_records
from print_handy.runtime import starter_pings

STARTER = "111222333444555666"


@pytest.fixture(autouse=True)
def ping_state(tmp_path, monkeypatch):
    """Episode tracking lives in a throwaway state file per test."""
    monkeypatch.setattr(starter_pings, "STATE", tmp_path / "starter_pings.json")
    return tmp_path / "starter_pings.json"


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

@pytest.mark.parametrize("kind", ["complete", "cancelled", "error",
                                  "started", "paused", "slice"])
def test_rendered_posts_carry_no_mention(archive, kind):
    """No post text carries a <@id> line any more (#1095): the ping is a
    send-path decision, so the same text is safe on every platform and
    in the MQTT/ntfy copy of the event."""
    assert "<@" not in _event(archive, kind)


def test_unknown_job_mentions_nobody(archive):
    assert "<@" not in summary.format_event("complete", "nope.gcode", archive=archive, emit=False)


# ------------------------------------------------- the classifier decision

@pytest.mark.parametrize("kind", ["complete", "cancelled", "error"])
def test_terminal_kinds_ping_the_starter(archive, kind):
    assert starter_pings.ping_for(kind, job_key="plate.gcode") == STARTER


@pytest.mark.parametrize("kind", ["watchdog", "paused", "shutdown", "alert"])
def test_alert_kinds_ping_the_starter(archive, kind):
    assert starter_pings.ping_for(kind, job_key="plate.gcode", now=1000.0) == STARTER


@pytest.mark.parametrize("kind", ["started", "slice", "progress", "heating",
                                  "calibration", "confirm", "setup", "choice",
                                  "roast", "dark", ""])
def test_routine_kinds_ping_nobody(archive, kind):
    """The bag-clips rule: setup and choice back-and-forth — picking a
    quantity included — slicing, start, progress and roast posts never
    ping, however the send path labels them."""
    assert starter_pings.ping_for(kind, job_key="plate.gcode") == ""


def test_no_starter_stamp_pings_nobody(archive):
    job_path = archive / "widget--abc" / "JOB.json"
    job = json.loads(job_path.read_text())
    del job["started_by"]
    job_path.write_text(json.dumps(job))
    assert starter_pings.ping_for("complete", job_key="plate.gcode") == ""
    assert starter_pings.ping_for("watchdog", job_key="plate.gcode") == ""


def test_starter_from_another_platform_pings_nobody(archive):
    job_path = archive / "widget--abc" / "JOB.json"
    job = json.loads(job_path.read_text())
    job["started_by"] = {"platform": "telegram", "user_id": STARTER}
    job_path.write_text(json.dumps(job))
    assert starter_pings.ping_for("complete", job_key="plate.gcode") == ""


def test_non_numeric_starter_id_pings_nobody(archive):
    job_path = archive / "widget--abc" / "JOB.json"
    job = json.loads(job_path.read_text())
    job["started_by"] = {"platform": "discord", "user_id": "not-a-user-id"}
    job_path.write_text(json.dumps(job))
    assert starter_pings.ping_for("complete", job_key="plate.gcode") == ""


def test_setting_off_pings_nobody(archive):
    restore = _with_config(discord={"mention_starter": False})
    try:
        assert starter_pings.ping_for("complete", job_key="plate.gcode") == ""
        assert starter_pings.ping_for("watchdog", job_key="plate.gcode") == ""
    finally:
        restore()


def test_telegram_install_pings_nobody(archive):
    restore = _with_config(bot={"platform": "telegram"})
    try:
        assert starter_pings.ping_for("complete", job_key="plate.gcode") == ""
    finally:
        restore()


# ------------------------------------------------------------ the REST send

class _Opener:
    def __init__(self):
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        return io.BytesIO(json.dumps({"id": "1"}).encode())


def test_rest_send_allows_only_the_named_user_to_ping():
    opener = _Opener()
    ok, _ = rest.send("tok", "123", "Done", opener=opener,
                      mention_user_id=STARTER)
    assert ok
    payload = json.loads(opener.requests[0].data)
    assert payload["allowed_mentions"] == {"parse": [], "users": [STARTER]}
    # Discord's allowed_mentions only filters mentions present in the
    # content: the ping must be in the posted text or nobody is notified.
    assert payload["content"] == f"<@{STARTER}> Done"


def test_rest_send_without_a_mention_pings_nobody():
    opener = _Opener()
    ok, _ = rest.send("tok", "123", "Done", opener=opener)
    assert ok
    payload = json.loads(opener.requests[0].data)
    assert payload["allowed_mentions"] == {"parse": []}
    assert payload["content"] == "Done"


@pytest.mark.parametrize("value", ["garbage", "<@1>", "１２３", "²", "", "12 3"])
def test_rest_send_ignores_a_non_numeric_mention_user_id(value):
    opener = _Opener()
    ok, _ = rest.send("tok", "123", "Done", opener=opener, mention_user_id=value)
    assert ok
    payload = json.loads(opener.requests[0].data)
    assert payload["allowed_mentions"] == {"parse": []}
    assert payload["content"] == "Done"


HOSTILE_TITLE = "Print complete: @everyone @here <@&999> <@777888999000111222> widget"


def test_a_hostile_title_cannot_ping_anyone_but_the_starter():
    """A title or filename carrying @everyone, a role or another user's
    mention reaches the content as typed, but allowed_mentions lets only
    the starter's own mention through and parses nothing else."""
    opener = _Opener()
    ok, _ = rest.send("tok", "123", HOSTILE_TITLE, opener=opener, mention_user_id=STARTER)
    assert ok
    payload = json.loads(opener.requests[0].data)
    assert payload["content"].startswith(f"<@{STARTER}> ")
    assert payload["allowed_mentions"] == {"parse": [], "users": [STARTER]}
    opener = _Opener()
    rest.send("tok", "123", HOSTILE_TITLE, opener=opener)
    payload = json.loads(opener.requests[0].data)
    assert payload["allowed_mentions"] == {"parse": []}


def test_the_mention_survives_a_long_post_and_rides_only_the_first_chunk():
    opener = _Opener()
    ok, _ = rest.send("tok", "123", "line\n" * 1000, opener=opener, mention_user_id=STARTER)
    assert ok and len(opener.requests) > 1
    contents = [json.loads(r.data)["content"] for r in opener.requests]
    assert contents[0].startswith(f"<@{STARTER}> ")
    assert all("<@" not in c for c in contents[1:])


def test_listener_discord_post_threads_the_decided_mention(monkeypatch):
    calls = []

    def fake_send(token, channel_id, text, **kwargs):
        calls.append(kwargs)
        return True, "1"

    monkeypatch.setattr(rest, "send", fake_send)
    monkeypatch.setattr(mel, "_bot_token", lambda: "tok")
    assert mel._post_rest_discord("Print complete: Widget", mention_user_id=STARTER)
    assert calls[-1] == {"mention_user_id": STARTER}
    # A post with no starter mention keeps the plain three-argument call.
    assert mel._post_rest_discord("Print started: Widget")
    assert calls[-1] == {}


def test_listener_post_decides_from_kind_and_job(archive, monkeypatch):
    """mel.post with a ping kind asks the classifier: a terminal kind for
    the archived job pings the starter; a start post never does."""
    calls = []

    def fake_send(token, channel_id, text, **kwargs):
        calls.append(kwargs)
        return True, "1"

    monkeypatch.setattr(rest, "send", fake_send)
    monkeypatch.setattr(mel, "_bot_token", lambda: "tok")
    assert mel.post("Print complete: Widget", ping_kind="complete", ping_job_key="plate.gcode")
    assert calls[-1] == {"mention_user_id": STARTER}
    assert mel.post("Print started: Widget", ping_kind="started", ping_job_key="plate.gcode")
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
    assert data["discord"]["mention_starter"] is True
    path = tmp_path / "local.yaml"
    path.write_text("discord:\n  mention_starter: false\n")
    data = settings.load(local=path, environ={})
    assert data["discord"]["mention_starter"] is False
    path.write_text("discord:\n  mention_starter: maybe\n")
    with pytest.raises(settings.SettingsError, match="mention_starter"):
        settings.load(local=path, environ={})
    data = settings.load(local=False,
                         environ={"PRINT_HANDY__DISCORD__MENTION_STARTER": "false"})
    assert data["discord"]["mention_starter"] is False


def test_old_setting_spelling_migrates(tmp_path):
    """The pre-#1095 key (mention_starter_on_complete) is a loader alias:
    an install that set it false stays silent under the new name, in a
    file and from the environment; the new name wins when both are set."""
    path = tmp_path / "local.yaml"
    path.write_text("discord:\n  mention_starter_on_complete: false\n")
    data = settings.load(local=path, environ={})
    assert data["discord"]["mention_starter"] is False
    assert "mention_starter_on_complete" not in data["discord"]
    data = settings.load(
        local=False,
        environ={"PRINT_HANDY__DISCORD__MENTION_STARTER_ON_COMPLETE": "false"})
    assert data["discord"]["mention_starter"] is False
    path.write_text("discord:\n  mention_starter_on_complete: false\n"
                    "  mention_starter: true\n")
    data = settings.load(local=path, environ={})
    assert data["discord"]["mention_starter"] is True


# ------------------------------------------------- button posts (alerts)

def _fake_button_post(monkeypatch):
    from print_handy.runtime import print_alert_buttons as pab
    calls = []

    def fake_buttons(token, channel_id, content, kind, filename, **kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(pab, "post_with_buttons", fake_buttons)
    monkeypatch.setattr(mel, "_bot_token", lambda: "tok")
    monkeypatch.setattr(mel, "_buttonless_standalone", lambda: False)
    monkeypatch.setattr(mel, "log", lambda line: None)
    return calls


def test_button_alert_pings_the_starter(archive, monkeypatch):
    """A firmware-shutdown alert posted with buttons pings its starter
    the way a plain terminal post does (#1095)."""
    calls = _fake_button_post(monkeypatch)
    assert mel.post_alert("**Printer firmware stopped.**\nPrint stopped with an error",
                          "shutdown", "plate.gcode")
    assert calls[-1] == {"mention_user_id": STARTER}


def test_pause_alert_pings_once_per_episode(archive, monkeypatch):
    """The same pause alert sent again — a re-post, a refresh, the
    runout upgrade of the same pause — does not ping again."""
    calls = _fake_button_post(monkeypatch)
    assert mel.post_alert("Print paused: Widget", "paused", "plate.gcode")
    assert calls[-1] == {"mention_user_id": STARTER}
    assert mel.post_alert("Print paused: Widget", "paused", "plate.gcode")
    assert calls[-1] == {}


def test_pause_alerts_ping_per_problem(archive, monkeypatch, ping_state):
    """A runout and a jam are different problems (#1095): each pause
    alert pings its starter, once its own episode is new and the
    per-job cooldown from the last landed ping has passed."""
    calls = _fake_button_post(monkeypatch)
    runout = "Filament runout: the active slot ran out of filament."
    jam = "Filament jam detected (IFS) while printing."
    assert mel.post_alert(runout, "paused", "plate.gcode")
    assert calls[-1] == {"mention_user_id": STARTER}
    # The same runout re-posted is the same episode: no second ping.
    assert mel.post_alert(runout, "paused", "plate.gcode")
    assert calls[-1] == {}
    # Age the cooldown anchor (the classifier reads the wall clock on
    # this path): the jam is a different problem and pings.
    data = json.loads(ping_state.read_text())
    for entry in data.values():
        entry["last_ping"] -= starter_pings.COOLDOWN_S + 60
    ping_state.write_text(json.dumps(data))
    assert mel.post_alert(jam, "paused", "plate.gcode")
    assert calls[-1] == {"mention_user_id": STARTER}


def test_runout_upgrade_repost_shares_the_pauses_episode(archive, monkeypatch):
    """The runout upgrade re-posts the SAME pause with more detail
    (#442): handed the original pause's episode explicitly, it does not
    ping a second time for the one problem."""
    calls = _fake_button_post(monkeypatch)
    assert mel.post_alert("Print paused: Widget", "paused", "plate.gcode")
    assert calls[-1] == {"mention_user_id": STARTER}
    assert mel.post_alert("Filament runout: the active slot ran out of filament.",
                          "paused", "plate.gcode", episode="paused:other")
    assert calls[-1] == {}


def test_watchdog_visual_alert_releases_a_fully_failed_ping(archive, monkeypatch):
    """The listener's watchdog visual alert mirrors post_alert: when
    the button post and the plain fallback both fail, the recorded
    ping is released, so the next watchdog alert for the print can
    ping instead of arriving silently."""
    from print_handy.runtime import print_alert_buttons as pab
    monkeypatch.setattr(pab, "post_visual_alert",
                        lambda *args, **kwargs: False)
    monkeypatch.setattr(mel, "_bot_token", lambda: "tok")
    monkeypatch.setattr(mel, "_buttonless_standalone", lambda: False)
    monkeypatch.setattr(mel, "log", lambda line: None)
    seen = []
    monkeypatch.setattr(mel, "post", lambda text, **kw: seen.append(kw) or False)
    mel._visual_alert("plate.gcode", b"\xff\xd8\xff\xd9", "spaghetti at layer 3")
    assert seen[-1] == {"mention_user_id": STARTER}
    monkeypatch.setattr(mel, "post", lambda text, **kw: seen.append(kw) or True)
    mel._visual_alert("plate.gcode", b"\xff\xd8\xff\xd9", "spaghetti at layer 3")
    assert seen[-1] == {"mention_user_id": STARTER}


def test_a_fully_failed_alert_delivery_can_ping_on_resend(archive, monkeypatch):
    """When the button post and its plain-text fallback both fail, the
    alert was never delivered, so its ping decision is released: a
    re-send of the same alert pings the starter instead of arriving
    silently because the failed attempt recorded the episode."""
    from print_handy.runtime import print_alert_buttons as pab
    monkeypatch.setattr(pab, "post_with_buttons",
                        lambda token, channel_id, content, kind, filename, **kw: False)
    monkeypatch.setattr(mel, "_bot_token", lambda: "tok")
    monkeypatch.setattr(mel, "_buttonless_standalone", lambda: False)
    monkeypatch.setattr(mel, "log", lambda line: None)
    fallback_kwargs = []
    monkeypatch.setattr(mel, "post",
                        lambda text, **kw: fallback_kwargs.append(kw) or False)
    assert not mel.post_alert("Print paused: Widget", "paused", "plate.gcode")
    assert fallback_kwargs[-1] == {"mention_user_id": STARTER}
    # The re-send asks the classifier again and pings again.
    calls = _fake_button_post(monkeypatch)
    assert mel.post_alert("Print paused: Widget", "paused", "plate.gcode")
    assert calls[-1] == {"mention_user_id": STARTER}


def test_button_post_allows_only_the_named_user_to_ping(monkeypatch, tmp_path):
    from print_handy.runtime import print_alert_buttons as pab
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
    assert pab.post_with_buttons("tok", "42", "stopped", "shutdown", "plate.gcode",
                                 mention_user_id=STARTER)
    assert seen[-1]["allowed_mentions"] == {"parse": [], "users": [STARTER]}
    assert seen[-1]["content"] == f"<@{STARTER}> stopped"
    assert pab.post_with_buttons("tok", "42", "paused", "paused", "plate.gcode")
    assert seen[-1]["allowed_mentions"] == {"parse": []}
    assert seen[-1]["content"] == "paused"
    assert pab.post_with_buttons("tok", "42", "x", "shutdown", "plate.gcode", mention_user_id="<@1>")
    assert seen[-1]["allowed_mentions"] == {"parse": []}
    assert seen[-1]["content"] == "x"
    # A hostile filename in the alert text pings nobody but the starter.
    assert pab.post_with_buttons("tok", "42", HOSTILE_TITLE, "shutdown", "plate.gcode",
                                 mention_user_id=STARTER)
    assert seen[-1]["content"].startswith(f"<@{STARTER}> ")
    assert seen[-1]["allowed_mentions"] == {"parse": [], "users": [STARTER]}
    assert pab.post_visual_text_alert("tok", "42", "camera down", "plate.gcode",
                                      mention_user_id=STARTER)
    assert seen[-1]["content"] == f"<@{STARTER}> camera down"
    assert seen[-1]["allowed_mentions"] == {"parse": [], "users": [STARTER]}
