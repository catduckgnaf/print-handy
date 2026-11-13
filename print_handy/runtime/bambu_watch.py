"""Bambu camera parity: status images, and a launched-print monitor (#294).

Bambu printers have no Moonraker listener (#256 keeps the listener
Moonraker-only), so the places the Moonraker path shows a camera image —
status, the completion post, and the camera watchdog — are served here
instead, from the Bambu LAN path itself:

- ``status_camera()`` is the on-demand piece: one snapshot from the
  configured printer's camera (printers/bambulab/camera.py), saved as a
  frame file a chat post can attach, or a plain note saying why not. It
  never raises: a status answer must not fail because a camera did.
- ``BambuPrintMonitor`` watches one print Print Handy itself launched
  (started by the print path in core/model_job._print_bambu on a confirmed
  start): it polls the printer's normalised status on the adapter, and
  when the print reaches a terminal state it makes the completion post —
  the same format_event text the Moonraker path uses — carrying the final
  camera snapshot through the MEDIA: marker when one could be taken.
  It also drives the #295 timelapse capture session for the print: on
  the first active poll it opens one shared capture session
  (#246's print_handy.runtime.timelapse_capture) fed by the adapter's
  camera frame source (printers/bambulab adapter.camera_frame_source —
  port-6000 stills for A1/P1, go2rtc stills for the X1 family), and on
  the terminal transition it stops the session, encodes the frames
  through the same encode/sizing seam the other launched-print monitors
  use, and attaches the video alongside the final snapshot. A printer
  that cannot feed frames (an X1 whose go2rtc fan-out is not running,
  a model with no verified camera protocol) simply gets no capture —
  the reason is logged once, and the completion post is unaffected.

The monitor runs on a daemon thread inside whichever process launched the
print (normally the bot). It is deliberately not a supervised process and
keeps no state on disk: if that process restarts mid-print, monitoring
stops with it — a stated limitation (docs/BAMBU-LAB.md), not a silent one.
Prints started outside Print Handy are never watched, for the same reason:
nothing here polls a printer nobody launched a print on.

Everything network- or chat-shaped is injectable (status_fn, camera_fn,
post) so the whole lifecycle is testable offline with fakes.
"""
from __future__ import annotations

import functools
import logging
import threading
import time
from pathlib import Path

from .. import logs
from . import _config
from . import discord_print_summary as print_summary
from . import local_vision
from . import stock_watch, timelapse_capture, timelapse_encode
from . import camera_watch as _cw
from . import visual_watch as _vw

WATCH_PROMPT = _cw.WATCH_PROMPT

logger = logging.getLogger(__name__)

POLL_S = 30.0
MAX_MONITOR_S = 48 * 3600.0
# The overall deadline of one camera check, the Moonraker listener's
# (VISUAL_CHECK_DEADLINE_S): a camera or verdict endpoint that never answers
# is abandoned and the operator told, instead of holding the monitor.
CHECK_DEADLINE_S = _cw.CHECK_DEADLINE_S
_ACTIVE = ("printing", "paused")
_TERMINAL = {"complete": "complete", "cancelled": "cancelled", "error": "error", "standby": "complete"}


def log(line):
    logger.info("%s", logs.redact(str(line)))


def _default_encode(session):
    """Encode a stopped session's frames and size the result for delivery.

    The same shared seam the Prusa and stock monitors use
    (stock_watch.encode_frames: the [timelapse] encoder choice with the
    CPU fallback, then the platform upload limit), so no family forks
    the frame pipeline. Returns the attachable video path, or None when
    the video exists but cannot be sized under the platform limit.
    Raises when the encode itself failed.
    """
    from . import moonraker_event_listener as mel

    raw = stock_watch.encode_frames(session.frames_dir, session.directory / "timelapse.mp4")
    try:
        return stock_watch.delivery_video(mel.platform_sized_video(raw), session.filename, session)
    except Exception as exc:
        log(f"bambu timelapse sizing failed for {session.filename!r}: {exc!r}")
        return None


def _too_big_text():
    """The completion line for a recorded-but-undeliverable timelapse, per platform."""
    try:
        from . import moonraker_event_listener as mel
        return mel._timelapse_too_big_text()
    except Exception:
        return "Timelapse was recorded but is too large to attach."


def write_frame(filename, image) -> Path:
    """Save one camera frame where chat posts can attach it (the listener's frame dir).

    The shared writer (visual_watch.write_frame) prunes the folder to the
    newest frames, so alert and status frames no longer pile up.
    """
    return _vw.write_frame(filename, image)


def status_camera(adapter):
    """(frame path, note) for the configured Bambu printer's camera. Never raises.

    On success the note is empty. On any failure — LAN not configured, a
    model whose camera path has not landed, an unreachable camera — the
    path is None and the note is the camera layer's own plain-language
    error (which never carries the access code).
    """
    try:
        image = adapter.camera_snapshot()
    except Exception as exc:
        return None, str(exc)
    try:
        return write_frame("bambu-camera", image), ""
    except Exception as exc:
        return None, f"the camera image could not be saved: {exc}"


def status_with_camera(adapter) -> dict:
    """The Bambu status snapshot, with the camera image alongside it (#294 stage 2).

    Mirrors print_stop.status()'s Bambu shape. The camera is best-effort:
    its outcome lands in `camera_note` (empty on success), and
    `camera_image_path` names the saved frame when one was taken. With no
    LAN host configured this returns the status-only answer plus the
    "camera is not configured" note instead of raising.
    """
    if not adapter.lan_configured():
        return {"state": "unknown", "filename": "", "layer": None, "total_layer": None,
                "progress": None, "print_duration": None, "camera_image_path": None,
                "camera_note": "camera is not configured: set [bambu] lan_host and lan_serial and "
                               "supply the LAN access code via the environment to see camera images"}
    live = adapter.normalize_status(adapter.client().snapshot())
    st = {"state": str(live.get("state") or "unknown"), "filename": live.get("filename") or "",
          "layer": live.get("layer"), "total_layer": live.get("total_layer"),
          "progress": live.get("progress"), "print_duration": None}
    path, note = status_camera(adapter)
    st["camera_image_path"] = str(path) if path else None
    st["camera_note"] = note
    return st


