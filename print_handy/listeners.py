"""Listener domains: the registry behind the supervisor and healthcheck (#299).

Print Handy's listeners are split by *domain* — the configured printer's
connection / firmware type (`print_handy.printers.base.CONNECTIONS`). The
domain selects the listener *type*; a listener *instance* serves one
configured printer. Since #336 the container supervisor runs one instance
per roster printer (`--printer <id>`, below) and supervises and
healthchecks each instance independently — one instance failing must
never take down the bot, Spoolman, or another instance (#256). Nothing
here is a global singleton: the registry answers "which listener, if
any, serves this connection", once per printer instance.

Availability is three-valued, and the distinction matters to the supervisor:

- ``implemented`` — a listener module exists and is started.
- ``planned``     — the domain is known but has no listener yet. This is a
  normal state, not an error: the supervisor starts nothing for the domain
  and the container idles exactly as it did before the registry existed.
- ``none``        — the connection has no listener by definition (offline:
  Print Handy never contacts the printer).

An *unresolvable configuration* is none of these; callers keep the #256
handling for it (the gate exits 2 and the supervisor stops with the settings
error rather than guessing).

Normalised contract
-------------------
Every domain listener observes its printer and reports through one shared
shape, the shape the printer adapters' ``normalize_status`` already returns
and the Moonraker listener already works with internally:

- Status fields (``STATUS_FIELDS``): ``state``, ``filename``, ``progress``
  (0..1 or None), ``hotend_c`` / ``bed_c`` and their ``*_target_c``
  companions, plus optional ``remaining_min``, ``layer`` and ``total_layer``.
  ``validate_status`` pins the core of that shape.
- Event kinds (``EVENT_KINDS``): ``status`` (a fresh status snapshot),
  ``completion`` (a print finished), ``error`` (the printer reports an
  error; the payload keeps the domain's raw representation — for Bambu
  that includes its HMS entries), and ``snapshot_available`` (a camera
  snapshot can be fetched for this printer now). Consumers (Discord posts,
  the camera watchdog, timelapse) subscribe by kind and never need to know
  which domain produced the event.

Liveness is the existing heartbeat (print_handy.healthcheck): a running
listener beats with its domain's ``runtime`` identity, and the container
healthcheck requires that heartbeat exactly when the registry says a
listener is implemented for the configured connection.
"""
from __future__ import annotations

import runpy
import sys
from dataclasses import dataclass

from . import instance as _instance
from .printers import base as _printer_base

IMPLEMENTED = "implemented"
PLANNED = "planned"
NONE = "none"

# Core status fields every domain reports (adapters' normalize_status).
STATUS_FIELDS = ("state", "filename", "progress", "hotend_c", "bed_c")
# Optional status fields a domain reports when its protocol carries them.
STATUS_OPTIONAL_FIELDS = ("hotend_target_c", "bed_target_c", "remaining_min",
                          "layer", "total_layer", "toolheads")
# Event kinds a listener may emit into the bot core.
EVENT_KINDS = ("status", "completion", "error", "snapshot_available")


@dataclass(frozen=True)
class ListenerDomain:
    """One listener domain: a connection type and the listener serving it."""

    name: str  # the domain name; today identical to the connection it serves
    connection: str  # one of printers.base.CONNECTIONS
    availability: str  # IMPLEMENTED, PLANNED or NONE
    module: str | None = None  # `python3 -m` target when implemented
    runtime: str | None = None  # heartbeat identity when implemented
    transport: str = ""  # how the listener talks to the printer, for docs

    @property
    def implemented(self) -> bool:
        return self.availability == IMPLEMENTED


