"""Canonical 3MF source descriptor and Cura settings translation (#1525).

One descriptor, built through the existing author-settings intake: every
3MF Print Handy accepts is classified here by what its bytes prove, never
by its file name. Kinds: ``bambu``, ``orca``, ``prusa``, ``cura``,
``anycubic`` (detection only, on the published X-ACNext slice-info
marker), ``generic-core``, ``gcode-only`` and ``unknown``. The
user-facing class is one of ``editable-project``, ``geometry-only``,
``sliced-only`` and ``unsupported-corrupt``, so an import can say plainly
what the file is before anything slices it.

Parsing is bounded: member count, per-member and total uncompressed
size, compression ratio, model XML size and config size all have hard
limits, and a source that exceeds them is refused by name instead of
being decoded without bound. The source file is never mutated.

Cura projects store their setting stacks as INI-style ``.cfg`` members
under ``Cura/`` (the workspace writer serializes each container stack
into the archive). ``parse_cura_settings`` resolves each stack in Cura's
own container order and keeps only the designer's literal choices (the
user and quality_changes layers), never a vendor profile's default. The
validated subset in the compatibility registry's
``cura-key-translation`` rule maps those keys to Orca names; everything
else a Cura project carries — machine identity, start/end G-code,
temperatures, accelerations, extruder offsets — is never translated,
because the destination printer owns those settings.
"""
from __future__ import annotations

import configparser
import io
import re
import zipfile
from pathlib import Path
from urllib.parse import unquote_plus
from xml.etree import ElementTree as ET

from . import compat_registry

MAX_MEMBERS = 2048
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_MODEL_BYTES = 16 * 1024 * 1024
MAX_CONFIG_BYTES = 4 * 1024 * 1024
MAX_RATIO = 200
RATIO_FLOOR_BYTES = 1 * 1024 * 1024

MODEL_MEMBERS = ("3D/3dmodel.model", "3D/3DModel.model")
CURA_PREFIX = "Cura/"
SLICE_INFO = "Metadata/slice_info.config"
PROJECT_SETTINGS = "Metadata/project_settings.config"
PRUSA_CONFIG = "Metadata/Slic3r_PE.config"

# Keys a Cura source may never contribute, whatever member they hide in.
# The registry rule names the same set; this tuple is the fast local check
# and the registry-parity test pins the two together.
FORBIDDEN_SOURCE_KEYS = tuple(
    compat_registry.get_rule("cura-forbidden-keys").parameters["keys"])

_NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
_APPLICATION = re.compile(rb'<metadata\s+name="Application"\s*>\s*([^<]*?)\s*</metadata>')
# A genuine OrcaSlicer save names its Bambu base in Application
# ("BambuStudio-02.08.01.55") and itself in a separate OrcaSlicer metadata
# element ("2.5.1"); that element is the producer proof, proven by
# tests/corpus/producer-fixtures/orca-genuine.3mf (#1525). Bambu Studio
# saves carry no such element.
_ORCA_METADATA = re.compile(rb'<metadata\s+name="OrcaSlicer"\s*>\s*([^<]*?)\s*</metadata>')
_PLATE_THUMBNAIL = re.compile(r"Metadata/plate_\d+\.png$", re.I)


class DescriptorRefused(ValueError):
    """A 3MF the descriptor will not classify, with the reason."""


def _bounded_read(archive, name, limit):
    info = archive.getinfo(name)
    if info.file_size > limit:
        raise DescriptorRefused(
            f"{name} is {info.file_size} bytes uncompressed; the limit is {limit}.")
    data = archive.read(name)
    if len(data) > limit:
        raise DescriptorRefused(f"{name} expands past its stated size.")
    return data


