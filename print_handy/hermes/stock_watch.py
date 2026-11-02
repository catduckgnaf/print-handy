"""Launched-print monitor for stock Flashforge jobs: completion post + timelapse (#373).

Stock Flashforge firmware has no listener (#299's registry keeps the
domain "planned"; #256 keeps the Moonraker listener Moonraker-only), so —
exactly as #295 Stage 2 prescribes for the listener-less families, and
following the Bambu monitor's shape (#294) — the print path starts this
monitor for a print Print Handy itself launched. It polls the adapter's
normalised /detail status, and:

- on the first active poll it opens one shared timelapse capture session
  (#246's print_handy.hermes.timelapse_capture) fed by the camera source
  the printer itself advertises in /detail (printers.flashforge_stock.
  camera — the AD5X has no built-in camera, so an empty cameraStreamUrl
  simply means no capture for this print, logged once);
- on the terminal transition it stops the session, encodes the frames to
  an H.264 MP4, sizes it through the listener's platform sizing
  (platform_sized_video: the configured platform's upload limit, the
  [timelapse] encoder choice, CPU fallback), and makes the completion
  post — the same format_event text every family uses — with the video
  attached through the MEDIA: marker.

Failure posture (the #235 lesson): capture, the stream control, the
encode and the sizing each fail soft into a log line; the completion
post always goes out, with the plain text when there is no video and
with the recorded-but-too-large line when a video exists but cannot be
sized under the platform limit. Duplicate polls and status flaps never
re-open the session and never post twice: the capture attempt and the
terminal post each happen at most once per monitor.

The monitor runs on a daemon thread inside whichever process launched
the print (normally the bot) and keeps its capture state in the session
manifest on disk, but the monitor itself is not restarted: if that
process restarts mid-print, monitoring stops with it — the same stated
limitation as the Bambu monitor. Prints started outside Print Handy are
never watched: nothing here polls a printer nobody launched a print on.

Everything network- or chat-shaped is injectable (status_fn,
camera_url_fn, stream_fn, begin_fn, encode_fn, post) so the whole
lifecycle is testable offline with fakes.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path

from .. import logs
from ..printers.flashforge_stock import camera as stock_camera
from . import _config
from . import discord_print_summary as print_summary
from . import timelapse_capture, vertical_short

logger = logging.getLogger(__name__)

POLL_S = 30.0
MAX_MONITOR_S = 48 * 3600.0
TIMELAPSE_FPS = 10
_ACTIVE = ("printing", "paused")
_TERMINAL = {"complete": "complete", "cancelled": "cancelled", "error": "error", "standby": "complete"}


def log(line):
    logger.info("%s", logs.redact(str(line)))


def encode_frames(frames_dir, target, *, fps=TIMELAPSE_FPS, ffmpeg_bin=None, encoder=None, run=None):
    """Encode one session's frames (frame-%06d.jpg) to an H.264 MP4 at target.

    ffmpeg is the encoder — the codec choice comes from the same
    vertical_short seam the sizing shrink uses, with the [timelapse]
    encoder choice, and a hardware encode that fails retries with the
    CPU encoder (the platform_sized_video contract). Raises when no
    encode produced the file: the caller degrades to the text post.
    """
    from . import moonraker_event_listener as mel

    ffmpeg_bin = ffmpeg_bin or mel.FFMPEG_BIN
    encoder = encoder or mel._timelapse_encoder()
    run = run or subprocess.run
    target = Path(target)
    partial = target.with_name(f".{target.name}.partial.mp4")
    pattern = str(Path(frames_dir) / "frame-%06d.jpg")

    def attempt(choice):
        run([ffmpeg_bin, "-y", "-v", "error", *vertical_short.encoder_input_args(choice),
             "-framerate", str(fps), "-i", pattern, "-an",
             "-vf", vertical_short.encoder_filter(choice, "scale='min(1280,iw)':-2"),
             *vertical_short.encoder_args(choice),
             "-movflags", "+faststart", str(partial)],
            capture_output=True, timeout=600, check=True)

    try:
        attempt(encoder)
    except Exception:
        if encoder == "cpu":
            raise
        log(f"timelapse {encoder} encode failed for {target.name}; retrying with the CPU encoder")
        attempt("cpu")
    if not partial.is_file() or partial.stat().st_size <= 0:
        raise RuntimeError(f"ffmpeg produced no video at {partial}")
    partial.replace(target)
    return target


def _default_encode(session):
    """Encode a stopped session's frames and size the result for delivery.

    Returns the attachable video path, or None when the video exists but
    cannot be sized under the platform upload limit (the shrink failure
    is logged here; the caller adds the recorded-but-too-large line).
    Raises when the encode itself failed.
    """
    from . import moonraker_event_listener as mel

    raw = encode_frames(session.frames_dir, session.directory / "timelapse.mp4")
    try:
        return mel.platform_sized_video(raw)
    except Exception as exc:
        log(f"stock timelapse sizing failed for {session.filename!r}: {exc!r}")
        return None


def _too_big_text():
    """The completion line for a recorded-but-undeliverable timelapse, per platform."""
    try:
        from . import moonraker_event_listener as mel
        return mel._timelapse_too_big_text()
    except Exception:
        return "Timelapse was recorded but is too large to attach."


class StockPrintMonitor:
    """Poll one launched stock Flashforge print to its end; post its completion.

    Alert-only by construction, like the Bambu monitor: it reads status
    and the camera and posts to chat; the only printer write it can make
    is opening/closing the camera stream it is capturing from. Its pause
    post (#460) goes through family_control.PauseAlerts: a printing ->
    paused transition posts the shared pause alert, whose Resume/Cancel
    buttons actuate only when an operator presses one — the monitor
    itself holds no job-control callable.
    """

    def __init__(self, filename, *, status_fn, camera_url_fn, stream_fn, post,
                 begin_fn=None, encode_fn=None, pause_alerts=None,
                 clock=time.monotonic, wall=time.time, sleep=time.sleep,
                 poll_s=POLL_S, capture_poll_s=5.0):
        self.filename = filename or ""
        self._status_fn = status_fn
        self._camera_url_fn = camera_url_fn
        self._stream_fn = stream_fn
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
        self._stream_open = False
        self.outcome = None

    # -- capture lifecycle -------------------------------------------------

    def _begin_default(self, url):
        return timelapse_capture.begin(
            self.filename, self.filename, url=url,
            fetch=timelapse_capture.fetch_mjpeg_frame, log=log)

    def _begin_capture(self):
        """Open the one capture session for this print. Never raises.

        Called at most once per monitor (the caller guards it), so a
        duplicate poll or a status flap can never open a second session.
        Every failure lands as "no capture for this print" plus a log
        line naming the reason — the diagnostic the issue asks for.
        """
        try:
            url = self._camera_url_fn()
        except Exception as exc:
            log(f"stock camera source could not be read for {self.filename!r}: {exc!r}; "
                "no timelapse for this print")
            return None
        if not url:
            log(f"stock printer advertises no camera source for {self.filename!r} "
                "(cameraStreamUrl empty — no camera installed or camera disabled); "
                "no timelapse for this print")
            return None
        try:
            self._stream_fn("open")
            self._stream_open = True
        except Exception as exc:
            # The stream may already be open (or the firmware may serve it
            # without the control); capture proceeds and the fetch decides.
            log(f"stock camera stream open failed for {self.filename!r}: {exc!r}; "
                "trying to capture anyway")
        try:
            session = self._begin_fn(url)
        except Exception as exc:
            log(f"timelapse capture could not begin for {self.filename!r}: {exc!r}")
            return None
        if session is None:
            log(f"timelapse capture unavailable for {self.filename!r} "
                "(capture disabled or refused); the print continues without it")
        return session

    def _stop_capture(self, state):
        """Stop the session and the stream, whatever state they are in. Never raises."""
        session, self._session = self._session, None
        if session is not None:
            try:
                session.stop(state)
            except Exception as exc:
                log(f"timelapse capture stop failed for {self.filename!r}: {exc!r}")
        if self._stream_open:
            self._stream_open = False
            try:
                self._stream_fn("close")
            except Exception as exc:
                log(f"stock camera stream close failed for {self.filename!r}: {exc!r}")
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
                log(f"stock timelapse encode failed for {self.filename!r}: {exc!r}; "
                    "posting the completion without it")
        elif session is not None:
            dark_frames = getattr(session, "dark_frames", 0)
            if dark_frames:
                # #476: name the camera-dark outcome in the completion
                # post itself (the #383 principle), not just the log.
                text = (f"{text}\n{timelapse_capture.dark_outcome_text(dark_frames)}")
                log(f"stock timelapse for {self.filename!r} has no usable frames: "
                    f"all {dark_frames} frame(s) were completely dark; "
                    "posting the completion without it")
            else:
                log(f"stock timelapse for {self.filename!r} has no frames "
                    f"({session.failures} failed fetch(es)); posting the completion without it")
        if video is not None:
            try:
                self._post(f"{text}\nMEDIA:{video}")
                return kind
            except Exception as exc:
                log(f"stock completion post with timelapse failed for {self.filename!r}: {exc!r}; "
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
                # A reconnect or a refused read costs one poll, never the
                # session: capture state lives in this monitor, and the
                # session's own tick retries its fetches the same way.
                log(f"stock monitor status read failed for {self.filename!r}: {exc!r}")
                self._sleep(self._poll_s)
                continue
            state = str(st.get("state") or "unknown")
            fname = st.get("filename") or ""
            if state in _ACTIVE:
                if fname and self.filename and fname != self.filename:
                    log(f"stock monitor: a different print {fname!r} is running; "
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
                    self._session.tick()
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
        log(f"stock monitor gave up on {self.filename!r} after {MAX_MONITOR_S / 3600:g}h "
            "without a terminal state")
        self._stop_capture("abandoned")
        return None


def _default_post(message):
    from . import moonraker_event_listener as mel
    return mel.post(message)


def start_monitor(adapter, filename, *, post=None, thread_factory=None, printer_id=None):
    """Start a StockPrintMonitor for a just-launched stock print on a daemon thread.
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
            log(f"stock monitor not started for {filename!r}: no chat is configured to post to")
            return None
    try:
        host = str(((adapter.config or {}).get("flashforge_stock") or {}).get("host") or "").strip()
        try:
            interval = timelapse_capture.capture_settings(_config.cfg())["capture_interval_s"]
        except Exception:
            interval = 5.0
        from . import family_control
        monitor = StockPrintMonitor(
            filename,
            status_fn=lambda: adapter.normalize_status(adapter.client().detail()),
            camera_url_fn=lambda: stock_camera.stream_url_for(adapter.client(), host),
            stream_fn=lambda action: stock_camera.set_stream(adapter.client(), action),
            post=post or _default_post,
            pause_alerts=family_control.pause_alerts_for(adapter, filename, printer_id),
            capture_poll_s=interval)
        factory = thread_factory or (lambda target: threading.Thread(target=target, daemon=True,
                                                                     name=f"stock-watch-{filename}"))
        thread = factory(monitor.run)
        thread.start()
        return thread
    except Exception as exc:
        log(f"stock monitor could not start for {filename!r}: {exc!r}")
        return None
