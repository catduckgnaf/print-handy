"""Designer intent read from a 3MF and checked before it rides over the printer template (#868).

Print Handy slices from the printer's own Orca template and copies back a short allowlist of the
author's process keys (orca_adapter.PROCESS_KEYS). This module owns everything about those author
values: reading them (Bambu/Orca project_settings.config, or a PrusaSlicer Slic3r_PE.config
translated to Orca names), keeping an author speed only where the designer lowered it below the
profile's, checking enum values against what Orca accepts, and the facts about the source the
support rules need (painted supports, variable layer height).

The rules follow ForgeBridge's published foreign-3MF conversion rules (https://forgebridge.app/);
the key and value tables here are Print Handy's own.
"""
import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

PROJECT = 'Metadata/project_settings.config'
PRUSA_CONFIG = 'Metadata/Slic3r_PE.config'
PRUSA_MODEL = 'Metadata/Slic3r_PE_model.config'

# PrusaSlicer key -> Orca key for the settings Print Handy keeps (plus the speeds, kept only when
# lowered). Unmapped Prusa keys are ignored, as before. perimeter_generator is deliberately not
# mapped: PrusaSlicer defaults to Arachne, and an Orca wall_generator=arachne selects the
# print-in-place flex profile (orca_adapter.FLEX_PROFILE) for every PrusaSlicer project.
PRUSA_KEYS = {
    'layer_height': 'layer_height', 'first_layer_height': 'initial_layer_print_height',
    'perimeters': 'wall_loops', 'fill_density': 'sparse_infill_density',
    'fill_pattern': 'sparse_infill_pattern', 'fill_angle': 'infill_direction',
    'top_fill_pattern': 'top_surface_pattern', 'bottom_fill_pattern': 'bottom_surface_pattern',
    'top_solid_layers': 'top_shell_layers', 'bottom_solid_layers': 'bottom_shell_layers',
    'top_solid_min_thickness': 'top_shell_thickness', 'bottom_solid_min_thickness': 'bottom_shell_thickness',
    'support_material': 'enable_support', 'support_material_threshold': 'support_threshold_angle',
    'support_material_buildplate_only': 'support_on_build_plate_only', 'seam_position': 'seam_position',
    'elefant_foot_compensation': 'elefant_foot_compensation', 'ironing_flowrate': 'ironing_flow',
    'ironing_spacing': 'ironing_spacing', 'brim_width': 'brim_width', 'brim_separation': 'brim_object_gap',
    'external_perimeter_speed': 'outer_wall_speed', 'perimeter_speed': 'inner_wall_speed',
    'infill_speed': 'sparse_infill_speed', 'solid_infill_speed': 'internal_solid_infill_speed',
    'top_solid_infill_speed': 'top_surface_speed', 'gap_fill_speed': 'gap_infill_speed',
    'support_material_speed': 'support_speed', 'support_material_interface_speed': 'support_interface_speed',
    'bridge_speed': 'bridge_speed', 'first_layer_speed': 'initial_layer_speed', 'travel_speed': 'travel_speed',
    'ironing_speed': 'ironing_speed', 'small_perimeter_speed': 'small_perimeter_speed',
}
# Prusa values whose Orca spelling differs; any other value passes through to the enum check.
_PRUSA_VALUES = {
    'rectilinear': 'zig-zag', 'stars': 'tri-hexagon', 'monotoniclines': 'monotonicline', 'rear': 'back',
}

_INFILL = frozenset((
    'concentric', 'zig-zag', 'grid', 'line', 'cubic', 'triangles', 'tri-hexagon', 'gyroid', 'honeycomb',
    'adaptivecubic', 'alignedrectilinear', '3dhoneycomb', 'hilbertcurve', 'archimedeanchords', 'octagramspiral',
    'supportcubic', 'lightning', 'crosshatch', 'quartercubic', 'zigzag', 'crosszag', 'lockedzag', '2dlattice',
    '2dhoneycomb', 'tpmsd'))
_SURFACE = frozenset(('concentric', 'zig-zag', 'monotonic', 'monotonicline', 'alignedrectilinear', 'hilbertcurve',
                      'archimedeanchords', 'octagramspiral'))
# Enum values Orca's CLI accepts for the kept keys (OrcaSlicer 2.4/2.5 config names). An author value
# outside its set (another slicer's spelling, a newer slicer's option) falls back to the template's.
ORCA_ENUMS = {
    'sparse_infill_pattern': _INFILL,
    'top_surface_pattern': _SURFACE,
    'bottom_surface_pattern': _SURFACE,
    'ironing_pattern': frozenset(('concentric', 'zig-zag')),
    'ironing_type': frozenset(('no ironing', 'top', 'topmost', 'solid')),
    'support_type': frozenset(('normal(auto)', 'tree(auto)', 'normal(manual)', 'tree(manual)')),
    'support_style': frozenset(('default', 'grid', 'snug', 'organic', 'tree_slim', 'tree_strong', 'tree_hybrid')),
    'seam_position': frozenset(('nearest', 'aligned', 'aligned_back', 'back', 'random')),
    'wall_sequence': frozenset(('inner wall/outer wall', 'outer wall/inner wall', 'inner-outer-inner wall')),
    'wall_generator': frozenset(('classic', 'arachne')),
    'gap_fill_target': frozenset(('everywhere', 'topbottom', 'nowhere')),
}


