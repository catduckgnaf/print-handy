"""The designs page: the design workshop, over the session catalog (#1449).

``GET /designs`` leads with the workshop and lists the design sessions
on disk below it — newest first — and ``GET /designs/<id>`` shows one
session with its final ``.scad`` source and downloads for the ``.scad``,
the final ``.stl`` and the preview PNG. A mesh edit session also offers
its base mesh (``source.stl``): the composed ``.scad`` imports that file
through ``base()``, so the source alone cannot reproduce the design
without it.
Everything catalogued comes from :mod:`print_handy.core.design`, which
owns the session store (``<cache>/design-sessions/``): this module only
reads through its catalog API, so the retention window (14 days) and
the on-disk layout have exactly one owner.

The workshop runs no design pipeline of its own. Its forms submit to
the operator's web chat session (``POST /designs`` and
``POST /designs/choice`` in the server), which runs the message through
``BotCore.process``: the same ``/print-design`` dispatch, the same
engine, the same Slice it / Change it / Cancel question and the same
hand-off into prepared intake as every chat platform. The page renders
the live session for this browser's chat scope (its preview, its state,
the open question's own buttons) from the session store and the chat
service's view of that scope. When design is not set up (disabled, or
no model configured) the workshop says why, in the chat handler's own
terms, and shows no form that cannot work; the catalog below renders
the same either way, because disabling design stops new sessions, it
does not hide the ones already made.

Catalog serving is by session id and file kind only, and
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
from . import chat as chat_module
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


# ---------------------------------------------------------------- workshop

def design_availability(data):
    """(ready, reason) for designing from this page (#1449).

    The same three postures the chat design handler answers with,
    resolved the same way: ``design.enabled`` false is off; an endpoint
    that resolves through :func:`design.resolve_http_config` ([design]
    inheriting [llm], the resolver the design transport itself uses)
    is ready; explicit true with no endpoint, and auto with no
    endpoint, are the two not-configured reasons.
    """
    raw = ((data or {}).get("design") or {}).get("enabled")
    if raw is False:
        return False, ("Design is turned off: design.enabled is false. Set "
                       "design.enabled = true (PRINT_HANDY__DESIGN__ENABLED=true), "
                       "or remove it so design turns on when a model is configured. "
                       "See the design guide, linked below.")
    if design.resolve_http_config(data) is not None:
        return True, ""
    if raw is True:
        return False, ("Design is enabled, but no design model is configured: set "
                       "[design] chat_url and model (or the [llm] values they "
                       "inherit), then reload this page.")
    return False, ("Design is not set up yet: configure a design model by setting "
                   "[design] chat_url and model (or the [llm] values they "
                   "inherit), then reload this page.")


def owner_for_scope(scope):
    """The design owner key for one Web UI session's chat scope.

    The chat design handler keys a session by its source: chat id,
    thread, user. A web chat source is the operator in the web chat
    with the scope as its thread, so the key is built from the web
    adapter's own constants, never a second spelling of them.
    """
    from ..bot import web
    return f"{web.CHAT_ID}:{scope or ''}:{web.OPERATOR_ID}"


def start_message(description):
    """The web chat message that starts a design: the /print-design command.

    The command form, not a bare description, so the dispatch always
    treats it as a fresh design (superseding any session this operator
    had waiting), exactly as typing the command in chat does.
    """
    return f"/print-design {description}"


def workshop_model(data, view):
    """Everything the workshop section renders, for one chat view.

    ``view`` is the browser session's scoped web chat view, or None
    when web chat is off (the server builds no service then, so there
    is nothing to submit through). The live session is this scope's
    own, found by its design owner key; another session's design is
    never shown. ``messages`` is the tail of the bot's replies in this
    scope, so a finished turn's dimensions and assumptions are on the
    page next to the preview.
    """
    ready, reason = design_availability(data)
    model = {"ready": ready, "reason": reason, "chat_off": view is None,
             "session": None, "prompt": None, "busy": False, "messages": [],
             "seq": 0}
    if view is None:
        return model
    entries = view.entries()
    if entries:
        model["seq"] = entries[-1]["seq"]
    model["busy"] = bool(view.busy())
    model["prompt"] = view.open_prompt()
    model["messages"] = [str(entry.get("content") or "") for entry in entries
                         if entry.get("kind") == "message"][-6:]
    root = designs_root(data)
    session = design.find_session_for_owner(root, owner_for_scope(view.scope))
    if session is not None:
        model["session"] = {
            "id": session["id"],
            "name": session_name(session),
            "state": session_state(session),
            "turns": int(session.get("turns") or 0),
            "updated": _when(session.get("updated_at")),
            "updated_epoch": int(session.get("updated_at") or 0),
            "has_preview": design.session_file(root, session["id"], "preview") is not None,
        }
    return model


# ---------------------------------------------------------------- rendering

def _esc(value):
    return html.escape(str(value), quote=True)


def _page(title, body, *, theme=shell.DEFAULT_THEME, nav=None):
    """Through the one shell (#813); the header chrome carries the nav."""
    return shell.page(title, body, path="/designs", theme=theme, nav=nav)


def _render_workshop(workshop, *, form_token):
    """The workshop section at the top of GET /designs (#1449).

    Three shapes, matching what can actually happen: web chat off or
    design not set up is a plain reason and no form; ready with no live
    session is the start form; a live session adds its panel (preview,
    the bot's latest replies, and either the open question's buttons,
    a working note while a turn runs, or the follow-up form). Every
    form posts back to this page's own routes, which submit through
    the scoped web chat view: the buttons are the question's own,
    answered through the prompt machinery, never a page-side shortcut.
    """
    parts = ["<h2>Design something new</h2>"]
    if workshop is None or workshop["chat_off"]:
        parts.append("<p>Designing from this page runs through web chat, and web "
                     "chat is turned off (<code>webui.chat_enabled</code>). Turn it "
                     "on in Settings to design here, or design from Discord, "
                     "Telegram or Matrix with <code>/print-design</code>.</p>")
        return "".join(parts)
    if not workshop["ready"]:
        parts.append(f"<p>{_esc(workshop['reason'])}</p>")
        return "".join(parts)
    session = workshop["session"]
    parts.append(
        '<form method="post" action="/designs">'
        f'<input type="hidden" name="_csrf" value="{_esc(form_token)}">'
        '<input type="hidden" name="mode" value="start">'
        '<div class="field"><label for="d-text">Describe the part</label>'
        '<input id="d-text" name="text" autocomplete="off" '
        'placeholder="a 40x20x3 plate with two 4 mm holes 30 mm apart"></div>'
        '<button type="submit" class="accent">Start designing</button></form>')
    if session is not None:
        parts.append("<p>Starting a new design replaces the one below that is "
                     "still waiting for your call.</p>")
        parts.append("<h3>Your current design</h3>")
        parts.append(
            "<table>"
            f"<tr><th>Design</th><td>{_esc(session['name'])}</td></tr>"
            f"<tr><th>State</th><td>{_esc(session['state'])}</td></tr>"
            f"<tr><th>Turns</th><td>{session['turns']}</td></tr>"
            f"<tr><th>Updated</th><td>{_esc(session['updated'])}</td></tr>"
            "</table>")
        if session["has_preview"]:
            parts.append(
                f'<p><img src="/designs/{_esc(session["id"])}/preview'
                f'?v={session["updated_epoch"]}" '
                f'alt="Preview of {_esc(session["name"])}" width="320"></p>')
        for message in workshop["messages"]:
            parts.append(f'<p class="chat-bot">{_esc(message)}</p>')
        prompt = workshop["prompt"]
        if prompt:
            buttons = []
            for index, choice in enumerate(prompt["choices"]):
                buttons.append(
                    '<form method="post" action="/designs/choice" class="inline">'
                    f'<input type="hidden" name="_csrf" value="{_esc(form_token)}">'
                    f'<input type="hidden" name="clarify_id" '
                    f'value="{_esc(prompt["clarify_id"])}">'
                    f'<input type="hidden" name="index" value="{index}">'
                    f'<button type="submit">{_esc(choice)}</button></form>')
            parts.append('<div class="chat-prompt"><p>'
                         f'{_esc(prompt["question"])}</p>'
                         + "".join(buttons) + "</div>")
        elif workshop["busy"]:
            # A turn is running and no question is waiting: poll like the
            # chat page until the reply lands, then reload into it.
            parts.append(f'<noscript><meta http-equiv="refresh" '
                         f'content="{chat_module.REFRESH_SECONDS}"></noscript>')
            parts.append(chat_module.POLL_SCRIPT.replace(
                "__SEQ__", str(int(workshop["seq"]))))
            parts.append(f'<p class="chat-bot chat-working"><em>'
                         f'{chat_module.WORKING_TEXT}</em></p>')
        else:
            parts.append(
                '<form method="post" action="/designs">'
                f'<input type="hidden" name="_csrf" value="{_esc(form_token)}">'
                '<input type="hidden" name="mode" value="revise">'
                '<div class="field"><label for="d-change">Describe the change</label>'
                '<input id="d-change" name="text" autocomplete="off" '
                'placeholder="make it 5 mm taller"></div>'
                '<button type="submit" class="accent">Send the change</button></form>')
    return "".join(parts)


def render_designs_page(rows, *, theme=shell.DEFAULT_THEME, nav=None, workshop=None,
                        form_token="", notice=None, error=None):
    """The GET /designs page: the workshop, then the catalog of sessions."""
    parts = []
    if notice:
        parts.append(f'<p class="notice">{_esc(notice)}</p>')
    if error:
        parts.append(f'<p class="error">{_esc(error)}</p>')
    parts.append(_render_workshop(workshop, form_token=form_token))
    parts.append("<h2>Designs on this machine</h2>"
                 "<p>Every design session kept here (14 days), from this page or "
                 "from chat with <code>/print-design</code> (or <code>/design</code>) "
                 "(<a href=\"https://github.com/catduckgnaf/print-handy/blob/main/docs/DESIGN.md\">"
                 "the design guide</a>). Opening one shows its source and files; "
                 "slicing and printing run through the normal confirmation flow.</p>"
                 "<p>Looking for a model to print instead? <a href=\"/sources\">Model "
                 "sources</a> lists every site a link can be dropped from.</p>")
    if not rows:
        parts.append("<p>No designs yet. Start one in the workshop above, or "
                     "describe a part with <code>/print-design</code> in chat: "
                     "finished sessions will be listed here.</p>")
        return _page("designs", "".join(parts), theme=theme, nav=nav)
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
    return _page("designs", "".join(parts), theme=theme, nav=nav)


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
    "FILE_KINDS", "catalog_rows", "design_availability", "designs_root",
    "file_for", "load_detail", "owner_for_scope", "render_design_detail",
    "render_designs_page", "session_kind", "session_name", "session_state",
    "start_message", "workshop_model",
]
