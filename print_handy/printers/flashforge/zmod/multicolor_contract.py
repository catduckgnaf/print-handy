"""Checksum-bound per-tool material contract, shared by finalization and launch."""
import hashlib
import json
import os
from pathlib import Path
import re

MAP_PREFIX = '; hermes_tool_slots = '
# Matched against every line of the plate, so compiled once.
TOOL_LINE = re.compile(r'T(\d+)(?:\s*;.*)?')
HEATER_TOOL_LINE = re.compile(r'M10[49].*\bT(\d+)\b.*')
# First characters a stripped tool/heater line can hide behind (#925): the
# letter itself, or whitespace strip() removes that splitlines() does not
# already treat as a line boundary. Every other line — the motion and
# comment bulk of a plate — is skipped without a strip or a regex call.
_TOOL_CANDIDATES = frozenset('T \t\x1f\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005'
                             '\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000')
_HEATER_CANDIDATES = frozenset('M \t\x1f\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005'
                               '\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000')


FILAMENT_TYPE_LINE = re.compile(r'^; filament_type = (.+)$', re.M)


def _first_line_match(text, pattern, literal):
    """pattern.search(text) for a re.M pattern '^<literal>...': the same match, found by trying the
    pattern only where the literal starts a line instead of at every character (about 1 s on 25 MB)."""
    pos = text.find(literal)
    while pos >= 0:
        if pos == 0 or text[pos - 1] == '\n':
            found = pattern.match(text, pos)
            if found is not None:
                return found
        pos = text.find(literal, pos + 1)
    return None


def used_tools(text):
    # One streaming pass over the plate (#925): the old scan paid a strip()
    # and a fullmatch() for every one of ~1.6M lines; candidate lines still
    # go through the exact same fullmatch, everything else is one lookup.
    # (A compiled whole-buffer multiline regex was measured slower than
    # this on the same file, so the pass stays line-based but filtered.)
    # Standard Orca single-extruder start code uses T0 only on heater
    # declarations; that still identifies the logical tool for the explicit map.
    # Both sets come from one pass (a second pass cost 0.4 s on 25 MB, #1392):
    # the heater set is the answer only when no tool line exists.
    tools, heater_tools = set(), set()
    tool_match, heater_match = TOOL_LINE.fullmatch, HEATER_TOOL_LINE.fullmatch
    for line in text.splitlines():
        head = line[:1]
        if head in _TOOL_CANDIDATES and (m := tool_match(line.strip())):
            tools.add(int(m.group(1)))
        if not tools and head in _HEATER_CANDIDATES and (m := heater_match(line.strip())):
            heater_tools.add(int(m.group(1)))
    return tools or heater_tools


def normalize_mapping(mapping, state):
    if not isinstance(mapping, dict) or not mapping:
        raise RuntimeError('unknown tool mapping: explicit nonempty tool-to-slot map required')
    result = {}
    for key, value in mapping.items():
        if not re.fullmatch(r'[0-3]', str(key)):
            raise RuntimeError('malformed tool mapping key')
        if isinstance(value, dict):
            slot = value.get('physical_slot')
            embedded_spool_id = value.get('spoolman_id')
        else:
            slot = value
            embedded_spool_id = None
        if isinstance(slot, bool) or not isinstance(slot, int) or slot not in range(1, 5):
            raise RuntimeError('malformed physical slot mapping')
        configured = state.get('filament_slots', {}).get(str(slot), {})
        if not configured.get('material') or not configured.get('physical_color_verified'):
            raise RuntimeError(f'unknown or unverified mapping for physical slot {slot}')
        if embedded_spool_id is not None and embedded_spool_id != configured.get('spoolman_id'):
            # print-now --accept-spool-change: the operator confirmed the swap. Material family, live slot
            # material and live RGB checks still apply; only the slice-time spool id is waived.
            if os.environ.get('PRINT_ACCEPT_SPOOL_CHANGE') != '1':
                raise RuntimeError(f'slot {slot} spool changed since slicing (was {embedded_spool_id}, now {configured.get("spoolman_id")}); reslice or confirm')
        result[str(key)] = slot
    # The mapped tools need not be contiguous from T0 (#1305): a slicer plate
    # that uses a subset of the project's filaments (filaments 1 and 3, or
    # filament 3 alone) emits exactly those tools (T0 and T2, or T2), and the
    # operator's explicit map for them is the selection to preserve. What must
    # hold is coverage — every tool the file uses is mapped — and that is
    # enforced by validate_materials and finalize, which see the file's used
    # tools; this function only validates the map itself.
    #
    # Two tools may name one slot (a shared spool, classify_ifs_action's
    # 'shared_spool'): each tool is still material-checked against that slot,
    # and it does not enable infinite spool. Print Handy's own slices never
    # produce one (two picks of one slot merge into one tool); only an
    # operator's explicit map or remap can.
    return result