def _prusa(config_bytes):
    """Orca-named author settings from a PrusaSlicer Slic3r_PE.config ('; key = value' lines)."""
    raw = {}
    for line in config_bytes.decode('utf-8', 'replace').splitlines():
        line = line.strip()
        if line.startswith(';') and '=' in line:
            key, _, value = line[1:].partition('=')
            raw[key.strip()] = value.strip()
    out = {orca: _PRUSA_VALUES.get(raw[prusa], raw[prusa]) for prusa, orca in PRUSA_KEYS.items() if raw.get(prusa)}
    if out.get('support_threshold_angle') == '0':
        del out['support_threshold_angle']  # PrusaSlicer's 0 is "detect automatically", not an angle
    if 'ironing' in raw:
        out['ironing_type'] = raw.get('ironing_type', 'top') if raw['ironing'] == '1' else 'no ironing'
    if 'support_material_style' in raw:
        tree = raw['support_material_style'] == 'organic'
        manual = raw.get('support_material_auto') == '0'
        out['support_type'] = ('tree' if tree else 'normal') + ('(manual)' if manual else '(auto)')
        out['support_style'] = raw['support_material_style'] if raw['support_material_style'] in ('grid', 'snug', 'organic') else 'default'
    if 'external_perimeters_first' in raw:
        out['wall_sequence'] = 'outer wall/inner wall' if raw['external_perimeters_first'] == '1' else 'inner wall/outer wall'
    return out


def read(source):
    """The author's settings a source carries, in Orca key names; {} when it carries none.

    A Bambu/Orca project_settings.config is read as is. A PrusaSlicer project (Slic3r_PE.config)
    is translated through PRUSA_KEYS. Anything else (STL, a core 3MF) has no author settings.
    """
    import json
    source = Path(source)
    if source.suffix.lower() != '.3mf':
        return {}
    with zipfile.ZipFile(source) as archive:
        names = archive.namelist()
        if PROJECT in names:
            return json.loads(archive.read(PROJECT))
        if PRUSA_CONFIG in names:
            return _prusa(archive.read(PRUSA_CONFIG))
    return {}


def _read_raw_project(source):
    """Return unmodified slicer project settings and their source metadata, if available."""
    source = Path(source)
    if source.suffix.lower() != '.3mf':
        return {'format': 'unknown', 'settings': None, 'source_version': None}
    with zipfile.ZipFile(source) as archive:
        names = archive.namelist()
        if PROJECT in names:
            raw = json.loads(archive.read(PROJECT))
            return {'format': 'orca_project', 'settings': raw,
                    'normalized_settings': raw,
                    'source_version': raw.get('version') or raw.get('slicer_version')}
        if PRUSA_CONFIG in names:
            data = archive.read(PRUSA_CONFIG).decode('utf-8', 'replace')
            version = next((line.lstrip('; ').strip() for line in data.splitlines()
                            if line.lower().startswith('; generated by ')), None)
            return {'format': 'prusaslicer_project', 'settings': data,
                    'normalized_settings': _prusa(data.encode()),
                    'source_version': version}
    return {'format': 'unknown', 'settings': None, 'source_version': None}


