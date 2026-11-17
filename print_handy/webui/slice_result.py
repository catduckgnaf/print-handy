"""The Web UI slice-result card and U1 feasibility pages (#1525).

Both pages are read-only views over an archived job, built by
``core.slice_result`` from the job's own recorded files: the result
card shows the plate thumbnail presence, layer count, estimated time
and estimated grams per logical filament and per mapped slot with the
spool-match confidence `core.plate_statistics` computed, and the U1
page shows `core.u1_feasibility`'s report for the job's source. Neither
page slices, prints, sends a printer command or touches inventory, and
a value the files do not prove is shown as unknown, never filled in.
"""
from __future__ import annotations

import logging

from print_handy.core import slice_result, u1_feasibility

from . import shell

_esc = shell._esc
logger = logging.getLogger(__name__)


def _spool_rows(data):
    """Normalized Spoolman spools for suggestions, or () when unreachable.

    Suggestions are read-only: a Spoolman outage simply means no
    suggestions are shown, and the card says the mapping is unconfirmed.
    """
    try:
        from print_handy.core import spoolman_client
        rows = spoolman_client.list_spools() or ()
    except Exception:
        logger.warning("slice result: Spoolman did not answer; the card "
                       "shows no spool suggestions", exc_info=True)
        return ()
    from print_handy.core import filament_color
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        filament = row.get("filament") if isinstance(row.get("filament"), dict) else {}
        hex_value = filament_color.first_hex(
            filament.get("color_hex") or filament.get("color"))
        out.append({"id": row.get("id"),
                    "material": filament.get("material"),
                    # The inventory's form is #RRGGBB; Spoolman's color_hex
                    # carries no '#', and the suggestion matcher compares
                    # the two spellings as different colors.
                    "color": f"#{hex_value}" if hex_value else None})
    return out


def result_artifacts(data, archive, job_id):
    return slice_result.result_for_job(archive, job_id, spools=_spool_rows(data))


def render_result_page(artifacts, job_id, *, theme=shell.DEFAULT_THEME, nav=None,
                       has_source=False) -> str:
    parts = ["<p>The slice's own numbers for this job, read from its plate "
             "files. Grams are the slicer's estimates, not measurements, and "
             "slicing alone never deducts inventory. A spool suggestion is a "
             "match to confirm, never the mounted roll.</p>"]
    for index, artifact in enumerate(artifacts or ()):
        parts.append(f"<h2>{_esc(artifact.get('plate_id') or 'Plate')}</h2>")
        if artifact.get("thumbnail_present"):
            parts.append(
                f"<p><img src=\"/slice-result/{_esc(job_id)}/thumbnail/{index}\" "
                "alt=\"The plate thumbnail the slicer embedded\" "
                "style=\"max-width:320px\"></p>")
        parts.append(
            "<table>"
            f"<tr><th>Thumbnail</th><td>{'shown from the plate file' if artifact.get('thumbnail_present') else 'not present'}</td></tr>"
            f"<tr><th>Layers</th><td>{artifact.get('layer_count') if artifact.get('layer_count') is not None else 'unknown'}</td></tr>"
            f"<tr><th>Estimated time</th><td>{_esc(artifact.get('estimated_time') or 'unknown')}</td></tr>"
            f"<tr><th>Total estimated grams</th><td>{artifact.get('total_estimated_grams') if artifact.get('total_estimated_grams') is not None else 'unknown'}</td></tr>"
            f"<tr><th>Statistics source</th><td>{_esc(artifact.get('statistics_source') or 'unknown')}</td></tr>"
            "</table>")
        rows = []
        for row in artifact.get("filaments") or ():
            grams = row.get("estimated_grams")
            slot = row.get("physical_slot")
            suggestions = "; ".join(
                f"spool #{s.get('spool_id')} ({s.get('confidence')} match)"
                for s in row.get("spool_suggestions") or ()) or "none — unconfirmed"
            rows.append(
                "<tr>"
                f"<td>{_esc(row.get('logical_filament'))}</td>"
                f"<td>{_esc(row.get('material') or 'unknown')}</td>"
                f"<td>{_esc(row.get('color') or 'unknown')}</td>"
                f"<td>{grams if grams is not None else 'unknown'}</td>"
                f"<td>{slot if slot is not None else 'unmapped'}</td>"
                f"<td>{_esc(suggestions)}</td>"
                "</tr>")
        parts.append("<table><tr><th>Filament</th><th>Material</th><th>Color</th>"
                     "<th>Estimated grams</th><th>Slot</th><th>Spool suggestions</th></tr>"
                     + "".join(rows) + "</table>")
        parts.append(f"<p>{_esc(artifact.get('note') or '')}</p>")
    if has_source:
        parts.append(f"<p><a href=\"/u1-feasibility/{_esc(job_id)}\">U1 feasibility "
                     "report for this job's source</a> (read-only; U1 printing is "
                     "unverified).</p>")
    return shell.page("slice-result", "".join(parts), path="/sliced",
                      theme=theme, heading="Slice result", nav=nav)


def render_u1_page(report, job_id, *, theme=shell.DEFAULT_THEME, nav=None) -> str:
    text = u1_feasibility.render_text(report)
    parts = ["<p>Read-only feasibility for this job's source on the Snapmaker "
             "U1's four independent toolheads. No colors were collapsed and no "
             "swap was planned. U1 printing through Print Handy is unverified "
             "on hardware; this report is not a print plan.</p>",
             f"<pre>{_esc(text)}</pre>"]
    return shell.page("u1-feasibility", "".join(parts), path="/sliced",
                      theme=theme, heading="U1 feasibility", nav=nav)