def _fact_value(adapter, name):
    """One sourced fact about the serving printer (printers/bambulab/facts.py).

    Returns the fact's value, or None when the model's facts do not record
    it — an unrecorded fact is never claimed in the verdict context.
    """
    facts = getattr(adapter, "facts", None)
    if isinstance(facts, dict):
        fact = facts.get(name)
        if fact is not None:
            return getattr(fact, "value", fact)
    return None


def vision_context(adapter) -> str:
    """The verdict context for a Bambu frame: the listener's wording, this printer's facts.

    The Moonraker context is built from its serving printer the same way;
    a Bambu frame judged against another printer's description would be a
    wrong prompt, so the enclosure and plate traits come from the serving
    model's sourced facts: "enclosed" only when the facts record an
    enclosure, "open frame" only when they record none, and the recorded
    default plate. A trait the facts do not record is not claimed.
    """
    label = getattr(adapter, "label", None) or "Bambu Lab printer"
    traits = []
    enclosed = _fact_value(adapter, "enclosed")
    if enclosed is True:
        traits.append("enclosed")
    elif enclosed is False:
        traits.append("open frame")
    plate = _fact_value(adapter, "default_plate")
    if isinstance(plate, str) and plate.strip():
        # Sentence-case the recorded plate name, keeping acronyms (PEI) as recorded.
        traits.append(" ".join(word if word.isupper() else word.lower()
                               for word in plate.split()))
    description = f"Printer: {label}"
    if traits:
        description += ", " + ", ".join(traits)
    return (f"{description}. "
            "Reply with JSON only, no markdown: {\"verdict\":\"ok|air|spaghetti|detached|empty_idle|uncertain\",\"reason\":\"short\"}. "
            "ok means plastic is on the bed and printing normally. "
            "air means the bed is empty while a print should be there. "
            "spaghetti means a tangled failed extrusion. "
            "detached means a printed part has come off the plate. "
            "empty_idle means the printer is idle and the bed is empty. "
            "uncertain means the frame is not clear enough to judge.")


def _web_alert(message, kind, filename, **fields):
    """The shared web chat alert (listener_common._web_alert). Never raises."""
    from . import listener_common
    listener_common._web_alert(message, kind, filename, **fields)


def _web_scoped(fn):
    """listener_common._web_scoped, resolved when the function runs."""
    @functools.wraps(fn)
    def run(*args, **kwargs):
        from . import listener_common
        return listener_common._web_scoped(fn)(*args, **kwargs)
    return run


@_web_scoped
def deliver_visual_alert(filename, image, reason, *, post=None, token=None, chat_id=None, buttonless=None,
                         identity=None, printer_id=None):
    """Post one watchdog alert with its snapshot, the Moonraker alert's shape (#294 stage 3).

    Same delivery as the listener's _visual_alert: the button post
    (print_alert_buttons.post_visual_alert — Pause / "It's fine" / "Stop
    asking for this print") when the platform has buttons, a plain MEDIA:
    post on buttonless standalone platforms, and a plain post as the
    fallback when the button post fails. ``identity`` is this print's mute
    identity (#476). Returns the saved frame path.
    """
    path = write_frame(filename, image)
    text = f"Print camera check: {print_summary.safe_label(filename, 'print')}\n{reason}"
    default_post = post is None
    if post is None or token is None or chat_id is None or buttonless is None:
        from . import moonraker_discord_announcements as announcer
        from . import moonraker_event_listener as mel
        post = post or mel.post
        token = token if token is not None else mel._bot_token()
        chat_id = chat_id if chat_id is not None else announcer.DISCORD_CHAT_ID
        buttonless = mel._buttonless_standalone() if buttonless is None else buttonless
    # A watchdog alert asking for a decision pings the starter (#1095),
    # once per episode; the decision is threaded to whichever transport
    # delivers. An injected post callable keeps its one-argument shape —
    # only the production sender (mel.post) takes the decision kwarg.
    from . import starter_pings
    mention = starter_pings.ping_for("watchdog", job_key=filename, episode="watchdog") or None
    media_text = text + f"\n{path}\nMEDIA:{path}"
    _web_alert(media_text, "visual", filename, identity=identity, printer_id=printer_id)
    if buttonless:
        delivered = None
        if default_post:
            delivered = post(media_text, **({"mention_user_id": mention} if mention else {}))
        else:
            post(media_text)
        if mention and default_post and not delivered:
            starter_pings.release_ping("watchdog", job_key=filename, episode="watchdog")
        return path
    try:
        from . import print_alert_buttons as pab
        kwargs = {"mention_user_id": mention} if mention else {}
        if printer_id:
            kwargs["printer_id"] = printer_id
        if pab.post_visual_alert(token, chat_id, text, filename, path, identity=identity, **kwargs):
            return path
        log("bambu visual button post failed; falling back to text")
    except Exception as exc:
        log(f"bambu visual button post error: {exc!r}")
    delivered = None
    if default_post:
        delivered = post(media_text, **({"mention_user_id": mention} if mention else {}))
    else:
        post(media_text)
    if mention and default_post and not delivered:
        # Every delivery attempt failed, so the recorded ping never
        # landed: release it, and a later alert can ping — the rule
        # post_alert and post_pause_alert already follow (#1095). Only
        # the production sender reports delivery; an injected post
        # callable's return is not a verdict.
        starter_pings.release_ping("watchdog", job_key=filename, episode="watchdog")
    return path


