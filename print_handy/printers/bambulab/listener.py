"""Bambu Lab listener: the long-lived LAN MQTT session (#299 Stage B, #298 Stage 3).

The second listener domain, after Moonraker. Where the one-shot client
(``client.py``) connects, takes one complete snapshot and disconnects, this
listener holds one MQTT session open, keeps a persistent merged view of the
printer's ``print`` state, and reports through the normalised listener
contract (``print_handy.listeners``): normalised status dicts plus discrete
events (``status`` / ``completion`` / ``error`` / ``snapshot_available``).

Connection discipline — protocol facts from the #298 Stage 0 audit of
Bambuddy (AGPL: facts only, no code or text copied; see
docs/BAMBU-CONNECTIVITY.md):

- MQTT over TLS on port 8883 as ``bblp`` with the LAN access code, keepalive
  30 s, subscribed to ``device/<serial>/report``.
- On every (re)connect: request ``get_version``, then ``pushall`` (A1/P1
  report deltas, so state is incomplete until a full ``push_status``
  arrives). There is deliberately **no** periodic pushall cadence; the one
  extra request is at layer 1 when the layer total is still unknown.
- **Ack-echo guard:** only frames whose ``print.command`` is absent or
  ``push_status`` are telemetry. Command acknowledgements never update
  state.
- State is a persistent merged object (``client.merge_print``): partial
  frames update the fields they carry, nothing else.
- **Staleness + hard-reset reconnect:** no message for 60 s means the
  session is stale; the clock starts at connect, so a session that never
  receives a first report is recycled on the same deadline. A reconnect retires the MQTT client object entirely
  and builds a fresh one from the factory (clean session), so a publish
  queued on the dead client can never replay into a later session — the
  safety property behind Bambuddy's hard reset. Reconnects wait a 30 s
  cooldown.

The listener is **read-only**: the only payloads it ever publishes are its
own ``get_version`` and ``pushall`` requests. No print commands, no AMS
commands, no ``gcode_line`` — ever.

Events go to a sink: by default they are appended to ``listener.events``
(bounded) and logged. There is no event bus in the bot core yet; the
consumers named in #299 (Discord posts, watchdog, timelapse #246, camera
#294/#295) subscribe by kind when they are wired to this domain.

Unconfigured LAN: when ``[bambu] lan_host``/``lan_serial`` are not set, the
configured Bambu printer behaves exactly as it did before this listener
existed — slicing and handoff, no telemetry. The supervisor starts this
module for every ``bambulab_lan`` configuration (the registry gate cannot
see inside the vendor config), and the healthcheck requires this
instance's heartbeat, so the unconfigured listener instead stays alive,
beats its heartbeat with ``printer_connected=False``, and never touches
the network. That is the Moonraker listener's pattern: liveness is the
process, printer connectivity is reported, not required.

Nothing here has been run against a real Bambu printer; the transport is
injectable and every behaviour above is pinned by offline fake-MQTT tests.
"""
from __future__ import annotations

import json
import time

from ... import __version__, healthcheck, listeners
from .client import (GET_VERSION, MQTT_PORT, PUSHALL, BambuLanError,
                     merge_print)

# Heartbeat identity; must match the registry entry in print_handy.listeners.
RUNTIME = "bambu_event_listener"

KEEPALIVE_S = 30
STALE_AFTER_S = 60.0
RECONNECT_COOLDOWN_S = 30.0
LOOP_TICK_S = 0.5
_EVENTS_KEPT = 200


def log(line):
    print(f"bambu listener: {line}", flush=True)


class _StaleConnection(Exception):
    """The session went 60 s without a message; hard-reset and reconnect."""


_UNSET = object()  # lan_client sentinel: "build it from the adapter" vs an explicit None


def _watch_coverage_reporter():
    """The camera watch's coverage for a Bambu Lab printer, for the heartbeat (#1320).

    The in-print watch runs in the launched-print monitor, not here; the
    listener reports whether it can run (camera_watch.listener_coverage).
    Imported lazily, like the event sink: the bot core must not become an
    import-time dependency of the printer package. Never raises.
    """
    def compute():
        from ...runtime import _config, camera_watch, listener_common
        return camera_watch.listener_coverage(
            "bambulab_lan", _config.cfg(),
            lambda: listener_common._profile_env_value("CLIPROXY_API_KEY"))
    try:
        from ...runtime import camera_watch
        return camera_watch.CoverageReporter(compute)
    except Exception:
        class _Off:
            def current(self):
                return None
        return _Off()


