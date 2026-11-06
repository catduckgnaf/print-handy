"""Sliced .gcode.3mf packages for ZMod printers (#564).

OrcaSlicer's CLI emits plain ``plate_N.gcode`` on every channel Print Handy
pins; the ``.gcode.3mf`` the printer ecosystems trade in is a packaging step
over that validated G-code (the Bambu handoff in core/delivery.py is the
in-repo precedent). For ZMod 1.7.5+ the package is the better delivery
artifact: the ZMod wiki scopes the MD5 preprocessor and the file scan to
G-code only ("For 3MF files, scanning and color mapping are instant; the
preprocessor is not needed" — Global, SCAN_FILE_COLORS; FORCE_MD5 "only
applies when sending G-code"), KAMP data is instant for 3MF, and the AD5X
native screen can remap channels on a 3MF file but not on a G-code file.

The package layout mirrors Orca's own sliced export: the finalized G-code,
byte-identical to what the .gcode path sends, at ``Metadata/plate_1.gcode``
with its MD5 sidecar, a ``Metadata/plate_1.json`` written from the G-code's
own header comments, ``Metadata/slice_info.config``, and the plate previews
when they can be produced. slice_info.config is the file ZMod actually reads:
Klipper-side ``zmod.get_colors()`` maps each ``<filament id= type= color=>``
of the plate to tool ``id - 1``, and ZMod's Moonraker ``ThreeMFParser`` takes
time, weight, printer model and filament types/colours from the plate's
metadata and filaments. A slice_info.config without them still prints, but the
printer has no colours to map (#564 Stage 0, Q8 probe, layout A vs B). Same G-code in, same bytes out: the zip uses a
fixed timestamp, so one slice always packs to one package.

Format selection is version-aware. 3MF support is a ZMod 1.7.5 feature
(changelog sweep: the 3MF rows exist in 1.7.5 and no earlier release), so
``auto`` delivers 3MF only when the printer's ZMod version is known to be
new enough; older or unknown keeps today's .gcode, and an operator can
force either format explicitly.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

# The first ZMod release with 3MF support (wiki changelog, 1.7.5 section).
ZMOD_3MF_FLOOR = (1, 7, 5)
FORMATS = ("gcode", "3mf", "auto")
PACKAGE_SUFFIX = ".gcode.3mf"

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
 <Default Extension="gcode" ContentType="text/x.gcode"/>
 <Default Extension="json" ContentType="application/json"/>
 <Default Extension="png" ContentType="image/png"/>
</Types>
"""
_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""
_MODEL = """<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
 <metadata name="Application">Print Handy</metadata>
 <resources/>
 <build/>
</model>
"""
# Fixed timestamp: the same G-code always packs to the same bytes (as delivery.bambu_package).
_ZIP_TIME = (2020, 1, 1, 0, 0, 0)
_THUMBNAIL_NAMES = {"plate": "Metadata/plate_{n}.png", "plate_small": "Metadata/plate_{n}_small.png"}


def is_package(path) -> bool:
    return str(path).lower().endswith(PACKAGE_SUFFIX)


def inner_name(plate=1) -> str:
    return f"Metadata/plate_{int(plate)}.gcode"


def read_gcode_text(path) -> str:
    """The G-code text of a print artifact: the file itself, or the package's inner G-code.

    A package must hold exactly one plate G-code — the same rule the Bambu LAN
    print path applies — so an ambiguous or G-code-less package is refused,
    never guessed at.
    """
    path = Path(path)
    if not is_package(path):
        return path.read_text(errors="replace")
    with zipfile.ZipFile(path) as archive:
        plates = [n for n in archive.namelist()
                  if n.startswith("Metadata/plate_") and n.endswith(".gcode")]
        if plates != [inner_name(1)]:
            raise RuntimeError(
                f"{path.name} holds plates {plates or 'none'}; only a single-plate package "
                f"({inner_name(1)}) can be read")
        return archive.read(plates[0]).decode(errors="replace")


# Orca writes the comments plate_metadata reads in its header block and
# repeats its full config in the footer; both live within the first and last
# 64 KB of the file (#925). Metadata extraction searches those two slices
# instead of scanning a 50+ MB body whose middle is toolpath. A key found in
# the head keeps the head value — the same first match a whole-text search
# reported — and the tail is searched only for keys the head does not carry.
_METADATA_SLICE = 64 * 1024


def _metadata_slices(text):
    """The head and tail slices metadata is read from, in search order."""
    if len(text) <= 2 * _METADATA_SLICE:
        return (text,)
    return (text[:_METADATA_SLICE], text[-_METADATA_SLICE:])


def _as_parts(text_or_parts):
    """Metadata helpers take a whole text or a pre-sliced parts tuple."""
    if isinstance(text_or_parts, str):
        return _metadata_slices(text_or_parts)
    return tuple(text_or_parts)


def _search_metadata(parts, pattern):
    """First match of a metadata pattern, head slice before tail slice."""
    for part in _as_parts(parts):
        match = re.search(pattern, part, re.M)
        if match:
            return match
    return None


