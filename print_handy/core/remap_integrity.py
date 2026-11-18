"""Remap integrity checks for cross-slicer 3MF work (#1525, tranche C).

One validation pass over a source and a proposed filament remap, built
on the existing inventory (`source_colors`) and package rules
(`package_remap`) rather than a second reader. It answers, before any
byte is rewritten:

* roles survive: a modifier is never a printable part, a negative
  volume stays a subtraction, support blockers and enforcers are named;
* painted identity is present or absent as a fact, never assumed;
* per-filament arrays are handled atomically: an array the remapper
  does not know is named as an unknown passthrough, and a palette whose
  color and material lists disagree in length is a problem, not a guess;
* plate selection names only plates the source actually has;
* logical filament, material, physical slot and toolhead are tracked
  as four separate facts — a five-color source on four tools is a
  finding, not a silent collapse;
* unknown references (a decision or mapping entry naming a filament
  the source does not have) are rejected, never adopted.

After a remap, before any byte is written, ``verify_remapped_package``
(the remap slice route and the editable export) and
``verify_rebuilt_package`` (the rebuilt export) check the package against
its source and raise ``IntegrityRefused`` naming every lost or
misassigned fact. ``sliced_plates`` names the source plates a slice
prints, for its provenance.
"""
from __future__ import annotations

import collections
import json
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from . import layer_gcode, package_remap, painted_geometry, source_colors, source_descriptor

logger = logging.getLogger(__name__)

SUPPORT_SUBTYPES = ("support_blocker", "support_enforcer")
NON_PRINTING_SUBTYPES = ("modifier_part", "negative_part") + SUPPORT_SUBTYPES


def plate_ids(manifest: dict) -> list:
    """Plate ids a manifest knows, from parts or per-plate usage."""
    found = []
    for part in manifest.get("parts") or ():
        plate = part.get("plate_id") or part.get("plate")
        if plate is not None and plate not in found:
            found.append(plate)
    for plate in (manifest.get("plate_usage") or {}):
        if plate not in found:
            found.append(plate)
    return found


def tracking(manifest: dict, decisions: dict | None = None,
             tool_count: int | None = None) -> list[dict]:
    """Logical filament / material / physical slot / toolhead, per filament.

    ``decisions`` maps a source filament number to a mapping with a
    ``slot`` (physical slot) when the operator has chosen one. A slot is
    never invented here: unchosen stays None. The toolhead is the slot's
    head only when the printer model says slots and heads coincide
    (``tool_count`` given and equal to the slot count the caller passes
    via decisions); otherwise it stays None rather than pretending a
    feeder slot is an independent head.
    """
    decisions = decisions or {}
    rows = []
    for filament in manifest.get("filaments") or ():
        number = filament.get("source_extruder")
        choice = decisions.get(number) or decisions.get(str(number)) or {}
        slot = choice.get("slot") if isinstance(choice, dict) else choice
        rows.append({
            "logical_filament": number,
            "material": filament.get("material"),
            "color": filament.get("color"),
            "physical_slot": slot,
            "toolhead": None,  # filled only by a printer model that proves it
        })
    return rows


