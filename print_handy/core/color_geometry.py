"""Source-bound, instance-preserving multipart core 3MF for Orca."""
import io
import copy
import json
import math
import re
from pathlib import Path
import posixpath
import xml.etree.ElementTree as ET
import zipfile

from .. import settings
from . import source_colors
from .source_colors import inventory
from .orca_adapter import PROCESS_KEYS, _check_rotation, _rotate, digest, transform_mirrors
from .threemf_canonical import require_millimeter

CORE = 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'
IDENTITY = [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]


def _kept_part_settings(settings):
    """The per-volume author settings a rebuilt export may carry (#867): the kept
    author-intent keys (orca_adapter.PROCESS_KEYS) with values Orca accepts.
    Everything else about a volume is the job profiles' business; the slicer
    copy's adoption pass applies the same restriction to authored files."""
    if not settings:
        return []
    from . import author_settings
    return [(key, settings[key]) for key in sorted(settings)
            if key in PROCESS_KEYS and not author_settings.invalid_enum(key, settings[key])]


def _dropped_part_settings(settings):
    """The per-volume author setting keys a rebuilt export does NOT carry (#1110 C9).

    The allowlist restriction itself is unchanged (registry rule
    author-intent-allowlist); what Phase C changes is that the drop is named
    in the export report instead of silent, so a dropped feature/support
    assignment is a recorded decision, never an invisible one.
    """
    if not settings:
        return []
    kept = {key for key, _value in _kept_part_settings(settings)}
    return sorted(key for key in settings if key not in kept)
def _rewound(mesh):
    """A copy of a 3MF mesh element with every triangle's winding reversed.

    Used when a leaf is flattened through a mirroring transform: the copied
    triangles get world-space vertices, so their order must be reversed to
    keep the solid's normals pointing outward (orca_adapter.transform_mirrors).
    """
    mesh = copy.deepcopy(mesh)
    for node in mesh.iter():
        if node.tag.split('}')[-1] == 'triangle':
            v2, v3 = node.get('v2'), node.get('v3')
            node.set('v2', v3)
            node.set('v3', v2)
    return mesh


def _write_part_settings(pc, settings):
    for key, value in _kept_part_settings(settings):
        ET.SubElement(pc, 'metadata', key=key, value=str(value))


