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


def used_tools(text):
    tools = {int(m.group(1)) for line in text.splitlines()
             if (m := TOOL_LINE.fullmatch(line.strip()))}
    # Standard Orca single-extruder start code uses T0 only on heater
    # declarations; that still identifies the logical tool for the explicit map.
    if not tools:
        tools = {int(m.group(1)) for line in text.splitlines()
                 if (m := HEATER_TOOL_LINE.fullmatch(line.strip()))}
    return tools


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
    if set(map(int, result)) != set(range(len(result))):
        raise RuntimeError('tool mapping must be contiguous from T0')
    return result


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


def validate_materials(text, material, state, mapping=None):
    if not isinstance(material, str) or not material.strip():
        raise RuntimeError('missing filament_type metadata')
    from ....core.orca_config import split_config_list
    try:
        materials = [item.strip() for item in split_config_list(material)]
    except ValueError as exc:
        raise RuntimeError('malformed per-tool filament_type metadata') from exc
    if any(not re.fullmatch(r'[A-Za-z0-9_+ .()-]+', item) for item in materials):
        raise RuntimeError('malformed per-tool filament_type metadata')
    tools = used_tools(text)
    if mapping is None:
        mapping = embedded_mapping(text, state)
    if mapping is None:
        if len(materials) != 1 or tools - {0}:
            raise RuntimeError('unknown tool mapping for multicolor material validation')
        from ....core.printer_state import same_material
        if not same_material(materials[0], state['spool']['material']):
            raise RuntimeError(f"G-code filament_type {material!r} != loaded {state['spool']['material']!r}")
        return None
    mapping = normalize_mapping(mapping, state)
    if not tools or tools - set(map(int, mapping)):
        raise RuntimeError('used tools disagree with explicit mapping')

    # Orca writes project-wide filament lists into G-code even when a derived
    # single-color plate remaps every object to T0. These unused entries are
    # slicer metadata, not extra toolheads or physical IFS slots.
    if len(materials) == len(mapping):
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


def finalize(path, colors, state=None, mapping=None):
    """Idempotent color/map insertion, then MD5 of exact remaining bytes.

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
    from .native_startup import normalize
    text = normalize(raw.decode('utf-8'))
    if mapping is not None:
        mapping = normalize_mapping(mapping, state)
        match = re.search(r'^; filament_type = (.+)$', text, re.M)
        if match is None:
            used = used_tools(text)
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
        validate_materials(text, material_text, state, mapping)
    tools = used_tools(text)
    if mapping is None and tools - {0}:
        raise RuntimeError('single-spool adapter cannot finalize multiple logical tools')
    lines = [line for line in text.splitlines(keepends=True)
             if not line.strip().lower().startswith('; zmod_color_data =')
             and not line.startswith(MAP_PREFIX)]
    metadata = []
    if mapping is not None:
        if state is None:
            raise RuntimeError('mapped finalization requires hardware state')
        recorded = {}
        for tool, slot in mapping.items():
            spool_id = state['filament_slots'][str(slot)].get('spoolman_id')
            recorded[tool] = {'physical_slot': slot, 'spoolman_id': spool_id} if spool_id is not None else slot
        metadata.append(MAP_PREFIX + json.dumps(recorded, sort_keys=True, separators=(',', ':')) + '\n')
        for tool in sorted(map(int, mapping)):
            slot = state['filament_slots'][str(mapping[str(tool)])]
            # Same fallback as slicing: a slot without a code gets its name's
            # color or the neutral default (WARNING logged), never a refusal.
            from print_handy.core.filament_color import slot_hex
            color = slot_hex(mapping[str(tool)], slot)
            metadata.append(f'; zmod_color_data = {tool}|{color}|{slot["material"]}\n')
    else:
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
