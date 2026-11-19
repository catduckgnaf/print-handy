"""Automatic cost inputs from files Print Handy already handles (#1625).

G-code dialects (PrusaSlicer / OrcaSlicer footer, Bambu Studio header,
UltiMaker Cura header) are read from the head and tail of the file only.
Sliced 3MF files are read through ``Metadata/slice_info.config`` without
parsing the mesh. An unsliced 3MF, and a PrusaSlicer 3MF (which never
contains time or weight), produce the explicit not-sliced state instead
of a confident zero cost.

Resin: ChiTuBox and Lychee textual metadata headers are parsed where
they carry volume and time. The binary ChiTuBox container (.cxdlpv4)
is parsed for the resin volume and print time its header and print
parameters block carry. A Lychee .lys file is a saved scene (models
and preparation), not a slice: it carries neither value, so it lands
in the explicit not-sliced state with that reason instead of a guess.
"""
from __future__ import annotations

import io
import logging
import math
import re
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_HEAD_BYTES = 64 * 1024
_TAIL_BYTES = 256 * 1024
_SLICE_INFO_MAX = 16 * 1024 * 1024


@dataclass
class ParsedUsage:
    """What a file says about a job, with where it came from."""

    grams_per_filament: list = field(default_factory=list)
    total_grams: float | None = None
    wipe_tower_grams: float | None = None
    time_seconds: float | None = None
    volume_ml: float | None = None
    slicer_price_per_kg: float | None = None
    filament_types: list = field(default_factory=list)
    source: str = ""          # provenance label for the cost engine
    sliced: bool = True
    not_sliced_reason: str = ""

    @property
    def hours(self):
        return (self.time_seconds or 0) / 3600.0


def _floats(text):
    return [float(part) for part in str(text).split(",") if part.strip()]


def _duration_seconds(text):
    """'1h 2m 3s', '2d 1h', '25m 30s' or plain seconds to seconds."""
    text = str(text).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    total = 0.0
    matched = False
    for value, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([dhms])", text):
        matched = True
        total += float(value) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    return total if matched else None


def parse_gcode_text(text, *, density=1.24, diameter=1.75):
    """Parse one G-code text in any supported dialect. Returns ParsedUsage."""
    usage = ParsedUsage(source="slicer G-code")
    head = text[:_HEAD_BYTES]
    tail = text[-_TAIL_BYTES:] if len(text) > _TAIL_BYTES else text
    both = head + "\n" + tail

    # PrusaSlicer / OrcaSlicer footer (also the config dump at the tail).
    per_grams = None
    match = re.search(r";\s*filament used \[g\]\s*=\s*([0-9., ]+)", both)
    if match:
        per_grams = _floats(match.group(1))
    total = re.search(r";\s*total filament used \[g\]\s*=\s*([0-9.]+)", both)
    if total:
        usage.total_grams = float(total.group(1))
    tower = re.search(r";\s*total filament used for wipe tower \[g\]\s*=\s*([0-9.]+)", both)
    if tower:
        usage.wipe_tower_grams = float(tower.group(1))
    if per_grams:
        usage.grams_per_filament = per_grams
        if usage.total_grams is None:
            usage.total_grams = sum(per_grams)
    # Normal mode is preferred over silent mode.
    time_match = (re.search(r";\s*estimated printing time \(normal mode\)\s*=\s*([^\n;]+)", both)
                  or re.search(r";\s*estimated printing time\s*=\s*([^\n;]+)", both))
    if time_match:
        usage.time_seconds = _duration_seconds(time_match.group(1))
    price = re.search(r";\s*filament_cost\s*=\s*([0-9.]+)", tail)
    if price:
        usage.slicer_price_per_kg = float(price.group(1))
    types = re.search(r";\s*filament_type\s*=\s*([^\n]+)", tail)
    if types:
        usage.filament_types = [part.strip() for part in types.group(1).split(";") if part.strip()]

    # Bambu Studio header dialect (both ':' and '=' separator forms).
    if usage.total_grams is None:
        bambu = re.search(r";\s*total filament weight \[g\]\s*[:=]\s*([0-9.]+)", both)
        if bambu:
            usage.total_grams = float(bambu.group(1))
            usage.grams_per_filament = [usage.total_grams]
    if usage.time_seconds is None:
        bambu_time = (re.search(r";\s*total estimated time\s*[:=]\s*([^\n;]+)", both)
                      or re.search(r";\s*model printing time\s*[:=]\s*([^\n;]+)", both))
        if bambu_time:
            usage.time_seconds = _duration_seconds(bambu_time.group(1))

    # UltiMaker Cura header: length only, plus time in seconds.
    if usage.total_grams is None:
        cura = re.search(r";Filament used:\s*([0-9., ]+)m\b", head)
        if cura:
            lengths_m = _floats(cura.group(1))
            area_mm2 = math.pi * (diameter / 2.0) ** 2
            grams = [length * 1000.0 * area_mm2 * density / 1000.0 for length in lengths_m]
            usage.grams_per_filament = grams
            usage.total_grams = sum(grams)
    if usage.time_seconds is None:
        cura_time = re.search(r";TIME:\s*(\d+)", head)
        if cura_time:
            usage.time_seconds = float(cura_time.group(1))
    return usage


