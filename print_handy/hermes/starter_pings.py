"""Starter pings (#1095): the one classifier that decides who gets pinged.

Before #1095 only a print's terminal post pinged the person who started
it, and the send path found that ping by regex-matching a ``<@id>`` line
the renderer had appended to the message text. Now the alerts that need
the starter ping them too, and the decision lives here instead: a send
path hands this module a post *kind* and the job (or a job key to
resolve), and gets back the one Discord user ID the post may ping — or
"" for nobody. Callers never build ``<@id>`` lines, and the rendered
text of a post never carries a mention on any platform: the Discord
send path (``bot.rest.starter_mention``) puts the one ``<@id>`` at the
front of the posted content — Discord's ``allowed_mentions`` only
filters mentions present in the content, so without it nobody is
notified — and restricts ``allowed_mentions`` to that single user, so
no other mention in a title or filename can ping.

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

Episode and cooldown: an episode is one problem on one job — a pause's
episode is its cause (``paused:runout``, ``paused:jam``), so a runout
and a later jam on the same print are different problems, while the
runout upgrade re-post of one pause shares that pause's episode. The
same episode re-sent, refreshed or repeated never pings twice; a
different problem (a different episode on the same job) pings again,
but never inside COOLDOWN_S of the job's last ping, so a flapping
detector alternating problems cannot spam — except a firmware
shutdown, which is exempt from the cooldown: it is the alert that
matters most, and a detector's flapping is not its failure mode. A
resume clears the job's pause episodes (the problem ended; if it
pauses the print again that is a new occurrence), leaving the cooldown
anchored on the last ping that landed. An episode record older than
EPISODE_TTL_S no longer suppresses: a pause that recurs hours later is
a new occurrence of the problem, not a re-send of the old alert, while
re-sends and refreshes (which happen within minutes) stay silent.
Tracking is persisted like the alert buttons' state (a restart
mid-episode does not re-ping), keyed by job and episode, and pruned
with the job's terminal post. The state file is shared by every
per-printer listener process, so every load-modify-save runs under a
cross-process file lock and lands by atomic rename: no process can
lose another's update or leave the file half-written.
"""
import contextlib
import json
import os
import threading
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # non-POSIX platform: in-process lock only
    fcntl = None

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
    """Write the store by atomic rename (the bot/health.py pattern): a
    concurrent reader in another listener process sees the old file or
    the new one, never a truncated half-write."""
    try:
        path = Path(STATE)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, path)
    except OSError:
        pass


@contextlib.contextmanager
def _locked():
    """The store's critical section, in this process and across them.

    The thread lock is layered over an OS file lock on a sibling file
    (the core/control_lock.py pattern): the state file is shared by
    every per-printer listener process, and a thread lock alone let two
    processes load the same snapshot and lose each other's update.
    flock is released by the kernel when a holder dies, so a crashed
    listener cannot wedge the store; the sections are milliseconds of
    JSON I/O, so waiting briefly is right — losing an update is the bug
    being fixed. Where fcntl is unavailable, or the lock file cannot be
    opened, the thread lock stands alone rather than the decision.
    """
    with _LOCK:
        fd = None
        if fcntl is not None:
            try:
                path = Path(str(STATE) + ".lock")
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                if fd is not None:
                    os.close(fd)
                fd = None
        try:
            yield
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(fd)


def _job_key(job, job_key):
    """The tracking key for a job: the job's own file when the job is
    known, else the caller's key, else the job's title.

    The job's own file is the one identity every caller spelling of
    the same print shares: an alert path keys on the print's filename
    and a terminal decision may carry the job itself, but both resolve
    to the same job (find_job resolves by name), so keying on the
    resolved job's file keeps one entry per print — the terminal prune
    always finds the episodes the alert paths recorded, and two
    different jobs whose files share a name stay separate entries.
    """
    if isinstance(job, dict):
        for field in ("_job_path", "gcode"):
            if job.get(field):
                return str(job[field])
    if job_key:
        return str(job_key)
    if isinstance(job, dict) and job.get("title"):
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
    reason text never mints a new episode (a pause's callers pass their
    cause, ``paused:<cause>``). ``now`` is injectable for
    tests. A decision that pings also records it: an alert decision is
    the ping, so a re-sent or refreshed post asking again gets "". A
    caller whose delivery then fails outright undoes the record with
    ``release_ping``, so the re-send it makes can ping after all.
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
            with _locked():
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
    with _locked():
        data = _load()
        entry = data.get(key)
        if not isinstance(entry, dict):
            entry = {"episodes": {}, "last_ping": 0.0}
        episodes = entry.get("episodes") or {}
        if episode in episodes and moment - float(episodes[episode]) < EPISODE_TTL_S:
            return ""
        # The cooldown throttles flapping detectors alternating
        # problems; a firmware shutdown is exempt — it is the one alert
        # that must never be swallowed by an earlier, lesser ping.
        if kind != "shutdown" and moment - float(entry.get("last_ping") or 0.0) < COOLDOWN_S:
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


def release_ping(kind, job=None, *, job_key=None, episode=None):
    """Undo an alert ping decision whose delivery failed outright.

    ``ping_for`` records an alert ping at decision time, so a re-sent or
    refreshed post does not ping twice. When every delivery attempt for
    that post failed, though, the ping never happened: the send paths
    that know (the button post and its plain-text fallback both refused)
    release the record here, and a later re-send of the same alert can
    ping. Only the released episode is dropped; the job's last-ping
    anchor falls back to its surviving episodes, so another problem's
    cooldown is still measured from the last ping that did land.
    Terminal kinds record nothing, so releasing one is a no-op.
    """
    if str(kind or "") not in ALERT_KINDS:
        return
    # Resolve like ping_for does, so the key names the same entry the
    # decision recorded (the resolved job's file, not the raw spelling
    # this caller happened to hold).
    key = _job_key(_resolve_job(job, job_key), job_key)
    if not key:
        return
    episode = str(episode or kind)
    with _locked():
        data = _load()
        entry = data.get(key)
        if not isinstance(entry, dict):
            return
        episodes = entry.get("episodes") or {}
        if episode not in episodes:
            return
        del episodes[episode]
        if episodes:
            entry["episodes"] = episodes
            entry["last_ping"] = max(float(at) for at in episodes.values())
            data[key] = entry
        else:
            data.pop(key, None)
        _save(data)


def clear_pause_episodes(job=None, *, job_key=None):
    """Forget a job's pause episodes: the print resumed.

    The pause that was pinged is over, so its episodes stop
    suppressing: if the same problem pauses the print again it is a
    new occurrence and pings again, subject to the per-job cooldown,
    which stays anchored on the last ping that landed (the entry and
    its anchor survive). Only the pause kind's episodes are dropped —
    a watchdog episode from before the pause is a different problem
    and keeps its window.
    """
    key = _job_key(_resolve_job(job, job_key), job_key)
    if not key:
        return
    with _locked():
        data = _load()
        entry = data.get(key)
        if not isinstance(entry, dict):
            return
        episodes = entry.get("episodes") or {}
        kept = {name: at for name, at in episodes.items()
                if name != "paused" and not str(name).startswith("paused:")}
        if len(kept) == len(episodes):
            return
        entry["episodes"] = kept
        data[key] = entry
        _save(data)


def reset():
    """Drop all episode tracking (tests and process-fresh installs)."""
    with _locked():
        _save({})
