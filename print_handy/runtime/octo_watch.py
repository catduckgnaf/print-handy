"""Launched-print monitor for OctoPrint jobs: completion post + timelapse (#373).

OctoPrint has no listener (#299's registry keeps the domain "planned";
#256 keeps the Moonraker listener Moonraker-only), so — exactly as
#295 Stage 2 prescribes for the listener-less families, and following the
Bambu (#294) and stock Flashforge (#377) monitors' shape — the print path
starts this monitor for a print Print Handy itself launched. Before this
monitor existed, a launched OctoPrint print got a start confirmation and
then nothing at all: no completion post, and the timelapse OctoPrint
itself renders never reached the chat — the total-silence end of the
same defect class #373 reports for the Moonraker route.

The monitor polls the adapter's normalised status, and:

- on the terminal transition (the printer returns to standby after the
  launched job was seen active; progress at 100% means complete, short
  of that means the job ended early) it makes the completion post — the
  same format_event text every family uses;
- for a completed job it asks OctoPrint for its rendered timelapse
  movies (GET /api/timelapse), picks the movie OctoPrint rendered for
  this job, downloads it, sizes it through the listener's platform
  sizing (platform_sized_video), and attaches it to the completion post
  through the MEDIA: marker.

Failure posture (the #373 contract — attach, explain, or log; never
silence): a movie that exists but cannot be downloaded or sized is
explained in the completion post; no matching movie (OctoPrint's
timelapse feature is off, or the render isn't listed) is a log line and
the plain completion post, since no video was ever expected; status
read failures cost one poll each and are logged. Duplicate polls and
status flaps never post twice: the terminal post happens at most once
per monitor.

The monitor runs on a daemon thread inside whichever process launched
the print (normally the bot) and is not restarted: if that process
restarts mid-print, monitoring stops with it — the same stated
limitation as the Bambu and stock monitors. Prints started outside
Print Handy are never watched: nothing here polls a printer nobody
launched a print on.

Everything network- or chat-shaped is injectable (status_fn, movies_fn,
video_fn, post) so the whole lifecycle is testable offline with fakes.
"""
from __future__ import annotations

import logging
import tempfile
import threading
import time
from pathlib import Path

from .. import logs
from . import discord_print_summary as print_summary

logger = logging.getLogger(__name__)

POLL_S = 30.0
MAX_MONITOR_S = 48 * 3600.0
_ACTIVE = ("printing", "paused")
_UNKNOWN_STREAK_TERMINAL = 10  # ~5 min of unreadable state after an active print = error end
RENDER_WAIT_S = 300.0  # how long a completion waits for this job's queued timelapse render (P2-147)


def log(line):
    logger.info("%s", logs.redact(str(line)))


def movie_for_job(movies, filename):
    """The newest rendered movie OctoPrint made for this job, or None.

    OctoPrint names a rendered movie after the job's file with a render
    timestamp suffix: '<gcode name>_<YYYYMMDDHHMMSS>.mp4' (the stamp may
    carry separators). The match is strict on identity — the exact
    job-name prefix — and tolerant of the stamp's shape, so another
    job's movie is never attached to this completion.
    """
    stem = filename[:-len(".gcode")] if filename.endswith(".gcode") else filename
    best = None
    for movie in movies or []:
        name = str((movie or {}).get("name") or "")
        if not name.endswith(".mp4") or not name.startswith(stem + "_"):
            continue
        stamp = name[len(stem) + 1:-len(".mp4")]
        if not stamp or not all(c.isdigit() or c in "_-" for c in stamp):
            continue
        if best is None or name > str(best.get("name") or ""):
            best = movie
    return best


def _too_big_text():
    """The completion line for a recorded-but-undeliverable timelapse, per platform."""
    try:
        from . import moonraker_event_listener as mel
        return mel._timelapse_too_big_text()
    except Exception:
        return "Timelapse was recorded but is too large to attach."


def _fetch_and_size(client, movie):
    """Download the movie OctoPrint rendered and size it for delivery.

    Returns the attachable video path, or None when the video exists but
    cannot be sized under the platform upload limit (the shrink failure
    is logged here; the caller adds the recorded-but-too-large line).
    Raises when the download itself failed.
    """
    from . import moonraker_event_listener as mel

    name = Path(str(movie.get("name") or "timelapse.mp4")).name
    with tempfile.NamedTemporaryFile("wb", suffix=f"-{name}", delete=False) as handle:
        handle.write(client.download(str(movie.get("url") or "")))
        raw = Path(handle.name)
    try:
        return mel.platform_sized_video(raw)
    except Exception as exc:
        log(f"octoprint timelapse sizing failed for {name!r}: {exc!r}")
        return None


