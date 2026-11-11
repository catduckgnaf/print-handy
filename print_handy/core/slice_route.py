"""Which package a per-part color slice hands the slicer (#1279).

A per-part color job on a single-nozzle multi-material printer (an IFS or
AMS: ``multi_slot``) takes one of two routes. :func:`export_per_part` picks
the route, writes the package and names the route in the report's
``slice_route`` entry, so every slice record says which one it took.

* ``remap``: the job keeps the source project and only renumbers its
  filaments. Every palette filament has its own slot and no two share one,
  nothing is split off, resized, turned or merged from other plates. The
  source package itself is sliced, remapped by
  :func:`package_remap.remap_package` with the job's filament mapping and
  moved as a whole onto the bed by the same rigid translation the rebuild
  would apply. The project's own palette, per-filament settings, role and
  feature filaments, purge matrix, paint, height changes and height ranges
  stay with it, renumbered.
* ``rebuild``: every other job keeps today's exporter,
  :func:`color_geometry.export_color_geometry`, which rebuilds the package
  from the parts' meshes: a filament merge (two source filaments on one
  slot), a palette filament the job does not load, a plate split or plate
  merge, a scale change, a per-object fit, recorded item shifts, objects
  whose footprints touch (the rebuild joins them into one assembly), an
  identity toolhead numbering (``multi_tool`` printers), a pause-mode height
  change, a source with no slicer project, PrusaSlicer object roles that
  differ, and a printer whose primary slicer is not OrcaSlicer (Bambu Studio
  for Bambu Lab printers, or Flash Studio), where the remap route has no
  real-slicer verification. The single-color paths
  (``export_single_color_plate``) are not per-part jobs and never come here.

Both routes run the same checks first: the remap route measures the job with
``export_color_geometry(target=None)``, so every refusal the rebuild makes
(unmapped paint or layer tools, mirrored paint, an incomplete selection) is
made for the remap route too, except the refusal of height ranges that name
an extruder, which the remap route translates instead,
and the part records, build bounds, translation and prime tower placement the
slice path and its gates read are the rebuild's own numbers. The returned
tool mapping is the rebuild's, so the confirmation shows the same filaments
on the same slots on either route.

When ``remap_package`` refuses (an embedded plate G-code, a filament sequence
record, a non-uniform purge vector, an unclassified member, ...), the
refusal is named. The job falls back to the rebuild only when the rebuild
carries every filament reference the project makes: the rebuild never reads
the project's print-wide feature filaments or PrusaSlicer role extruders,
so a project that names one would print its walls, infill or tower on a
different filament than the remap would, and the slice is refused with
both reasons instead. A fallback carries ``notice``, which the start
confirmation shows the operator before anything is uploaded.
"""
import json
import re
import zipfile
from pathlib import Path

ROUTE_REMAP = 'remap'
ROUTE_REBUILD = 'rebuild'

_PROJECT = 'Metadata/project_settings.config'
_MODEL_SETTINGS = 'Metadata/model_settings.config'
_PRUSA_CONFIG = 'Metadata/Slic3r_PE.config'
_MODEL = '3D/3dmodel.model'
_PRUSA_MODEL = 'Metadata/Slic3r_PE_model.config'
_RANGES = 'Metadata/layer_config_ranges.xml'
# The PrusaSlicer roles an object with no extruder of its own prints with
# (absent means extruder 1). OrcaSlicer has no such fallback: an object with
# no extruder prints filament 1, so the remap route writes the role filament
# onto those objects, and needs the three roles to agree.
_PRUSA_OBJECT_ROLES = ('perimeter_extruder', 'infill_extruder', 'solid_infill_extruder')
# PrusaSlicer print-wide role extruders. 0 means the object's own extruder;
# 1 is the slicer default PrusaSlicer writes for every project, and an
# object extruder overrides it, so only a value past 1 is a reference the
# rebuilt export (which reads no Slic3r_PE.config) would lose.
_PRUSA_ROLE_KEYS = ('perimeter_extruder', 'infill_extruder', 'solid_infill_extruder',
                    'wipe_tower_extruder')


