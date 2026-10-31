"""Offline 3MF part/color inventory and job-bound operator decisions.

Source extruders are one based. Physical IFS slots are separate identifiers.
Never infer a slot from RGB or flatten a painted project without consent.
"""
import json
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


def inventory(path):
    from .intake_resources import check_source
    path = Path(path)
    check_source(path)
    if path.suffix.lower() != '.3mf':
        # Non-3MF sources have no palette, but every decision is still bound to the file bytes.
        res = {'source_sha256': __import__('hashlib').sha256(path.read_bytes()).hexdigest(),
               'parts': [], 'filaments': [], 'painted': False, 'requires_choices': False}
        _append_filament_stats(path, res)
        return res
    parts, filaments, painted, cutters = [], [], False, []
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
        if 'Metadata/model_settings.config' in names:
            root = ET.fromstring(archive.read('Metadata/model_settings.config'))
            for obj in root.findall('object'):
                meta = {m.get('key'): m.get('value') for m in obj.findall('metadata')}
                children = obj.findall('part') or [obj]
                for part in children:
                    pm = {m.get('key'): m.get('value') for m in part.findall('metadata')}
                    raw = pm.get('extruder', meta.get('extruder'))
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
        # Negative parts and modifiers are cutters/settings volumes, not printed
        # bodies: never a color question, never a separate printed object.
        _annotate_parts(archive, names, parts, filaments)
        cutters = [p for p in parts if p.get('subtype', 'normal_part') != 'normal_part']
        parts = [p for p in parts if p.get('subtype', 'normal_part') == 'normal_part']
        if not parts:
            raise RuntimeError('unresolved 3MF part inventory')
    res = {'source_sha256': __import__('hashlib').sha256(path.read_bytes()).hexdigest(),
           'parts': parts, 'cutters': cutters, 'filaments': filaments, 'painted': painted,
           'requires_choices': painted or len(parts) > 1 or len(filaments) > 1}
    _append_filament_stats(path, res)
    return res


def _append_filament_stats(path, manifest):
    path = Path(path)
    stats = {'total_g': 0.0, 'total_m': 0.0, 'extruders': {}}
    if path.suffix.lower() == '.3mf':
        try:
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
                if 'Metadata/slice_info.config' in names:
                    root = ET.fromstring(z.read('Metadata/slice_info.config'))
                    for plate in root.findall('.//plate'):
                        for fil in plate.findall('.//filament'):
                            g = float(fil.get('used_g', 0))
                            m = float(fil.get('used_m', 0))
                            ext_id = int(fil.get('id', 1))
                            if g > 0 or m > 0:
                                # One filament can feed several plates: add them, never keep only the last plate.
                                ext = stats['extruders'].setdefault(ext_id, {'g': 0.0, 'm': 0.0})
                                ext['g'] += g
                                ext['m'] += m
                                stats['total_g'] += g
                                stats['total_m'] += m
                    for ext in stats['extruders'].values():
                        ext['g'], ext['m'] = round(ext['g'], 1), round(ext['m'], 1)
        except Exception:
            pass
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


def prompts(manifest):
    """Legacy presentation retains part and palette prompts for old JOB schemas."""
    rows = [{'key': 'part:' + p['key'], 'question': f"Choose slot/color/material for {_prompt_name(p)}",
             'source_extruder': p['source_extruder']} for p in manifest['parts']]
    if manifest['painted'] or len(manifest['filaments']) > 1:
        rows += _palette_rows(manifest)
    if len({row['key'] for row in rows}) != len(rows):
        raise ValueError('duplicate part/color selection key')
    return rows


def canonical_prompts(manifest):
    """Geometry slots are authoritative; unused author palette rows are not choices.

    Triangle-painted regions override object extruders and use their palette.
    """
    if manifest['painted']:
        return _palette_rows(manifest, canonical=True)
    stats = manifest.get('filament_stats', {})
    ext_stats = stats.get('extruders', {})

    rows = []
    for p in manifest['parts']:
        ext = p.get('source_extruder')
        # No per-part grams here: an extruder total shown on every part read as N x the real usage.
        stat_suffix = ''
        rows.append({'key': 'part:' + p['key'],
                     'question': f"Choose the color/material for {_prompt_name(p)}{stat_suffix}",
                     'source_extruder': p['source_extruder']})
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
    if manifest.get('painted'):
        used |= {ext for ext in filaments if ext}
    colours = set()
    for ext in used:
        filament = filaments.get(ext) or {}
        colours.add((str(filament.get('color') or '').strip().casefold(),
                     str(filament.get('material') or '').strip().casefold()))
    return colours


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


def grouped_prompts(manifest):
    """canonical_prompts with numbered part families collapsed into one question.

    Each row carries 'keys' (decision keys it answers). Painted filament rows
    are never grouped.
    """
    rows = canonical_prompts(manifest)
    if manifest['painted']:
        return [{**row, 'keys': [row['key']]} for row in rows]
    by_key = {row['key']: row for row in rows}
    out = []
    for fam in part_families(manifest):
        keys = ['part:' + k for k in fam['keys']]
        if len(keys) == 1:
            out.append({**by_key[keys[0]], 'keys': keys})
        else:
            out.append({'key': keys[0], 'keys': keys, 'source_extruder': by_key[keys[0]]['source_extruder'],
                        'label': fam['label'], 'question': fam['question']})
    out += [{**row, 'keys': [row['key']]} for row in rows if not row['key'].startswith('part:')]
    return out


def validate_decisions(manifest, decisions, state, resolve):
    if not isinstance(decisions, dict) or decisions.get('source_sha256') != manifest['source_sha256']:
        raise ValueError('color decisions must bind the exact source SHA256')
    schema = decisions.get('schema')
    if schema not in (None, 'part_slots_v1'):
        raise ValueError('unknown color decision schema')
    mode = decisions.get('mode')
    if mode == 'single_color':
        return {'source_sha256': manifest['source_sha256'], 'mode': mode,
                **({'schema': schema} if schema else {}),
                'selection': resolve(state, decisions.get('selection'))}
    if mode != 'per_part':
        raise ValueError('choose single_color explicitly or supply per_part/filament selections')
    expected = {p['key'] for p in (canonical_prompts(manifest) if schema == 'part_slots_v1' else prompts(manifest))}
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
