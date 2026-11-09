"""Starter pings (#1095): the one classifier that decides who gets pinged.

Before #1095 only a print's terminal post pinged the person who started
it, and the send path found that ping by regex-matching a ``<@id>`` line
the renderer had appended to the message text. Now the alerts that need
the starter ping them too, and the decision lives here instead: a send
path hands this module a post *kind* and the job (or a job key to
resolve), and gets back the one Discord user ID the post may ping — or
"" for nobody. Callers never build ``<@id>`` lines, and the rendered
text of a post never carries a mention on any platform; the ping rides
``allowed_mentions`` on the Discord send, restricted to that single
user, exactly as the #1029 transport already enforced.

Ping set (catduck, 2026-11-08: "it pings on issues, errors, pasta cam,
and completion, not picking the number of bag clips"):

- terminal posts — complete / cancelled / error — ping once, as #1029
  established (no episode dedupe or cooldown: one terminal post per
  print, and a delivery retry must still be able to ping);
- alert posts that need a person — the pasta/camera watchdog, a print
  paused or stopped by an error, runout or fault, a firmware shutdown,
  and the generic button alert — ping once per alert *episode*;
- everything else never pings: slicing, plan, confirmation and setup
  back-and-forth including interactive choice prompts (picking a
  quantity is catduck's example), print started, layer and progress
  updates, heating/calibration/start-of-print status, roast follow-ups,
  and the dark-camera notice (a camera problem, not a print alert).

Episode and cooldown: an episode is one problem on one job. The same
episode re-sent, refreshed or repeated never pings twice; a different
problem (a different episode on the same job) pings again, but never
inside COOLDOWN_S of the job's last ping, so a flapping detector
alternating problems cannot spam. An episode record older than
EPISODE_TTL_S no longer suppresses: a pause that recurs hours later is
a new occurrence of the problem, not a re-send of the old alert, while
re-sends and refreshes (which happen within minutes) stay silent.
Tracking is persisted like the alert buttons' state (a restart
mid-episode does not re-ping), keyed by job and episode, and pruned
with the job's terminal post.
"""
import json
import threading
import time
from pathlib import Path

from . import _config
from . import discord_print_summary as print_summary

STATE = _config.state_file("PRINT_STARTER_PING_STATE", "starter_pings.json")

# Kinds that end a print; the terminal post pings the starter once.
TERMINAL_KINDS = frozenset({"complete", "cancelled", "error"})
# Alert kinds that need the starter at the printer or on a button.
ALERT_KINDS = frozenset({"watchdog", "paused", "shutdown", "alert"})
# Minimum seconds between two alert pings for the same job, whatever the
# episode: the backstop against a flapping detector alternating problems.
COOLDOWN_S = 600
# How long one episode record suppresses a repeat ping for the same
# problem on the same job. Re-sends and refreshes land inside it; a
# genuinely new occurrence of the same problem lands outside it.
EPISODE_TTL_S = 1800
# A tracked job's alert state is worthless a day after its last ping.
_PRUNE_S = 24 * 3600

_LOCK = threading.Lock()


def mentions_on():
    """Whether starter pings are on (settings + platform gate).

    On unless ``discord.mention_starter`` is set false (the old
    ``mention_starter_on_complete`` spelling is folded onto it by the
    settings loader's KEY_ALIASES, so an install that set the old key
    false stays silent). Discord only: on a Telegram or Matrix install
    nobody is pinged. Any settings failure means off — a ping is a
    notification, never something to guess at.
    """
    try:
        cfg = _config.cfg()
        if str((cfg.get("bot") or {}).get("platform") or "discord") != "discord":
            return False
        return bool((cfg.get("discord") or {}).get("mention_starter", True))
    except Exception:
        return False


def _load():
    try:
        data = json.loads(Path(STATE).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data):
    try:
        Path(STATE).parent.mkdir(parents=True, exist_ok=True)
        Path(STATE).write_text(json.dumps(data))
    except OSError:
        pass


def _job_key(job, job_key):
    """The tracking key for a job: the caller's key, else the job's own file."""
    if job_key:
        return str(job_key)
    if isinstance(job, dict):
        for field in ("_job_path", "gcode"):
            if job.get(field):
                return str(job[field])
        if job.get("title"):
            return f"title:{job['title']}"
    return ""


def _resolve_job(job, job_key):
    """The job dict for a decision: the caller's, else resolved by key."""
    if isinstance(job, dict):
        return job
    if job_key:
        try:
            found, _plate = print_summary.find_job(str(job_key))
            return found or {}
        except Exception:
            return {}
    return {}


def ping_for(kind, job=None, *, job_key=None, episode=None, now=None):
    """The Discord user ID this post may ping, or "" when it pings nobody.

    ``kind`` is the post kind the send path declares; ``job`` the job
    dict (or None, with ``job_key`` naming the print so the job can be
    resolved); ``episode`` names the alert problem for dedupe — the kind
    itself when the caller passes none, so a watchdog alert's varying
    reason text never mints a new episode. ``now`` is injectable for
    tests. A decision that pings also records it: an alert decision is
    the ping, so a re-sent or refreshed post asking again gets "".
    """
    kind = str(kind or "")
    if kind not in TERMINAL_KINDS and kind not in ALERT_KINDS:
        return ""
    if not mentions_on():
        return ""
    job = _resolve_job(job, job_key)
    user_id = print_summary.starter_user_id(job)
    if not user_id:
        return ""
    if kind in TERMINAL_KINDS:
        # The print is over: its alert tracking is dead weight either way.
        key = _job_key(job, job_key)
        if key:
            with _LOCK:
                data = _load()
                if data.pop(key, None) is not None:
                    _save(data)
        return user_id
    key = _job_key(job, job_key)
    if not key:
        # An alert with no job identity cannot be episode-tracked; the
        # watchdog and pause paths always name their file, so this is a
        # caller bug shape — ping rather than silently drop a real alert.
        return user_id
    moment = time.time() if now is None else float(now)
    episode = str(episode or kind)
    with _LOCK:
        data = _load()
        entry = data.get(key)
        if not isinstance(entry, dict):
            entry = {"episodes": {}, "last_ping": 0.0}
        episodes = entry.get("episodes") or {}
        if episode in episodes and moment - float(episodes[episode]) < EPISODE_TTL_S:
            return ""
        if moment - float(entry.get("last_ping") or 0.0) < COOLDOWN_S:
            return ""
        episodes[episode] = moment
        entry["episodes"] = episodes
        entry["last_ping"] = moment
        data[key] = entry
        # Prune jobs whose last ping is a day old while we hold the file.
        stale = [k for k, v in data.items()
                 if isinstance(v, dict) and moment - float(v.get("last_ping") or 0.0) > _PRUNE_S]
        for k in stale:
            del data[k]
        _save(data)
    return user_id


def reset():
    """Drop all episode tracking (tests and process-fresh installs)."""
    with _LOCK:
        _save({})