def read_head_tail(path):
    """The head and tail of a file as one text (the whole file when small)."""
    path = Path(path)
    size = path.stat().st_size
    with open(path, "rb") as handle:
        if size <= _HEAD_BYTES + _TAIL_BYTES:
            return handle.read().decode("utf-8", errors="replace")
        head = handle.read(_HEAD_BYTES)
        handle.seek(-_TAIL_BYTES, 2)
        tail = handle.read(_TAIL_BYTES)
    return head.decode("utf-8", errors="replace") + "\n" + tail.decode("utf-8", errors="replace")


def parse_gcode_file(path, **kwargs):
    return parse_gcode_text(read_head_tail(path), **kwargs)


def parse_slice_info(xml_text):
    """One slice_info.config XML text to a ParsedUsage (sliced state)."""
    import xml.etree.ElementTree as ET

    usage = ParsedUsage(source="3MF slice info")
    root = ET.fromstring(xml_text)
    total_weight = 0.0
    found = False
    for plate in root.iter():
        if plate.tag.rsplit("}", 1)[-1] != "plate":
            continue
        grams = []
        for node in plate.iter():
            kind = node.tag.rsplit("}", 1)[-1]
            if kind == "metadata":
                key, value = node.get("key"), node.get("value")
                if key == "prediction" and value:
                    usage.time_seconds = float(value)
                    found = True
                elif key == "weight" and value:
                    total_weight = float(value)
                    found = True
            elif kind == "filament":
                used_g = node.get("used_g")
                if used_g:
                    grams.append(float(used_g))
                    found = True
                if node.get("type"):
                    usage.filament_types.append(node.get("type"))
        if grams:
            usage.grams_per_filament = grams
    if usage.grams_per_filament:
        usage.total_grams = sum(usage.grams_per_filament)
    elif total_weight:
        usage.total_grams = total_weight
        usage.grams_per_filament = [total_weight]
    usage.sliced = found
    if not found:
        usage.not_sliced_reason = "not sliced yet: slice the file or enter values manually"
    return usage


