"""Starter pings classifier (#1095): episodes, cooldown, and the window
where false positives live.

The classifier (starter_pings) is the one place that decides whether a
post pings the print's starter. Alert pings are once per episode: the
same problem re-sent, refreshed or repeated stays silent; a different
problem pings again outside the per-job cooldown; and a watchdog that
flaps during the first layers — the likeliest spam source — can never
turn its varying reasons into a pile of pings.
"""
import copy
import json

import pytest

from print_handy.hermes import _config
from print_handy.hermes import discord_print_summary as summary
from print_handy.hermes import starter_pings

STARTER = "111222333444555666"
JOB = {"title": "Widget", "gcode": "/archive/widget/plate.gcode",
       "started_by": {"platform": "discord", "user_id": STARTER}}
T0 = 1_000_000.0


@pytest.fixture(autouse=True)
def ping_state(tmp_path, monkeypatch):
    monkeypatch.setattr(starter_pings, "STATE", tmp_path / "starter_pings.json")


def ping(kind, **kwargs):
    kwargs.setdefault("job", JOB)
    return starter_pings.ping_for(kind, **kwargs)


# ------------------------------------------------------------- episodes

def test_same_episode_never_pings_twice():
    assert ping("watchdog", episode="watchdog", now=T0) == STARTER
    # A re-sent prompt, a button refresh, a repeated check of the same
    # problem: same episode, no second ping.
    assert ping("watchdog", episode="watchdog", now=T0 + 60) == ""
    assert ping("watchdog", episode="watchdog", now=T0 + 600) == ""


def test_different_problem_pings_again_after_the_cooldown():
    assert ping("watchdog", episode="watchdog", now=T0) == STARTER
    # A different problem inside the cooldown is held back...
    assert ping("paused", episode="paused", now=T0 + 60) == ""
    # ...and pings once the cooldown has passed.
    assert ping("paused", episode="paused", now=T0 + starter_pings.COOLDOWN_S + 1) == STARTER


def test_episode_recurs_after_its_ttl():
    """A pause that happens again hours later is a new occurrence of the
    problem, not a re-send of the old alert."""
    assert ping("paused", episode="paused", now=T0) == STARTER
    assert ping("paused", episode="paused", now=T0 + starter_pings.EPISODE_TTL_S + 1) == STARTER


def test_episode_state_survives_a_restart(tmp_path):
    """Tracking is persisted: a process restart mid-episode does not
    re-ping the same alert."""
    assert ping("watchdog", episode="watchdog", now=T0) == STARTER
    data = json.loads((tmp_path / "starter_pings.json").read_text())
    assert data  # the decision was written to the state file
    assert ping("watchdog", episode="watchdog", now=T0 + 30) == ""


def test_terminal_post_prunes_the_jobs_alert_tracking():
    assert ping("watchdog", episode="watchdog", now=T0) == STARTER
    assert ping("complete", now=T0 + 30) == STARTER
    # The print is over; its next life (same file printed again) starts
    # with a clean episode slate.
    assert ping("watchdog", episode="watchdog", now=T0 + 60) == STARTER


def test_terminal_posts_are_not_episode_limited():
    """A terminal post pings once per post, as #1029 established —
    including a delivery retry of the same completion."""
    assert ping("complete", now=T0) == STARTER
    assert ping("complete", now=T0 + 5) == STARTER


# ------------------------------------------------- failed deliveries

def test_a_failed_delivery_releases_the_episode():
    """A ping decision whose delivery failed outright is not the ping:
    releasing it lets the caller's re-send inside the episode window
    ping, instead of the alert being silently mention-less forever."""
    assert ping("watchdog", episode="watchdog", now=T0) == STARTER
    starter_pings.release_ping("watchdog", job=JOB, episode="watchdog")
    assert ping("watchdog", episode="watchdog", now=T0 + 60) == STARTER


def test_release_keeps_other_episodes_and_their_cooldown_anchor():
    """Releasing one episode must not erase a different problem's
    record, and the job's last-ping anchor falls back to the surviving
    episodes, so the cooldown is measured from the last delivered ping."""
    assert ping("watchdog", episode="watchdog", now=T0) == STARTER
    assert ping("paused", episode="paused", now=T0 + starter_pings.COOLDOWN_S + 1) == STARTER
    starter_pings.release_ping("paused", job=JOB, episode="paused")
    # The surviving watchdog episode still suppresses its own re-send.
    assert ping("watchdog", episode="watchdog", now=T0 + starter_pings.COOLDOWN_S + 70) == ""
    # And a new problem is measured against the watchdog ping, not the
    # released one: the cooldown from T0 has long passed.
    assert ping("alert", episode="alert",
                now=T0 + starter_pings.COOLDOWN_S + 70) == STARTER


