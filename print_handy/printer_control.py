"""The one printer-control service, keyed by printer id (#1317, proposal 1 of #1160).

Every control entry point asks this module, never a transport directly:
the chat commands (``/print-stop``, ``/print-resume``, ``/print-reboot``,
the exclude and spool-question stops), the listener alert buttons on every
platform (Discord, Telegram and Matrix all land in
``print_alert_buttons.handle_click``), the Web UI control routes
(``webui/controls.py``) and the CLI (``python -m print_handy.runtime.panic``).
``tests/test_printer_control_entry_points.py`` enumerates those entry points
and fails when one reaches a transport without passing through here.

Interface
---------
``for_printer(printer_id)`` returns a ``PrinterControl`` for one roster
printer (None is the configured default printer, exactly as every caller
treated None before). It owns:

- capability discovery: ``connection`` and ``capabilities()``, the verbs
  this printer's transport can carry (``Capabilities.supports``);
- the heater ceiling: ``ceiling`` / ``refusal`` ask
  ``core.heater_ceiling`` with this printer's adapter, settings view and
  hardware record, so a printer has one answer whichever surface asks;
- cancel-then-verify-heaters-off: ``cancel`` re-reads state, sends at most
  one cancel for the confirmed file, reads the state back and then reads the
  heater targets back, naming any heater still set (#1147);
- pause and resume with the same discipline (``pause``, ``send_pause`` for a
  caller that runs its own readback, ``resume``);
- emergency stop and recovery: ``emergency_stop`` (the panic ladder:
  emergency stop, firmware restart, ready wait, REBOOT fallback),
  ``clear_shutdown`` and ``force_reboot``;
- the Web UI's one-command shape: ``send(command, sender=...)``, the single
  door every Web UI control command crosses.

Routing is decided once, here: a recorded family alert goes to that family's
own adapter control (``family_control``), a Bambu LAN printer to its LAN
client, and Moonraker to Moonraker. The transport drivers are unchanged
(``hermes/print_stop.py``, ``hermes/print_control.py``,
``hermes/family_control.py``, ``hermes/panic.py`` and the Web UI's
``default_send``); this service is the only caller allowed to reach them, so
a gate added to a driver applies to every surface at once. Drivers are looked
up at call time (``core._external.load``), so a test that replaces a driver
function sees every surface use the replacement.

Out of scope: starting a print (stream B, #1318). Nothing here loosens a
gate or adds a retry; the Web UI keeps its own immediate HTTP contract and
chat keeps its verified, worded one, because merging those contracts would
change behavior rather than consolidate it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .core import _external
from .core import heater_ceiling as _heater_ceiling

# Every verb the service can route. ``capabilities()`` returns the subset a
# printer's transport carries.
VERBS = ("status", "pause", "resume", "cancel", "emergency_stop", "firmware_restart",
         "server_restart", "reboot", "temperature", "gcode", "exclude")
MOONRAKER_VERBS = frozenset(VERBS)
# The adapter-control families (family_control.FAMILY_CONNECTIONS) carry
# status, pause, resume and cancel through their adapter's control().
_FAMILY_CONTROL_VERBS = frozenset({"status", "pause", "resume", "cancel"})

FOREIGN_ALERT_TEXT = "That alert belongs to a printer I can't control from here. Nothing sent."

_DEFAULT = object()  # "this printer's own hardware record" for the heater ceiling


def _driver(name: str):
    """A transport driver module, looked up at call time (print_handy.runtime.<name>)."""
    return _external.load(name)


def _kw(printer_id: str | None) -> dict[str, Any]:
    """The driver keyword for one roster printer; none for the default printer (the drivers' own default)."""
    return {"printer_id": printer_id} if printer_id else {}


@dataclass(frozen=True)
class Capabilities:
    """What one printer's transport can carry. ``verbs`` is a subset of ``VERBS``."""

    connection: str | None
    verbs: frozenset

    def supports(self, verb: str) -> bool:
        return verb in self.verbs


class PrinterControl:
    """Control of one printer, keyed by its roster id (None: the configured default printer)."""

    def __init__(self, printer_id: str | None = None, *, adapter=None, view=None):
        self.printer_id = str(printer_id) if printer_id else None
        self._adapter = adapter
        self._view = view
        self._family: dict[str, Any] = {}

    def __repr__(self) -> str:
        return f"PrinterControl({self.printer_id!r})"

    # ------------------------------------------------------------ discovery

    @property
    def adapter(self):
        """This printer's adapter, resolved once (family_control.adapter_for)."""
        if self._adapter is None:
            self._adapter = _driver("family_control").adapter_for(self.printer_id)
        return self._adapter

    @property
    def view(self) -> dict[str, Any]:
        """This printer's settings view (the adapter's own config, else the roster view)."""
        if self._view is None:
            config = getattr(self.adapter, "config", None)
            if isinstance(config, dict):
                self._view = config
            else:
                from . import settings
                self._view = settings.view_for(self.printer_id) if self.printer_id else settings.current()
        return self._view

    @property
    def connection(self) -> str | None:
        return _driver("family_control").connection_of(self.adapter)

    def capabilities(self) -> Capabilities:
        """The verbs this printer's transport carries, from its connection and adapter surface."""
        adapter = self.adapter
        connection = self.connection
        if connection == "moonraker":
            return Capabilities(connection, MOONRAKER_VERBS)
        verbs: set[str] = set()
        if connection in _driver("family_control").FAMILY_CONNECTIONS and callable(getattr(adapter, "control", None)):
            verbs |= _FAMILY_CONTROL_VERBS
        if connection not in (None, "offline") and callable(getattr(adapter, "set_target_temperature", None)):
            verbs.add("temperature")
        if connection == "octoprint" and callable(getattr(adapter, "send_gcode", None)):
            verbs.add("gcode")
        return Capabilities(connection, frozenset(verbs))

    def is_bambu_lan(self) -> bool:
        """Whether stop and pause go to this printer's Bambu LAN client (print_stop's rule)."""
        return _driver("print_stop")._lan_adapter(**_kw(self.printer_id)) is not None

    def _family_control(self, connection: str):
        """The family adapter control for a recorded alert's connection, or None when it no longer matches."""
        if "control" not in self._family:
            self._family["control"] = _driver("family_control").control_for(self.printer_id)
        control = self._family["control"]
        if control is None or control.connection != connection:
            return None
        return control

    def accepts_alert(self, connection: str) -> bool:
        """Whether a family alert recorded with ``connection`` still belongs to this printer's control."""
        return self._family_control(connection) is not None

    # -------------------------------------------------------- heater ceiling

    def hardware_record(self):
        """This printer's hardware record (``/nozzle change``, ``printer_state set``), or None."""
        try:
            from .core import printer_state
            record = printer_state.load(self.printer_id) if self.printer_id else printer_state.load()
        except Exception:  # no readable hardware record: configuration is all there is
            return None
        return record if isinstance(record, dict) else None

    def ceiling(self, heater: str, *, state=_DEFAULT) -> float:
        """The highest target (C) this printer accepts for ``heater`` (core.heater_ceiling)."""
        state = self.hardware_record() if state is _DEFAULT else state
        return _heater_ceiling.ceiling(self.adapter, heater, state=state, view=self.view)

    def refusal(self, heater: str, target, *, state=_DEFAULT) -> str | None:
        """Why ``target`` is above this printer's ``heater`` ceiling, or None when it is allowed."""
        state = self.hardware_record() if state is _DEFAULT else state
        return _heater_ceiling.refusal(self.adapter, heater, target, state=state, view=self.view)

    # --------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        """The job status every family reads the same way (print_stop.status)."""
        return _driver("print_stop").status(**_kw(self.printer_id))

    def controller_status(self) -> dict[str, Any]:
        """The Moonraker status with the controller state (print_control.status: klippy)."""
        return _driver("print_control").status(**_kw(self.printer_id))

    # ----------------------------------------------------------- operations

    def cancel(self, expected_filename: str = "", *, alert_connection: str | None = None,
               **wait) -> tuple[bool, str]:
        """One cancel for the confirmed file, then the state and the heater targets read back.

        ``alert_connection`` is the connection a family alert was recorded
        with: the press goes to that family's own adapter control, and a
        printer whose control no longer matches is refused without sending.
        Otherwise print_stop.cancel routes Moonraker, Bambu LAN and the other
        families. Returns (sent, truthful text).
        """
        if alert_connection:
            control = self._family_control(alert_connection)
            if control is None:
                return False, FOREIGN_ALERT_TEXT
            return control.cancel(expected_filename, **wait)
        return _driver("print_stop").cancel(expected_filename, **_kw(self.printer_id), **wait)

    def pause(self, expected_filename: str = "", **wait) -> tuple[bool, str]:
        """One verified Bambu LAN pause for the confirmed file (print_stop.pause)."""
        return _driver("print_stop").pause(expected_filename, **_kw(self.printer_id), **wait)

    def send_pause(self) -> None:
        """One Moonraker pause, for a caller that confirms it by its own readback. Raises on a failed send."""
        _driver("print_control")._send("/printer/print/pause", method="POST", timeout=15,
                                       **_kw(self.printer_id))

    def resume(self, expected_filename: str = "", *, alert_connection: str | None = None,
               **wait) -> tuple[bool, str]:
        """One resume for the confirmed paused file, then read back. (resumed, truthful text)."""
        if alert_connection:
            control = self._family_control(alert_connection)
            if control is None:
                return False, FOREIGN_ALERT_TEXT
            return control.resume(expected_filename, **wait)
        return _driver("print_control").resume(expected_filename, **_kw(self.printer_id), **wait)

    def emergency_stop(self, **ladder) -> dict[str, Any]:
        """The panic ladder (panic.force_restart): emergency stop, firmware restart, ready wait, REBOOT fallback."""
        return _driver("panic").force_restart(printer_id=self.printer_id, **ladder)

    def clear_shutdown(self, *args, **kw) -> tuple[bool, str]:
        """One firmware restart to clear an emergency stop, then wait for ready."""
        return _driver("print_alert_buttons").clear_shutdown(*args, **_kw(self.printer_id), **kw)

    def force_reboot(self, **kw) -> tuple[bool, str]:
        """The safe REBOOT flow (print_control.force_reboot); refused while printing."""
        return _driver("print_control").force_reboot(**_kw(self.printer_id), **kw)

    def purge(self, **kw) -> dict[str, Any]:
        """The guarded toolhead purge (panic.purge), held to this printer's nozzle ceiling."""
        return _driver("panic").purge(**kw)

    # ------------------------------------------------- Web UI one-shot shape

    def send(self, command: tuple, *, sender: Callable) -> Any:
        """Send one Web UI command shape through ``sender(adapter, view, command)``.

        Shapes: ``("control", action)``, ``("temperature", heater, target)``,
        ``("power", action)`` and ``("gcode", script)``. The sender is the Web
        UI's transport driver (``webui.controls.default_send``) or a test's.
        """
        return sender(self.adapter, self.view, command)


def for_printer(printer_id: str | None = None, *, adapter=None, view=None) -> PrinterControl:
    """The control service for one roster printer; None is the configured default printer."""
    return PrinterControl(printer_id, adapter=adapter, view=view)


# The control entry points: every surface that turns an operator's request into
# a printer command. Each must call this service; the enumeration test parses
# each one and fails when it is missing, does not call the service, or reaches
# a transport driver directly. A new entry point is added here and to the test.
ENTRY_POINTS = (
    # chat commands (the chat handlers serve Discord, Telegram, Matrix and Web UI chat;
    # they moved from plugins/print-feedback into print_handy/bot/handlers, #1323)
    ("print_handy/bot/handlers/print_stop.py", "print_stop_dispatch"),
    ("print_handy/bot/handlers/print_resume.py", "print_resume_dispatch"),
    ("print_handy/bot/handlers/print_reboot.py", "print_reboot_dispatch"),
    ("print_handy/bot/handlers/exclude.py", "exclude_dispatch"),
    ("print_handy/bot/handlers/common.py", "_cancel_spool_question_print"),
    ("print_handy/bot/handlers/common.py", "_offer_force_restart"),
    ("print_handy/bot/handlers/common.py", "alert_button_handlers"),
    ("print_handy/core/prepared_intake.py", "_offer_force_restart"),
    # listener alert buttons (Discord, Telegram and Matrix clicks all land here)
    ("print_handy/runtime/print_alert_buttons.py", "handle_click"),
    ("print_handy/runtime/print_alert_buttons.py", "recover_from_stop"),
    ("print_handy/runtime/moonraker_event_listener.py", "screen_cancel_reboot"),
    # Web UI control routes
    ("print_handy/webui/controls.py", "temperature_limit"),
    ("print_handy/webui/controls.py", "control"),
    ("print_handy/webui/controls.py", "set_temperature"),
    ("print_handy/webui/controls.py", "set_targets"),
    ("print_handy/webui/controls.py", "power_action"),
    ("print_handy/webui/controls.py", "send_gcode"),
    ("print_handy/webui/controls.py", "exclude_object"),
    # CLI
    ("print_handy/runtime/panic.py", "main"),
    ("print_handy/runtime/panic.py", "_nozzle_ceiling_refusal"),
)
