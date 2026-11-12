"""The read-only designs catalog (#388, stage 3).

``GET /designs`` lists the design sessions on disk — newest first — and
``GET /designs/<id>`` shows one session with its final ``.scad`` source
and downloads for the ``.scad``, the final ``.stl`` and the preview PNG.
A mesh edit session also offers its base mesh (``source.stl``): the
composed ``.scad`` imports that file through ``base()``, so the source
alone cannot reproduce the design without it.
Everything comes from :mod:`print_handy.core.design`, which owns the
session store (``<cache>/design-sessions/``): this module only reads
through its catalog API, so the retention window (14 days) and the
on-disk layout have exactly one owner.

The catalog is a catalogue of what exists on disk, so it renders the
same whether or not ``[design]`` is currently enabled — disabling design
stops new sessions, it does not hide the ones already made. There are
no write actions here at all: no delete, no slice, no print. Serving is
by session id and file kind only, and
:func:`print_handy.core.design.session_file` resolves the name from
the session record (a bare filename inside the session's own folder),
so a path outside a session directory cannot be reached by
construction.
"""
from __future__ import annotations

import html
import time
from pathlib import Path

from ..core import design
from . import shell

# The file kinds a session exposes, with their download presentation:
# kind -> (content type, inline?) — the preview is inline so the
# catalog can show it as an image; the source and the STL download.
FILE_KINDS = {
    "scad": ("text/plain; charset=utf-8", False),
    "stl": ("model/stl", False),
    "preview": ("image/png", True),
    "base": ("model/stl", False),
}


def designs_root(data) -> Path:
    """The design session store's root for these settings.

    Resolves the cache directory exactly as the design engine's callers
    do: ``paths.cache``, falling back to ``<state_home>/cache``.
    """
    paths = (data or {}).get("paths") or {}
    cache = paths.get("cache") or (Path(paths.get("state_home") or ".") / "cache")
    return design.sessions_root(cache)


def session_name(session) -> str:
    """The display name: the session's first instruction, trimmed.

    The record's ``description`` is the text the session was started
    with (a create description, or the change requested of an edit).
    With none recorded, fall back to the source file's name, then to a
    plain placeholder — there is no separate naming store to consult.
    """
    text = " ".join(str(session.get("description") or "").split())
    if text:
        return text if len(text) <= 80 else text[:77] + "…"
    source = session.get("source") or {}
    if source.get("name"):
        return f"Edit of {source['name']}"
    return "Untitled design"


def session_state(session) -> str:
    """The catalog state label for a session record's own status.

    ``cancelled`` and ``handed_off`` are the record's finished states.
    Of the live ones, a session with a finished render is *ready* (it
    can be sliced, changed or cancelled from chat); one still waiting
    for its next message or its first render is *in progress*.
    """
    status = session.get("status")
    if status == "cancelled":
        return "Cancelled"
    if status == "handed_off":
        return "Handed off"
    if status == "awaiting_revision":
        return "In progress"
    if session.get("stl"):
        return "Ready"
    return "In progress"


def session_kind(session) -> str:
    """What the session is: a create, or an edit with its source named.

    Edit rows reuse the engine's own :func:`design.source_note`, the
    same one-liner the chat reply carries, so the catalog and Discord
    describe a session's source (and a mesh edit's limits) identically.
    """
    if session.get("kind") == "edit":
        return design.source_note(session) or "Edited model"
    return "Created from a description"


def _when(epoch) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(epoch)))
    except (TypeError, ValueError, OSError):
        return "unknown"


def catalog_rows(data):
    """The catalog as render-ready row dicts, newest first."""
    root = designs_root(data)
    rows = []
    for session in design.list_sessions(root):
        rows.append({
            "id": session["id"],
            "name": session_name(session),
            "kind": session_kind(session),
            "state": session_state(session),
            "turns": int(session.get("turns") or 0),
            "created": _when(session.get("created_at")),
            "updated": _when(session.get("updated_at")),
            "has_preview": design.session_file(root, session["id"], "preview") is not None,
            "has_stl": design.session_file(root, session["id"], "stl") is not None,
            "has_scad": design.session_file(root, session["id"], "scad") is not None,
            "has_base": design.session_file(root, session["id"], "base") is not None,
        })
    return rows


def load_detail(data, session_id):
    """(session, scad_text, has_stl, has_preview, has_base) for the detail page.

    Returns None when the id names no session. The ``.scad`` shown is
    the session's current source of truth — the record's ``scad``
    file — read through the same catalog resolution as the downloads,
    and returned as text for escaped rendering.
    """
    root = designs_root(data)
    try:
        session = design.load_session(root, session_id)
    except design.DesignError:  # a malformed id names no session
        return None
    if session is None:
        return None
    scad_path = design.session_file(root, session_id, "scad")
    text = scad_path.read_text(encoding="utf-8", errors="replace") if scad_path else ""
    return (session, text,
            design.session_file(root, session_id, "stl") is not None,
            design.session_file(root, session_id, "preview") is not None,
            design.session_file(root, session_id, "base") is not None)