class RemapRefused(RuntimeError):
    """The job only renumbers filaments, but neither route can slice it as confirmed."""


def _plate_count(source):
    with zipfile.ZipFile(source) as archive:
        if _MODEL_SETTINGS not in archive.namelist():
            return 0
        return len(re.findall(rb'<plate\b', archive.read(_MODEL_SETTINGS)))


def _slicer_project(source):
    """'bambu' (Bambu Studio / OrcaSlicer project), 'prusa', or None for a bare 3MF."""
    with zipfile.ZipFile(source) as archive:
        names = set(archive.namelist())
    if _PROJECT in names:
        return 'bambu'
    if _PRUSA_CONFIG in names:
        return 'prusa'
    return None


def plan(source, decisions, *, item_shifts=None, object_ids=None, scale=None, fit_by_object=None,
         identity_tools=False, layer_mode=None, slicer='orca'):
    """{'route': 'remap', 'filament_map': {source filament: job filament}} or
    {'route': 'rebuild', 'reason': why the job is not a pure renumbering}.

    The job filament numbers are the rebuild's compacted tools, 1-based: the
    job's slots in ascending order. Nothing is read beyond the source's
    member list, plate records and inventory; nothing is written.
    """
    source = Path(source)

    def rebuild(reason):
        return {'route': ROUTE_REBUILD, 'reason': reason}

    if source.suffix.lower() != '.3mf' or not zipfile.is_zipfile(source):
        return rebuild('the source is not a 3MF project')
    if slicer != 'orca':
        return rebuild(f'this printer slices with {slicer}; the remap route is verified with '
                       'OrcaSlicer only')
    if identity_tools:
        return rebuild('the printer numbers filaments by toolhead (multi_tool), which the rebuilt '
                       'export writes directly')
    if layer_mode is not None:
        return rebuild('height color changes print as pauses, which only the rebuilt export writes')
    if item_shifts is not None:
        return rebuild('the job moves objects by a recorded plate merge')
    if fit_by_object is not None:
        return rebuild('the job turns objects to fit the bed')
    if scale is not None and float(scale) != 1.0:
        return rebuild('the job prints the model at a different scale')
    if object_ids is not None:
        return rebuild('the job prints a subset of the source objects (one plate of several)')
    family = _slicer_project(source)
    if family is None:
        return rebuild('the source carries no slicer project to remap')
    if _plate_count(source) > 1:
        return rebuild('the source has more than one plate, which this job merges onto one')
    if family == 'prusa':
        with zipfile.ZipFile(source) as archive:
            roles = _prusa_roles(archive.read(_PRUSA_CONFIG))
        if len(set(roles.values())) > 1:
            return rebuild('the PrusaSlicer project prints walls and infill of an object without its '
                           'own extruder on different extruders (' +
                           ', '.join(f'{k} = {v}' for k, v in roles.items()) + '), which OrcaSlicer '
                           'cannot express per object')
    from .source_colors import inventory
    manifest = inventory(source)
    if decisions.get('mode') != 'per_part' or decisions.get('source_sha256') != manifest['source_sha256']:
        return rebuild('the decisions are not exact-source per-part decisions')
    choices = decisions.get('choices') or {}
    palette = sorted({int(f['source_extruder']) for f in manifest.get('filaments') or []
                      if str(f.get('source_extruder', '')).isdigit()})
    if not palette:
        return rebuild('the source names no filament palette')
    slot_of = {}
    for number in palette:
        picks = []
        row = choices.get(f'filament:{number}')
        if isinstance(row, dict) and 'slot' in row:
            picks.append(int(row['slot']))
        for part in manifest['parts']:
            if part.get('source_extruder') == number:
                row = choices.get('part:' + part['key'])
                if isinstance(row, dict) and 'slot' in row:
                    picks.append(int(row['slot']))
        if not picks:
            return rebuild(f'palette filament {number} is not loaded for this job')
        if len(set(picks)) > 1:
            return rebuild(f'parts on palette filament {number} print from different slots')
        slot_of[number] = picks[0]
    slots = sorted(set(slot_of.values()))
    if len(slots) != len(palette):
        return rebuild('the job prints two palette filaments from one slot')
    return {'route': ROUTE_REMAP, 'family': family, 'slots': dict(slot_of),
            'filament_map': {number: slots.index(slot) + 1 for number, slot in slot_of.items()}}


