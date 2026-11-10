"""Offline 3MF part/color inventory and job-bound operator decisions.

Source extruders are one based. Physical IFS slots are separate identifiers.
Never infer a slot from RGB or flatten a painted project without consent.
"""
import json
import math
import re
import posixpath
from pathlib import Path
import xml.etree.ElementTree as ET
from .part_translator import display_name
from .mesh_fast import archive_tables, MeshTable
from .compact_geometry import transform_vertices_array
import zipfile
import subprocess

# Slicer triangle painting only (Bambu/Orca paint_color, PrusaSlicer
# mmu_segmentation): the painted-region pipeline can split exactly these.
# Core 3MF materials (basematerials/colorgroup with object pid/pindex) are
# per-object author colors, read by _core_material_colors; they are not
# paint (#711). Before #711 they set painted with no palette, which asked an
# unanswerable 'painted:unknown' question and then rejected every answer.
_PAINT_TAGS = ()
_PAINT_ATTRS = (b'paint_color', b'mmu_segmentation')


def _has_paint(data):
    """Byte-level equivalent of walking every node for paint tags/attributes (bytes.find is C speed)."""
    for attr in _PAINT_ATTRS:
        i = data.find(attr + b'=')
        while i != -1:
            if data[i - 1:i] in (b' ', b':', b'\t', b'\n', b'\r'):
                return True
            i = data.find(attr + b'=', i + 1)
    return False


_FACE_COLOR_MARKS = (b' p1=', b'\tp1=', b'multiproperties', b'texture2d')

#: The note shown when core face colors print as one color (#714).
FACE_COLORS_NOTE = ('This file colors individual faces (per-triangle, multiproperties, or texture colors). '
                    'Face colors are ignored; it prints in one color.')


def _has_face_colors_stream(stream):
    """True when a core model member carries face-level color properties (#714)."""
    carry = b''
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            return False
        window = carry + chunk
        if any(mark in window for mark in _FACE_COLOR_MARKS):
            return True
        carry = window[-32:]


def _has_paint_stream(stream):
    """_has_paint over a member stream, without holding the member in memory.

    A match is accepted only from context bytes actually present in the
    scanned window, so a True here is a True for the whole member. A match
    the window truncates (its lookahead byte or its 40-byte lookbehind is
    cut off) is re-scanned in the next window: the 128-byte carry places it
    at offset >= 40 with its lookahead present, or it was already fully
    visible in the previous window. End of stream is the member's real end,
    so a match cut off there is genuinely not paint, exactly as _has_paint
    judges the full bytes.
    """
    carry = b''
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            return False
        window = carry + chunk
        if _has_paint(window):
            return True
        carry = window[-128:]


def _without_mesh_bodies(data):
    """Drop vertex/triangle payloads so structure-only parsing skips millions of elements.

    Each <mesh>...</mesh> becomes <mesh/>, so mesh presence is preserved exactly.
    """
    if b'<mesh' not in data or b':mesh' in data:
        return data
    out, pos = [], 0
    while True:
        start = data.find(b'<mesh', pos)
        if start == -1:
            break
        end = data.find(b'</mesh>', start)
        if end == -1:
            return data
        out += [data[pos:start], b'<mesh/>']
        pos = end + len(b'</mesh>')
    out.append(data[pos:])
    return b''.join(out)


def cutter_ids(archive):
    """{(object_id, part_id): subtype} for Bambu/Orca non-printing volumes.

    negative_part subtracts from its object at slice time; modifier_part,
    support_blocker and support_enforcer only change settings. None of them is
    a printed body, so none may be asked about or exported as a solid.
    """
    if 'Metadata/model_settings.config' not in archive.namelist():
        return {}
    root = ET.fromstring(archive.read('Metadata/model_settings.config'))
    out = {}
    for obj in root.findall('object'):
        for part in obj.findall('part'):
            subtype = part.get('subtype') or 'normal_part'
            if subtype != 'normal_part':
                out[(obj.get('id'), part.get('id'))] = subtype
    return out


def component_parts(archive):
    """Resolve build instances to mesh leaves, preserving repeated instances."""
    models = {}
    for name in archive.namelist():
        if not name.lower().endswith('.model'):
            continue
        if name in models:
            raise RuntimeError('duplicate 3MF model path')
        root = ET.fromstring(_without_mesh_bodies(archive.read(name)))
        objects = {}
        for node in root.iter():
            if node.tag.split('}')[-1] == 'object':
                oid = node.get('id')
                if not oid or oid in objects:
                    raise RuntimeError('duplicate or missing 3MF object ID')
                objects[oid] = node
        models[name] = (root, objects)
    if '3D/3dmodel.model' not in models:
        raise RuntimeError('unresolved 3MF root model')
    leaves = []

    def visit(name, oid, trail, key):
        identity = (name, oid)
        if identity in trail:
            raise RuntimeError('cyclic 3MF component reference')
        if name not in models or oid not in models[name][1]:
            raise RuntimeError(f'unresolved 3MF component reference: {name} object {oid}')
        obj = models[name][1][oid]
        components = [n for n in obj.iter() if n.tag.split('}')[-1] == 'component']
        mesh = any(n.tag.split('}')[-1] == 'mesh' for n in obj)
        if components and mesh:
            raise RuntimeError('ambiguous 3MF object has mesh and components')
        if components:
            for i, node in enumerate(components):
                target = next((v for k, v in node.attrib.items() if k.split('}')[-1] == 'path'), None)
                target = (target.lstrip('/') if target.startswith('/') else
                          posixpath.normpath(posixpath.join(posixpath.dirname(name), target))) if target else name
                visit(target, node.get('objectid'), trail + [identity], f'{key}/{i}')
        elif mesh:
            leaves.append({'key': key, 'instance_key': key, 'object_id': oid, 'part_id': oid,
                           'model_path': name, 'ancestry': trail + [identity],
                           'name': obj.get('name') or f'Object {oid}',
                           'source_extruder': None, 'status': 'planned'})
        else:
            raise RuntimeError(f'unresolved 3MF geometry: {name} object {oid}')

    root = models['3D/3dmodel.model'][0]
    builds = [n for n in root if n.tag.split('}')[-1] == 'build']
    items = [n for build in builds for n in build if n.tag.split('}')[-1] == 'item']
    if len(builds) != 1 or not items:
        raise RuntimeError('unresolved 3MF build inventory')
    for i, item in enumerate(items):
        target = next((v for k, v in item.attrib.items() if k.split('}')[-1] == 'path'), None)
        name = target.lstrip('/') if target and target.startswith('/') else posixpath.normpath(posixpath.join('3D', target)) if target else '3D/3dmodel.model'
        visit(name, item.get('objectid'), [], f'build:{i}')
    return leaves


def _apply_plate_names(archive, names, parts):
    """Bambu exports often name every object after one STL; the author's plate names say what each piece is.

    Only parts whose names collide get renamed, and only when the object sits on a named plate.
    """
    if 'Metadata/model_settings.config' not in names:
        return
    root = ET.fromstring(archive.read('Metadata/model_settings.config'))
    plate_of = {}
    for plate in root.iter('plate'):
        meta = [(m.get('key'), m.get('value')) for m in plate.iter('metadata')]
        label = next((v for k, v in meta if k == 'plater_name' and v and v.strip()), None)
        if label:
            for k, v in meta:
                if k == 'object_id':
                    plate_of[v] = label.strip()
    counts = {}
    for part in parts:
        counts[part['name']] = counts.get(part['name'], 0) + 1
    for part in parts:
        label = plate_of.get(part['object_id'])
        if label and counts[part['name']] > 1:
            part['source_name'] = part['name']
            part['name'] = label