def validate(source, mapping=None, selected_plate_ids=None,
             decisions=None) -> dict:
    """Validate a proposed remap/selection against a source 3MF.

    Returns {"ok", "problems", "warnings", "roles", "tracking",
    "descriptor"}. ``ok`` is False only for a named problem; warnings
    never block (filament conditions are warnings the operator can
    accept, per the standing ruling).
    """
    source = Path(source)
    problems: list[str] = []
    warnings: list[str] = []
    descriptor = source_descriptor.describe(source)
    if descriptor["user_class"] == "unsupported-corrupt":
        problems.append("the source is not a readable, usable 3MF: "
                        + "; ".join(descriptor["problems"] or ["unknown damage"]))
    try:
        manifest = source_colors.inventory(source)
    except Exception as exc:  # inventory refuses damaged packages by name
        problems.append(f"source inventory failed: {exc}")
        manifest = {"parts": [], "filaments": [], "painted": False,
                    "plate_usage": {}}
    parts = list(manifest.get("parts") or [])
    # The inventory separates non-printing volumes into `cutters`: a
    # modifier, negative volume or support blocker/enforcer is never in
    # `parts`, by design. Roles are read across both lists.
    all_volumes = parts + list(manifest.get("cutters") or [])
    roles = {
        "modifiers": [p["key"] for p in all_volumes if p.get("subtype") == "modifier_part"],
        "negative_parts": [p["key"] for p in all_volumes if p.get("subtype") == "negative_part"],
        "support_blockers": [p["key"] for p in all_volumes if p.get("subtype") == "support_blocker"],
        "support_enforcers": [p["key"] for p in all_volumes if p.get("subtype") == "support_enforcer"],
        "printable_parts": [p["key"] for p in parts
                            if p.get("subtype", "normal_part") == "normal_part"],
    }
    # A modifier or negative volume can never be a printable part: the
    # inventory keeps subtypes distinct, and this pass fails loudly if a
    # future reader ever merges them.
    overlap = set(roles["printable_parts"]) & (
        set(roles["modifiers"]) | set(roles["negative_parts"]))
    if overlap:
        problems.append(f"non-printing volumes listed as printable: {sorted(overlap)}")
    try:
        with zipfile.ZipFile(source) as archive:
            negatives = source_colors.negative_volumes(archive)
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        # An unreadable archive here only loses the negative-volume
        # warning; the descriptor pass already refuses damaged archives.
        logger.info("negative-volume scan skipped for %s: %s", Path(source).name, exc)
        negatives = []
    if negatives and not roles["negative_parts"]:
        warnings.append(
            f"{len(negatives)} negative volume(s) ride as model volumes; they "
            "stay subtractions in any remap or the remap refuses by name")
    if manifest.get("painted"):
        warnings.append("the source carries painted color identity; a remap must "
                        "translate paint states, never carry them verbatim")
    palette = [f.get("source_extruder") for f in manifest.get("filaments") or ()]
    # Per-filament arrays: schema-verified, atomic.
    if source.suffix.lower() == ".3mf" and zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            if "Metadata/project_settings.config" in archive.namelist():
                try:
                    settings = json.loads(archive.read("Metadata/project_settings.config"))
                except ValueError:
                    settings = None
                    problems.append("project_settings.config does not parse as JSON")
                if isinstance(settings, dict):
                    colors = settings.get("filament_colour")
                    types = settings.get("filament_type")
                    if (isinstance(colors, list) and isinstance(types, list)
                            and len(colors) != len(types)):
                        problems.append(
                            "filament_colour and filament_type disagree in length "
                            f"({len(colors)} vs {len(types)}); the palette is not "
                            "schema-verified, so no remap may proceed on it")
                    for key in package_remap.unknown_palette_arrays(
                            settings, len(palette)):
                        warnings.append(
                            f"per-filament-shaped array {key!r} is not a known "
                            "filament array; it passes through as authored and is "
                            "never permuted on a length guess")
    if mapping is not None:
        try:
            normalized = package_remap.validate_mapping(mapping)
        except RuntimeError as exc:
            problems.append(str(exc))
            normalized = {}
        for number in normalized:
            if palette and number not in palette:
                problems.append(
                    f"the mapping names source filament {number}, which the "
                    "source palette does not have; unknown references are "
                    "rejected, not adopted")
    if decisions:
        for number in decisions:
            try:
                known = int(number) in palette
            except (TypeError, ValueError):
                known = False
            if palette and not known:
                problems.append(
                    f"a decision names source filament {number!r}, which the "
                    "source palette does not have; it is rejected, not adopted")
    known_plates = plate_ids(manifest)
    if selected_plate_ids is not None:
        for plate in selected_plate_ids:
            if known_plates and plate not in known_plates:
                problems.append(
                    f"selected plate {plate!r} is not a plate of this source "
                    f"(plates: {known_plates}); the selection is rejected, "
                    "not adopted")
    rows = tracking(manifest, decisions)
    used_slots = [r["physical_slot"] for r in rows if r["physical_slot"] is not None]
    if len(set(used_slots)) != len(used_slots):
        warnings.append("two logical filaments share one physical slot; that is "
                        "a warning to accept, never a silent merge")
    return {"ok": not problems, "problems": problems, "warnings": warnings,
            "roles": roles, "tracking": rows, "descriptor": descriptor}