def export_color_geometry(source, target, decisions, item_shifts=None, object_ids=None, scale=None, fit_by_object=None,
                          identity_tools=False, tower_mm=None, layer_mode=None):
    """object_ids limits export to those build objects (one authored plate);
    scale shrinks the selection uniformly before bed centering.

    identity_tools (#346, multi_tool printers): a part assigned to position
    n exports as slicer tool n-1 — the toolhead itself — instead of the
    compacted tool order the IFS merge uses, and the returned mapping is
    the identity {tool: position}.

    fit_by_object ({object_id: {'rotation': [9]}}) turns individual
    exported objects in the bed plane (#221 R2b, the merge path's analogue
    of export_single_color_plate's per-object fit): each named object's
    leaves — including its cutter volumes, so a turned object stays rigid
    — rotate by the recorded matrix about the world origin, and the
    object's item_shifts entry then places the rotated bounds, which is
    the placement the merge plan proved. It therefore requires
    item_shifts, refuses a resized export (the shifts were planned at
    authored size), an unknown or unexported object, a non-authored
    scale, and any rotation that is not a rigid rotation.
    """
    if layer_mode not in (None, 'pause'):
        raise RuntimeError("layer_mode must be None or 'pause'")
    manifest = inventory(source)
    full_parts = manifest['parts']
    if object_ids is not None:
        object_ids = {str(o) for o in object_ids}
        manifest = dict(manifest, parts=[p for p in full_parts if str(p['object_id']) in object_ids])
        if not manifest['parts']:
            raise RuntimeError('object subset selects no source parts')
    scale = float(scale) if scale is not None else 1.0
    if not scale > 0:
        raise RuntimeError('painted export scale must be greater than 0')
    if decisions.get('source_sha256') != manifest['source_sha256'] or decisions.get('mode') != 'per_part':
        raise RuntimeError('multicolor geometry requires exact-source per_part decisions')
    # Height ranges (Phase C, #1110 criterion 4): this rebuilt export renumbers
    # object ids, and layer_config_ranges.xml keys its ranges by source object
    # id, so its extruder options cannot be translated honestly here. A source
    # carrying them is refused by name; the package remapper
    # (package_remap.remap_package) and the single-color copy, which preserve
    # object ids, remap the same member instead. Copy paths are unaffected.
    with zipfile.ZipFile(source) as _archive:
        source_members = _archive.namelist()
        if 'Metadata/layer_config_ranges.xml' in source_members and \
                b'opt_key="extruder"' in _archive.read('Metadata/layer_config_ranges.xml'):
            raise RuntimeError('source carries Metadata/layer_config_ranges.xml height-range '
                               'extruder assignments, which the rebuilt geometry export cannot '
                               'translate (it renumbers object ids); refusing rather than drop '
                               'them silently')
    painted = bool(manifest['painted'])
    choices = decisions['choices']
    schema = decisions.get('schema')
    if schema not in (None, 'part_slots_v1'):
        raise RuntimeError('unknown color decision schema')
    planner = source_colors.canonical_prompts if schema == 'part_slots_v1' else source_colors.prompts
    expected = {row['key'] for row in planner(dict(manifest, parts=full_parts))}
    if set(choices) != expected:
        raise RuntimeError('incomplete part/source-filament decisions')
    # Parts and their source palette rows are distinct UI decisions, but a part
    # cannot silently disagree with the palette assignment it refers to.
    if painted:
        # Triangle paint: each painted state N is source filament N; unpainted
        # triangles and state 0 use the part's own extruder. Map every state
        # actually present to a contiguous tool for the chosen physical slot.
        from . import painted_geometry
        used = set()
        base_state = {}
        for part in manifest['parts']:
            row = painted_geometry.leaf_states(source, part)
            # Fully painted parts never print their base extruder; give the
            # object-level extruder the part's dominant paint instead.
            dominant = max(row['states'], key=row['states'].get) if row['states'] else None
            base_state[part['key']] = part['source_extruder'] if row['unpainted'] or not dominant else dominant
            used |= {state or part['source_extruder'] for state in row['states']}
            if row['unpainted']:
                used.add(part['source_extruder'])
        missing = sorted(n for n in used if f'filament:{n}' not in choices)
        if missing:
            raise RuntimeError(f'painted source filament(s) {missing} have no slot choice')
        slots = sorted({choices[f'filament:{n}']['slot'] for n in used})
        # A height-based color change to a filament no triangle paints with
        # still needs its slot in the tool numbering (#870).
        for row in manifest.get('layer_changes') or []:
            sel = choices.get(f"filament:{row.get('source_extruder')}")
            if sel is not None:
                slots = sorted(set(slots) | {sel['slot']})
        paint_table = {n: (choices[f'filament:{n}']['slot'] if identity_tools
                           else slots.index(choices[f'filament:{n}']['slot']) + 1) for n in used}
    else:
        # A support-designation row (#867) names the supports' spool, not the
        # part's, so it may differ from the part choices for the same source
        # extruder. A height-change row for that extruder is not exempt: it
        # prints the parts themselves (#870).
        layer_exts = {row.get('source_extruder') for row in manifest.get('layer_changes') or []}
        designated = manifest.get('support_filaments') or {}
        support_only = {e for e in (designated.get('base'), designated.get('interface'))
                        if e and e not in layer_exts}
        for part in manifest['parts']:
            palette = choices.get(f"filament:{part['source_extruder']}")
            if (palette and part['source_extruder'] not in support_only
                    and palette != choices['part:' + part['key']]):
                raise RuntimeError('part selection contradicts its source filament selection')
        slots = sorted({choices['part:' + part['key']]['slot'] for part in manifest['parts']})
        # Height-based color changes (#870): the slots their filaments were
        # picked for are tools of this job too, not only the parts' slots.
        for row in manifest.get('layer_changes') or []:
            sel = choices.get(f"filament:{row.get('source_extruder')}")
            if sel is not None:
                slots = sorted(set(slots) | {sel['slot']})
    if not slots:
        raise RuntimeError('multipart selection requires at least one slot')
    if not identity_tools and len(slots) > 4:
        # The compacted mapping is bounded by the four physical IFS slots. An
        # identity (toolhead) mapping is bounded by the machine's toolheads
        # instead — five on the XL — which intake and the adapter's tool
        # mapping contract already enforce.
        raise RuntimeError('multipart selection requires one to four IFS slots')
    mapping = ({str(slot - 1): slot for slot in slots} if identity_tools
               else {str(i): slot for i, slot in enumerate(slots)})
    with zipfile.ZipFile(source) as archive:
        models = {}
        for name in archive.namelist():
            if name.endswith('.model'):
                root = ET.fromstring(archive.read(name))
                require_millimeter(root)
                models[name] = (root, {n.get('id'): n for n in root if n.tag.split('}')[-1] == 'resources' for n in n if n.tag.split('}')[-1] == 'object'})
        leaves = []

        def transform(node):
            values = list(map(float, node.get('transform', ' '.join(map(str, IDENTITY))).split()))
            if len(values) != 12 or not all(math.isfinite(v) for v in values):
                raise RuntimeError('invalid 3MF transform')
            return values

        def visit(name, oid, chain, key):
            obj = models[name][1][oid]
            mesh = next((n for n in obj if n.tag.split('}')[-1] == 'mesh'), None)
            if mesh is not None:
                vertices = next(n for n in mesh if n.tag.split('}')[-1] == 'vertices')
                points = []
                for vertex in vertices:
                    point = tuple(float(vertex.get(k)) for k in ('x', 'y', 'z'))
                    for matrix in chain:
                        point = tuple(sum(point[j] * matrix[j * 3 + i] for j in range(3)) + matrix[9 + i] for i in range(3))
                    if not all(math.isfinite(v) for v in point):
                        raise RuntimeError('nonfinite 3MF vertex')
                    points.append(point)
                if not points:
                    raise RuntimeError('empty part geometry')
                # Paint subdivision trees depend on triangle vertex order.
                # Refuse rather than emit an inside-out solid or guess a
                # paint-tree permutation when flattening a reflected leaf.
                if transform_mirrors(chain):
                    if painted:
                        raise RuntimeError('mirrored painted geometry cannot be flattened safely; '
                                           'export an unmirrored source or a geometry-only model')
                    mesh = _rewound(mesh)
                leaves.append((key, mesh, points))
                return
            components = next(n for n in obj if n.tag.split('}')[-1] == 'components')
            for i, component in enumerate(components):
                path = next((v for k, v in component.attrib.items() if k.split('}')[-1] == 'path'), None)
                path = path.lstrip('/') if path and path.startswith('/') else posixpath.normpath(posixpath.join(posixpath.dirname(name), path)) if path else name
                visit(path, component.get('objectid'), [transform(component)] + chain, f'{key}/{i}')

        root = models['3D/3dmodel.model'][0]
        build = next(n for n in root if n.tag.split('}')[-1] == 'build')
        for i, item in enumerate(build):
            if object_ids is not None and item.get('objectid') not in object_ids:
                continue
            if item.get('printable', '1') != '1':
                raise RuntimeError('nonprintable build item requires explicit selection')
            path = next((v for k, v in item.attrib.items() if k.split('}')[-1] == 'path'), None)
            path = path.lstrip('/') if path and path.startswith('/') else posixpath.normpath(posixpath.join('3D', path)) if path else '3D/3dmodel.model'
            visit(path, item.get('objectid'), [transform(item)], f'build:{i}')
    if scale != 1.0:
        leaves[:] = [(k, m, [tuple(c * scale for c in pt) for pt in pts]) for k, m, pts in leaves]
    rotations = {}
    if fit_by_object is not None:
        if item_shifts is None:
            raise RuntimeError('a per-object fit requires the recorded item shifts that place the turned objects')
        if scale != 1.0:
            raise RuntimeError('a per-object fit cannot be combined with a resized export')
        if not isinstance(fit_by_object, dict) or not fit_by_object:
            raise RuntimeError('fit_by_object must be a mapping of object id to a per-object fit')
        exported = {str(p['object_id']) for p in manifest['parts']}
        for raw_id, raw_fit in fit_by_object.items():
            if str(raw_id) not in exported:
                raise RuntimeError(f'per-object fit names object(s) that are not exported: {raw_id}')
            if not isinstance(raw_fit, dict):
                raise RuntimeError('per-object fit must be a mapping with a rotation')
            try:
                fit_scale = float(raw_fit.get('scale') or 1.0)
            except (TypeError, ValueError) as exc:
                raise RuntimeError('per-object fit scale must be numeric') from exc
            if fit_scale != 1.0:
                raise RuntimeError('per-object fit must keep the authored scale')
            try:
                matrix = _check_rotation(raw_fit.get('rotation'))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f'per-object fit {exc}') from exc
            if matrix is None:
                raise RuntimeError('per-object fit requires a rotation')
            rotations[str(raw_id)] = matrix
    if rotations:
        object_of_leaf = {p['instance_key']: str(p['object_id']) for p in manifest['parts']}
        object_of_leaf.update({c['instance_key']: str(c['object_id'])
                               for c in manifest.get('cutters', [])})
        leaves[:] = [(key, mesh, [_rotate(point, rotations[object_of_leaf[key]]) for point in points]
                      if object_of_leaf.get(key) in rotations else points)
                     for key, mesh, points in leaves]
    # Negative/modifier volumes ride along inside their own object, never as a
    # separate solid (a solid cutter collides with its body: Orca -101).
    cutter_rows = {c['instance_key']: c for c in manifest.get('cutters', [])
                   if object_ids is None or str(c['object_id']) in object_ids}
    cut_leaves = [leaf for leaf in leaves if leaf[0] in cutter_rows]
    leaves[:] = [leaf for leaf in leaves if leaf[0] not in cutter_rows]
    if len(leaves) != len(manifest['parts']):
        raise RuntimeError('geometry and selection inventory disagree')
    bounds = [(min(p[a] for _, _, ps in leaves for p in ps), max(p[a] for _, _, ps in leaves for p in ps)) for a in range(3)]
    delta = [110 - sum(bounds[a]) / 2 for a in (0, 1)] + [-bounds[2][0]]
    if item_shifts is None:
        delta = _tower_friendly_delta(bounds, delta, tower=tower_mm)
    if item_shifts is not None and {str(k) for k in item_shifts} != {str(p['object_id']) for p in manifest['parts']}:
        raise RuntimeError('item shifts must name exactly the source objects')
    tag = lambda name: '{' + CORE + '}' + name
    ET.register_namespace('', CORE)
    model = ET.Element(tag('model'), unit='millimeter')
    resources = ET.SubElement(model, tag('resources'))
    config = ET.Element('config')
    # One plate object per source build leaf. A single assembly makes Orca
    # treat every part as one object, so the wipe tower cannot sit between them
    # and slicing returns -101 (placeable areas / gcode conflict).
    plate = ET.SubElement(config, 'plate')
    ET.SubElement(plate, 'metadata', key='plater_id', value='1')
    build = ET.SubElement(model, tag('build'))
    records = []
    part_bounds = []
    settings_by_mesh = {}
    group_of = {}
    # Group per source build item (not object id: repeated instances of one
    # object stay separate). Parts of one build item share space by design;
    # exported as separate Orca objects they overlap and fail as G-code
    # conflicts, and auto-arrange scatters them into the tower lane. Build
    # items whose footprints overlap or touch (an inlay seated in a body that
    # the author kept as its own item, #912) are one assembly for the same
    # reason, so they are merged too (union-find over items).
    item_of = lambda instance_key: instance_key.split('/')[0]
    part_by_key = {p['instance_key']: p for p in manifest['parts']}
    leaf_boxes = {}
    for key, _mesh, points in leaves:
        # Footprints are compared where the leaves land, because per-object
        # shifts can move touching items apart or together.
        leaf_move = ([float(v) for v in item_shifts[str(part_by_key[key]['object_id'])]]
                     if item_shifts is not None else delta)
        leaf_boxes.setdefault(item_of(key), []).append(
            [(min(p[a] for p in points) + leaf_move[a], max(p[a] for p in points) + leaf_move[a]) for a in (0, 1)])
    # Orca's conflict checker uses extrusion paths, so touching or near-touching
    # footprints (within one perimeter-ish gap) also count as overlapping.
    touch = lambda a, b: all(a[i][0] < b[i][1] + 1 and b[i][0] < a[i][1] + 1 for i in (0, 1))
    overlapping_items = {item for item, boxes in leaf_boxes.items()
                         if any(touch(a, b) for i, a in enumerate(boxes) for b in boxes[i + 1:])}
    parent = {item: item for item in leaf_boxes}

    def find(item):
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    # Negative volumes subtract from every part of their Orca object, so an
    # item that carries cutters is never merged with another item: its cutter
    # would also cut the other item's geometry (a plug seated in a cut recess
    # would vanish). Such an item keeps its own assembly.
    cut_items = {item_of(cutter_rows[k]['instance_key']) for k, _, _ in cut_leaves}
    ordered_items = list(leaf_boxes)
    for i, first in enumerate(ordered_items):
        for second in ordered_items[i + 1:]:
            if first in cut_items or second in cut_items:
                continue
            if find(first) != find(second) and any(
                    touch(a, b) for a in leaf_boxes[first] for b in leaf_boxes[second]):
                parent[find(second)] = find(first)
    members = {}
    for item in ordered_items:
        members.setdefault(find(item), []).append(item)
    first_part_of = {}
    for part in manifest['parts']:
        first_part_of.setdefault(item_of(part['instance_key']), part)
    for root, items in members.items():
        if painted or len(items) > 1 or any(i in cut_items or i in overlapping_items for i in items):
            lead = first_part_of[items[0]]
            shared = {'name': lead.get('object_name') or lead['name'], 'parts': [], 'cutters': [], 'root': root}
            for item in items:
                group_of[item] = shared
    for i, ((key, mesh, points), part) in enumerate(zip(leaves, manifest['parts']), 1):
        # inventory metadata may reorder parts: match resolved build instance.
        part = next(p for p in manifest['parts'] if p['instance_key'] == key)
        if painted:
            tool = paint_table[base_state[part['key']]] - 1
            slot = choices[f'filament:{base_state[part["key"]]}']['slot'] if identity_tools else slots[tool]
        else:
            slot = choices['part:' + part['key']]['slot']
            tool = slot - 1 if identity_tools else slots.index(slot)
        mesh_id = str(i)
        if part.get('settings'):
            settings_by_mesh[mesh_id] = part['settings']
        group = group_of.get(item_of(part['instance_key']))
        obj = ET.SubElement(resources, tag('object'), id=mesh_id, type='model', name=part['name'])
        new_mesh = copy.deepcopy(mesh)
        for node in new_mesh.iter():
            node.tag = tag(node.tag.split('}')[-1])
        for tri in new_mesh.find(tag('triangles')):
            # Core per-triangle material properties name resources this rebuilt
            # package does not carry; leaving them would emit a dangling
            # reference. The face colors themselves stay disclosed through the
            # inventory's face_colors_ignored note (Phase C, #1110 C10).
            for attr in ('pid', 'p1', 'p2', 'p3'):
                tri.attrib.pop(attr, None)
            mmu_key = next((k for k in tri.attrib if k.split('}')[-1] == 'mmu_segmentation'), None)
            if painted:
                from . import painted_geometry
                code = tri.get('paint_color')
                if code:
                    tri.set('paint_color', painted_geometry.remap_code(code, paint_table))
                elif mmu_key is not None:
                    # PrusaSlicer paint, translated to the paint_color form the
                    # Orca destination reads, remapped through the same table
                    # (registry rule mmu-segmentation-paint-translation).
                    tri.set('paint_color', painted_geometry.remap_code(tri.get(mmu_key), paint_table))
            else:
                tri.attrib.pop('paint_color', None)
            if mmu_key is not None:
                del tri.attrib[mmu_key]
        placed = []
        move = [float(v) for v in item_shifts[str(part['object_id'])]] if item_shifts is not None else delta
        for vertex, point in zip(new_mesh.find(tag('vertices')), points):
            xyz = tuple(point[a] + move[a] for a in range(3))
            placed.append(xyz)
            for a, axis in enumerate(('x', 'y', 'z')):
                vertex.set(axis, repr(xyz[a]))
        obj.append(new_mesh)
        if group is not None:
            # Volumes of one source object stay one Orca object (assembly of
            # parts); separate objects would overlap and fail as G-code conflicts.
            group['parts'].append((mesh_id, part['name'], tool))
        else:
            obj_config = ET.SubElement(config, 'object', id=mesh_id)
            ET.SubElement(obj_config, 'metadata', key='name', value=part['name'])
            ET.SubElement(obj_config, 'metadata', key='extruder', value=str(tool + 1))
            pc = ET.SubElement(obj_config, 'part', id=mesh_id, subtype='normal_part')
            ET.SubElement(pc, 'metadata', key='name', value=part['name'])
            ET.SubElement(pc, 'metadata', key='extruder', value=str(tool + 1))
            _write_part_settings(pc, part.get('settings'))
            instance = ET.SubElement(plate, 'model_instance')
            ET.SubElement(instance, 'metadata', key='object_id', value=mesh_id)
            ET.SubElement(instance, 'metadata', key='instance_id', value='0')
            ET.SubElement(build, tag('item'), objectid=mesh_id, printable='1')
        box = [(min(p[a] for p in placed), max(p[a] for p in placed)) for a in range(3)]
        part_bounds.append(box)
        records.append({'part_key': part['key'], 'build_key': key, 'tool': tool, 'slot': slot,
                        'vertices': len(points), 'triangles': len(new_mesh.find(tag('triangles'))),
                        'bounds_mm': box})
    next_id = len(leaves) + 1
    cutter_bounds = {}
    for key, mesh, points in cut_leaves:
        row = cutter_rows[key]
        group = group_of[item_of(row['instance_key'])]
        move = [float(v) for v in item_shifts[str(row['object_id'])]] if item_shifts is not None else delta
        cut_id = str(next_id)
        next_id += 1
        obj = ET.SubElement(resources, tag('object'), id=cut_id, type='model', name=row['name'])
        new_mesh = copy.deepcopy(mesh)
        for node in new_mesh.iter():
            node.tag = tag(node.tag.split('}')[-1])
            node.attrib.pop('paint_color', None)
        placed = []
        for vertex, point in zip(new_mesh.find(tag('vertices')), points):
            xyz = tuple(point[a] + move[a] for a in range(3))
            placed.append(xyz)
            for a, axis in enumerate(('x', 'y', 'z')):
                vertex.set(axis, repr(xyz[a]))
        for tri in new_mesh.find(tag('triangles')):
            for attr in ('pid', 'p1', 'p2', 'p3'):
                tri.attrib.pop(attr, None)
            for key in [k for k in tri.attrib if k.split('}')[-1] == 'mmu_segmentation']:
                del tri.attrib[key]
        obj.append(new_mesh)
        if row.get('settings'):
            settings_by_mesh[cut_id] = row['settings']
        group['cutters'].append((cut_id, row['name'], row['subtype']))
        # The report's part rows carry bounds but cutter rows never did, so
        # the merged-placement proof compared part-only landed bounds
        # against all-volume authored bounds and refused sound exports
        # whose cutter reaches past the body (P2-85).
        box = [(min(p[a] for p in placed), max(p[a] for p in placed)) for a in range(3)]
        oid = str(row['object_id'])
        prev = cutter_bounds.get(oid)
        cutter_bounds[oid] = box if prev is None else [
            (min(prev[a][0], box[a][0]), max(prev[a][1], box[a][1])) for a in range(3)]
    for group in {id(g): g for g in group_of.values()}.values():
        wrapper_id = str(next_id)
        next_id += 1
        wrapper = ET.SubElement(resources, tag('object'), id=wrapper_id, type='model', name=group['name'])
        comps = ET.SubElement(wrapper, tag('components'))
        obj_config = ET.SubElement(config, 'object', id=wrapper_id)
        ET.SubElement(obj_config, 'metadata', key='name', value=group['name'])
        ET.SubElement(obj_config, 'metadata', key='extruder', value=str(group['parts'][0][2] + 1))
        for mesh_id, name, tool in group['parts']:
            ET.SubElement(comps, tag('component'), objectid=mesh_id)
            pc = ET.SubElement(obj_config, 'part', id=mesh_id, subtype='normal_part')
            ET.SubElement(pc, 'metadata', key='name', value=name)
            ET.SubElement(pc, 'metadata', key='extruder', value=str(tool + 1))
            _write_part_settings(pc, settings_by_mesh.get(mesh_id))
        for cut_id, name, subtype in group['cutters']:
            ET.SubElement(comps, tag('component'), objectid=cut_id)
            pc = ET.SubElement(obj_config, 'part', id=cut_id, subtype=subtype)
            ET.SubElement(pc, 'metadata', key='name', value=name)
            _write_part_settings(pc, settings_by_mesh.get(cut_id))
        instance = ET.SubElement(plate, 'model_instance')
        ET.SubElement(instance, 'metadata', key='object_id', value=wrapper_id)
        ET.SubElement(instance, 'metadata', key='instance_id', value='0')
        ET.SubElement(build, tag('item'), objectid=wrapper_id, printable='1')
    if group_of:
        # Tower clearance is per printed object, i.e. per assembly.
        merged = {}
        for record, box in zip(records, part_bounds):
            item = item_of(record['build_key'])
            oid = group_of[item]['root'] if item in group_of else item
            prev = merged.get(oid)
            merged[oid] = box if prev is None else [(min(prev[a][0], box[a][0]), max(prev[a][1], box[a][1])) for a in range(3)]
        part_bounds = list(merged.values())
    # Height-based color changes (#870): carry the source's per-layer tool
    # changes into the exported geometry, remapped onto the job's tools, so
    # Orca emits them at their heights. In 'pause' mode (a single-extruder job
    # whose operator chose manual swaps) each change becomes a pause instead.
    layer_payload, layer_report = None, None
    if manifest.get('layer_changes'):
        from . import layer_gcode
        with zipfile.ZipFile(source) as archive:
            xml = archive.read(layer_gcode.MEMBER)
            if object_ids is not None and 'Metadata/model_settings.config' in archive.namelist():
                plate_id = layer_gcode.plate_id_for_objects(
                    archive.read('Metadata/model_settings.config'), object_ids)
                if plate_id is None:
                    xml = None
                else:
                    xml = layer_gcode.for_plate(xml, plate_id)
        if xml is None:
            layer_report = {'kept': 0, 'dropped': len(manifest['layer_changes']),
                            'reason': 'the exported objects are not on a plate the per-layer entries name'}
        elif layer_mode == 'pause':
            layer_payload, layer_report = layer_gcode.as_pauses(xml, z_scale=scale or 1.0)
        else:
            tool_map = {}
            for row in manifest['layer_changes']:
                ext = row.get('source_extruder')
                if not ext or ext in tool_map:
                    continue
                if painted and ext in paint_table:
                    tool_map[ext] = paint_table[ext]
                    continue
                selection = choices.get(f'filament:{ext}')
                if selection is None:
                    part = next((p for p in full_parts if p.get('source_extruder') == ext), None)
                    selection = choices.get('part:' + part['key']) if part else None
                if selection is None:
                    continue
                tool_map[ext] = selection['slot'] if identity_tools else slots.index(selection['slot']) + 1
            # Phase C (#1110 C7): align the in-export path with remap_archive —
            # a tool change whose extruder has no mapping refuses, never drops
            # silently. (The member-level remap() drop behavior stays for its
            # other callers; this caller has the decisions to know better.)
            unresolved = sorted({row['source_extruder']
                                 for row in layer_gcode.tool_changes(layer_gcode.parse(xml))
                                 if row['source_extruder'] not in tool_map})
            if unresolved:
                raise RuntimeError(f'per-layer tool change(s) to source extruder(s) {unresolved} '
                                   'have no slot choice; refusing rather than drop them')
            layer_payload, layer_report = layer_gcode.remap(xml, tool_map, z_scale=scale or 1.0)
        if layer_report is not None and not layer_report.get('kept'):
            layer_payload = None
    from . import layer_gcode as _layer_gcode_module
    from .package_remap import write_package_atomically
    payload = {'[Content_Types].xml': '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>',
               '_rels/.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>',
               '3D/3dmodel.model': ET.tostring(model, encoding='utf-8', xml_declaration=True),
               'Metadata/model_settings.config': ET.tostring(config, encoding='utf-8', xml_declaration=True)}
    if layer_payload is not None:
        payload[_layer_gcode_module.MEMBER] = layer_payload
    # Staged write (Phase C, #1110 C1): a failure during the write itself can
    # no longer leave a truncated destination; the staging file validates
    # (zip integrity) before it replaces the target.
    write_package_atomically(target, payload)
    towers = prime_tower_candidates(part_bounds, tower=tower_mm)
    tower = towers[0] if towers else None  # None: a towerless plan (#832)
    dropped_part_settings = [{'part_key': row['key'], 'key': key}
                             for row in list(manifest['parts']) + list(manifest.get('cutters', []))
                             for key in _dropped_part_settings(row.get('settings'))]
    governing = ['author-intent-allowlist']
    with zipfile.ZipFile(source) as _archive:
        if any(b'mmu_segmentation' in _archive.read(name)
               for name in source_members if name.lower().endswith('.model')):
            governing.append('mmu-segmentation-paint-translation')
    return mapping, {'source_sha256': digest(source), 'converted_sha256': digest(target), 'parts': records,
                     'dropped_members': sorted(set(source_members) - set(payload)),
                     'dropped_part_settings': dropped_part_settings,
                     'governing_rules': governing,
                     **({'layer_changes': layer_report} if layer_report is not None else {}),
                     **({'cutter_bounds_by_object': cutter_bounds} if cutter_bounds else {}),
                     'build_bounds_mm': bounds, 'translation_mm': delta if item_shifts is None else None, 'merged_placement': item_shifts is not None, 'prime_tower_xy_mm': list(tower) if tower else None,
                     'prime_tower_candidates_mm': [list(t) for t in towers],
                     **({'fit_by_object': {oid: {'rotation': matrix} for oid, matrix in rotations.items()}} if rotations else {}),
                     'policy': 'all mesh leaves as independent build items; component transforms; one rigid translation; float64 XML'
                     if not rotations else
                     'all mesh leaves as independent build items; component transforms; per-object yaw rotations about the world origin, each composed with its per-object translation; float64 XML'}


