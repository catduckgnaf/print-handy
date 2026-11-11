"""Generic atomic 3MF package remapping (#1110 criterion 4, Phase C).

Status: remap_package is EXPERIMENTAL and has no production caller; only
write_package_atomically is used by the live exporters (#1155).

The geometry exporters in color_geometry rebuild or collapse a package for a
specific job. This module is the mapping-driven package remapper the Phase C
audit called for: given an explicit filament mapping {source extruder:
destination filament} (1-based both sides, a bijection onto a contiguous
1..M destination numbering), it rewrites *every* filament reference in the
package and nothing else:

- the project palette and every per-filament array are permuted by the
  mapping, and the flush (purge) matrix is permuted on both axes;
- support designations are remapped, and a designation whose filament the
  mapping removes falls back to 0 (the object's own filament) with the
  fallback named in the report — never a value past the new palette;
- object and part extruders in model_settings.config (and in a PrusaSlicer
  Slic3r_PE_model.config, whose palette lists and support extruders in
  Slic3r_PE.config are permuted the same way);
- triangle paint: paint_color states are remapped and mmu_segmentation
  states are translated to paint_color (see painted_geometry);
- per-layer tool changes (an unmapped one refuses, as in
  layer_gcode.remap), height-range extruder options and — when the
  caller supplies an object map — the object ids those ranges key on, and
  sliced usage filament ids in slice_info.config.

Object-id policy: a package remap NEVER renumbers object ids on its own.
Ids change only through the caller's explicit object_map, and every
object-keyed reference the remapper knows (height ranges, model settings,
plate instances) is translated with the same map. The rebuilt geometry
export is the one path that renumbers; it therefore refuses height ranges
rather than carrying them (see color_geometry.export_color_geometry).

Fail closed, by name, rather than guess:

- flush_volumes_vector: its layout semantics are not established in this
  repository (only "two entries per filament" in one synthetic fixture), so
  any remap that would have to permute it refuses and names the key. The
  identity mapping needs no permutation and passes it through untouched.
- Metadata/filament_sequence.json under a non-identity mapping: what real
  packages store there beyond the embedded preset record is not established,
  and the preset record is provenance evidence about the *source*, so the
  remapper refuses rather than rewrite or silently drop it.
- Embedded plate G-code (Metadata/plate_N.gcode): bare G-code is never
  rewritten in this converter, and its tool commands would contradict the
  new numbering, so its presence refuses the remap.
- Any member with no classification in MEMBER_HANDLING refuses by name.
  Classified opaque members (thumbnails, relationship parts, vendor
  records, Auxiliaries/) carry no filament reference the repository knows
  and are preserved byte-for-byte.

Atomicity (the C1 shell, on the staged-write pattern): the whole
mapping is validated before any byte is written; the new package is written
to a staging file beside the target, validated (zip integrity plus a
cross-reference consistency re-read: every filament reference in the staged
package resolves under the new numbering), and only then replaces the
target. A failure at any point leaves the source bytes and any pre-existing
destination bytes identical, and preserves unrelated members byte-for-byte
on success.
"""
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path

from . import layer_gcode, painted_geometry

PROJECT = 'Metadata/project_settings.config'
MODEL_SETTINGS = 'Metadata/model_settings.config'
PRUSA_CONFIG = 'Metadata/Slic3r_PE.config'
PRUSA_MODEL = 'Metadata/Slic3r_PE_model.config'
RANGES = 'Metadata/layer_config_ranges.xml'
SLICE_INFO = 'Metadata/slice_info.config'
FILAMENT_SEQUENCE = 'Metadata/filament_sequence.json'

# Per-filament project keys live in color_geometry (its _PER_FILAMENT_KEYS is
# the established list); imported lazily to avoid a module cycle.
def _per_filament_key(key):
    from .color_geometry import _PER_FILAMENT_KEYS
    return _PER_FILAMENT_KEYS.fullmatch(key) is not None


# OrcaSlicer per-feature filament settings (#1155): each value is a 1-based
# filament number, 0 meaning no specific filament (the object's own, or for
# the tower the filaments in play). They are filament references, so every
# export that renumbers filaments remaps them with the parts' own mapping.
FEATURE_FILAMENT_KEYS = ('wall_filament', 'sparse_infill_filament', 'solid_infill_filament',
                         'wipe_tower_filament')
