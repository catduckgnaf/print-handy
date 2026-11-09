"""Per-layer custom G-code from a source 3MF (#870): height-based color changes.

A Bambu Studio, OrcaSlicer or PrusaSlicer project can carry
``Metadata/custom_gcode_per_layer.xml``: entries pinned to a layer height that
switch the active extruder (ChromaCanvas-style color-by-height prints, signs,
any authored tool change by layer), pause the print, or run custom G-code.

Print Handy slices from its own template and rebuilt geometry, so without
explicit carrying those entries vanish: a one-part height-color model asked
for one color and printed in it, silently. This module is the inventory and
the rewrite: parse the entries, say which extruders the tool changes use,
remap those extruders onto the job's tools for the geometry export, or turn
them into pauses for a single-extruder job whose operator chose to swap the
filament by hand. Pauses and custom G-code are reported, never carried as
tool changes.
"""
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

MEMBER = 'Metadata/custom_gcode_per_layer.xml'

_TOOL_WORDS = ('tool_change', 'toolchange', 'extruder_change', 'extruderchange',
               'color_change', 'colour_change', 'colorchange', 'colourchange')
# Numeric types appear in two slicer dialects: Bambu/Orca writes
# ColorChange=0, PausePrint=1; PrusaSlicer writes PausePrint=1, ColorChange=2,
# ToolChange=4. The gcode attribute, when it names the action, wins over the
# number (the #868 fixture pairs type="2" with gcode="tool_change").
_TYPE_KINDS = {'0': 'tool_change', '1': 'pause', '2': 'tool_change', '4': 'tool_change'}
_NAME_KINDS = {'colorchange': 'tool_change', 'colourchange': 'tool_change',
               'toolchange': 'tool_change', 'extruderchange': 'tool_change',
               'pauseprint': 'pause', 'pause': 'pause'}


def kind_of(attrs):
    """Classify one <layer> entry: 'tool_change', 'pause' or 'custom'.

    A tool change is any entry whose effect is "from this height, print with
    another extruder" — an authored tool change or a color change alike.
    Anything else with a G-code payload is custom; it is reported, not kept.
    """
    gcode = re.sub(r'[^a-z0-9]', '', str(attrs.get('gcode') or '').lower())
    if any(word.replace('_', '') in gcode for word in _TOOL_WORDS) or gcode == 'm600':
        return 'tool_change'
    if 'pause' in gcode:
        return 'pause'
    if gcode:
        return 'custom'
    raw_type = str(attrs.get('type') or '').strip()
    named = _NAME_KINDS.get(re.sub(r'[^a-z]', '', raw_type.lower()))
    if named:
        return named
    return _TYPE_KINDS.get(raw_type, 'custom')


def parse(xml_bytes):
    """All entries in one custom_gcode_per_layer.xml, in document order.

    Each row: plate (the plate_info id, else the plate's 1-based position),
    top_z (float or None), kind, source_extruder (int or None), color.
    Raises on malformed XML: a file that claims per-layer entries Print Handy
    cannot read must not be sliced as if it had none.
    """
    root = ET.fromstring(xml_bytes)
    rows = []
    plates = [node for node in root.iter() if node.tag.split('}')[-1] == 'plate']
    if not plates:
        plates = [root]
    for position, plate in enumerate(plates, 1):
        info = next((n for n in plate if n.tag.split('}')[-1] == 'plate_info'), None)
        plate_id = str(info.get('id')) if info is not None and info.get('id') else str(position)
        for node in plate.iter():
            if node.tag.split('}')[-1] != 'layer':
                continue
            attrs = dict(node.attrib)
            try:
                top_z = float(attrs['top_z'])
            except (KeyError, TypeError, ValueError):
                top_z = None
            try:
                extruder = int(attrs['extruder'])
            except (KeyError, TypeError, ValueError):
                extruder = None
            rows.append({'plate': plate_id, 'top_z': top_z, 'kind': kind_of(attrs),
                         'source_extruder': extruder,
                         'color': (attrs.get('color') or '').strip() or None})
    return rows


def read(path):
    """The entries of the source at `path`; [] when it carries none."""
    path = Path(path)
    if path.suffix.lower() != '.3mf':
        return []
    with zipfile.ZipFile(path) as archive:
        if MEMBER not in archive.namelist():
            return []
        return parse(archive.read(MEMBER))


def tool_changes(entries):
    return [row for row in entries if row['kind'] == 'tool_change']


def tool_change_extruders(entries):
    """Sorted source extruders the height-based tool changes switch to."""
    return sorted({row['source_extruder'] for row in tool_changes(entries)
                   if row['source_extruder']})


def _rewrite(xml_bytes, convert, z_scale=1.0):
    """Apply convert(layer_element) -> keep? to every entry; return (bytes, kept, dropped).

    A kept entry's top_z is multiplied by z_scale: when the exported geometry
    is scaled, the change heights scale with it, so a resized print keeps its
    color boundaries at the same relative heights.
    """
    root = ET.fromstring(xml_bytes)
    kept = dropped = 0
    for plate in [n for n in root.iter() if n.tag.split('}')[-1] == 'plate'] or [root]:
        for node in list(plate):
            if node.tag.split('}')[-1] != 'layer':
                continue
            if convert(node):
                if z_scale != 1.0 and node.get('top_z'):
                    try:
                        node.set('top_z', f"{float(node.get('top_z')) * z_scale:g}")
                    except ValueError:
                        pass
                kept += 1
            else:
                plate.remove(node)
                dropped += 1
    return ET.tostring(root, encoding='utf-8', xml_declaration=True), kept, dropped