def _prusa_roles(config):
    """{role key: extruder} for the object roles of a Slic3r_PE.config; absent means 1."""
    text = config.decode('utf-8', 'replace')
    roles = {}
    for key in _PRUSA_OBJECT_ROLES:
        match = re.search(rf'^\s*;\s*{key}\s*=\s*(\d+)\s*$', text, re.M)
        roles[key] = int(match.group(1)) if match else 1
    return roles


def _prusa_objects_on_role_filament(payload):
    """Slic3r_PE_model.config bytes naming the role filament on every build object with no extruder.

    Runs on the remapped package, whose roles already carry the new
    numbering. An object that names its own extruder keeps it.
    """
    import xml.etree.ElementTree as ET
    roles = set(_prusa_roles(payload[_PRUSA_CONFIG]).values())
    if len(roles) != 1:
        raise RuntimeError('PrusaSlicer object roles name different extruders')
    role = str(roles.pop() or 1)
    model = ET.fromstring(payload[_MODEL])
    build_ids = [item.get('objectid') for item in model.iter() if item.tag.split('}')[-1] == 'item']
    config = (ET.fromstring(payload[_PRUSA_MODEL]) if _PRUSA_MODEL in payload
              else ET.Element('config'))
    objects = {node.get('id'): node for node in config.findall('object')}
    for oid in build_ids:
        node = objects.get(oid)
        if node is None:
            node = objects[oid] = ET.SubElement(config, 'object', id=oid, instances_count='1')
        entry = next((m for m in node.findall('metadata') if m.get('key') == 'extruder'), None)
        if entry is None:
            ET.SubElement(node, 'metadata', type='object', key='extruder', value=role)
        elif (entry.get('value') or '0').strip() in ('', '0'):
            entry.set('value', role)
    return ET.tostring(config, encoding='utf-8', xml_declaration=True)


def _translate_build(payload, delta):
    """The 3D/3dmodel.model bytes with every build item moved by delta (x, y, z mm)."""
    data = payload[_MODEL]
    identity = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0]

    build = re.search(rb'<(?:\w+:)?build\b[^>]*>.*?</(?:\w+:)?build>', data, re.S)
    if build is None:
        raise RuntimeError('remapped package has no build to place on the bed')

    def move(match):
        item = match.group(0)
        found = re.search(rb'\stransform="([^"]*)"', item)
        values = [float(v) for v in found.group(1).split()] if found else list(identity)
        if len(values) != 12:
            raise RuntimeError('remapped package has an invalid build item transform')
        for axis in range(3):
            values[9 + axis] += float(delta[axis])
        text = ' '.join(repr(v) for v in values).encode()
        if found:
            return item[:found.start(1)] + text + item[found.end(1):]
        return re.sub(rb'^(<(?:\w+:)?item)\b', lambda m: m.group(1) + b' transform="' + text + b'"', item)

    moved = re.sub(rb'<(?:\w+:)?item\b[^>]*?/?>', move, build.group(0))
    return data[:build.start()] + moved + data[build.end():]


