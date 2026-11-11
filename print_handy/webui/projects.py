"""The Web UI Projects page: a read-only readout of multi-part projects (#1204 item 5).

One section per archived job that carries a ``project_v1`` manifest
(:mod:`print_handy.core.projects`). Everything shown is read from what
the print pipeline already writes; this page keeps no store of its own:

- the job's ``JOB.json``: the manifest's source components, the
  embedded ``multipart_plan`` (plates, labels, quantities) and the
  claimed ``project_number``;
- the live plan file ``<records vault>/Plans/<plan_id>.json``: the
  original run's per-plate ``completed_quantity`` (the embedded copy is
  slice-time state and never updates);
- the print records ``<records vault>/Prints/print-*.json``: every
  physical attempt, matched by ``project_id`` and ``project_plate_id``,
  grouped by ``project_run_id`` (``original`` or a reprint run).

The page starts nothing and approves nothing. Resuming a project still
happens in chat (``/print-next``, ``/print-again``), behind the same
per-plate bed-clear confirmation as before. Every interpolated value is
escaped; the page adds no script and no inline style.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..core import print_stats, projects
from . import data as data_module
from . import layers as layers_module
from . import shell

_esc = shell._esc

ORIGINAL_RUN = "original"
# A readout of the newest projects only: an archive can hold years of jobs.
MAX_PROJECTS = 50


def _read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _count(value, default):
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return default


def _number(value):
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _project_id(job):
    return (job.get("project_id")
            or (job.get("multipart_plan") or {}).get("project_id")
            or (job.get("project") or {}).get("project_id"))


def _records_by_project(vault):
    """{project_id: [record, ...]} from the print records, oldest first."""
    records, _skipped = print_stats.load_records(vault)
    grouped = {}
    for record in records:
        pid = record.get("project_id")
        if isinstance(pid, str) and pid:
            grouped.setdefault(pid, []).append(record)
    for rows in grouped.values():
        rows.sort(key=lambda r: (str(r.get("started_at") or ""), _count(r.get("print_number"), 0)))
    return grouped


def _attempt(record):
    status = str(record.get("status") or "unknown")
    if record.get("success") is True:
        outcome = "succeeded"
    elif status == "printing":
        outcome = "printing"
    elif record.get("success") is False:
        outcome = status if status not in ("", "unknown") else "failed"
    else:
        outcome = status
    return {
        "print_number": _number(record.get("print_number")),
        "attempt_id": record.get("print_attempt_id") or record.get("record_id"),
        "run_id": record.get("project_run_id") or ORIGINAL_RUN,
        "plate_ref": record.get("project_plate_ref"),
        "outcome": outcome,
        "succeeded": record.get("success") is True,
        "started_at": record.get("started_at"),
        "completed_at": record.get("completed_at"),
    }


def project_view(job_path, job, plans_dir, records):
    """The readout for one project job, or None when it is not a project.

    ``records`` are this project's print records. Plates come from the
    embedded multipart plan; a project with no split plan (every
    component on one plate) is one plate, A, whose completion comes from
    its successful original-run records.
    """
    project_id = _project_id(job)
    if not project_id:
        return None
    manifest = job.get("project") if isinstance(job.get("project"), dict) else {}
    plan = job.get("multipart_plan") if isinstance(job.get("multipart_plan"), dict) else {}
    live = {}
    if plan.get("plan_id"):
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(plan["plan_id"])).strip(".-")
        if safe:
            live = _read_json(Path(plans_dir) / f"{safe}.json") or {}
    live_parts = {str(p.get("id")): p for p in live.get("parts") or [] if isinstance(p, dict)}
    number = (_number(job.get("project_number")) or _number(live.get("project_number"))
              or _number(plan.get("project_number"))
              or next((_number(r.get("project_number")) for r in records
                       if _number(r.get("project_number"))), None))
    components = []
    for row in manifest.get("components") or []:
        if not isinstance(row, dict):
            continue
        components.append({
            "component_id": str(row.get("component_id") or ""),
            "name": str(row.get("name") or row.get("component_id") or "component"),
            "quantity": _count(row.get("quantity_required"), 1) or 1,
            "required": row.get("required") is not False,
            "role": row.get("role"),
        })
    attempts = [_attempt(r) for r in records]
    plate_of = {}
    plates = []
    parts = [p for p in plan.get("parts") or [] if isinstance(p, dict)]
    if not parts:
        parts = [{"id": "plate1", "label": str(job.get("title") or "plate"),
                  "project_plate_id": "plate1", "project_plate_suffix": "A", "quantity": 1}]
    for pos, part in enumerate(parts):
        plate_id = str(part.get("project_plate_id") or part.get("id") or f"plate{pos + 1}")
        suffix = part.get("project_plate_suffix")
        if not isinstance(suffix, str) or not re.fullmatch(r"[A-Z]+", suffix):
            suffix = projects.plate_suffix(pos)
        live_part = live_parts.get(str(part.get("id")), {})
        quantity = _count(live_part.get("quantity"), _count(part.get("quantity"), 1)) or 1
        indexes = [i for i, r in enumerate(records)
                   if str(r.get("project_plate_id") or "") == plate_id
                   or (not r.get("project_plate_id") and len(parts) == 1)]
        for i in indexes:
            plate_of[i] = plate_id
        mine = [attempts[i] for i in indexes]
        if plan.get("parts"):
            done = _count(live_part.get("completed_quantity"),
                          _count(part.get("completed_quantity"), 0))
        else:
            done = sum(1 for a in mine if a["succeeded"] and a["run_id"] == ORIGINAL_RUN)
        plates.append({
            "plate_id": plate_id, "suffix": suffix,
            "ref": projects.project_ref(number, suffix),
            "label": str(part.get("label") or plate_id),
            "component_keys": [str(k) for k in part.get("component_keys") or []],
            "quantity": quantity,
            "completed_quantity": min(done, quantity),
            "attempts": mine,
        })
    remaining = [p for p in plates if p["completed_quantity"] < p["quantity"]]
    runs = {}
    for i, attempt in enumerate(attempts):
        run = runs.setdefault(attempt["run_id"], {"run_id": attempt["run_id"],
                                                  "attempts": 0, "succeeded_plates": set()})
        run["attempts"] += 1
        if attempt["succeeded"] and i in plate_of:
            run["succeeded_plates"].add(plate_of[i])
    run_rows = []
    for run_id in sorted(runs, key=lambda k: (k != ORIGINAL_RUN, k)):
        run = runs[run_id]
        run_rows.append({"run_id": run_id, "attempts": run["attempts"],
                         "plates_succeeded": len(run["succeeded_plates"]),
                         "plates_total": len(plates)})
    if not remaining:
        state = "complete"
    elif any(a["outcome"] == "printing" and a["run_id"] == ORIGINAL_RUN for a in attempts):
        state = "printing"
    else:
        state = "resumable"
    return {
        "project_id": project_id,
        "project_number": number,
        "title": str(manifest.get("title") or plan.get("title") or job.get("title")
                     or Path(job_path).parent.name),
        "source_kind": manifest.get("source_kind") or plan.get("source_kind")
        or job.get("project_source_kind"),
        "plan_id": plan.get("plan_id"),
        "job": Path(job_path).parent.name,
        "components": components,
        "plates": plates,
        "runs": run_rows,
        "state": state,
        "remaining": [p["ref"] or p["suffix"] for p in remaining],
    }


def project_views(archive, vault):
    """Every archived project job's readout, newest job first."""
    archive = Path(archive)
    vault = Path(vault)
    try:
        jobs = sorted(archive.glob("*/JOB.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        jobs = []
    by_project = None
    views = []
    for job_path in jobs:
        job = _read_json(job_path)
        if job is None or not _project_id(job):
            continue
        if by_project is None:
            by_project = _records_by_project(vault)
        view = project_view(job_path, job, vault / "Plans", by_project.get(_project_id(job), []))
        if view is not None:
            views.append(view)
        if len(views) >= MAX_PROJECTS:
            break
    return views


_STATE_TEXT = {
    "complete": "Complete: every plate is printed.",
    "printing": "A plate of this project is printing now.",
    "resumable": "Resumable",
}


def _state_line(view):
    if view["state"] != "resumable":
        return _STATE_TEXT[view["state"]]
    left = ", ".join(str(r) for r in view["remaining"])
    how = (f"/print-next {view['plan_id']}" if view.get("plan_id") else "/print-again")
    return (f"Resumable: {len(view['remaining'])} of {len(view['plates'])} plates left ({left}). "
            f"Continue in chat with {how}; each plate asks for its own bed-clear confirmation.")


def _attempt_text(attempt):
    bits = []
    if attempt["print_number"]:
        bits.append(f"#{attempt['print_number']}")
    bits.append(attempt["outcome"])
    if attempt["run_id"] != ORIGINAL_RUN:
        bits.append(f"reprint run {attempt['run_id']}")
    if attempt["started_at"]:
        bits.append(f"started {attempt['started_at']}")
    return " · ".join(bits)


def _render_project(view):
    number = view["project_number"]
    head = f"Project {number}: {view['title']}" if number else f"Project: {view['title']}"
    parts = [f'<section class="card"><h2>{_esc(head)}</h2>',
             f'<p class="muted mono">{_esc(view["project_id"])}'
             f'{" · " + _esc(view["source_kind"]) if view["source_kind"] else ""}</p>',
             f"<p>{_esc(_state_line(view))}</p>"]
    if view["components"]:
        rows = "".join(
            "<tr>"
            f"<td>{_esc(c['name'])}</td>"
            f"<td>{_esc(c['quantity'])}</td>"
            f"<td>{'required' if c['required'] else 'optional'}</td>"
            f"<td>{_esc(c['role'] or '')}</td>"
            "</tr>" for c in view["components"])
        parts.append("<h3>Components</h3><div class=\"table-scroll\"><table>"
                     "<tr><th>Component</th><th>Quantity</th><th>Required</th><th>Role</th></tr>"
                     f"{rows}</table></div>")
    plate_rows = []
    for plate in view["plates"]:
        attempts = ("<br>".join(_esc(_attempt_text(a)) for a in plate["attempts"])
                    or "not printed yet")
        plate_rows.append(
            "<tr>"
            f"<td class=\"mono\">{_esc(plate['ref'] or plate['suffix'])}</td>"
            f"<td>{_esc(plate['label'])}</td>"
            f"<td>{_esc(plate['completed_quantity'])} of {_esc(plate['quantity'])}</td>"
            f"<td>{attempts}</td>"
            "</tr>")
    parts.append("<h3>Plates</h3><div class=\"table-scroll\"><table>"
                 "<tr><th>Plate</th><th>Parts</th><th>Printed</th><th>Attempts</th></tr>"
                 f"{''.join(plate_rows)}</table></div>")
    if view["runs"]:
        run_rows = "".join(
            "<tr>"
            f"<td class=\"mono\">{_esc(r['run_id'])}</td>"
            f"<td>{_esc(r['attempts'])}</td>"
            f"<td>{_esc(r['plates_succeeded'])} of {_esc(r['plates_total'])}</td>"
            "</tr>" for r in view["runs"])
        parts.append("<h3>Runs</h3><p class=\"note\">The original run counts toward the "
                     "project; a reprint run is counted on its own and never changes the "
                     "original's totals.</p><div class=\"table-scroll\"><table>"
                     "<tr><th>Run</th><th>Attempts</th><th>Plates succeeded</th></tr>"
                     f"{run_rows}</table></div>")
    parts.append("</section>")
    return "".join(parts)


def render_projects_page(views, *, theme=shell.DEFAULT_THEME, nav=None):
    """The GET /projects page."""
    intro = ("<p>Multi-part projects from uploads and model links, read from each job's "
             "project manifest and its print records. This page is read only: continue "
             "or reprint a project in chat.</p>")
    if not views:
        body = intro + ("<p>No projects yet. Send several STL or 3MF files in one chat "
                        "message, or a model link with several files, to start one.</p>")
    else:
        body = intro + "".join(_render_project(v) for v in views)
    return shell.page("projects", body, path="/projects", theme=theme,
                      heading="Projects", nav=nav)


def _jsonable(views):
    return [dict(v, plates=[dict(p) for p in v["plates"]]) for v in views]


def respond(data, path, *, theme=shell.DEFAULT_THEME, nav=None):
    """(code, body, content type) for GET /projects and GET /api/projects,
    in the order the server's ``_send`` takes them."""
    views = project_views(layers_module.archive_root(data), data_module._vault(data, None))
    if path == "/api/projects":
        return 200, json.dumps({"ok": True, "projects": _jsonable(views)},
                               indent=1, sort_keys=True, default=str), "application/json"
    return 200, render_projects_page(views, theme=theme, nav=nav), "text/html; charset=utf-8"


__all__ = ["project_view", "project_views", "render_projects_page", "respond"]
