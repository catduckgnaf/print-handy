"""The tool map header line Print Handy writes into every mapped G-code file.

One line, ``; print_handy_tool_slots = <json>``, records which physical slot
(and which Spoolman spool) each logical tool was sliced for. Finalization
writes it, tool renumbering rewrites it, and launch, remap, offline
verification and the spool receipt read it back. This module is the only
place that knows its spelling: everything else calls ``parse``, ``format``
or ``is_map_line``.

The value is compact JSON with sorted keys: logical tool numbers as strings,
each mapped to ``{"physical_slot": n}`` plus ``"spoolman_id"`` when the slot
had a spool bound at slice time.

Files sliced before 2.4.0 carry the old ``; hermes_tool_slots = `` line
instead. They are not read (#1817): ``parse`` refuses them with
``STALE_MESSAGE`` so the operator reslices, and ``is_map_line`` still
matches the old line so finalization never leaves it beside a new one.
"""
from __future__ import annotations

import json
import re

__all__ = ["ANCHOR", "PREFIX", "STALE_MESSAGE", "StaleToolMap", "ToolMapError",
           "entry", "format_line", "is_map_line", "mentions", "parse", "parse_line"]

PREFIX = "; print_handy_tool_slots = "
# The literal line prefix, usable as a GcodeLines anchor.
ANCHOR = PREFIX
# Written by Print Handy before 2.4.0. Recognised only to refuse and to strip.
_OLD_PREFIX = "; hermes_tool_slots = "
STALE_MESSAGE = "this G-code was sliced by an older Print Handy; reslice"


class ToolMapError(RuntimeError):
    """The file's tool map line is duplicated or unreadable."""


class StaleToolMap(ToolMapError):
    """The file carries only the pre-2.4.0 map line."""

    def __init__(self):
        super().__init__(STALE_MESSAGE)


def entry(slot, spoolman_id=None):
    """One tool's map value."""
    value = {"physical_slot": slot}
    if spoolman_id is not None:
        value["spoolman_id"] = spoolman_id
    return value


def format_line(mapping):
    """The map line (no line ending) for ``{tool: entry(...)}``."""
    for tool, value in mapping.items():
        if not isinstance(value, dict) or "physical_slot" not in value:
            raise ValueError(f"tool map value for T{tool} must be entry(slot, spoolman_id)")
    return PREFIX + json.dumps({str(k): v for k, v in mapping.items()}, sort_keys=True, separators=(",", ":"))


def is_map_line(line):
    """True for a current or pre-2.4.0 map line; finalization drops both before writing its own."""
    return line.startswith(PREFIX) or line.startswith(_OLD_PREFIX)


def mentions(text):
    """A cheap substring test: the text may hold a map line (current or stale)."""
    return PREFIX in text or _OLD_PREFIX in text


def _decode(payload):
    try:
        raw = json.loads(payload.rstrip("\r"))
    except (ValueError, TypeError) as exc:
        raise ToolMapError("malformed tool mapping metadata") from exc
    if not isinstance(raw, dict):
        raise ToolMapError("malformed tool mapping metadata")
    out = {}
    for tool, value in raw.items():
        if not isinstance(value, dict) or "physical_slot" not in value:
            raise ToolMapError("malformed tool mapping metadata")
        out[str(tool)] = entry(value["physical_slot"], value.get("spoolman_id"))
    return out


def parse_line(line):
    """The map one line holds, or None when it is not a current map line.

    A pre-2.4.0 line raises StaleToolMap; an unreadable one ToolMapError."""
    if line.startswith(PREFIX):
        return _decode(line[len(PREFIX):])
    if line.startswith(_OLD_PREFIX):
        raise StaleToolMap()
    return None


def parse(text, lines=None):
    """``{tool: {"physical_slot": n, "spoolman_id"?: id}}`` from ``text``, or None when it has no map.

    ``lines`` is the caller's GcodeLines scan of ``text`` (declare ``ANCHOR``
    in it); without it the text is scanned here. Raises StaleToolMap when the
    file has only the pre-2.4.0 line, ToolMapError when the map is duplicated
    or unreadable.
    """
    if lines is not None:
        text = lines.text
        found = lines.findall(r"^" + re.escape(PREFIX) + r"(.*)$", ANCHOR)
    else:
        text = text or ""
        found = [line[len(PREFIX):] for line in text.splitlines() if line.startswith(PREFIX)]
    if not found:
        if _OLD_PREFIX in text and any(line.startswith(_OLD_PREFIX) for line in text.splitlines()):
            raise StaleToolMap()
        return None
    if len(found) != 1:
        raise ToolMapError("duplicate tool mapping metadata")
    return _decode(found[0])