def remap(xml_bytes, tool_map, z_scale=1.0):
    """Rewrite tool-change extruders through {source extruder: job tool (1-based)}.

    Entries for an extruder the job does not map are dropped rather than left
    pointing at a tool the sliced job does not have; pauses and custom
    entries are dropped too — only the height color changes are carried, and
    the caller reports the rest from the inventory. Returns (bytes, report).

    A kept entry's `color` attribute rides verbatim: it is display metadata
    (tool selection comes from the `extruder` attribute), and Orca's 3MF
    loader requires the attribute — a build that stripped it got Orca -6
    ("the input model file can not be parsed") for every height-color slice
    in real-slicer CI (PR #1133). Never strip it.
    """
    def convert(node):
        if kind_of(dict(node.attrib)) != 'tool_change':
            return False
        try:
            source = int(node.get('extruder'))
        except (TypeError, ValueError):
            return False
        tool = tool_map.get(source)
        if tool is None:
            return False
        node.set('extruder', str(tool))
        return True
    data, kept, dropped = _rewrite(xml_bytes, convert, z_scale)
    return data, {'kept': kept, 'dropped': dropped,
                  'remapped': {str(src): tool for src, tool in sorted(tool_map.items())}}


def remap_archive(source, target, tool_map, z_scale=1.0):
    """Atomically remap only per-layer tool changes in a copied 3MF archive.

    Reject an archive with a source tool lacking an explicit destination mapping;
    never silently discard a designer-authored layer change. Other ZIP members
    are copied byte-for-byte. The source is never modified.
    """
    source, target = Path(source), Path(target)
    with zipfile.ZipFile(source) as archive:
        names = archive.namelist()
        if MEMBER not in names:
            raise RuntimeError('3MF has no per-layer G-code member to remap')
        original = archive.read(MEMBER)
        entries = parse(original)
        unresolved = sorted({row['source_extruder'] for row in entries
                             if row['kind'] == 'tool_change' and row['source_extruder'] not in tool_map})
        if unresolved:
            raise RuntimeError(f'unmapped source layer tool(s): {unresolved}')
        converted, report = remap(original, tool_map, z_scale=z_scale)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + '.tmp')
        try:
            with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as out:
                for name in names:
                    out.writestr(name, converted if name == MEMBER else archive.read(name))
            # Validate output before atomic replacement.
            with zipfile.ZipFile(tmp) as check:
                if check.testzip() is not None or parse(check.read(MEMBER)) is None:
                    raise RuntimeError('remapped 3MF archive failed validation')
            tmp.replace(target)
        finally:
            if tmp.exists():
                tmp.unlink()
    return report


def as_pauses(xml_bytes, z_scale=1.0):
    """Turn every tool change into a pause at the same height (single extruder).

    PausePrint is type 1 in both the Bambu/Orca and the Prusa dialect, and a
    pause entry carries no G-code of its own: the slicer emits the printer
    profile's own pause G-code there, so the operator swaps the filament by
    hand and resumes. Returns (bytes, report).
    """
    def convert(node):
        if kind_of(dict(node.attrib)) != 'tool_change':
            return False
        node.set('type', '1')
        node.set('gcode', '')
        node.set('extra', '')
        return True
    data, kept, dropped = _rewrite(xml_bytes, convert, z_scale)
    return data, {'kept': kept, 'dropped': dropped, 'as_pauses': True}


def plate_id_for_objects(model_settings_bytes, object_ids):
    """The 1-based plate (document order in model_settings.config) whose
    instances include one of `object_ids`, as the plate_info id string the
    per-layer XML uses, or None when no plate matches."""
    wanted = {str(o) for o in object_ids}
    root = ET.fromstring(model_settings_bytes)
    for position, plate in enumerate(root.findall('plate'), 1):
        ids = {m.get('value') for m in plate.findall('model_instance/metadata')
               if m.get('key') == 'object_id'}
        if ids & wanted:
            info = next((m for m in plate.findall('metadata') if m.get('key') == 'plater_id'), None)
            return str(info.get('value')) if info is not None and info.get('value') else str(position)
    return None


def for_plate(xml_bytes, plate_id):
    """Only the <plate> block for `plate_id`, as a standalone document."""
    root = ET.fromstring(xml_bytes)
    plates = [n for n in root.iter() if n.tag.split('}')[-1] == 'plate']
    if len(plates) <= 1:
        return xml_bytes
    for position, plate in enumerate(plates, 1):
        info = next((n for n in plate if n.tag.split('}')[-1] == 'plate_info'), None)
        pid = str(info.get('id')) if info is not None and info.get('id') else str(position)
        if pid != str(plate_id):
            root.remove(plate)
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)