def _check_archive_bounds(infos):
    if len(infos) > MAX_MEMBERS:
        raise DescriptorRefused(
            f"the archive has {len(infos)} entries; the limit is {MAX_MEMBERS}.")
    total = 0
    seen = set()
    for info in infos:
        if info.filename in seen:
            raise DescriptorRefused(f"duplicate archive entry: {info.filename}")
        seen.add(info.filename)
        if info.filename.startswith("/") or ".." in Path(info.filename).parts:
            raise DescriptorRefused(f"unsafe archive entry path: {info.filename}")
        if info.file_size > MAX_MEMBER_BYTES:
            raise DescriptorRefused(
                f"{info.filename} is {info.file_size} bytes uncompressed; "
                f"the limit is {MAX_MEMBER_BYTES}.")
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise DescriptorRefused(
                f"the archive expands past {MAX_TOTAL_BYTES} bytes in total.")
        if (info.file_size > RATIO_FLOOR_BYTES and info.compress_size > 0
                and info.file_size / info.compress_size > MAX_RATIO):
            raise DescriptorRefused(
                f"{info.filename} is compressed more than {MAX_RATIO}:1, "
                "which is how a ZIP bomb looks.")


def _model_facts(archive, names):
    """(object_count, build_item_count, has_mesh, application) from the model."""
    member = next((m for m in MODEL_MEMBERS if m in names), None)
    if member is None:
        model_members = [n for n in names if n.startswith("3D/") and n.endswith(".model")]
        member = model_members[0] if model_members else None
    if member is None:
        return 0, 0, False, None
    data = _bounded_read(archive, member, MAX_MODEL_BYTES)
    head = data[:1 << 16]
    app_match = _APPLICATION.search(head)
    application = app_match.group(1).decode("utf-8", "replace") if app_match else None
    orca_match = _ORCA_METADATA.search(head)
    if orca_match:
        application = f"OrcaSlicer-{orca_match.group(1).decode('utf-8', 'replace')}"
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise DescriptorRefused(f"the model XML does not parse: {exc}") from None
    objects = root.findall(f".//{_NS}object")
    items = root.findall(f".//{_NS}build/{_NS}item")
    has_mesh = root.find(f".//{_NS}mesh") is not None
    if not has_mesh:
        # Genuine Orca/Bambu saves keep the mesh in component files under
        # 3D/Objects/ and reference them from the root model; geometry
        # lives there, not in the root member.
        for other in sorted(names):
            if other == member or not (other.startswith("3D/")
                                       and other.endswith(".model")):
                continue
            try:
                component = ET.fromstring(_bounded_read(archive, other,
                                                        MAX_MODEL_BYTES))
            except (ET.ParseError, DescriptorRefused):
                continue
            if component.find(f".//{_NS}mesh") is not None:
                has_mesh = True
                break
    return len(objects), len(items), has_mesh, application


# Cura's own container order inside one stack, highest priority first: a
# stack file lists its container ids in this order under [containers], and
# this tuple is the fallback when a stack names none.
CURA_LAYER_ORDER = ("user", "quality_changes", "intent", "quality", "material",
                    "variant", "definition_changes", "definition")
# The layers that hold the designer's own choices. Every other layer is a
# vendor or machine default (quality, intent, material, variant, machine
# definition) that the destination printer's own profile replaces.
CURA_DESIGNER_LAYERS = ("user", "quality_changes")
_CURA_STACK_TYPES = ("machine", "extruder_train")
_CURA_INSTANCE_SUFFIX = ".inst.cfg"


def _read_cura_cfg(archive, name):
    """A Cura .cfg member as a ConfigParser, or None when it does not parse."""
    data = _bounded_read(archive, name, MAX_CONFIG_BYTES)
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        parser.read_string(data.decode("utf-8", "replace"))
    except configparser.Error:
        return None
    return parser


def _cura_values(parser):
    """One container's [values], formulas (``=...``) included."""
    if not parser.has_section("values"):
        return {}
    return {key.strip(): value.strip() for key, value in parser.items("values")}


def _cura_stack_order(parser):
    """The container ids a stack lists under [containers], highest priority first."""
    if not parser.has_section("containers"):
        return []
    rows = []
    for index, container_id in parser.items("containers"):
        try:
            rows.append((int(index), container_id.strip()))
        except ValueError:
            continue
    return [container_id for _index, container_id in sorted(rows)]