def file_for(data, session_id, kind):
    """(path, content_type, inline) for one catalog download, or None."""
    if kind not in FILE_KINDS:
        return None
    path = design.session_file(designs_root(data), session_id, kind)
    if path is None:
        return None
    content_type, inline = FILE_KINDS[kind]
    return path, content_type, inline


# ---------------------------------------------------------------- rendering

def _esc(value):
    return html.escape(str(value), quote=True)


def _page(title, body, *, theme=shell.DEFAULT_THEME):
    """Through the one shell (#813); the header chrome carries the nav."""
    return shell.page(title, body, path="/designs", theme=theme)


def render_designs_page(rows, *, theme=shell.DEFAULT_THEME):
    """The GET /designs list: one row per session, newest first."""
    parts = ["<p>Looking for a model to print? <a href=\"/sources\">Model sources</a> "
             "lists every site a link can be dropped from.</p>"
             "<p>Designs made from chat with <code>/print-design</code> (or <code>/design</code>) "
             "(<a href=\"https://github.com/catduckgnaf/print-handy/blob/main/docs/DESIGN.md\">"
             "the design guide</a>). This catalog is read-only: it shows the "
             "sessions kept on this machine (14 days) and serves their files; "
             "slicing and printing still happen from chat, through the normal "
             "confirmation flow.</p>"]
    if not rows:
        parts.append("<p>No designs yet. Designs are made from chat: "
                     "describe a part with <code>/print-design</code>, or drop a "
                     "model and ask to design or change it — finished sessions "
                     "will be listed here.</p>")
        return _page("designs", "".join(parts), theme=theme)
    body = []
    for row in rows:
        preview = (f'<img src="/designs/{_esc(row["id"])}/preview" '
                   f'alt="Preview of {_esc(row["name"])}" width="160">'
                   if row["has_preview"] else "(no preview)")
        body.append(
            "<tr>"
            f'<td><a href="/designs/{_esc(row["id"])}">{_esc(row["name"])}</a></td>'
            f"<td>{_esc(row['kind'])}</td>"
            f"<td>{_esc(row['state'])}</td>"
            f"<td>{row['turns']}</td>"
            f"<td>{_esc(row['created'])}</td>"
            f"<td>{_esc(row['updated'])}</td>"
            f"<td>{preview}</td>"
            "</tr>")
    parts.append("<table><tr><th>Design</th><th>Kind</th><th>State</th>"
                 "<th>Turns</th><th>Created</th><th>Updated</th><th>Preview</th></tr>"
                 + "".join(body) + "</table>")
    return _page("designs", "".join(parts), theme=theme)


def render_design_detail(session, scad_text, *, has_stl, has_preview, has_base=False,
                         theme=shell.DEFAULT_THEME):
    """The GET /designs/<id> page: facts, downloads, the .scad source."""
    sid = _esc(session.get("id") or "")
    parts = [f"<h2>{_esc(session_name(session))}</h2>",
             "<table>"
             f"<tr><th>Kind</th><td>{_esc(session_kind(session))}</td></tr>"
             f"<tr><th>State</th><td>{_esc(session_state(session))}</td></tr>"
             f"<tr><th>Turns</th><td>{int(session.get('turns') or 0)}</td></tr>"
             f"<tr><th>Created</th><td>{_esc(_when(session.get('created_at')))}</td></tr>"
             f"<tr><th>Updated</th><td>{_esc(_when(session.get('updated_at')))}</td></tr>"
             "</table>"]
    downloads = [f'<li><a href="/designs/{sid}/scad">Download the .scad source</a></li>']
    if has_base:
        downloads.append(
            f'<li><a href="/designs/{sid}/base">Download the base mesh (source.stl)</a> — '
            "the .scad source imports this file, so keep it next to the source "
            "to open the design in OpenSCAD</li>")
    if has_stl:
        downloads.append(f'<li><a href="/designs/{sid}/stl">Download the final .stl</a></li>')
    if has_preview:
        downloads.append(f'<li><a href="/designs/{sid}/preview">Download the preview PNG</a></li>')
    parts.append("<h3>Files</h3><ul>" + "".join(downloads) + "</ul>")
    if has_preview:
        parts.append(f'<p><img src="/designs/{sid}/preview" '
                     f'alt="Preview of {_esc(session_name(session))}" width="320"></p>')
    parts.append("<h3>OpenSCAD source</h3>")
    if scad_text:
        parts.append(f"<pre>{_esc(scad_text)}</pre>")
    else:
        parts.append("<p>No source recorded for this session yet.</p>")
    return _page("design", "".join(parts), theme=theme)


__all__ = [
    "FILE_KINDS", "catalog_rows", "designs_root", "file_for", "load_detail",
    "render_design_detail", "render_designs_page", "session_kind",
    "session_name", "session_state",
]
