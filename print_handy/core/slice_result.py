"""Production glue for the #1525 slice-result surfaces.

`core.plate_statistics` builds the per-plate statistics artifact and
`core.u1_feasibility` builds the U1 report, but both are library code:
they take caller-supplied text and records. This module is the caller
the chat command, the Web UI pages and the tests share — it resolves an
archived job the same way the sliced-downloads page does (the request
names a job, never a path; the job's own JOB.json names its files, and
every file must resolve inside the job's own folder), reads the plate
G-code and any statistics JSON the slicer left beside it, takes the
logical filaments from the job record or the source inventory, and
hands the artifact builders exactly what the files prove. Anything a
file does not state stays unknown in the artifact, and nothing here
slices, prints, sends a printer command or touches inventory.
"""
from __future__ import annotations

import json
import logging
import re
import zipfile
from pathlib import Path

from . import plate_statistics, plate_thumbnail, source_colors, u1_feasibility

logger = logging.getLogger(__name__)

_JOB_ID = re.compile(r"^[A-Za-z0-9._-]{1,136}$")
_PLATE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,120}\.gcode(\.3mf)?$")
_SOURCE_KEYS = ("source_file", "source", "model_file")


def load_job(archive, job_id):
    """(JOB.json path, payload) for one archived job, or None.

    The same containment rule as the Web UI's sliced page: the job id is
    an archive folder name, and the JOB.json must resolve inside the
    archive.
    """
    if not _JOB_ID.fullmatch(str(job_id or "")) or job_id in (".", ".."):
        return None
    root = Path(archive).resolve()
    job = root / str(job_id) / "JOB.json"
    try:
        job = job.resolve()
    except OSError:
        return None
    if not job.is_relative_to(root) or not job.is_file():
        return None
    try:
        payload = json.loads(job.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return job, payload


def _inside(job_path: Path, raw) -> Path | None:
    """A file the job names, resolved inside the job's own folder only."""
    if not raw:
        return None
    path = Path(str(raw))
    if not path.is_absolute():
        path = job_path.parent / path
    try:
        path = path.resolve()
        root = job_path.parent.resolve()
    except OSError:
        return None
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path


def plate_files(job_path: Path, payload: dict) -> list[Path]:
    """The job's plate G-code files, in JOB.json order, de-duplicated."""
    candidates = [payload.get("gcode")] + [
        row.get("gcode") for row in payload.get("derived_plates") or []
        if isinstance(row, dict)]
    out, seen = [], set()
    for candidate in candidates:
        path = _inside(job_path, candidate)
        if path is None or path in seen or not _PLATE_NAME.fullmatch(path.name):
            continue
        seen.add(path)
        out.append(path)
    return out


def source_path(job_path: Path, payload: dict) -> Path | None:
    """The job's source .3mf inside its own folder, or None."""
    candidates = []
    for key in _SOURCE_KEYS:
        if payload.get(key):
            candidates.append(payload[key])
    provenance = payload.get("provenance")
    if isinstance(provenance, dict):
        for key in _SOURCE_KEYS:
            if provenance.get(key):
                candidates.append(provenance[key])
    for raw in candidates:
        path = _inside(job_path, raw)
        if path is not None and path.suffix.lower() == ".3mf":
            return path
    for sibling in sorted(job_path.parent.glob("*.3mf")):
        if "editable" not in sibling.name:
            return sibling
    return None


def _filaments(job_path: Path, payload: dict) -> list[dict]:
    """Logical filaments: the job's color manifest, else the source inventory.

    Falls back to one unnamed filament so a plain single-filament slice
    still gets its row (with unknown material and color, never invented).
    """
    manifest = payload.get("color_manifest")
    if not (isinstance(manifest, dict) and manifest.get("filaments")):
        audit = payload.get("audit")
        if isinstance(audit, dict):
            manifest = audit.get("color_manifest")
    if isinstance(manifest, dict) and manifest.get("filaments"):
        return [dict(row) for row in manifest["filaments"] if isinstance(row, dict)]
    source = source_path(job_path, payload)
    if source is not None:
        try:
            inventory = source_colors.inventory(source)
        except Exception:
            logger.warning("slice result: source inventory unreadable for %s",
                           source, exc_info=True)
            inventory = {}
        if inventory.get("filaments"):
            return [dict(row) for row in inventory["filaments"]]
    return [{"source_extruder": 1, "material": None, "color": None}]


def _slot_map(payload: dict) -> dict:
    for key in ("tool_to_physical_slot", "slot_map"):
        mapping = payload.get(key)
        if isinstance(mapping, dict) and mapping:
            return dict(mapping)
    multislot = payload.get("multislot")
    if isinstance(multislot, dict):
        mapping = multislot.get("tool_to_physical_slot")
        if isinstance(mapping, dict) and mapping:
            return dict(mapping)
    return {}


def _read_plate(path: Path) -> tuple[str, bool]:
    """(G-code text, thumbnail present) for one plate file.

    A .gcode.3mf plate carries its toolpaths and thumbnail inside the
    package; a plain .gcode carries both as comments. A package that
    cannot be read proves neither and yields empty text, so every
    statistic for it stays unknown rather than guessed.
    """
    if path.name.endswith(".3mf"):
        try:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                thumbnail = any(name.startswith("Metadata/plate_")
                                and name.endswith(".png") for name in names)
                gcode_names = [name for name in names if name.endswith(".gcode")]
                text = (archive.read(gcode_names[0]).decode("utf-8", "replace")
                        if gcode_names else "")
                return text, thumbnail
        except (OSError, zipfile.BadZipFile):
            return "", False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", False
    return text, bool(plate_thumbnail.embedded_thumbnails(text))


def _statistics_text(job_path: Path, plate: Path) -> str | None:
    """The statistics JSON the slicer left beside this plate, if any."""
    stem = plate.name.removesuffix(".3mf").removesuffix(".gcode")
    for candidate in (job_path.parent / f"{stem}.statistics.json",
                      job_path.parent / "statistics.json"):
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return None


def result_for_job(archive, job_id, *, spools=()):
    """The per-plate statistics artifacts for one archived job, or None.

    One artifact per plate file the job records. ``spools`` is the
    caller's normalized spool list ({id, material, color} rows) used for
    the deterministic suggestions; with no list, suggestions are empty
    and the card says the mapping is unconfirmed rather than guessing.
    """
    loaded = load_job(archive, job_id)
    if loaded is None:
        return None
    job_path, payload = loaded
    plates = plate_files(job_path, payload)
    if not plates:
        return None
    filaments = _filaments(job_path, payload)
    slot_map = _slot_map(payload)
    artifacts = []
    for plate in plates:
        text, thumbnail = _read_plate(plate)
        artifacts.append(plate_statistics.build_plate_statistics(
            gcode_text=text,
            statistics_text=_statistics_text(job_path, plate),
            filaments=filaments, slot_map=slot_map, spools=spools,
            job_id=str(job_id), plate_id=plate.name,
            printer_id=payload.get("printer_id"),
            thumbnail_present=thumbnail,
            print_started=bool(payload.get("started_at") or payload.get("print_number")),
            completion_state="recorded" if payload.get("print_number") else "not_started"))
    return artifacts


def thumbnail_png(archive, job_id, plate_index=0):
    """The plate's actual slicer thumbnail PNG bytes, or None.

    The thumbnail the slicer itself embedded — a ``Metadata/plate_*.png``
    member of a .gcode.3mf package, or the largest embedded thumbnail
    block of a plain .gcode — never a geometry render standing in for
    one. None when the plate carries none.
    """
    loaded = load_job(archive, job_id)
    if loaded is None:
        return None
    job_path, payload = loaded
    plates = plate_files(job_path, payload)
    if not (0 <= plate_index < len(plates)):
        return None
    plate = plates[plate_index]
    if plate.name.endswith(".3mf"):
        try:
            with zipfile.ZipFile(plate) as package:
                members = [name for name in package.namelist()
                           if name.startswith("Metadata/plate_")
                           and name.endswith(".png")]
                if not members:
                    return None
                return package.read(sorted(members)[0])
        except (OSError, zipfile.BadZipFile):
            return None
    text, _present = _read_plate(plate)
    found = plate_thumbnail.embedded_thumbnails(text)
    return found[0][2] if found else None


def u1_report_for_job(archive, job_id):
    """The U1 feasibility report for one archived job's source, or None."""
    loaded = load_job(archive, job_id)
    if loaded is None:
        return None
    job_path, payload = loaded
    source = source_path(job_path, payload)
    if source is None:
        return None
    return u1_feasibility.feasibility(source)


def render_text(artifacts) -> str:
    """The chat form of the result card: one block per plate.

    Estimated grams are labeled estimates, unknowns are stated as
    unknown, and spool suggestions name their confidence — an exact
    material/color match or a partial one — because a suggestion is
    never the mounted roll until the operator confirms it.
    """
    lines = []
    for artifact in artifacts or ():
        lines.append(f"Slice result — {artifact.get('plate_id') or 'plate'}"
                     f" (job {artifact.get('job_id') or 'unknown'})")
        lines.append(f"Thumbnail: {'yes' if artifact.get('thumbnail_present') else 'not present'}")
        layers = artifact.get("layer_count")
        lines.append(f"Layers: {layers if layers is not None else 'unknown'}")
        lines.append(f"Estimated time: {artifact.get('estimated_time') or 'unknown'}")
        for row in artifact.get("filaments") or ():
            grams = row.get("estimated_grams")
            grams_text = (f"estimated {grams:g} g" if grams is not None
                          else "estimated grams unknown")
            slot = row.get("physical_slot")
            slot_text = f"slot {slot}" if slot is not None else "slot unmapped"
            material = row.get("material") or "material unknown"
            color = row.get("color") or "color unknown"
            lines.append(f"- filament {row.get('logical_filament')}: {material}, "
                         f"{color} — {grams_text}, {slot_text}")
            for suggestion in row.get("spool_suggestions") or ():
                lines.append(f"  spool suggestion: #{suggestion.get('spool_id')} "
                             f"({suggestion.get('confidence')} match — confirm before printing)")
        total = artifact.get("total_estimated_grams")
        lines.append(f"Total: {f'estimated {total:g} g' if total is not None else 'unknown'}")
        lines.append(artifact.get("note") or "")
    return "\n".join(lines) + ("\n" if lines else "")