def _annotate_parts(archive, names, parts, filaments):
    """Give each part what a person needs to recognise it: real size, plate and the author's color.

    Best effort and display only; a failure here never blocks intake.
    """
    try:
        # The streaming reader's retained structure carries objects,
        # components and build items without materialising mesh elements;
        # files it declines keep the reference parse of the raw member.
        data, _fast = archive_tables(archive, '3D/3dmodel.model')
        root = ET.fromstring(data)
        objects = {n.get('id'): n for n in root.iter() if n.tag.split('}')[-1] == 'object'}
        items = {n.get('objectid'): n for n in root.iter() if n.tag.split('}')[-1] == 'item'}
        cache = {}

        def mat(node):
            v = [float(x) for x in (node.get('transform') or '1 0 0 0 1 0 0 0 1 0 0 0').split()]
            return v if len(v) == 12 else [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]

        def apply(m, x, y, z):
            return (x * m[0] + y * m[3] + z * m[6] + m[9], x * m[1] + y * m[4] + z * m[7] + m[10],
                    x * m[2] + y * m[5] + z * m[8] + m[11])

        for part in parts:
            obj = objects.get(part.get('object_id'))
            if obj is None:
                continue
            comps = [c for c in obj.iter() if c.tag.split('}')[-1] == 'component']
            want = [c for c in comps if c.get('objectid') == part.get('part_id')] or comps
            item = items.get(part['object_id'])
            item_m = None
            if item is not None:
                # Rotation/scale only: translation does not change size.
                item_m = mat(item)[:9] + [0, 0, 0]
            lo = hi = None
            for comp in want:
                path_attr = next((v for k, v in comp.attrib.items() if k.split('}')[-1] == 'path'), None)
                mpath = path_attr.lstrip('/') if path_attr else '3D/3dmodel.model'
                if mpath not in cache:
                    data, fast = archive_tables(archive, mpath)
                    if fast is not None:
                        cache[mpath] = fast
                if mpath not in cache:
                    mroot = ET.fromstring(data)
                    cache[mpath] = {o.get('id'): [(float(v.get('x')), float(v.get('y')), float(v.get('z')))
                                                  for v in o.iter() if v.tag.split('}')[-1] == 'vertex']
                                    for o in mroot.iter() if o.tag.split('}')[-1] == 'object'}
                entry = cache[mpath].get(comp.get('objectid'))
                if entry is None:
                    continue
                m = mat(comp)
                chain = [m] if item_m is None else [m, item_m]
                if isinstance(entry, MeshTable):
                    if not len(entry.vertices):
                        continue
                    # Same per-point operation order as apply(), on the whole
                    # vertex array; min/max selection is exact.
                    arr = transform_vertices_array(entry.vertices, chain, apply)
                    c_lo, c_hi = arr.min(axis=0), arr.max(axis=0)
                    if lo is None:
                        lo, hi = c_lo.tolist(), c_hi.tolist()
                    else:
                        for axis in range(3):
                            lo[axis] = min(lo[axis], float(c_lo[axis]))
                            hi[axis] = max(hi[axis], float(c_hi[axis]))
                else:
                    for pt in entry:
                        p = apply(m, *pt)
                        if item_m is not None:
                            p = apply(item_m, *p)
                        if lo is None:
                            lo, hi = list(p), list(p)
                        else:
                            for axis in range(3):
                                if p[axis] < lo[axis]:
                                    lo[axis] = p[axis]
                                if p[axis] > hi[axis]:
                                    hi[axis] = p[axis]
            if lo is not None:
                part['bounds_mm'] = [round(hi[axis] - lo[axis], 1) for axis in range(3)]
        if 'Metadata/model_settings.config' in names:
            settings = ET.fromstring(archive.read('Metadata/model_settings.config'))
            plate_of = {}
            for number, plate in enumerate(settings.findall('plate'), 1):
                for inst in plate.findall('model_instance'):
                    md = {m.get('key'): m.get('value') for m in inst.findall('metadata')}
                    plate_of[md.get('object_id')] = number
            if len(set(plate_of.values())) > 1:
                for part in parts:
                    if part.get('object_id') in plate_of:
                        part['plate'] = plate_of[part['object_id']]
        palette = {f.get('source_extruder'): f.get('color') for f in filaments}
        if len({c for c in palette.values() if c}) > 1:
            for part in parts:
                color = palette.get(part.get('source_extruder'))
                if color:
                    part['author_color'] = color
        _core_material_colors(root, objects, parts)
    except Exception:
        pass


def _core_material_colors(root, objects, parts):
    """Author colors from core 3MF materials (#711): object pid/pindex into basematerials/colorgroup.

    Plain 3MF exports (Blender, Fusion, generators) carry per-object colors
    here instead of a slicer palette. Display only; a part that already has a
    slicer palette color keeps it.
    """
    groups = {}
    for node in root.iter():
        tag = node.tag.split('}')[-1]
        if tag in ('basematerials', 'colorgroup'):
            attr = 'displaycolor' if tag == 'basematerials' else 'color'
            groups[node.get('id')] = [str(c.get(attr) or '')[:7] for c in node
                                      if c.tag.split('}')[-1] in ('base', 'color')]
    if not groups:
        return

    def color_of(obj):
        if obj is None or obj.get('pid') not in groups:
            return None
        try:
            value = groups[obj.get('pid')][int(obj.get('pindex') or 0)]
        except (ValueError, IndexError):
            return None
        return value if re.fullmatch(r'#[0-9A-Fa-f]{6}', value) else None

    found = {}
    for part in parts:
        if part.get('author_color'):
            continue
        color = color_of(objects.get(part.get('part_id'))) or color_of(objects.get(part.get('object_id')))
        if color:
            found[id(part)] = color
    if len(set(found.values())) > 1:  # one color everywhere tells the operator nothing
        for part in parts:
            if id(part) in found:
                part['author_color'] = found[id(part)]


def _prusa_palette(config_bytes):
    """Filament palette from a PrusaSlicer Metadata/Slic3r_PE.config (#717)."""
    values = {}
    for line in config_bytes.decode('utf-8', 'replace').splitlines():
        line = line.strip()
        if not line.startswith(';') or '=' not in line:
            continue
        key, _, value = line[1:].partition('=')
        values[key.strip()] = value.strip()

    def split(key):
        raw = values.get(key, '')
        return [item.strip().strip('"') for item in raw.split(';')] if raw else []

    extruder, filament, materials = split('extruder_colour'), split('filament_colour'), split('filament_type')
    count = max(len(extruder), len(filament), len(materials))
    palette = []
    for i in range(count):
        color = (extruder[i] if i < len(extruder) else '') or (filament[i] if i < len(filament) else '')
        if color and not re.fullmatch(r'#[0-9A-Fa-f]{6}', color):
            color = ''
        palette.append({'source_extruder': i + 1, 'color': color.upper() or None,
                        'material': (materials[i] if i < len(materials) else None) or None})
    return palette


# Metadata keys that describe a volume itself, never a setting override.
_STRUCTURAL_META = frozenset(('name', 'extruder', 'matrix', 'source_file', 'source_object_id',
                              'source_volume_id', 'source_offset_x', 'source_offset_y',
                              'source_offset_z'))


def _attach_part_settings(archive, names, parts, painted):
    """Carry each part's own author settings onto its inventory row (#867).

    Bambu/Orca model_settings.config metadata beyond the structural keys is the
    part's settings, keyed by (object, part). A PrusaSlicer Slic3r_PE_model.config
    names settings per object and per volume: volumes match the object's leaves in
    component order (volume settings apply only when the counts agree, so a volume
    is never matched to the wrong mesh), the config's extruder assignment is the
    part's source extruder (an unpainted file names it nowhere else), and its
    settings arrive translated to Orca names. A row only gains a 'settings' entry
    when it carries any.
    """
    if 'Metadata/model_settings.config' in names:
        root = ET.fromstring(archive.read('Metadata/model_settings.config'))

        def meta_of(node):
            return {m.get('key'): m.get('value') for m in node.findall('metadata')
                    if m.get('key') is not None and m.get('value') is not None}

        lookup = {}
        for obj in root.findall('object'):
            # Object-level metadata is an override for every part of the object;
            # a part's own entry wins over it, exactly as Orca applies them.
            object_settings = {k: v for k, v in meta_of(obj).items() if k not in _STRUCTURAL_META}
            for part in obj.findall('part') or [obj]:
                settings = {**object_settings,
                            **{k: v for k, v in meta_of(part).items() if k not in _STRUCTURAL_META}}
                if settings:
                    lookup[(obj.get('id'), part.get('id'))] = settings
        for part in parts:
            settings = lookup.get((str(part.get('object_id')), str(part.get('part_id'))))
            if settings:
                part['settings'] = settings
        return
    if 'Metadata/Slic3r_PE_model.config' not in names:
        return
    from . import author_settings
    parsed = author_settings.parse_prusa_model(archive.read('Metadata/Slic3r_PE_model.config'))
    if not parsed:
        return
    groups = {}
    for part in parts:
        ancestry = part.get('ancestry') or []
        wrapper = ancestry[0][1] if len(ancestry) > 1 else part.get('object_id')
        # The same object can be placed by several build items (instances);
        # each instance's rows are matched against the config's volumes
        # separately, or a two-volume object placed twice would present four
        # rows against two volumes and lose every volume setting (#876).
        instance = str(part.get('key') or '').split('/', 1)[0]
        groups.setdefault((str(wrapper), instance), []).append(part)
    for (wrapper, _instance), rows in groups.items():
        entry = parsed.get(wrapper)
        if not entry:
            continue
        volumes = entry['volumes']
        matched = len(volumes) == len(rows)
        for index, part in enumerate(rows):
            volume = volumes[index] if matched else {}
            settings = {**entry['settings'], **(volume.get('settings') or {})}
            if settings:
                part['settings'] = settings
            if not painted:
                extruder = volume.get('extruder') or entry['extruder']
                if extruder:
                    part['source_extruder'] = extruder