def _resolve_cura_layers(layers):
    """One stack's designer values: each key from its highest-priority layer.

    ``layers`` is [(type, values)] in priority order. Only the user and
    quality_changes layers contribute, so a vendor default never stands in
    for a choice the designer did not make. A formula is Cura's own
    expression, not a value: it still wins its key over the lower layers,
    and then the key is left out rather than carried from a layer the
    formula overrode.
    """
    values: dict[str, str] = {}
    for layer_type, layer_values in layers:
        if layer_type not in CURA_DESIGNER_LAYERS:
            continue
        for key, value in layer_values.items():
            values.setdefault(key, value)
    return {key: value for key, value in values.items() if not value.startswith("=")}


def parse_cura_settings(archive, names):
    """The designer's literal Cura settings, resolved through the stacks.

    A Cura workspace serializes each container stack as a ``*.global.cfg``
    or ``*.extruder.cfg`` member listing its container ids, highest
    priority first, under ``[containers]``; each container is an
    ``<id>.inst.cfg`` member whose ``[metadata] type`` names its layer
    (user, quality_changes, intent, quality, material, variant,
    definition_changes). Each stack is resolved in its own order and only
    literal values from the user and quality_changes layers are returned:
    vendor quality, intent, material, variant and machine-definition
    defaults are not the designer's intent, and a formula (``=...``) is
    Cura's own expression, never a value. A stack with no ``[containers]``
    falls back to CURA_LAYER_ORDER over the typed containers.

    The global stack is resolved first and the extruder stacks after it,
    highest position first, so extruder 0's choice wins for a key that
    several stacks set. A flat project with no stack file and untyped
    ``[values]`` members (the spec-synthetic layout) is one layer the
    author wrote and is read as the user layer. Returns {} when the
    archive carries no Cura stack.
    """
    members = sorted(n for n in names
                     if n.startswith(CURA_PREFIX) and n.endswith(".cfg"))
    if not members:
        return {}
    stacks, instances, untyped = [], {}, []
    for name in members:
        parser = _read_cura_cfg(archive, name)
        if parser is None:
            continue
        kind = parser.get("metadata", "type", fallback="").strip()
        if kind in _CURA_STACK_TYPES or parser.has_section("containers"):
            position = parser.get("metadata", "position", fallback="")
            try:
                rank = -1 if kind == "machine" else int(position)
            except ValueError:
                rank = 0
            stacks.append((rank, name, _cura_stack_order(parser)))
        elif kind in CURA_LAYER_ORDER:
            stem = name[len(CURA_PREFIX):]
            if stem.endswith(_CURA_INSTANCE_SUFFIX):
                stem = stem[:-len(_CURA_INSTANCE_SUFFIX)]
            instances[stem] = (kind, _cura_values(parser))
            # Ids are stored as file names; tolerate a percent-encoded one.
            instances.setdefault(unquote_plus(stem), instances[stem])
        elif not kind and parser.has_section("values"):
            untyped.append(_resolve_cura_layers([("user", _cura_values(parser))]))
    values: dict[str, str] = {}
    if not stacks:
        layers = sorted(instances.items(),
                        key=lambda row: (CURA_LAYER_ORDER.index(row[1][0]), row[0]))
        values.update(_resolve_cura_layers([layer for _stem, layer in layers]))
        if not instances:
            for layer in untyped:
                values.update(layer)
        return values
    # Global (rank -1) first, then extruders from the highest position down,
    # so each later stack overrides and extruder 0 is applied last.
    for _rank, _name, order in sorted(stacks, key=lambda row: (row[0] != -1, -row[0], row[1])):
        layers = [instances[container_id] for container_id in order
                  if container_id in instances]
        if not order:
            layers = sorted(instances.values(),
                            key=lambda layer: CURA_LAYER_ORDER.index(layer[0]))
        values.update(_resolve_cura_layers(layers))
    return values