# --- The package the slicer is handed (#1525, PR C) -------------------------
#
# validate() above reads a source before a remap. The checks below compare
# the package a slice or an export is about to write against its source, so
# a remapper or rebuild that loses or misassigns a filament fact refuses by
# name before anything is written (no partial package, no artifact made
# ready). They read member bytes with regular expressions and reuse the
# decode scope's paint census (painted_geometry.paint_states_in), so a
# painted model is never parsed as XML here and never decoded twice.

class IntegrityRefused(RuntimeError):
    """A package that would lose or misassign a filament fact; nothing was written."""


_REF_KEYS = frozenset(('extruder',) + package_remap.OBJECT_FILAMENT_KEYS
                      + package_remap.PRUSA_ROLE_KEYS)
_META = re.compile(rb'<metadata\b[^>]*?\bkey="([A-Za-z0-9_]+)"[^>]*?\bvalue="([^"]*)"')
_CHILD = re.compile(rb'<(part|volume)\b([^>]*)>(.*?)</\1>', re.S)
_SUBTYPE = re.compile(rb'\bsubtype="([^"]+)"')
_RANGE_EXTRUDER = re.compile(rb'<option opt_key="extruder">([^<]*)</option>')
_PRUSA_CODE = re.compile(rb'<code\b[^>]*>')
_PRUSA_CODE_EXTRUDER = re.compile(rb'\bextruder="(\d+)"')
# Support, seam and fuzzy-skin paint name no filament; a remap carries them
# byte for byte, so their count per member never changes.
_SIDE_PAINT = re.compile(rb'(paint_supports|paint_seam|paint_fuzzy_skin|custom_supports|custom_seam)="')
_PALETTE_KEYS = ('filament_colour', 'filament_type')
# The identity mapping covers every filament number a package can name.
_IDENTITY_SPAN = 256
_PRUSA_PALETTE_KEYS = ('extruder_colour', 'filament_colour', 'filament_type')
# Object and part keys whose unmapped value the remapper writes as 0 (unknown
# usage, named in its notes) instead of refusing.
_NEUTRAL_KEYS = frozenset(package_remap.OBJECT_FILAMENT_KEYS)


def _number(raw):
    return package_remap._filament_number(raw)


def _settings_facts(data):
    """({(object, child index or None, key): raw}, {(object, role): count}) of a settings member.

    Reads Bambu/Orca model_settings.config parts and PrusaSlicer
    Slic3r_PE_model.config volumes alike. The role of a part is its subtype;
    of a volume, its volume_type (the older modifier flag reads as a
    ParameterModifier).
    """
    refs, roles = {}, collections.Counter()
    for block in package_remap._OBJECT_BLOCK.finditer(data):
        oid, body = block.group(2).decode(), block.group(3)
        first = _CHILD.search(body)
        own = body[:first.start()] if first else body
        for key, value in _META.findall(own):
            if key.decode() in _REF_KEYS:
                refs[(oid, None, key.decode())] = value.decode()
        for index, child in enumerate(_CHILD.finditer(body)):
            metas = {k.decode(): v.decode() for k, v in _META.findall(child.group(3))}
            subtype = _SUBTYPE.search(child.group(2))
            if subtype:
                role = subtype.group(1).decode()
            elif child.group(1) == b'volume':
                role = metas.get('volume_type') or ('ParameterModifier' if metas.get('modifier') == '1'
                                                    else 'ModelPart')
            else:
                role = 'normal_part'
            roles[(oid, role)] += 1
            for key, value in metas.items():
                if key in _REF_KEYS:
                    refs[(oid, index, key)] = value
    return refs, roles