@_web_scoped
def deliver_dark_notice(filename, *, post=None, token=None, chat_id=None, buttonless=None,
                        identity=None, printer_id=None):
    """Post the once-per-print dark-camera notice (#476), the listener's shape.

    A camera problem, not a print alert: plain text on buttonless
    platforms; on Discord the same text with only the "Stop asking for
    this print" button. No image — the frame is black.
    """
    text = (f"Camera check: {print_summary.safe_label(filename, 'print')} — the camera image is "
            "completely dark, so I can't check the print. Check the camera or its light. "
            "Monitoring continues; nothing was sent to the printer.")
    if post is None or token is None or chat_id is None or buttonless is None:
        from . import moonraker_discord_announcements as announcer
        from . import moonraker_event_listener as mel
        post = post or mel.post
        token = token if token is not None else mel._bot_token()
        chat_id = chat_id if chat_id is not None else announcer.DISCORD_CHAT_ID
        buttonless = mel._buttonless_standalone() if buttonless is None else buttonless
    _web_alert(text, "visual-dark", filename, identity=identity, printer_id=printer_id)
    if buttonless:
        post(text)
        return
    try:
        from . import print_alert_buttons as pab
        if pab.post_visual_dark_notice(token, chat_id, text, filename, identity=identity,
                                       **({"printer_id": printer_id} if printer_id else {})):
            return
        log("bambu visual dark button post failed; falling back to text")
    except Exception as exc:
        log(f"bambu visual dark notice post error: {exc!r}")
    post(text)


@_web_scoped
def deliver_visual_text_alert(filename, reason, *, post=None, token=None, chat_id=None, buttonless=None,
                              identity=None, printer_id=None):
    """Post a watchdog alert with no snapshot: the camera capture itself failed.

    The listener's _visual_alert_text shape: the visual alert's text and
    buttons without an image upload, plain text on buttonless platforms
    and as the fallback when the button post fails.
    """
    text = f"Print camera check: {print_summary.safe_label(filename, 'print')}\n{reason}"
    default_post = post is None
    if post is None or token is None or chat_id is None or buttonless is None:
        from . import moonraker_discord_announcements as announcer
        from . import moonraker_event_listener as mel
        post = post or mel.post
        token = token or mel._bot_token()
        chat_id = chat_id if chat_id is not None else announcer.DISCORD_CHAT_ID
        buttonless = mel._buttonless_standalone() if buttonless is None else buttonless
    _web_alert(text, "visual", filename, identity=identity, printer_id=printer_id)
    from . import starter_pings
    mention = starter_pings.ping_for("watchdog", job_key=filename, episode="watchdog") or None
    if buttonless:
        delivered = None
        if default_post:
            delivered = post(text, **({"mention_user_id": mention} if mention else {}))
        else:
            post(text)
        if mention and default_post and not delivered:
            starter_pings.release_ping("watchdog", job_key=filename, episode="watchdog")
        return
    try:
        from . import print_alert_buttons as pab
        kwargs = {"mention_user_id": mention} if mention else {}
        if printer_id:
            kwargs["printer_id"] = printer_id
        if pab.post_visual_text_alert(token, chat_id, text, filename, identity=identity, **kwargs):
            return
        log("bambu visual text button post failed; falling back to text")
    except Exception as exc:
        log(f"bambu visual text alert post error: {exc!r}")
    delivered = None
    if default_post:
        delivered = post(text, **({"mention_user_id": mention} if mention else {}))
    else:
        post(text)
    if mention and default_post and not delivered:
        starter_pings.release_ping("watchdog", job_key=filename, episode="watchdog")