def _header_list(parts, key):
    match = _search_metadata(parts, rf"^; {re.escape(key)} = (.+)$")
    if not match:
        return []
    from .orca_config import split_config_list
    try:
        return [item.strip() for item in split_config_list(match.group(1))]
    except ValueError:
        return []


def _header_seconds(parts):
    """Print seconds from Orca's time headers, or None when the header names none."""
    match = _search_metadata(parts, r"^; estimated printing time \(normal mode\) = (.+)$")
    raw = match.group(1).strip() if match else None
    if raw is None:
        match = _search_metadata(parts, r"total estimated time:\s*([^;\n]+)")
        raw = match.group(1).strip() if match else None
    if not raw:
        return None
    total = 0.0
    seen = False
    for value, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([dhms])", raw):
        total += float(value) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
        seen = True
    return int(total) if seen else None


def _header_floats(parts, key):
    match = _search_metadata(parts, rf"^; {re.escape(key)} = (.+)$")
    if not match:
        return []
    out = []
    # Per-filament usage values are comma-separated in real Orca headers
    # ("20.00, 15.50"); the semicolon split is kept for the synthetic form.
    for item in re.split(r"[;,]", match.group(1)):
        try:
            out.append(float(item.strip()))
        except ValueError:
            out.append(None)
    return out


def _plate_metadata_from(parts, plate=1) -> dict:
    types = _header_list(parts, "filament_type")
    colours = _header_list(parts, "filament_colour")
    used_g = _header_floats(parts, "filament used [g]")
    filaments = []
    for index in range(max(len(types), len(colours))):
        row = {"id": index + 1}
        if index < len(types) and types[index]:
            row["type"] = types[index]
        if index < len(colours) and colours[index]:
            row["color"] = colours[index]
        if index < len(used_g) and used_g[index] is not None:
            row["used_g"] = used_g[index]
        filaments.append(row)
    meta = {"index": int(plate), "filaments": filaments}
    seconds = _header_seconds(parts)
    if seconds is not None:
        meta["prediction"] = seconds
    if used_g and all(v is not None for v in used_g):
        meta["weight"] = round(sum(used_g), 2)
    return meta


def plate_metadata(text, plate=1) -> dict:
    """Plate metadata for the package, sourced only from the G-code's own header comments.

    Filament types/colours are the per-tool lists Orca writes (`; filament_type`,
    `; filament_colour`); time and weight ride along when the header names them.
    Nothing is invented: a field the header does not carry is left out.
    Only the head and tail slices are searched (#925): those comments live
    in Orca's header block and footer config, never in the toolpath middle.
    """
    return _plate_metadata_from(_metadata_slices(text), plate)


