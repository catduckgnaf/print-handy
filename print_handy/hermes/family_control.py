"""Start/pause parity for the non-Moonraker printer families (#460).

One shared mechanism, not five copies. Moonraker has had the full event
contract all along — start posts (launched and external), pause posts
with Resume/Cancel buttons, chat resume/cancel — from its domain
listener plus ``print_control`` / ``print_stop``. Every other family had
only a launched-print monitor that posts the terminal post and treats
"paused" as a silent active state. This module is the shared machinery
that closes that gap:

- ``FamilyControl`` — resume/cancel for one family's printer through the
  family's *own* adapter ``control()`` operation, with the same
  discipline the Moonraker paths use: re-read the state first, refuse
  when the state no longer matches, refuse when a different file is
  loaded than the one the operator confirmed, send at most one command,
  then report what the read-back actually shows. Nothing here ever
  points the Moonraker transport at a non-Moonraker endpoint.
- The pause-alert posting path (``post_pause_alert`` /
  ``retire_pause_alerts`` / ``PauseAlerts``) — the same Discord button
  alert the Moonraker listener posts (``print_alert_buttons``), with the
  alert record carrying its connection (and roster printer id) so the
  gateway's click handler can route the press back here instead of to
  the Moonraker control path.
- ``status_fn_for`` — the one place that knows how each family's
  adapter reads a normalised status (the same reads the launched-print
  monitors poll), shared by the control path, the monitors and the
  polling listener domains.
- ``BambuEventSink`` — the consumer that wires the Bambu domain
  listener's already-detected transitions (#299 Stage B) to these
  posts. The listener detected pauses all along; its sink was log-only.
- ``FamilyAutoLight`` — the #516 auto light-off automation for the two
  families with a switchable chamber light (Bambu Lab, Elegoo SDCP):
  the Moonraker listener's light hooks are the reference, this driver
  is their family counterpart, feeding the same pure
  ``light_control.AutoLightPolicy`` the family's lifecycle transitions
  and idle verdicts. It is the one printer write a family listener
  makes unprompted, and it extends to exactly one accessory — the
  light — only when the operator opted in.

Who posts what, per print — exactly one owner for each post:

- A print Print Handy launched is claimed by the print path
  (``model_job.claim_start_announcement``, the same claim the Moonraker
  listener honours): its pause alerts come from its launched-print
  monitor (``PauseAlerts``), its start confirmation and completion from
  the print path and the monitor, exactly as before this change.
- A print started outside Print Handy has no monitor: the family's
  listener domain (the Bambu listener's sink, or
  ``family_event_listener`` for the polling families) posts its start,
  its pause alerts and its completion, and retires its pause alerts
  when it ends.
- The listener retires pause alerts at a print's end even for claimed
  prints: retiring is idempotent (the alert record is claimed under a
  lock by whoever retires first), and it covers the case where the
  monitor that posted the alert is gone by the time the print ends.
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import Any

from .. import logs
from . import discord_print_summary as print_summary

logger = logging.getLogger(__name__)

# The families this module serves: every connection with an adapter
# control surface and no Moonraker transport. Moonraker keeps its own
# listener/control path (the reference); offline has no printer contact.
FAMILY_CONNECTIONS = ("bambulab_lan", "prusalink", "elegoo_sdcp",
                      "flashforge_stock", "octoprint")

ACTIVE = ("printing", "paused")
# States a print ends in, as the adapters' normalised statuses name them.
# "standby" is a terminal observation too: a print that returns to standby
# without a terminal state was stopped at the printer (#370's rule).
TERMINAL_STATES = ("complete", "cancelled", "error", "standby")

_RETIRE_HEADLINES = {
    "complete": "Print complete.",
    "cancelled": "Print cancelled.",
    "error": "Print stopped with an error.",
    "standby": "Print ended at the printer.",
}


def log(line):
    logger.info("%s", logs.redact(str(line)))


def connection_of(adapter) -> str | None:
    """An adapter's connection, asked the way the rest of the codebase asks.

    Adapters have no ``vendor`` attribute: the connection is the
    adapter's ``connection`` property (the vendor profile's connection),
    with the adapter's ``transport`` as the fallback (#409).
    """
    return getattr(adapter, "connection", None) or getattr(adapter, "transport", None)


def status_fn_for(adapter):
    """A zero-arg callable returning the adapter's normalised status.

    The one place that knows each family's raw status read — the same
    reads the launched-print monitors poll in their ``start_monitor``.
    The client is built per call, as ``print_stop.status`` does for
    Bambu: a control click or a listener poll never holds a stale
    client, and an unconfigured family raises here, where the caller
    treats it as a failed read.
    """
    connection = connection_of(adapter)
    if connection == "bambulab_lan":
        return lambda: adapter.normalize_status(adapter.client().snapshot())
    if connection == "prusalink":
        return lambda: adapter.normalize_status(adapter.client().snapshot())
    if connection == "elegoo_sdcp":
        return lambda: adapter.normalize_status(adapter.client().status())
    if connection == "octoprint":
        return lambda: adapter.normalize_status(adapter.client().status())
    if connection == "flashforge_stock":
        return lambda: adapter.normalize_status(adapter.client().detail())
    raise ValueError(f"no normalised status read for connection {connection!r}")


def adapter_for(printer_id: str | None = None):
    """The configured printer's adapter, or one roster entry's (#335)."""
    from print_handy import printers
    if not printer_id:
        return printers.current()
    from print_handy import settings
    return printers.get(settings.by_id(printer_id)["model"], settings.view_for(printer_id))


class FamilyControl:
    """Resume/cancel for one non-Moonraker printer, through its own adapter.

    Mirrors ``print_control.resume`` / ``print_stop.cancel``: re-read
    first, one command at most, read back, truthful text. State and
    filename checks run against the family's normalised status; where a
    family's status carries no filename, the filename check degrades to
    the state check alone (the same both-known rule the monitors use).
    """

    def __init__(self, adapter, *, status_fn=None, sleep=time.sleep):
        self.adapter = adapter
        self.connection = connection_of(adapter)
        self._status_fn = status_fn or status_fn_for(adapter)
        self._sleep = sleep

    def status(self) -> dict[str, Any]:
        return self._status_fn()

    def resume(self, expected_filename: str = "", wait_s: float = 20,
               poll_s: float = 1.0) -> tuple[bool, str]:
        """One resume for the confirmed paused file, then read back. Never retried."""
        before = self.status()
        state = str(before.get("state") or "unknown")
        if state == "printing":
            return False, "Nothing to resume: it's already printing. Nothing sent."
        if state != "paused":
            return False, f"Nothing paused to resume (printer is {state}). Nothing sent."
        if expected_filename and before.get("filename") and before["filename"] != expected_filename:
            return False, "A different print is paused than the one in this alert. Nothing sent."
        self.adapter.control("resume")
        deadline = time.monotonic() + wait_s
        after = before
        while time.monotonic() < deadline:
            self._sleep(poll_s)
            try:
                after = self.status()
            except Exception:
                continue
            if after.get("state") == "printing":
                break
        if after.get("state") == "printing":
            where = where_text(after)
            return True, f"Resumed. Printer now reports printing{', ' + where if where else ''}."
        # #837 bonus: same contract as print_control.resume — a resume
        # that never took is a failure, not a success with a caveat.
        return False, (f"Resume sent, but the printer still reports {after.get('state')} "
                       f"after {wait_s:.0f}s. Check the printer screen. Not retrying.")

    def cancel(self, expected_filename: str = "", wait_s: float = 20,
               poll_s: float = 1.0) -> tuple[bool, str]:
        """One cancel, only if the same file is still printing or paused. Never retried."""
        before = self.status()
        state = str(before.get("state") or "unknown")
        if state not in ACTIVE:
            return False, f"Nothing to stop: printer is {state}. Nothing sent."
        if expected_filename and before.get("filename") and before["filename"] != expected_filename:
            return False, "A different print is running now than the one in this alert. Nothing sent."
        self.adapter.control("stop")
        deadline = time.monotonic() + wait_s
        after = before
        while time.monotonic() < deadline:
            self._sleep(poll_s)
            try:
                after = self.status()
            except Exception:
                continue
            if after.get("state") not in ACTIVE:
                break
        if after.get("state") in ACTIVE:
            return True, (f"Cancel sent, but the printer still reports {after['state']} "
                          f"after {wait_s:.0f}s. Check the printer screen. Not retrying.")
        if self.connection == "bambulab_lan" and after.get("state") == "error":
            # A Bambu printer reports a stopped job as FAILED until the
            # operator clears it on the screen (print_stop.cancel's rule).
            return True, ("Stopped. The printer shows the cancelled job as an error "
                          "until you acknowledge it on its screen.")
        return True, f"Stopped. Printer now reports {after.get('state')}."


def control_for(printer_id: str | None = None) -> FamilyControl | None:
    """The family control for the configured (or one roster) printer.

    None when the printer is not one of the parity families — Moonraker
    keeps its own control path, and offline never contacts the printer.
    """
    adapter = adapter_for(printer_id)
    if connection_of(adapter) not in FAMILY_CONNECTIONS:
        return None
    return FamilyControl(adapter)


def where_text(st: dict[str, Any]) -> str:
    """'layer 12/240, 42% done' from a normalised status; '' when unknown."""
    bits = []
    if st.get("layer") and st.get("total_layer"):
        bits.append(f"layer {st['layer']}/{st['total_layer']}")
    if isinstance(st.get("progress"), (int, float)) and st["progress"]:
        bits.append(f"{st['progress'] * 100:.0f}% done")
    return ", ".join(bits)


# --------------------------------------------------------------------- posts

def _mel():
    from . import moonraker_event_listener as mel
    return mel


def paused_text(filename: str, status: dict[str, Any] | None = None) -> str:
    """The family pause alert text: the shared 'paused' format_event shape.

    The Moonraker alert adds its where clause from print_stop.status();
    here the where comes from the family's normalised status the caller
    already read. No typed commands are named: /print-resume and
    /print-stop are Moonraker-shaped, so the text points at the buttons
    and the printer screen only.
    """
    where = where_text(status) if status else ""
    extra = (f"Paused at {where}. " if where else "") + \
        "Fix it at the printer, then tap Resume or Cancel below."
    return print_summary.format_event("paused", filename, extra=extra)


def _roster_printer_id(printer_id: str | None) -> str | None:
    """The real roster id: the default printer's own id when None (2.2.5 review).

    A None printer_id on a stored alert, or on a scoped retirement, would
    mean "any printer" to print_alert_buttons.retire_alerts; the default
    printer's alerts must carry and be matched by its actual id.
    """
    if printer_id:
        return printer_id
    try:
        from print_handy import settings
        return settings.default_printer_id()
    except Exception:
        return None


def post_pause_alert(filename: str, connection: str, printer_id: str | None = None,
                     status: dict[str, Any] | None = None) -> bool:
    """Post one pause alert with Resume/Cancel buttons, Moonraker-shaped.

    The alert record carries the connection (and roster printer id), so
    ``print_alert_buttons.handle_click`` routes a press to this family's
    control instead of the Moonraker path. On buttonless standalone
    platforms (Telegram/Matrix) the alert is plain text, exactly as the
    Moonraker listener's post_alert behaves there; a failed button post
    falls back to plain text the same way.
    """
    mel = _mel()
    printer_id = _roster_printer_id(printer_id)
    text = paused_text(filename, status)
    if mel._buttonless_standalone():
        return mel.post(text)
    try:
        from . import moonraker_discord_announcements as announcer
        from . import print_alert_buttons as pab
        if pab.post_with_buttons(mel._bot_token(), announcer.DISCORD_CHAT_ID,
                                 text, "paused", filename,
                                 connection=connection, printer_id=printer_id):
            log(f"post buttons kind=paused family={connection} {text.splitlines()[0][:120]!r}")
            return True
        log("family button post failed; falling back to plain text")
    except Exception as exc:
        log(f"family button post error: {exc!r}")
    return mel.post(text)


def retire_pause_alerts(filename: str | None, headline: str = "Print ended.",
                        printer_id: str | None = None) -> int:
    """Strip the buttons from this job's live pause alerts and say why (#370's rule).

    ``printer_id`` scopes the retirement to the owning printer's alerts
    (P2-176); None means the default printer (its real roster id), never
    "every printer" — one printer's print ending must not strip another's
    live buttons (2.2.5 review).
    """
    mel = _mel()
    if mel._buttonless_standalone():
        return 0
    printer_id = _roster_printer_id(printer_id)
    try:
        from . import print_alert_buttons as pab
        return pab.retire_alerts(
            mel._bot_token(), filename,
            "This paused prompt is closed; its buttons no longer act on a live job.",
            ("paused",), headline=headline, printer_id=printer_id)
    except Exception as exc:
        log(f"retire family paused prompt failed file={filename!r}: {exc!r}")
        return 0


def retire_headline(state: str) -> str:
    """The retire headline for a terminal state, mirroring the Moonraker listener's."""
    return _RETIRE_HEADLINES.get(str(state or ""), "Print ended.")


def post_family_event(kind: str, filename: str) -> bool:
    """A plain lifecycle post (start / terminal) in the shared format_event shape."""
    return _mel().post(print_summary.format_event(kind, filename))


class PauseAlerts:
    """Transition tracker a launched-print monitor feeds its polls into.

    The monitor owns its launched print's pause alerts: a printing ->
    paused transition posts exactly one alert (per pause episode), and
    the terminal transition retires the print's alerts so no stale
    Resume/Cancel buttons outlive the job. A resume at the printer
    leaves the alert live, exactly as the Moonraker listener leaves
    its: the buttons re-read state on a press and refuse truthfully.
    Repeated polls of the same state post nothing. Never raises.
    """

    def __init__(self, filename, connection, printer_id=None, *, poster=None, retirer=None):
        self.filename = filename or ""
        self.connection = connection
        self.printer_id = printer_id
        self._poster = poster or post_pause_alert
        if retirer is None:
            # The default retirer is scoped to this tracker's printer
            # (P2-176); an injected retirer keeps the two-argument
            # (filename, headline) contract.
            retirer = lambda fn, headline: retire_pause_alerts(fn, headline, printer_id)
        self._retirer = retirer
        self._prev: str | None = None

    def note(self, state, status=None):
        state = str(state or "unknown")
        prev, self._prev = self._prev, state
        if prev is None:
            return  # the first observation is a baseline, never a transition
        if state == "paused" and prev == "printing":
            try:
                self._poster(self.filename, self.connection, self.printer_id, status)
            except Exception as exc:
                log(f"pause alert post failed file={self.filename!r}: {exc!r}")
        elif prev in ACTIVE and state in TERMINAL_STATES:
            try:
                self._retirer(self.filename, retire_headline(state))
            except Exception as exc:
                log(f"pause alert retire failed file={self.filename!r}: {exc!r}")


def pause_alerts_for(adapter, filename, printer_id=None) -> PauseAlerts | None:
    """A PauseAlerts for a monitor's adapter, or None for non-family printers."""
    connection = connection_of(adapter)
    if connection not in FAMILY_CONNECTIONS:
        return None
    if printer_id is None:
        from .. import listeners
        printer_id = listeners.instance_printer_id()
    return PauseAlerts(filename, connection, printer_id)


# ------------------------------------------------------------- auto light (#516)

def _fact_value(adapter, name):
    """One sourced fact about the serving printer (its family's facts.py).

    Returns the fact's value, or None when the model's facts do not record
    it — an unrecorded fact is never claimed in the verdict context.
    """
    facts = getattr(adapter, "facts", None)
    if isinstance(facts, dict):
        fact = facts.get(name)
        if fact is not None:
            return getattr(fact, "value", fact)
    return None


def _idle_context(adapter) -> str:
    """The verdict context for a family printer's idle plate check.

    The Bambu watchdog's wording (bambu_watch.vision_context) with no
    plate-type claim: the vocabulary definitions are the verdict
    contract, and a frame judged against another printer's description
    would be a wrong prompt. The enclosure trait takes the same care
    (the Moonraker and Bambu contexts are built from their serving
    printer the same way): "enclosed" only when the serving model's
    sourced facts record an enclosure, "open frame" only when they
    record none, and no enclosure claim when the facts do not record
    it — this context used to say "enclosed" for every family printer.
    """
    label = getattr(adapter, "label", None) or "3D printer"
    traits = []
    enclosed = _fact_value(adapter, "enclosed")
    if enclosed is True:
        traits.append("enclosed")
    elif enclosed is False:
        traits.append("open frame")
    traits.append("build plate")
    return (f"Printer: {label}, {', '.join(traits)}. "
            "Reply with JSON only, no markdown: {\"verdict\":\"ok|air|spaghetti|detached|empty_idle|uncertain\",\"reason\":\"short\"}. "
            "ok means plastic is on the bed and printing normally. "
            "air means the bed is empty while a print should be there. "
            "spaghetti means a tangled failed extrusion. "
            "detached means a printed part has come off the plate. "
            "empty_idle means the printer is idle and the bed is empty. "
            "uncertain means the frame is not clear enough to judge.")


def _family_verdict(adapter):
    """A verdict(image, prompt) through the shared vision call (visual_watch)."""
    mel = _mel()
    context = _idle_context(adapter)

    def verdict(image, prompt):
        from . import visual_watch as vw
        return vw.visual_verdict(
            image, prompt, chat_url=mel._vision_chat_url, model=mel._VISION_MODEL,
            api_key=lambda: mel._profile_env_value("CLIPROXY_API_KEY"), context=context,
            idle=True)

    return verdict


class FamilyAutoLight:
    """The #516 auto light-off automation for one non-Moonraker printer.

    The Moonraker listener's light hooks are the reference; this driver
    is their family counterpart, with the same pure policy
    (``light_control.AutoLightPolicy``) and the same gate order —
    policy enabled, vision endpoint ready, light target available —
    before anything is switched. Every collaborator is injected:

    - ``send(on)`` switches the light through the family's own
      transport (Bambu MQTT ledctrl, SDCP Cmd 403).
    - ``available()`` probes the printer's own report for the light
      (Bambu ``lights_report``, SDCP ``LightStatus``) and answers
      True/False/None. An explicit False is cached and reported once:
      that printer has no chamber light and the feature is unavailable
      for it. None is unknown — a failed or light-less-shaped report
      is never treated as proof of absence and never blocks a send.
    - ``snapshot()`` returns one camera frame as bytes, or None when
      the family has no frame source to judge. SDCP is that case
      today (no camera endpoint is verified — see
      printers/elegoo_sdcp/camera.py): its idle checks find no frame,
      say so once, and the light stays on; the verdict leg is wired so
      a source verified later slots in unchanged.
    - ``verdict(image, prompt)`` judges a frame (visual_watch).
    - ``vision_ready()`` is the camera watchdog's endpoint gate.
    - ``runner(fn)`` runs the idle-check leg; the Bambu factory runs
      it on a daemon thread because its sink sits on the MQTT callback
      thread, while the polling family listener runs it inline.

    Nothing here raises, and nothing here touches anything but the
    light: a failed send logs and the print flow never notices.
    """

    def __init__(self, connection, *, send, snapshot=None, verdict=None,
                 available=None, vision_ready=None, policy=None,
                 clock=time.time, runner=None):
        from . import light_control
        self.connection = connection
        self._send = send
        self._snapshot = snapshot
        self._verdict = verdict
        self._available_probe = available
        self._vision_ready = vision_ready or (lambda: True)
        self._clock = clock
        self._runner = runner or (lambda fn: fn())
        if policy is None:
            from . import _config
            vision = _config.cfg().get("vision") or {}
            policy = light_control.AutoLightPolicy(vision.get("auto_light_off_minutes", 0))
        self._policy = policy
        self._absent = False
        self._absent_logged = False
        self._no_frame_logged = False
        self._check_inflight = False

    @property
    def enabled(self):
        return self._policy.enabled

    def _available(self):
        """True when the light may be switched; False only on a reported absence."""
        if self._absent:
            return False
        if self._available_probe is None:
            return True
        try:
            probed = self._available_probe()
        except Exception:
            return True  # a failed probe is unknown, never a verdict of absence
        if probed is False:
            self._absent = True
            if not self._absent_logged:
                self._absent_logged = True
                log(f"auto light: this {self.connection} printer reports no chamber "
                    "light; the feature is unavailable for it and nothing will be switched")
            return False
        return True

    def print_started(self):
        """Print-start transition: light on, from state only (never vision)."""
        try:
            if not self._policy.enabled:
                return
            if self._policy.print_started(self._clock()) != "turn_on":
                return
            if not self._vision_ready():
                return
            if not self._available():
                return
            self._send(True)
            log(f"auto light: print started; light on ({self.connection})")
        except Exception as exc:
            # Best-effort: a failed light command logs once per start and
            # never blocks or fails the print flow.
            log(f"auto light: light-on at print start failed ({self.connection}): {exc!r}")

    def print_finished(self):
        """Terminal print-end transition: open the idle window."""
        try:
            self._policy.print_finished(self._clock())
        except Exception as exc:
            log(f"auto light: opening the idle window failed ({self.connection}): {exc!r}")

    def idle_tick(self, state):
        """An idle observation: run one plate check when the policy says one is due."""
        try:
            policy = self._policy
            if not policy.enabled or state not in TERMINAL_STATES:
                return
            if not policy.should_check(self._clock()):
                return
            if self._check_inflight:
                return
            self._check_inflight = True

            def leg():
                try:
                    self._idle_check()
                finally:
                    self._check_inflight = False

            self._runner(leg)
        except Exception as exc:
            self._check_inflight = False
            log(f"auto light idle tick failed ({self.connection}): {exc!r}")

    def _idle_check(self):
        """One idle plate check. Never raises.

        Mirrors the Moonraker listener's: no vision endpoint or no
        light target notes the check and moves on; no frame (SDCP's
        unverified camera) notes it too — an unjudgeable plate can
        never turn the light off; a completely dark frame is judged
        "uncertain" (#476's rule), which resets the empty streak.
        Only a sustained empty_idle streak spanning the configured
        minutes switches the light off.
        """
        from . import light_control, local_empty, visual_watch
        policy = self._policy
        try:
            if not self._vision_ready():
                # Stage 1 (#455): with no vision route ready, the
                # calibrated plate reference is the one local producer
                # that can still certify the empty plate — judge the
                # frame with it alone (no endpoint, no failure detector).
                image = self._snapshot() if self._snapshot is not None else None
                if image is not None and not visual_watch.frame_dark(image):
                    verdict = local_empty.idle_verdict(image)
                    if verdict is not None:
                        action = policy.note_verdict(verdict[0], self._clock())
                        log(f"auto light idle check verdict={verdict[0]} "
                            f"detail={verdict[1]!r} ({self.connection})")
                        if action == "turn_off":
                            self._send(False)
                            log(f"auto light: plate seen empty for {policy.minutes} "
                                f"minutes; light off ({self.connection})")
                        return
                policy.note_check(self._clock())
                return
            if not self._available():
                policy.note_check(self._clock())
                return
            image = self._snapshot() if self._snapshot is not None else None
            if image is None:
                if not self._no_frame_logged:
                    self._no_frame_logged = True
                    log(f"auto light: no camera frame is available for {self.connection} "
                        "idle checks, so the plate cannot be judged and the light stays on")
                policy.note_check(self._clock())
                return
            if visual_watch.frame_dark(image):
                verdict, detail = "uncertain", "camera frame completely dark"
            else:
                verdict, detail = self._verdict(image, light_control.IDLE_CHECK_PROMPT)
            action = policy.note_verdict(verdict, self._clock())
            log(f"auto light idle check verdict={verdict} detail={detail!r} ({self.connection})")
            if action == "turn_off":
                self._send(False)
                log(f"auto light: plate seen empty for {policy.minutes} minutes; "
                    f"light off ({self.connection})")
        except Exception as exc:
            log(f"auto light idle check failed ({self.connection}): {exc!r}")
            with contextlib.suppress(Exception):
                # An unobservable plate is an uncertain observation: it
                # resets the empty streak (the Moonraker listener's
                # P2-201 rule), instead of letting a pre-outage streak
                # satisfy the sustained-empty requirement.
                policy.note_verdict("uncertain", self._clock())


def _thread_runner(fn):
    threading.Thread(target=fn, daemon=True, name="family-auto-light").start()


def auto_light_for(adapter, printer_id=None) -> FamilyAutoLight | None:
    """The auto-light driver for one family adapter, or None.

    Bambu Lab (its chamber light) and Elegoo SDCP (Cmd 403) are the
    families with a light to switch. The other parity families —
    PrusaLink, OctoPrint, stock FlashForge — and offline have no light
    target at all: None, their listeners run exactly as before, and
    bot check names the feature unavailable for them. Construction
    never raises: a driver that cannot be built is a logged None,
    never a broken listener.
    """
    connection = connection_of(adapter)
    try:
        if connection == "bambulab_lan":
            from . import bambu_watch, light_control
            return FamilyAutoLight(
                connection,
                send=lambda on: adapter.set_chamber_light(on),
                snapshot=lambda: adapter.camera_snapshot(),
                verdict=bambu_watch._default_verdict(adapter),
                available=lambda: light_control.bambu_light_report(
                    adapter.client().snapshot()),
                vision_ready=lambda: _mel()._vision_ready(),
                runner=_thread_runner)
        if connection == "elegoo_sdcp":
            from . import light_control
            from ..printers.elegoo_sdcp import camera as elegoo_camera
            client_box: dict = {}

            def client():
                if "client" not in client_box:
                    client_box["client"] = adapter.client()
                return client_box["client"]

            def probe():
                state = light_control.sdcp_light_state(client().status())
                return True if state is not None else None

            def snapshot():
                _url, fetch = elegoo_camera.camera_source(client())
                return fetch() if fetch is not None else None

            return FamilyAutoLight(
                connection,
                send=lambda on: adapter.set_light(on),
                snapshot=snapshot,
                verdict=_family_verdict(adapter),
                available=probe,
                vision_ready=lambda: _mel()._vision_ready())
    except Exception as exc:
        log(f"auto light: driver for connection {connection!r} could not be built: {exc!r}")
    return None


# ------------------------------------------------------------- Bambu sink

class BambuEventSink:
    """Posts the Bambu domain listener's transitions for external prints.

    The Bambu listener (#299 Stage B) detected started / paused /
    resumed / cancelled / completion / error all along, but its events
    went to a log. This sink is the consumer ``main()`` wires in: for a
    print started outside Print Handy (no start claim from the print
    path) it posts the start, the pause alerts and the terminal post —
    the Moonraker listener's contract. A claimed (Print Handy-launched)
    print is the launched-print monitor's: the sink stays silent for it
    and only retires its pause alerts when it ends.

    Baseline rule, the Moonraker listener's own: a print already active
    when this sink first observes the printer is never classified — no
    start post, no pause post, no terminal post for it. Only a start
    transition witnessed after an observed inactive state classifies a
    print, via the same start-claim file the Moonraker listener reads.
    """

    def __init__(self, *, claim_fn=None, start_poster=None, pause_poster=None,
                 terminal_poster=None, retirer=None, printer_id=None, light=None):
        self._claim_fn = claim_fn
        self._start_poster = start_poster or post_family_event
        self._pause_poster = pause_poster or post_pause_alert
        self._terminal_poster = terminal_poster or post_family_event
        if retirer is None:
            # Scoped to this sink's printer, like PauseAlerts (P2-176);
            # an injected retirer keeps the two-argument contract.
            retirer = lambda fn, headline: retire_pause_alerts(fn, headline, printer_id)
        self._retirer = retirer
        self._printer_id = printer_id
        self._light = light
        self._saw_inactive = False
        self._external: set[str] = set()

    def _claimed(self, filename) -> bool:
        if self._claim_fn is not None:
            return bool(self._claim_fn(filename))
        return bool(_mel().start_claimed(filename))

    def __call__(self, kind, event):
        status = (event or {}).get("status") or {}
        filename = status.get("filename") or ""
        state = str(status.get("state") or "unknown")
        transition = (event or {}).get("transition")
        try:
            if self._light is not None:
                # #516: the light automation follows every print, claimed
                # or external — the light is not a post, so the claim
                # split below does not apply to it. Starts and terminal
                # transitions feed the policy; idle status observations
                # drive its plate checks.
                if transition == "started" and filename:
                    self._light.print_started()
                elif kind == "status" and state in TERMINAL_STATES:
                    self._light.idle_tick(state)
            if kind == "status":
                if state in TERMINAL_STATES or state == "unknown":
                    self._saw_inactive = True
                if transition == "started" and filename:
                    if self._saw_inactive and not self._claimed(filename):
                        self._external.add(filename)
                        self._start_poster("started", filename)
                    else:
                        self._external.discard(filename)
                elif transition == "paused" and filename in self._external:
                    self._pause_poster(filename, "bambulab_lan", self._printer_id, status)
                elif transition == "cancelled" and filename:
                    self._terminal(filename, "cancelled")
            elif kind == "completion" and filename:
                self._terminal(filename, "complete")
            elif kind == "error" and filename:
                self._terminal(filename, "error")
        except Exception as exc:
            log(f"bambu event sink failed kind={kind} file={filename!r}: {exc!r}")

    def _terminal(self, filename, kind):
        external = filename in self._external
        self._external.discard(filename)
        self._saw_inactive = True
        if self._light is not None:
            self._light.print_finished()
        if external:
            self._terminal_poster(kind, filename)
        self._retirer(filename, retire_headline(
            "complete" if kind == "complete" else kind))


def bambu_event_sink(printer_id=None) -> BambuEventSink:
    """The sink the Bambu listener's main() wires as its emit target."""
    light = None
    try:
        light = auto_light_for(adapter_for(printer_id))
    except Exception as exc:
        log(f"auto light: Bambu driver could not be built: {exc!r}")
    return BambuEventSink(printer_id=printer_id, light=light)