class BambuPrintMonitor:
    """Poll one launched Bambu print to its end; post its completion, with the final image
    and, when a capture session ran, the print's timelapse alongside it (#295).

    Alert-only unless the operator opted in: the monitor reads status and
    the camera and posts to chat. Its one printer action is the opt-in
    early-layer auto-pause (visual_watch.auto_pause_early), decided by the
    same shared rule as the Moonraker listener (visual_watch.
    auto_pause_decision: a risky print, two independent suspicious views)
    and sent through the same code as the alert's Pause button
    (``auto_pause``, which start_monitor wires to print_alert_buttons'
    pause path, print_stop.pause). Without ``auto_pause`` it holds no
    printer-control callable at all — the watchdog's Pause is the operator
    pressing the alert's button, which the gateway routes to the adapter's
    control("pause") (print_stop.pause).
    The pause post (#460, via family_control.PauseAlerts) works the same
    way: a printing -> paused transition posts the shared pause alert,
    whose Resume/Cancel buttons the gateway routes to family_control.
    """

    def __init__(self, filename, *, status_fn, camera_fn, post,
                 watch=None, verdict=None, alert=None, changed=None,
                 dark_notice=None, text_alert=None,
                 camera_source_fn=None, begin_fn=None, encode_fn=None,
                 pause_alerts=None, event_recorder=None,
                 auto_pause=None, watch_mode_fn=None, printer_id=None,
                 check_deadline_s=None,
                 clock=time.monotonic, wall=time.time, sleep=time.sleep,
                 poll_s=POLL_S, capture_poll_s=5.0):
        self.filename = filename or ""
        self._status_fn = status_fn
        self._camera_fn = camera_fn
        self._post = post
        self._pause_alerts = pause_alerts
        # #981: the always-on watchdog event recorder (watchdog_events);
        # injectable so tests never touch the real records vault.
        self._event_recorder = event_recorder
        self._watch = watch
        self._verdict = verdict
        self._alert = alert
        self._dark_notice = dark_notice
        self._text_alert = text_alert
        # This print's watchdog-mute identity (#476): one monitor exists per
        # launched print, so filename + launch time identifies the print —
        # a later reprint of the same file is a different monitor and asks
        # again. Derived in __init__ from the injected wall clock.
        self._identity = f"bambu:{self.filename}@{int(wall())}"
        self._changed = changed or _frame_changed_safe
        self._camera_source_fn = camera_source_fn
        self._begin_fn = begin_fn or self._begin_default
        self._encode_fn = encode_fn or _default_encode
        self._clock = clock
        self._wall = wall
        self._sleep = sleep
        self._poll_s = poll_s
        self._capture_poll_s = max(1.0, min(float(capture_poll_s), poll_s))
        self._session = None
        self._last_frame = None
        self._last_layer = None
        # The shared policy pieces the Moonraker listener uses too: the
        # early-layer schedule for a risky print (watch_mode_fn: () ->
        # (mode, settled), retried until the start context is found), the
        # opt-in auto-pause (auto_pause: (filename, identity) -> reply
        # text; None keeps the monitor alert-only), this print's roster
        # printer, and the overall check deadline (None runs checks inline,
        # as offline tests do; start_monitor sets CHECK_DEADLINE_S).
        self._auto_pause = auto_pause
        self._watch_mode_fn = watch_mode_fn
        self._watch_mode_settled = watch_mode_fn is None
        self._printer_id = printer_id
        self._check_deadline_s = check_deadline_s
        self._check_thread = None
        self._check_started_at = None
        self._check_timeout_noticed = False
        self._monitor_stopped = False  # set when run() stops: a late check result is dropped
        # A failed capture never advances the watch's snapshot baseline, so
        # due() keeps offering a probe on every poll while the camera is
        # down; the watch's FailureEpisode keeps that from becoming an
        # alert per poll (shared with timeouts and verdict failures).
        self.outcome = None

    # -- capture lifecycle (#295) --------------------------------------------

    def _begin_default(self, url, fetch):
        return timelapse_capture.begin(
            self.filename, self.filename, url=url, fetch=fetch, log=log)

    def _begin_capture(self):
        """Open the one capture session for this print. Never raises.

        Called at most once per monitor (the caller guards it), so a
        duplicate poll or a status flap can never open a second session.
        With no camera source wired (camera_source_fn is None — monitors
        constructed outside start_monitor) there is no capture at all,
        exactly the pre-#295 behaviour. Every failure lands as "no
        capture for this print" plus a log line naming the reason.
        """
        if self._camera_source_fn is None:
            return None
        try:
            url, fetch = self._camera_source_fn()
        except Exception as exc:
            log(f"bambu camera source could not be read for {self.filename!r}: {exc!r}; "
                "no timelapse for this print")
            return None
        if not url:
            log(f"bambu printer has no camera source for {self.filename!r}; "
                "no timelapse for this print")
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
        """Stop the session, whatever state it is in. Never raises.

        Every way out of run() passes here, so this also marks the monitor
        stopped: a camera check still running drops its result.
        """
        self._monitor_stopped = True
        session, self._session = self._session, None
        if session is not None:
            try:
                session.stop(state)
            except Exception as exc:
                log(f"timelapse capture stop failed for {self.filename!r}: {exc!r}")
        return session

    # -- terminal post ---------------------------------------------------------


    def _send(self, text):
        """Deliver a terminal post (#1095): the production sender lets the
        starter pings classifier decide the ping from the terminal kind
        and this print; an injected post callable keeps its shape."""
        if self._post is _default_post:
            return self._post(text, ping_kind=self._terminal_kind, ping_job_key=self.filename)
        return self._post(text)

    def _terminal_post(self, state, session=None):
        kind = _TERMINAL[state]
        self._terminal_kind = kind
        text = print_summary.format_event(kind, self.filename)
        video = None
        undeliverable = False
        if session is not None and session.frame_count > 0:
            try:
                video = self._encode_fn(session)
                undeliverable = video is None
            except Exception as exc:
                log(f"bambu timelapse encode failed for {self.filename!r}: {exc!r}; "
                    "posting the completion without it")
        elif session is not None:
            dark_frames = getattr(session, "dark_frames", 0)
            if dark_frames:
                # #476: name the camera-dark outcome in the completion
                # post itself (the #383 principle), not just the log.
                text = (f"{text}\n{timelapse_capture.dark_outcome_text(dark_frames)}")
                log(f"bambu timelapse for {self.filename!r} has no usable frames: "
                    f"all {dark_frames} frame(s) were completely dark; "
                    "posting the completion without it")
            else:
                log(f"bambu timelapse for {self.filename!r} has no frames "
                    f"({session.failures} failed fetch(es)); posting the completion without it")
        try:
            image = self._camera_fn()
            path = write_frame(self.filename, image)
        except Exception as exc:
            log(f"bambu completion image unavailable for {self.filename!r}: {exc!r}")
            path = None
        if video is not None:
            text = timelapse_encode.caption_with_number(text, None, self.filename)
            message = text
            if path is not None:
                message += f"\n{path}\nMEDIA:{path}"
            message += f"\nMEDIA:{video}"
            try:
                # The production sender reports a refusal as a False
                # return, not an exception (P2-173): only an explicit
                # False means undelivered — injected senders returning
                # None delivered fine.
                if self._send(message) is not False:
                    return kind
                log(f"bambu completion post with timelapse was refused for {self.filename!r}; "
                    "posting without the video")
            except Exception as exc:
                log(f"bambu completion post with timelapse failed for {self.filename!r}: {exc!r}; "
                    "posting without the video")
        if undeliverable:
            text = f"{text}\n{_too_big_text()}"
        if path is not None:
            try:
                if self._send(text + f"\n{path}\nMEDIA:{path}") is False:
                    log(f"bambu completion post with the snapshot was refused for "
                        f"{self.filename!r}; posting the text only")
                    self._send(text)
            except Exception as exc:
                log(f"bambu completion post with the snapshot failed for {self.filename!r}: {exc!r}; "
                    "posting the text only")
                self._send(text)
        else:
            self._send(text)
        return kind

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
                log(f"bambu monitor status read failed for {self.filename!r}: {exc!r}")
                self._sleep(self._poll_s)
                continue
            state = str(st.get("state") or "unknown")
            fname = st.get("filename") or ""
            if state in _ACTIVE:
                if fname and self.filename and fname != self.filename:
                    log(f"bambu monitor: a different print {fname!r} is running; {self.filename!r} is gone")
                    if self._pause_alerts:
                        self._pause_alerts.note("standby", st)
                    self._stop_capture("superseded")
                    return None
                seen_active = True
                if self._pause_alerts:
                    self._pause_alerts.note(state, st)
                self.poll_active(st)
                layer = st.get("layer")
                if isinstance(layer, int) and not isinstance(layer, bool):
                    # The layer the session's ticks carry: without it a
                    # layer-triggered capture can never fire (P2-172).
                    self._last_layer = layer
                if not capture_attempted:
                    capture_attempted = True
                    self._session = self._begin_capture()
                if self._session is not None and state == "printing" and not self._check_running():
                    # A pause holds the session open but takes no frames,
                    # exactly as the other launched-print monitors do.
                    self._session.tick(self._last_layer)
                self._sleep_between_polls(tick=state == "printing")
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
        log(f"bambu monitor gave up on {self.filename!r} after {MAX_MONITOR_S / 3600:g}h without a terminal state")
        self._stop_capture("abandoned")
        return None

    def _sleep_between_polls(self, *, tick):
        """Wait one status-poll interval, ticking the capture session inside it.

        Status keeps the monitor's poll cadence (each read is a one-shot
        MQTT snapshot); frames are due at the [timelapse] capture
        interval, so while a session is live and the print is printing
        the wait is spent in capture-interval steps that tick the
        session — the session's own trigger decides whether a frame is
        actually due. Without a live session this is exactly the old
        single sleep.
        """
        if not tick or self._session is None:
            self._sleep(self._poll_s)
            return
        elapsed = 0.0
        while elapsed < self._poll_s:
            step = min(self._capture_poll_s, self._poll_s - elapsed)
            self._sleep(step)
            elapsed += step
            if self._session is not None and not self._check_running():
                self._session.tick(self._last_layer)

    def _check_running(self):
        """True while a camera check's thread is still running.

        The capture session takes no frame meanwhile: the camera serves one
        caller at a time, as it did when the monitor waited for each check.
        Status polling goes on regardless.
        """
        thread = self._check_thread
        return thread is not None and thread.is_alive()

    def poll_active(self, st):
        """One active-print poll: the camera watchdog's trigger (#294 stage 3).

        Mirrors the Moonraker listener with the shared VisualPrintWatch
        policy: while the print is printing, layer changes drive the layer
        2 / layer 5 / stall checks (``due``) and the clock drives the
        periodic, early-layer and empty-bed re-checks (``tick``), both at
        the poll cadence. A risky print (visual_watch.risky_job on its start
        context) gets the early-layer schedule. Where the firmware reports
        no layer, ``tick`` runs the clock-only schedule and the operator is
        told once. A check still running (or hung past its deadline) skips
        this poll's check.
        """
        if self._watch is None or self._verdict is None or self._alert is None:
            return
        self._post_detector_notices()
        self._last_state = st.get("state")
        if st.get("state") != "printing":
            return
        self._settle_watch_mode()
        if not self._check_slot_free():
            return
        layer = st.get("layer")
        layer = layer if isinstance(layer, int) and not isinstance(layer, bool) else None
        elapsed = st.get("print_duration")
        elapsed = (float(elapsed) if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool)
                   and elapsed >= 0 else None)
        now = self._wall()
        if layer is not None:
            reason = self._watch.due(layer, now, snapshot_changed=None) or self._watch.tick(layer, now)
        else:
            reason = self._watch.tick(None, now, elapsed_s=elapsed)
            if reason:
                self._note_layerless_watch()
        if reason:
            self._run_check(reason, layer)

    def _settle_watch_mode(self):
        """Pick the early-layer or normal schedule once the start context exists. Never raises."""
        if self._watch_mode_settled:
            return
        try:
            mode, settled = self._watch_mode_fn()
        except Exception as exc:
            log(f"bambu camera watch mode lookup failed file={self.filename!r}: {exc!r}")
            return
        self._watch.set_mode(mode)
        self._watch_mode_settled = bool(settled)
        if mode == _vw.WATCH_MODE_EARLY:
            log(f"bambu camera watch mode=early_layer file={self.filename!r}: every layer for "
                f"{self._watch.early_layers} layers, at least every {self._watch.early_interval_s}s")

    def _note_layerless_watch(self):
        """Post, once per print, that the watch runs on a timer: the printer reports no layers."""
        if self._watch.layerless_noticed:
            return
        self._watch.layerless_noticed = True
        minutes = max(1, round(self._watch.profile["periodic_s"] / 60))
        text = (f"Camera watch: {print_summary.safe_label(self.filename, 'print')} reports no layer "
                f"numbers, so I'm checking the camera on a timer instead, about every {minutes} "
                "minutes. Stall detection needs layer numbers and is off for this print.")
        log(f"bambu camera watch file={self.filename!r}: no layer numbers reported; checking on a timer")
        try:
            self._post(text)
        except Exception as exc:
            log(f"bambu layerless notice failed for {self.filename!r}: {exc!r}")

    def _post_detector_notices(self):
        """Post the local detector's pending failure and recovery notices. Never raises."""
        try:
            if not local_vision.watch_uses_detector():
                return
            for text in local_vision.take_notices():
                log(f"bambu camera watch: {text}")
                self._post(text)
        except Exception as exc:
            log(f"bambu local detector notice failed: {exc!r}")

    def _check_slot_free(self):
        """True when no earlier check is still running (single flight).

        A check that overran the deadline is abandoned: the operator is told
        once that this print is unmonitored while the watch retries (the
        Moonraker listener's timeout notice, same throttle), and no new check
        starts until the stuck one returns.
        """
        thread = self._check_thread
        if thread is None or not thread.is_alive():
            self._check_thread = None
            return True
        if (not self._check_timeout_noticed and self._check_deadline_s is not None
                and self._clock() - self._check_started_at >= self._check_deadline_s):
            self._check_timeout_noticed = True
            self._check_timed_out()
        return False

    def _run_check(self, reason, layer):
        """Start one camera check, under the overall deadline when one is set.

        With a deadline the check runs on its own thread and this returns at
        once: the monitor keeps polling (a print that ends or changes while a
        check hangs is seen on the next poll), and the next polls'
        ``_check_slot_free`` tells the operator once the check overruns.
        """
        if self._check_deadline_s is None:
            self.visual_check(reason, layer)
            return
        self._check_started_at = self._clock()
        self._check_timeout_noticed = False
        thread = threading.Thread(target=self.visual_check, args=(reason, layer), daemon=True,
                                  name=f"bambu-check-{self.filename}")
        self._check_thread = thread
        thread.start()

    def _abandoned(self):
        """True inside a check thread whose result must be dropped.

        A check that overran its deadline (as the listener cancels its task):
        the operator was already told the print is unmonitored, and the frame
        it judged is old. Or a check still running when the monitor stopped
        (the print ended or was superseded): its verdict is about a print
        this monitor no longer watches.
        """
        thread = self._check_thread
        return (thread is not None and thread is threading.current_thread()
                and (self._check_timeout_noticed or self._monitor_stopped))

    def _check_timed_out(self):
        """The current check overran its deadline: log it and alert (throttled). Never raises."""
        self.check_timeouts = getattr(self, "check_timeouts", 0) + 1
        log(f"bambu visual check timed out file={self.filename!r} after {self._check_deadline_s:.0f}s "
            f"(timeouts so far: {self.check_timeouts}); the camera or the vision endpoint did not answer")
        try:
            if not self._watch.failure_alert_due(self._wall()):
                log(f"bambu visual timeout alert suppressed for {self.filename!r}: "
                    "already alerted within the periodic interval")
            elif self._visual_muted():
                log(f"bambu visual alert suppressed for muted print {self.filename!r}")
            elif self._text_alert is not None:
                self._text_alert(self.filename, _vw.TIMEOUT_ALERT_TEXT, identity=self._identity)
        except Exception as exc:
            log(f"bambu visual timeout alert failed file={self.filename!r}: {exc!r}")

    def _record_watchdog_event(self, verdict, *, action):
        """Best-effort #981 event recording beside this print watch.

        The recorder (watchdog_events.record_for_file by default) never
        raises by contract; this wrapper swallows even a broken injected
        recorder, because an events-file failure must never break the
        watch or the print it watches.
        """
        try:
            recorder = self._event_recorder
            if recorder is None:
                from . import watchdog_events
                recorder = watchdog_events.record_for_file
            recorder(filename=self.filename, verdict=verdict, action=action)
        except Exception as exc:
            log(f"bambu watchdog event record failed file={self.filename!r}: {exc!r}")

    def visual_check(self, reason, layer):
        """Capture and classify one frame; notify humans only, never actuate.

        The Moonraker watchdog's rules, unchanged: a suspicious verdict
        (missing plastic, spaghetti, detached, uncertain) alerts with the
        snapshot; layers that advanced over an unchanged frame are promoted
        to a stall; at sensitivity 1–2 a healthy verdict vetoes a stall.
        """
        image = None
        try:
            image = self._camera_fn()
            captured_at = self._wall()
            if _vw.frame_dark(image):
                # #476: a completely dark frame is a camera fact, never a
                # print verdict — no vision call, no stall baseline, no
                # print alert; at most one dark-camera notice per print.
                log(f"bambu visual check file={self.filename!r} reason={reason} layer={layer} "
                    "verdict=dark detail='camera image completely dark'")
                if self._watch is not None and not self._watch.dark_noticed:
                    self._watch.dark_noticed = True
                    if self._visual_muted():
                        log(f"bambu visual dark notice suppressed for muted print {self.filename!r}")
                    elif self._dark_notice is not None:
                        self._dark_notice(self.filename, identity=self._identity)
                return
            prior = self._last_frame
            changed = None if prior is None else self._changed(prior, image)
            watch = self._watch
            # The shared per-check rules (camera_watch, #1320): stall
            # promotion, then the baseline frame; a layer-progress probe
            # that is not a stall adopts the frame without moving the
            # periodic clock and asks no verdict.
            reason, _stall = _cw.promote_stall(watch, reason, changed, layer)
            self._last_frame = image
            if _cw.keep_frame(watch, reason, layer, image, self._wall()):
                return
            verdict, detail = self._verdict(image, WATCH_PROMPT)
            # Bambu Lab's recorded rules (camera_watch.FAMILY_RULES): a
            # healthy verdict vetoes a stall only where the sensitivity
            # profile allows it, and a supposedly empty idle bed during a
            # print alerts at once.
            suspicious = _cw.is_suspicious(verdict, reason=reason, changed=changed,
                                           profile=watch.profile,
                                           rules=_cw.FAMILY_RULES["bambulab_lan"])
            log(f"bambu visual check file={self.filename!r} reason={reason} layer={layer} "
                f"changed={changed} verdict={verdict} detail={detail!r}")
            if self._abandoned():
                log(f"bambu visual check discarded file={self.filename!r} reason={reason} "
                    f"verdict={verdict}: it overran its deadline")
                return
            # A verdict call that could not judge the frame is a watch failure
            # (one throttled episode with failed snapshots and timeouts); any
            # real judgement ends the episode, with one notice if it alerted.
            watch_failed = _cw.watch_failed(verdict, detail)
            if not watch_failed and watch.failures.recovered() and not self._visual_muted():
                self._post(_vw.recovery_text(print_summary.safe_label(self.filename, "print")))
            # The opt-in early-layer auto-pause: the Moonraker rule, shared
            # (visual_watch.note_verdict and auto_pause_decision).
            early = watch.in_early_window(layer)
            signature = _vw.frame_signature(image) if verdict in _vw.AUTO_PAUSE_VERDICTS else None
            streak = watch.note_verdict(verdict, layer, printer_id=self._printer_id,
                                        print_identity=self._identity, signature=signature,
                                        frame_age_s=self._wall() - captured_at, now=self._wall())
            frame_counted = watch.last_counted
            if verdict in _vw.AUTO_PAUSE_VERDICTS and early and not frame_counted:
                log(f"bambu visual check file={self.filename!r} reason={reason}: suspicious frame not "
                    f"counted toward auto-pause: {watch.last_evidence}")
            if suspicious:
                if self._visual_muted():
                    log(f"bambu visual alert suppressed for muted print {self.filename!r}")
                    self._record_watchdog_event(verdict, action="suppressed")
                elif watch_failed and not watch.failure_alert_due(self._wall()):
                    log(f"bambu visual failure alert suppressed for {self.filename!r}: "
                        "already alerted within the periodic interval")
                    self._record_watchdog_event(verdict, action="suppressed")
                else:
                    # Once per print, reserved before posting (decide_auto_pause).
                    pause_now, pause_why = _cw.decide_auto_pause(
                        watch,
                        enabled=self._auto_pause is not None and _vw.watch_settings()["auto_pause_early"],
                        printer_id=self._printer_id, identity=self._identity,
                        state=getattr(self, "_last_state", None), verdict=verdict,
                        frame_age_s=self._wall() - captured_at, early=early,
                        frame_counted=frame_counted)
                    if streak and not pause_now:
                        log(f"bambu auto-pause not sent file={self.filename!r}: {pause_why}")
                    text = _cw.alert_text(reason, layer, verdict, detail,
                                          pause_why=pause_why if pause_now else "")
                    self._alert(self.filename, image, text, identity=self._identity)
                    self._record_watchdog_event(verdict, action="auto_pause" if pause_now else "alert")
                    if pause_now:
                        # The alert's own Pause path: it re-reads the printer
                        # and refuses a different file or a printer that is
                        # no longer printing.
                        log(f"bambu auto-pause early layers file={self.filename!r} layer={layer} "
                            f"streak={streak}: sending pause")
                        reply = self._auto_pause(self.filename, self._identity)
                        log(f"bambu auto-pause early layers file={self.filename!r}: {reply}")
                        self._post(f"Auto-pause: {print_summary.safe_label(self.filename, 'print')}\n{reply}")
            else:
                self._record_watchdog_event(verdict, action="none")
        except Exception as exc:
            log(f"bambu visual check failed file={self.filename!r} reason={reason}: {exc!r}")
            if self._abandoned():
                return  # the timeout already told the operator
            if image is not None:
                try:
                    if self._watch is not None and not self._watch.failure_alert_due(self._wall()):
                        log(f"bambu visual failure alert suppressed for {self.filename!r}: "
                            "already alerted within the periodic interval")
                    elif self._visual_muted():
                        log(f"bambu visual alert suppressed for muted print {self.filename!r}")
                    else:
                        self._alert(self.filename, image, _cw.vision_failed_text(reason, layer),
                                    identity=self._identity)
                except Exception as alert_exc:
                    log(f"bambu visual alert failed file={self.filename!r}: {alert_exc!r}")
            elif self._text_alert is not None:
                # The snapshot itself failed, so there is no image to
                # attach — but the operator still gets an alert: a dead
                # camera must not silently leave the print unmonitored
                # (the Moonraker watchdog's P2-8 rule, mirrored here).
                # Throttled, though: the failed capture never updates the
                # watch's snapshot baseline, so due() offers another probe
                # on the next poll (~30 s) for as long as the camera stays
                # down. The watch's FailureEpisode (shared with timeouts and
                # verdict failures) lets the first failure alert and then at
                # most one more per periodic interval. Probes keep running;
                # only the alert is throttled, so a camera that recovers is
                # picked up on the next poll.
                try:
                    if self._watch is not None and not self._watch.failure_alert_due(self._wall()):
                        log(f"bambu visual camera-failure alert suppressed for {self.filename!r}: "
                            "already alerted within the periodic interval")
                    elif self._visual_muted():
                        log(f"bambu visual alert suppressed for muted print {self.filename!r}")
                    else:
                        self._text_alert(self.filename, _cw.camera_failed_text(reason, layer),
                                         identity=self._identity)
                except Exception as alert_exc:
                    log(f"bambu visual alert failed file={self.filename!r}: {alert_exc!r}")

    def _visual_muted(self):
        """True when this print's watchdog posts were muted (#476). Never raises."""
        try:
            from . import print_alert_buttons as pab
            return pab.visual_muted(self.filename, self._identity)
        except Exception:
            return False