DOMAINS = {
    "moonraker": ListenerDomain(
        name="moonraker", connection="moonraker", availability=IMPLEMENTED,
        module="print_handy.hermes.moonraker_event_listener",
        runtime="moonraker_event_listener", transport="Moonraker websocket"),
    "bambulab_lan": ListenerDomain(
        name="bambulab_lan", connection="bambulab_lan", availability=IMPLEMENTED,
        module="print_handy.printers.bambulab.listener",
        runtime="bambu_event_listener", transport="Bambu LAN MQTT (long-lived)"),
    "prusalink": ListenerDomain(
        name="prusalink", connection="prusalink", availability=IMPLEMENTED,
        module="print_handy.hermes.family_event_listener",
        runtime="prusalink_event_listener",
        transport="PrusaLink HTTP status polling (#460)"),
    "elegoo_sdcp": ListenerDomain(
        name="elegoo_sdcp", connection="elegoo_sdcp", availability=IMPLEMENTED,
        module="print_handy.hermes.family_event_listener",
        runtime="elegoo_sdcp_event_listener",
        transport="Elegoo SDCP status polling (#460)"),
    "octoprint": ListenerDomain(
        name="octoprint", connection="octoprint", availability=IMPLEMENTED,
        module="print_handy.hermes.family_event_listener",
        runtime="octoprint_event_listener",
        transport="OctoPrint HTTP status polling (#460)"),
    "flashforge_stock": ListenerDomain(
        name="flashforge_stock", connection="flashforge_stock", availability=IMPLEMENTED,
        module="print_handy.hermes.family_event_listener",
        runtime="flashforge_stock_event_listener",
        transport="Stock Flashforge LAN HTTP status polling (#460)"),
    "offline": ListenerDomain(
        name="offline", connection="offline", availability=NONE,
        transport="none: Print Handy never contacts the printer"),
}

# The registry covers every connection a vendor may declare; a connection
# added to CONNECTIONS without a domain is a programming error, not a new
# supervisor state.
_missing = [c for c in _printer_base.CONNECTIONS if c not in DOMAINS]
if _missing:
    raise RuntimeError(f"listener registry has no domain for connections {_missing}")


def domain_for_connection(connection):
    """The listener domain serving `connection`, or None when unknown."""
    return DOMAINS.get(connection)


def implemented_connections():
    """Connections whose domain has a listener implementation today."""
    return tuple(c for c, domain in DOMAINS.items() if domain.implemented)


def validate_status(status):
    """Problems with a normalised status dict; empty when it fits the contract."""
    if not isinstance(status, dict):
        return ["status is not a dict"]
    problems = [f"missing status field {field!r}" for field in STATUS_FIELDS
                if field not in status]
    if "state" in status and not isinstance(status["state"], str):
        problems.append("status 'state' must be a string")
    if "filename" in status and not isinstance(status["filename"], str):
        problems.append("status 'filename' must be a string")
    for field in ("progress", "hotend_c", "bed_c"):
        value = status.get(field)
        if value is not None and (isinstance(value, bool)
                                  or not isinstance(value, (int, float))):
            problems.append(f"status {field!r} must be a number or None")
    progress = status.get("progress")
    if isinstance(progress, (int, float)) and not isinstance(progress, bool) \
            and not 0 <= progress <= 1:
        problems.append("status 'progress' must be within 0..1")
    return problems


# The printer instance this process serves, installed by launch() when it
# starts a per-printer instance (#336). A listener process serves exactly
# one printer for its whole life; the healthcheck resolves heartbeat
# ownership through here when no explicit printer is named. None in every
# other process (the bot, one-shot tools, the legacy no-selector launch).
# The value itself lives in print_handy.instance: this module is executed
# as __main__ by the supervisor, and a __main__ module's globals are not
# the canonical module's — storing the identity here left every importer
# of print_handy.listeners reading None (P1-81).


def instance_printer_id():
    """The roster id of the printer this process listens for, or None."""
    return _instance.printer_id()


def _set_instance_printer_id(printer_id):
    _instance.set_printer_id(printer_id)


def _instance_view(printer_id, data):
    """The settings a per-printer listener process runs on (#336).

    The printer's own view (settings.view_for), deep-copied, with the
    platform chat fields pointed at the printer's claimed chat when its
    roster entry sets a chat_id — the claim model bot.routing validates —
    so the listener's lifecycle posts land in the owning printer's channel
    (design §2.3/§2.5). An entry without a chat_id keeps the platform home
    chats, its fallback claim.
    """
    import copy

    from . import settings

    view = copy.deepcopy(settings.view_for(printer_id, data))
    claim = str(settings.by_id(printer_id, data).get("chat_id") or "")
    if claim:
        for section, key in (("discord", "chat_id"), ("telegram", "chat_id"),
                             ("matrix", "room_id")):
            if isinstance(view.get(section), dict):
                view[section][key] = claim
    return view