def drop_unused_tools(text, mapping, used=None):
    """The map without entries for tools the file never selects.

    Only for maps Print Handy built itself, where an unused entry is a
    known by-product (a support filament's tool when Orca generated no
    support, #867); an operator's explicit map is checked, not trimmed, by
    validate_materials. A file that selects no tool at all is left for the
    coverage check to refuse."""
    if used is None:
        used = used_tools(text)
    if not used:
        return dict(mapping)
    return {tool: slot for tool, slot in mapping.items() if int(tool) in used}


def describe_mapping(mapping, state):
    """Plain operator-facing tool-to-slot and color/material lines."""
    normalized = normalize_mapping(mapping, state)
    rows = []
    for tool, slot in sorted(normalized.items(), key=lambda item: int(item[0])):
        cfg = state.get('filament_slots', {}).get(str(slot), {})
        color = cfg.get('color') or cfg.get('name') or 'unknown color'
        material = cfg.get('material') or 'unknown material'
        rows.append(f'T{tool} -> slot {slot}: {color} {material}')
    return '\n'.join(rows)


def embedded_mapping(text, state):
    lines = [line[len(MAP_PREFIX):] for line in text.splitlines() if line.startswith(MAP_PREFIX)]
    if not lines:
        return None
    if len(lines) != 1:
        raise RuntimeError('duplicate tool mapping metadata')
    try:
        raw_mapping = json.loads(lines[0])
    except (ValueError, TypeError) as exc:
        raise RuntimeError('malformed tool mapping metadata') from exc
    return normalize_mapping(raw_mapping, state)