def _frame_changed_safe(before, after):
    """visual_watch._frame_changed, or None when the frames cannot be compared."""
    try:
        return _vw._frame_changed(before, after)
    except Exception:
        return None


def _default_post(message, **kwargs):
    from . import moonraker_event_listener as mel
    return mel.post(message, **kwargs)


def _post_target_configured():
    """True when a completion post would have somewhere to go.

    Mirrors the listener post path's own checks: in standalone mode the
    configured platform's chat, otherwise the announcer's Discord chat.
    A print launched with no chat configured (a CLI run, a test) gets no
    monitor — there is nobody to post the completion or alerts to.
    """
    try:
        from . import moonraker_event_listener as mel
        platform = str(_config.cfg()["bot"].get("platform") or "discord")
        if platform == "telegram":
            return bool(mel._telegram_chat_id())
        if platform == "matrix":
            return bool(mel._matrix_room_id())
        from . import moonraker_discord_announcements as announcer
        return bool(announcer.DISCORD_CHAT_ID)
    except Exception:
        return False


def _default_verdict(adapter, streak=None):
    """The shared vision verdict call with this printer's context (visual_watch.visual_verdict).

    ``streak`` is a print monitor's per-print local-detector escalation
    state (#455), created fresh by start_monitor so it lives and dies
    with the print; the auto light-off driver passes none — idle plate
    checks judge single frames.
    """
    # The shared vision route (listener_common, #1320): the Bambu Lab
    # listener's auto light-off builds this too, and it must not import the
    # Moonraker listener to reach it.
    from . import listener_common as common

    def verdict(image, prompt):
        return _vw.visual_verdict(
            image, prompt, chat_url=common._vision_chat_url, model=common._VISION_MODEL,
            api_key=lambda: common._profile_env_value("CLIPROXY_API_KEY"), context=vision_context(adapter),
            local_streak=streak)

    return verdict