class OctoPrintMonitor:
    """Poll one launched OctoPrint print to its end; post its completion.

    Read-only by construction, like the other launched-print monitors:
    it reads status and the timelapse list and posts to chat; it never
    writes to the printer. Its one alert is the pause post (#460): a
    printing -> paused transition posts the shared pause alert through
    family_control.PauseAlerts, whose Resume/Cancel buttons actuate
    only when an operator presses one — the monitor itself holds no
    printer-control callable.
    """

    def __init__(self, filename, *, status_fn, movies_fn, video_fn, post,
                 pause_alerts=None, pending_fn=None,
                 clock=time.monotonic, sleep=time.sleep, poll_s=POLL_S):
        self.filename = filename or ""
        self._status_fn = status_fn
        self._movies_fn = movies_fn
        self._video_fn = video_fn
        self._post = post
        self._pause_alerts = pause_alerts
        self._pending_fn = pending_fn
        self._clock = clock
        self._sleep = sleep
        self._poll_s = poll_s
        self.outcome = None

    # -- terminal post -------------------------------------------------------

    def _render_pending_for_job(self):
        """True while OctoPrint's render queue still holds this job's movie.

        Unrendered entries name the movie being rendered; match on the
        same stem rule movie_for_job uses (the job stem, optionally with
        the render-stamp suffix), tolerating a bare string entry.
        """
        stem = self.filename[:-len(".gcode")] if self.filename.endswith(".gcode") else self.filename
        for entry in self._pending_fn() or []:
            name = str(entry.get("name") or "") if isinstance(entry, dict) else str(entry or "")
            base = name[:-len(".mp4")] if name.endswith(".mp4") else name
            if base == stem or base.startswith(stem + "_"):
                return True
        return False

    def _await_movie(self):
        """(movie, lookup_failed): the rendered movie for this job.

        OctoPrint renders in a background queue after the print ends, so
        a just-finished job's movie legitimately appears late (P2-147).
        While the queue says this job is still rendering, re-poll the
        listing on the monitor's cadence, bounded by RENDER_WAIT_S. With
        no queue wired (pending_fn None) — or nothing pending for this
        job — the first lookup is the whole answer, exactly as before. A
        listing or queue read that fails is lookup_failed: the post
        explains instead of claiming there was no video.
        """
        deadline = self._clock() + RENDER_WAIT_S
        while True:
            try:
                movie = movie_for_job(self._movies_fn(), self.filename)
            except Exception as exc:
                log(f"octoprint timelapse list failed for {self.filename!r}: {exc!r}")
                return None, True
            if movie is not None:
                return movie, False
            if self._pending_fn is None:
                return None, False
            try:
                pending = self._render_pending_for_job()
            except Exception as exc:
                log(f"octoprint timelapse render queue couldn't be read for "
                    f"{self.filename!r}: {exc!r}")
                return None, True
            if not pending:
                return None, False
            if self._clock() >= deadline:
                log(f"octoprint timelapse for {self.filename!r} still rendering after "
                    f"{RENDER_WAIT_S:g}s; posting the completion without it")
                return None, False
            self._sleep(self._poll_s)


    def _send(self, text):
        """Deliver a terminal post (#1095): the production sender lets the
        starter pings classifier decide the ping from the terminal kind
        and this print; an injected post callable keeps its shape."""
        if self._post is _default_post:
            return self._post(text, ping_kind=self._terminal_kind, ping_job_key=self.filename)
        return self._post(text)

    def _terminal_post(self, kind):
        self._terminal_kind = kind
        text = print_summary.format_event(kind, self.filename)
        if kind != "complete":
            self._send(text)
            return kind
        movie, lookup_failed = self._await_movie()
        if movie is None:
            if lookup_failed:
                self._send(f"{text}\nTimelapse not attached: the rendered video "
                           "couldn't be listed (see the monitor log).")
            else:
                log(f"no rendered timelapse listed for {self.filename!r} "
                    "(OctoPrint timelapse off or not rendered); posting the completion without it")
                self._send(text)
            return kind
        try:
            video = self._video_fn(movie)
        except Exception as exc:
            log(f"octoprint timelapse fetch failed for {self.filename!r}: {exc!r}; "
                "posting the completion without it")
            self._send(f"{text}\nTimelapse not attached: the rendered video "
                       "couldn't be downloaded (see the monitor log).")
            return kind
        if video is None:
            self._send(f"{text}\n{_too_big_text()}")
            return kind
        try:
            # The production sender reports a refusal as a False return,
            # not an exception (the P2-169/P2-173 contract): only an
            # explicit False means undelivered.
            if self._send(f"{text}\nMEDIA:{video}") is False:
                log(f"octoprint completion post with timelapse was refused for "
                    f"{self.filename!r}; posting the text only")
                self._send(text)
        except Exception as exc:
            log(f"octoprint completion post with timelapse failed for {self.filename!r}: {exc!r}; "
                "posting the text only")
            self._send(text)
        return kind

    # -- the poll loop ---------------------------------------------------------

    def run(self):
        """Poll until the launched print ends. Returns the terminal kind, or None."""
        started = self._clock()
        seen_active = False
        unknown_streak = 0
        while self._clock() - started <= MAX_MONITOR_S:
            try:
                st = self._status_fn()
            except Exception as exc:
                # A reconnect or a refused read costs one poll, never the monitor.
                log(f"octoprint monitor status read failed for {self.filename!r}: {exc!r}")
                self._sleep(self._poll_s)
                continue
            state = str(st.get("state") or "unknown")
            fname = st.get("filename") or ""
            if state in _ACTIVE:
                if fname and self.filename and fname != self.filename:
                    log(f"octoprint monitor: a different print {fname!r} is running; "
                        f"{self.filename!r} is gone")
                    if self._pause_alerts:
                        self._pause_alerts.note("standby", st)
                    return None
                seen_active = True
                unknown_streak = 0
                if self._pause_alerts:
                    self._pause_alerts.note(state, st)
                self._sleep(self._poll_s)
            elif state == "standby" and seen_active:
                # OctoPrint's normalised states have no "complete": back to
                # standby after an active job is the terminal transition,
                # and the final progress separates complete from ended-early.
                progress = st.get("progress")
                kind = "complete" if isinstance(progress, (int, float)) and progress >= 1.0 else "cancelled"
                if self._pause_alerts:
                    self._pause_alerts.note(kind, st)
                self.outcome = self._terminal_post(kind)
                return self.outcome
            elif seen_active and state == "unknown":
                # An errored OctoPrint sits outside the ready/printing flags
                # until acknowledged; a sustained unreadable state after an
                # active print is its terminal error, not a reason to poll
                # for two days.
                unknown_streak += 1
                if unknown_streak >= _UNKNOWN_STREAK_TERMINAL:
                    log(f"octoprint monitor: {self.filename!r} unreadable for "
                        f"{unknown_streak} polls after printing; posting the error outcome")
                    if self._pause_alerts:
                        self._pause_alerts.note("error", st)
                    self.outcome = self._terminal_post("error")
                    return self.outcome
                self._sleep(self._poll_s)
            else:
                unknown_streak = 0
                self._sleep(self._poll_s)
        log(f"octoprint monitor gave up on {self.filename!r} after {MAX_MONITOR_S / 3600:g}h "
            "without a terminal state")
        return None


