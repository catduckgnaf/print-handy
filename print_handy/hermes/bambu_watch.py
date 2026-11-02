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
  (#246's print_handy.hermes.timelapse_capture) fed by the adapter's
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

import logging
import os
import threading
import time
from pathlib import Path

from .. import logs
from . import _config
from . import discord_print_summary as print_summary
from . import local_vision
from . import stock_watch, timelapse_capture
from . import visual_watch as _vw

WATCH_PROMPT = "Is there plastic on the bed under the nozzle, or is there spaghetti/detachment?"

logger = logging.getLogger(__name__)

POLL_S = 30.0
MAX_MONITOR_S = 48 * 3600.0
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
        return mel.platform_sized_video(raw)
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
    """Save one camera frame where chat posts can attach it (the listener's frame dir)."""
    target = Path(os.environ.get("PRINT_WATCH_FRAME_DIR") or _config.cache_dir() / "print-watch")
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{int(time.time())}-{Path(filename or 'print').stem}.jpg"
    path.write_bytes(image)
    return path


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


def vision_context(adapter) -> str:
    """The verdict context for a Bambu frame: the listener's wording, this printer's name.

    The Moonraker context names its printer ("Flashforge AD5X"); a Bambu
    frame judged against another printer's description would be a wrong
    prompt, so the context is built from the configured printer's label.
    """
    label = getattr(adapter, "label", None) or "Bambu Lab printer"
    return (f"Printer: {label}, enclosed, smooth PEI plate. "
            "Reply with JSON only, no markdown: {\"verdict\":\"ok|air|spaghetti|detached|empty_idle|uncertain\",\"reason\":\"short\"}. "
            "ok means plastic is on the bed and printing normally. "
            "air means the bed is empty while a print should be there. "
            "spaghetti means a tangled failed extrusion. "
            "detached means a printed part has come off the plate. "
            "empty_idle means the printer is idle and the bed is empty. "
            "uncertain means the frame is not clear enough to judge.")


def deliver_visual_alert(filename, image, reason, *, post=None, token=None, chat_id=None, buttonless=None,
                         identity=None):
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
    if post is None or token is None or chat_id is None or buttonless is None:
        from . import moonraker_discord_announcements as announcer
        from . import moonraker_event_listener as mel
        post = post or mel.post
        token = token if token is not None else mel._bot_token()
        chat_id = chat_id if chat_id is not None else announcer.DISCORD_CHAT_ID
        buttonless = mel._buttonless_standalone() if buttonless is None else buttonless
    if buttonless:
        post(text + f"\n{path}\nMEDIA:{path}")
        return path
    try:
        from . import print_alert_buttons as pab
        if pab.post_visual_alert(token, chat_id, text, filename, path, identity=identity):
            return path
        log("bambu visual button post failed; falling back to text")
    except Exception as exc:
        log(f"bambu visual button post error: {exc!r}")
    post(text + f"\n{path}\nMEDIA:{path}")
    return path


def deliver_dark_notice(filename, *, post=None, token=None, chat_id=None, buttonless=None,
                        identity=None):
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
    if buttonless:
        post(text)
        return
    try:
        from . import print_alert_buttons as pab
        if pab.post_visual_dark_notice(token, chat_id, text, filename, identity=identity):
            return
        log("bambu visual dark button post failed; falling back to text")
    except Exception as exc:
        log(f"bambu visual dark notice post error: {exc!r}")
    post(text)


def deliver_visual_text_alert(filename, reason, *, post=None, token=None, chat_id=None, buttonless=None,
                              identity=None):
    """Post a watchdog alert with no snapshot: the camera capture itself failed.

    The listener's _visual_alert_text shape: the visual alert's text and
    buttons without an image upload, plain text on buttonless platforms
    and as the fallback when the button post fails.
    """
    text = f"Print camera check: {print_summary.safe_label(filename, 'print')}\n{reason}"
    if post is None or token is None or chat_id is None or buttonless is None:
        from . import moonraker_discord_announcements as announcer
        from . import moonraker_event_listener as mel
        post = post or mel.post
        token = token or mel._bot_token()
        chat_id = chat_id if chat_id is not None else announcer.DISCORD_CHAT_ID
        buttonless = mel._buttonless_standalone() if buttonless is None else buttonless
    if buttonless:
        post(text)
        return
    try:
        from . import print_alert_buttons as pab
        if pab.post_visual_text_alert(token, chat_id, text, filename, identity=identity):
            return
        log("bambu visual text button post failed; falling back to text")
    except Exception as exc:
        log(f"bambu visual text alert post error: {exc!r}")
    post(text)


class BambuPrintMonitor:
    """Poll one launched Bambu print to its end; post its completion, with the final image
    and, when a capture session ran, the print's timelapse alongside it (#295).

    Alert-only by construction: the monitor reads status and the camera and
    posts to chat. It holds no printer-control callable at all — the
    watchdog's Pause is the operator pressing the alert's button, which the
    gateway routes to the adapter's control("pause") (print_stop.pause).
    The pause post (#460, via family_control.PauseAlerts) works the same
    way: a printing -> paused transition posts the shared pause alert,
    whose Resume/Cancel buttons the gateway routes to family_control.
    """

    def __init__(self, filename, *, status_fn, camera_fn, post,
                 watch=None, verdict=None, alert=None, changed=None,
                 dark_notice=None, text_alert=None,
                 camera_source_fn=None, begin_fn=None, encode_fn=None,
                 pause_alerts=None,
                 clock=time.monotonic, wall=time.time, sleep=time.sleep,
                 poll_s=POLL_S, capture_poll_s=5.0):
        self.filename = filename or ""
        self._status_fn = status_fn
        self._camera_fn = camera_fn
        self._post = post
        self._pause_alerts = pause_alerts
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
        self._last_periodic_at = None
        # When the last camera-failure text alert went out. A failed
        # capture never advances the watch's snapshot baseline, so due()
        # keeps offering a probe on every poll while the camera is down;
        # this stamp is what keeps that from becoming an alert per poll.
        self._last_camera_failure_alert_at = None
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
        """Stop the session, whatever state it is in. Never raises."""
        session, self._session = self._session, None
        if session is not None:
            try:
                session.stop(state)
            except Exception as exc:
                log(f"timelapse capture stop failed for {self.filename!r}: {exc!r}")
        return session

    # -- terminal post ---------------------------------------------------------

    def _terminal_post(self, state, session=None):
        kind = _TERMINAL[state]
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
            message = text
            if path is not None:
                message += f"\n{path}\nMEDIA:{path}"
            message += f"\nMEDIA:{video}"
            try:
                # The production sender reports a refusal as a False
                # return, not an exception (P2-173): only an explicit
                # False means undelivered — injected senders returning
                # None delivered fine.
                if self._post(message) is not False:
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
                if self._post(text + f"\n{path}\nMEDIA:{path}") is False:
                    log(f"bambu completion post with the snapshot was refused for "
                        f"{self.filename!r}; posting the text only")
                    self._post(text)
            except Exception as exc:
                log(f"bambu completion post with the snapshot failed for {self.filename!r}: {exc!r}; "
                    "posting the text only")
                self._post(text)
        else:
            self._post(text)
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
                if self._session is not None and state == "printing":
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
            if self._session is not None:
                self._session.tick(self._last_layer)

    def poll_active(self, st):
        """One active-print poll: the camera watchdog's trigger (#294 stage 3).

        Mirrors the Moonraker listener: while the print is printing, the
        shared VisualPrintWatch policy decides from layer and time when a
        check is due (layer 2, layer 5, periodic, stall probes). Where the
        firmware reports no layer, the policy cannot run — the periodic
        interval from the same sensitivity profile still applies, driven
        by the poll cadence, exactly as the issue specifies.
        """
        if self._watch is None or self._verdict is None or self._alert is None:
            return
        if st.get("state") != "printing":
            return
        layer = st.get("layer")
        layer = layer if isinstance(layer, int) and not isinstance(layer, bool) else None
        now = self._wall()
        if layer is not None:
            reason = self._watch.due(layer, now, snapshot_changed=None)
        else:
            if self._last_periodic_at is None:
                self._last_periodic_at = now
                return
            if now - self._last_periodic_at < self._watch.profile["periodic_s"]:
                return
            self._last_periodic_at = now
            reason = "periodic"
        if reason:
            self.visual_check(reason, layer)

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
            snapshot_before = watch.last_snapshot_layer
            stall = (reason in {"layer_progress", "periodic"} and changed is False
                     and isinstance(layer, int) and layer >= 5
                     and snapshot_before and layer - snapshot_before >= watch.profile["stall_layers"]
                     and watch.alerted_stall_layer is None)
            if stall:
                reason = "stalled_layers"
                watch.alerted_stall_layer = layer
            if reason != "layer_progress":
                self._last_frame = image
                watch.record(layer, self._wall(), image)
            if reason == "layer_progress":
                # A layer-progress probe that is not a stall adopts the new
                # frame as the baseline without moving the periodic clock.
                self._last_frame = image
                watch.last_snapshot = image
                if isinstance(layer, int):
                    watch.last_snapshot_layer = max(watch.last_snapshot_layer, layer)
                    watch.progress_seen_layer = layer
                return
            verdict, detail = self._verdict(image, WATCH_PROMPT)
            suspicious = verdict in _vw.VISUAL_SUSPICIOUS_VERDICTS
            if reason == "stalled_layers" and changed is False:
                suspicious = not (watch.profile["vision_veto"] and verdict in _vw.VISUAL_HEALTHY_VERDICTS)
            if verdict == "uncertain":
                suspicious = True
            log(f"bambu visual check file={self.filename!r} reason={reason} layer={layer} "
                f"changed={changed} verdict={verdict} detail={detail!r}")
            if suspicious:
                if self._visual_muted():
                    log(f"bambu visual alert suppressed for muted print {self.filename!r}")
                else:
                    self._alert(self.filename, image,
                                f"{reason} at layer {layer or 'unknown'}: {verdict}; "
                                f"{detail or 'visual check needs operator review'}",
                                identity=self._identity)
        except Exception as exc:
            log(f"bambu visual check failed file={self.filename!r} reason={reason}: {exc!r}")
            if image is not None:
                try:
                    if self._visual_muted():
                        log(f"bambu visual alert suppressed for muted print {self.filename!r}")
                    else:
                        self._alert(self.filename, image,
                                    f"{reason} at layer {layer or 'unknown'}: the vision check could not be completed, "
                                    "so treat this as unreviewed and check the print on the printer screen",
                                    identity=self._identity)
                except Exception as alert_exc:
                    log(f"bambu visual alert failed file={self.filename!r}: {alert_exc!r}")
            elif self._text_alert is not None:
                # The snapshot itself failed, so there is no image to
                # attach — but the operator still gets an alert: a dead
                # camera must not silently leave the print unmonitored
                # (the Moonraker watchdog's P2-8 rule, mirrored here).
                # Throttled separately, though: the failed capture never
                # updates the watch's snapshot baseline, so due() offers
                # another probe on the next poll (~30 s) for as long as
                # the camera stays down — without a throttle each probe
                # would repost this alert instead of waiting out the
                # profile's periodic interval like a normal alert cycle.
                # Probes keep running; only the alert is throttled, so a
                # camera that recovers is picked up on the next poll.
                try:
                    if self._visual_muted():
                        log(f"bambu visual alert suppressed for muted print {self.filename!r}")
                    elif self._camera_failure_alert_throttled():
                        log(f"bambu visual camera-failure alert suppressed for {self.filename!r}: "
                            "already alerted within the periodic interval")
                    else:
                        self._text_alert(self.filename,
                                         f"{reason} at layer {layer or 'unknown'}: the camera snapshot could not be captured, "
                                         "so this print is unmonitored until the camera answers again; check the camera",
                                         identity=self._identity)
                        self._last_camera_failure_alert_at = self._wall()
                except Exception as alert_exc:
                    log(f"bambu visual alert failed file={self.filename!r}: {alert_exc!r}")

    def _camera_failure_alert_throttled(self):
        """True when a camera-failure alert already went out inside the interval.

        The interval is the watch profile's periodic_s — the same spacing
        the watch's normal alert cycle uses (900 s at the default
        sensitivity) — so a dead camera nags no more often than a healthy
        one would be checked. With no watch wired there is no interval to
        honor and nothing throttles.
        """
        if self._last_camera_failure_alert_at is None or self._watch is None:
            return False
        return self._wall() - self._last_camera_failure_alert_at < self._watch.profile["periodic_s"]

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


def _default_post(message):
    from . import moonraker_event_listener as mel
    return mel.post(message)


def _post_target_configured():
    """True when a completion post would have somewhere to go.

    Mirrors the listener post path's own checks: in standalone mode the
    configured platform's chat, otherwise the announcer's Discord chat.
    A print launched with no chat configured (a CLI run, a test) gets no
    monitor — there is nobody to post the completion or alerts to.
    """
    try:
        from . import moonraker_event_listener as mel
        if _config.standalone():
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
    from . import moonraker_event_listener as mel

    def verdict(image, prompt):
        return _vw.visual_verdict(
            image, prompt, chat_url=mel._vision_chat_url, model=mel._VISION_MODEL,
            api_key=lambda: mel._profile_env_value("CLIPROXY_API_KEY"), context=vision_context(adapter),
            local_streak=streak)

    return verdict


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
        watch = verdict = alert = dark_notice = text_alert = None
        from . import moonraker_event_listener as mel
        if _config.cfg()["visual_watch"]["enabled"] and mel._vision_ready():
            watch = _vw.VisualPrintWatch()
            # One escalation streak per monitored print (#455): a fresh
            # FailureStreak here is the per-print reset — the monitor
            # (and its streak) is discarded when the print ends.
            verdict = _default_verdict(adapter, streak=local_vision.FailureStreak())
            alert = deliver_visual_alert
            dark_notice = deliver_dark_notice
            text_alert = deliver_visual_text_alert
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
            pause_alerts=family_control.pause_alerts_for(adapter, filename, printer_id))
        factory = thread_factory or (lambda target: threading.Thread(target=target, daemon=True,
                                                                     name=f"bambu-watch-{filename}"))
        thread = factory(monitor.run)
        thread.start()
        return thread
    except Exception as exc:
        log(f"bambu monitor could not start for {filename!r}: {exc!r}")
        return None