# The same references as object or part metadata in model_settings.config,
# plus the per-object support designations.
OBJECT_FILAMENT_KEYS = ('wall_filament', 'sparse_infill_filament', 'solid_infill_filament',
                        'support_filament', 'support_interface_filament')
# Plate filament maps (model_settings.config <plate> metadata): one entry per
# palette filament naming the nozzle that filament prints from, so the list is
# positional and follows the palette order like any per-filament array.
PLATE_FILAMENT_MAP_KEYS = ('filament_map', 'filament_maps')
# Project list keys whose entries are not per filament, so a palette-length
# one is a coincidence of length (a 4-point bed outline on a 4-filament
# project), never an unknown per-filament array.
_NOT_PER_FILAMENT_LISTS = frozenset({
    'printable_area', 'bed_exclude_area', 'head_wrap_detect_zone', 'thumbnails',
    'wipe_tower_x', 'wipe_tower_y', 'compatible_printers', 'compatible_prints',
    'upward_compatible_machine', 'different_settings_to_system', 'flush_volumes_matrix',
    'flush_volumes_vector', 'inherits_group', 'flush_multiplier'})


def _filament_number(raw):
    """int for a filament reference value ('2', 2, ['2']), or None when it is not one."""
    if isinstance(raw, list):
        if len(raw) != 1:
            return None
        raw = raw[0]
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def remap_feature_filaments(settings, value_map, count, *, where='project'):
    """Remap the per-feature filament keys of one settings dict; returns (settings, notes).

    value_map is {source filament: destination filament}. 0 stays 0. A value
    the map covers takes its destination. A value it does not cover, one past
    the count-filament palette, or one that is not a filament number is
    UNKNOWN usage (owner ruling, #1189/#1190): never a used filament and never
    a refusal. It is written as no specific filament (0) where 0 means that
    (the tower, object and part metadata), and removed where it does not (a
    print-wide wall or infill key, whose slicer default then applies). Each
    unknown is named in the notes; nothing is dropped silently.
    """
    settings = dict(settings)
    notes = []
    keys = FEATURE_FILAMENT_KEYS if where == 'project' else OBJECT_FILAMENT_KEYS
    for key in keys:
        if key not in settings:
            continue
        raw = settings[key]
        value = _filament_number(raw)
        if value == 0:
            continue
        if value is not None and value in value_map and (count is None or value <= count):
            new = str(value_map[value])
            settings[key] = [new] if isinstance(raw, list) else new
            continue
        reason = ('is not a filament number' if value is None else
                  f'names filament {value}, outside the {count}-filament palette'
                  if count is not None and value > count else
                  f'names filament {value}, which this export does not map')
        neutral = where != 'project' or key == 'wipe_tower_filament'
        if neutral:
            settings[key] = ['0'] if isinstance(raw, list) else '0'
        else:
            del settings[key]
        notes.append({'key': key, 'where': where, 'source_value': raw, 'status': 'unknown',
                      'reason': f'{key} {reason}; recorded as unknown usage',
                      'written': settings.get(key)})
    return settings, notes


def _positional(items, positions, count):
    """items re-ordered to `positions` (1-based source indexes, destination order), or None."""
    if count is None or len(items) != count:
        return None
    return [items[source - 1] for source in positions]