def export_volume_plate(source, target, scale=1.0, rotation=None, bed=None):
    """Single-filament 3MF that keeps negative/modifier volumes inside their object.

    Same placement contract as orca_adapter.export_geometry (optional rigid
    rotation, uniform scale, center XY on ``bed`` -- the target printer's (x, y)
    bed, the 220 mm AD5X bed when omitted -- lowest printed vertex on Z0) but
    cutters stay typed volumes so Orca subtracts them instead of printing them
    as a colliding solid.
    """
    from .orca_adapter import _check_rotation, _rotate
    from . import source_colors
    scale = float(scale) if scale is not None else 1.0
    if not scale > 0:
        raise ValueError('scale must be greater than 0')
    rotation = _check_rotation(rotation)
    manifest = inventory(source)
    kind = {p['instance_key']: (p, p.get('subtype', 'normal_part'))
            for p in manifest['parts'] + manifest.get('cutters', [])}
    with zipfile.ZipFile(source) as archive:
        models = {}
        for name in archive.namelist():
            if name.endswith('.model'):
                root = ET.fromstring(archive.read(name))
                require_millimeter(root)
                models[name] = (root, {n.get('id'): n for r in root if r.tag.split('}')[-1] == 'resources'
                                       for n in r if n.tag.split('}')[-1] == 'object'})
        leaves = []

        def transform(node):
            values = list(map(float, node.get('transform', ' '.join(map(str, IDENTITY))).split()))
            if len(values) != 12 or not all(math.isfinite(v) for v in values):
                raise RuntimeError('invalid 3MF transform')
            return values

        def visit(name, oid, chain, key, item_id):
            obj = models[name][1][oid]
            mesh = next((n for n in obj if n.tag.split('}')[-1] == 'mesh'), None)
            if mesh is not None:
                verts = next(n for n in mesh if n.tag.split('}')[-1] == 'vertices')
                pts = []
                for vertex in verts:
                    pt = tuple(float(vertex.get(k)) for k in ('x', 'y', 'z'))
                    for m in chain:
                        pt = tuple(sum(pt[j] * m[j * 3 + i] for j in range(3)) + m[9 + i] for i in range(3))
                    if rotation is not None:
                        pt = _rotate(pt, rotation)
                    pts.append(tuple(c * scale for c in pt))
                # Paint codes are dropped below, so a mirrored leaf can
                # always be rewound to keep its normals outward.
                if transform_mirrors(chain):
                    mesh = _rewound(mesh)
                leaves.append((key, item_id, mesh, pts))
                return
            comps = next(n for n in obj if n.tag.split('}')[-1] == 'components')
            for i, comp in enumerate(comps):
                path = next((v for k, v in comp.attrib.items() if k.split('}')[-1] == 'path'), None)
                path = path.lstrip('/') if path and path.startswith('/') else posixpath.normpath(posixpath.join(posixpath.dirname(name), path)) if path else name
                visit(path, comp.get('objectid'), [transform(comp)] + chain, f'{key}/{i}', item_id)

        root = models['3D/3dmodel.model'][0]
        build = next(n for n in root if n.tag.split('}')[-1] == 'build')
        for i, item in enumerate(build):
            if item.get('printable', '1') != '1':
                raise RuntimeError('nonprintable build item requires explicit selection')
            path = next((v for k, v in item.attrib.items() if k.split('}')[-1] == 'path'), None)
            path = path.lstrip('/') if path and path.startswith('/') else posixpath.normpath(posixpath.join('3D', path)) if path else '3D/3dmodel.model'
            visit(path, item.get('objectid'), [transform(item)], f'build:{i}', i)
    if set(k for k, *_ in leaves) != set(kind):
        raise RuntimeError('geometry and volume inventory disagree')
    printed = [pt for k, _, _, pts in leaves if kind[k][1] == 'normal_part' for pt in pts]
    if not printed:
        raise RuntimeError('3MF contains no printable volume')
    bounds = [(min(p[a] for p in printed), max(p[a] for p in printed)) for a in range(3)]
    center = [float(v) / 2 for v in (bed or (220.0, 220.0))[:2]]
    delta = [center[a] - sum(bounds[a]) / 2 for a in (0, 1)] + [-bounds[2][0]]
    tag = lambda name: '{' + CORE + '}' + name
    ET.register_namespace('', CORE)
    model = ET.Element(tag('model'), unit='millimeter')
    resources = ET.SubElement(model, tag('resources'))
    config = ET.Element('config')
    plate = ET.SubElement(config, 'plate')
    ET.SubElement(plate, 'metadata', key='plater_id', value='1')
    out_build = ET.SubElement(model, tag('build'))
    groups, next_id, counts = {}, 1, {}
    for key, item_id, mesh, pts in leaves:
        part, subtype = kind[key]
        mesh_id = str(next_id)
        next_id += 1
        obj = ET.SubElement(resources, tag('object'), id=mesh_id, type='model', name=part['name'])
        new_mesh = copy.deepcopy(mesh)
        for node in new_mesh.iter():
            node.tag = tag(node.tag.split('}')[-1])
            node.attrib.pop('paint_color', None)
            for key in [k for k in node.attrib if k.split('}')[-1] == 'mmu_segmentation']:
                del node.attrib[key]
            if node.tag.split('}')[-1] == 'triangle':
                for attr in ('pid', 'p1', 'p2', 'p3'):
                    node.attrib.pop(attr, None)
        for vertex, pt in zip(new_mesh.find(tag('vertices')), pts):
            for a, axis in enumerate(('x', 'y', 'z')):
                vertex.set(axis, repr(pt[a] + delta[a]))
        obj.append(new_mesh)
        counts[subtype] = counts.get(subtype, 0) + 1
        groups.setdefault(item_id, {'name': part.get('object_name') or part['name'], 'vols': []})['vols'].append(
            (mesh_id, part['name'], subtype, part.get('settings')))
    for group in groups.values():
        if not any(sub == 'normal_part' for _, _, sub, _ in group['vols']):
            raise RuntimeError('object has only negative/modifier volumes')
        wrapper_id = str(next_id)
        next_id += 1
        wrapper = ET.SubElement(resources, tag('object'), id=wrapper_id, type='model', name=group['name'])
        comps = ET.SubElement(wrapper, tag('components'))
        obj_config = ET.SubElement(config, 'object', id=wrapper_id)
        ET.SubElement(obj_config, 'metadata', key='name', value=group['name'])
        ET.SubElement(obj_config, 'metadata', key='extruder', value='1')
        for mesh_id, name, subtype, settings in group['vols']:
            ET.SubElement(comps, tag('component'), objectid=mesh_id)
            pc = ET.SubElement(obj_config, 'part', id=mesh_id, subtype=subtype)
            ET.SubElement(pc, 'metadata', key='name', value=name)
            if subtype == 'normal_part':
                ET.SubElement(pc, 'metadata', key='extruder', value='1')
            _write_part_settings(pc, settings)
        instance = ET.SubElement(plate, 'model_instance')
        ET.SubElement(instance, 'metadata', key='object_id', value=wrapper_id)
        ET.SubElement(instance, 'metadata', key='instance_id', value='0')
        ET.SubElement(out_build, tag('item'), objectid=wrapper_id, printable='1')
    from .package_remap import write_package_atomically
    write_package_atomically(target, {
        '[Content_Types].xml': '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>',
        '_rels/.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>',
        '3D/3dmodel.model': ET.tostring(model, encoding='utf-8', xml_declaration=True),
        'Metadata/model_settings.config': ET.tostring(config, encoding='utf-8', xml_declaration=True)})
    placed = [(bounds[a][0] + delta[a], bounds[a][1] + delta[a]) for a in range(3)]
    res = {'volumes': counts, 'build_bounds_mm': bounds, 'translation_mm': delta, 'placed_bounds_mm': placed,
           'converted_sha256': digest(target),
           'policy': 'printed volumes plus typed negative/modifier volumes per object; rigid translation; float64 XML'}
    if scale != 1.0:
        res['uniform_scale'] = str(scale)
    if rotation is not None:
        res['rotation_matrix'] = rotation
    return res