def parse_3mf_bytes(data: bytes):
    """A 3MF package's usage, or the explicit not-sliced state."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return ParsedUsage(sliced=False, source="3MF slice info",
                           not_sliced_reason="not a readable 3MF package")
    names = archive.namelist()
    slice_info = next((n for n in names if n.endswith("slice_info.config")), None)
    if slice_info is not None:
        info = archive.getinfo(slice_info)
        if info.file_size <= _SLICE_INFO_MAX:
            usage = parse_slice_info(archive.read(slice_info).decode("utf-8", errors="replace"))
            if usage.sliced:
                return usage
    # A PrusaSlicer 3MF never contains time or weight: name it plainly.
    settings_blob = b""
    for name in names:
        if name.endswith(("project_settings.config", "model_settings.config")):
            try:
                settings_blob += archive.read(name)[:_HEAD_BYTES]
            except Exception as exc:
                logger.debug("cost parse: could not read %s in the 3MF: %s", name, exc)
    model_head = b""
    model_name = next((n for n in names if n.endswith(".model")), None)
    if model_name:
        try:
            model_head = archive.read(model_name)[:_HEAD_BYTES]
        except Exception as exc:
            logger.debug("cost parse: could not read the 3MF model part: %s", exc)
    if b"PrusaSlicer" in settings_blob or b"PrusaSlicer" in model_head:
        return ParsedUsage(sliced=False, source="3MF slice info",
                           not_sliced_reason=("PrusaSlicer 3MF files carry no time or weight: "
                                              "export the G-code or enter values manually"))
    return ParsedUsage(sliced=False, source="3MF slice info",
                       not_sliced_reason="not sliced yet: slice the file or enter values manually")


def parse_3mf_file(path):
    return parse_3mf_bytes(Path(path).read_bytes())


# --- resin textual metadata -------------------------------------------------

_RESIN_VOLUME_PATTERNS = (
    re.compile(r"resin[ _-]*volume[^\d]{0,12}([0-9]+(?:\.[0-9]+)?)\s*mL", re.I),
    re.compile(r"volume[ _-]*ml\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)", re.I),
    re.compile(r"\"volume\"\s*:\s*([0-9]+(?:\.[0-9]+)?)", re.I),
)
_RESIN_TIME_PATTERNS = (
    re.compile(r"print[ _-]*time\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)\s*(s|sec|seconds)?\b", re.I),
    re.compile(r"\"printTime\"\s*:\s*([0-9]+)", re.I),
    re.compile(r"estimated[ _-]*time\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)\s*(s|sec)", re.I),
)


def parse_resin_text(text):
    """ChiTuBox / Lychee style textual metadata: volume mL and print time.

    Returns None when the text carries neither, so the caller falls
    through rather than reporting a guessed volume. The binary
    containers are dispatched by extension in usage_from_file before
    this textual fallback runs.
    """
    volume = None
    for pattern in _RESIN_VOLUME_PATTERNS:
        match = pattern.search(text)
        if match:
            volume = float(match.group(1))
            break
    seconds = None
    for pattern in _RESIN_TIME_PATTERNS:
        match = pattern.search(text)
        if match:
            seconds = float(match.group(1))
            break
    if volume is None and seconds is None:
        return None
    return ParsedUsage(volume_ml=volume, time_seconds=seconds,
                       source="resin file metadata")


# --- resin binary containers ------------------------------------------------
#
# ChiTuBox .cxdlpv4 (the Creality HALOT / ChiTuBox v4 sliced format).
# Layout, cross-checked between two independent open-source
# implementations that read and write real files: UVtools
# (UVtools.Core/FileFormats/CrealityCXDLPv4File.cs) and Danslicer
# (Danslicer.Core/IO/ChituWriter.cs):
#
#   offset 0   uint32 big-endian: magic size, always 9
#   offset 4   9 bytes: the magic "CXSW3DV2" plus a NUL
#   offset 13  uint16 big-endian: format version, 4 for .cxdlpv4
#   offset 15  uint32 big-endian: printer model length, NUL included,
#              followed by that many model bytes
#   then little-endian fields from the end of the model string:
#              resolution X/Y (uint16 each), bed X/Y/Z, print height and
#              layer height (floats), bottom layer count, preview and
#              layer-table offsets, layer count, print time in seconds
#              (uint32, at model end + 44), projector type, the print
#              parameters block offset and size (68 bytes), anti-alias
#              level, light PWM values, the layer encryption key and
#              the slicer table offset and size.
# The print parameters block (little-endian floats) carries the resin
# volume in milliliters at offset 20, after the lift and retract
# settings; weight and cost estimates follow it. The header and the
# parameters block are stored unencrypted; the encryption key applies
# to layer image data only, which this parser never reads.

_CXDLPV4_MAGIC = b"CXSW3DV2\x00"
_CXDLPV4_PARAMS_SIZE = 68
_CXDLPV4_VOLUME_OFFSET = 20
_CXDLPV4_SOURCE = "ChiTuBox .cxdlpv4 container"
_LYS_SOURCE = "Lychee .lys container"
_LYS_SCAN_BYTES = 2 * 1024 * 1024


def _cxdlpv4_header(head):
    """(fields, None) or (None, reason) from .cxdlpv4 header bytes."""
    if len(head) < 13:
        return None, "the file ends inside the .cxdlpv4 header"
    magic_size = struct.unpack_from(">I", head, 0)[0]
    if magic_size != len(_CXDLPV4_MAGIC) or head[4:13] != _CXDLPV4_MAGIC:
        return None, "the header does not carry the CXSW3DV2 magic"
    if len(head) < 19:
        return None, "the file ends inside the .cxdlpv4 header"
    version = struct.unpack_from(">H", head, 13)[0]
    if version != 4:
        return None, (f"the container is CXDLP version {version}, "
                      "not the version 4 .cxdlpv4 layout")
    model_len = struct.unpack_from(">I", head, 15)[0]
    if model_len < 1 or model_len > 1024:
        return None, "the printer model name in the header is not readable"
    base = 19 + model_len
    if len(head) < base + 80:
        return None, "the file ends inside the .cxdlpv4 header"
    fields = {
        "layer_count": struct.unpack_from("<I", head, base + 36)[0],
        "print_time": struct.unpack_from("<I", head, base + 44)[0],
        "params_offset": struct.unpack_from("<I", head, base + 52)[0],
        "params_size": struct.unpack_from("<I", head, base + 56)[0],
    }
    return fields, None


def _cxdlpv4_usage(fields, params):
    """A sliced ParsedUsage, or the cannot-estimate state with a reason."""
    if params is None or len(params) < _CXDLPV4_VOLUME_OFFSET + 4:
        return ParsedUsage(
            sliced=False, source=_CXDLPV4_SOURCE,
            not_sliced_reason=("the print parameters block is missing or "
                               "truncated in the .cxdlpv4 container"))
    volume = struct.unpack_from("<f", params, _CXDLPV4_VOLUME_OFFSET)[0]
    if not math.isfinite(volume) or volume <= 0:
        return ParsedUsage(
            sliced=False, source=_CXDLPV4_SOURCE,
            not_sliced_reason="the .cxdlpv4 container carries no resin volume")
    if not fields["layer_count"]:
        return ParsedUsage(
            sliced=False, source=_CXDLPV4_SOURCE,
            not_sliced_reason="the .cxdlpv4 container carries no layers")
    seconds = fields["print_time"]
    return ParsedUsage(volume_ml=volume,
                       time_seconds=float(seconds) if seconds else None,
                       source=_CXDLPV4_SOURCE)


def _cxdlpv4_params_from(data, fields):
    offset = fields["params_offset"]
    if offset <= 0 or fields["params_size"] < _CXDLPV4_VOLUME_OFFSET + 4:
        return None
    if offset + _CXDLPV4_VOLUME_OFFSET + 4 > len(data):
        return None
    return data[offset:offset + _CXDLPV4_PARAMS_SIZE]


def parse_cxdlpv4_bytes(data: bytes):
    """One .cxdlpv4 container's usage, or the cannot-estimate state."""
    fields, reason = _cxdlpv4_header(data[:_HEAD_BYTES])
    if fields is None:
        return ParsedUsage(sliced=False, source=_CXDLPV4_SOURCE,
                           not_sliced_reason=reason)
    return _cxdlpv4_usage(fields, _cxdlpv4_params_from(data, fields))