def validate_materials(text, material, state, mapping=None, tools=None):
    """``tools`` is used_tools(text) when the caller already has it (finalize does)."""
    if not isinstance(material, str) or not material.strip():
        raise RuntimeError('missing filament_type metadata')
    from ....core.orca_config import split_config_list
    try:
        materials = [item.strip() for item in split_config_list(material)]
    except ValueError as exc:
        raise RuntimeError('malformed per-tool filament_type metadata') from exc
    if any(not re.fullmatch(r'[A-Za-z0-9_+ .()-]+', item) for item in materials):
        raise RuntimeError('malformed per-tool filament_type metadata')
    if tools is None:
        tools = used_tools(text)
    # An operator's explicit map is held to "map only the used tools"; a map
    # already embedded in a finalized file is not, so files finalized before
    # #1305 (an unused support tool, a stale T0) keep launching as they did.
    explicit = mapping is not None
    if mapping is None:
        mapping = embedded_mapping(text, state)
    if mapping is None:
        if len(materials) != 1 or tools - {0}:
            raise RuntimeError('unknown tool mapping for multicolor material validation')
        from ....core import material_accept
        from ....core.printer_state import same_material
        if not same_material(materials[0], state['spool']['material']):
            # A warning the operator may accept with Print as sliced (#1336).
            found = material_accept.material_problem(materials[0], state['spool']['material'])
            if found:
                raise RuntimeError(material_accept.message(found, ''))
        return None
    mapping = normalize_mapping(mapping, state)
    if not tools or tools - set(map(int, mapping)):
        raise RuntimeError('used tools disagree with explicit mapping')
    unused = sorted(set(map(int, mapping)) - tools)
    if unused and explicit:
        # An explicit map entry for a tool the file never selects would be
        # finalized into zmod_color_data and SET_ZCOLOR, and launch would
        # require that slot loaded and color-matched, for nothing (#1305).
        names = ', '.join(f'T{tool}' for tool in unused)
        used_names = ', '.join(f'T{tool}' for tool in sorted(tools))
        raise RuntimeError(f'tool mapping names {names}, which the file never uses '
                           f'(it uses {used_names}); map only the used tools')

    # Orca writes project-wide filament lists into G-code even when a derived
    # single-color plate remaps every object to T0. These unused entries are
    # slicer metadata, not extra toolheads or physical IFS slots.
    # The positional branch (material index == tool index for every mapped
    # tool) applies only when the list is long enough to index every mapped
    # tool; a sparse map can share the list's length by coincidence (a map
    # for T0 and T2 against a two-entry list), and indexing it positionally
    # would run past the list instead of naming the omission.
    if len(materials) == len(mapping) and max(map(int, mapping)) < len(materials):
        indexes = sorted(mapping, key=int)
    else:
        indexes = sorted(str(tool) for tool in tools)
        if any(int(index) >= len(materials) for index in indexes):
            raise RuntimeError('per-tool material metadata omits a used logical tool')
    for index in indexes:
        if index not in mapping:
            continue
        slot = mapping[index]
        actual = state['filament_slots'][str(slot)]['material']
        from ....core.printer_state import same_material
        if not same_material(materials[int(index)], actual):
            raise RuntimeError(f'T{index} material {materials[int(index)]!r} != slot {slot} material {actual!r}')
    return mapping


def _mapped_metadata(mapping, state):
    """The map line and one zmod_color_data line per tool that finalize inserts for ``mapping``."""
    recorded = {}
    for tool, slot in mapping.items():
        spool_id = state['filament_slots'][str(slot)].get('spoolman_id')
        recorded[tool] = {'physical_slot': slot, 'spoolman_id': spool_id} if spool_id is not None else slot
    metadata = [MAP_PREFIX + json.dumps(recorded, sort_keys=True, separators=(',', ':')) + '\n']
    from print_handy.core.filament_color import slot_hex
    for tool in sorted(map(int, mapping)):
        slot = state['filament_slots'][str(mapping[str(tool)])]
        # Same fallback as slicing: a slot without a code gets its name's
        # color or the neutral default (WARNING logged), never a refusal.
        color = slot_hex(mapping[str(tool)], slot)
        metadata.append(f'; zmod_color_data = {tool}|{color}|{slot["material"]}\n')
    return metadata


