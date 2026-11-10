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

from . import compat_registry
from pathlib import Path
from xml.etree import ElementTree as ET

PROJECT = 'Metadata/project_settings.config'
PRUSA_CONFIG = 'Metadata/Slic3r_PE.config'
PRUSA_MODEL = 'Metadata/Slic3r_PE_model.config'

# PrusaSlicer key -> Orca key for the settings Print Handy keeps (plus the speeds, kept only when
# lowered). Unmapped Prusa keys are ignored, as before. perimeter_generator is deliberately not
# mapped: PrusaSlicer defaults to Arachne, and an Orca wall_generator=arachne selects the
# print-in-place flex profile (orca_adapter.FLEX_PROFILE) for every PrusaSlicer project.
PRUSA_KEYS = dict(compat_registry.get_rule('prusa-key-translation').parameters['mapping'])
# Prusa values whose Orca spelling differs; any other value passes through to the enum check.
_PRUSA_VALUES = dict(compat_registry.get_rule('prusa-value-translation').parameters['mapping'])

ORCA_ENUMS = {key: frozenset(values) for key, values in compat_registry.get_rule('orca-enum-fallback').parameters['enums'].items()}


def _prusa(config_bytes):
    """Orca-named author settings from a PrusaSlicer Slic3r_PE.config ('; key = value' lines)."""
    raw = {}
    for line in config_bytes.decode('utf-8', 'replace').splitlines():
        line = line.strip()
        if line.startswith(';') and '=' in line:
            key, _, value = line[1:].partition('=')
            raw[key.strip()] = value.strip()
    out = {orca: _PRUSA_VALUES.get(raw[prusa], raw[prusa]) for prusa, orca in PRUSA_KEYS.items() if raw.get(prusa)}
    _threshold = compat_registry.get_rule('prusa-support-threshold-zero-drop').parameters
    if out.get(_threshold['key']) == _threshold['sentinel']:
        del out[_threshold['key']]  # PrusaSlicer's 0 is "detect automatically", not an angle
    if 'ironing' in raw:
        _ironing = compat_registry.get_rule('prusa-ironing-translation').parameters
        out['ironing_type'] = raw.get('ironing_type', _ironing['default_on_value']) if raw['ironing'] == '1' else _ironing['off_value']
    if 'support_material_style' in raw:
        _style = compat_registry.get_rule('prusa-support-style-translation').parameters
        tree = raw['support_material_style'] == _style['tree_style']
        manual = raw.get('support_material_auto') == '0'
        out['support_type'] = ('tree' if tree else 'normal') + ('(manual)' if manual else '(auto)')
        out['support_style'] = raw['support_material_style'] if raw['support_material_style'] in _style['carried_styles'] else _style['fallback_style']
    if 'external_perimeters_first' in raw:
        _seq = compat_registry.get_rule('prusa-wall-sequence-translation').parameters
        out['wall_sequence'] = _seq['on_value'] if raw['external_perimeters_first'] == '1' else _seq['off_value']
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


def _source_preset_settings(source, raw):
    """Read exact embedded preset evidence; never infer a preset from its display name."""
    source = Path(source)
    if source.suffix.lower() != '.3mf' or raw.get('format') != 'orca_project':
        return {}
    with zipfile.ZipFile(source) as archive:
        try:
            profiles = json.loads(archive.read('Metadata/filament_sequence.json'))
        except (KeyError, ValueError, UnicodeDecodeError):
            profiles = None
        if not isinstance(profiles, dict):
            return {}
        candidates = profiles.get('print', [])
        if isinstance(candidates, list):
            for candidate in candidates:
                if isinstance(candidate, dict) and isinstance(candidate.get('setting'), dict):
                    return candidate['setting']
    return {}


PROVENANCE_ARTIFACT = 'conversion_provenance.json'

_FAMILY_BY_FORMAT = {'orca_project': 'orca', 'prusaslicer_project': 'prusaslicer'}


def source_identity(source):
    """(slicer family, slicer version, format) a source records about itself.

    Family is the registry's family name ('orca', 'prusaslicer') or None when
    the source carries no slicer project at all (an STL, a core 3MF).
    """
    raw = _read_raw_project(source)
    return _FAMILY_BY_FORMAT.get(raw['format']), raw['source_version'], raw['format']