def conversion_provenance(source, target, process, author_process, author_notes):
    """Build a per-slice record without guessing intent when source preset evidence is absent."""
    raw = _read_raw_project(source)
    source_settings = raw['settings']
    target_settings = target if isinstance(target, dict) else {}
    author_process = author_process if isinstance(author_process, dict) else {}
    project_values = raw.get('normalized_settings')
    if not isinstance(project_values, dict):
        project_values = author_process
    changed = (source_settings.get('different_settings_to_system')
               if isinstance(source_settings, dict) else
               [line.partition('=')[1].strip() for line in source_settings.splitlines()
                if line.strip().startswith('; different_settings_to_system =')]
               if isinstance(source_settings, str) else None)
    if isinstance(source_settings, str) and isinstance(changed, list):
        changed = changed[0].split(';') if changed else None
    if isinstance(changed, list):
        explicit = {key for row in changed if isinstance(row, str)
                    for key in row.split(';') if key}
    elif isinstance(changed, str):
        explicit = {key for key in changed.split(';') if key}
    else:
        explicit = None
    entries = {}
    conflicts = []
    for key in sorted(set(author_process) | set(target_settings)):
        source_value = project_values.get(key)
        target_value = target_settings.get(key)
        deliberate = (key in explicit) if explicit is not None else None
        selected = author_process.get(key) if deliberate is True else process.get(key)
        origin = ('designer' if deliberate is True else
                  'source_preset' if deliberate is False else 'unknown')
        reason = ('different_settings_to_system' if deliberate is True else
                  'inherited_from_source_preset' if deliberate is False else
                  'source preset or changed-setting metadata unavailable')
        entries[key] = {'source_preset_value': None, 'project_value': source_value,
                        'target_value': target_value, 'selected_value': selected,
                        'origin': origin, 'reason': reason}
        if deliberate is True and source_value is not None and target_value is not None and source_value != target_value:
            conflicts.append({'key': key, 'source_value': source_value,
                              'target_value': target_value, 'selected_value': selected})
    return {'schema_version': 1, 'source_format': raw['format'],
            'source_slicer_version': raw['source_version'], 'destination_slicer_version': None,
            'source_profile_version': None, 'destination_profile_version': None,
            'intent_status': 'known' if explicit is not None else 'unknown',
            'settings': entries, 'unresolved_conflicts': conflicts,
            'conversion_notes': list(author_notes or [])}


def prusa_project(data):
    """A PrusaSlicer project config (Slic3r_PE.config bytes) translated to Orca names."""
    return _prusa(data)


def parse_prusa_model(data):
    """Per-object settings from a PrusaSlicer Slic3r_PE_model.config member.

    Shape, from PrusaSlicer's own exports: <config><object id="N"> holds object-level
    <metadata type="object" key=".." value=".."/> entries and one <volume> per volume in
    document order, each with <metadata type="volume" .../> entries. 'name' and 'extruder'
    are structural; every other key is a Prusa config name, translated through PRUSA_KEYS
    exactly like the project-level config (a key with no translation is dropped).
    Returns {object_id: {'name', 'extruder', 'settings', 'volumes': [...]}}.
    """
    out = {}
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return out

    def entries(node):
        name = extruder = None
        settings = {}
        for meta in node.findall('metadata'):
            key, value = meta.get('key'), meta.get('value')
            if key is None or value is None:
                continue
            if key == 'name':
                name = value
            elif key == 'extruder':
                try:
                    extruder = int(value)
                except ValueError:
                    extruder = None
            elif key in PRUSA_KEYS:
                settings[PRUSA_KEYS[key]] = _PRUSA_VALUES.get(value, value)
        return name, extruder, settings

    for obj in root.findall('object'):
        oid = obj.get('id')
        if oid is None:
            continue
        name, extruder, settings = entries(obj)
        volumes = []
        for volume in obj.findall('volume'):
            v_name, v_extruder, v_settings = entries(volume)
            volumes.append({'name': v_name, 'extruder': v_extruder, 'settings': v_settings})
        out[str(oid)] = {'name': name, 'extruder': extruder, 'settings': settings, 'volumes': volumes}
    return out


def read_prusa_model(source):
    """parse_prusa_model for the member of a 3MF source; {} when it carries none."""
    source = Path(source)
    if source.suffix.lower() != '.3mf' or not zipfile.is_zipfile(source):
        return {}
    with zipfile.ZipFile(source) as archive:
        if PRUSA_MODEL not in archive.namelist():
            return {}
        return parse_prusa_model(archive.read(PRUSA_MODEL))


def support_designation(source):
    """The filament the author designated for supports, as source extruder numbers.

    {'base': int|None, 'interface': int|None}; 0 in a project names the object's own
    filament, which is no designation. Bambu/Orca projects carry support_filament /
    support_interface_filament in project_settings.config; PrusaSlicer carries
    support_material_extruder / support_material_interface_extruder in Slic3r_PE.config.
    Unparseable or absent values are None; range-checking against the palette is the
    caller's job (source_colors.inventory).
    """
    out = {'base': None, 'interface': None}
    source = Path(source)
    if source.suffix.lower() != '.3mf' or not zipfile.is_zipfile(source):
        return out

    def number(value):
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    with zipfile.ZipFile(source) as archive:
        names = archive.namelist()
        if PROJECT in names:
            try:
                project = json.loads(archive.read(PROJECT))
            except (ValueError, UnicodeDecodeError):
                project = {}
            if isinstance(project, dict):
                out['base'] = number(project.get('support_filament'))
                out['interface'] = number(project.get('support_interface_filament'))
        elif PRUSA_CONFIG in names:
            values = {}
            for line in archive.read(PRUSA_CONFIG).decode('utf-8', 'replace').splitlines():
                line = line.strip()
                if not line.startswith(';') or '=' not in line:
                    continue
                key, _, value = line[1:].partition('=')
                values[key.strip()] = value.strip()
            out['base'] = number(values.get('support_material_extruder'))
            out['interface'] = number(values.get('support_material_interface_extruder'))
    return out