def translate_cura_settings(raw):
    """The validated Cura subset, translated to Orca key names.

    Only keys named by the registry's ``cura-key-translation`` rule are
    translated, and only when the value parses and sits inside the
    rule's stated range. Returns (translated, notes): notes name every
    carried key and every candidate that was refused, so provenance can
    show what happened. Forbidden keys are never returned.
    """
    rule = compat_registry.get_rule("cura-key-translation")
    mapping = rule.parameters["mapping"]
    translated, notes = {}, []
    for cura_key, spec in mapping.items():
        if cura_key not in raw:
            continue
        value = raw[cura_key]
        orca_key, kind = spec["orca"], spec["kind"]
        try:
            if kind == "float":
                number = float(value)
                if not (spec["min"] < number <= spec["max"]):
                    notes.append(f"cura {cura_key}={value} is outside the validated "
                                 f"range and was not carried")
                    continue
                translated[orca_key] = f"{number:g}"
            elif kind == "int":
                number = int(float(value))
                if not (spec["min"] <= number <= spec["max"]):
                    notes.append(f"cura {cura_key}={value} is outside the validated "
                                 f"range and was not carried")
                    continue
                translated[orca_key] = str(number)
            elif kind == "percent":
                number = float(str(value).rstrip("%"))
                if not (0 <= number <= 100):
                    notes.append(f"cura {cura_key}={value} is outside the validated "
                                 f"range and was not carried")
                    continue
                translated[orca_key] = f"{number:g}%"
            elif kind == "enum":
                if value not in spec["values"]:
                    notes.append(f"cura {cura_key}={value} has no validated Orca "
                                 "equivalent and was not carried")
                    continue
                translated[orca_key] = spec["values"][value]
            elif kind == "bool_support":
                translated[orca_key] = "1" if value.lower() in ("true", "1") else "0"
            else:  # pragma: no cover - the registry test pins the kinds
                continue
        except (ValueError, TypeError):
            notes.append(f"cura {cura_key}={value!r} does not parse and was not carried")
            continue
        notes.append(f"cura {cura_key} carried as {orca_key}={translated[orca_key]}")
    leaked = [k for k in raw if k in FORBIDDEN_SOURCE_KEYS and k in translated]
    for key in leaked:  # defensive: the mapping never names a forbidden key
        del translated[key]
    return translated, notes