def remap_filament_metadata(data, value_map, positions, count, *, member):
    """Remap the feature, support and plate filament-map metadata of a settings member.

    data is model_settings.config bytes. Object and part metadata naming a
    filament (OBJECT_FILAMENT_KEYS) are remapped through value_map with the
    rules of remap_feature_filaments (an unmapped value becomes 0, named).
    A plate filament map is positional: it keeps the entries of `positions`
    (the source filaments in destination order) when it has one entry per
    palette filament; any other shape is kept as authored and named as an
    unknown passthrough. Returns (bytes, notes).
    """
    notes = []
    key_re = '|'.join(re.escape(k) for k in OBJECT_FILAMENT_KEYS + PLATE_FILAMENT_MAP_KEYS)
    pattern = re.compile(rb'(<metadata key="(' + key_re.encode() + rb')" value=")([^"]*)(")')

    def fix(match):
        key, raw = match.group(2).decode(), match.group(3).decode()
        if key in PLATE_FILAMENT_MAP_KEYS:
            sep = ',' if ',' in raw else ' '
            items = [item for item in re.split(r'[,\s]+', raw.strip()) if item]
            reordered = _positional(items, positions, count)
            if reordered is None:
                if items and len(items) != len(positions):
                    notes.append({'key': key, 'where': member, 'source_value': raw,
                                  'status': 'unknown_passthrough',
                                  'reason': f'plate {key} has {len(items)} entries for a '
                                            f'{count}-filament palette; kept as authored'})
                return match.group(0)
            return match.group(1) + sep.join(reordered).encode() + match.group(4)
        remapped, row_notes = remap_feature_filaments({key: raw}, value_map, count, where=member)
        notes.extend(row_notes)
        return match.group(1) + str(remapped[key]).encode() + match.group(4)

    return pattern.sub(fix, data), notes


def unknown_palette_arrays(settings, count):
    """Project list keys of palette length that no per-filament rule names (#1155).

    _PER_FILAMENT_KEYS is an allowlist, so an array it does not know is not
    permuted or cut. Permuting every palette-length list would corrupt the
    ones that only happen to have that length, so such an array is passed
    through as authored and named here as an unknown passthrough instead.
    """
    if not count or count < 2:
        return []
    return sorted(key for key, value in settings.items()
                  if isinstance(value, list) and len(value) == count
                  and key not in _NOT_PER_FILAMENT_LISTS and not _per_filament_key(key)
                  and key not in FEATURE_FILAMENT_KEYS)


_PRESERVED_EXACT = frozenset({'[Content_Types].xml', '_rels/.rels', '3D/_rels/3dmodel.model.rels'})
_GCODE_MEMBER = re.compile(r'Metadata/plate_\d+\.gcode$')


def _is_preserved(name):
    """A member with no filament reference the repository knows: kept byte-for-byte."""
    if name in _PRESERVED_EXACT or name.startswith('_rels/') or name.endswith('.rels'):
        return True
    if name.startswith('Auxiliaries/'):
        return True
    lowered = name.lower()
    return lowered.endswith(('.png', '.jpg', '.jpeg', '.bin')) or name.startswith('Preview/')


_HANDLED = frozenset({PROJECT, MODEL_SETTINGS, PRUSA_CONFIG, PRUSA_MODEL, RANGES,
                      SLICE_INFO, layer_gcode.MEMBER})


def member_handling(name):
    """'remap', 'preserve', 'refuse-gcode', 'refuse-sequence' or None (unclassified)."""
    if name in _HANDLED:
        return 'remap'
    if name == FILAMENT_SEQUENCE:
        return 'refuse-sequence'
    if _GCODE_MEMBER.search(name):
        return 'refuse-gcode'
    if name.lower().endswith('.model'):
        return 'remap'  # paint attributes only; geometry bytes otherwise untouched
    if _is_preserved(name):
        return 'preserve'
    return None


def validate_mapping(mapping):
    """Normalize {source: destination} or raise: a bijection onto 1..M.

    A collision (two sources onto one destination) would have to merge two
    filaments' palette entries, matrix rows and designations into one; a gap
    would leave a destination filament with no source. Both refuse: merging
    is a collapse decision for the collapse exporters, not a permutation.
    """
    out = {}
    for source, dest in dict(mapping).items():
        try:
            source, dest = int(source), int(dest)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f'filament mapping {mapping!r} is not a mapping of filament numbers') from exc
        if source < 1 or dest < 1:
            raise RuntimeError(f'filament mapping {mapping!r} names a filament below 1')
        out[source] = dest
    if sorted(out.values()) != list(range(1, len(out) + 1)):
        raise RuntimeError(f'filament mapping {mapping!r} is not a bijection onto a contiguous '
                           'destination numbering (a collision merges filaments; a gap strands one)')
    return out