def _mapped(value, mapping, unmapped=None):
    """The destination of filament `value`: 0 stays 0, an unmapped one is `unmapped`."""
    if value == 0:
        return 0
    return mapping.get(value, unmapped)


def _where(oid, index):
    return f'object {oid}' + (f' part {index}' if index is not None else '')


def _check_settings(name, before, after, mapping, problems):
    src_refs, src_roles = _settings_facts(before)
    out_refs, out_roles = _settings_facts(after)
    if src_roles != out_roles:
        changed = sorted({oid for (oid, _role) in set(src_roles) | set(out_roles)
                          if src_roles.get((oid, _role)) != out_roles.get((oid, _role))})
        problems.append(f'{name}: the part and volume roles of object(s) {changed} changed '
                        '(a modifier, negative volume or support blocker/enforcer was lost or retyped)')
    for (oid, index, key), raw in src_refs.items():
        value = _number(raw)
        if value is None:
            continue  # not a filament number; the remapper names what it does with it
        if index is None and key == 'extruder' and value == 0:
            continue  # implicit filament 1, checked with the additions below
        expected = _mapped(value, mapping, unmapped=0 if key in _NEUTRAL_KEYS else None)
        out_raw = out_refs.get((oid, index, key))
        if out_raw is None:
            problems.append(f'{name}: {_where(oid, index)} lost its {key} (filament {value})')
        elif expected is not None and _number(out_raw) != expected:
            problems.append(f'{name}: {_where(oid, index)} {key} names filament {out_raw}, but '
                            f'source filament {value} maps to {expected}')
    for (oid, index, key), out_raw in out_refs.items():
        src_raw = src_refs.get((oid, index, key))
        implicit = index is None and key == 'extruder'
        if src_raw is not None and not (implicit and _number(src_raw) == 0):
            continue
        if implicit:
            # An object on implicit filament 1 is written explicitly when the
            # mapping moves filament 1 (Bambu/Orca), or given the role
            # filament (PrusaSlicer); anything else is a stray reference.
            allowed = ({0, mapping.get(1, 1)} if name == package_remap.MODEL_SETTINGS
                       else {0} | set(mapping.values()))
            if _number(out_raw) not in allowed:
                problems.append(f'{name}: object {oid} gained extruder {out_raw}, which is not '
                                'where its implicit filament 1 maps')
        else:
            problems.append(f'{name}: {_where(oid, index)} gained a {key} reference ({out_raw}) '
                            'the source does not make')


def _check_palette(name, src_lists, out_lists, mapping, problems):
    """Each palette list entry moves with its filament: destination d carries source n's entry."""
    identity = all(source == dest for source, dest in mapping.items())
    for key, items in src_lists.items():
        out = out_lists.get(key)
        if not isinstance(items, list):
            continue
        if identity:
            if out != items:
                problems.append(f'{name}: the {key} palette list changed under an identity mapping')
            continue
        if len(items) != len(mapping):
            continue
        if not isinstance(out, list) or len(out) != len(items):
            problems.append(f'{name}: the {key} palette list was lost or resized')
            continue
        for source, dest in mapping.items():
            if 1 <= source <= len(items) and 1 <= dest <= len(out) \
                    and str(out[dest - 1]).strip() != str(items[source - 1]).strip():
                problems.append(f'{name}: {key} of source filament {source} ({items[source - 1]}) '
                                f'is not on filament {dest} ({out[dest - 1]})')