PROD = 'http://schemas.microsoft.com/3dmanufacturing/production/2015/06'


def export_production_plate(source, target, decisions, slot_colors):
    """Copy a production 3MF and recolor the filament the plate already uses.

    Orca 2.4.2 slices this Bambu package only when every zip entry stays
    byte-identical except filament_colour. Rewriting model_settings extruder
    values aborts inside a vector access, and shortening the filament arrays
    does the same. Every plate object already names one filament index, so
    that index is recolored to the one slot the whole plate selected. A plate
    that mixes slots is refused rather than sliced as one color.
    """
    manifest = inventory(source)
    if decisions.get('source_sha256') != manifest['source_sha256'] or decisions.get('mode') != 'per_part':
        raise RuntimeError('multicolor geometry requires exact-source per_part decisions')
    choices = decisions['choices']
    expected = {row['key'] for row in source_colors.canonical_prompts(manifest)}
    if set(choices) != expected:
        raise RuntimeError('incomplete part/source-filament decisions')
    chosen = {choices['part:' + part['key']]['slot'] for part in manifest['parts']}
    if len(chosen) != 1:
        raise RuntimeError('production plate uses one filament index; mixed slots need per-object extruders')
    slot = chosen.pop()
    # The color only recolors the plate's filament label and preview. A
    # slot without a code gets its name's color or the neutral default
    # (with a WARNING) rather than a refused slice.
    from .filament_color import slot_hex
    color = slot_hex(slot, slot_colors.get(str(slot)) or slot_colors.get(slot))
    with zipfile.ZipFile(source) as archive:
        # Every member rides along, Auxiliaries/ included (Phase C, #1110 C10):
        # the copy's preservation contract is the whole member set, and what a
        # Bambu package keeps under Auxiliaries/ is not the exporter's to drop.
        names = archive.namelist()
        if 'Metadata/project_settings.config' not in names or 'Metadata/model_settings.config' not in names:
            raise RuntimeError('production plate export requires project and model settings')
        cfg = archive.read('Metadata/model_settings.config').decode()
        indexes = {meta.split('value="')[1].split('"')[0]
                   for meta in re.findall(r'<metadata key="extruder" value="\d+"', cfg)}
        if len(indexes) != 1:
            raise RuntimeError('production plate objects name more than one filament index')
        index = int(indexes.pop())
        project = archive.read('Metadata/project_settings.config')
        colours = json.loads(project)['filament_colour']
        if not 1 <= index <= len(colours):
            raise RuntimeError('plate filament index is outside the project filament list')
        current = colours[index - 1]
        payload = {name: archive.read(name) for name in names}
    # Recolor only the plate's own array entry. The same hex repeats, so match
    # by position inside the filament_colour array, not by value.
    lines = project.split(b'\n')
    start = next(i for i, line in enumerate(lines) if line.strip().startswith(b'"filament_colour"'))
    entries = [i for i in range(start + 1, len(lines)) if lines[i].strip().startswith(b'"#')]
    end = next(i for i in range(start + 1, len(lines)) if b']' in lines[i])
    entries = [i for i in entries if i < end]
    if len(entries) != len(colours):
        raise RuntimeError('filament colour list does not match the project JSON')
    target_line = entries[index - 1]
    old = ('"' + current + '"').encode()
    if old not in lines[target_line]:
        raise RuntimeError('filament colour entry was not found verbatim')
    lines[target_line] = lines[target_line].replace(old, b'"' + color.encode() + b'"', 1)
    payload['Metadata/project_settings.config'] = b'\n'.join(lines)
    from .package_remap import write_package_atomically
    write_package_atomically(target, payload)
    return {str(index - 1): slot}, {'source_sha256': digest(source), 'converted_sha256': digest(target),
                                    'filament_index': index, 'filament_colour': color,
                                    'policy': 'production package copy; one filament colour byte patch'}


_S = settings.current()
# Prime tower math assumes a square bed; use the short side so a rectangle stays safe.
BED_MM = min(_S['printer']['bed_mm'])
TOWER_MM = _S['prime_tower']['size_mm']
TOWER_MARGIN_MM = _S['prime_tower']['part_clearance_mm']
TOWER_EDGE_MM = _S['prime_tower']['edge_mm']


def effective_tower_mm(nozzle_dia=None):
    """The one prime-tower width placement and the emitted Orca profile agree on.

    The larger of the configured ``prime_tower.size_mm`` and the fitted
    nozzle's tuned profile width: a bigger nozzle purges more and needs the
    wider tower, and a configured size below that cannot shrink the tower
    the slicer will actually build. Geometry that reserves the smaller
    number approves space the emitted tower cannot occupy (P2-2, P2-34).
    """
    width = float(TOWER_MM)
    if nozzle_dia is not None:
        from .nozzle import get_tuned_nozzle_profile
        profile = get_tuned_nozzle_profile(float(nozzle_dia))
        if profile is not None:
            width = max(width, float(profile.prime_tower_width))
    return width


def _compose(outer, inner):
    """Apply inner, then outer.

    3MF transforms act on row vectors (see _apply): p' = p . A + t. Applying
    inner then outer gives A = A_inner . A_outer and t = t_inner . A_outer + t_outer.
    """
    out = [0.0] * 12
    for row in range(3):
        for col in range(3):
            out[row * 3 + col] = sum(inner[row * 3 + k] * outer[k * 3 + col] for k in range(3))
    for col in range(3):
        out[9 + col] = sum(inner[9 + k] * outer[k * 3 + col] for k in range(3)) + outer[9 + col]
    return out


def fit_transform(fit):
    """World-space 3MF transform for an operator-chosen fit, or None.

    fit is {'scale': s, 'rotation': [9 row-major, column-vector convention]},
    the same rotation orca_adapter.export_geometry applies (q = s * R * p).
    In 3MF row-vector form that is A = s * R^T with no translation, so the
    whole selection turns and scales rigidly about one common origin.
    """
    if not fit:
        return None
    from .orca_adapter import _check_rotation
    scale = float(fit.get('scale') or 1.0)
    if not scale > 0:
        raise RuntimeError('fit scale must be greater than 0')
    try:
        rotation = _check_rotation(fit.get('rotation')) or [1, 0, 0, 0, 1, 0, 0, 0, 1]
    except ValueError as exc:
        raise RuntimeError(f'fit {exc}') from exc
    if scale == 1.0 and rotation == [1, 0, 0, 0, 1, 0, 0, 0, 1]:
        return None
    return [scale * rotation[col * 3 + row] for row in range(3) for col in range(3)] + [0.0, 0.0, 0.0]


def _apply(matrix, point):
    return tuple(sum(point[j] * matrix[j * 3 + i] for j in range(3)) + matrix[9 + i] for i in range(3))


def _matrix(node):
    values = list(map(float, node.get('transform', ' '.join(map(str, IDENTITY))).split()))
    if len(values) != 12 or not all(math.isfinite(v) for v in values):
        raise RuntimeError('invalid 3MF transform')
    return values


# Parsed 3MF model parts, keyed by archive path + member CRC/size. The fit
# planners (split_fit, plate_merge) ask for one object's world points dozens
# of times per intake; re-parsing the whole model XML on every call was the
# bulk of a ~2 minute fit check on dense multi-part models. Entries are
# immutable once built and the key changes whenever the member's bytes do.
_MODEL_CACHE = {}
_MODEL_CACHE_MAX = 8


def _cached_model(archive, member, build):
    filename = getattr(archive, 'filename', None)
    if not filename:
        return build(ET.fromstring(archive.read(member)))
    info = archive.getinfo(member)
    key = (build.__name__, str(Path(filename).resolve()), member, info.CRC, info.file_size, info.compress_size)
    hit = _MODEL_CACHE.get(key)
    if hit is None:
        hit = build(ET.fromstring(archive.read(member)))
        while len(_MODEL_CACHE) >= _MODEL_CACHE_MAX:
            _MODEL_CACHE.pop(next(iter(_MODEL_CACHE)))
        _MODEL_CACHE[key] = hit
    return hit