def _run_domain_module(module):
    """Run a listener domain module as ``__main__``.

    A module under ``print_handy.printers`` (the Bambu listener) is
    already in ``sys.modules`` by the time this runs: printer discovery
    imports every vendor-package module while looking for ``MODEL``.
    ``runpy`` warns about that ("found in sys.modules ... prior to
    execution", #409) and would leave the earlier import in place, so
    drop the stale entry first and let ``runpy`` execute it fresh.
    """
    sys.modules.pop(module, None)
    runpy.run_module(module, run_name="__main__")


def launch(data=None, printer_id=None):
    """Run a printer instance's listener in this process.

    Without `printer_id` this is the configured (default) printer — the
    original single-instance behaviour. With one, the instance serves
    that roster printer: the launcher installs the printer's settings
    view as this process's settings (settings.set_override) and records
    the instance identity (instance_printer_id) before running the domain
    module, so the module — written for one printer — reads its own
    printer's model, connection and channel throughout.

    Returns the supervisor-facing exit code when there is nothing to run
    (3 = the instance's domain has no listener yet or by definition); an
    implemented listener runs until it exits or raises, exactly as if its
    module had been started directly. Configuration problems propagate as
    settings.SettingsError, like the gate's unresolved case.
    """
    from . import healthcheck

    if printer_id is None:
        domain = healthcheck.listener_domain(data)
        if domain is None:
            print(f"listener: connection {healthcheck.listener_connection(data)!r} "
                  "has no registered listener domain; not starting one")
            return 3
        if not domain.implemented:
            print(f"listener: domain {domain.name!r} has no listener yet; not starting one")
            return 3
        _run_domain_module(domain.module)
        return 0

    from . import settings

    data = settings.current() if data is None else data
    settings.by_id(printer_id, data)  # an unknown id is the unresolved case
    domain = healthcheck.listener_domain(data, printer_id)
    if domain is None:
        print(f"listener: printer {printer_id!r}: connection "
              f"{healthcheck.listener_connection(data, printer_id)!r} has no "
              "registered listener domain; not starting one")
        return 3
    if not domain.implemented:
        print(f"listener: printer {printer_id!r}: domain {domain.name!r} has "
              "no listener yet; not starting one")
        return 3
    settings.set_override(_instance_view(printer_id, data))
    _set_instance_printer_id(printer_id)
    # #390 stage 4: every log line from this process carries its printer_id.
    # The token is deliberately never reset — the binding is process-lifetime,
    # exactly like the instance identity above.
    from . import logs
    logs.bind(printer_id=printer_id)
    logs.bind_process(printer_id=printer_id)  # worker threads inherit no ContextVar (P2-161)
    _run_domain_module(domain.module)
    return 0


def main(argv=None):
    from .core import resources
    resources.apply_to_environment()  # before numpy/onnxruntime load: pools follow the CPU quota
    args = list(sys.argv[1:] if argv is None else argv)
    printer_id = None
    if "--printer" in args:
        index = args.index("--printer")
        if index + 1 >= len(args):
            print("listener: --printer requires a printer id", file=sys.stderr)
            return 2
        printer_id = args[index + 1]
    try:
        from . import settings
        warning = settings.defaulted_model_warning()
    except Exception:  # an advisory check never stops the listener
        warning = None
    if warning:
        print(f"listener: WARNING {warning}", file=sys.stderr)
    try:
        return launch(printer_id=printer_id)
    except Exception as exc:  # settings errors name themselves; keep the code 2 contract
        from . import settings

        if isinstance(exc, settings.SettingsError):
            print(f"listener: configuration unresolved: {exc}", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    sys.exit(main())
