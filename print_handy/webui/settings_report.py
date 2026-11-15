"""Downloadable, evidence-based settings decisions from recorded slice provenance."""
from __future__ import annotations

import json
from pathlib import Path

from .sliced import _load_job


# Who set a selected value that differs from the project's value, as
# recorded by core.author_settings.conversion_provenance (selected_reason).
LAYER_REASONS = {
    "job": "This job's own request set a different value.",
    "operator": "An operator setting set a different value.",
    "material_profile": "The loaded filament's material profile set a different value.",
    "compatibility_fallback": "A compatibility rule replaced a value this printer or slicer cannot use.",
    "destination_profile": "The destination printer profile's value was used instead.",
}


def has_report(job: dict) -> bool:
    """True when the job recorded conversion provenance with per-setting rows."""
    provenance = job.get("provenance") if isinstance(job, dict) else None
    conversion = (provenance.get("conversion_provenance")
                  if isinstance(provenance, dict) else None)
    return isinstance(conversion, dict) and isinstance(conversion.get("settings"), dict)


def report_payload(archive: Path, job_id: str) -> dict | None:
    loaded = _load_job(Path(archive), job_id)
    if loaded is None:
        return None
    _path, job = loaded
    provenance = job.get("provenance")
    conversion = (provenance.get("conversion_provenance")
                  if isinstance(provenance, dict) else None)
    if not isinstance(conversion, dict) or not isinstance(conversion.get("settings"), dict):
        return None
    rows = []
    for key, item in sorted(conversion["settings"].items()):
        if not isinstance(item, dict):
            continue
        source = item.get("project_value")
        selected = item.get("selected_value")
        reason = item.get("selected_reason")
        if source is None or selected is None:
            continue
        if source == selected:
            status, explanation = "kept", "The slice used the project's own value."
        elif reason in LAYER_REASONS:
            status, explanation = "replaced", LAYER_REASONS[reason]
        else:
            # A difference with no recorded layer ('unattributed', 'designer'
            # with a different value, or anything newer) is not classified.
            continue
        rows.append({"setting": key, "status": status, "source_value": source,
                     "selected_value": selected, "reason": explanation,
                     "selection_reason": reason})
    return {"schema_version": 1, "job_id": job_id,
            "source_format": conversion.get("source_format"),
            "intent_status": conversion.get("intent_status"),
            "settings": rows,
            "limitations": "Only per-setting decisions explicitly supported by recorded slice provenance are listed. "
                           "Absent or ambiguous decisions are omitted, not treated as unchanged."}


def render_json(payload: dict) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