def _model_index(root):
    """Build tree view of a model: (objects by id, build items) for the item walk."""
    objects = {node.get('id'): node for node in root.iter() if node.tag.split('}')[-1] == 'object'}
    items = [node for node in root.iter() if node.tag.split('}')[-1] == 'item']
    return objects, items


def _model_vertices(root):
    """Vertex list per object id of one model part."""
    return {
        o.get('id'): [(float(v.get('x')), float(v.get('y')), float(v.get('z')))
                      for v in o.iter() if v.tag.split('}')[-1] == 'vertex']
        for o in root.iter() if o.tag.split('}')[-1] == 'object'
    }


def _world_item_points(archive, object_ids, fit=None):
    """World-space mesh vertices per selected build item, through the component chain.

    fit (a fit_transform matrix) is applied after the item transform, so the
    points are those of the scaled/rotated group. Original transforms stay in
    the archive; callers use the points to measure or place the derived plate.
    """
    objects, build_items = _cached_model(archive, '3D/3dmodel.model', _model_index)
    model_object_points = {}
    items = []
    for item in build_items:
        if item.get('objectid') not in object_ids:
            # Plain core assembly (#716): the inventory keys each same-file
            # component as its own part, so a selected id may be a component
            # of this build item. Each such component is its own item.
            comps = [node for node in objects[item.get('objectid')].iter()
                     if node.tag.split('}')[-1] == 'component' and node.get('objectid') in object_ids
                     and not any(key.split('}')[-1] == 'path' for key in node.attrib)]
            if comps:
                if '3D/3dmodel.model' not in model_object_points:
                    model_object_points['3D/3dmodel.model'] = _cached_model(archive, '3D/3dmodel.model', _model_vertices)
                world = _compose(fit, _matrix(item)) if fit else _matrix(item)
                for comp in comps:
                    points = model_object_points['3D/3dmodel.model'].get(comp.get('objectid'), [])
                    if not points:
                        raise RuntimeError('empty part geometry')
                    matrix = _compose(world, _matrix(comp))
                    items.append([_apply(matrix, point) for point in points])
            continue
        if item.get('printable', '1') != '1':
            raise RuntimeError('nonprintable build item requires explicit selection')
        obj = objects[item.get('objectid')]
        components = [node for node in obj.iter() if node.tag.split('}')[-1] == 'component']
        item_points = []
        if not components:
            # Plain core 3MF (#716): the build item names a mesh object directly.
            if '3D/3dmodel.model' not in model_object_points:
                model_object_points['3D/3dmodel.model'] = _cached_model(archive, '3D/3dmodel.model', _model_vertices)
            points = model_object_points['3D/3dmodel.model'].get(item.get('objectid'), [])
            world = _compose(fit, _matrix(item)) if fit else _matrix(item)
            item_points.extend([_apply(world, point) for point in points])
        for component in components:
            path_attr = next((value for key, value in component.attrib.items() if key.split('}')[-1] == 'path'), None)
            # Same-file components (BambuStudio painted projects) carry no path.
            path = path_attr.lstrip('/') if path_attr else '3D/3dmodel.model'
            if path not in model_object_points:
                model_object_points[path] = _cached_model(archive, path, _model_vertices)
            comp_obj_id = component.get('objectid')
            points = model_object_points[path].get(comp_obj_id, [])
            if points:
                world = _compose(fit, _matrix(item)) if fit else _matrix(item)
                matrix = _compose(world, _matrix(component))
                item_points.extend([_apply(matrix, point) for point in points])
        if not item_points:
            raise RuntimeError('empty part geometry')
        items.append(item_points)
    if len(items) != len(object_ids):
        raise RuntimeError('geometry and selection inventory disagree')
    return items


def _world_points(archive, object_ids, fit=None):
    """Every world-space mesh vertex of the selected build items, flattened."""
    return [point for item_points in _world_item_points(archive, object_ids, fit)
            for point in item_points]


def _world_hull_xy(archive, object_ids, fit=None):
    """Convex hull of the selected build items' mesh vertices in the bed plane.

    Returns [(hull, bounds)] with one entry per build item: ``hull`` is the
    item's world-space XY convex hull as [(x, y), ...] in authored orientation
    (computed with orca_adapter's monotone-chain hull, the same one the
    full-size fit check sweeps), and ``bounds`` is the item's world AABB in
    the ``_world_bounds`` shape. The hull is a superset of the item's actual
    footprint, so hull separation implies mesh separation. Shared by the fit
    check (split_fit) and the merge planner (plate_merge) so both judge the
    same footprint.
    """
    from .orca_adapter import _hull
    out = []
    for item_points in _world_item_points(archive, object_ids, fit):
        hull = _hull([(p[0], p[1]) for p in item_points])
        bounds = [(min(p[a] for p in item_points), max(p[a] for p in item_points))
                  for a in range(3)]
        out.append((hull, bounds))
    return out


def _world_bounds(archive, object_ids, fit=None):
    """World AABB of selected build items, through their component chain.

    fit (a fit_transform matrix) is applied after the item transform, so the
    bounds are those of the scaled/rotated group. Original transforms stay in
    the archive; the caller uses this to choose the derived plate placement.
    """
    return [[(min(point[axis] for point in item_points), max(point[axis] for point in item_points))
             for axis in range(3)]
            for item_points in _world_item_points(archive, object_ids, fit)]