def inventory(path):
    from .intake_resources import check_source
    path = Path(path)
    check_source(path)
    if path.suffix.lower() != '.3mf':
        # Non-3MF sources have no palette, but every decision is still bound to the file bytes.
        res = {'source_sha256': __import__('hashlib').sha256(path.read_bytes()).hexdigest(),
               'parts': [], 'filaments': [], 'painted': False, 'requires_choices': False,
               'layer_changes': [], 'layer_extras': [], 'plate_usage': {}}
        _append_filament_stats(path, res)
        return res
    parts, filaments, painted, cutters = [], [], False, []
    face_colors = False
    layer_entries = []
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if 'Metadata/project_settings.config' in names:
            project = json.loads(archive.read('Metadata/project_settings.config'))
            colors, materials = project.get('filament_colour', []), project.get('filament_type', [])
            if not isinstance(colors, list) or not isinstance(materials, list):
                raise RuntimeError('invalid 3MF filament palette')
            filaments = [{'source_extruder': i + 1, 'color': colors[i] if i < len(colors) else None,
                          'material': materials[i] if i < len(materials) else None}
                         for i in range(max(len(colors), len(materials)))]
        elif 'Metadata/Slic3r_PE.config' in names:
            # PrusaSlicer MMU project (#717): '; key = value' lines, values
            # separated by ';'. extruder_colour wins per extruder; an empty
            # entry falls back to that extruder's filament_colour.
            filaments = _prusa_palette(archive.read('Metadata/Slic3r_PE.config'))
        if 'Metadata/model_settings.config' in names:
            root = ET.fromstring(archive.read('Metadata/model_settings.config'))
            for obj in root.findall('object'):
                meta = {m.get('key'): m.get('value') for m in obj.findall('metadata')}
                children = obj.findall('part') or [obj]
                for part in children:
                    pm = {m.get('key'): m.get('value') for m in part.findall('metadata')}
                    raw = pm.get('extruder', meta.get('extruder'))
                    if raw == '0' and part is not obj:
                        # Bambu/Orca write extruder 0 on a volume to mean "inherit the
                        # object's filament" (common on modifier parts); it is not invalid.
                        raw = meta.get('extruder')
                    if raw is not None and (not raw.isdigit() or int(raw) < 1):
                        raise RuntimeError('invalid source extruder assignment')
                    parts.append({'key': f"{obj.get('id')}:{part.get('id')}",
                                  'object_id': obj.get('id'), 'part_id': part.get('id'),
                                  'name': pm.get('name') or meta.get('name') or f"Object {obj.get('id')}",
                                  'object_name': meta.get('name'),
                                  'subtype': part.get('subtype') or 'normal_part',
                                  'source_extruder': int(raw) if raw else None, 'status': 'planned'})
        for name in names:
            if name.lower().endswith('.model'):
                # Same test as walking every node, without building a million-element tree
                # or holding the whole member in memory.
                with archive.open(name) as member:
                    painted |= _has_paint_stream(member)
        if not painted and 'Metadata/model_settings.config' not in names and '3D/3dmodel.model' in names:
            # Core per-triangle colors (p1/p2/p3), multiproperties, and
            # texture2d (#714) are not slicer paint and cannot be split into
            # plates; the job prints in one color and says so.
            with archive.open('3D/3dmodel.model') as member:
                face_colors = _has_face_colors_stream(member)
        resolved = component_parts(archive)
        _apply_plate_names(archive, names, parts)
        if not parts:
            parts = resolved
        else:
            keys = [part['key'] for part in parts]
            if len(set(keys)) != len(keys):
                raise RuntimeError('duplicate 3MF metadata part selection key')
            mapped, covered = [], set()
            for part in parts:
                oid, pid = part['object_id'], part['part_id']
                matches = [leaf for leaf in resolved
                           if ('3D/3dmodel.model', oid) in leaf['ancestry']
                           and leaf['object_id'] == pid]
                identities = {(leaf['model_path'], leaf['object_id']) for leaf in matches}
                if not oid or not pid or len(identities) != 1:
                    raise RuntimeError('3MF metadata parts contradict resolved build identities')
                for leaf in matches:
                    if leaf['key'] in covered:
                        raise RuntimeError('duplicate 3MF metadata mapping to build part')
                    covered.add(leaf['key'])
                    mapped.append({**leaf, **part, 'key': part['key'] if len(matches) == 1
                                   else f"{part['key']}@{leaf['key']}"})
            if covered != {leaf['key'] for leaf in resolved}:
                raise RuntimeError('3MF metadata parts contradict resolved build inventory')
            parts = mapped
        _attach_part_settings(archive, names, parts, painted)
        # Negative parts and modifiers are cutters/settings volumes, not printed
        # bodies: never a color question, never a separate printed object.
        _annotate_parts(archive, names, parts, filaments)
        cutters = [p for p in parts if p.get('subtype', 'normal_part') != 'normal_part']
        parts = [p for p in parts if p.get('subtype', 'normal_part') == 'normal_part']
        if not parts:
            raise RuntimeError('unresolved 3MF part inventory')
        if 'Metadata/Slic3r_PE.config' in names and 'Metadata/model_settings.config' not in names:
            # PrusaSlicer (#717): an object with no per-object extruder prints
            # with extruder 1, so unpainted triangles map to filament 1.
            for part in parts:
                if part.get('source_extruder') is None:
                    part['source_extruder'] = 1
        # Height-based color changes (#870): per-layer tool changes are colors
        # in their own right. An extruder they switch to that the palette does
        # not name joins the palette with the color the entry itself carries,
        # so the change can be asked about like any other source color.
        from . import layer_gcode
        if layer_gcode.MEMBER in names:
            layer_entries = layer_gcode.parse(archive.read(layer_gcode.MEMBER))
        known = {f.get('source_extruder') for f in filaments}
        for row in layer_gcode.tool_changes(layer_entries):
            ext = row['source_extruder']
            if ext and ext not in known:
                known.add(ext)
                filaments.append({'source_extruder': ext, 'color': row['color'], 'material': None})
    layer_changes = [{'plate': row['plate'], 'top_z': row['top_z'],
                      'source_extruder': row['source_extruder'], 'color': row['color']}
                     for row in layer_gcode.tool_changes(layer_entries)]
    layer_extras = [{'plate': row['plate'], 'top_z': row['top_z'], 'kind': row['kind']}
                    for row in layer_entries if row['kind'] != 'tool_change']
    res = {'source_sha256': __import__('hashlib').sha256(path.read_bytes()).hexdigest(),
           'parts': parts, 'cutters': cutters, 'filaments': filaments, 'painted': painted,
           'face_colors_ignored': face_colors,
           'layer_changes': layer_changes, 'layer_extras': layer_extras,
           'requires_choices': painted or len(parts) > 1 or len(filaments) > 1 or bool(layer_changes)}
    # A dedicated support filament (#867): the author named a filament for the
    # supports (and optionally another for the interface) that no part prints
    # with. Recorded only when the designation names a real palette filament.
    from . import author_settings
    designation = author_settings.support_designation(path)
    support = {field: (ext if ext and 1 <= ext <= len(filaments) else None)
               for field, ext in designation.items()}
    if support['base'] or support['interface']:
        res['support_filaments'] = support
    _append_filament_stats(path, res)
    res['plate_usage'] = _plate_usage(path, res)
    return res


USAGE_ABSENT = 'source has no slicer usage metadata'
USAGE_NO_RECORDS = 'plate has no validated filament usage records'
USAGE_UNMAPPED = 'source filament usage record names a filament outside this project'
USAGE_INVALID = 'invalid source filament usage record'
USAGE_UNREADABLE = 'source slicer usage metadata could not be read'
# Unknown, but not a claim that failed: a project sliced for only some of its
# plates has no records for the rest, and a filament used only for supports
# need not appear in the source palette. The slicer's own output is
# authoritative for these, so they never refuse a slice.
_USAGE_NOT_A_FAILED_CLAIM = frozenset({USAGE_ABSENT, USAGE_NO_RECORDS, USAGE_UNMAPPED})


def _usage_claim_present(usage):
    """Whether a plate's usage record is a claim the gate must validate."""
    if not isinstance(usage, dict):
        return True
    if usage.get('usage_known') is False:
        return usage.get('usage_unknown_reason') not in _USAGE_NOT_A_FAILED_CLAIM
    return usage.get('extruders') != {}


def validate_plate_usage_if_present(manifest, selected_plate_ids=None):
    """Filament warnings for damaged source slicer usage claims, or [].

    Absence is UNKNOWN, not a warning: a plate with no records stays UNKNOWN
    and the destination slicer's output is authoritative. Only damaged or
    invalid claims warn — an unreadable usage file, or a record with bad
    numbers or a bad filament ID. Owner ruling (catduck, 2026-11-10): a
    filament condition never hard-blocks a print; it warns and the operator
    may accept it. require_plate_usage_known keeps the strict refusal for
    callers that ask for it directly; this gate converts its per-plate
    refusals into warning texts.
    """
    stats = manifest.get('filament_stats')
    plates = stats.get('plates') if isinstance(stats, dict) else None
    if not isinstance(plates, dict) or not plates:
        return []  # No source claim. Destination slicing supplies authoritative usage.
    wanted = (list(plates) if selected_plate_ids is None
              else [str(plate_id) for plate_id in selected_plate_ids])
    warnings = []
    for plate_id in wanted:
        if plate_id not in plates or not _usage_claim_present(plates[plate_id]):
            continue
        try:
            require_plate_usage_known(manifest, [plate_id])
        except RuntimeError as exc:
            warnings.append(str(exc).removesuffix('; no slicing attempted'))
    return warnings


def require_plate_usage_known(manifest, selected_plate_ids=None):
    """Refuse readiness when selected plates lack validated usage records.

    If the caller has not resolved plate selection, validate every source
    plate conservatively. Never turn a missing plate record into zero usage.
    """
    plates = ((manifest.get('filament_stats') or {}).get('plates'))
    if not isinstance(plates, dict) or not plates:
        raise RuntimeError('per-plate filament usage is unknown; no slicing attempted')
    selected = (list(plates) if selected_plate_ids is None
                else [str(plate_id) for plate_id in selected_plate_ids])
    for plate_id in selected:
        usage = plates.get(str(plate_id))
        if not isinstance(usage, dict) or usage.get('usage_known') is not True:
            reason = usage.get('usage_unknown_reason') if isinstance(usage, dict) else None
            detail = f': {reason}' if reason else ''
            raise RuntimeError(f'per-plate filament usage is unknown for plate {plate_id}{detail}; no slicing attempted')
        if not isinstance(usage.get('extruders'), dict) or not usage['extruders']:
            raise RuntimeError(f'per-plate filament usage is unknown for plate {plate_id}: no validated filament assignments; no slicing attempted')
        for source_id, amounts in usage['extruders'].items():
            try:
                ext_id = int(source_id)
                grams, meters = float(amounts['g']), float(amounts['m'])
            except (TypeError, ValueError, KeyError):
                raise RuntimeError(f'per-plate filament usage is unknown for plate {plate_id}: invalid assignment; no slicing attempted') from None
            if ext_id < 1 or not math.isfinite(grams) or not math.isfinite(meters) or grams < 0 or meters < 0:
                raise RuntimeError(f'per-plate filament usage is unknown for plate {plate_id}: invalid assignment; no slicing attempted')