def _check_project(name, before, after, mapping, problems):
    try:
        src, out = json.loads(before), json.loads(after)
    except ValueError as exc:
        problems.append(f'{name} does not parse as JSON ({exc})')
        return
    if not isinstance(src, dict) or not isinstance(out, dict):
        return
    _check_palette(name, {k: src.get(k) for k in _PALETTE_KEYS},
                   {k: out.get(k) for k in _PALETTE_KEYS}, mapping, problems)
    for key in ('support_filament', 'support_interface_filament') + package_remap.FEATURE_FILAMENT_KEYS:
        value = _number(src.get(key)) if key in src else None
        if value is None:
            continue
        if value and value not in mapping:
            # Unknown usage: a support designation falls back to 0; a wall or
            # infill key is removed and the tower written 0, each named.
            if key in out and _number(out[key]) not in (0, None):
                problems.append(f'{name}: {key} still names filament {out[key]}, which the '
                                'mapping does not cover')
            continue
        expected = _mapped(value, mapping)
        if key not in out:
            problems.append(f'{name}: {key} (filament {value}) was lost')
        elif _number(out[key]) != expected:
            problems.append(f'{name}: {key} names filament {out[key]}, but source filament '
                            f'{value} maps to {expected}')


def _prusa_lines(data):
    found = {}
    for line in data.decode('utf-8', 'replace').splitlines():
        match = package_remap._PRUSA_LINE.match(line)
        if match:
            found[match.group(2)] = match.group(4).strip()
    return found


def _check_prusa_config(name, before, after, mapping, problems):
    src, out = _prusa_lines(before), _prusa_lines(after)

    def lists(lines):
        return {k: package_remap._split_prusa_list(lines[k])[0]
                for k in _PRUSA_PALETTE_KEYS if lines.get(k)}
    _check_palette(name, lists(src), lists(out), mapping, problems)
    for key in package_remap.PRUSA_ROLE_KEYS:
        value = _number(src.get(key)) if key in src else None
        if value is None:
            continue
        if key not in out:
            problems.append(f'{name}: {key} (extruder {value}) was lost')
        elif _number(out[key]) != _mapped(value, mapping):
            problems.append(f'{name}: {key} names extruder {out[key]}, but source extruder '
                            f'{value} maps to {_mapped(value, mapping)}')


def _check_sequence(name, src_values, out_values, mapping, problems, what):
    if len(src_values) != len(out_values):
        problems.append(f'{name}: {len(src_values)} {what}(s) became {len(out_values)}')
        return
    for position, (value, out) in enumerate(zip(src_values, out_values, strict=True), 1):
        if value is None:
            continue
        expected = _mapped(value, mapping, unmapped=value)
        if out != expected:
            problems.append(f'{name}: {what} {position} names extruder {out}, but source '
                            f'extruder {value} maps to {expected}')


def _check_layers(name, before, after, mapping, problems):
    try:
        src, out = layer_gcode.parse(before), layer_gcode.parse(after)
    except ET.ParseError as exc:
        problems.append(f'{name} does not parse ({exc})')
        return
    shape_out = [(r['plate'], r['top_z'], r['kind']) for r in out]
    if [(r['plate'], r['top_z'], r['kind']) for r in src] != shape_out:
        lost = [f"{r['kind'].replace('_', ' ')} at {r['top_z']} mm" for r in src
                if (r['plate'], r['top_z'], r['kind']) not in shape_out]
        problems.append(f'{name}: per-layer entries changed'
                        + (f'; lost {", ".join(lost)}' if lost else ''))
        return
    _check_sequence(name, [r['source_extruder'] if r['kind'] != 'custom' else None for r in src],
                    [r['source_extruder'] for r in out], mapping, problems, 'per-layer entry')