def destination_profile_identity(template, templates_dir=None):
    """{'name', 'version'}: the destination process template's own identity.

    The version is the template's version identity, from the first real source
    that carries one: the template's own `version` when it has one, else the
    vendor version recorded in the template folder's SOURCE.json when the
    templates were generated from a vendor's profiles, else the template's
    Orca `setting_id` (the preset identity Orca itself writes), else its name.
    Nothing is invented: every template carries at least a name.
    """
    template = template if isinstance(template, dict) else {}
    version = template.get('version')
    if not version and templates_dir is not None:
        try:
            source_record = json.loads((Path(templates_dir) / 'SOURCE.json').read_text())
            version = source_record.get('vendor_version') if isinstance(source_record, dict) else None
        except (OSError, ValueError):
            version = None
    if not version:
        version = template.get('setting_id') or template.get('name')
    return {'name': template.get('name'), 'version': version}


def governing_rules(applied, source_family, source_version,
                    destination_family, destination_version):
    """(rule ids, rules outside their declared versions) for a conversion.

    `applied` names the rules whose decision points the conversion actually
    reached (the caller sets a flag where each decision runs). Every applied
    rule is recorded, in registry order: the converter applies a rule
    whatever its declared versions say, so dropping one would make the record
    understate what happened. A rule whose declared family or version range
    does not cover the recorded source or destination (an unknown version
    included) is listed again in the second value, one row per side, so the
    caller can refuse readiness for it. A rule whose destination family is
    not the producing slicer's (Orca-named profile values a Bambu Studio
    fallback then read) is not checked on its destination side: no slicer of
    its declared family ran.
    """
    wanted = set(applied or ())
    unknown = wanted - {rule.id for rule in compat_registry.RULES}
    if unknown:
        raise KeyError(f'unknown compatibility rule id(s): {sorted(unknown)}')
    ids, outside = [], []
    for rule in compat_registry.RULES:
        if rule.id not in wanted:
            continue
        ids.append(rule.id)
        if rule.source_family not in ('any', source_family) or not compat_registry.applies(
                rule.source_versions, source_version):
            outside.append({'rule': rule.id, 'side': 'source', 'family': source_family,
                            'version': source_version, 'declared_family': rule.source_family,
                            'declared_versions': rule.source_versions})
        if destination_family is None or rule.destination_family in ('any', destination_family):
            if rule.destination_family not in ('any', destination_family) or not compat_registry.applies(
                    rule.destination_versions, destination_version):
                outside.append({'rule': rule.id, 'side': 'destination', 'family': destination_family,
                                'version': destination_version, 'declared_family': rule.destination_family,
                                'declared_versions': rule.destination_versions})
    return ids, outside


def version_conflict_problems(outside):
    """One readiness refusal per rule that ran outside its declared versions (#1110 req. 5, 6)."""
    return [f"compatibility rule {row['rule']} is declared for {row['declared_family']} "
            f"{row['declared_versions']} but the {row['side']} is "
            f"{row['family'] or 'an unknown slicer'} {row['version'] or '(version unknown)'}; "
            'this foreign project conversion is not verified for automated printing'
            for row in outside]