def describe(source):
    """The canonical descriptor for a 3MF (or other) source path.

    Keys: kind, user_class, format, application, source_version,
    has_geometry, has_toolpaths, has_embedded_gcode, object_count,
    build_item_count, has_thumbnail, cura_settings (raw Cura values,
    untranslated), problems (refusals and contradictions found while
    reading). A non-3MF path describes as kind ``unknown``.
    """
    source = Path(source)
    out = {"kind": "unknown", "user_class": "unsupported-corrupt",
           "format": "unknown", "application": None, "source_version": None,
           "has_geometry": False, "has_toolpaths": False,
           "has_embedded_gcode": False, "object_count": 0,
           "build_item_count": 0, "has_thumbnail": False,
           "cura_settings": {}, "problems": []}
    if source.suffix.lower() != ".3mf":
        out["problems"].append("not a 3MF file")
        return out
    try:
        archive = zipfile.ZipFile(source)
    except (zipfile.BadZipFile, OSError) as exc:
        out["problems"].append(f"not a readable ZIP archive ({type(exc).__name__})")
        return out
    with archive:
        infos = archive.infolist()
        try:
            _check_archive_bounds(infos)
        except DescriptorRefused as exc:
            out["problems"].append(str(exc))
            return out
        names = [i.filename for i in infos]
        name_set = set(names)
        try:
            objects, items, has_mesh, application = _model_facts(archive, name_set)
        except DescriptorRefused as exc:
            out["problems"].append(str(exc))
            return out
        out.update({"object_count": objects, "build_item_count": items,
                    "has_geometry": has_mesh, "application": application})
        out["has_thumbnail"] = any(
            (n.lower().endswith((".png", ".jpg", ".jpeg")) and "thumbnail" in n.lower())
            or _PLATE_THUMBNAIL.search(n)
            for n in names)
        gcode_members = [n for n in names if n.lower().endswith((".gcode", ".gco"))]
        plate_gcode = [n for n in names if "/plate_" in n.lower()
                       and n.lower().endswith(".gcode")]
        out["has_embedded_gcode"] = bool(gcode_members)
        out["has_toolpaths"] = bool(plate_gcode) or source.name.lower().endswith(".gcode.3mf")
        # Producer identity comes from file content only.
        slice_info = b""
        if SLICE_INFO in name_set:
            slice_info = _bounded_read(archive, SLICE_INFO, MAX_CONFIG_BYTES)
        if b"X-ACNext-" in slice_info:
            out["kind"] = "anycubic"
            out["format"] = "anycubic_project"
        elif PROJECT_SETTINGS in name_set:
            out["format"] = "orca_project"
            lowered = (application or "").lower()
            if "bambustudio" in lowered or "bambu studio" in lowered:
                out["kind"] = "bambu"
            elif "orca" in lowered:
                out["kind"] = "orca"
            elif b"X-BBL-" in slice_info:
                out["kind"] = "bambu"
            else:
                out["kind"] = "orca"
                out["problems"].append(
                    "project_settings.config present but the producing application "
                    "is not named; classified with the Orca/Bambu lineage reader")
        elif PRUSA_CONFIG in name_set:
            out["kind"] = "prusa"
            out["format"] = "prusaslicer_project"
        elif any(n.startswith(CURA_PREFIX) for n in names):
            out["kind"] = "cura"
            out["format"] = "cura_project"
            out["cura_settings"] = parse_cura_settings(archive, name_set)
        elif application and "prusaslicer" in application.lower():
            # A genuine PrusaSlicer CLI export carries the Application
            # metadata and a Slic3r_PE_model.config but no settings
            # snapshot: the producer is proven, the class stays
            # geometry-only below because no settings ride along.
            out["kind"] = "prusa"
            out["format"] = "core_3mf"
        elif has_mesh:
            out["kind"] = "generic-core"
            out["format"] = "core_3mf"
        if application:
            match = re.search(r"(\d+(?:\.\d+)+)", application)
            out["source_version"] = match.group(1) if match else None
        # User-facing class: what the user can honestly do with this file.
        if out["has_toolpaths"] and not has_mesh:
            out["kind"] = "gcode-only" if out["kind"] in ("unknown", "generic-core") else out["kind"]
            out["user_class"] = "sliced-only"
        elif has_mesh and out["format"] in ("orca_project", "prusaslicer_project",
                                            "cura_project", "anycubic_project"):
            out["user_class"] = "editable-project"
        elif has_mesh:
            out["user_class"] = "geometry-only"
        elif out["has_toolpaths"]:
            out["user_class"] = "sliced-only"
        else:
            out["user_class"] = "unsupported-corrupt"
            out["problems"].append("no editable geometry and no toolpaths were found")
    return out


def user_facing_summary(descriptor) -> str:
    """One plain sentence naming the class, for intake messages."""
    kind = descriptor.get("kind")
    klass = descriptor.get("user_class")
    label = {"bambu": "Bambu Studio", "orca": "OrcaSlicer", "prusa": "PrusaSlicer",
             "cura": "Cura", "anycubic": "Anycubic Slicer Next",
             "generic-core": "a plain 3MF", "gcode-only": "a sliced package",
             "unknown": "an unknown producer"}.get(kind, str(kind))
    return {"editable-project": f"This is an editable project from {label}.",
            "geometry-only": f"This is geometry only from {label}; it carries no "
                             "slicer process settings to translate.",
            "sliced-only": "This file is already sliced: it has toolpaths but no "
                           "editable geometry, so it can be reviewed but not "
                           "re-sliced or exported as a project.",
            "unsupported-corrupt": "This file could not be read as a usable 3MF."
            }.get(klass, "This file could not be classified.")