def permute_project_settings(settings, mapping, *, source_count=None):
    """Permute one project_settings dict by `mapping`; returns (settings, notes).

    Every per-filament array is reordered so destination filament M carries
    source filament N's entries wherever mapping[N] == M. A source filament
    the mapping removes must not be referenced by any per-filament array
    entry that survives — arrays are cut to the mapped sources only, in
    destination order. The flush matrix is permuted on both axes. Scalars
    that are not designations are untouched.
    """
    settings = dict(settings)
    notes = []
    palette = settings.get('filament_colour')
    count = source_count or (len(palette) if isinstance(palette, list) else None)
    if count is None:
        raise RuntimeError('project settings carry no filament palette to remap')
    missing = [n for n in range(1, count + 1) if n not in mapping]
    if missing:
        raise RuntimeError(f'filament mapping does not cover source filament(s) {missing} '
                           'named by the project palette')
    beyond = sorted(n for n in mapping if n > count)
    if beyond:
        raise RuntimeError(f'filament mapping names source filament(s) {beyond}, past the '
                           f'{count}-filament project palette')
    order = [source for source, _dest in sorted(mapping.items(), key=lambda kv: kv[1])]
    for key, value in list(settings.items()):
        if not isinstance(value, list):
            continue
        if key == 'flush_volumes_matrix':
            continue  # permuted below, on both axes
        if key == 'flush_volumes_vector':
            if any(mapping[n] != n for n in range(1, count + 1)):
                raise RuntimeError('flush_volumes_vector cannot be remapped: its layout semantics '
                                   'are not established, so no permutation of it is honest; the '
                                   'remap refuses rather than guess a memory layout')
            continue
        if _per_filament_key(key):
            if len(value) != count:
                raise RuntimeError(f'per-filament project array {key} has {len(value)} entries for '
                                   f'a {count}-filament palette; its shape is not understood')
            settings[key] = [value[source - 1] for source in order]
    matrix = settings.get('flush_volumes_matrix')
    if isinstance(matrix, list) and matrix:
        if len(matrix) != count * count:
            raise RuntimeError(f'flush_volumes_matrix has {len(matrix)} entries, not the '
                               f'{count}x{count} the palette requires; refusing to permute it')
        settings['flush_volumes_matrix'] = [
            matrix[(a - 1) * count + (b - 1)] for a in order for b in order]
    for key in ('support_filament', 'support_interface_filament'):
        if key not in settings:
            continue
        raw = str(settings[key]).strip()
        try:
            designation = int(raw)
        except ValueError:
            raise RuntimeError(f'{key} value {raw!r} is not a filament number') from None
        if designation == 0:
            continue
        if designation in mapping:
            settings[key] = str(mapping[designation])
        elif 1 <= designation <= count:
            settings[key] = '0'
            notes.append(f'{key} named removed filament {designation}; supports fall back to '
                         "the object's own filament (0)")
        else:
            raise RuntimeError(f'{key} names filament {designation}, past the {count}-filament palette')
    settings, feature_notes = remap_feature_filaments(settings, mapping, count)
    notes += [row['reason'] for row in feature_notes]
    if any(mapping[n] != n for n in range(1, count + 1)):
        notes += [f'{key} has one entry per palette filament but is not a known per-filament '
                  'array; passed through unpermuted (unknown passthrough)'
                  for key in unknown_palette_arrays(settings, count)]
    return settings, notes


def remap_ranges(data, mapping, object_map=None):
    """Remap layer_config_ranges.xml bytes: extruder options and object ids.

    Non-extruder options are settings, not filament references; they pass
    through untouched (the adoption pass restricts them separately). A range
    naming an extruder the mapping does not cover, or — when an object map
    is supplied — an object it does not name, refuses.
    """
    object_map = {str(k): str(v) for k, v in dict(object_map or {}).items()}

    def fix_object(match):
        oid = match.group(2).decode()
        if object_map:
            if oid not in object_map:
                raise RuntimeError(f'layer_config_ranges.xml names object {oid}, which the '
                                   'object map does not translate')
            return match.group(1) + object_map[oid].encode() + match.group(3)
        return match.group(0)

    out = re.sub(rb'(<object[^>]*\bid=")(\d+)(")', fix_object, data)

    def fix_option(match):
        raw = match.group(2).strip()
        try:
            extruder = int(raw)
        except ValueError:
            raise RuntimeError(f'layer_config_ranges.xml names an invalid extruder {raw!r}') from None
        if extruder not in mapping:
            raise RuntimeError(f'layer_config_ranges.xml names extruder {extruder}, which the '
                               'filament mapping does not cover')
        return match.group(1) + str(mapping[extruder]).encode() + match.group(3)

    return re.sub(rb'(<option opt_key="extruder">)([^<]*)(</option>)', fix_option, out)


