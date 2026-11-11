"""The polling listener domains for the non-Moonraker families (#460).

One shared listener for prusalink, elegoo_sdcp, octoprint and
flashforge_stock — the four families whose domain was PLANNED in the
#299 registry. None of them has a push channel the Moonraker listener
could reuse (PrusaLink and OctoPrint are HTTP request/response, the
stock Flashforge LAN API is HTTP, SDCP is a WebSocket the launched-print
monitor already polls one-shot), so the domain polls the family's
normalised status — the same read its launched-print monitor uses
(``family_control.status_fn_for``) — and runs the Moonraker listener's
state machine over it:

- A print started outside Print Handy (no start claim from the print
  path — the same claim file the Moonraker listener reads) gets exactly
  one start post, one pause alert per pause episode (through
  ``family_control``, with the family's own Resume/Cancel), and one
  terminal post, and its pause alerts are retired when it ends.
- A claimed print is the print path's and its launched-print monitor's:
  this listener posts nothing for it, and only retires its pause alerts
  when it ends (retiring is idempotent; it covers a monitor that died
  before the print did).
- Baseline rule, the Moonraker listener's own: a print already active
  at the listener's first successful read is never classified — no
  start post, no pause post, no terminal post for it. A witnessed
  inactive -> active transition is what classifies a print. The
  classification is persisted across a listener restart, so a restart
  mid-print restores it without re-posting the start.

Bambu is not served here: its listener is event-driven (MQTT) and
already implemented; #460 wires its sink to the same family_control
posts (``family_control.BambuEventSink``).

Uncertain launch leases (#1147). A start whose response was lost leaves
the printer's launch lease uncertain, and every later start refuses until
it settles. On Moonraker the listener settles it from what the printer
reports; this domain does the same for its families (OctoPrint, PrusaLink,
SDCP, stock Flashforge), from the status it already polls: the printer
printing or paused on the lease's own file settles it as started
(``launch_state.settle_observed``), and the printer idle with both heater
targets at 0 for the Moonraker listener's ABANDONED_START_COLD_S, never
showing that file running, releases it as not started
(``launch_state.release_abandoned``, which leaves a live print-now's
``submitting`` row alone). Nothing is sent to the printer for either.
The rule is ``family_control.LaunchLeaseSettler``, which the Bambu
listener's sink feeds from its LAN telemetry too (#1241).

The listener never writes to the printer. The only printer writes in
this feature are the Resume/Cancel an operator explicitly presses,
routed by the gateway to ``family_control.FamilyControl`` — plus, for
the one family here with a light (Elegoo SDCP), the opt-in #516 auto
light-off: the listener feeds print starts, print ends and idle polls
to ``family_control.FamilyAutoLight``, which switches the chamber
light and nothing else, only when the operator turned the feature on.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .. import __version__, debug_mode, healthcheck, listeners, logs
from . import _config, family_control

logger = logging.getLogger(__name__)

POLL_S = 15.0

# The registry runtime names for these domains (print_handy/listeners.py
# carries the same strings; the healthcheck resolves a domain's
# expected runtime from the registry, so the two must agree).
RUNTIMES = {
    "prusalink": "prusalink_event_listener",
    "elegoo_sdcp": "elegoo_sdcp_event_listener",
    "octoprint": "octoprint_event_listener",
    "flashforge_stock": "flashforge_stock_event_listener",
}

_ACTIVE = family_control.ACTIVE
_TERMINAL = family_control.TERMINAL_STATES


def log(line):
    logger.info("%s", logs.redact(str(line)))


def runtime_for(connection: str) -> str | None:
    return RUNTIMES.get(connection)


class FamilyListener:
    """Poll one family's printer and post the external-print lifecycle.

    Every collaborator is injectable for tests: the status read, the
    start-claim check, the three posters, the retirer, the heartbeat.
    """

    def __init__(self, connection, status_fn, *, runtime=None, printer_id=None,
                 claim_fn=None, start_poster=None, pause_poster=None,
                 terminal_poster=None, retirer=None, beat=None,
                 clock=time.monotonic, sleep=time.sleep, poll_s=POLL_S,
                 state_path=None, light=None):
        self.connection = connection
        self._status_fn = status_fn
        self._runtime = runtime or RUNTIMES[connection]
        self._printer_id = printer_id
        self._claim_fn = claim_fn
        self._start_poster = start_poster or family_control.post_family_event
        self._pause_poster = pause_poster or family_control.post_pause_alert
        self._terminal_poster = terminal_poster or family_control.post_family_event
        if retirer is None:
            # The default retirer is scoped to this listener's printer
            # (P2-176); an injected retirer keeps the two-argument
            # (filename, headline) contract.
            retirer = lambda fn, headline: family_control.retire_pause_alerts(
                fn, headline, printer_id)
        self._retirer = retirer
        self._light = light
        self._beat_fn = beat or (lambda **details: healthcheck.beat(runtime=self._runtime, **details))
        self._clock = clock
        self._sleep = sleep
        self._poll_s = poll_s
        self._state_path = Path(state_path) if state_path else _config.state_file(
            "PRINT_HANDY_FAMILY_LISTENER_STATE", "family_listener.json", per_instance=True)
        self._stopped = False
        self._failures = 0
        self._last_beat_at = None
        self._prev_state: str | None = None
        self._lease_settler = None  # family_control.LaunchLeaseSettler, built on first use
        self._current: dict | None = None
        self._restored: dict | None = self._load_record()

    # -- persisted classification -------------------------------------------

    def _load_record(self):
        try:
            record = json.loads(self._state_path.read_text())
        except Exception:
            return None
        if isinstance(record, dict) and record.get("filename"):
            return {"filename": str(record["filename"]),
                    "external": bool(record.get("external"))}
        return None

    def _save_record(self):
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            if self._current is None:
                self._state_path.unlink(missing_ok=True)
            else:
                self._state_path.write_text(json.dumps(
                    {"filename": self._current["filename"],
                     "external": self._current["external"]}, sort_keys=True))
        except Exception as exc:
            log(f"family listener state write failed: {exc!r}")

    # -- classification -------------------------------------------------------

    def _claimed(self, filename) -> bool:
        if self._claim_fn is not None:
            return bool(self._claim_fn(filename))
        from . import moonraker_event_listener as mel
        return bool(mel.start_claimed(filename))

    def _begin(self, filename, *, witnessed: bool):
        """Classify a newly observed active print. Returns the record."""
        if not witnessed:
            return {"filename": filename or "", "external": False, "baselined": True}
        external = not (filename and self._claimed(filename))
        record = {"filename": filename or "", "external": external, "baselined": False}
        self._current = record
        self._save_record()
        if external and filename:
            try:
                self._start_poster("started", filename)
            except Exception as exc:
                log(f"family start post failed file={filename!r}: {exc!r}")
        return record

    def _end(self, state, filename, progress=None):
        """Terminal handling for the tracked print: post (external only) + retire.

        A return to standby is a stop at the printer, unless the job reached
        100%: OctoPrint has no "complete" state, so a finished job reads as
        standby with full progress (the rule octo_watch already applies to
        launched prints).
        """
        current, self._current = self._current, None
        self._save_record()
        if current is None:
            return
        name = current["filename"] or filename or ""
        if state == "standby" and isinstance(progress, (int, float)) and progress >= 1.0:
            state = "complete"
        kind = "cancelled" if state == "standby" else state
        if current["external"] and not current["baselined"] and name:
            try:
                self._terminal_poster(kind, name)
            except Exception as exc:
                log(f"family terminal post failed file={name!r}: {exc!r}")
        try:
            self._retirer(name, family_control.retire_headline(state))
        except Exception as exc:
            log(f"family pause retire failed file={name!r}: {exc!r}")
        # #516: a tracked print ended — open the auto light-off idle
        # window (external and claimed prints alike; the light follows
        # the printer, not the announcement split).
        if self._light is not None:
            try:
                self._light.print_finished()
            except Exception as exc:
                log(f"auto light print-finished hook failed: {exc!r}")

    # -- one poll ---------------------------------------------------------------

    def poll_once(self):
        """One status read and the transitions it implies. Never raises."""
        try:
            status = self._status_fn()
        except Exception as exc:
            self._failures += 1
            if self._failures == 1 or self._failures % 20 == 0:
                log(f"family listener ({self.connection}) status read failed "
                    f"({self._failures} in a row): {exc!r}")
            self._beat(False, "unknown")
            return
        self._failures = 0
        state = str((status or {}).get("state") or "unknown")
        filename = (status or {}).get("filename") or ""
        try:
            self._process(state, filename, status or {})
        except Exception as exc:
            log(f"family listener ({self.connection}) transition handling failed: {exc!r}")
        try:
            self._settle_lease(state, filename, status or {})
        except Exception as exc:
            log(f"family listener ({self.connection}) launch lease settle failed: {exc!r}")
        self._beat(True, state)

    # -- uncertain launch leases (#1147) ---------------------------------------

    def _settle_lease(self, state, filename, status, now=None):
        """Settle this printer's submitting/uncertain lease from one status read, as Moonraker does."""
        if self._lease_settler is None:
            self._lease_settler = family_control.LaunchLeaseSettler(self._printer_id)
        return self._lease_settler.observe(state, filename, status, now=now)

    def _process(self, state, filename, status):
        prev, self._prev_state = self._prev_state, state
        if state in _ACTIVE:
            superseded = False
            if self._current is not None and filename and self._current["filename"] \
                    and filename != self._current["filename"]:
                # The tracked print was replaced without an observed
                # inactive gap: close it out, then classify the new one
                # as a witnessed start.
                self._end("standby", filename)
                superseded = True
            if self._current is None:
                if self._restored is not None and filename \
                        and filename == self._restored["filename"]:
                    self._current = {"filename": filename,
                                     "external": self._restored["external"],
                                     "baselined": True}
                    self._restored = None
                elif superseded or (prev is not None and prev not in _ACTIVE):
                    self._current = self._begin(filename, witnessed=True)
                    # #516: a witnessed fresh start turns the light on
                    # (state event only — a baselined print, already
                    # running when first seen, does not).
                    if self._light is not None:
                        try:
                            self._light.print_started()
                        except Exception as exc:
                            log(f"auto light print-started hook failed: {exc!r}")
                else:
                    self._current = self._begin(filename, witnessed=False)
            if prev == "printing" and state == "paused" \
                    and self._current["external"] and not self._current["baselined"]:
                try:
                    self._pause_poster(self._current["filename"] or filename,
                                       self.connection, self._printer_id, status)
                except Exception as exc:
                    log(f"family pause post failed file={filename!r}: {exc!r}")
            if prev == "paused" and state == "printing" and self._current is not None:
                # A resume ends the pause: its starter-ping episodes
                # stop suppressing, so a later pause is a new problem
                # that can ping (#1095).
                try:
                    from . import starter_pings
                    starter_pings.clear_pause_episodes(
                        job_key=self._current["filename"] or filename)
                except Exception as exc:
                    log(f"family pause episode clear failed file={filename!r}: {exc!r}")
        elif state in _TERMINAL:
            if self._current is not None:
                # prev is active for the direct transition; an unknown
                # gap in between lands here too — the tracked print was
                # active when classified, so it still ends properly.
                self._end(state, filename, status.get("progress"))
            self._restored = None
            # #516: idle observations drive the light policy's plate
            # checks (its own interval gates how often one really runs).
            if self._light is not None:
                try:
                    self._light.idle_tick(state)
                except Exception as exc:
                    log(f"auto light idle tick hook failed: {exc!r}")
        else:
            # An unreadable/unknown state is not an ending: a tracked
            # print stays tracked (its next active read resumes the
            # transitions, its terminal read ends it). Nothing posts.
            pass

    # -- liveness / run ---------------------------------------------------------

    def _beat(self, connected: bool, state: str):
        now = self._clock()
        if self._last_beat_at is not None and now - self._last_beat_at < healthcheck.HEARTBEAT_S:
            return
        self._last_beat_at = now
        try:
            details = {"printer_connected": bool(connected), "state": state}
            if self._printer_id:
                details["printer_id"] = self._printer_id
            self._beat_fn(**details)
        except Exception as exc:
            log(f"family listener heartbeat write failed: {exc!r}")

    def stop(self):
        self._stopped = True

    def run_forever(self):
        log(f"family listener starting (Print Handy {__version__}) for connection {self.connection} "
            f"(runtime {self._runtime})")
        while not self._stopped:
            self.poll_once()
            try:
                debug_mode.poll()  # #478: observe the shared debug-mode state
            except Exception:
                pass
            if self._stopped:
                break
            self._sleep(self._poll_s)
        return 0


def main():
    from .. import logs as _logs, printers

    _logs.configure()
    adapter = printers.current()
    connection = family_control.connection_of(adapter)
    if connection not in RUNTIMES:
        log(f"configured printer uses connection {connection!r}, which has no "
            "polling listener domain; nothing to listen to")
        return 3
    listener = FamilyListener(
        connection, family_control.status_fn_for(adapter),
        runtime=RUNTIMES[connection],
        printer_id=listeners.instance_printer_id(),
        # #516: the auto-light driver for the families that have one
        # (Elegoo SDCP here; None for PrusaLink / OctoPrint / stock
        # FlashForge, which have no light target).
        light=family_control.auto_light_for(adapter))
    return listener.run_forever()


if __name__ == "__main__":
    raise SystemExit(main())