def export_single_color_plate(source, target, decisions, part_keys, bed=BED_MM, slot_colors=None, item_shifts=None, fit=None, fit_by_object=None):
    """Derive one single-slot production plate without deleting source XML.

    Orca 2.4.2 refuses to load a package whose object or mesh blocks were
    removed (return -6), and it emits the source filament index (T2) when
    objects keep extruder 3. This copy therefore keeps every object,
    component, mesh file and relationship byte for byte. Plate membership is
    expressed only through the build item printable flag: selected items stay
    printable="1", every other item becomes printable="0". Every object and
    part extruder becomes logical filament 1, so the slice uses T0 only. The
    physical slot stays in the report and in slice_model(filament_slot=...).
    Selected items keep their original transforms when the group already fits
    the bed; otherwise one rigid translation is applied to selected items
    only. Mixed slots and a group that cannot fit are refused.

    A triangle-painted source is chosen per source filament (painted
    part_slots_v1 decisions carry only 'filament:N' rows), so a part's slot
    is every filament its triangles use (_part_selections); the plate is
    refused unless they all name one slot, and only the paint attributes
    are dropped from the copied meshes since the plate has one filament.

    item_shifts ({object_id: [dx, dy, dz]}) gives each selected object its own
    absolute translation (one-bed merge of several source plates). The caller
    owns spacing; every placed box is still checked against the bed.

    fit ({'scale': s, 'rotation': [9]}) is the operator's verified resize or
    reorientation. It is composed into every selected build item about one
    common origin before bounds, placement and the crop check, so the group
    keeps its authored layout, only smaller or turned. Meshes stay untouched.

    fit may also carry 'placement_mm': [x, y], the bed coordinates the
    (possibly turned) group's placed bounds must start at. The group is then
    shifted rigidly so its minimum XY lands exactly there instead of being
    kept as authored or centered; the caller owns proving that placement
    avoids any declared bed exclusion, and the bounds are still checked
    against the bed here.

    fit_by_object ({object_id: {'rotation': [9]}}) turns individual selected
    objects instead of the whole plate (#221 R2): each named object's build
    items are composed with its own rotation (at authored scale; the
    rotation is about the world origin, and the object's item_shifts entry
    — or the group shift when no shifts are given — then places the rotated
    bounds, which is equivalent to turning about the object's own centre).
    It composes with item_shifts, unlike the whole-plate fit, but never
    with fit or a fit placement. A per-object rotation must land its own
    object flat: every build item of a turned object shares that object's
    rotated Z minimum within the same 0.05 mm tolerance the whole-plate
    fit uses, or the export refuses.
    """
    manifest = inventory(source)
    world = fit_transform(fit)
    placement = None
    if fit and fit.get('placement_mm') is not None:
        raw = fit['placement_mm']
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise RuntimeError('fit placement_mm must be an [x, y] pair in bed coordinates')
        try:
            placement = [float(raw[0]), float(raw[1])]
        except (TypeError, ValueError) as exc:
            raise RuntimeError('fit placement_mm must be numeric bed coordinates') from exc
        if not all(math.isfinite(v) for v in placement) or any(v < 0 for v in placement):
            raise RuntimeError('fit placement_mm must be finite and on the bed')
        if item_shifts is not None:
            raise RuntimeError('a placed plate cannot reuse merge shifts planned without the placement')
    if world is not None and item_shifts is not None:
        raise RuntimeError('a resized plate cannot reuse merge shifts planned at authored size')
    worlds = {}
    raw_fits = {}
    if fit_by_object is not None:
        if world is not None or placement is not None:
            raise RuntimeError('a per-object fit cannot be combined with a whole-plate fit or placement')
        if not isinstance(fit_by_object, dict) or not fit_by_object:
            raise RuntimeError('fit_by_object must be a mapping of object id to a per-object fit')
        for raw_id, raw_fit in fit_by_object.items():
            if not isinstance(raw_fit, dict):
                raise RuntimeError('per-object fit must be a mapping with a rotation')
            try:
                scale = float(raw_fit.get('scale') or 1.0)
            except (TypeError, ValueError) as exc:
                raise RuntimeError('per-object fit scale must be numeric') from exc
            if scale != 1.0:
                raise RuntimeError('per-object fit must keep the authored scale')
            matrix = fit_transform(raw_fit)
            if matrix is not None:
                worlds[str(raw_id)] = matrix
                raw_fits[str(raw_id)] = raw_fit
    if decisions.get('source_sha256') != manifest['source_sha256'] or decisions.get('mode') != 'per_part':
        raise RuntimeError('single-color plate requires exact-source per_part decisions')
    if decisions.get('schema') not in (None, 'part_slots_v1'):
        raise RuntimeError('unknown color decision schema')
    planner = source_colors.canonical_prompts if decisions.get('schema') == 'part_slots_v1' else source_colors.prompts
    if set(decisions['choices']) != {row['key'] for row in planner(manifest)}:
        raise RuntimeError('incomplete part/source-filament decisions')
    choices = decisions['choices']
    # Same rule as the multipart exporter: a part may not disagree with the
    # palette row it refers to, or the plate would be labeled for one slot
    # while the decisions say another. Painted part_slots_v1 decisions have
    # no part rows at all (the palette rows are the choices), so there is
    # nothing a part could contradict there. A support-designation row is
    # exempt for the same reason as in the multipart exporter (#867): it
    # names the supports' spool, and on a one-filament plate the supports
    # print in the plate's filament whatever it says.
    layer_exts = {row.get('source_extruder') for row in manifest.get('layer_changes') or []}
    designated = manifest.get('support_filaments') or {}
    support_only = {e for e in (designated.get('base'), designated.get('interface'))
                    if e and e not in layer_exts}
    for part in manifest['parts']:
        own = choices.get('part:' + part['key'])
        palette = choices.get(f"filament:{part['source_extruder']}")
        if (own is not None and palette and part['source_extruder'] not in support_only
                and palette != own):
            raise RuntimeError('part selection contradicts its source filament selection')
    wanted = list(dict.fromkeys(part_keys))
    parts = [part for part in manifest['parts'] if part['key'] in wanted]
    if len(parts) != len(wanted) or len(wanted) != len(part_keys):
        raise RuntimeError('single-color plate part keys do not match the source inventory')
    slots = {row['slot'] for part in parts for row in _part_selections(source, manifest, choices, part)}
    if len(slots) != 1:
        raise RuntimeError('single-color plate mixes physical slots')
    slot = slots.pop()
    selected = {part['object_id'] for part in parts}
    # A build object that carries a selected part must not also carry a part
    # of another slot: the printable flag selects whole build objects.
    for part in manifest['parts']:
        if part['object_id'] in selected and part['key'] not in wanted:
            raise RuntimeError('selected build object also contains an unselected part')
    # Core assemblies select per component (#716); geometry and the derived
    # package come from a copy with one build item per component.
    original_source = source
    source = _flatten_core_assemblies(source)
    with zipfile.ZipFile(source) as archive:
        names = archive.namelist()
        if '3D/3dmodel.model' not in names:
            raise RuntimeError('single-color plate requires production model settings')
        core_source = 'Metadata/model_settings.config' not in names
        own_boxes = {}
        if worlds:
            unknown_fits = sorted(set(worlds) - {str(oid) for oid in selected})
            if unknown_fits:
                raise RuntimeError('per-object fit names object(s) that are not selected: '
                                   + ','.join(unknown_fits))
            for oid in selected:
                own_boxes[oid] = _world_bounds(archive, {oid}, worlds.get(str(oid)))
            boxes = [box for oid in sorted(own_boxes, key=str) for box in own_boxes[oid]]
        else:
            boxes = _world_bounds(archive, selected, world)
        payload = {name: archive.read(name) for name in names}
    minimum = [min(box[axis][0] for box in boxes) for axis in range(3)]
    maximum = [max(box[axis][1] for box in boxes) for axis in range(3)]
    margin = _S['layout']['single_copy_edge_mm']
    # A per-object turn must land its own object flat, whichever placement
    # branch runs: every build item of a turned object shares that object's
    # rotated Z minimum (a bed-plane yaw satisfies this by construction).
    for oid, item_boxes in own_boxes.items():
        if str(oid) in worlds:
            floor = min(box[2][0] for box in item_boxes)
            if any(box[2][0] - floor > 0.05 for box in item_boxes):
                raise RuntimeError('per-object fit lifts a part off the bed')
    if item_shifts is not None:
        if {str(k) for k in item_shifts} != set(selected):
            raise RuntimeError('item shifts must name exactly the selected objects')
        shift = {str(k): [float(v) for v in vals] for k, vals in item_shifts.items()}
        with zipfile.ZipFile(source) as archive:
            if worlds:
                single = {oid: [(min(box[a][0] for box in own), max(box[a][1] for box in own))
                                 for a in range(3)]
                          for oid, own in ((oid, _world_bounds(archive, {oid}, worlds.get(str(oid))))
                                           for oid in selected)}
            else:
                single = {oid: _world_bounds(archive, {oid})[0] for oid in selected}
        moved = [[(box[a][0] + shift[oid][a], box[a][1] + shift[oid][a]) for a in range(3)]
                 for oid, box in single.items()]
        if any(b[a][0] < 0 or b[a][1] > bed - margin for b in moved for a in range(2)) or any(b[2][0] < -1e-3 for b in moved):
            raise RuntimeError('merged placement does not fit the target bed')
        if any(b[2][1] - b[2][0] > bed for b in moved):
            raise RuntimeError('part group exceeds the target bed height')
        placed = [(min(b[a][0] for b in moved), max(b[a][1] for b in moved)) for a in range(2)]
    else:
        if maximum[2] - minimum[2] > bed:
            raise RuntimeError('part group exceeds the target bed height')
        if world is not None and any(box[2][0] - minimum[2] > 0.05 for box in boxes):
            # Turning a laid-out group tips separate parts into the air.
            raise RuntimeError('resized layout lifts a part off the bed')
        if placement is not None:
            # Land the group's minimum XY exactly on the recorded placement
            # (e.g. one that avoids a declared bed exclusion, #221); Z keeps
            # the usual drop to the bed.
            shift = [placement[0] - minimum[0], placement[1] - minimum[1], -minimum[2]]
        elif all(minimum[axis] >= margin and maximum[axis] <= bed - margin for axis in range(2)) and abs(minimum[2]) < 1e-3:
            shift = [0.0, 0.0, 0.0]
        else:
            # Center, not corner: Orca adds brim/skirt outside the mesh and refuses
            # (-52) anything whose brim crosses the bed edge.
            shift = [bed / 2 - (minimum[0] + maximum[0]) / 2, bed / 2 - (minimum[1] + maximum[1]) / 2, -minimum[2]]
        placed = [(minimum[axis] + shift[axis], maximum[axis] + shift[axis]) for axis in range(2)]
        if any(high > bed - margin or low < 0 for low, high in placed):
            raise RuntimeError('part group does not fit the target bed without cropping')
        if placement is not None and any(abs(placed[axis][0] - placement[axis]) > 1e-6 for axis in range(2)):
            raise RuntimeError('placed plate does not start at the recorded placement')
    model_bytes = payload['3D/3dmodel.model']
    # A plain core 3MF (#716) has no slicer metadata: every object already
    # prints with logical filament 1. Synthesize the minimal settings the
    # checks below need; the derived package stays a core 3MF.
    settings_bytes = (_core_model_settings(model_bytes) if core_source
                      else payload['Metadata/model_settings.config'])
    # Orca 2.4.2 rejects a plate (-52, "partly inside") when a printable=0 item
    # still overlaps the bed volume. Park those items in +X beyond the bed.
    park = {}
    with zipfile.ZipFile(source) as archive:
        for oid in sorted(_build_ids(model_bytes) - selected):
            low, high = _world_bounds(archive, {oid})[0][0]
            if low < bed + 50:
                park[oid] = float(bed + 50 - low)
    model_out, kept = _select_build_items(model_bytes, selected, shift, park, world,
                                          fit_by_object=worlds or None)
    if sorted(kept) != sorted(selected):
        raise RuntimeError('derived plate dropped or duplicated a selected object')
    settings_out = _single_logical_filament(settings_bytes)
    settings_out = _collapse_to_one_plate(settings_out, selected)
    _validate_derived_plate(model_bytes, model_out, settings_out, selected)
    payload['3D/3dmodel.model'] = model_out
    if manifest['painted']:
        # Every painted region of the selected parts was proved above to map
        # to this one slot, and the plate declares one logical filament.
        # Paint codes would still name source filaments 2..N that the plate
        # no longer has, so drop them: the whole plate prints as filament 1.
        for name in list(payload):
            if name.lower().endswith('.model'):
                payload[name] = _PAINT_ATTR_RE.sub(b'', payload[name])
    if not core_source:
        payload['Metadata/model_settings.config'] = settings_out
    # The one filament this plate actually uses (Phase C, #1110 C3): when every
    # selected part prints with one source extruder, the collapsed project
    # keeps that filament's own per-filament entries, not entry 1's — and the
    # slot recolor below therefore targets that index, so the surviving entry
    # carries both the used filament's profile and the slot's verified color.
    used_extruders = {part['source_extruder'] for part in parts if part.get('source_extruder')}
    if manifest['painted']:
        from . import painted_geometry as _painted
        for part in parts:
            row = _painted.leaf_states(source, part)
            used_extruders |= {state or part['source_extruder'] for state in row['states']}
            if row['unpainted'] or not row['states']:
                used_extruders.add(part['source_extruder'])
    used_extruders.discard(None)
    keep_index = next(iter(used_extruders)) if len(used_extruders) == 1 else 1
    color = None
    if slot_colors:
        row = slot_colors.get(str(slot)) or slot_colors.get(slot) or {}
        if isinstance(row, dict) and row.get('physical_color_verified') is not True:
            from .slice_refusal import unconfirmed_slot_color
            raise unconfirmed_slot_color(slot, row, 'derived plate slot color is not physically verified')
        from .filament_color import slot_hex
        color = slot_hex(slot, row)
        if 'Metadata/project_settings.config' in payload:
            payload['Metadata/project_settings.config'] = _recolor_filament_indexes(
                payload['Metadata/project_settings.config'], {keep_index}, color)
        elif not core_source:
            raise RuntimeError('derived plate slot color requires project settings')
        # A core 3MF has no project settings; the slice gets the verified slot
        # color from slice_model(filament_slot=...) and the report (#716).
    if 'Metadata/project_settings.config' in payload:
        payload['Metadata/project_settings.config'] = _declare_one_filament(
            payload['Metadata/project_settings.config'], keep_index=keep_index)
    # No stale filament reference rides along (Phase C, #1110 C8): every other
    # member that can name a filament is remapped to the plate's one filament,
    # dropped with the drop named in the report, or named as preserved. This
    # copy never renumbers object ids, so height ranges keep their object keys
    # and only their extruder values move.
    from . import layer_gcode as _layer_gcode
    dropped_entries, preserved_unremapped, single_layer_report = [], [], None
    if _layer_gcode.MEMBER in payload:
        entries = _layer_gcode.parse(payload[_layer_gcode.MEMBER])
        tool_map = {ext: 1 for ext in _layer_gcode.tool_change_extruders(entries)}
        if tool_map:
            payload[_layer_gcode.MEMBER], single_layer_report = _layer_gcode.remap(
                payload[_layer_gcode.MEMBER], tool_map)
        else:
            # Only pauses/custom entries: they are the job's to re-derive, and
            # the member naming source heights for another plate shape drops.
            del payload[_layer_gcode.MEMBER]
            dropped_entries.append(_layer_gcode.MEMBER)
    if 'Metadata/layer_config_ranges.xml' in payload:
        payload['Metadata/layer_config_ranges.xml'] = re.sub(
            rb'(<option opt_key="extruder">)\d+(</option>)', rb'\g<1>1\g<2>',
            payload['Metadata/layer_config_ranges.xml'])
    for name in list(payload):
        if (name == 'Metadata/slice_info.config'
                or re.fullmatch(r'Metadata/plate_\d+\.gcode', name)):
            # Sliced usage and embedded G-code name the removed numbering and
            # cannot be rewritten honestly (usage is re-derived by slicing;
            # bare G-code is never rewritten).
            del payload[name]
            dropped_entries.append(name)
    if 'Metadata/Slic3r_PE.config' in payload:
        # The derived plate stays the author's package for a Prusa source
        # (the slice-time adoption translates and removes this member from
        # its own copy, #867), so it is translated to the one-filament
        # numbering instead of dropped: palette lists collapse to the kept
        # filament's entry and the support extruders follow it.
        prusa_lines = []
        for line in payload['Metadata/Slic3r_PE.config'].decode('utf-8', 'replace').splitlines():
            stripped = line.strip()
            if stripped.startswith(';') and '=' in stripped:
                key, _, value = stripped[1:].partition('=')
                key, value = key.strip(), value.strip()
                if key in ('extruder_colour', 'filament_colour', 'filament_type'):
                    items = [item.strip() for item in value.split(';')] if value else []
                    if 1 <= keep_index <= len(items):
                        line = f'; {key} = {items[keep_index - 1]}'
                elif key in ('support_material_extruder', 'support_material_interface_extruder'):
                    try:
                        designation = int(value)
                    except ValueError:
                        designation = None
                    if designation is not None and designation >= 1:
                        line = f'; {key} = {"1" if designation == keep_index else "0"}'
            prusa_lines.append(line)
        payload['Metadata/Slic3r_PE.config'] = ('\n'.join(prusa_lines) + '\n').encode()
    if 'Metadata/Slic3r_PE_model.config' in payload:
        payload['Metadata/Slic3r_PE_model.config'] = re.sub(
            rb'(<metadata[^>]*key="extruder" value=")\d+(")', rb'\g<1>1\g<2>',
            payload['Metadata/Slic3r_PE_model.config'])
    if 'Metadata/filament_sequence.json' in payload:
        # Preserved byte-for-byte on purpose: the only reader of this member
        # reads its embedded print preset as provenance evidence about the
        # *source* (Phase A's conversion provenance compares against it), and
        # no sequence semantics beyond that record are established (Phase C
        # reconciliation of audit row R26 with Phase A). Named, not silent.
        preserved_unremapped.append('Metadata/filament_sequence.json')
    from .package_remap import write_package_atomically
    write_package_atomically(target, payload)
    with zipfile.ZipFile(source) as archive:
        changed = sorted(name for name in payload if payload[name] != archive.read(name))
    return {'source_sha256': digest(original_source), 'converted_sha256': digest(target),
            'part_keys': [part['key'] for part in parts], 'object_ids': sorted(selected),
            'nonprintable_object_ids': sorted(_build_ids(model_out) - selected),
            'slot': slot, 'logical_filament': 1, 'translation_mm': shift,
            **({'fit': {'scale': float(fit.get('scale') or 1.0), 'rotation': fit.get('rotation'), 'item_transform': world,
                        **({'placement_mm': placement} if placement is not None else {})}} if world or placement is not None else {}),
            **({'fit_by_object': {oid: {'rotation': raw_fits[oid].get('rotation'),
                                          'item_transform': matrix}
                                   for oid, matrix in worlds.items()}} if worlds else {}),
            **({'placement_mm': placement} if placement is not None else {}), 'merged_placement': item_shifts is not None, 'bounds_mm': boxes,
            'placed_bounds_mm': placed, 'bed_mm': bed, 'changed_entries': changed,
            'dropped_entries': sorted(dropped_entries),
            'preserved_unremapped_entries': preserved_unremapped,
            **({'layer_changes': single_layer_report} if single_layer_report is not None else {}),
            'governing_rules': ['per-filament-array-permutation', 'support-designation-remap',
                                'height-range-extruder-remap', 'remap-member-classification'],
            'policy': 'production package copy; every object, mesh and relationship kept; unselected build items printable=0; all extruders logical 1; selected transforms kept unless the group must shift to fit',
            **({'filament_colour': color} if color is not None else {})}


