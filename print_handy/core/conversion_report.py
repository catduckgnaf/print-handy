"""Readable conversion report from slice provenance (#1525, tranche F).

The merged settings report (PR #1522) serves the JSON decisions recorded
in ``conversion_provenance``. This module renders the *same* record for
people: an HTML view for the Web UI and a plain-text view for chat. It
holds no second truth — every row is classified from the provenance
entry itself (project value, selected value, selected reason, origin),
and a decision the record does not support is shown as unknown, never
reclassified.

Row classes: preserved (selected == project), translated (a foreign
project whose value was carried under a different name — the record's
conversion notes name these), replaced by destination (selected reason
is the destination profile or a compatibility fallback), dropped
(project value present, selected absent), unknown (anything the record
cannot classify).

Operator reference profiles: audited for this tranche and deliberately
not added. The destination template system already *is* the versioned,
validated profile mechanism (templates carry their own identity and
vendor version, recorded by ``author_settings.destination_profile_identity``),
and the issue's own rule applies: uploading a random project does not
validate its printer profile. A second profile store would duplicate
the template system without adding validation.
"""
from __future__ import annotations

import html
import re

_REPLACED_REASONS = {"destination_profile", "compatibility_fallback",
                     "material_profile", "operator", "job"}


_CARRIED = re.compile(r"\bcarried as ([A-Za-z0-9_]+)=")


def translated_keys(conversion: dict) -> set[str]:
    """Destination keys the record's conversion notes say were carried under a new name.

    A foreign project's setting translated to an Orca key is noted as
    "<slicer> <source_key> carried as <orca_key>=<value>" (source_descriptor);
    those notes are the record's own statement that a row is a translation.
    """
    keys = set()
    for note in conversion.get("conversion_notes") or ():
        match = _CARRIED.search(str(note))
        if match:
            keys.add(match.group(1))
    return keys


def classify(entry: dict, *, translated: bool = False) -> str:
    """The report class of one provenance settings entry.

    ``translated`` is True when the record's conversion notes name this key
    as carried from a foreign project under a different name (#1681).
    """
    project = entry.get("project_value")
    selected = entry.get("selected_value")
    if project is None and selected is None:
        return "unknown"
    if selected is None:
        return "dropped"
    if project is None:
        return "unknown"
    if entry.get("selected_reason") in _REPLACED_REASONS and project != selected:
        return "replaced"
    if translated:
        return "translated"
    if project == selected:
        return "preserved"
    if entry.get("selected_reason") == "designer":
        return "preserved"
    return "unknown"


def build_report(conversion: dict, *, job_id: str | None = None) -> dict:
    """A structured readable report from one conversion_provenance record."""
    settings = conversion.get("settings") or {}
    carried = translated_keys(conversion)
    rows = []
    for key in sorted(settings):
        entry = settings[key] if isinstance(settings[key], dict) else {}
        rows.append({"setting": key, "class": classify(entry, translated=key in carried),
                     "project_value": entry.get("project_value"),
                     "selected_value": entry.get("selected_value"),
                     "selected_reason": entry.get("selected_reason"),
                     "origin": entry.get("origin"),
                     "reason": entry.get("reason")})
    counts = {}
    for row in rows:
        counts[row["class"]] = counts.get(row["class"], 0) + 1
    return {"schema_version": 1, "job_id": job_id,
            "source_format": conversion.get("source_format"),
            "source_slicer": conversion.get("source_slicer"),
            "source_slicer_version": conversion.get("source_slicer_version"),
            "destination_slicer_version": conversion.get("destination_slicer_version"),
            "destination_profile_name": conversion.get("destination_profile_name"),
            "intent_status": conversion.get("intent_status"),
            "governing_rules": list(conversion.get("governing_rules") or ()),
            "notes": list(conversion.get("conversion_notes") or ()),
            "unresolved_conflicts": list(conversion.get("unresolved_conflicts") or ()),
            "counts": counts, "settings": rows}


def render_text(report: dict) -> str:
    """The chat form: short lines, no markup."""
    lines = ["Conversion report"
             + (f" for job {report['job_id']}" if report.get("job_id") else "")]
    source = report.get("source_slicer") or report.get("source_format") or "unknown source"
    version = report.get("source_slicer_version")
    lines.append(f"Source: {source}" + (f" {version}" if version else ""))
    if report.get("destination_profile_name"):
        lines.append(f"Destination profile: {report['destination_profile_name']}")
    counts = report.get("counts") or {}
    if counts:
        lines.append("Settings: " + ", ".join(
            f"{counts[k]} {k}" for k in sorted(counts)))
    for row in report.get("settings") or ():
        if row["class"] == "preserved":
            continue
        lines.append(f"- {row['setting']}: {row['class']} "
                     f"(project {row['project_value']!r} -> selected "
                     f"{row['selected_value']!r})")
    for note in report.get("notes") or ():
        lines.append(f"Note: {note}")
    for conflict in report.get("unresolved_conflicts") or ():
        lines.append(f"Unresolved: {conflict.get('key')} — the project asked for "
                     f"{conflict.get('source_value')!r}, the destination profile "
                     f"carries {conflict.get('target_value')!r}.")
    return "\n".join(lines) + "\n"


def render_html(report: dict) -> str:
    """The Web UI form: a small standalone fragment, all values escaped."""
    def esc(value):
        return html.escape("" if value is None else str(value))

    rows = "".join(
        f"<tr><td>{esc(row['setting'])}</td><td>{esc(row['class'])}</td>"
        f"<td>{esc(row['project_value'])}</td><td>{esc(row['selected_value'])}</td>"
        f"<td>{esc(row['origin'])}</td></tr>"
        for row in report.get("settings") or ())
    notes = "".join(f"<li>{esc(note)}</li>" for note in report.get("notes") or ())
    return (
        '<section class="conversion-report"><h2>Conversion report</h2>'
        f"<p>Source: {esc(report.get('source_slicer') or report.get('source_format') or 'unknown')}"
        f" {esc(report.get('source_slicer_version') or '')} · "
        f"Destination profile: {esc(report.get('destination_profile_name') or 'unknown')} · "
        f"Designer intent: {esc(report.get('intent_status') or 'unknown')}</p>"
        "<table><thead><tr><th>Setting</th><th>Outcome</th><th>Project</th>"
        "<th>Selected</th><th>Origin</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        + (f"<ul>{notes}</ul>" if notes else "")
        + "</section>")
