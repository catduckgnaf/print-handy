"""The Web UI Sliced files page: download what Print Handy already sliced (#1443).

``GET /sliced`` lists the sliced jobs in the archive — newest first — and
``GET /sliced/<job>/<n>`` downloads that job's n-th plate file, so a model
shared as a link (or made with AI) can be sliced for the configured printer
and the finished file taken away to print by other means: a USB stick, an SD
card, the printer's own screen. That is the ForgeBridge-shaped flow — model
in, printer-ready file out — served by the user's own install, behind the
Web UI's existing login.

Serving follows the Layers page's containment rule exactly: a file is
served only when a job's own ``JOB.json`` names it and it resolves inside
that job's archive folder. Unlike Layers, which draws ``.gcode`` only, a
download is format-agnostic: the plate file the job records is the file the
printer takes, plain ``.gcode`` or a ZMod ``.gcode.3mf`` alike. Nothing
here slices, uploads to a printer, or starts anything; slicing happens in
chat or on the on-page workflow (the Sources page's Start with a link form
opens it), and this page only hands back what a slice already produced.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import shell

_esc = shell._esc

# A job id is an archive folder name (the bound the archive readers use).
_JOB_ID = re.compile(r"^[A-Za-z0-9._-]{1,136}$")
# A plate file: a plain name ending in .gcode or .gcode.3mf, nothing else.
_PLATE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,120}\.gcode(\.3mf)?$")

CONTENT_TYPES = {".3mf": "application/octet-stream", ".gcode": "text/plain; charset=utf-8"}


def _safe_job(job_id) -> bool:
    return bool(_JOB_ID.fullmatch(str(job_id or ""))) and job_id not in (".", "..")


def _candidates(payload) -> list:
    """A job's plate files in order: ``gcode``, then each derived plate's."""
    return [payload.get("gcode")] + [row.get("gcode") for row in payload.get("derived_plates") or []
                                     if isinstance(row, dict)]


def _plate_path(job: Path, gcode) -> Path | None:
    """The plate file inside this job's folder, or None when it is not the job's own."""
    if not gcode:
        return None
    path = Path(str(gcode))
    if not path.is_absolute():
        path = job.parent / path
    try:
        path = path.resolve()
        root = job.parent.resolve()
    except OSError:
        return None
    if not _PLATE_NAME.fullmatch(path.name):
        return None
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path


def _load_job(archive: Path, job_id: str) -> tuple[Path, dict] | None:
    """(JOB.json path, payload) for one archived job, or None."""
    if not _safe_job(job_id):
        return None
    root = Path(archive).resolve()
    job = root / job_id / "JOB.json"
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


# The page lists the newest jobs only: the archive grows forever and
# every listed job costs a JOB.json parse and a stat per plate.
SLICED_ROW_LIMIT = 100


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0


def _has_settings_report(payload: dict) -> bool:
    from .settings_report import has_report
    return has_report(payload)


def list_sliced(archive: Path, limit: int = SLICED_ROW_LIMIT) -> list:
    """Downloadable sliced jobs, newest first: one row per job, one entry per plate.

    A job with no plate file still on disk inside its own folder is skipped,
    exactly as an unreadable JOB.json is. At most ``limit`` rows are listed;
    jobs are ordered by their JOB.json's modification time before any is
    parsed, so older jobs past the limit are never read.
    """
    root = Path(archive)
    if not root.is_dir():
        return []
    rows = []
    for job in sorted(root.glob("*/JOB.json"), key=_mtime, reverse=True):
        if len(rows) >= limit:
            break
        job_id = job.parent.name
        loaded = _load_job(root, job_id)
        if loaded is None:
            continue
        job_path, payload = loaded
        plates, seen = [], set()
        for index, candidate in enumerate(_candidates(payload)):
            path = _plate_path(job_path, candidate)
            if path is None or path in seen:
                continue
            seen.add(path)
            try:
                size = path.stat().st_size
            except OSError:
                continue
            plates.append({"index": index, "name": path.name, "size": size})
        if not plates:
            continue
        try:
            mtime = job_path.stat().st_mtime
        except OSError:
            mtime = 0
        has_source = any(name.suffix.lower() == ".3mf" and "editable" not in name.name
                         for name in job_path.parent.glob("*.3mf"))
        rows.append({"job_id": job_id, "title": str(payload.get("title") or job_id),
                     "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)) if mtime else "unknown",
                     "mtime": mtime, "plates": plates, "has_source": has_source,
                     "settings_report": _has_settings_report(payload)})
    rows.sort(key=lambda row: row["mtime"], reverse=True)
    return rows


def resolve_download(archive: Path, job_id: str, index) -> Path | None:
    """The job's ``index``-th plate file, or None when it is not that job's own file."""
    if not re.fullmatch(r"[0-9]{1,4}", str(index or "")):
        return None
    loaded = _load_job(Path(archive), job_id)
    if loaded is None:
        return None
    job_path, payload = loaded
    candidates = _candidates(payload)
    if int(index) >= len(candidates):
        return None
    return _plate_path(job_path, candidates[int(index)])


def content_type(path: Path) -> str:
    """The download's content type: a .gcode.3mf is a package, a .gcode is text."""
    if path.name.endswith(".3mf"):
        return CONTENT_TYPES[".3mf"]
    return CONTENT_TYPES[".gcode"]


