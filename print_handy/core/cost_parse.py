"""Automatic cost inputs from files Print Handy already handles (#1625).

G-code dialects (PrusaSlicer / OrcaSlicer footer, Bambu Studio header,
UltiMaker Cura header) are read from the head and tail of the file only.
Sliced 3MF files are read through ``Metadata/slice_info.config`` without
parsing the mesh. An unsliced 3MF, and a PrusaSlicer 3MF (which never
contains time or weight), produce the explicit not-sliced state instead
of a confident zero cost.

Resin: ChiTuBox and Lychee textual metadata headers are parsed where
they carry volume and time. The binary ChiTuBox project format
(.cxdlpv4) and the Lychee .lys container are not parsed; callers name
that remainder instead of guessing.
"""
from __future__ import annotations

import io
import logging
import math
import re
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

    Returns None when the text carries neither, so binary project
    formats (.cxdlpv4, .lys) fall through to the named remainder rather
    than a guessed volume.
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


def usage_from_file(path):
    """Best automatic usage for one dropped file, by its kind.

    .gcode parses directly; .3mf goes through slice_info; a sliced
    .gcode.3mf package's inner plate G-code is parsed when slice_info
    carries nothing. Resin textual files parse for volume and time.
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
    try:
        text = read_head_tail(path)
    except OSError:
        return ParsedUsage(sliced=False, not_sliced_reason="could not read the file")
    resin = parse_resin_text(text)
    if resin is not None:
        return resin
    return ParsedUsage(sliced=False,
                       not_sliced_reason="not sliced yet: slice or enter values manually")
