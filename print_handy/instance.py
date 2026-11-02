"""The printer instance this process serves, if any (#336).

This state lives apart from print_handy.listeners on purpose: the
supervisor runs that module as ``__main__`` (``python3 -m
print_handy.listeners --printer <id>``), and a module executed as
``__main__`` is a different module object from the one other code
imports under its canonical name. An identity stored in the launcher's
own globals therefore never reached the healthcheck or the domain
listeners importing ``print_handy.listeners`` — they all read ``None``
and resolved the roster's first printer (P1-81). This module is only
ever imported canonically, so the identity installed here is the
identity every importer reads.
"""
from __future__ import annotations

_printer_id: str | None = None


def printer_id() -> str | None:
    """The roster id of the printer this process serves, or None."""
    return _printer_id


def set_printer_id(printer_id: str | None) -> None:
    global _printer_id
    _printer_id = printer_id