def _default_auto_pause(filename, identity, *, printer_id=None):
    """The opt-in early-layer auto-pause: the camera alert's Pause button path.

    print_alert_buttons.handle_click("pause") routes a Bambu printer to
    print_stop.pause, which re-reads the LAN state, checks that the alerted
    file is the one printing, and sends one pause; it never cancels.
    """
    from . import print_alert_buttons as pab
    alert = {"kind": "visual", "filename": filename, "identity": identity}
    if printer_id:
        alert["printer_id"] = str(printer_id)
    return pab.handle_click("pause", alert)


def _default_watch_mode(filename):
    """``(mode, settled)`` for this print's camera watch from its start context.

    The same rule as the Moonraker listener (visual_watch.risky_job on the
    recorded start context): a risky print gets the early-layer schedule.
    ``settled`` is False while no context is recorded yet, so the monitor
    asks again on the next poll.
    """
    from . import print_records
    context = print_records.load_context(filename) if filename else {}
    mode = _vw.WATCH_MODE_EARLY if _vw.risky_job(context) else _vw.WATCH_MODE_NORMAL
    return mode, bool(context)


def _post_watch_not_running(filename, post):
    """One notice at the launch: the camera watch is on but cannot run, and why. Never raises."""
    try:
        from . import moonraker_event_listener as mel
        reason = _vw.watch_unavailable_reason(
            lambda: mel._profile_env_value("CLIPROXY_API_KEY")) or "no vision route is ready"
        log(f"bambu camera watch not running for {filename!r}: {reason}")
        post(f"Camera watch is on but not running for "
             f"{print_summary.safe_label(filename, 'print')}: {reason}. This print is not "
             "being watched; nothing was sent to the printer.")
    except Exception as exc:
        log(f"bambu camera watch status notice failed: {exc!r}")