def _fmt_size(size) -> str:
    return f"{size / 1_000_000:.1f} MB" if size >= 1_000_000 else f"{max(size, 0) / 1_000:.0f} KB"


def render_sliced_page(rows, *, theme=shell.DEFAULT_THEME, nav=None) -> str:
    """The GET /sliced page: every archived sliced job, with a download per plate."""
    parts = ["<p>Files Print Handy has already sliced, ready to download and "
             "print by other means — a USB stick, an SD card, the printer's own "
             "screen. To slice something new, paste a model link on the "
             "<a href=\"/sources\">Sources</a> page or drop it in "
             "<a href=\"/chat\">chat</a> and choose <strong>Slice and Download</strong>; "
             "the file lands here. Nothing on this page starts a print. "
             "Jobs sliced outside Print Handy land on the "
             "<a href=\"/external-intake\">External intake</a> page.</p>"]
    if not rows:
        parts.append("<p>No sliced files are in the archive yet.</p>")
        return shell.page("sliced", "".join(parts), path="/sliced",
                          theme=theme, heading="Sliced files", nav=nav)
    body = []
    for row in rows:
        report = ('<a href="/settings-report/' + _esc(row['job_id']) + '">Settings report</a>'
                  if row.get("settings_report") else "")
        report += (' <a href="/slice-result/' + _esc(row['job_id']) + '">Slice result</a>')
        if row.get("has_source"):
            report += (' <a href="/u1-feasibility/' + _esc(row['job_id'])
                       + '">U1 feasibility</a>')
        for number, plate in enumerate(row["plates"]):
            body.append(
                "<tr>"
                f"<td>{_esc(row['title'])}</td>"
                f"<td>{_esc(row['when'])}</td>"
                f"<td>{_esc(plate['name'])}</td>"
                f"<td>{_esc(_fmt_size(plate['size']))}</td>"
                f"<td><a href=\"/sliced/{_esc(row['job_id'])}/{int(plate['index'])}\">Download</a></td>"
                f"<td>{report if number == 0 else ''}</td>"
                "</tr>")
    parts.append('<div class="table-scroll"><table><tr><th>Model</th><th>Sliced</th><th>File</th>'
                 "<th>Size</th><th></th><th></th></tr>" + "".join(body) + "</table></div>")
    return shell.page("sliced", "".join(parts), path="/sliced",
                      theme=theme, heading="Sliced files", nav=nav)


__all__ = ["content_type", "list_sliced", "render_sliced_page", "resolve_download"]
