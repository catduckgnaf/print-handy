"""Relabel a mapped ZMod G-code so its tool numbers ARE the physical IFS slots (#1648).

A ZMod print that Print Handy starts names its slots only in the start
command (``SET_ZCOLOR ... T0=3``). A reprint from the printer screen or
HelixScreen sends no map, so ZMod applies its default identity map
(T0 -> slot 1, T1 -> slot 2, ...) and prints the wrong filament. Relabeling
the uploaded copy so logical tool ``Tn`` is the tool for slot ``n + 1``
makes the default map the chosen one; the start command then carries the
identity map (``T2=3``) and agrees with it. That identity default is what
the #1648 report observed; the repository holds no ZMod source that states it.

Only the bytes uploaded to the printer change. The archived G-code, its
sha256 bindings and the per-tool usage bookkeeping keep the original
numbering; the caller records both maps.

The relabel touches tool selections (``T<n>`` lines), Print Handy's own map
and color lines, and the per-filament header lists ZMod and Print Handy read
(colour, type, settings id, temperatures, usage). It never touches a heater
line's tool parameter: on these single-extruder printers ``M104 S<t> T0``
addresses the one heater, not a filament, and ZMod's native startup contract
(``printers/flashforge/zmod/native_startup.py``) requires exactly ``T0``
there; Klipper would read ``M104 T2`` as ``extruder2`` and fail the start.

For a ``.gcode.3mf`` ZMod takes the plate's filaments from
``Metadata/slice_info.config`` (``core/gcode_3mf.py``), which the package
builds from the relabeled usage lists, so a single-tool slice on slot 3 is
listed as filament 3, tool T2, even though its body has no ``T`` line.

A map that is not one-to-one, names a slot outside 1..slots, or leaves a
used tool unmapped is not relabeled (``None``): the caller keeps today's
behavior.
"""
from __future__ import annotations

import hashlib
import json
import re

MAP_PREFIX = '; hermes_tool_slots = '
COLOR_PREFIX = '; zmod_color_data = '
# Group 3 keeps a CRLF file's '\r' with the line.
_TOOL_LINE = re.compile(r'^([ \t]*)T(\d+)([ \t]*(?:;.*)?\r?)$')
# Per-filament header/footer lists: one entry per logical tool.
LIST_KEYS = ('filament_colour', 'extruder_colour', 'filament_type', 'filament_settings_id',
             'filament_ids', 'nozzle_temperature', 'nozzle_temperature_initial_layer',
             'filament used [g]', 'filament used [mm]', 'filament used [cm3]', 'filament cost')
_ZERO_KEYS = {'filament used [g]', 'filament used [mm]', 'filament used [cm3]', 'filament cost'}
_LIST_LINE = re.compile(r'^; (' + '|'.join(re.escape(k) for k in LIST_KEYS) + r') = (.*?)(\r?)$')
_USAGE_KEYS = ('filament used [g]', 'filament used [mm]')


def used_tools(text):
    """The logical tools ``text`` feeds filament through, as a set of ints.

    Tool-change lines are the answer when the file has any. A single-tool
    Orca slice has none: its only ``T`` is the heater's ``M104 S<t> T0``,
    which names the one heater, not a filament, so heater lines are never
    read here. Without tool lines the tool comes from what names filament:
    Print Handy's map line (``; hermes_tool_slots``), else the
    ``; zmod_color_data`` lines, checked against the first per-filament usage
    list (its entries with nonzero use) when the file has one; with neither,
    the usage list alone. Disagreeing sources, or none, give an empty set,
    which ``relabel`` refuses.
    """
    tools, mapped, colored, usage = set(), None, set(), None
    for line in text.splitlines():
        head = line.lstrip(' \t')[:1]
        if head == 'T':
            m = _TOOL_LINE.match(line)
            if m:
                tools.add(int(m.group(2)))
        elif head == ';' and not tools:
            if line.startswith(MAP_PREFIX):
                try:
                    mapped = {int(k) for k in json.loads(line[len(MAP_PREFIX):])}
                except (ValueError, TypeError, AttributeError):
                    mapped = set()  # unreadable map: relabel refuses on the map line as well
            elif line.lower().startswith(COLOR_PREFIX):
                tool = line[len(COLOR_PREFIX):].partition('|')[0].strip()
                if tool.isdigit():
                    colored.add(int(tool))
            elif usage is None:
                m = _LIST_LINE.match(line)
                if m and m.group(1) in _USAGE_KEYS:
                    usage = _nonzero_entries(m.group(2))
    if tools:
        return tools
    named = mapped if mapped is not None else colored
    if not named:
        return usage or set()
    if usage and usage != named:
        return set()
    return named