def _default_post(message, **kwargs):
    from . import moonraker_event_listener as mel
    return mel.post(message, **kwargs)


def start_monitor(adapter, filename, *, post=None, thread_factory=None, printer_id=None):
    """Start an OctoPrintMonitor for a just-launched OctoPrint print on a daemon thread.

    printer_id is the launched job's roster printer: the monitor's pause
    alerts carry it, so their Resume/Cancel buttons act on that printer
    rather than the default one (SAFE-4 / P1-101).

    Returns the thread (or None when monitoring could not start — a
    failed monitor must never fail the print that launched it). With no
    chat configured to post to (a CLI run, a test), no monitor starts:
    there is nobody to post the completion or the timelapse to.
    """
    if post is None:
        from . import bambu_watch
        if not bambu_watch._post_target_configured():
            log(f"octoprint monitor not started for {filename!r}: no chat is configured to post to")
            return None
    try:
        client = adapter.client()
        from . import family_control
        monitor = OctoPrintMonitor(
            filename,
            status_fn=lambda: adapter.normalize_status(client.status()),
            movies_fn=client.timelapse_movies,
            # A client without the render-queue read degrades to the
            # single-lookup behavior (pending_fn None), never to no
            # monitor at all.
            pending_fn=getattr(client, "timelapse_unrendered", None),
            video_fn=lambda movie: _fetch_and_size(client, movie),
            post=post or _default_post,
            pause_alerts=family_control.pause_alerts_for(adapter, filename, printer_id))
        factory = thread_factory or (lambda target: threading.Thread(target=target, daemon=True,
                                                                     name=f"octo-watch-{filename}"))
        thread = factory(monitor.run)
        thread.start()
        return thread
    except Exception as exc:
        log(f"octoprint monitor could not start for {filename!r}: {exc!r}")
        return None