# ---------------------------------------------------------------------------
# Per-plate filament usage analysis (#1110 Phase B).
#
# The slicer-reported totals above say how much of each filament a plate
# consumed, but not WHY: which assignments make the plate use that filament.
# This analysis derives each plate's used source filaments from the source
# itself, one usage class at a time, and every class carries its evidence.
# A class is 'known' when the source determines it, 'unknown' when the source
# should carry the fact but it cannot be determined (never guessed, never
# silently zero), and 'not_carried_by_format' when the source format names no
# such assignment at all — a named limitation, not a hidden filament. Logical
# source filament IDs are retained end to end; loaded slots and physical
# nozzles are destination concepts and never appear in a usage record.

_USAGE_CLASSES = ('objects', 'painting', 'layer_changes', 'height_ranges',
                  'wall', 'infill', 'support', 'interface', 'tower')

# Prompt label for a filament a plate uses only through one usage class.
_CLASS_LABELS = {'height_ranges': 'height-range', 'wall': 'wall', 'infill': 'infill',
                 'tower': 'prime tower', 'painting': 'painted-region',
                 'layer_changes': 'layer-change', 'support': 'support',
                 'interface': 'support interface', 'objects': 'object'}


def _usage_row(status, filaments=(), evidence=None, reason=None):
    row = {'status': status, 'source_filaments': sorted({int(f) for f in filaments if f}),
           'evidence': evidence}
    if reason:
        row['reason'] = reason
    return row


def _plate_membership(path, manifest):
    """({plate_id: [part, ...]}, {object_id: plate_id}) from the source's own plate assignments.

    Prefers model_job.plate_manifest (plater_id identity, the same identity
    slice_info usage records use); falls back to the inventory's per-part
    plate annotation, then to one implicit plate holding every part.
    """
    parts = list(manifest.get('parts') or [])
    try:
        from . import model_job as _mj
        plates = _mj.plate_manifest(path)['plates']
    except Exception:
        plates = None
    if plates:
        object_plate = {str(oid): str(plate['id']) for plate in plates for oid in plate['object_ids']}
        grouped = {str(plate['id']): [] for plate in plates}
        for part in parts:
            plate_id = object_plate.get(str(part.get('object_id')))
            if plate_id is not None:
                grouped[plate_id].append(part)
        return grouped, object_plate
    if any(part.get('plate') is not None for part in parts):
        grouped, object_plate = {}, {}
        for part in parts:
            if part.get('plate') is None:
                continue
            plate_id = str(part['plate'])
            grouped.setdefault(plate_id, []).append(part)
            object_plate[str(part.get('object_id'))] = plate_id
        return grouped, object_plate
    return ({'1': parts} if parts else {}), {str(p.get('object_id')): '1' for p in parts}


def _prusa_role_extruders(path):
    """Per-feature extruders a PrusaSlicer config names ({} for other formats).

    perimeter_extruder / infill_extruder / solid_infill_extruder are the
    PrusaSlicer spellings of the wall and infill assignments; 0 or an absent
    key means the object's own extruder, which is no distinct assignment.
    """
    out = {}
    try:
        with zipfile.ZipFile(path) as archive:
            if 'Metadata/Slic3r_PE.config' not in archive.namelist():
                return out
            data = archive.read('Metadata/Slic3r_PE.config').decode('utf-8', 'replace')
    except Exception:
        return out
    values = {}
    for line in data.splitlines():
        line = line.strip()
        if line.startswith(';') and '=' in line:
            key, _, value = line[1:].partition('=')
            values[key.strip()] = value.strip()
    for key in ('perimeter_extruder', 'infill_extruder', 'solid_infill_extruder'):
        try:
            number = int(str(values.get(key) or '').strip())
        except ValueError:
            number = None
        if number and number > 0:
            out[key] = number
    return out