def _nonzero_entries(raw):
    """Indexes of a usage list's entries above zero; an unreadable list names none."""
    out = set()
    for i, item in enumerate(raw.replace(';', ',').split(',')):
        try:
            if float(item) > 0:
                out.add(i)
        except ValueError:
            return set()
    return out


def tool_map(mapping, used, slots=4):
    """{old_tool: new_tool} that makes each used tool's number its slot - 1, or None when unsafe."""
    try:
        normalized = {int(t): int(s['physical_slot'] if isinstance(s, dict) else s)
                      for t, s in (mapping or {}).items()}
    except (TypeError, ValueError, KeyError):
        return None
    if not used or any(t not in normalized for t in used):
        return None
    chosen = {t: normalized[t] for t in used}
    if len(set(chosen.values())) != len(chosen) or any(not 1 <= s <= slots for s in chosen.values()):
        return None
    return {t: s - 1 for t, s in chosen.items()}


def _split_unquoted(raw, sep):
    """Split on ``sep`` outside double quotes; items keep their quotes (Orca's `"Silk PLA";PLA`)."""
    items, start, quoted, escaped = [], 0, False, False
    for i, char in enumerate(raw):
        if escaped:
            escaped = False
        elif quoted and char == '\\':
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif not quoted and raw.startswith(sep, i):
            items.append(raw[start:i])
            start = i + len(sep)
    items.append(raw[start:])
    return items


def _relabel_list(key, raw, relabel_map):
    raw = raw.strip()
    # Orca writes settings vectors with ';' and its usage totals with ', '.
    sep = ', ' if key in _ZERO_KEYS else ';'
    values = _split_unquoted(raw, sep)
    if not values or max(relabel_map) >= len(values):
        return raw
    size = max(max(relabel_map.values()) + 1, len(values))
    filler = '0' if key in _ZERO_KEYS else values[0]
    out = [filler] * size
    targets = set(relabel_map.values())
    for i, value in enumerate(values):
        # An unused source entry keeps its place when no used tool moves there.
        if i not in targets and i not in relabel_map:
            out[i] = value
    for old, new in relabel_map.items():
        out[new] = values[old]
    return sep.join(out)


def relabel(text, mapping, used, slots=4):
    """Return (relabeled_text, identity_mapping), or (None, None) when the map cannot be relabeled.

    ``mapping`` is the launch map (logical tool -> 1-based slot, or the
    embedded ``{"physical_slot": n, "spoolman_id": m}`` form); ``used`` the
    tools the file feeds (``used_tools``). A map that is already the identity returns the
    text unchanged with its identity map. A leading ``; MD5:`` line is
    recomputed over the relabeled body.
    """
    relabel_map = tool_map(mapping, used, slots)
    if relabel_map is None:
        return None, None
    identity = {str(new): new + 1 for new in relabel_map.values()}
    if all(old == new for old, new in relabel_map.items()):
        return text, identity
    md5_line = None
    body = text
    if body.startswith('; MD5:'):
        md5_line, _, body = body.partition('\n')
    lines = body.split('\n')
    for i, line in enumerate(lines):
        head = line.lstrip(' \t')[:1]
        if head == 'T':
            m = _TOOL_LINE.match(line)
            if m and int(m.group(2)) in relabel_map:
                lines[i] = f"{m.group(1)}T{relabel_map[int(m.group(2))]}{m.group(3)}"
        elif head == ';':
            if line.startswith(MAP_PREFIX):
                cr = '\r' if line.endswith('\r') else ''
                try:
                    data = json.loads(line[len(MAP_PREFIX):].rstrip('\r'))
                    moved = {str(relabel_map.get(int(k), int(k))): v for k, v in data.items()}
                except (ValueError, TypeError, AttributeError):
                    return None, None
                lines[i] = MAP_PREFIX + json.dumps(moved, sort_keys=True, separators=(',', ':')) + cr
            elif line.lower().startswith(COLOR_PREFIX):
                tool, bar, rest = line[len(COLOR_PREFIX):].partition('|')
                if bar and tool.strip().isdigit() and int(tool) in relabel_map:
                    lines[i] = f"{line[:len(COLOR_PREFIX)]}{relabel_map[int(tool)]}|{rest}"
            else:
                m = _LIST_LINE.match(line)
                if m:
                    lines[i] = f"; {m.group(1)} = {_relabel_list(m.group(1), m.group(2), relabel_map)}{m.group(3)}"
    body = '\n'.join(lines)
    if md5_line is not None:
        cr = '\r' if md5_line.endswith('\r') else ''
        return '; MD5:' + hashlib.md5(body.encode('utf-8')).hexdigest() + cr + '\n' + body, identity
    return body, identity
