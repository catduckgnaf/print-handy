"""One canonical form for a 3MF working copy (#814, #825).

3MF allows several spellings of the same model: the core namespace bound to a
prefix (``<ns0:model>``, what ElementTree writes) instead of the default
namespace, and a length unit other than millimeter. Every reader downstream
(audit, color inventory, geometry, layout, plate planning, the slicer itself)
would otherwise have to handle each spelling, and Orca's expat reader handles
neither. Intake rewrites the job's working copy once, here, so that everything
after it sees only:

* the core namespace as the default namespace in every ``.model`` part, and
* ``unit="millimeter"``, with the unit factor folded into each build item's
  transform (world = S . M_item . M_component . v, so meshes and component
  transforms stay byte for byte).

A file that is already canonical is not rewritten at all. A unit that cannot be
converted exactly (unknown, or declared non-millimeter by a production-extension
part other than the root) is refused with a plain message, never guessed.
"""
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from .intake_resources import IntakeLimitExceeded

CORE_NS = 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'
ROOT_MODEL = '3D/3dmodel.model'

UNIT_TO_MM = {
    'millimeter': 1.0,
    'millimetre': 1.0,
    'mm': 1.0,
    'centimeter': 10.0,
    'centimetre': 10.0,
    'cm': 10.0,
    'meter': 1000.0,
    'metre': 1000.0,
    'm': 1000.0,
    'inch': 25.4,
    'in': 25.4,
    'foot': 304.8,
    'ft': 304.8,
    'micron': 0.001,
    'micrometer': 0.001,
}

# The phrase diagnostics.known_cause keys on, so a refusal that crosses the worker boundary as text
# is still classified (configuration) instead of falling to PH_*_UNKNOWN.
UNIT_REFUSAL_CUE = '3mf declares the length unit'


class UnsupportedThreeMFUnit(IntakeLimitExceeded):
    """A 3MF whose length unit Print Handy cannot convert exactly; nothing was sliced or started."""


def unit_refusal(detail):
    return UnsupportedThreeMFUnit(
        f'This 3MF declares the length unit {detail} '
        'Re-export it in millimeters (or inches, centimeters, meters, feet or microns in the main model), '
        'then send it again. Nothing was sliced or started.')


def require_millimeter(root):
    """Assertion for geometry readers: intake already converted every unit, so this never fires for a job.

    It exists so a model that skipped intake canonicalization is refused with a classified message
    (diagnostics.known_cause) instead of an unclassified RuntimeError.
    """
    unit = root.get('unit', 'millimeter')
    if unit != 'millimeter':
        raise unit_refusal(f"'{unit}', and it was not converted to millimeters when it was received.")


def default_core_namespace(model_bytes):
    """Return a 3MF model part with its core namespace bound to the default namespace.

    Orca and Bambu Studio read 3MF through expat without namespace processing, so they compare raw
    element names ("model", "build", "item"). A file whose core elements are prefixed (<ns0:model
    xmlns:ns0="...core/2015/02">) is valid XML but invisible to them: no build item is found. When
    the root binds the core namespace to a prefix and declares no default namespace, drop that prefix
    from element tags and declare the core namespace as the default instead. Other namespaces,
    attributes and every other byte are left as they were; any other input is returned unchanged.
    """
    root = re.search(rb'<(\w+):model\b[^>]*>', model_bytes)
    if root is None:
        return model_bytes
    start = root.group(0)
    if re.search(rb'\sxmlns\s*=', start):
        return model_bytes
    core = re.escape(CORE_NS.encode())
    prefixes = {m.group(1) for m in re.finditer(rb'\sxmlns:(\w+)\s*=\s*(?:"' + core + rb'"|\'' + core + rb'\')', start)}
    if root.group(1) not in prefixes:
        return model_bytes
    out = model_bytes
    for prefix in prefixes:
        out = re.sub(rb'(</?)' + re.escape(prefix) + rb':', rb'\1', out)
    declared = []

    def declaration(match):
        if match.group(1) not in prefixes:
            return match.group(0)
        declared.append(1)
        return b' xmlns="' + CORE_NS.encode() + b'"' if len(declared) == 1 else b''

    head = re.search(rb'<model\b[^>]*>', out)
    fixed = re.sub(rb'\sxmlns:(\w+)\s*=\s*(?:"[^"]*"|\'[^\']*\')', declaration, head.group(0))
    return out[:head.start()] + fixed + out[head.end():]