def _touching_items(parts):
    """'build:0 and build:1' for the first two build items whose placed footprints touch, or None.

    The same rule export_color_geometry merges items by: footprints within
    1 mm of each other on both axes count as touching, because the slicer's
    conflict check compares extrusion paths, not meshes.
    """
    boxes = {}
    for row in parts:
        item = row['build_key'].split('/')[0]
        box = row['bounds_mm']
        boxes.setdefault(item, []).append(box)
    items = list(boxes)
    touch = lambda a, b: all(a[i][0] < b[i][1] + 1 and b[i][0] < a[i][1] + 1 for i in (0, 1))  # noqa: E731
    for i, first in enumerate(items):
        for second in items[i + 1:]:
            if any(touch(a, b) for a in boxes[first] for b in boxes[second]):
                return f'{first} and {second}'
    return None


def rebuild_loses_filament_references(source):
    """Why the rebuilt export would print a filament reference differently, or None.

    The rebuild reads no project settings: print-wide wall, infill and tower
    filaments (Bambu/Orca) and role extruders (PrusaSlicer) are not carried,
    and it refuses height ranges that name an extruder.
    """
    from .package_remap import FEATURE_FILAMENT_KEYS, _filament_number
    found = []
    with zipfile.ZipFile(source) as archive:
        names = set(archive.namelist())
        if _RANGES in names and b'opt_key="extruder"' in archive.read(_RANGES):
            found.append('height-range extruder assignments in ' + _RANGES)
        if _PROJECT in names:
            try:
                project = json.loads(archive.read(_PROJECT))
            except ValueError:
                project = {}
            for key in FEATURE_FILAMENT_KEYS:
                value = _filament_number(project.get(key)) if isinstance(project, dict) else None
                if value:
                    found.append(f'{key} = {value}')
        if _PRUSA_CONFIG in names:
            text = archive.read(_PRUSA_CONFIG).decode('utf-8', 'replace')
            for key in _PRUSA_ROLE_KEYS:
                match = re.search(rf'^\s*;\s*{key}\s*=\s*(\d+)\s*$', text, re.M)
                if match and int(match.group(1)) > 1:
                    found.append(f'{key} = {int(match.group(1))}')
    if not found:
        return None
    return ('the project carries filament references the rebuilt export does not carry ('
            + ', '.join(found) + ')')


