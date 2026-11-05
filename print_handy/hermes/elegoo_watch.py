"""Launched-print monitor for Elegoo SDCP jobs: completion post + timelapse (#295).

Elegoo SDCP printers (the Centauri line) have no listener (#299's
registry keeps the domain "planned"; #256 keeps the Moonraker listener
Moonraker-only), so — exactly as #295 Stage 2 prescribes for the
listener-less families, and following the Prusa monitor's shape
(#447) — the print path starts this monitor for a print Print Handy
itself launched. Before this monitor existed, a launched SDCP print
got a start confirmation and then nothing at all: no completion post,
ever. It polls the adapter's normalised status (SDCP Cmd 0 answers),
and:

- on the first active poll it opens one shared timelapse capture
  session (#246's print_handy.hermes.timelapse_capture) fed by the
  printer's camera source (printers.elegoo_sdcp.camera). No SDCP
  camera endpoint is verified in this codebase — the seam resolves
  to "no source" today (see that module for the audit), so a launched
  print currently gets the completion post with no capture, the
  reason logged once; the capture leg below is fully wired, so a
  source verified later slots in without touching this monitor;
- on the terminal transition it stops the session, encodes the frames
  through the same encode/sizing seam the stock and Prusa monitors
  use (the [timelapse] encoder choice, CPU fallback, the platform
  upload limit), and makes the completion post — the same format_event
  text every family uses — with the video attached through the MEDIA:
  marker.

Failure posture (the #235 lesson): the camera read, the capture, the
encode and the sizing each fail soft into a log line; the completion
post always goes out, with the plain text when there is no video and
with the recorded-but-too-large line when a video exists but cannot
be sized under the platform limit. Duplicate polls and status flaps
never re-open the session and never post twice: the capture attempt
and the terminal post each happen at most once per monitor.

The monitor runs on a daemon thread inside whichever process launched
the print (normally the bot) and keeps its capture state in the
session manifest on disk, but the monitor itself is not restarted: if
that process restarts mid-print, monitoring stops with it — the same
stated limitation as the Bambu, stock and Prusa monitors. Prints
started outside Print Handy are never watched: nothing here polls a
printer nobody launched a print on.

Everything network- or chat-shaped is injectable (status_fn,
camera_source_fn, begin_fn, encode_fn, post) so the whole lifecycle is
testable offline with fakes.
"""
from __future__ import annotations

import logging
import threading
import time

from .. import logs
from ..printers.elegoo_sdcp import camera as elegoo_camera
from . import _config
from . import discord_print_summary as print_summary
from . import stock_watch, timelapse_capture, timelapse_encode

logger = logging.getLogger(__name__)

POLL_S = 30.0
MAX_MONITOR_S = 48 * 3600.0
_ACTIVE = ("printing", "paused")
_TERMINAL = {"complete": "complete", "cancelled": "cancelled", "error": "error", "standby": "complete"}


def log(line):
    logger.info("%s", logs.redact(str(line)))


def _default_encode(session):
    """Encode a stopped session's frames and size the result for delivery.

    The encode itself is the stock monitor's shared seam
    (stock_watch.encode_frames: the [timelapse] encoder choice with the
    CPU fallback), so no family forks the frame pipeline. Returns the
    attachable video path, or None when the video exists but cannot be
    sized under the platform upload limit (the shrink failure is logged
    here; the caller adds the recorded-but-too-large line). Raises when
    the encode itself failed.
    """
    from . import moonraker_event_listener as mel

    raw = stock_watch.encode_frames(session.frames_dir, session.directory / "timelapse.mp4")
    try:
        return stock_watch.delivery_video(mel.platform_sized_video(raw), session.filename, session)
    except Exception as exc:
        log(f"elegoo timelapse sizing failed for {session.filename!r}: {exc!r}")
        return None


def _too_big_text():
    """The completion line for a recorded-but-undeliverable timelapse, per platform."""
    try:
        from . import moonraker_event_listener as mel
        return mel._timelapse_too_big_text()
    except Exception:
        return "Timelapse was recorded but is too large to attach."