_ROOT_TAG = re.compile(rb'<(?:\w+:)?model\b[^>]*>')
_UNIT_ATTR = re.compile(rb'(\sunit\s*=\s*)(["\'])([^"\']*)\2')
_ITEM_TAG = re.compile(rb'<(?:\w+:)?item\b[^>]*>')
_ITEM_NAME = re.compile(rb'<(?:\w+:)?item')
_TRANSFORM_ATTR = re.compile(rb'(\stransform\s*=\s*)(["\'])([^"\']*)\2')


def declared_unit(model_bytes):
    """The unit string the root element declares, as written, or None when it declares none."""
    root = _ROOT_TAG.search(model_bytes)
    attr = _UNIT_ATTR.search(root.group(0)) if root else None
    return attr.group(3).decode('utf-8', 'replace') if attr else None


def _unit_factor(unit, where):
    """Millimeters per declared unit; an unknown unit is refused, naming it."""
    factor = UNIT_TO_MM.get(unit.strip().lower())
    if factor is None:
        raise unit_refusal(f"'{unit}'{where}, which Print Handy does not know.")
    return factor


def _set_millimeter(model_bytes):
    root = _ROOT_TAG.search(model_bytes)
    head = _UNIT_ATTR.sub(lambda m: m.group(1) + m.group(2) + b'millimeter' + m.group(2), root.group(0), count=1)
    return model_bytes[:root.start()] + head + model_bytes[root.end():]


def _number(value):
    return format(value, '.12g').encode('ascii')


def _scale_build_items(model_bytes, factor, unit):
    def edit(match):
        tag = match.group(0)
        found = _TRANSFORM_ATTR.search(tag)
        if found is None:
            matrix = [factor, 0, 0, 0, factor, 0, 0, 0, factor, 0, 0, 0]
            return _ITEM_NAME.match(tag).group(0) + b' transform="' + b' '.join(_number(v) for v in matrix) + b'"' + \
                tag[_ITEM_NAME.match(tag).end():]
        try:
            values = [float(v) for v in found.group(3).split()]
        except ValueError:
            values = []
        if len(values) != 12:
            raise unit_refusal(f"'{unit}', but a build item transform is not 12 numbers, "
                               'so it cannot be converted to millimeters.')
        scaled = b' '.join(_number(v * factor) for v in values)
        return tag[:found.start(3)] + scaled + tag[found.end(3):]

    return _ITEM_TAG.sub(edit, model_bytes)


def canonical_model_parts(models):
    """Canonical bytes for the model parts that are not already canonical.

    ``models`` maps part name to bytes for every ``.model`` part of one package. Returns only the
    parts that change; raises UnsupportedThreeMFUnit for a unit that cannot be converted exactly.
    """
    changed = {}
    for name, data in models.items():
        out = default_core_namespace(data)
        unit = declared_unit(out)
        factor = 1.0
        if unit is not None and unit != 'millimeter':
            if name == ROOT_MODEL:
                factor = _unit_factor(unit, '')
            else:
                # An external production-extension part has no build of its own to carry the factor.
                # Its meshes are placed by the root's component references, so a unit the root does not
                # share cannot be folded in without guessing.
                if _unit_factor(unit, f' in {name}') != 1.0:
                    raise unit_refusal(f"'{unit}' in {name}; only the main model's unit is converted.")
            out = _set_millimeter(out)
        if factor != 1.0:
            out = _scale_build_items(out, factor, unit)
        if out != data:
            changed[name] = out
    return changed


def canonicalize_3mf(path, keep_original=None):
    """Rewrite the 3MF at path into canonical form, in place. Returns whether anything changed.

    A canonical file is not touched. ``keep_original``, when given, receives a copy of the user's
    bytes before they are replaced. Entry order and names are kept; the rewrite is atomic.
    """
    path = Path(path)
    with zipfile.ZipFile(path) as archive:
        names = [info.filename for info in archive.infolist() if info.filename.lower().endswith('.model')]
        changed = canonical_model_parts({name: archive.read(name) for name in names})
        if not changed:
            return False
        entries = [(info, changed.get(info.filename) if info.filename in changed else archive.read(info.filename))
                   for info in archive.infolist()]
    if keep_original is not None:
        keep_original = Path(keep_original)
        keep_original.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, keep_original)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.', suffix='.tmp')
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as out:
            for info, data in entries:
                out.writestr(info.filename, data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


def intake_working_copy(path):
    """Canonicalize a freshly downloaded or uploaded source in place; the user's bytes go to ./original/.

    Call once, right after the source passes the intake size checks and before anything reads it.
    STL files and canonical 3MF files are returned untouched. Returns the same path.
    """
    path = Path(path)
    # A missing file has nothing to canonicalize; the next reader reports it as it always has.
    if path.suffix.lower() == '.3mf' and path.is_file():
        canonicalize_3mf(path, keep_original=path.parent / 'original' / path.name)
    return path