def _xml_attr(value) -> str:
    return (str(value).replace("&", "&amp;").replace('"', "&quot;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _printer_model_id_from(parts):
    match = _search_metadata(parts, r"^; printer_model = (.+)$")
    if not match:
        return None
    return re.sub(r"^flashforge\s+", "", match.group(1).strip(), flags=re.I) or None


def _printer_model_id(text):
    """The model id Orca's own exports carry (`AD5X`), from `; printer_model = Flashforge AD5X`."""
    return _printer_model_id_from(_metadata_slices(text))


def _slice_info(plate, nozzle_mm, meta=None, printer_model_id=None):
    """slice_info.config in the shape Orca's sliced export writes for one plate.

    Filaments carry their 1-based slot id, so ZMod maps each to tool id - 1.
    Like Orca, a filament the header shows as unused (0 g) is left out; when
    the header names no usage, every filament is listed.
    """
    meta = meta or {}
    rows = [f'  <metadata key="index" value="{int(plate)}"/>\n']
    if printer_model_id:
        rows.append(f'  <metadata key="printer_model_id" value="{_xml_attr(printer_model_id)}"/>\n')
    if nozzle_mm:
        rows.append(f'  <metadata key="nozzle_diameters" value="{float(nozzle_mm):g}"/>\n')
    if meta.get("prediction") is not None:
        rows.append(f'  <metadata key="prediction" value="{int(meta["prediction"])}"/>\n')
    if meta.get("weight") is not None:
        rows.append(f'  <metadata key="weight" value="{float(meta["weight"]):.2f}"/>\n')
    for row in meta.get("filaments", []):
        if not row.get("type") and not row.get("color"):
            continue
        if row.get("used_g") is not None and row["used_g"] <= 0:
            continue
        attrs = (f'id="{int(row["id"])}" type="{_xml_attr(row.get("type", ""))}" '
                 f'color="{_xml_attr(row.get("color", ""))}"')
        if row.get("used_g") is not None:
            attrs += f' used_g="{float(row["used_g"]):.2f}"'
        rows.append(f"  <filament {attrs}/>\n")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<config>\n <header>\n'
            '  <header_item key="X-BBL-Client-Type" value="slicer"/>\n'
            '  <header_item key="X-Print-Handy" value="zmod 3mf delivery"/>\n </header>\n <plate>\n'
            + "".join(rows) + ' </plate>\n</config>\n')


def package(gcode, target, plate=1, nozzle_mm=0.4, thumbnails=None, printer_model_id=None) -> Path:
    """Write the .gcode.3mf for one validated, finalized G-code. Deterministic bytes.

    The inner G-code is stored byte-identical — every host-side check that ran
    on the .gcode (verification, mapping, MD5) reads the same bytes back out.
    `thumbnails` is a plate_thumbnail.Thumbnails or its images dict, as for
    delivery.bambu_package; previews are cosmetic and never required.
    `printer_model_id` defaults to the model the G-code's own header names.
    """
    gcode = Path(gcode)
    raw = gcode.read_bytes()
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    images = getattr(thumbnails, "images", thumbnails) or {}
    # Metadata comes from the head+tail slices only (#925): decoding the
    # whole 50+ MB body just to read its header and footer comments was a
    # full extra pass over the file. Small files keep the whole-text read.
    if len(raw) <= 2 * _METADATA_SLICE:
        parts = (raw.decode(errors="replace"),)
    else:
        parts = (raw[:_METADATA_SLICE].decode(errors="replace"),
                 raw[-_METADATA_SLICE:].decode(errors="replace"))
    meta = _plate_metadata_from(parts, plate)
    model_id = printer_model_id or _printer_model_id_from(parts)
    entries = [
        ("[Content_Types].xml", _CONTENT_TYPES.encode()),
        ("_rels/.rels", _RELS.encode()),
        ("3D/3dmodel.model", _MODEL.encode()),
        (inner_name(plate), raw),
        (inner_name(plate) + ".md5", hashlib.md5(raw).hexdigest().upper().encode()),
        (f"Metadata/plate_{int(plate)}.json",
         (json.dumps(meta, indent=1) + "\n").encode()),
        ("Metadata/slice_info.config", _slice_info(plate, nozzle_mm, meta, model_id).encode()),
    ] + [(name.format(n=int(plate)), images[role])
         for role, name in _THUMBNAIL_NAMES.items() if images.get(role)]
    tmp = target.with_name(target.name + ".part")
    # Compression level 1 instead of the default 6 (#925): deflate at level
    # 6 was the single-threaded tail of the packaging pass on a 50+ MB
    # plate. Measured locally on a 56.6 MB synthetic plate, level 1 packs
    # ~3.7x faster for a ~1.42x larger package (7.9 MB vs 5.6 MB — G-code
    # is line-oriented text whose redundancy is mostly captured at the
    # first level), and the format is unchanged: ZIP_DEFLATED at any level
    # reads back through every standard zip reader, ZMod's included. The
    # fixed timestamp keeps one G-code packing to one package layout; the
    # compressed bytes differ from a level-6 pack of the same G-code.
    # The level rides on each ZipInfo: with an explicit ZipInfo, zipfile
    # ignores the archive-level compresslevel and uses the entry's own.
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info._compresslevel = 1
            archive.writestr(info, data)
    tmp.replace(target)
    return target


def package_path_for(gcode) -> Path:
    """Where a G-code's package lives: alongside it, `<stem>.gcode.3mf` for `plate_1.gcode`."""
    gcode = Path(gcode)
    stem = gcode.name[:-len(".gcode")] if gcode.name.endswith(".gcode") else gcode.stem
    return gcode.parent / (stem + PACKAGE_SUFFIX)


# ------------------------------------------------------------------ format choice

def resolve_format(setting, zmod_version) -> tuple[str, str]:
    """(format, reason): the delivery format for one ZMod printer.

    `setting` is [printer] zmod_delivery_format ("gcode" | "3mf" | "auto");
    `zmod_version` is a version tuple like (1, 7, 5), or None when the
    printer did not report one. "auto" is the conservative default: 3MF only
    on positive evidence of a new-enough ZMod, .gcode otherwise — today's
    behaviour is what an unknown printer gets.
    """
    setting = str(setting or "gcode").strip().lower()
    if setting == "gcode":
        return "gcode", "setting is gcode"
    if setting == "3mf":
        return "3mf", "setting forces 3mf (operator override)"
    if setting != "auto":
        return "gcode", f"unknown setting {setting!r}; using gcode"
    if zmod_version is None:
        return "gcode", "ZMod version unknown; 3MF needs a known 1.7.5+"
    if tuple(zmod_version) >= ZMOD_3MF_FLOOR:
        return "3mf", f"ZMod {'.'.join(map(str, zmod_version))} supports 3MF"
    return "gcode", (f"ZMod {'.'.join(map(str, zmod_version))} predates 3MF support "
                     f"({'.'.join(map(str, ZMOD_3MF_FLOOR))})")


def version_from_status(status) -> tuple[int, ...] | None:
    """The installed ZMod version from a firmware_update.UpdateStatus, or None.

    None covers every "not proven" case alike: no zmod component, no current
    version on it, or a version string that does not parse.
    """
    component = status.component("zmod") if status is not None else None
    if component is None or not component.current:
        return None
    from .. import firmware_update
    parts = firmware_update.version_parts(component.current)
    return tuple(parts) if parts else None
