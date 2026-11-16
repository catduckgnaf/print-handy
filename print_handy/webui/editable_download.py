"""Editable 3MF export downloads for archived jobs (#1525).

The request names a job, never a path: the job's own JOB.json must name
its source file, the source must exist and be a .3mf, and the export is
built fresh by ``core.editable_export`` into the job's own folder. A
sliced-only or unreadable source resolves to None here and the route
answers with the export's concrete refusal reason instead.
"""
from __future__ import annotations

import json
from pathlib import Path

from print_handy.core import editable_export

from .sliced import _load_job

_SOURCE_KEYS = ("source_file", "source", "model_file")


def _source_path(job_path: Path, payload: dict) -> Path | None:
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
        path = Path(str(raw))
        if not path.is_absolute():
            path = job_path.parent / path
        try:
            path = path.resolve()
        except OSError:
            continue
        if path.is_file() and path.suffix.lower() == ".3mf":
            return path
    # A job folder often keeps its source beside JOB.json.
    for sibling in sorted(job_path.parent.glob("*.3mf")):
        if "editable" not in sibling.name:
            return sibling
    return None


def export_for_job(archive: Path, job_id: str) -> tuple[Path, dict] | tuple[None, str]:
    """(export path, report) for a job, or (None, refusal reason)."""
    loaded = _load_job(Path(archive), job_id)
    if loaded is None:
        return None, "no such job"
    job_path, payload = loaded
    source = _source_path(job_path, payload)
    if source is None:
        return None, "this job does not name a source 3MF to export"
    target = job_path.parent / f"{source.stem}-editable.3mf"
    try:
        report = editable_export.export_editable(source, target)
    except editable_export.ExportRefused as exc:
        return None, str(exc)
    return target, report