def remap_slice_info(data, mapping):
    """Remap the filament ids of sliced usage records; an unmapped id refuses."""
    def fix(match):
        fid = int(match.group(2))
        if fid not in mapping:
            raise RuntimeError(f'slice_info.config names filament {fid}, which the filament '
                               'mapping does not cover')
        return match.group(1) + str(mapping[fid]).encode() + match.group(3)
    return re.sub(rb'(<filament[^>]*\bid=")(\d+)(")', fix, data)


def remap_extruder_metadata(data, mapping, *, member):
    """Remap every <metadata key="extruder" value="N"/> in a settings member.

    Value 0 on a part means "inherit the object's extruder" and is structural,
    not a filament reference; it passes through. Any other unmapped value
    refuses, naming the member.
    """
    def fix(match):
        value = int(match.group(2))
        if value == 0:
            return match.group(0)
        if value not in mapping:
            raise RuntimeError(f'{member} names extruder {value}, which the filament '
                               'mapping does not cover')
        return match.group(1) + str(mapping[value]).encode() + match.group(3)
    # Bambu/Orca writes <metadata key="extruder" value="N"/>; PrusaSlicer
    # writes <metadata type="object" key="extruder" value="N"/>. Both match.
    return re.sub(rb'(<metadata[^>]*key="extruder" value=")(\d+)(")', fix, data)


def remap_prusa_member(data, mapping):
    """Permute a Slic3r_PE.config's palette lists and remap its support extruders."""
    lines = data.decode('utf-8', 'replace').splitlines()
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(';') and '=' in stripped:
            key, _, value = stripped[1:].partition('=')
            key, value = key.strip(), value.strip()
            if key in ('extruder_colour', 'filament_colour', 'filament_type'):
                items = [item.strip() for item in value.split(';')] if value else []
                if items and len(items) == len(mapping) and set(mapping) == set(range(1, len(items) + 1)):
                    order = [s for s, _d in sorted(mapping.items(), key=lambda kv: kv[1])]
                    value = ';'.join(items[s - 1] for s in order)
                    line = f'; {key} = {value}'
            elif key in ('support_material_extruder', 'support_material_interface_extruder'):
                try:
                    designation = int(value)
                except ValueError:
                    designation = None
                if designation:
                    value = str(mapping.get(designation, 0))
                    line = f'; {key} = {value}'
        out.append(line)
    return ('\n'.join(out) + ('\n' if lines else '')).encode()


def _remap_model_member(data, mapping):
    """Paint-only rewrite of a .model member; geometry bytes otherwise untouched."""
    states = painted_geometry.paint_states_in(data)
    base = set()
    for state in states:
        if state and state not in mapping:
            raise RuntimeError(f'paint state {state} has no filament mapping')
    table = {state: mapping.get(state, state) for state in states}
    # State 0 is the part's own extruder, resolved by the settings remap, not
    # by the paint table.
    table[0] = 0
    return painted_geometry.rewrite_paint_bytes(data, table)