def _speed(value):
    """A plain positive mm/s number, else None (percentages and 0 = "use the default" are not speeds)."""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def lowered_speeds(author, template):
    """{speed key: author value} for each template speed the designer set strictly lower (ForgeBridge 1.4.0).

    Speeds come from the printer template; a designer slowing a feature down is a deliberate choice
    for that model (fragile detail, a bridge), so it is kept. A faster author speed never is.
    """
    out = {}
    for key, value in template.items():
        if not key.endswith('_speed'):
            continue
        ours, theirs = _speed(value), _speed(author.get(key))
        if ours is not None and theirs is not None and theirs < ours:
            out[key] = str(author[key][0] if isinstance(author[key], (list, tuple)) else author[key]).strip()
    return out


def invalid_enum(key, value):
    """True when `value` is not one Orca accepts for the enum `key` (False for keys that are not enums)."""
    allowed = ORCA_ENUMS.get(key)
    return allowed is not None and str(value).strip() not in allowed


def carry(author, template, keys):
    """(process overrides, notes): the author's kept keys plus lowered speeds, enum-checked.

    An author enum value Orca does not accept is replaced by the template's value, or dropped
    (Orca's default) when the template sets none; each replacement is named in a note.
    """
    out = {k: author[k] for k in keys if k in author}
    lowered = lowered_speeds(author, template)
    out.update(lowered)
    notes = [f'kept the designer-lowered {k} {v} mm/s (profile {template[k]})' for k, v in sorted(lowered.items())]
    for key in sorted(out):
        if invalid_enum(key, out[key]):
            if key in template:
                notes.append(f'author {key} {out[key]!r} is not an OrcaSlicer value; using {template[key]!r}')
                out[key] = template[key]
            else:
                notes.append(f'author {key} {out[key]!r} is not an OrcaSlicer value; using the slicer default')
                del out[key]
    return out, notes


def _model_members(archive):
    return [name for name in archive.namelist() if name.lower().endswith('.model')]


def _mentions(archive, name, markers, chunk=1 << 20):
    """True when a member contains any marker, streamed so a large mesh is never held whole."""
    tail = b''
    keep = max(len(m) for m in markers) - 1
    with archive.open(name) as stream:
        while block := stream.read(chunk):
            window = tail + block
            if any(m in window for m in markers):
                return True
            tail = window[-keep:]
    return False


_SUPPORT_PAINT = re.compile(rb'(?:paint_supports|custom_supports)="([0-9A-Fa-f]+)"')


def painted_supports(source):
    """True when the source paints at least one support enforcer (Bambu/Orca paint_supports,
    PrusaSlicer slic3rpe:custom_supports). Blocker-only painting and unreadable codes are not."""
    from .painted_geometry import decode_states
    source = Path(source)
    if source.suffix.lower() != '.3mf':
        return False
    with zipfile.ZipFile(source) as archive:
        for name in _model_members(archive):
            if not _mentions(archive, name, (b'paint_supports="', b'custom_supports="')):
                continue
            for match in _SUPPORT_PAINT.finditer(archive.read(name)):
                try:
                    if decode_states(match.group(1).decode()).get(1):  # state 1 = enforcer, 2 = blocker
                        return True
                except ValueError:
                    continue
    return False


def variable_layer_height(source):
    """True when the source carries a layer height profile or a height range that sets layer_height."""
    source = Path(source)
    if source.suffix.lower() != '.3mf':
        return False
    with zipfile.ZipFile(source) as archive:
        names = set(archive.namelist())
        for name in ('Metadata/layer_heights_profile.txt', 'Metadata/Slic3r_PE_layer_heights_profile.txt'):
            if name in names and archive.read(name).strip():
                return True
        ranges = 'Metadata/layer_config_ranges.xml'
        return ranges in names and b'opt_key="layer_height"' in archive.read(ranges)


def tree_style(process, source):
    """A note when organic tree supports must slice as Tree Hybrid (ForgeBridge 1.4.0), else None.

    Organic trees fail with variable layer height and with a zero top Z distance (a soluble or
    zero-gap interface); Tree Hybrid handles both. `process` is changed in place.
    """
    if process.get('support_style') != 'organic':
        return None
    try:
        zero_gap = float(str(process.get('support_top_z_distance', '0.2')).strip()) == 0
    except ValueError:
        zero_gap = False
    if not (zero_gap or variable_layer_height(source)):
        return None
    process['support_style'] = 'tree_hybrid'
    why = 'a zero top Z distance' if zero_gap else 'variable layer height'
    return f'organic tree supports slice as Tree Hybrid with {why}'