_PAINT_ATTR_RE = re.compile(rb'\s+(?:[\w.-]+:)?(?:paint_color|mmu_segmentation)="[^"]*"')


def _part_selections(source, manifest, choices, part):
    """Every decision row whose slot one source part prints with.

    A part row ('part:<key>') is the part's own choice. A triangle-painted
    source is chosen per source filament instead (part_slots_v1 has only
    'filament:N' rows), and one part prints every filament its triangles
    are painted with plus its own extruder for unpainted triangles — the
    same states the multicolor exporter maps. PrusaSlicer mmu_segmentation
    paint is decoded per state by painted_geometry (Phase C), so such a
    part counts exactly the filaments its triangles name, like paint_color.
    A part with no row that names it is refused, never guessed.
    """
    rows = []
    if 'part:' + part['key'] in choices:
        rows.append(choices['part:' + part['key']])
    if manifest['painted']:
        from . import painted_geometry
        states = painted_geometry.leaf_states(source, part)
        used = {state or part['source_extruder'] for state in states['states']}
        if states['unpainted'] or not used:
            used.add(part['source_extruder'])
        missing = sorted(n for n in used if f'filament:{n}' not in choices)
        if missing:
            raise RuntimeError(f'painted source filament(s) {missing} have no slot choice')
        rows += [choices[f'filament:{n}'] for n in sorted(used)]
    if not rows:
        raise RuntimeError(f"part {part['key']} has no slot choice")
    return rows


def _flatten_core_assemblies(source):
    """Plain core 3MF assembly (#716): one build item whose same-file components
    carry different colors. The plate exporter selects whole build items, so
    give every component its own build item (item transform composed onto the
    component transform). Meshes and resources stay byte for byte. Production
    packages (with model settings) are returned unchanged.
    """
    with zipfile.ZipFile(source) as archive:
        names = archive.namelist()
        if 'Metadata/model_settings.config' in names or '3D/3dmodel.model' not in names:
            return source
        model = archive.read('3D/3dmodel.model')
        payload = {name: archive.read(name) for name in names}
    objects, items = _model_index(ET.fromstring(model))
    rows = []
    flattened = False
    for item in items:
        comps = [node for node in objects[item.get('objectid')].iter() if node.tag.split('}')[-1] == 'component']
        if comps and not any(key.split('}')[-1] == 'path' for comp in comps for key in comp.attrib):
            flattened = True
            for comp in comps:
                matrix = _compose(_matrix(item), _matrix(comp))
                rows.append((comp.get('objectid'), matrix, item.get('printable')))
        else:
            rows.append((item.get('objectid'), _matrix(item), item.get('printable')))
    if not flattened:
        return source
    def rebuild(match):
        # Emit items under the build element's own prefix: in a prefixed core
        # 3MF (<ns0:build>) a bare <item> is in no namespace and the reader
        # finds no build item (#814).
        prefix = match.group(2).decode()
        build = ''.join(
            f'<{prefix}item objectid="{oid}" transform="{" ".join(repr(v) for v in matrix)}"'
            + (f' printable="{printable}"' if printable is not None else '') + '/>'
            for oid, matrix, printable in rows)
        return match.group(1) + build.encode() + match.group(3)

    payload['3D/3dmodel.model'] = re.sub(br'(<((?:\w+:)?)build\b[^>]*>).*?(</(?:\w+:)?build>)',
                                         rebuild, model, count=1, flags=re.S)
    # In memory, not a temp file: nothing to clean up on any exit path.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out:
        for name, data in payload.items():
            out.writestr(name, data)
    return buffer


def _core_model_settings(model_bytes):
    """Minimal model settings for a plain core 3MF: logical filament 1 per build object (#716)."""
    rows = ''.join(f'<object id="{oid}"><metadata key="extruder" value="1"/></object>'
                   for oid in sorted(_build_ids(model_bytes), key=int))
    return f'<config>{rows}</config>'.encode()


def _build_ids(model_bytes):
    return {match.decode() for match in re.findall(br'<(?:\w+:)?item\b[^>]*\bobjectid="(\d+)"', model_bytes)}


def _select_build_items(model_bytes, selected, shift, park=None, fit=None, fit_by_object=None):
    """Flip printable on build items only. Resource XML and meshes stay byte for byte.

    fit is composed into selected item transforms (item first, then fit), then
    the placement shift is added to the translation. fit_by_object maps an
    object id to its own fit matrix, composed the same way for that object's
    items only (#221 R2); an object named there ignores the whole-plate fit.
    """
    kept = []

    def with_transform(line, values):
        """Write the 12 transform values into an item line, inserting the
        attribute when the item carries none: a direct-mesh core build item
        may omit it, and an omitted transform is the identity (P2-282)."""
        rendered = b' '.join(repr(v).encode() for v in values)
        transform = re.search(br'transform="([^"]+)"', line)
        if transform is not None:
            return line[:transform.start(1)] + rendered + line[transform.end(1):]
        return re.sub(br'\s*/?>$', lambda end: b' transform="' + rendered + b'"' + end.group(0), line, count=1)

    def current_values(line):
        transform = re.search(br'transform="([^"]+)"', line)
        if transform is None:
            return [float(v) for v in IDENTITY]
        values = list(map(float, transform.group(1).split()))
        if len(values) != 12:
            raise RuntimeError('invalid 3MF transform')
        return values

    def edit(match):
        line = match.group(0)
        object_id = re.search(br'\bobjectid="(\d+)"', line).group(1).decode()
        flag = b'1' if object_id in selected else b'0'
        if re.search(br'\bprintable="[^"]*"', line):
            line = re.sub(br'\bprintable="[^"]*"', b'printable="' + flag + b'"', line, count=1)
        else:
            line = re.sub(br'\s*/?>$', lambda end: b' printable="' + flag + b'"' + end.group(0), line, count=1)
        if object_id not in selected:
            move = (park or {}).get(object_id)
            if move:
                values = current_values(line)
                values[9] += move
                line = with_transform(line, values)
            return line
        kept.append(object_id)
        item_shift = shift.get(object_id, [0.0, 0.0, 0.0]) if isinstance(shift, dict) else shift
        item_fit = fit
        if fit_by_object and object_id in fit_by_object:
            item_fit = fit_by_object[object_id]
        if any(item_shift) or item_fit:
            values = current_values(line)
            if item_fit:
                values = _compose(item_fit, values)
            for axis in range(3):
                values[9 + axis] += item_shift[axis]
            line = with_transform(line, values)
        return line

    out = re.sub(br'<(?:\w+:)?item\b[^>]*>', edit, model_bytes)
    return out, kept


def _single_logical_filament(settings_bytes):
    """Point every object and part at logical filament 1. Nothing else changes."""
    return re.sub(br'(<metadata key="extruder" value=")\d+(")', br'\g<1>1\g<2>', settings_bytes)


def _collapse_to_one_plate(settings_bytes, selected):
    """Put every selected object on the first plate and drop the other plates.

    Orca decides plate membership from model_settings.config, not from build
    item positions. A merged layout that leaves an object listed on plate 2
    makes Orca see an empty plate 2 and exit with -50 (real Orca 2.4.2).
    """
    plates = list(re.finditer(br'<plate>.*?</plate>', settings_bytes, re.S))
    if len(plates) < 2:
        return settings_bytes
    instances = []
    for plate in plates:
        for inst in re.finditer(br'[ \t]*<model_instance>.*?</model_instance>\s*', plate.group(0), re.S):
            match = re.search(br'<metadata key="object_id" value="(\d+)"', inst.group(0))
            if match and match.group(1).decode() in selected:
                instances.append(inst.group(0))
    found = {re.search(br'value="(\d+)"', i).group(1).decode() for i in instances}
    if found != set(selected):
        raise RuntimeError('merged plate collapse lost or duplicated a selected object')
    first = plates[0].group(0)
    head = re.split(br'[ \t]*<model_instance>', first, maxsplit=1)[0]
    rebuilt = head + b''.join(instances) + b'  </plate>'
    out = settings_bytes[:plates[0].start()] + rebuilt
    tail = settings_bytes[plates[-1].end():]
    return out + tail


def _validate_derived_plate(model_before, model_after, settings_after, selected):
    """Fail closed before Orca sees a package whose references disagree."""
    strip = lambda data: re.sub(br'<(?:\w+:)?item\b[^>]*>', b'', data)
    if strip(model_before) != strip(model_after):
        raise RuntimeError('derived plate changed resource XML outside build items')
    root = ET.fromstring(model_after)
    items = [node for node in root.iter() if node.tag.split('}')[-1] == 'item']
    printable = {item.get('objectid') for item in items if item.get('printable', '1') == '1'}
    if printable != set(selected):
        raise RuntimeError('derived plate printable build items disagree with the selected parts')
    resources = {node.get('id') for node in root.iter() if node.tag.split('}')[-1] == 'object'}
    if not {item.get('objectid') for item in items} <= resources:
        raise RuntimeError('derived plate build item names a missing object')
    values = set(re.findall(br'<metadata key="extruder" value="(\d+)"', settings_after))
    if values != {b'1'}:
        raise RuntimeError('derived plate still names more than one logical filament')
    settings = ET.fromstring(settings_after)
    configured = {node.get('id') for node in settings.iter() if node.tag == 'object'}
    if not set(selected) <= configured:
        raise RuntimeError('derived plate lost a selected object configuration')