def parse_cxdlpv4_file(path):
    """parse_cxdlpv4_bytes with bounded reads: header, then the block."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(_HEAD_BYTES)
            fields, reason = _cxdlpv4_header(head)
            if fields is None:
                return ParsedUsage(sliced=False, source=_CXDLPV4_SOURCE,
                                   not_sliced_reason=reason)
            params = None
            offset = fields["params_offset"]
            if offset > 0 and fields["params_size"] >= _CXDLPV4_VOLUME_OFFSET + 4:
                handle.seek(offset)
                params = handle.read(_CXDLPV4_PARAMS_SIZE)
    except OSError:
        return ParsedUsage(sliced=False, source=_CXDLPV4_SOURCE,
                           not_sliced_reason="could not read the file")
    return _cxdlpv4_usage(fields, params)


def parse_lys_bytes(data: bytes):
    """A Lychee .lys file's usage: always the cannot-estimate state.

    A .lys is a saved Lychee scene (models and preparation work in a
    "mango" container, or the same members in a ZIP), not a slice.
    Neither the container manifest (a version plus a file index) nor
    the encoded scene payload carries resin volume or print time;
    Lychee computes both only when a slice is exported to a printer
    file. So the honest answer is the explicit cannot-estimate state,
    never a volume derived from the stored meshes.
    """
    if data[:4] == b"PK\x03\x04" or b'"mangoFiles"' in data[:_LYS_SCAN_BYTES]:
        return ParsedUsage(
            sliced=False, source=_LYS_SOURCE,
            not_sliced_reason=("Lychee .lys files are saved scenes (models and "
                               "preparation); they do not carry resin volume or "
                               "print time, which Lychee computes only when a "
                               "slice is exported: export the slice or enter "
                               "values manually"))
    return ParsedUsage(sliced=False, source=_LYS_SOURCE,
                       not_sliced_reason="not a readable Lychee .lys container")


def parse_lys_file(path):
    try:
        with open(path, "rb") as handle:
            data = handle.read(_LYS_SCAN_BYTES)
    except OSError:
        return ParsedUsage(sliced=False, source=_LYS_SOURCE,
                           not_sliced_reason="could not read the file")
    return parse_lys_bytes(data)


def usage_from_file(path):
    """Best automatic usage for one dropped file, by its kind.

    .gcode parses directly; .3mf goes through slice_info; a sliced
    .gcode.3mf package's inner plate G-code is parsed when slice_info
    carries nothing. Resin textual files parse for volume and time;
    a .cxdlpv4 container parses for both from its header and print
    parameters block; a .lys scene returns its cannot-estimate state.
    Anything else returns the not-sliced state.
    """
    path = Path(path)
    name = path.name.lower()
    if name.endswith(".gcode") or name.endswith(".gco"):
        return parse_gcode_file(path)
    if name.endswith(".3mf"):
        usage = parse_3mf_file(path)
        if usage.sliced:
            return usage
        try:
            with zipfile.ZipFile(path) as archive:
                plates = [n for n in archive.namelist()
                          if n.startswith("Metadata/plate_") and n.endswith(".gcode")]
                if len(plates) == 1:
                    inner = archive.read(plates[0]).decode("utf-8", errors="replace")
                    inner_usage = parse_gcode_text(inner)
                    if inner_usage.total_grams is not None:
                        inner_usage.source = "3MF plate G-code"
                        return inner_usage
        except zipfile.BadZipFile as exc:
            logger.debug("cost parse: the .gcode.3mf payload is not readable: %s", exc)
        return usage
    if name.endswith(".cxdlpv4"):
        return parse_cxdlpv4_file(path)
    if name.endswith(".lys"):
        return parse_lys_file(path)
    try:
        text = read_head_tail(path)
    except OSError:
        return ParsedUsage(sliced=False, not_sliced_reason="could not read the file")
    resin = parse_resin_text(text)
    if resin is not None:
        return resin
    return ParsedUsage(sliced=False,
                       not_sliced_reason="not sliced yet: slice or enter values manually")