def _check_model(name, before, after, mapping, problems):
    if collections.Counter(_SIDE_PAINT.findall(before)) != collections.Counter(_SIDE_PAINT.findall(after)):
        problems.append(f'{name}: support, seam or fuzzy-skin paint changed; it names no filament '
                        'and must be carried as authored')
    if not (b'paint_color=' in before or b'mmu_segmentation=' in before):
        if b'paint_color=' in after:
            problems.append(f'{name}: gained filament paint the source does not have')
        return
    src = painted_geometry.paint_states_in(before)
    out = painted_geometry.paint_states_in(after)
    expected = collections.Counter()
    for state, leaves in src.items():
        expected[_mapped(state, mapping, unmapped=state)] += leaves
    if b'paint_color=' in before and b'mmu_segmentation=' in before:
        # A triangle may carry both attributes; the remap keeps paint_color
        # and drops the other, so only the states are comparable here.
        stray = sorted(set(out) - set(expected))
        if stray:
            problems.append(f'{name}: paint names state(s) {stray}, which no source paint maps to')
    elif dict(expected) != out:
        problems.append(f'{name}: painted regions changed filament or were lost (expected leaf '
                        f'states {dict(sorted(expected.items()))}, found {dict(sorted(out.items()))})')


def verify_remapped_package(source_payload, payload, mapping=None, *, may_drop=()):
    """Refuse a structure-preserving package that loses or misassigns a filament fact.

    ``source_payload`` and ``payload`` are {member: bytes} of the source and
    of the package about to be written; ``mapping`` is {source filament:
    destination filament}. Checked: every member carried (except slice
    records and the names in ``may_drop``), object part and volume roles,
    every object, part and volume extruder, support designation and feature
    filament, the palette, PrusaSlicer role extruders, height-range
    extruders, per-layer tool changes and pauses, and triangle paint. Raises
    IntegrityRefused naming every problem; returns the members checked.
    ``mapping`` None is the identity: every reference must stay as authored.
    """
    if mapping is None:
        mapping = {n: n for n in range(1, _IDENTITY_SPAN + 1)}
    mapping = {int(k): int(v) for k, v in dict(mapping).items()}
    problems, checked = [], []
    for name, before in source_payload.items():
        if name not in payload:
            if name not in may_drop and package_remap.member_handling(name) != 'slice-record':
                problems.append(f'{name} was dropped')
            continue
        after = payload[name]
        if name in (package_remap.MODEL_SETTINGS, package_remap.PRUSA_MODEL):
            _check_settings(name, before, after, mapping, problems)
        elif name == package_remap.PROJECT:
            _check_project(name, before, after, mapping, problems)
        elif name == package_remap.PRUSA_CONFIG:
            _check_prusa_config(name, before, after, mapping, problems)
        elif name in (package_remap.RANGES, package_remap.PRUSA_RANGES):
            _check_sequence(name, [_number(v.decode()) for v in _RANGE_EXTRUDER.findall(before)],
                            [_number(v.decode()) for v in _RANGE_EXTRUDER.findall(after)], mapping,
                            problems, 'height-range extruder')
        elif name == package_remap.PRUSA_LAYER_GCODE:
            _check_sequence(name, _code_extruders(before), _code_extruders(after), mapping, problems,
                            'height entry')
        elif name == layer_gcode.MEMBER:
            _check_layers(name, before, after, mapping, problems)
        elif name.lower().endswith('.model'):
            _check_model(name, before, after, mapping, problems)
        else:
            continue
        checked.append(name)
    if problems:
        raise IntegrityRefused('the package for the slicer would lose or misassign source facts: '
                               + '; '.join(problems) + '. Nothing was written.')
    return checked


def _code_extruders(data):
    out = []
    for tag in _PRUSA_CODE.findall(data):
        match = _PRUSA_CODE_EXTRUDER.search(tag)
        out.append(int(match.group(1)) if match else None)
    return out


def _as_bytes(data):
    return data.encode() if isinstance(data, str) else (data or b'')