def _validate_consistency(payload, mapping):
    """Re-read the staged package: every filament reference resolves under the mapping."""
    dest_count = len(mapping)
    for name, data in payload.items():
        if name == MODEL_SETTINGS or name == PRUSA_MODEL:
            for raw in re.findall(rb'<metadata[^>]*key="extruder" value="(\d+)"', data):
                if int(raw) != 0 and not 1 <= int(raw) <= dest_count:
                    raise RuntimeError(f'remapped {name} still names extruder {raw.decode()}, '
                                       'outside the destination numbering')
            keys = '|'.join(OBJECT_FILAMENT_KEYS).encode()
            for key, raw in re.findall(rb'<metadata key="(' + keys + rb')" value="(\d+)"', data):
                if int(raw) > dest_count:
                    raise RuntimeError(f'remapped {name} still names {key.decode()} '
                                       f'{raw.decode()}, outside the destination numbering')
        elif name == RANGES:
            for raw in re.findall(rb'<option opt_key="extruder">(\d+)</option>', data):
                if not 1 <= int(raw) <= dest_count:
                    raise RuntimeError('remapped layer_config_ranges.xml still names an '
                                       'extruder outside the destination numbering')
        elif name == SLICE_INFO:
            for raw in re.findall(rb'<filament[^>]*\bid="(\d+)"', data):
                if not 1 <= int(raw) <= dest_count:
                    raise RuntimeError('remapped slice_info.config still names a filament '
                                       'outside the destination numbering')
        elif name.lower().endswith('.model'):
            if b'mmu_segmentation' in data:
                raise RuntimeError(f'remapped {name} still carries mmu_segmentation paint')
            for state in painted_geometry.paint_states_in(data):
                if state > dest_count:
                    raise RuntimeError(f'remapped {name} still carries paint state {state}, '
                                       'outside the destination numbering')
        elif name == PROJECT:
            project = json.loads(data)
            if len(project.get('filament_colour') or []) != dest_count:
                raise RuntimeError('remapped project palette does not match the destination numbering')
            for key in FEATURE_FILAMENT_KEYS:
                value = _filament_number(project.get(key, 0))
                if value is None or value > dest_count:
                    raise RuntimeError(f'remapped project {key} still names a filament outside '
                                       'the destination numbering')
    if layer_gcode.MEMBER in payload:
        for row in layer_gcode.parse(payload[layer_gcode.MEMBER]):
            if row['kind'] == 'tool_change' and row['source_extruder'] not in set(mapping.values()):
                raise RuntimeError('remapped per-layer member still names a tool outside '
                                   'the destination numbering')


def write_package_atomically(target, payload, validate=None, *, source=None):
    """The C1 staged-write shell: stage, validate, fsync, replace; never partial.

    payload is {member name: bytes} in the order they should be written.
    validate, when given, is called with the payload dict before replacing
    and again on the staged file's re-read payload. The package is staged in
    a uniquely named temporary file in the target's directory (so concurrent
    writers never share a staging name and the final replace stays on one
    filesystem), flushed and fsynced, then os.replace()d onto the target.
    Any failure removes the staging file and leaves a pre-existing target
    byte-identical. When source is given and resolves to the same path as
    target, the write is refused before anything is staged: the original must
    stay immutable.
    """
    target = Path(target)
    if source is not None and Path(source).resolve() == target.resolve():
        raise RuntimeError(f'refusing to write the package over its own source {target}; '
                           'the original must stay immutable')
    target.parent.mkdir(parents=True, exist_ok=True)
    if validate is not None:
        validate(payload)
    fd, tmp_name = tempfile.mkstemp(prefix=target.name + '.', suffix='.tmp', dir=target.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, 'wb') as handle:
            with zipfile.ZipFile(handle, 'w', zipfile.ZIP_DEFLATED) as out:
                for name, data in payload.items():
                    out.writestr(name, data)
            handle.flush()
            os.fsync(handle.fileno())
        with zipfile.ZipFile(tmp) as check:
            if check.testzip() is not None:
                raise RuntimeError('staged 3MF package failed its integrity check')
            if validate is not None:
                validate({name: check.read(name) for name in check.namelist()})
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()