def _height_range_extruders(path, object_plate):
    """({plate_id: set(extruders)}, unknown_reason) from layer_config_ranges.xml.

    A height range with opt_key 'extruder' assigns a filament to an object's
    layers between min_z and max_z. An absent member is known-empty; a member
    that cannot be parsed, or a range whose extruder value is not a valid
    source filament ID, makes the class unknown — never absent.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            if 'Metadata/layer_config_ranges.xml' not in archive.namelist():
                return {}, None
            data = archive.read('Metadata/layer_config_ranges.xml')
    except Exception as exc:
        return {}, f'layer_config_ranges.xml could not be read: {exc}'
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return {}, 'layer_config_ranges.xml could not be parsed'
    out = {}
    for obj in root.iter():
        if obj.tag.split('}')[-1] != 'object':
            continue
        plate_id = object_plate.get(str(obj.get('id')))
        if plate_id is None:
            continue
        for option in obj.iter():
            if option.tag.split('}')[-1] != 'option' or option.get('opt_key') != 'extruder':
                continue
            try:
                extruder = int(str(option.text or '').strip())
                if extruder < 1:
                    raise ValueError
            except ValueError:
                return {}, (f"height range for object {obj.get('id')} names an invalid "
                            f"extruder {option.text!r}")
            out.setdefault(plate_id, set()).add(extruder)
    return out, None


def _painting_usage(path, manifest, plate_parts):
    """The painting class row for one plate's parts (painted_geometry states).

    State N >= 1 is source filament N; state 0 and unpainted triangles print
    the part's base extruder. PrusaSlicer mmu_segmentation paint is decoded
    per state by painted_geometry with the same TriangleSelector decoding as
    paint_color (Phase C, #1110 criterion 4), so its states count exactly.
    """
    if not manifest.get('painted'):
        return _usage_row('known', evidence='source carries no paint_color/mmu_segmentation attributes')
    from . import painted_geometry
    used, mmu = set(), False
    model_bytes = {}
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            for part in plate_parts:
                states = painted_geometry.leaf_states(path, part)
                base = part.get('source_extruder')
                needs_base = bool(states.get('unpainted')) or 0 in (states.get('states') or {})
                for state, count in (states.get('states') or {}).items():
                    if count:
                        used.add(int(state) if state else base)
                if needs_base:
                    used.add(base)
                model_path = part['ancestry'][-1][0] if part.get('ancestry') else '3D/3dmodel.model'
                if model_path in names and model_path not in model_bytes:
                    model_bytes[model_path] = archive.read(model_path)
                if b'mmu_segmentation=' in model_bytes.get(model_path, b''):
                    mmu = True
    except Exception as exc:
        return _usage_row('unknown', reason=f'paint states could not be decoded: {exc}')
    if None in used:
        return _usage_row('unknown', reason='a painted part has no base extruder for its '
                                             'unpainted or base-state triangles')
    if mmu:
        return _usage_row('known', used, evidence='mmu_segmentation triangle states decoded per '
                                                  'part (same selector decoding as paint_color)')
    return _usage_row('known', used, evidence='paint_color triangle states decoded per part')


def _plate_usage(path, manifest):
    """{plate_id: usage entry} for a 3MF source; {} when it has no plates/parts.

    Merges the source analysis with the slicer-reported per-plate totals in
    filament_stats: where both exist and name different filament sets, the
    plate's usage is UNKNOWN with the disagreement recorded — neither claim
    is dropped or averaged. See the section comment above for class status
    semantics.
    """
    parts = manifest.get('parts') or []
    if not parts:
        return {}
    grouped, object_plate = _plate_membership(path, manifest)
    if not grouped:
        return {}
    designated = manifest.get('support_filaments') or {}
    # The recorded support_filaments are clamped to the palette when the
    # manifest is built (an out-of-palette designation records as None),
    # which would erase exactly the designations the invalid-designation
    # check below exists for. Read the raw designations from the source
    # project as well; range-checking them against the palette is this
    # function's job (see author_settings.support_designation).
    from . import author_settings
    raw_designation = author_settings.support_designation(path)
    designated = {field: (raw_designation.get(field)
                          if raw_designation.get(field) is not None
                          else designated.get(field))
                  for field in ('base', 'interface')}
    prusa_roles = _prusa_role_extruders(path)
    range_extruders, range_unknown = _height_range_extruders(path, object_plate)
    palette_ids = {int(row['source_extruder']) for row in manifest.get('filaments') or []
                   if row.get('source_extruder') is not None}
    invalid_designations = {name: ext for name, ext in designated.items()
                            if ext is not None and ext not in palette_ids}
    stats_plates = ((manifest.get('filament_stats') or {}).get('plates')) or {}
    unreadable = 'unreadable' in stats_plates
    out = {}
    for plate_id, plate_parts in grouped.items():
        classes = {}
        # Object/part assignments.
        assigned = {p.get('source_extruder') for p in plate_parts}
        if None in assigned:
            classes['objects'] = _usage_row(
                'unknown', reason='a part on this plate has no source extruder assignment')
        else:
            classes['objects'] = _usage_row(
                'known', assigned,
                evidence='object/part extruder assignments in the source model settings '
                         '(Metadata/model_settings.config or Metadata/Slic3r_PE_model.config)')
        # Triangle painting.
        classes['painting'] = _painting_usage(path, manifest, plate_parts)
        # Layer changes (per-layer tool changes pinned to this plate).
        layer_rows = [r for r in manifest.get('layer_changes') or []
                      if str(r.get('plate')) == str(plate_id)]
        if any(r.get('source_extruder') is None for r in layer_rows):
            classes['layer_changes'] = _usage_row(
                'unknown', reason='a per-layer tool change on this plate names no extruder')
        else:
            classes['layer_changes'] = _usage_row(
                'known', {r.get('source_extruder') for r in layer_rows},
                evidence='Metadata/custom_gcode_per_layer.xml tool changes for this plate')
        # Height ranges.
        if range_unknown:
            classes['height_ranges'] = _usage_row('unknown', reason=range_unknown)
        else:
            classes['height_ranges'] = _usage_row(
                'known', range_extruders.get(str(plate_id), ()),
                evidence='Metadata/layer_config_ranges.xml extruder ranges for this plate')
        # Wall / infill: only the PrusaSlicer format names per-feature
        # extruders; a Bambu/Orca project carries no distinct assignment.
        if prusa_roles:
            wall = {prusa_roles['perimeter_extruder']} if 'perimeter_extruder' in prusa_roles else set()
            infill = {prusa_roles[k] for k in ('infill_extruder', 'solid_infill_extruder')
                      if k in prusa_roles}
            classes['wall'] = _usage_row('known', wall or (assigned - {None}),
                                         evidence='perimeter_extruder in Metadata/Slic3r_PE.config')
            classes['infill'] = _usage_row('known', infill or (assigned - {None}),
                                           evidence='infill_extruder in Metadata/Slic3r_PE.config')
        else:
            classes['wall'] = _usage_row(
                'not_carried_by_format',
                reason='the source project format names no separate wall filament; walls print '
                       'with the object filament and no distinct wall assignment is carried')
            classes['infill'] = _usage_row(
                'not_carried_by_format',
                reason='the source project format names no separate infill filament; infill prints '
                       'with the object filament and no distinct infill assignment is carried')
        # Support / interface: an invalid explicit designation is an unknown
        # usage claim, not permission to silently omit that filament.
        base, interface = designated.get('base'), designated.get('interface')
        invalid_here = {name: ext for name, ext in invalid_designations.items()
                        if (name == 'base' and base is not None) or
                           (name == 'interface' and interface is not None)}
        if invalid_here:
            reason = 'support designation names source filament outside project palette: ' + \
                     ', '.join(f'{name}={ext}' for name, ext in sorted(invalid_here.items()))
            classes['support'] = _usage_row('unknown', reason=reason)
            classes['interface'] = _usage_row('unknown', reason=reason)
        elif base:
            classes['support'] = _usage_row(
                'known', {base}, evidence='support filament designated in the source project settings')
            classes['interface'] = _usage_row(
                'known', {interface or base},
                evidence='support interface filament designated in the source project settings'
                if interface else 'support interface prints with the designated support filament')
        else:
            classes['support'] = _usage_row(
                'known', evidence='no dedicated support filament designated; supports print '
                                  'with the object filament')
            classes['interface'] = _usage_row(
                'known', evidence='no dedicated support interface filament designated; the interface '
                                  'prints with the object filament')
        # Tower: neither source format names a distinct tower filament.
        classes['tower'] = _usage_row(
            'not_carried_by_format',
            reason='the source project format names no wipe/prime tower filament; the tower prints '
                   'with the filaments in play and no distinct tower assignment is carried')
        used, filament_classes = set(), {}
        for name in _USAGE_CLASSES:
            row = classes[name]
            if row['status'] == 'known':
                for ext in row['source_filaments']:
                    used.add(ext)
                    filament_classes.setdefault(str(ext), []).append(name)
        unknown = [name for name in _USAGE_CLASSES if classes[name]['status'] == 'unknown']
        entry = {'plate': str(plate_id), 'part_keys': [p['key'] for p in plate_parts],
                 'classes': classes, 'source_filaments': sorted(used),
                 'filament_classes': filament_classes, 'unknown_classes': unknown,
                 'not_carried_classes': [n for n in _USAGE_CLASSES
                                         if classes[n]['status'] == 'not_carried_by_format'],
                 'usage_known': not unknown, 'slicer_reported': None, 'disagreement': None}
        # Merge the slicer-reported totals for this plate, where they exist.
        if unreadable:
            entry['slicer_unknown_reason'] = USAGE_UNREADABLE
            entry['usage_known'] = False
        elif str(plate_id) in stats_plates:
            reported = stats_plates[str(plate_id)]
            entry['slicer_reported'] = {
                'extruders': reported.get('extruders') or {},
                'usage_known': reported.get('usage_known'),
                'usage_unknown_reason': reported.get('usage_unknown_reason')}
            if reported.get('usage_known'):
                slicer_set = {int(k) for k in (reported.get('extruders') or {})}
                if slicer_set != used:
                    entry['disagreement'] = (
                        f'slicer-reported filaments {sorted(slicer_set)} disagree with the source '
                        f'analysis filaments {sorted(used)} for plate {plate_id}')
                    entry['usage_known'] = False
            elif reported.get('usage_unknown_reason') == USAGE_INVALID:
                entry['usage_known'] = False
        out[str(plate_id)] = entry
    return out


def used_source_filaments(manifest, plate_id):
    """The source filament IDs one plate uses, per its usage analysis ([] when unanalyzed)."""
    entry = (manifest.get('plate_usage') or {}).get(str(plate_id)) or {}
    return list(entry.get('source_filaments') or [])


def _plate_readiness_problem_rows(manifest, decisions, selected_plate_ids, slot_capacity=None):
    """(problem, is_filament_warning) rows behind plate_readiness_problems.

    A problem is a filament warning when it reports filament usage data the
    source could not establish: a usage class that is unknown (including a
    support designation naming a filament outside the project palette), a
    plate with no usage analysis, slicer-reported usage that could not be
    validated, or a disagreement between the slicer-reported and analyzed
    filament sets. Owner ruling (catduck, 2026-11-10): those warn and the
    operator may accept them; they never hard-block a print. The rest are
    not filament data and keep refusing: no plate selected, decisions bound
    to another source, a used filament with no slot mapping in the decisions
    (missing operator input — accepting cannot invent the mapping), and a
    mapping past the destination's loaded-slot capacity (a hardware fact).
    """
    rows = []
    selected = [str(p) for p in (selected_plate_ids or [])]
    if not selected:
        return [('no plate is selected; per-plate filament usage cannot be validated', False)]
    usage = manifest.get('plate_usage') or {}
    if not isinstance(decisions, dict) or decisions.get('source_sha256') != manifest.get('source_sha256'):
        rows.append(('color decisions are not bound to this source; no filament mapping is validated', False))
        decisions = {}
    mode = decisions.get('mode')
    choices = decisions.get('choices') if isinstance(decisions.get('choices'), dict) else {}
    selection = decisions.get('selection') if isinstance(decisions.get('selection'), dict) else {}
    parts_by_plate = {}
    for part in manifest.get('parts') or []:
        parts_by_plate.setdefault(str(part.get('key')), part)
    mapped_slots = set()
    for plate_id in selected:
        entry = usage.get(plate_id)
        if entry is None:
            rows.append((f'plate {plate_id}: per-plate filament usage is unknown (no usage '
                         'analysis for this plate); its color mapping cannot be validated', True))
            continue
        for name in entry.get('unknown_classes') or []:
            reason = (entry['classes'].get(name) or {}).get('reason') or 'usage cannot be determined'
            rows.append((f'plate {plate_id}: {name} filament usage is unknown ({reason}); '
                         'its color mapping cannot be validated', True))
        if entry.get('disagreement'):
            rows.append((f"plate {plate_id}: {entry['disagreement']}; "
                         'its color mapping cannot be validated', True))
        if not entry.get('usage_known') and not entry.get('unknown_classes') \
                and not entry.get('disagreement'):
            reason = entry.get('slicer_unknown_reason') or 'slicer-reported usage could not be validated'
            rows.append((f'plate {plate_id}: per-plate filament usage is unknown ({reason}); '
                         'its color mapping cannot be validated', True))
        plate_parts = [parts_by_plate[k] for k in entry.get('part_keys') or [] if k in parts_by_plate]
        for ext in entry.get('source_filaments') or []:
            slots = set()
            if mode == 'single_color' and selection.get('slot') is not None:
                slots = {selection['slot']}
            elif mode == 'per_part':
                row = choices.get(f'filament:{ext}')
                if isinstance(row, dict) and row.get('slot') is not None:
                    slots = {row['slot']}
                else:
                    holders = [p for p in plate_parts if p.get('source_extruder') == ext]
                    picked = [choices.get('part:' + p['key']) for p in holders]
                    if holders and all(isinstance(c, dict) and c.get('slot') is not None for c in picked):
                        slots = {c['slot'] for c in picked}
            if not slots:
                rows.append((f'plate {plate_id}: source filament {ext} is used but has no '
                             'validated slot mapping', False))
            mapped_slots |= {s for s in slots if isinstance(s, int) and not isinstance(s, bool)}
    if slot_capacity is not None:
        for slot in sorted(mapped_slots):
            if slot < 1 or slot > slot_capacity:
                rows.append((f'slot {slot} is named by a filament mapping but the destination '
                             f'has only {slot_capacity} loaded slots; a shared nozzle does '
                             'not add slots or spools', False))
    return rows


def plate_readiness_problems(manifest, decisions, selected_plate_ids, slot_capacity=None):
    """Named reasons automated readiness must block for the selected plates.

    A selected plate blocks when a usage class for it is unknown, when the
    slicer-reported and analyzed filament sets disagree, or when a filament
    the plate uses has no validated slot mapping in the source-bound
    decisions. An unselected plate never contributes a problem. Mappings are
    validated against the destination's loaded-slot capacity when given: a
    shared nozzle (AD5X) does not add slots, so a mapping naming a slot past
    the capacity, however many nozzles share the path, is not validated.
    Logical source filament IDs, loaded slots and nozzles stay distinct:
    decisions map source IDs to slots, and only slots are capacity-checked.
    """
    return [text for text, _ in _plate_readiness_problem_rows(
        manifest, decisions, selected_plate_ids, slot_capacity=slot_capacity)]


def filament_readiness_warnings(manifest, decisions, selected_plate_ids, slot_capacity=None):
    """The filament-warning subset of the readiness problems.

    These report filament usage data the source could not establish. Under
    the warn-and-accept ruling they are presented to the operator, who may
    accept them and print anyway; the remaining readiness problems are not
    filament data and keep refusing (see _plate_readiness_problem_rows).
    """
    return [text for text, is_warning in _plate_readiness_problem_rows(
        manifest, decisions, selected_plate_ids, slot_capacity=slot_capacity) if is_warning]


def require_plate_readiness(manifest, decisions, selected_plate_ids, slot_capacity=None):
    """Raise with the named reasons when a selected plate is not ready to print."""
    problems = plate_readiness_problems(manifest, decisions, selected_plate_ids, slot_capacity)
    if problems:
        raise RuntimeError('print readiness blocked: ' + '; '.join(problems))


def _append_filament_stats(path, manifest):
    path = Path(path)
    has_slice_usage = False
    stats = {'total_g': 0.0, 'total_m': 0.0, 'extruders': {}}
    if path.suffix.lower() == '.3mf':
        try:
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
                if 'Metadata/slice_info.config' in names:
                    # The file's presence is the claim, so a parse failure
                    # below is a failed claim, never an absent one.
                    has_slice_usage = True
                    root = ET.fromstring(z.read('Metadata/slice_info.config'))
                    stats['plates'] = {}
                    expected_filaments = {int(f.get('source_extruder')) for f in manifest.get('filaments') or []
                                          if f.get('source_extruder')}
                    model_settings = None
                    if 'Metadata/model_settings.config' in names:
                        model_settings = ET.fromstring(z.read('Metadata/model_settings.config'))
                    for plate_index, plate in enumerate(root.findall('.//plate'), 1):
                        # Ignore metadata-only/empty plates: without assigned model
                        # instances there is no source filament demand to validate.
                        plate_index_node = plate.find("metadata[@key='index']")
                        usage_plate_id = plate_index_node.get('value') if plate_index_node is not None else str(plate_index)
                        if model_settings is not None:
                            source_plate = next((candidate for candidate in model_settings.findall('.//plate')
                                                 if any(m.get('key') == 'plater_id' and m.get('value') == usage_plate_id
                                                        for m in candidate.findall('.//metadata'))), None)
                            if source_plate is not None and not source_plate.findall('.//model_instance'):
                                continue
                        # Bambu's slice_info.config reports real sliced usage per plate.
                        # Preserve its plate index and source filament IDs; do not
                        # conflate logical source colors with loaded slots/nozzles.
                        meta = {m.get('key'): m.get('value') for m in plate.findall('metadata')}
                        plate_id = meta.get('index') or str(plate_index)
                        plate_stats = {'extruders': {}, 'total_g': 0.0, 'total_m': 0.0,
                                       'usage_known': True, 'usage_unknown_reason': None}
                        for fil in plate.findall('.//filament'):
                            try:
                                g = float(fil.get('used_g', 0))
                                m = float(fil.get('used_m', 0))
                                ext_id = int(fil.get('id'))
                                if ext_id < 1 or not math.isfinite(g) or not math.isfinite(m) or g < 0 or m < 0:
                                    raise ValueError
                            except (TypeError, ValueError):
                                plate_stats['usage_known'] = False
                                plate_stats['usage_unknown_reason'] = USAGE_INVALID
                                continue
                            if expected_filaments and ext_id not in expected_filaments:
                                # An invalid record on the same plate stays the reason.
                                plate_stats['usage_known'] = False
                                if plate_stats['usage_unknown_reason'] != USAGE_INVALID:
                                    plate_stats['usage_unknown_reason'] = USAGE_UNMAPPED
                                continue
                            if g > 0 or m > 0:
                                # One filament can feed several plates: add them, never keep only the last plate.
                                ext = stats['extruders'].setdefault(ext_id, {'g': 0.0, 'm': 0.0})
                                ext['g'] += g
                                ext['m'] += m
                                stats['total_g'] += g
                                stats['total_m'] += m
                                plate_stats['extruders'][ext_id] = {'g': g, 'm': m}
                                plate_stats['total_g'] += g
                                plate_stats['total_m'] += m
                        observed = set(plate_stats['extruders'])
                        if expected_filaments and not observed and plate_stats['usage_known']:
                            plate_stats['usage_known'] = False
                            plate_stats['usage_unknown_reason'] = USAGE_NO_RECORDS
                        plate_stats['total_g'] = round(plate_stats['total_g'], 1)
                        plate_stats['total_m'] = round(plate_stats['total_m'], 1)
                        stats['plates'][str(plate_id)] = plate_stats
                    for ext in stats['extruders'].values():
                        ext['g'], ext['m'] = round(ext['g'], 1), round(ext['m'], 1)
        except Exception:
            if has_slice_usage:
                stats['plates'] = {'unreadable': {'extruders': {}, 'total_g': 0.0, 'total_m': 0.0,
                                                  'usage_known': False,
                                                  'usage_unknown_reason': USAGE_UNREADABLE}}
    if stats['total_g'] == 0:
        try:
            from . import model_job as _mj
            info_out = _mj.model_info_output(path)
            vols = re.findall(r'volume\s*=\s*([0-9.]+)', info_out)
            if vols:
                tot_cm3 = sum(float(v) for v in vols) / 1000.0
                est_g = tot_cm3 * 1.24 * 0.43
                est_m = est_g / 2.98
                stats['total_g'] = round(est_g, 1)
                stats['total_m'] = round(est_m, 1)
                stats['estimated'] = True  # volume guess, not a slicer figure
                if manifest.get('filaments'):
                    stats['extruders'][1] = {'g': stats['total_g'], 'm': stats['total_m']}
        except Exception:
            pass
    stats['total_g'] = round(stats['total_g'], 1)
    stats['total_m'] = round(stats['total_m'], 1)
    if path.suffix.lower() == '.3mf' and not has_slice_usage:
        stats['plates'] = {'unknown': {'extruders': {}, 'total_g': 0.0, 'total_m': 0.0,
                                       'usage_known': False,
                                       'usage_unknown_reason': USAGE_ABSENT}}
    manifest['filament_stats'] = stats


def _shape_word(bounds):
    """Plain shape from a bounding box, for CAD primitive names that say nothing."""
    try:
        x, y, z = (float(v) for v in bounds[:3])
    except Exception:
        return ''
    big, small = max(x, y, z), min(x, y, z)
    if big <= 15:
        return 'Small pin/peg'
    if z == small and z < 0.2 * min(x, y):
        return 'Flat round disc' if abs(x - y) < 0.1 * max(x, y) else 'Flat plate'
    if z == big and z > 2 * max(x, y):
        return 'Tall post'
    if z == small:
        return 'Low round piece' if abs(x - y) < 0.1 * max(x, y) else 'Low piece'
    return 'Chunky piece'


def _prompt_parts(part, plural=False):
    """(base name, [detail, ...]) for a question; _prompt_name joins them.

    plural words the author colour for a group of identical parts ('author made them Pink').
    """
    try:
        from . import part_translator
        base = part_translator.translate_name(part.get('name') or '') if not part_translator.is_meaningless(part.get('name'))             else (part.get('object_name') or 'Main body')
        hint = part_translator.size_hint(part)
    except Exception:
        base, hint = part.get('name') or '', ''
    base = re.sub(r'[_]+', ' ', str(base)).strip()
    shape = _shape_word(part.get('bounds_mm'))
    if shape and re.fullmatch(r'(?i)(cylinder|cube|box|sphere|cone|body|object|part|mesh|solid|extrude|torus)(\s*[a-z0-9]{0,3})?(\s*\d+)?', base):
        base = f'{shape} ("{base}")'
    details = [hint] if hint else []
    if part.get('plate'):
        details.append(f"plate {part['plate']}")
    if part.get('author_color'):
        try:
            from . import part_translator
            color = part_translator.named_color(part['author_color']).title()
        except Exception:
            color = str(part['author_color'])
        details.append(f"author made {'them' if plural else 'it'} {color}")
    return base, details


def _prompt_name(part):
    """English display name for a question. Never used as a decision key.

    'Cylinder A (98 x 98 x 12 mm, plate 2, author made it White)': name alone
    is useless when the CAD export calls half the model 'Cylinder'.
    """
    base, details = _prompt_parts(part)
    return f"{base}, {', '.join(details)}" if details else base


def short_part_name(part):
    """The part's display name without size, plate or author colour (for summaries)."""
    return _prompt_parts(part)[0]


def _family_question(fam, chunk):
    """One question for a group of sibling parts, without repeating identical parts.

    'Choose one color/material for the 4 chains (15 x 87.7 x 6 mm, author made them Pink)'
    """
    count = len(chunk)
    who = f"both {fam['noun']}" if count == 2 else f"the {count} {fam['noun']}"
    parts = [_prompt_parts(p, plural=True) for p in chunk]
    detail_sets = {tuple(d) for _, d in parts}
    bases = list(dict.fromkeys(b for b, _ in parts))
    if len(detail_sets) == 1:
        details = list(next(iter(detail_sets)))
        inner = ', '.join(bases) if len(bases) > 1 else ''
        inner = '; '.join(x for x in (inner, ', '.join(details)) if x)
        return f"Choose one color/material for {who}" + (f" ({inner})" if inner else '')
    counts = {}
    for name in fam['names']:
        counts[name] = counts.get(name, 0) + 1
    listed = '; '.join(f'{n}× {name}' if n > 1 else name for name, n in counts.items())
    return f"Choose one color/material for {who}: {listed}"


def _palette_rows(manifest, canonical=False):
    """Prompts for triangle-painted source regions, not object assignments."""
    palette = manifest['filaments']
    if not palette:
        return [{'key': 'painted:unknown',
                 'question': 'Painted colors are unresolved; inspect the source palette before slicing'}]
    stats = manifest.get('filament_stats', {})
    ext_stats = stats.get('extruders', {})
    rows = []
    for f in palette:
        ext = f.get('source_extruder')
        color = str(f.get('color') or '').strip()
        material = str(f.get('material') or 'unknown material').strip()
        if canonical and not color:
            raise ValueError('Painted source regions cannot be distinguished by their color/material labels; '
                             'preparation requires a source with identifiable regions or separate named parts')
        try:
            from . import part_translator
            name = part_translator.named_color(color)
        except Exception:
            name = color or 'unknown color'
        stat_suffix = ''
        if canonical and ext and ext in ext_stats:
            s = ext_stats[ext]
            stat_suffix = f" [uses ~{s['g']}g / {s['m']}m]"
        action = 'Choose the loaded color/material for' if canonical else 'Map'
        rows.append({'key': f'filament:{ext}',
                     'question': (f"{action} {name.title()} regions ({color or 'unknown color'}, {material})"
                                  f"{stat_suffix}" if canonical else
                                  f"Map {name.title()} regions ({color or 'unknown color'}, {material}) to an IFS slot"),
                     'source_extruder': ext})
    if len({row['key'] for row in rows}) != len(rows):
        raise ValueError('duplicate part/color selection key')
    return rows


def _selected_scope(manifest, selected_plate_ids):
    """(part_keys, filaments, conservative) a selected-plate print is scoped to.

    None when no selection is given (the whole source is in scope). The
    filaments come from the per-plate usage analysis; when a selected plate's
    usage is partly unknown the scope is conservative — filament questions
    are not dropped on a guess, and readiness (plate_readiness_problems) is
    what blocks. Without an analysis (a hand-built manifest), part plate
    annotations, layer changes and support designations scope as far as they
    reach.
    """
    if selected_plate_ids is None:
        return None
    selected = {str(p) for p in selected_plate_ids}
    usage = manifest.get('plate_usage') or {}
    part_keys, filaments, conservative = set(), set(), False
    if usage:
        for plate_id in selected:
            entry = usage.get(plate_id)
            if entry is None:
                conservative = True
                continue
            part_keys |= set(entry.get('part_keys') or [])
            filaments |= set(entry.get('source_filaments') or [])
            if entry.get('unknown_classes') or entry.get('disagreement'):
                conservative = True
        return part_keys, filaments, conservative
    parts = manifest.get('parts') or []
    if parts and all(p.get('plate') is None for p in parts) and len(selected) == 1:
        part_keys = {p['key'] for p in parts}  # one implicit plate holds every part
    else:
        part_keys = {p['key'] for p in parts
                     if p.get('plate') is not None and str(p['plate']) in selected}
    filaments = {p.get('source_extruder') for p in parts if p['key'] in part_keys}
    filaments |= {r.get('source_extruder') for r in manifest.get('layer_changes') or []
                  if str(r.get('plate')) in selected}
    filaments |= {e for e in (manifest.get('support_filaments') or {}).values() if e}
    if manifest.get('painted'):
        filaments |= {f.get('source_extruder') for f in manifest.get('filaments') or []}
    return part_keys, {f for f in filaments if f}, conservative


def prompts(manifest, selected_plate_ids=None):
    """Legacy presentation retains part and palette prompts for old JOB schemas."""
    scope = _selected_scope(manifest, selected_plate_ids)
    rows = [{'key': 'part:' + p['key'], 'question': f"Choose slot/color/material for {_prompt_name(p)}",
             'source_extruder': p['source_extruder']} for p in manifest['parts']
            if scope is None or p['key'] in scope[0]]
    if manifest['painted'] or len(manifest['filaments']) > 1:
        palette = _palette_rows(manifest)
        if scope is not None and not scope[2]:
            palette = [row for row in palette
                       if row.get('source_extruder') in scope[1]
                       or row['key'] == 'painted:unknown']
        rows += palette
    if len({row['key'] for row in rows}) != len(rows):
        raise ValueError('duplicate part/color selection key')
    return rows


def canonical_prompts(manifest, selected_plate_ids=None):
    """Geometry slots are authoritative; unused author palette rows are not choices.

    Triangle-painted regions override object extruders and use their palette.
    With selected_plate_ids, the questions cover only the colors the selected
    plates actually use (per the plate usage analysis): an unselected plate's
    parts and colors never prompt.
    """
    scope = _selected_scope(manifest, selected_plate_ids)
    if manifest['painted']:
        rows = _palette_rows(manifest, canonical=True)
        if scope is not None and not scope[2]:
            rows = [row for row in rows if row.get('source_extruder') in scope[1]]
        return rows
    stats = manifest.get('filament_stats', {})
    ext_stats = stats.get('extruders', {})

    rows = []
    for p in manifest['parts']:
        if scope is not None and p['key'] not in scope[0]:
            continue  # an unselected plate's parts never prompt
        ext = p.get('source_extruder')
        # No per-part grams here: an extruder total shown on every part read as N x the real usage.
        stat_suffix = ''
        rows.append({'key': 'part:' + p['key'],
                     'question': f"Choose the color/material for {_prompt_name(p)}{stat_suffix}",
                     'source_extruder': p['source_extruder']})
    # Height-based color changes (#870): an extruder the per-layer tool
    # changes switch to is a color question in its own right, alongside the
    # part colors. An extruder a part already asks about needs no second row.
    # Scoped to a selected-plate print, only the parts actually asked about
    # cover their extruder; an extruder used only on an unselected plate is
    # not covered here (and its layer rows below are scoped out too).
    covered = {p.get('source_extruder') for p in manifest['parts']
               if scope is None or p['key'] in scope[0]}
    # Extruders that already have a filament:<ext> question of their own (a
    # height-change row). A support designation dedups against these only:
    # its question is keyed the same way and asks for that extruder's spool.
    filament_rows = set()
    filaments = {f.get('source_extruder'): f for f in manifest.get('filaments') or []}
    for row in manifest.get('layer_changes') or []:
        ext = row.get('source_extruder')
        if scope is not None and str(row.get('plate')) not in {str(p) for p in selected_plate_ids}:
            continue  # a layer change on an unselected plate never prompts
        if not ext or ext in covered:
            continue
        covered.add(ext)
        filament_rows.add(ext)
        filament = filaments.get(ext) or {}
        color = str(filament.get('color') or row.get('color') or '').strip()
        material = str(filament.get('material') or 'unknown material').strip()
        if not color:
            raise ValueError('Height-based color changes cannot be distinguished by their color/material '
                             'labels; preparation requires a source with identifiable regions or separate '
                             'named parts')
        try:
            from . import part_translator
            name = part_translator.named_color(color)
        except Exception:
            name = color
        heights = sorted({r['top_z'] for r in manifest['layer_changes']
                          if r.get('source_extruder') == ext and r.get('top_z') is not None
                          and (scope is None or str(r.get('plate')) in {str(p) for p in selected_plate_ids})})
        where = f" at {' and '.join(f'{z:g} mm' for z in heights)}" if heights else ''
        rows.append({'key': f'filament:{ext}',
                     'question': f"Choose the loaded color/material for the height-based color change to "
                                 f"{name.title()} ({color}, {material}){where}",
                     'source_extruder': ext})
    # A dedicated support filament (#867): the author named a filament for the
    # supports (and optionally a second one for the interface layers). Like a
    # height-change extruder it is a color question in its own right, so its
    # spool can be loaded and the supports pointed at it. It is asked even
    # when a part prints with the same source extruder: in per-part decisions
    # that extruder's parts can be split across spools, so no single part
    # choice names the support spool (#876).
    designated = manifest.get('support_filaments') or {}
    for field, label in (('base', 'support'), ('interface', 'support interface')):
        ext = designated.get(field)
        if not ext or ext in filament_rows:
            continue
        if scope is not None and not scope[2] and ext not in scope[1]:
            continue  # no selected plate uses this designated filament
        filament_rows.add(ext)
        filament = filaments.get(ext) or {}
        color = str(filament.get('color') or '').strip()
        material = str(filament.get('material') or 'unknown material').strip()
        try:
            from . import part_translator
            name = part_translator.named_color(color)
        except Exception:
            name = color
        rows.append({'key': f'filament:{ext}',
                     'question': f"Choose the loaded color/material for the {label} filament "
                                 f"{name.title() if name else 'the designated filament'} "
                                 f"({color or 'unknown color'}, {material})",
                     'source_extruder': ext})
    # Filaments a plate uses only through a usage class with no row of its
    # own yet (a height-range extruder assignment, a PrusaSlicer wall or
    # infill extruder): the plate cannot print until that filament is mapped,
    # so it is asked about like any other source color, named by its class.
    usage = manifest.get('plate_usage') or {}
    relevant = usage if scope is None else {pid: usage[pid] for pid in
                                            (str(p) for p in selected_plate_ids) if pid in usage}
    for plate_id in sorted(relevant, key=str):
        entry = relevant[plate_id]
        for ext in entry.get('source_filaments') or []:
            if ext in covered or ext in filament_rows:
                continue
            covered.add(ext)
            filament_rows.add(ext)
            filament = filaments.get(ext) or {}
            color = str(filament.get('color') or '').strip()
            material = str(filament.get('material') or 'unknown material').strip()
            try:
                from . import part_translator
                name = part_translator.named_color(color)
            except Exception:
                name = color
            classes = (entry.get('filament_classes') or {}).get(str(ext)) or ['objects']
            label = _CLASS_LABELS.get(classes[0], 'source')
            rows.append({'key': f'filament:{ext}',
                         'question': f"Choose the loaded color/material for the {label} filament "
                                     f"{name.title() if name else 'the designated filament'} "
                                     f"({color or 'unknown color'}, {material})",
                         'source_extruder': ext})
    if manifest['painted'] and not manifest['parts']:
        palette = manifest['filaments']
        labels = [(str(f.get('color') or '').strip(), str(f.get('material') or 'unknown material').strip())
                  for f in palette]
        if not palette or any(not color for color, material in labels) or len(set(labels)) != len(labels):
            raise ValueError('Painted source regions cannot be distinguished by their color/material labels; '
                             'preparation requires a source with identifiable regions or separate named parts')
        for f, (color, material) in zip(palette, labels):
            ext = f.get('source_extruder')
            stat_suffix = ''
            if ext and ext in ext_stats:
                s = ext_stats[ext]
                stat_suffix = f" [uses ~{s['g']}g / {s['m']}m]"
            rows.append({'key': f"filament:{f['source_extruder']}",
                         'question': f"Choose the loaded color/material for source regions colored {color} ({material}){stat_suffix}",
                         'source_extruder': f['source_extruder']})
    return rows


def prompt_source_colour(manifest, row):
    """The (color, material) identity of one colour question row, from its
    source extruder. Rows sharing an identity print the same source colour."""
    ext = row.get('source_extruder')
    if ext is None and str(row.get('key') or '').startswith('filament:'):
        try:
            ext = int(str(row['key']).split(':', 1)[1])
        except ValueError:
            ext = None
    if ext is None and str(row.get('key') or '').startswith('part:'):
        part = next((p for p in manifest.get('parts') or [] if p['key'] == str(row['key'])[5:]), {})
        ext = part.get('source_extruder')
    filament = next((f for f in manifest.get('filaments') or []
                     if f.get('source_extruder') == ext), {})
    return (str(filament.get('color') or '').strip().casefold(),
            str(filament.get('material') or '').strip().casefold())


def distinct_source_colours(manifest):
    """The distinct (color, material) pairs a source actually prints with.

    Counted over the source filaments its parts or painted regions use, so two
    source extruders sharing one colour count once. A multi_tool printer needs
    one toolhead per entry here (#346).
    """
    filaments = {f.get('source_extruder'): f for f in manifest.get('filaments') or []}
    used = {p.get('source_extruder') for p in manifest.get('parts') or [] if p.get('source_extruder')}
    # Height-based color changes print with their extruders too (#870).
    used |= {row.get('source_extruder') for row in manifest.get('layer_changes') or []
             if row.get('source_extruder')}
    # A designated support filament prints the supports, in its own color (#867).
    used |= {ext for ext in (manifest.get('support_filaments') or {}).values() if ext}
    # Filaments a plate uses only through another usage class (height ranges,
    # per-feature extruders) print too (#1110 Phase B analysis).
    for entry in (manifest.get('plate_usage') or {}).values():
        used |= {ext for ext in entry.get('source_filaments') or [] if ext}
    if manifest.get('painted'):
        used |= {ext for ext in filaments if ext}
    colours = set()
    for ext in used:
        filament = filaments.get(ext) or {}
        colours.add((str(filament.get('color') or '').strip().casefold(),
                     str(filament.get('material') or '').strip().casefold()))
    return colours


def layer_change_summary(manifest):
    """Plain sentence naming a source's height-based color changes (#870):
    each switch with its height and the color it switches to, from the
    palette (falling back to the color the entry itself carries)."""
    filaments = {f.get('source_extruder'): f for f in manifest.get('filaments') or []}
    bits = []
    for row in manifest.get('layer_changes') or []:
        filament = filaments.get(row.get('source_extruder')) or {}
        color = str(filament.get('color') or row.get('color') or '').strip()
        try:
            from . import part_translator
            name = part_translator.named_color(color).title() if color else ''
        except Exception:
            name = color
        label = name or f"extruder {row.get('source_extruder')}"
        where = f" at {row['top_z']:g} mm" if row.get('top_z') is not None else ''
        bits.append(f"to {label}{where}")
    return 'changes color by height: ' + ', '.join(bits) if bits else ''


def _family_key(name):
    """'Stencil 2 (Circle) - Tabs.stl' -> 'stencil # - tabs'. Parenthetical variants and numbers are ignored."""
    stem = re.sub(r'\.(stl|3mf|obj|step|stp)$', '', str(name or '').strip(), flags=re.I)
    stem = re.sub(r'\([^)]*\)|\[[^\]]*\]', ' ', stem)
    stem = re.sub(r'\d+', '#', stem.lower())
    return re.sub(r'\s+', ' ', stem).strip(' -_')


def _family_noun(key, count):
    head = key.split('#', 1)[0].strip(' -_') or key.split(' - ', 1)[0]
    head = re.sub(r'[_]+', ' ', head or 'parts').strip()
    try:
        from . import part_translator
        head = part_translator.translate_name(head).split(' (', 1)[0].lower()
    except Exception:
        pass
    return head if head.endswith('s') or count == 1 else head + 's'


def part_families(manifest):
    """Group named parts that differ only by number or parenthetical variant,
    preserving source_extruder boundaries so multi-color slot assignments
    are never collapsed across different extruders.

    Returns ordered groups [{'keys': [part keys], 'names': [...], 'label': str}].
    A group of one is an ordinary part. Only numbered or identically named
    siblings with the same source extruder group; distinct names or different
    extruders always get their own group.
    """
    order, groups = [], {}
    for part in manifest['parts']:
        ext = part.get('source_extruder')
        key = _family_key(part['name'])
        # Unnamed parts never group by accident.
        if not key:
            key = '\0' + part['key']
        # Group must be partitioned by extruder if known
        full_key = (ext, key)
        if full_key not in groups:
            order.append(full_key)
            groups[full_key] = []
        groups[full_key].append(part)
    out = []
    for full_key in order:
        ext, key = full_key
        members = groups[full_key]
        groupable = len(members) > 1 and ('#' in key or len({p['name'] for p in members}) == 1)
        chunks = [members] if groupable else [[p] for p in members]
        for chunk in chunks:
            names = [_prompt_name(p) for p in chunk]
            ext_suffix = ""  # 'extruder 2' means nothing to the person choosing; author color says it
            noun = _family_noun(key, len(chunk))
            if re.match(r'(?i)(cylinder|cube|box|sphere|cone|body|object|part|mesh|solid|extrude|torus)', noun):
                noun = 'matching pieces'
            label = (f"{'both' if len(chunk) == 2 else f'all {len(chunk)}'} {noun}{ext_suffix}" if len(chunk) > 1 else f"{names[0]}{ext_suffix}")
            fam = {'keys': [p['key'] for p in chunk], 'names': names, 'label': label, 'noun': noun}
            if len(chunk) > 1:
                fam['question'] = _family_question(fam, chunk)
            out.append(fam)
    return out


def grouped_prompts(manifest, selected_plate_ids=None):
    """canonical_prompts with numbered part families collapsed into one question.

    Each row carries 'keys' (decision keys it answers). Painted filament rows
    are never grouped. With selected_plate_ids, families are formed over the
    selected plates' parts only, so an unselected plate never prompts.
    """
    rows = canonical_prompts(manifest, selected_plate_ids=selected_plate_ids)
    if manifest['painted']:
        return [{**row, 'keys': [row['key']]} for row in rows]
    by_key = {row['key']: row for row in rows}
    scope = _selected_scope(manifest, selected_plate_ids)
    family_manifest = manifest if scope is None else {
        **manifest, 'parts': [p for p in manifest['parts'] if p['key'] in scope[0]]}
    out = []
    for fam in part_families(family_manifest):
        keys = ['part:' + k for k in fam['keys']]
        if len(keys) == 1:
            out.append({**by_key[keys[0]], 'keys': keys})
        else:
            out.append({'key': keys[0], 'keys': keys, 'source_extruder': by_key[keys[0]]['source_extruder'],
                        'label': fam['label'], 'question': fam['question']})
    out += [{**row, 'keys': [row['key']]} for row in rows if not row['key'].startswith('part:')]
    return out


def validate_decisions(manifest, decisions, state, resolve, selected_plate_ids=None):
    if not isinstance(decisions, dict) or decisions.get('source_sha256') != manifest['source_sha256']:
        raise ValueError('color decisions must bind the exact source SHA256')
    schema = decisions.get('schema')
    if schema not in (None, 'part_slots_v1'):
        raise ValueError('unknown color decision schema')
    mode = decisions.get('mode')
    if mode == 'single_color':
        out = {'source_sha256': manifest['source_sha256'], 'mode': mode,
               **({'schema': schema} if schema else {}),
               'selection': resolve(state, decisions.get('selection'))}
        # A single-color print of a height-color source must say what happens
        # to the changes (#870): pauses for a manual swap, or one color with
        # the changes dropped. The choice is recorded, never left silent.
        height = decisions.get('height_colors')
        if height is not None:
            if height not in ('pause', 'one_color'):
                raise ValueError("height_colors must be 'pause' or 'one_color'")
            out['height_colors'] = height
        return out
    if mode != 'per_part':
        raise ValueError('choose single_color explicitly or supply per_part/filament selections')
    expected = {p['key'] for p in (canonical_prompts(manifest, selected_plate_ids=selected_plate_ids)
                                   if schema == 'part_slots_v1'
                                   else prompts(manifest, selected_plate_ids=selected_plate_ids))}
    choices = decisions.get('choices')
    if not isinstance(choices, dict) or set(choices) != expected or 'painted:unknown' in expected:
        raise ValueError('every part and source filament requires an explicit selection')
    return {'source_sha256': manifest['source_sha256'], 'mode': mode,
            **({'schema': schema} if schema else {}),
            'choices': {key: resolve(state, value) for key, value in choices.items()}}


def require_single_color(manifest, decisions):
    if not manifest['requires_choices']:
        return
    if not decisions or decisions.get('source_sha256') != manifest['source_sha256']:
        raise RuntimeError('part/color choices required; run color-plan and color-select before slicing')
    if decisions.get('mode') != 'single_color':
        raise RuntimeError('per-part colors recorded; canonical STL adapter cannot preserve colors. No slicing attempted')