def test_release_is_a_noop_for_terminal_kinds_and_unknown_episodes():
    """Terminal decisions record nothing, so releasing one changes
    nothing; releasing an episode that was never recorded is harmless."""
    assert ping("complete", now=T0) == STARTER
    starter_pings.release_ping("complete", job=JOB)
    starter_pings.release_ping("watchdog", job=JOB, episode="never-recorded")
    assert ping("complete", now=T0 + 5) == STARTER


# ------------------------------------------- the first-layer spam window

def test_first_layer_watchdog_flapping_cannot_spam():
    """Start-of-print false positives are the likeliest ping spam: the
    watchdog's reasons vary from check to check in the first layers, so
    this drives it the way the watcher does — one episode name for the
    watchdog, whatever the reason text says — plus an alternating
    second detector, inside the first minutes of a print."""
    assert ping("watchdog", episode="watchdog", now=T0) == STARTER
    for i in range(1, 8):
        # Same watchdog, new wording every check ("layer 1: spaghetti",
        # "layer 2: uncertain", ...): still the one episode.
        assert ping("watchdog", episode="watchdog", now=T0 + i * 20) == ""
        # A second, alternating problem is held by the cooldown.
        assert ping("watchdog", episode="watchdog-alt", now=T0 + i * 20) == ""
    # Nothing in the whole first-layer window produced a second ping.


def test_watchdog_alert_at_a_quiet_print_still_pings():
    """Episode state is per job: another print's watchdog is unaffected."""
    other = {"title": "Gadget", "gcode": "/archive/gadget/plate.gcode",
             "started_by": {"platform": "discord", "user_id": STARTER}}
    assert ping("watchdog", episode="watchdog", now=T0) == STARTER
    assert starter_pings.ping_for("watchdog", job=other, episode="watchdog", now=T0 + 5) == STARTER


# -------------------------------------------------------------- identity

def test_job_key_alone_resolves_the_job(tmp_path, monkeypatch):
    """Send paths that hold only the print's filename (the listener's
    alert posts) resolve the job through the shared archive lookup."""
    folder = tmp_path / "archive" / "widget--abc"
    folder.mkdir(parents=True)
    gcode = folder / "plate.gcode"
    gcode.write_text("; estimated printing time (normal mode) = 1h 0m\n")
    job = {"title": "Widget", "gcode": str(gcode),
           "started_by": {"platform": "discord", "user_id": STARTER}}
    (folder / "JOB.json").write_text(json.dumps(job))
    monkeypatch.setattr(summary, "ARCHIVE", tmp_path / "archive")
    monkeypatch.setattr(summary, "CONTEXT_PATH", tmp_path / "no-context.json")
    assert starter_pings.ping_for("watchdog", job_key="plate.gcode", now=T0) == STARTER
    assert starter_pings.ping_for("complete", job_key="missing.gcode") == ""


def test_alert_without_any_job_identity_still_pings():
    """An alert kind with a job dict but no file identity cannot be
    episode-tracked; a real alert is not dropped for a bookkeeping gap."""
    job = {"title": "Widget",
           "started_by": {"platform": "discord", "user_id": STARTER}}
    assert starter_pings.ping_for("watchdog", job=job, now=T0) == STARTER


# ------------------------------------------------------- the visual send

def test_visual_alert_send_allows_only_the_starter(monkeypatch, tmp_path):
    """The watchdog's button post (print_alert_buttons.post_visual_alert)
    carries the classifier's decision as its one allowed mention."""
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
        seen.append(request)
        return Response()

    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    monkeypatch.setattr(pab, "STATE", tmp_path / "alerts.json")
    monkeypatch.setattr(pab.urllib.request, "urlopen", fake_open)
    assert pab.post_visual_alert("tok", "42", "Print camera check: Widget",
                                 "plate.gcode", frame, mention_user_id=STARTER)
    body = seen[-1].data
    payload = json.loads(body.split(b"\r\n\r\n", 1)[1].split(b"\r\n--")[0])
    assert payload["allowed_mentions"] == {"parse": [], "users": [STARTER]}
    assert pab.post_visual_alert("tok", "42", "Print camera check: Widget",
                                 "plate.gcode", frame)
    body = seen[-1].data
    payload = json.loads(body.split(b"\r\n\r\n", 1)[1].split(b"\r\n--")[0])
    assert payload["allowed_mentions"] == {"parse": []}