def remap_package(source, target, mapping, object_map=None):
    """Atomically remap every filament reference of a 3MF package by `mapping`.

    EXPERIMENTAL: no production code path calls this function. The live
    export paths (export_color_geometry, export_single_color_plate and
    adopt_job_settings in color_geometry) import only write_package_atomically
    from this module; remap_package is exercised by tests alone (see
    docs/3MF-CONVERSION-EVIDENCE.md). Do not cite it as evidence that a
    production export remaps a package.

    mapping is {source extruder: destination filament}; object_map, when
    given, is {source object id: destination object id} applied to every
    object-keyed reference (height ranges, model settings objects and plate
    instances). Without it, object ids are preserved exactly. Returns a
    report naming the mapping, the members rewritten, the members preserved
    byte-for-byte, and any designation fallback taken.
    """
    mapping = validate_mapping(mapping)
    object_map = {str(k): str(v) for k, v in dict(object_map or {}).items()}
    source, target = Path(source), Path(target)
    with zipfile.ZipFile(source) as archive:
        names = archive.namelist()
        payload = {name: archive.read(name) for name in names}
    identity = all(mapping[n] == n for n in mapping)
    notes, rewritten = [], []
    order = [s for s, _d in sorted(mapping.items(), key=lambda kv: kv[1])]
    palette_count = len(mapping)
    if PROJECT in payload:
        try:
            palette = json.loads(payload[PROJECT]).get('filament_colour')
        except (ValueError, AttributeError):
            palette = None
        if isinstance(palette, list) and palette:
            palette_count = len(palette)
    for name in names:
        handling = member_handling(name)
        if handling is None:
            raise RuntimeError(f'3MF member {name} has no classified remap handling; refusing '
                               'rather than carry or drop it silently')
        if handling == 'refuse-gcode':
            raise RuntimeError(f'3MF member {name} embeds sliced G-code, whose tool commands '
                               'cannot be remapped; refusing rather than ship a package that '
                               'contradicts its own numbering')
        if handling == 'refuse-sequence' and not identity:
            raise RuntimeError('Metadata/filament_sequence.json cannot be remapped: its sequence '
                               'semantics are not established, and its embedded preset record is '
                               'provenance evidence about the source; refusing a non-identity remap')
        if handling == 'preserve':
            continue
        data = payload[name]
        if name == PROJECT:
            project, project_notes = permute_project_settings(json.loads(data), mapping)
            notes += project_notes
            payload[name] = json.dumps(project, indent=4).encode()
        elif name in (MODEL_SETTINGS, PRUSA_MODEL):
            out = remap_extruder_metadata(data, mapping, member=name)
            if name == MODEL_SETTINGS:
                out, metadata_notes = remap_filament_metadata(out, mapping, order, palette_count,
                                                              member=name)
                notes += [row['reason'] for row in metadata_notes]
            if object_map:
                def fix_id(match):
                    oid = match.group(2).decode()
                    if oid not in object_map:
                        raise RuntimeError(f'{name} names object {oid}, which the object map '
                                           'does not translate')
                    return match.group(1) + object_map[oid].encode() + match.group(3)
                out = re.sub(rb'(<object[^>]*\bid=")(\d+)(")', fix_id, out)
                out = re.sub(rb'(<metadata key="object_id" value=")(\d+)(")', fix_id, out)
            payload[name] = out
        elif name == PRUSA_CONFIG:
            payload[name] = remap_prusa_member(data, mapping)
        elif name == RANGES:
            payload[name] = remap_ranges(data, mapping, object_map or None)
        elif name == SLICE_INFO:
            payload[name] = remap_slice_info(data, mapping)
        elif name == layer_gcode.MEMBER:
            entries = layer_gcode.parse(data)
            unresolved = sorted({row['source_extruder'] for row in entries
                                 if row['kind'] == 'tool_change'
                                 and row['source_extruder'] not in mapping})
            if unresolved:
                raise RuntimeError(f'unmapped source layer tool(s): {unresolved}')
            converted, _layer_report = layer_gcode.remap(
                data, mapping)
            payload[name] = converted
        elif name.lower().endswith('.model'):
            payload[name] = _remap_model_member(data, mapping)
        if payload[name] != data:
            rewritten.append(name)
    write_package_atomically(target, payload, validate=lambda p: _validate_consistency(p, mapping),
                             source=source)
    from .orca_adapter import digest
    return {'source_sha256': digest(source), 'converted_sha256': digest(target),
            'mapping': {str(k): v for k, v in sorted(mapping.items())},
            'object_map': object_map, 'rewritten_entries': sorted(rewritten),
            'preserved_entries': sorted(n for n in names if n not in rewritten),
            'notes': notes,
            'governing_rules': ['per-filament-array-permutation', 'flush-matrix-permutation',
                                'flush-vector-unestablished-refusal', 'support-designation-remap',
                                'height-range-extruder-remap', 'mmu-segmentation-paint-translation',
                                'remap-member-classification'],
            'policy': 'generic package remap; every classified filament reference rewritten by '
                      'the mapping, unrelated members byte-for-byte, staged write with a '
                      'consistency re-read; object ids preserved unless an object map is given'}