def start_monitor(adapter, filename, *, post=None, camera_fn=None, status_fn=None,
                  camera_source_fn=None, thread_factory=None, printer_id=None):
    """Start a BambuPrintMonitor for a just-launched print on a daemon thread.

    printer_id is the launched job's roster printer: the monitor's pause
    alerts carry it, so their Resume/Cancel buttons act on that printer
    rather than the default one (SAFE-4 / P1-101).

    Returns the thread (or None when monitoring could not start — a failed
    monitor must never fail the print that launched it).
    """
    if post is None and status_fn is None and camera_fn is None and not _post_target_configured():
        log(f"bambu monitor not started for {filename!r}: no chat is configured to post to")
        return None
    try:
        watch = verdict = alert = dark_notice = text_alert = auto_pause = None
        from . import moonraker_event_listener as mel
        if _config.cfg()["visual_watch"]["enabled"] and mel._vision_ready():
            settings = _vw.watch_settings(_config.cfg())
            watch = _vw.VisualPrintWatch(early_layers=settings["early_layers"],
                                         early_interval_s=settings["early_interval_s"])
            # One escalation streak per monitored print (#455): a fresh
            # FailureStreak here is the per-print reset — the monitor
            # (and its streak) is discarded when the print ends.
            verdict = _default_verdict(adapter, streak=local_vision.FailureStreak())
            # Every camera post names this print's roster printer, so the
            # alert buttons act on it and never on the default printer.
            alert = functools.partial(deliver_visual_alert, printer_id=printer_id)
            dark_notice = functools.partial(deliver_dark_notice, printer_id=printer_id)
            text_alert = functools.partial(deliver_visual_text_alert, printer_id=printer_id)
            auto_pause = functools.partial(_default_auto_pause, printer_id=printer_id)
        elif _config.cfg()["visual_watch"]["enabled"]:
            _post_watch_not_running(filename, post or _default_post)
        if camera_source_fn is None and hasattr(adapter, "camera_frame_source"):
            camera_source_fn = adapter.camera_frame_source
        try:
            interval = timelapse_capture.capture_settings(_config.cfg())["capture_interval_s"]
        except Exception:
            interval = 5.0
        from . import family_control
        monitor = BambuPrintMonitor(
            filename,
            status_fn=status_fn or (lambda: adapter.normalize_status(adapter.client().snapshot())),
            camera_fn=camera_fn or adapter.camera_snapshot,
            post=post or _default_post,
            watch=watch, verdict=verdict, alert=alert, dark_notice=dark_notice,
            text_alert=text_alert,
            camera_source_fn=camera_source_fn, capture_poll_s=interval,
            pause_alerts=family_control.pause_alerts_for(adapter, filename, printer_id),
            auto_pause=auto_pause, printer_id=printer_id,
            watch_mode_fn=(functools.partial(_default_watch_mode, filename)
                           if watch is not None else None),
            check_deadline_s=CHECK_DEADLINE_S)
        factory = thread_factory or (lambda target: threading.Thread(target=target, daemon=True,
                                                                     name=f"bambu-watch-{filename}"))
        thread = factory(monitor.run)
        thread.start()
        return thread
    except Exception as exc:
        log(f"bambu monitor could not start for {filename!r}: {exc!r}")
        return None