def export_per_part(source, target, decisions, *, item_shifts=None, object_ids=None, scale=None,
                    fit_by_object=None, identity_tools=False, tower_mm=None, layer_mode=None,
                    slicer='orca'):
    """(tool mapping, geometry report) for a per-part color slice; see the module docstring.

    The report is the rebuild's own shape (part records, bounds, translation,
    prime tower) plus ``slice_route``: ``{'route', 'reason'}``, and on the
    remap route ``remap`` (the remapper's report), or on a fallback
    ``remap_refused`` and ``notice``. Raises :class:`RemapRefused` naming
    both reasons when the remap refuses and the rebuild is not equivalent.
    """
    from .color_geometry import export_color_geometry
    source, target = Path(source), Path(target)
    kwargs = dict(item_shifts=item_shifts, object_ids=object_ids, scale=scale, fit_by_object=fit_by_object,
                  identity_tools=identity_tools, tower_mm=tower_mm, layer_mode=layer_mode)
    chosen = plan(source, decisions, slicer=slicer, **{k: v for k, v in kwargs.items() if k != 'tower_mm'})

    def rebuild(route):
        mapping, geometry = export_color_geometry(source, target, decisions, **kwargs)
        geometry['slice_route'] = route
        return mapping, geometry

    if chosen['route'] == ROUTE_REBUILD:
        return rebuild({'route': ROUTE_REBUILD, 'reason': chosen['reason']})
    filament_map = chosen['filament_map']
    # Every check and refusal of the rebuild, and its placement, without writing.
    # Height ranges are the remapped package's to carry (remap_package
    # translates their extruders), so the measure leaves them to this route.
    _rebuild_tools, measured = export_color_geometry(source, None, decisions, ranges_carried=True, **kwargs)
    # The remap numbering is the rebuild's compaction over every palette
    # filament: job filament n (tool n-1) is the job's n-th slot in ascending
    # order. It equals the rebuild's tools when every filament prints a part;
    # a support-only filament gets its tool here instead of being appended
    # by support_tool_pointers.
    mapping = {str(job - 1): chosen['slots'][number]
               for number, job in sorted(filament_map.items(), key=lambda kv: kv[1])}
    tool_of_slot = {slot: int(tool) for tool, slot in mapping.items()}
    from .source_colors import inventory
    manifest = inventory(source)
    part_filament = {p['key']: p['source_extruder'] for p in manifest['parts']}
    painted = bool(manifest.get('painted'))
    if any(row['slot'] not in tool_of_slot or
           (not painted and row['slot'] != chosen['slots'].get(part_filament.get(row['part_key'])))
           for row in measured['parts']):
        # Defensive: plan() and the rebuild disagree on which slot prints a
        # part. Take the rebuild, which is what the decisions describe.
        return rebuild({'route': ROUTE_REBUILD,
                        'reason': "a part prints from a slot other than its filament's, so the "
                                  'rebuild prints what the decisions describe'})
    measured['parts'] = [dict(row, tool=tool_of_slot[row['slot']]) for row in measured['parts']]
    joined = _touching_items(measured['parts'])
    if joined:
        # The rebuild joins build items whose footprints touch into one
        # assembly (#912): as separate objects of the saved project they
        # would conflict in the slicer's G-code check.
        return rebuild({'route': ROUTE_REBUILD,
                        'reason': f'separate objects touch or overlap ({joined}), and only the '
                                  'rebuilt export joins them into one assembly'})
    staged = target.with_name(target.stem + '.remap' + target.suffix)
    try:
        from .package_remap import remap_package, write_package_atomically
        try:
            report = remap_package(source, staged, filament_map)
        except RuntimeError as exc:
            refusal = str(exc)
            lost = rebuild_loses_filament_references(source)
            if lost:
                raise RemapRefused(f'the source project cannot be remapped in place: {refusal}; '
                                   f'and the rebuilt export is not equivalent: {lost}. Nothing was '
                                   'sliced') from exc
            notice = ('The source project could not be sliced as saved with its filaments '
                      f'renumbered ({refusal}). It was rebuilt from its parts instead: every part '
                      'prints on the slot shown, and project-wide filament settings such as purge '
                      'volumes come from this printer\'s profile.')
            return rebuild({'route': ROUTE_REBUILD, 'reason': 'the remapper refused the source project',
                            'remap_refused': refusal, 'notice': notice})
        with zipfile.ZipFile(staged) as archive:
            payload = {name: archive.read(name) for name in archive.namelist()}
        delta = measured.get('translation_mm') or [0.0, 0.0, 0.0]
        payload[_MODEL] = _translate_build(payload, delta)
        if chosen['family'] == 'prusa':
            payload[_PRUSA_MODEL] = _prusa_objects_on_role_filament(payload)
        write_package_atomically(target, payload, source=source)
    finally:
        staged.unlink(missing_ok=True)
    from .orca_adapter import digest
    geometry = {key: value for key, value in measured.items() if key != 'measure_only'}
    geometry.update({
        'converted_sha256': digest(target),
        'remap': {key: report[key] for key in ('mapping', 'rewritten_entries', 'preserved_entries',
                                               'dropped_entries', 'notes', 'governing_rules')},
        'slice_route': {'route': ROUTE_REMAP,
                        'reason': 'the job keeps the source project and only renumbers its filaments',
                        'filament_map': {str(k): v for k, v in sorted(filament_map.items())}},
        'policy': 'source project remapped by filament (package_remap.remap_package); every build '
                  'item moved by one rigid translation'})
    return mapping, geometry


def route_notice(provenance):
    """The message the start confirmation shows for a slice's route, or None (#1279).

    Only a fallback speaks: the remapper refused the source project by name
    and the equivalent rebuild sliced it instead. The remap route and an
    ordinary rebuild print what the confirmation already shows.
    """
    route = ((provenance or {}).get('geometry') or {}).get('slice_route') or {}
    notice = route.get('notice')
    return f'Heads up on the slice: {notice}' if notice else None