class BambuListener:
    """One long-lived Bambu MQTT session for the configured printer instance.

    `adapter` is the configured model's BambuAdapter (status normalisation,
    LAN configuration). `lan_client` defaults to ``adapter.client()``; pass
    one (or an ``mqtt_factory``) in tests. `emit(kind, event)` is the event
    sink; `clock`/`sleep`/`beat` are injectable for offline tests.
    """

    def __init__(self, adapter, *, lan_client=_UNSET, mqtt_factory=None, emit=None,
                 clock=time.monotonic, sleep=time.sleep, beat=None, printer_id=None):
        self.adapter = adapter
        # The roster id of the printer this instance serves (#336); events
        # carry it so consumers route by printer without knowing the domain.
        self.printer_id = printer_id
        if lan_client is _UNSET:
            lan_client = adapter.client() if adapter.lan_configured() else None
        self._lan = lan_client
        self._factory = mqtt_factory
        if self._factory is None and self._lan is not None:
            self._factory = self._lan._mqtt_factory
        self._clock = clock
        self._sleep = sleep
        self._beat_fn = beat or (lambda **details: healthcheck.beat(runtime=RUNTIME, **details))
        # The camera watch's coverage for this printer (#1320), in the heartbeat.
        self._coverage = _watch_coverage_reporter()
        self.events = []
        self._emit_sink = emit
        self._stopped = False
        # Persistent merged telemetry state (never replaced wholesale).
        self._print = {}
        self._info = None
        self._complete = False
        # Session state.
        self._connected = False
        self._last_message_at = None
        self._last_beat_at = None
        self._snapshot_announced = False
        self._layer1_pushall_for = None
        # Event dedupe state.
        self._status = None
        self._status_key = None
        self._terminal_key = None
        self._error_key = None
        # Periodic-hook state: the staleness clock starts at connect (P2-116),
        # and the idle-observation / debug-poll hooks run on the heartbeat
        # cadence inside the session loop (P1-107, P2-187).
        self._session_started_at = None
        self._status_emitted_at = None
        self._last_debug_poll_at = None

    # ------------------------------------------------------------ events
    def _emit(self, kind, status, **details):
        event = {"kind": kind, "status": status, **details}
        if self.printer_id is not None:
            event["printer_id"] = self.printer_id
        self.events.append(event)
        if len(self.events) > _EVENTS_KEPT:
            del self.events[:len(self.events) - _EVENTS_KEPT]
        if self._emit_sink is not None:
            self._emit_sink(kind, event)
        else:
            log(f"event {kind}: state={status.get('state')!r} "
                f"file={status.get('filename')!r} {details or ''}".rstrip())

    def _process_status(self):
        """Normalise the merged state and emit contract events for changes."""
        status = self.adapter.normalize_status({"print": self._print})
        problems = listeners.validate_status(status)
        if problems:
            log(f"normalised status breaks the listener contract: {problems}; not emitting")
            return
        gcode = status.get("gcode_state") or ""
        prev = self._status
        prev_state = prev["state"] if prev else None
        state = status["state"]
        filename = status["filename"]
        transition = None
        if state == "printing" and prev_state == "paused":
            transition = "resumed"
        elif state == "printing" and prev_state not in ("printing", "paused"):
            transition = "started"
            self._terminal_key = None
            self._error_key = None
        elif state == "paused" and prev_state == "printing":
            transition = "paused"
        elif state == "standby" and prev_state in ("printing", "paused"):
            transition = "cancelled"  # a stop returns the printer to idle
        # Dedupe against the whole reported status (P2-117): a delta that
        # changes only progress, temperatures, targets or remaining time is
        # a real update the sink must see, not a repeat.
        key = json.dumps(status, sort_keys=True, default=str)
        if transition is not None or key != self._status_key:
            self._emit("status", status, transition=transition)
            self._status_key = key
            self._status_emitted_at = self._clock()
        if state == "complete" and prev_state in ("printing", "paused"):
            terminal = ("completion", filename)
            if self._terminal_key != terminal:
                self._terminal_key = terminal
                self._emit("completion", status, transition="finished")
        if state == "error":
            error_key = (filename, json.dumps(status.get("hms_errors") or [],
                                              sort_keys=True, default=str),
                         status.get("print_error_text"))
            if prev_state != "error" or (self._error_key is not None
                                         and self._error_key != error_key):
                self._error_key = error_key
                self._emit("error", status, transition="failed",
                           hms_errors=status.get("hms_errors") or [],
                           print_error_text=status.get("print_error_text"))
        elif prev_state == "error":
            self._error_key = None
        self._status = status
        if self._complete and not self._snapshot_announced:
            self._snapshot_announced = True
            try:
                kind = self.adapter.camera_kind()
            except Exception:
                kind = None
            if kind is not None:
                self._emit("snapshot_available", status, camera_kind=kind)

    # ------------------------------------------------------------ frames
    def _on_message(self, payload):
        """One report-topic payload. The ack-echo guard lives here."""
        self._last_message_at = self._clock()
        if not isinstance(payload, dict):
            return
        info = payload.get("info")
        if isinstance(info, dict) and info.get("command") == "get_version":
            self._info = info
        update = payload.get("print")
        if not isinstance(update, dict):
            return
        command = update.get("command")
        if command is not None and command != "push_status":
            return  # a command acknowledgement is not telemetry
        self._print = merge_print(self._print, update)
        if command == "push_status" and update.get("msg") == 0:
            self._complete = True
        self._process_status()

    # ------------------------------------------------------------ session
    def _session(self):
        """One connection, from connect to failure/staleness/stop.

        Builds a brand-new MQTT client from the factory (hard reset): the
        retired client is disconnected and dropped, queue and all.
        """
        lan = self._lan
        client = self._factory(lan.host, lan.serial, lan._access_code)
        box = {"denied": False, "failed": None}

        def on_connect(mqtt_client, _userdata, _flags, reason_code, *_rest):
            rc = getattr(reason_code, "value", reason_code)
            if rc == 5:
                box["denied"] = True
                return
            if rc != 0:
                box["failed"] = f"MQTT connection refused (rc={rc})"
                return
            self._connected = True
            mqtt_client.subscribe(lan.report_topic)
            try:
                lan._publish(mqtt_client, GET_VERSION)
                lan._publish(mqtt_client, PUSHALL)
            except BambuLanError as exc:
                box["failed"] = str(exc)

        def on_message(_client, _userdata, message):
            try:
                payload = json.loads(message.payload)
            except (ValueError, TypeError, AttributeError):
                return
            self._on_message(payload)

        client.on_connect = on_connect
        client.on_message = on_message
        self._print = {}
        self._complete = False
        self._snapshot_announced = False
        self._layer1_pushall_for = None
        self._last_message_at = None
        self._session_started_at = self._clock()
        self._status_emitted_at = None
        try:
            try:
                client.connect(lan.host, MQTT_PORT, keepalive=KEEPALIVE_S)
            except Exception as exc:
                raise BambuLanError(f"Bambu Lab printer at {lan.host} could not be reached "
                                    f"({type(exc).__name__})") from exc
            while not self._stopped:
                if box["denied"]:
                    raise BambuLanError("the printer rejected the LAN access code "
                                        "(MQTT access denied)")
                if box["failed"]:
                    raise BambuLanError(f"Bambu Lab LAN connection failed: {box['failed']}")
                client.loop(timeout=LOOP_TICK_S)
                now = self._clock()
                # The staleness clock starts at connect (P2-116): a session
                # that never receives a first report goes stale on the same
                # 60 s deadline instead of being held forever.
                last_heard = (self._last_message_at if self._last_message_at is not None
                              else self._session_started_at)
                if last_heard is not None and now - last_heard > STALE_AFTER_S:
                    raise _StaleConnection(
                        f"no message from the printer for {now - last_heard:.0f}s")
                self._maybe_layer1_pushall(client)
                self._maybe_beat()
                self._maybe_poll_debug()
                self._maybe_idle_observation()
        finally:
            self._connected = False
            try:
                client.disconnect()
            except Exception:
                pass

    # Terminal states for the idle-observation hook; mirrors the set the
    # family event sink feeds the auto-light policy (family_control).
    _TERMINAL_STATES = ("complete", "cancelled", "error", "standby")

    def _maybe_idle_observation(self):
        """Forward an idle observation on the heartbeat cadence (P1-107).

        Status events are deduped, so a printer sitting unchanged in a
        terminal state emits nothing further — and the sink's auto-light
        idle checks, which run off status observations, starve with the
        window open. While the state stays terminal, re-forward the
        current status periodically, independently of dedupe; the sink's
        policy gates how often a plate check really runs.
        """
        if self._status is None or self._status["state"] not in self._TERMINAL_STATES:
            return
        now = self._clock()
        if (self._status_emitted_at is not None
                and now - self._status_emitted_at < healthcheck.HEARTBEAT_S):
            return
        self._status_emitted_at = now
        self._emit("status", self._status, transition=None)

    def _maybe_poll_debug(self):
        """Poll the shared debug-mode state on the heartbeat cadence (P2-187).

        run_forever polls between sessions, but a healthy session can run
        for hours: debug start, stop and expiry must be observed while it
        is live, not only after a reconnect.
        """
        now = self._clock()
        if (self._last_debug_poll_at is not None
                and now - self._last_debug_poll_at < healthcheck.HEARTBEAT_S):
            return
        self._last_debug_poll_at = now
        self._poll_debug_mode()

    def _maybe_layer1_pushall(self, client):
        """pushall once at layer 1 when the layer total is still unknown (audit fact)."""
        if not self._complete or self._status is None:
            return
        status = self._status
        if status.get("layer") != 1 or status.get("total_layer"):
            return
        job = status.get("filename") or ""
        if self._layer1_pushall_for == job:
            return
        self._layer1_pushall_for = job
        self._lan._publish(client, PUSHALL)

    # ------------------------------------------------------------ liveness
    def _beat_now(self):
        self._last_beat_at = self._clock()
        fresh = bool(self._connected and self._last_message_at is not None
                     and self._clock() - self._last_message_at <= STALE_AFTER_S)
        state = self._status["state"] if self._status else "unknown"
        details = {}
        coverage = self._coverage.current()
        if coverage is not None:
            details["camera_watch"] = coverage  # #1320: active, or off and why
        try:
            self._beat_fn(printer_connected=fresh, state=state, **details)
        except Exception as exc:
            log(f"heartbeat write failed: {exc!r}")

    def _maybe_beat(self):
        if self._last_beat_at is None or self._clock() - self._last_beat_at >= healthcheck.HEARTBEAT_S:
            self._beat_now()

    def _wait(self, seconds):
        """Sleep in ticks until `seconds` pass or stop() is called, beating on schedule."""
        deadline = self._clock() + seconds
        while not self._stopped and self._clock() < deadline:
            self._maybe_beat()
            self._sleep(min(LOOP_TICK_S, max(0.0, deadline - self._clock())))

    # ------------------------------------------------------------ run
    def stop(self):
        self._stopped = True

    @property
    def active(self):
        """True when a LAN session can run: configured, with a usable client factory."""
        return self._lan is not None and self.adapter.lan_configured()

    def _poll_debug_mode(self):
        """#478: observe the shared debug-mode state (lazy import: this
        module sits under the printers package settings loads first)."""
        try:
            from ... import debug_mode
            debug_mode.poll()
        except Exception:
            pass

    def run_forever(self):
        """Serve until stop() — reconnecting forever, never exiting on printer errors."""
        if not self.active:
            log("LAN telemetry is not configured ([bambu] lan_host/lan_serial); "
                "listener idling with heartbeat, no network contact")
            while not self._stopped:
                self._poll_debug_mode()
                self._wait(LOOP_TICK_S)
            return 0
        log(f"listener starting (Print Handy {__version__}) for the Bambu Lab printer at {self._lan.host} "
            f"(serial {self._lan.serial})")
        while not self._stopped:
            try:
                self._session()
            except _StaleConnection as exc:
                log(f"session stale ({exc}); hard-reset reconnect after cooldown")
            except BambuLanError as exc:
                log(f"session failed: {exc}; reconnect after cooldown")
            except Exception as exc:  # transport surprises must not kill the listener
                log(f"session failed unexpectedly: {exc!r}; reconnect after cooldown")
            if self._stopped:
                break
            self._poll_debug_mode()
            self._wait(RECONNECT_COOLDOWN_S)
        return 0