class ElegooPrintMonitor:
    """Poll one launched SDCP print to its end; post its completion.

    Read-only by construction, like the other launched-print monitors:
    it reads status (and, once a source verifies, the camera) and
    posts to chat; it never writes to the printer. Its one alert is
    the pause post (#460): a printing -> paused transition posts the
    shared pause alert through family_control.PauseAlerts, whose
    Resume/Cancel buttons actuate only when an operator presses one —
    the monitor itself holds no printer-control callable.
    """

    def __init__(self, filename, *, status_fn, camera_source_fn, post,
                 begin_fn=None, encode_fn=None, pause_alerts=None,
                 clock=time.monotonic, wall=time.time, sleep=time.sleep,
                 poll_s=POLL_S, capture_poll_s=5.0):
        self.filename = filename or ""
        self._status_fn = status_fn
        self._camera_source_fn = camera_source_fn
        self._post = post
        self._pause_alerts = pause_alerts
        self._begin_fn = begin_fn or self._begin_default
        self._encode_fn = encode_fn or _default_encode
        self._clock = clock
        self._wall = wall
        self._sleep = sleep
        self._poll_s = poll_s
        self._capture_poll_s = max(1.0, min(float(capture_poll_s), poll_s))
        self._session = None
        self.outcome = None

    # -- capture lifecycle -------------------------------------------------

    def _begin_default(self, url, fetch):
        return timelapse_capture.begin(
            self.filename, self.filename, url=url, fetch=fetch, log=log)

    def _begin_capture(self):
        """Open the one capture session for this print. Never raises.

        Called at most once per monitor (the caller guards it), so a
        duplicate poll or a status flap can never open a second session.
        Every failure lands as "no capture for this print" plus a log
        line naming the reason — the diagnostic the issue asks for.
        """
        try:
            url, fetch = self._camera_source_fn()
        except Exception as exc:
            log(f"elegoo camera source could not be read for {self.filename!r}: {exc!r}; "
                "no timelapse for this print")
            return None
        if not url:
            log(f"no verified SDCP camera source for {self.filename!r} "
                "(no camera endpoint is verified for SDCP in this codebase — "
                "see printers/elegoo_sdcp/camera.py); no timelapse for this print")
            return None
        try:
            session = self._begin_fn(url, fetch)
        except Exception as exc:
            log(f"timelapse capture could not begin for {self.filename!r}: {exc!r}")
            return None
        if session is None:
            log(f"timelapse capture unavailable for {self.filename!r} "
                "(capture disabled or refused); the print continues without it")
        return session

    def _stop_capture(self, state):
        """Stop the session, whatever state it is in. Never raises."""
        session, self._session = self._session, None
        if session is not None:
            try:
                session.stop(state)
            except Exception as exc:
                log(f"timelapse capture stop failed for {self.filename!r}: {exc!r}")
        return session

    # -- terminal post -------------------------------------------------------

    def _terminal_post(self, state, session):
        kind = _TERMINAL[state]
        text = print_summary.format_event(kind, self.filename)
        video = None
        undeliverable = False
        if session is not None and session.frame_count > 0:
            try:
                video = self._encode_fn(session)
                undeliverable = video is None
            except Exception as exc:
                log(f"elegoo timelapse encode failed for {self.filename!r}: {exc!r}; "
                    "posting the completion without it")
        elif session is not None:
            log(f"elegoo timelapse for {self.filename!r} has no frames "
                f"({session.failures} failed fetch(es)); posting the completion without it")
        if video is not None:
            text = timelapse_encode.caption_with_number(text, None, self.filename)
            try:
                self._post(f"{text}\nMEDIA:{video}")
                return kind
            except Exception as exc:
                log(f"elegoo completion post with timelapse failed for {self.filename!r}: {exc!r}; "
                    "posting the text only")
        if undeliverable:
            text = f"{text}\n{_too_big_text()}"
        self._post(text)
        return kind

    # -- the poll loop ---------------------------------------------------------

    def run(self):
        """Poll until the launched print ends. Returns the terminal kind, or None."""
        started = self._clock()
        seen_active = False
        capture_attempted = False
        while self._clock() - started <= MAX_MONITOR_S:
            try:
                st = self._status_fn()
            except Exception as exc:
                # A dropped WebSocket or a refused read costs one poll,
                # never the session: capture state lives in this
                # monitor, and the session's own tick retries its
                # fetches the same way.
                log(f"elegoo monitor status read failed for {self.filename!r}: {exc!r}")
                self._sleep(self._poll_s)
                continue
            state = str(st.get("state") or "unknown")
            fname = st.get("filename") or ""
            if state in _ACTIVE:
                if fname and self.filename and fname != self.filename:
                    log(f"elegoo monitor: a different print {fname!r} is running; "
                        f"{self.filename!r} is gone")
                    if self._pause_alerts:
                        self._pause_alerts.note("standby", st)
                    self._stop_capture("superseded")
                    return None
                seen_active = True
                if self._pause_alerts:
                    self._pause_alerts.note(state, st)
                if not capture_attempted:
                    capture_attempted = True
                    self._session = self._begin_capture()
                if self._session is not None and state == "printing":
                    # A pause holds the session open but takes no frames,
                    # exactly as the Moonraker listener's capture hook does.
                    # The tick carries the reported layer (the SDCP
                    # adapter supplies it): without it a layer-triggered
                    # capture can never fire (P2-172).
                    layer = st.get("layer")
                    if not (isinstance(layer, int) and not isinstance(layer, bool)):
                        layer = None
                    self._session.tick(layer)
                self._sleep(self._capture_poll_s if self._session is not None else self._poll_s)
            elif state in _TERMINAL:
                if seen_active or not self.filename or fname == self.filename:
                    if self._pause_alerts:
                        self._pause_alerts.note(state, st)
                    session = self._stop_capture(_TERMINAL[state])
                    self.outcome = self._terminal_post(state, session)
                    return self.outcome
                self._stop_capture("superseded")
                return None
            else:
                self._sleep(self._poll_s)
        log(f"elegoo monitor gave up on {self.filename!r} after {MAX_MONITOR_S / 3600:g}h "
            "without a terminal state")
        self._stop_capture("abandoned")
        return None


def _default_post(message):
    from . import moonraker_event_listener as mel
    return mel.post(message)


def start_monitor(adapter, filename, *, post=None, thread_factory=None, printer_id=None):
    """Start an ElegooPrintMonitor for a just-launched SDCP print on a daemon thread.

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
            log(f"elegoo monitor not started for {filename!r}: no chat is configured to post to")
            return None
    try:
        client = adapter.client()

        def camera_source():
            return elegoo_camera.camera_source(client)

        try:
            interval = timelapse_capture.capture_settings(_config.cfg())["capture_interval_s"]
        except Exception:
            interval = 5.0
        from . import family_control
        monitor = ElegooPrintMonitor(
            filename,
            status_fn=lambda: adapter.normalize_status(client.status()),
            camera_source_fn=camera_source,
            post=post or _default_post,
            pause_alerts=family_control.pause_alerts_for(adapter, filename, printer_id),
            capture_poll_s=interval)
        factory = thread_factory or (lambda target: threading.Thread(target=target, daemon=True,
                                                                     name=f"elegoo-watch-{filename}"))
        thread = factory(monitor.run)
        thread.start()
        return thread
    except Exception as exc:
        log(f"elegoo monitor could not start for {filename!r}: {exc!r}")
        return None