def verify_rebuilt_package(payload, *, roles, printable_parts, tools, painted_triangles,
                           layer_entries=0, layer_kind='tool_change'):
    """Refuse a rebuilt package (color_geometry) that drops a role, part, paint or height change.

    The rebuild renumbers objects, so it is checked against counts the
    exporter derives from the source inventory: ``roles`` {subtype: count}
    of the non-printing volumes it must carry, ``printable_parts``, the
    1-based ``tools`` an extruder may name, ``painted_triangles`` (painted
    source triangles of the exported parts; 0 for an unpainted job) and the
    ``layer_entries`` per-layer entries of ``layer_kind`` it must write.
    Raises IntegrityRefused naming every problem.
    """
    problems = []
    settings = _as_bytes(payload.get(package_remap.MODEL_SETTINGS))
    found = collections.Counter()
    for match in _CHILD.finditer(settings):
        if match.group(1) == b'part':
            subtype = _SUBTYPE.search(match.group(2))
            found[subtype.group(1).decode() if subtype else 'normal_part'] += 1
    carried = {role: n for role, n in found.items() if role != 'normal_part'}
    wanted = {role: n for role, n in roles.items() if n}
    if carried != wanted:
        problems.append(f'non-printing volumes {dict(sorted(wanted.items()))} were written as '
                        f'{dict(sorted(carried.items()))}')
    if found.get('normal_part', 0) != printable_parts:
        problems.append(f'{printable_parts} printable part(s) were written as '
                        f'{found.get("normal_part", 0)}')
    stray = sorted({int(v) for v in re.findall(rb'<metadata key="extruder" value="(\d+)"', settings)}
                   - set(tools) - {0})
    if stray:
        problems.append(f'extruder(s) {stray} name no tool of this job (tools {sorted(tools)})')
    model = _as_bytes(payload.get(package_remap.MODEL))
    if b'mmu_segmentation=' in model:
        problems.append('PrusaSlicer paint was not translated to paint_color')
    written = model.count(b'paint_color="') - model.count(b'paint_color=""')
    if written != painted_triangles:
        problems.append(f'{painted_triangles} painted triangle(s) were written as {written}')
    if layer_entries:
        member = payload.get(layer_gcode.MEMBER)
        rows = layer_gcode.parse(_as_bytes(member)) if member else []
        kept = sum(1 for row in rows if row['kind'] == layer_kind)
        if kept != layer_entries:
            problems.append(f'{layer_entries} height color change(s) were written as {kept} '
                            f'{layer_kind.replace("_", " ")} entries')
    if problems:
        raise IntegrityRefused('the rebuilt package would lose or misassign source facts: '
                               + '; '.join(problems) + '. Nothing was written.')


def sliced_plates(source, object_ids=None):
    """The source plate ids (strings) a slice of ``object_ids`` carries; None when unknown.

    The plate identity is the plater_id of model_job.plate_manifest, the
    same identity the per-plate usage records (source_colors plate_usage,
    slice_info) key on. A 3MF with no plate records is one implicit plate,
    '1', as slicing treats it. ``object_ids`` None means the whole source.
    """
    source = Path(source)
    if source.suffix.lower() != '.3mf':
        return None
    with zipfile.ZipFile(source) as archive:
        names = set(archive.namelist())
        settings = (archive.read(package_remap.MODEL_SETTINGS)
                    if package_remap.MODEL_SETTINGS in names else b'')
    if b'<plate' not in settings:
        return ['1']
    from . import model_job
    try:
        plates = model_job.plate_manifest(source)['plates']
    except RuntimeError as exc:
        logger.info('plate records of %s unreadable, sliced plates unknown: %s', source.name, exc)
        return None
    wanted = None if object_ids is None else {str(o) for o in object_ids}
    return [str(plate['id']) for plate in plates
            if wanted is None or wanted & {str(o) for o in plate['object_ids']}]