def _recolor_filament_indexes(project, indexes, color):
    """Rewrite filament_colour entries this plate still uses. Other bytes stay."""
    colours = json.loads(project)['filament_colour']
    lines = project.split(b'\n')
    start = next(i for i, line in enumerate(lines) if line.strip().startswith(b'"filament_colour"'))
    end = next(i for i in range(start + 1, len(lines)) if b']' in lines[i])
    entries = [i for i in range(start + 1, end) if lines[i].strip().startswith(b'"#')]
    if len(entries) != len(colours):
        raise RuntimeError('filament colour list does not match the project JSON')
    for index in indexes:
        if not 1 <= index <= len(colours):
            raise RuntimeError('plate filament index is outside the project filament list')
        target_line = entries[index - 1]
        old = ('"' + colours[index - 1] + '"').encode()
        if old not in lines[target_line]:
            raise RuntimeError('filament colour entry was not found verbatim')
        lines[target_line] = lines[target_line].replace(old, b'"' + color.encode() + b'"', 1)
    return b'\n'.join(lines)


# Project keys Orca keeps once per filament that do not carry the filament_ prefix.
_PER_FILAMENT_KEYS = re.compile(
    r'(?:filament_.*|default_filament_colour|(?:cool|eng|hot|textured|textured_cool|supertack)_plate_temp(?:_initial_layer)?'
    r'|nozzle_temperature(?:_initial_layer|_range_low|_range_high)?|chamber_temperature|idle_temperature'
    r'|fan_(?:min|max)_speed|fan_cooling_layer_time|full_fan_speed_layer|close_fan_the_first_x_layers'
    r'|overhang_fan_(?:speed|threshold)|reduce_fan_stop_start_freq|slow_down_(?:layer_time|min_speed)'
    r'|enable_pressure_advance|pressure_advance|adaptive_pressure_advance(?:_model|_overhangs|_bridges)?'
    r'|additional_cooling_fan_speed|activate_air_filtration|during_print_exhaust_fan_speed'
    r'|complete_print_exhaust_fan_speed|required_nozzle_HRC|temperature_vitrification|dont_slow_down_outer_wall'
    r'|support_material_interface_fan_speed|bed_temperature_difference|enable_overhang_bridge_fan'
    r'|overhang_threshold_participating_cooling|volumetric_speed_coefficients)')


def _declare_one_filament(project, keep_index=1):
    """Cut every per-filament array of the project settings to the one filament a derived plate runs.

    A derived plate points every object at logical filament 1 (T0), but it keeps
    the source project settings, whose per-filament arrays still list every
    filament of the source. OrcaSlicer 2.5.0-beta rejects that (exit -5,
    "filament_is_support's count 1 not equal to filament_colour's size 2") once
    the loaded filament profile contributes one entry; 2.4.2 never checked.

    The surviving entries are the *used* source filament's (keep_index, the
    source extruder the plate's parts print with — Phase C, #1110 C3): keeping
    entry 1 unconditionally would leave a plate that uses filament 2 carrying
    filament 1's temperatures, density and profile id. The flush matrix
    collapses to the kept filament's diagonal entry (0 by construction), the
    flush vector to the kept filament's own pair (the established layout is
    two entries per filament), and a support designation that named the kept
    filament now names filament 1 while one that named a removed filament
    falls back to 0, the object's own filament (registry rule
    support-designation-remap). Scalar and machine-level keys stay as
    authored. Sources that already declare one filament keep their entries;
    their designations are still normalized when keep_index is 1.
    """
    settings = json.loads(project)
    colours = settings.get('filament_colour')
    if not isinstance(colours, list) or not colours:
        return project
    count = len(colours)
    if not 1 <= keep_index <= count:
        raise RuntimeError(f'derived plate keeps source filament {keep_index}, outside the '
                           f'{count}-filament palette')
    keep = keep_index - 1
    if count >= 2:
        for key, value in settings.items():
            if not isinstance(value, list) or not value:
                continue
            if key == 'flush_volumes_vector':
                settings[key] = value[2 * keep:2 * keep + 2] if len(value) == 2 * count else value[:2]
            elif key == 'different_settings_to_system' and len(value) == count + 2:
                settings[key] = [value[0], value[1], value[-1]]
            elif _PER_FILAMENT_KEYS.fullmatch(key):
                settings[key] = [value[keep]] if len(value) == count else value[:1]
        matrix = settings.get('flush_volumes_matrix')
        if isinstance(matrix, list) and len(matrix) == count * count:
            settings['flush_volumes_matrix'] = [matrix[keep * count + keep]]
        elif isinstance(matrix, list) and matrix:
            settings['flush_volumes_matrix'] = matrix[:1]
    designation_changed = False
    for key in ('support_filament', 'support_interface_filament'):
        if key not in settings:
            continue
        try:
            designation = int(str(settings[key]).strip())
        except (TypeError, ValueError):
            continue
        if designation == keep_index:
            new = '1'
        elif designation >= 1:
            new = '0'
        else:
            continue
        if str(settings[key]) != new:
            settings[key] = new
            designation_changed = True
    if count < 2 and not designation_changed:
        return project  # already one filament, nothing to cut: bytes stay as authored
    return json.dumps(settings, indent=4).encode()


def _translate_selected_items(model_bytes, selected, shift):
    """Translate selected build items and drop unselected resource objects.

    Mesh bytes are not rewritten. An unselected object is removed so its
    component cannot keep pointing at a mesh the plate no longer carries.
    """
    lines = model_bytes.split(b'\n')
    kept = []
    out = []
    for line in lines:
        match = re.search(br'<(?:\w+:)?item\b[^>]*\bobjectid="(\d+)"[^>]*>', line)
        if match is None:
            out.append(line)
            continue
        if match.group(1).decode() not in selected:
            continue
        transform = re.search(br'transform="([^"]+)"', line)
        if transform is None:
            raise RuntimeError('selected build item has no transform')
        values = list(map(float, transform.group(1).split()))
        if len(values) != 12:
            raise RuntimeError('invalid 3MF transform')
        for axis in range(3):
            values[9 + axis] += shift[axis]
        replacement = b' '.join(repr(value).encode() for value in values)
        line = line[:transform.start(1)] + replacement + line[transform.end(1):]
        kept.append(match.group(1).decode())
        out.append(line)
    chosen = {str(object_id) for object_id in selected}

    def drop_resource(match):
        return b'' if match.group(1).decode() not in chosen else match.group(0)

    # Match one object open through its own close, including the multiline
    # component block. A greedy DOTALL match swallows every following object
    # and keeps an empty shell, which Orca then refuses to load.
    model_out = re.sub(br'<(?:\w+:)?object\b[^>]*\bid="(\d+)"[^>]*>.*?</(?:\w+:)?object>\n?', drop_resource, b'\n'.join(out), flags=re.S)
    return model_out, kept


def _drop_unselected_settings(settings_bytes, selected):
    """Remove unselected object and instance blocks without reformatting the rest."""
    text = settings_bytes.decode()
    def drop_object(match):
        return '' if match.group(1) not in selected else match.group(0)
    text = re.sub(r'<object id="(\d+)">.*?</object>\n?', drop_object, text, flags=re.S)
    def drop_instance(match):
        identity = dict(re.findall(r'<metadata key="([^"]+)" value="([^"]*)"', match.group(0)))
        return '' if identity.get('object_id') not in selected else match.group(0)
    text = re.sub(r'<model_instance>.*?</model_instance>\n?', drop_instance, text, flags=re.S)
    return text.encode()


def _drop_unused_relationships(rels_bytes, unused_models):
    """Delete relationship lines whose target mesh was removed. Other lines stay."""
    unused = {name.lstrip('/') for name in unused_models}
    kept = []
    for line in rels_bytes.split(b'\n'):
        match = re.search(br'Target="([^"]+)"', line)
        target = match.group(1).decode().lstrip('/') if match else ''
        if target not in unused:
            kept.append(line)
    return b'\n'.join(kept)


def _tower_friendly_delta(bounds, centered, bed=BED_MM, tower=None):
    """Centered placement unless the prime tower cannot fit beside it; then
    slide the whole layout (rigidly) toward a bed edge to free a tower lane."""
    if tower is not None and float(tower) <= 0:
        return centered  # towerless (#832): nothing needs a lane
    w, h = bounds[0][1] - bounds[0][0], bounds[1][1] - bounds[1][0]
    lo, hi = TOWER_EDGE_MM, bed - TOWER_EDGE_MM
    cx, cy = (bed - w) / 2, (bed - h) / 2
    candidates = [(cx, cy), (cx, lo), (cx, hi - h), (lo, cy), (hi - w, cy),
                  (lo, lo), (hi - w, lo), (lo, hi - h), (hi - w, hi - h)]
    for x0, y0 in candidates:
        if x0 < lo - 1e-6 or y0 < lo - 1e-6 or x0 + w > hi + 1e-6 or y0 + h > hi + 1e-6:
            continue
        box = [(x0, x0 + w), (y0, y0 + h), (0, 0)]
        try:
            prime_tower_xy([box], tower=tower)
        except RuntimeError:
            continue
        return [x0 - bounds[0][0], y0 - bounds[1][0], centered[2]]
    return centered


def prime_tower_candidates(part_bounds, bed=None, tower=None, margin=None,
                           limit=4, spread=20):
    """Tower origins that clear every part by margin, best first.

    bed, tower and margin default to the configured values at call time
    (``printer.bed_mm``, ``prime_tower.size_mm``, ``prime_tower.part_clearance_mm``).

    Mesh bounds are not the whole story: Orca's tree supports flare past the
    part footprint, so the best-scoring spot can still fail Orca's G-code
    conflict check. Callers retry the next candidate; candidates are kept at
    least `spread` mm apart so a retry actually moves the tower.
    """
    bed = BED_MM if bed is None else bed
    if tower is not None and float(tower) <= 0:
        # A towerless plan (#832: the printer purges into its chute) has no tower to place, so
        # there is no clearance to prove and nothing to search.
        return []
    tower = TOWER_MM if tower is None else tower
    margin = TOWER_MARGIN_MM if margin is None else margin

    def overlaps(x, y):
        for (x0, x1), (y0, y1), _z in part_bounds:
            if x < x1 + margin and x + tower > x0 - margin and y < y1 + margin and y + tower > y0 - margin:
                return True
        return False

    def clearance(x, y):
        cx, cy = x + tower / 2, y + tower / 2
        gaps = []
        for (x0, x1), (y0, y1), _z in part_bounds:
            dx = 0 if x0 <= cx <= x1 else min(abs(cx - x0), abs(cx - x1))
            dy = 0 if y0 <= cy <= y1 else min(abs(cy - y0), abs(cy - y1))
            gaps.append((dx * dx + dy * dy) ** 0.5)
        return min(gaps) if gaps else bed

    step = 5
    # Orca grows the tower past its nominal width (brim plus purge depth); a
    # tower flush with the bed edge slices to G-code in the unprintable area.
    edge = math.ceil(TOWER_EDGE_MM)
    top = bed - tower - edge
    scored = sorted(((clearance(x, y), x, y)
                     for y in range(edge, int(top) + 1, step)
                     for x in range(edge, int(top) + 1, step) if not overlaps(x, y)),
                    key=lambda c: (-c[0], c[2], c[1]))
    if not scored:
        raise RuntimeError(f'no prime tower position clears every part on the {bed:g} mm bed')
    picked = []
    for _score, x, y in scored:
        if all(abs(x - px) >= spread or abs(y - py) >= spread for px, py in picked):
            picked.append((x, y))
            if len(picked) == limit:
                break
    return picked


def prime_tower_xy(part_bounds, bed=None, tower=None, margin=None):
    """Best axis-aligned tower origin (the old fixed (15, 150) landed inside a stencil); None when towerless."""
    candidates = prime_tower_candidates(part_bounds, bed=bed, tower=tower, margin=margin, limit=1)
    return candidates[0] if candidates else None


def _identity_for_key(source, key):
    from .source_colors import component_parts
    with zipfile.ZipFile(source) as archive:
        leaf = next(p for p in component_parts(archive) if p['key'] == key)
    return leaf['model_path'], leaf['object_id']