def conversion_provenance(source, target, process, author_process, author_notes, *,
                          destination_slicer_version=None, destination_profile=None,
                          governing_rules=(), rules_outside_declared_versions=()):
    """Build a per-slice record without guessing intent when source preset evidence is absent.

    Version fields record only what a real source states: the source slicer
    and profile versions come from the source 3MF's own project and embedded
    preset evidence; the destination slicer version is the producing slicer's
    own identity (read from its output by the adapter) and the destination
    profile is the template actually applied (see destination_profile_identity).
    A source that records no profile version keeps the field null and says so
    in source_profile_version_status — a fact about the source, distinct from
    a field nobody wired.
    """
    raw = _read_raw_project(source)
    source_settings = raw['settings']
    target_settings = target if isinstance(target, dict) else {}
    author_process = author_process if isinstance(author_process, dict) else {}
    project_values = raw.get('normalized_settings')
    if not isinstance(project_values, dict):
        project_values = author_process
    source_preset = _source_preset_settings(source, raw)
    # different_settings_to_system is Bambu Studio / OrcaSlicer preset
    # bookkeeping. PrusaSlicer has no such option and never writes it (none of
    # the PrusaSlicer fixtures or corpus projects carry it), and a PrusaSlicer
    # project embeds no exact source preset either, so its intent is UNKNOWN:
    # a line of that name in a Slic3r_PE.config is not evidence to read.
    changed = (source_settings.get('different_settings_to_system')
               if isinstance(source_settings, dict) else None)
    # The list holds one ';'-joined entry per preset: the print (process)
    # preset first, then each filament preset, then the printer preset. Only
    # the print entry speaks about process keys; merging the others in would
    # label a key some filament preset changed as the designer's process
    # choice. Present metadata (even an empty print entry) is evidence: it
    # says which keys differ from the system preset and that the rest do not.
    if isinstance(changed, list):
        changed = changed[0] if changed else None
    explicit = ({key.strip() for key in changed.split(';') if key.strip()}
                if isinstance(changed, str) else None)
    entries = {}
    conflicts = []
    for key in sorted(set(author_process) | set(target_settings)):
        source_value = project_values.get(key)
        target_value = target_settings.get(key)
        preset_value = source_preset.get(key)
        deliberate = ((key in explicit) if explicit is not None else
                      (source_value != preset_value if key in source_preset and source_value is not None else None))
        selected = process.get(key)
        origin = ('designer' if deliberate is True else 'inherited' if deliberate is False else 'unknown')
        reason = (('different_settings_to_system' if deliberate else
                   'not in different_settings_to_system; inherited from the source preset')
                  if explicit is not None else
                  'project differs from exact source preset' if deliberate is True else
                  'project value matches exact source preset' if deliberate is False else
                  'exact source preset comparison unavailable; intent unknown')
        entries[key] = {'source_preset_value': preset_value, 'project_value': source_value,
                        'target_value': target_value, 'selected_value': selected,
                        'origin': origin, 'reason': reason}
        if deliberate is True and source_value is not None and target_value is not None and source_value != target_value:
            conflicts.append({'key': key, 'source_value': source_value,
                              'target_value': target_value, 'selected_value': selected})
    # JOB.json economy: keep only settings the author carried or whose
    # selected value actually differs from the destination template.
    entries = {key: row for key, row in entries.items()
               if key in author_process or row['selected_value'] != row['target_value']}
    source_profile_version = source_preset.get('version') or None
    if source_profile_version:
        source_profile_status = 'recorded'
    elif raw['format'] != 'unknown':
        source_profile_status = 'not_recorded_by_source'
    else:
        source_profile_status = 'not_applicable'
    profile = destination_profile if isinstance(destination_profile, dict) else {}
    return {'schema_version': 1, 'source_format': raw['format'],
            'source_slicer_version': raw['source_version'],
            'destination_slicer_version': destination_slicer_version,
            'source_profile_version': source_profile_version,
            'source_profile_name': source_preset.get('name'),
            'source_profile_version_status': source_profile_status,
            'destination_profile_version': profile.get('version'),
            'destination_profile_name': profile.get('name'),
            'registry_version': compat_registry.REGISTRY_VERSION,
            'governing_rules': list(governing_rules or ()),
            'rules_outside_declared_versions': [dict(row) for row in rules_outside_declared_versions or ()],
            # Agrees with the rows by construction: known only when every
            # recorded setting's origin is established (designer or inherited).
            'intent_status': ('known' if (entries or explicit is not None)
                              and all(row['origin'] != 'unknown' for row in entries.values())
                              else 'unknown'),
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
    _suffix = compat_registry.get_rule('designer-lowered-speed-carry').parameters['key_suffix']
    for key, value in template.items():
        if not key.endswith(_suffix):
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
    _tree = compat_registry.get_rule('organic-tree-hybrid-conversion').parameters
    if process.get('support_style') != _tree['from_style']:
        return None
    try:
        zero_gap = float(str(process.get(_tree['gap_key'], _tree['gap_default'])).strip()) == 0
    except ValueError:
        zero_gap = False
    if not (zero_gap or variable_layer_height(source)):
        return None
    process['support_style'] = _tree['to_style']
    why = 'a zero top Z distance' if zero_gap else 'variable layer height'
    return f'organic tree supports slice as Tree Hybrid with {why}'
