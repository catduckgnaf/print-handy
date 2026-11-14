"""Shell-facing probe: which listener serves a printer instance?

docker/supervise.sh runs ``python3 -m print_handy.listener_gate`` before
starting children (#256, generalised by #299, per printer since #336).
The decision comes from the resolved configuration (the printer model
and its vendor connection, via print_handy.healthcheck) and the listener
registry (print_handy.listeners), never from whether a service URL
happens to be filled in, so a misconfiguration still fails loudly in
settings validation instead of silently changing which children run.

Forms: no arguments gates the configured (default) printer, as always;
``--printer <id>`` gates that roster instance; ``--roster`` prints the
configured printer ids, one per line, so the supervisor can enumerate
the instances it must gate (exit 2 when the roster cannot be resolved).

Exit codes: 0 = the instance's listener domain is implemented, start the
listener (through the registry launcher, ``python3 -m print_handy.listeners``);
3 = the domain is known but has no listener yet, or has none by definition
(the supervisor starts nothing and idles as before); 2 = the configuration
could not be resolved (the supervisor stops with the printed error rather
than guessing).
"""
from __future__ import annotations

import sys

from . import healthcheck, listeners, settings


def _roster():
    try:
        entries = settings.printers()
    except settings.SettingsError as exc:
        print(f"listener gate: configuration unresolved: {exc} [PH_GATE_CONFIGURATION]", file=sys.stderr)
        return 2
    warning = settings.unknown_settings_warning()
    if warning:
        print(f"listener gate: WARNING {warning}", file=sys.stderr)
    for entry in entries:
        print(entry["id"])
    return 0


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if "--roster" in args:
        return _roster()
    printer_id = None
    if "--printer" in args:
        index = args.index("--printer")
        if index + 1 >= len(args):
            print("listener gate: --printer requires a printer id", file=sys.stderr)
            return 2
        printer_id = args[index + 1]
    who = f"printer {printer_id!r}: " if printer_id is not None else ""
    try:
        domain = healthcheck.listener_domain(printer_id=printer_id)
    except settings.SettingsError as exc:
        print(f"listener gate: configuration unresolved: {exc} [PH_GATE_CONFIGURATION]", file=sys.stderr)
        return 2
    warning = settings.unknown_settings_warning()
    if warning:
        print(f"listener gate: WARNING {warning}", file=sys.stderr)
    if domain is None:
        connection = healthcheck.listener_connection(printer_id=printer_id)
        print(f"listener gate: {who}connection {connection!r} has no registered listener domain; not starting one")
        return 3
    if domain.implemented:
        print(f"listener gate: {who}domain {domain.name!r} listener is {domain.module}")
        return 0
    if domain.availability == listeners.NONE:
        print(f"listener gate: {who}domain {domain.name!r} has no listener; not starting one")
    else:
        print(f"listener gate: {who}domain {domain.name!r} has no listener yet; not starting one")
    return 3


if __name__ == "__main__":
    sys.exit(main())