def main():
    from ... import logs, printers

    logs.configure(role="listener")
    adapter = printers.current()
    # Adapters have no ``vendor`` attribute: the connection is the
    # adapter's ``connection`` property (the vendor profile's connection,
    # or None when the model carries no vendor), with the adapter's
    # ``transport`` as the fallback — a Bambu adapter reports
    # ``transport = "bambulab_lan"`` even when its vendor is None (#409).
    # The rest of the codebase asks through these two, never
    # ``adapter.vendor.connection``.
    connection = getattr(adapter, "connection", None) or getattr(adapter, "transport", None)
    if connection != "bambulab_lan":
        log(f"configured printer uses connection {connection!r}, "
            "not bambulab_lan; nothing to listen to")
        return 3
    printer_id = listeners.instance_printer_id()
    # The #460 sink: the listener detected the transitions all along;
    # this wires them to the shared family posts for prints started
    # outside Print Handy (claimed prints stay the launched-print
    # monitor's). The same sink settles an uncertain launch lease from
    # this telemetry (#1241, family_control.LaunchLeaseSettler), as the
    # polled families' listener does. Imported here, not at module scope: the sink lives in
    # the bot core (print_handy.runtime), which must not become an
    # import-time dependency of the printer package.
    from ...runtime import family_control
    emit = family_control.bambu_event_sink(printer_id)
    try:
        listener = BambuListener(adapter, emit=emit, printer_id=printer_id)
    except BambuLanError as exc:
        # e.g. host/serial set but the access-code secret is missing: the
        # client cannot even be built. Idle like the unconfigured case
        # rather than crash-looping the container; the log names the fix.
        log(f"LAN client cannot be built: {exc}; listener idling with heartbeat")
        listener = BambuListener(adapter, lan_client=None, emit=emit, printer_id=printer_id)
    return listener.run_forever()


if __name__ == "__main__":
    raise SystemExit(main())
