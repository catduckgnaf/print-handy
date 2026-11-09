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


# ------------------------------------------------------- shutdown urgency

def test_shutdown_pings_inside_the_cooldown():
    """A firmware shutdown is the alert that matters most: the per-job
    cooldown that throttles flapping detectors must not swallow it when
    it lands minutes after another problem's ping."""
    assert ping("watchdog", episode="watchdog", now=T0) == STARTER
    assert ping("shutdown", episode="shutdown", now=T0 + 60) == STARTER
    # A shutdown re-send is still the same episode, not a new ping.
    assert ping("shutdown", episode="shutdown", now=T0 + 120) == ""


# ------------------------------------------------------- per-cause pauses

def test_pause_episodes_are_per_problem():
    """One pause problem never stands in for another: the send paths
    record pause episodes per cause (paused:<cause>), so a runout and a
    later jam on the same print each ping once."""
    assert ping("paused", episode="paused:runout", now=T0) == STARTER
    # The same runout re-sent inside its window stays silent...
    assert ping("paused", episode="paused:runout", now=T0 + 900) == ""
    # ...while the jam is a different problem and pings.
    assert ping("paused", episode="paused:jam", now=T0 + 1200) == STARTER


def test_resume_clears_pause_episodes():
    """A resume ends the pause: its episodes stop suppressing, so the
    same problem pausing the print again is a new occurrence and pings
    (outside the per-job cooldown, as #1095 scoped it)."""
    assert ping("paused", episode="paused:other", now=T0) == STARTER
    starter_pings.clear_pause_episodes(job=JOB)
    assert ping("paused", episode="paused:other",
                now=T0 + starter_pings.COOLDOWN_S + 1) == STARTER


def test_resume_clearing_keeps_the_cooldown_anchor_and_other_episodes():
    """Clearing on resume drops only the pause episodes: the job's
    last-ping anchor still measures the cooldown for the next problem,
    and a watchdog episode recorded before the pause survives."""
    assert ping("watchdog", episode="watchdog", now=T0) == STARTER
    assert ping("paused", episode="paused:runout",
                now=T0 + starter_pings.COOLDOWN_S + 1) == STARTER
    starter_pings.clear_pause_episodes(job=JOB)
    # A different problem inside the cooldown is still held back: the
    # anchor is the last landed ping, cleared episodes or not.
    assert ping("paused", episode="paused:jam",
                now=T0 + starter_pings.COOLDOWN_S + 60) == ""
    # The watchdog's own episode was not a pause episode: still recorded.
    assert ping("watchdog", episode="watchdog",
                now=T0 + starter_pings.COOLDOWN_S + 700) == ""


# ------------------------------------------- key spellings and the reprint

def test_reprint_does_not_inherit_episodes_across_key_spellings():
    """An alert path names the print by filename; a terminal decision
    may carry the job itself. Both spellings resolve to the same job,
    whose own file is the tracking key — so the terminal prune finds
    the alert episodes either way, and a reprint of the same file
    inside the window does not inherit the earlier print's episodes."""
    assert starter_pings.ping_for("watchdog", job=JOB, job_key="plate.gcode",
                                  episode="watchdog", now=T0) == STARTER
    assert ping("complete", now=T0 + 30) == STARTER
    # The reprint's own watchdog alert pings: the slate was pruned.
    assert starter_pings.ping_for("watchdog", job=JOB, job_key="plate.gcode",
                                  episode="watchdog", now=T0 + 60) == STARTER
    # And the other way round: an alert recorded under the job's own
    # key is pruned by a terminal post naming the file.
    assert ping("paused", episode="paused:other", now=T0 + 700) == STARTER
    assert starter_pings.ping_for("complete", job=JOB, job_key="plate.gcode",
                                  now=T0 + 730) == STARTER
    assert ping("paused", episode="paused:other", now=T0 + 760) == STARTER


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


# ------------------------------------------- one state file, many processes

_PING_CHILD = r"""
import os, sys, time
sys.path.insert(0, os.environ["PH_REPO_ROOT"])
from print_handy.hermes import starter_pings
starter_pings.mentions_on = lambda: True
who = os.environ["PH_WHO"]
# Barrier: both processes start their bursts together, so the
# load-modify-save windows genuinely overlap.
go = os.environ["PH_GO"]
for _ in range(6000):
    if os.path.exists(go):
        break
    time.sleep(0.005)
for i in range(150):
    key = f"{who}-{i}.gcode"
    job = {"title": "W", "gcode": f"/archive/w/{key}",
           "started_by": {"platform": "discord", "user_id": "111222333444555666"}}
    got = starter_pings.ping_for("alert", job=job, episode="alert",
                                 now=1000.0 + i)
    assert got == "111222333444555666", (key, got)
"""


def test_two_processes_sharing_one_state_file_lose_nothing(tmp_path):
    """starter_pings.json is one file shared by every per-printer
    listener process: concurrent decisions from two processes must all
    survive — no lost job records, and the file is never left truncated
    or half-written for the next reader."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    state = tmp_path / "starter_pings.json"
    env = dict(os.environ, PRINT_STARTER_PING_STATE=str(state),
               PH_REPO_ROOT=str(Path(__file__).resolve().parents[2]),
               PH_GO=str(tmp_path / "go"))
    procs = [subprocess.Popen([sys.executable, "-c", _PING_CHILD],
                              env={**env, "PH_WHO": who},
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True)
             for who in ("alpha", "beta")]
    (tmp_path / "go").write_text("go")
    for proc in procs:
        _out, err = proc.communicate(timeout=300)
        assert proc.returncode == 0, err
    data = json.loads(state.read_text())
    assert set(data) == {f"/archive/w/{who}-{i}.gcode"
                         for who in ("alpha", "beta") for i in range(150)}