def finalize(path, colors, state=None, mapping=None, drop_unused=False):
    """Idempotent color/map insertion, then MD5 of exact remaining bytes.

    drop_unused=True trims map entries for tools the file never selects
    (drop_unused_tools) before validation; only Print Handy's own slice path
    passes it. An explicit operator map naming an unused tool is refused.

    Existing invalid checksums are refused, never silently repaired. Toolpaths,
    temperatures and tool commands are preserved; documented native startup
    heater declarations are inserted before checksum generation when absent.
    """
    path = Path(path)
    raw = path.read_bytes()
    if raw.startswith(b'; MD5:'):
        first, sep, payload = raw.partition(b'\n')
        if not sep or first.rstrip(b'\r') != b'; MD5:' + hashlib.md5(payload).hexdigest().encode():
            raise RuntimeError('refusing to replace invalid existing ZMod checksum')
        raw = payload
    # No shortcut for a file that already looks finalized: a valid MD5 and Print
    # Handy's own map lines can be written by anyone, so every call normalizes
    # and validates in full; an unchanged result is not rewritten (#1392 review).
    from .native_startup import normalize
    text = normalize(raw.decode('utf-8'))
    normalized = text
    tools = used_tools(text)
    if mapping is not None:
        if drop_unused:
            mapping = drop_unused_tools(text, mapping, used=tools)
        mapping = normalize_mapping(mapping, state)
        match = _first_line_match(text, FILAMENT_TYPE_LINE, '; filament_type = ')
        if match is None:
            used = tools
            if not used:
                raise RuntimeError('missing filament_type metadata and no logical tool was used')
            if used - set(map(int, mapping)):
                raise RuntimeError('used logical tool is outside explicit physical IFS mapping')
            materials = ['PLA'] * (max(map(int, mapping)) + 1)
            for tool in used:
                materials[tool] = state['filament_slots'][str(mapping[str(tool)])]['material']
            text = text.rstrip('\n') + '\n; filament_type = ' + ';'.join(materials) + '\n'
            lines = text.splitlines(keepends=True)
            material_text = ';'.join(materials)
        else:
            material_text = match.group(1).strip()
    if text is not normalized:  # the filament_type line was appended
        tools = used_tools(text)
    if mapping is not None:
        validate_materials(text, material_text, state, mapping, tools=tools)
    if mapping is None and tools - {0}:
        raise RuntimeError('single-spool adapter cannot finalize multiple logical tools')
    # Both dropped kinds of line contain '_' (no character lowercases to '_'), so the
    # strip/lower test runs only on those instead of on every line (#1392).
    lines = [line for line in text.splitlines(keepends=True)
             if '_' not in line
             or not (line.strip().lower().startswith('; zmod_color_data =') or line.startswith(MAP_PREFIX))]
    if mapping is not None:
        if state is None:
            raise RuntimeError('mapped finalization requires hardware state')
        metadata = _mapped_metadata(mapping, state)
    else:
        metadata = []
        color, material = colors
        metadata.append(f'; zmod_color_data = 0|{color}|{material}\r\n')
    index = next((i for i, line in enumerate(lines) if line.strip().lower().startswith('; header_block_end')), 0)
    lines[index:index] = metadata
    payload = ''.join(lines).encode('utf-8')
    result = b'; MD5:' + hashlib.md5(payload).hexdigest().encode() + b'\r\n' + payload
    if result != path.read_bytes():
        path.write_bytes(result)
    return hashlib.sha256(result).hexdigest()


def infinite_spool_groups(slots):
    """Candidate groups under the AD5X contract: same color AND same material.

    Firmware owns the run-out switch. This only classifies documented metadata.
    A single spool, a color-only match, or a material-only match is not a group.
    SET_EXTRUDER_SLOT, SET_CURRENT_PRUTOK, and PRINT-menu remaps are not inputs.
    """
    buckets = {}
    for slot_id, slot in slots.items():
        if isinstance(slot_id, bool) or not str(slot_id).isdigit():
            raise RuntimeError('malformed IFS slot id')
        from print_handy.core.filament_color import first_hex, resolve
        material = slot.get('material')
        # Any hex form (#RGB, RRGGBBAA) counts; a color name is read through its
        # name table. Only a slot with no color at all still cannot be grouped:
        # two unknowns must never look like one color.
        color, source = resolve({**slot, 'hex': first_hex(slot.get('hex')) or first_hex(slot.get('color'))})
        if source == 'default':
            raise RuntimeError(f'slot {slot_id} has no documented color')
        if not isinstance(material, str) or not material.strip():
            raise RuntimeError(f'slot {slot_id} has no documented material type')
        key = ('#' + color, material.strip())
        buckets.setdefault(key, []).append(int(slot_id))
    return [{'color': color, 'material': material, 'slots': sorted(members)}
            for (color, material), members in sorted(buckets.items()) if len(members) >= 2]


def classify_ifs_action(action):
    """Name a host action. None of these enable infinite spool."""
    names = {
        'set_extruder_slot': 'active-slot correction only',
        'set_current_prutok': 'active filament record only',
        'print_menu_remap': 'file-color to spool assignment',
        'shared_spool': 'one physical spool for more than one tool index',
    }
    if action not in names:
        raise RuntimeError('unknown IFS action')
    return {'action': action, 'classification': names[action], 'enables_infinite_spool': False}
