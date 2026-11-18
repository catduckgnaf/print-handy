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

The workshop is a conversation (#1636), drawn by the same transcript
renderer as the Chat page: your messages and the bot's replies in
order, each turn's preview in the thread at the turn that made it, the
open question inline with its own buttons, a working line while a turn
runs, and one message box. It runs no design pipeline of its own: the
box and the buttons submit to a web chat scope of the page's own (the
browser's scope plus a design-thread suffix, ``bot.web.design_scope``),
which runs every message through ``BotCore.process``: the same
``/print-design`` dispatch, engine, Slice it / Change it / Cancel
question and hand-off into prepared intake as every chat platform. The
first message of a thread is sent as the ``/print-design`` command;
every later one goes as typed, so a change, a typed "slice it" or a
choice number reach the design exactly as they do in chat. The thread
reads its own entries only, never the shared print feed, so Chat and
Workflow traffic and print posts stay out of it.

The conversation is saved beside its session (``core.design``'s thread
file, written by the web chat service), so it survives a reload and a
restart and reopens from the catalog. "New design" starts a new thread
(a new suffix, kept in a cookie); the session it leaves stays as it was
and can be continued from its own page. A session started on another
platform, or in another browser, can be read here but continues where
it was started: its owner key names that chat (``owner_for_scope``).

When design is not set up (disabled, or no model configured) or web
chat is off, the workshop says why, in the chat handler's own terms,
and shows no message box that cannot work; the catalog below renders
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
from . import shell, transcript

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
    return design.sessions_root_for(data)


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


def turn_preview_for(data, session_id, turn):
    """(path, content_type, inline) for one turn's preview, or None.

    The picture a turn posted in the thread (``?turn=N``), resolved by
    :func:`print_handy.core.design.turn_preview` from the session record,
    so only that session's own preview files can be named."""
    path = design.turn_preview(designs_root(data), session_id, turn)
    if path is None:
        return None
    content_type, inline = FILE_KINDS["preview"]
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
    """The design owner key for one web chat scope.

    The chat design handler keys a session by its source: chat id,
    thread, user. A web chat source is the operator in the web chat
    with the scope as its thread, so the key is built from the web
    adapter's own constants, never a second spelling of them.
    """
    from ..bot import web
    return web.DesignThreads.owner_for(scope)


def start_message(description):
    """The web chat message that starts a design: the /print-design command.

    The command form, not a bare description, so the dispatch always
    treats it as a fresh design (superseding any session this thread
    had waiting), exactly as typing the command in chat does.
    """
    return f"/print-design {description}"


def thread_of(session):
    """(browser scope, thread nonce) of a web design thread's session, else None.

    Sessions started in another chat (Discord, Telegram, Matrix, the
    Chat page) or by another browser name a different owner; they are
    shown here, and continue where they were started.
    """
    from ..bot import web
    parts = str((session or {}).get("owner_key") or "").split(":")
    if len(parts) != 3 or parts[0] != web.CHAT_ID or parts[2] != web.OPERATOR_ID:
        return None
    return web.design_thread_of(parts[1])


def _thread_entry(entry, session_id):
    """One saved thread row as a render entry (an image's url rebuilt here)."""
    from ..bot import web
    if entry.get("kind") != "image":
        return dict(entry)
    try:
        turn = int(entry.get("turn"))
    except (TypeError, ValueError):
        return None
    return {"kind": "image", "caption": entry.get("caption") or f"Turn {turn} preview",
            "url": web.preview_url(session_id, turn)}


def session_thread(data, session):
    """The saved conversation of a session, as render entries, oldest first.

    A session saved before conversations were kept (or started on
    another platform) has only its record's history: each turn's
    instruction and the preview it made. That is what is shown then,
    with a note saying the replies were not kept.
    """
    from ..bot import web
    root = designs_root(data)
    sid = session["id"]
    saved = [row for row in (_thread_entry(e, sid) for e in design.load_thread(root, sid))
             if row is not None]
    if saved:
        return saved
    rows = []
    history = [h for h in session.get("history") or [] if isinstance(h, dict)]
    if history or session.get("description"):
        rows.append({"kind": "prompt_closed",
                     "note": "Only your messages and the previews were kept for this "
                             "design, so the bot's replies are not shown."})
    if not history and session.get("description"):
        rows.append({"kind": "operator", "content": str(session["description"])})
    for index, turn in enumerate(history):
        text = turn.get("instruction") or (session.get("description") if index == 0 else "")
        if text:
            rows.append({"kind": "operator", "content": str(text)})
        number = turn.get("turn")
        if isinstance(number, int) and design.turn_preview(root, sid, number) is not None:
            rows.append({"kind": "image", "url": web.preview_url(sid, number),
                         "caption": f"Turn {number} preview"})
    return rows


def _live_rows(live, session_id):
    """The live entries the saved thread does not hold yet.

    The web chat service marks an entry ``saved`` (with its session id)
    once it is in a thread file. What is left is newer than everything
    saved: the operator's message while its first reply is on the way,
    or a reply that belongs to no session (a "not set up" answer).
    Entries saved to another session of this thread are that session's.
    """
    if session_id is None:
        return [e for e in live if not e.get("saved")]
    last = max((e["seq"] for e in live if e.get("saved")), default=0)
    return [e for e in live if not e.get("saved") and e["seq"] > last]


def workshop_model(data, view):
    """Everything the workshop renders, for one design-thread view.

    ``view`` is the browser's design-thread view (its own entries only,
    no shared posts), or None when web chat is off (the server builds
    no service then, so there is nothing to submit through). The
    thread's session is the newest one its owner key started; another
    thread's or another browser's design is never shown here.

    ``phase`` is what the message box sends: ``start`` (the text becomes
    the /print-design command: nothing is running, the design finished,
    or its first turn did not work) or ``reply`` (the text goes as typed:
    the answer to the open question, the change a waiting design is
    after, or a message while a turn runs, which the design answers
    itself). ``lost`` is a rendered design whose question a restart
    closed: the next message is its change.
    """
    ready, reason = design_availability(data)
    model = {"ready": ready, "reason": reason, "chat_off": view is None,
             "session": None, "prompt": None, "busy": False, "pending": False,
             "entries": [], "seq": 0, "phase": "start", "failed": False,
             "lost": False}
    if view is None:
        return model
    live = view.entries()
    if live:
        model["seq"] = live[-1]["seq"]
    busy = bool(view.busy())
    prompt = view.open_prompt()
    model.update(busy=busy, prompt=prompt, pending=busy and not prompt)
    session = design.newest_session_for_owner(designs_root(data), owner_for_scope(view.scope))
    if session is not None:
        status = session.get("status")
        turns = int(session.get("turns") or 0)
        live_turn = status == "active" and not busy and not prompt
        failed = live_turn and turns == 0
        lost = live_turn and turns > 0
        model.update(failed=failed, lost=lost)
        if prompt or busy or status == "awaiting_revision" or lost:
            model["phase"] = "reply"
        model["session"] = {
            "id": session["id"],
            "name": session_name(session),
            "description": str(session.get("description") or ""),
            "state": "Did not work" if failed else session_state(session),
            "updated": _when(session.get("updated_at")),
        }
        model["entries"] = session_thread(data, session) + _live_rows(live, session["id"])
    else:
        if prompt or busy:
            model["phase"] = "reply"
        model["entries"] = _live_rows(live, None)
    return model


def composer_message(model, text):
    """The web chat message the message box sends for ``text`` (see workshop_model)."""
    return start_message(text) if model["phase"] == "start" else text


def retry_message(model):
    """The message that tries a failed first turn again, or None."""
    session = model.get("session")
    if not model.get("failed") or not session or not session["description"].strip():
        return None
    return start_message(session["description"])


def resume_lost(data, model):
    """Put a design whose question a restart closed back to waiting (core.design)."""
    if model.get("lost") and model.get("session"):
        design.mark_waiting(designs_root(data), model["session"]["id"])


def continue_state(session, browser_scope):
    """How the detail page offers to go on with a session.

    ``current``: it is this browser's open design thread. ``continue``:
    another of this browser's threads, still live. ``finished``: it was
    handed off or cancelled. ``elsewhere``: another chat or browser
    started it, so it continues there (its owner key names that chat).
    """
    if session.get("status") in ("cancelled", "handed_off"):
        return "finished"
    thread = thread_of(session)
    if thread is None or not browser_scope or thread[0] != browser_scope:
        return "elsewhere"
    return "continue"


# ---------------------------------------------------------------- rendering

def _esc(value):
    return html.escape(str(value), quote=True)


def _page(title, body, *, theme=shell.DEFAULT_THEME, nav=None):
    """Through the one shell (#813); the header chrome carries the nav."""
    return shell.page(title, body, path="/designs", theme=theme, nav=nav)


INTRO = ("<p>Describe a part and Print Handy designs it, checks it, and shows you a "
         "preview right here. Then just keep talking: say a change, answer its "
         "questions, or say <em>slice it</em> to hand it to the normal print intake. "
         "Nothing prints without the usual confirmation.</p>")


def _hidden_token(form_token):
    return f'<input type="hidden" name="_csrf" value="{_esc(form_token)}">'


def _render_workshop(workshop, *, form_token):
    """The design conversation at the top of GET /designs (#1636).

    Web chat off or design not set up is a plain reason and no message
    box. Otherwise: a small header (the design's name, state and update
    time, and New design), the thread, the open question with its own
    buttons, a Try again for a first turn that did not work, and one
    message box. Every form posts back to this page's own routes, which
    submit through the design-thread view: the buttons are the
    question's own, answered through the prompt machinery.
    """
    parts = ['<section class="design-workshop" aria-label="Design conversation">']
    if workshop is None or workshop["chat_off"]:
        parts.append("<h2>Design something new</h2>"
                     "<p>Designing from this page runs through web chat, and web "
                     "chat is turned off (<code>webui.chat_enabled</code>). Turn it "
                     "on in Settings to design here, or design from Discord, "
                     "Telegram or Matrix with <code>/print-design</code>.</p></section>")
        return "".join(parts)
    if not workshop["ready"]:
        parts.append(f"<h2>Design something new</h2><p>{_esc(workshop['reason'])}</p>"
                     "</section>")
        return "".join(parts)
    session = workshop["session"]
    entries = workshop["entries"]
    new_form = ('<form method="post" action="/designs/new" class="inline">'
                f'{_hidden_token(form_token)}'
                '<button type="submit" class="secondary">New design</button></form>')
    if session is not None:
        parts.append(f"<h2>{_esc(session['name'])}</h2>"
                     '<div class="design-head">'
                     f'<p class="muted">{_esc(session["state"])} · updated '
                     f'{_esc(session["updated"])} · '
                     f'<a href="/designs/{_esc(session["id"])}">files and source</a></p>'
                     f"{new_form}</div>")
    else:
        parts.append("<h2>Design something new</h2>")
        if entries:
            parts.append(f'<div class="design-head">{new_form}</div>')
    if workshop["pending"]:
        # A turn is running and no question is waiting: poll like the
        # chat page (this page's own state route) until the reply lands.
        parts.append(f'<noscript><meta http-equiv="refresh" '
                     f'content="{chat_module.REFRESH_SECONDS}"></noscript>')
        parts.append(chat_module.poll_script(workshop["seq"], "/designs/state"))
    parts.append(transcript.render_log(entries, open_prompt=workshop["prompt"],
                                       working=workshop["pending"], empty=INTRO))
    parts.append(transcript.render_prompt(workshop["prompt"], action="/designs/choice",
                                          form_token=form_token))
    if workshop["failed"]:
        parts.append('<div class="chat-prompt" role="group" aria-label="Try again">'
                     '<div class="chat-body"><p>That design did not come out. Send it '
                     'again as it is, or say it another way below.</p></div>'
                     '<div class="chat-choices">'
                     '<form method="post" action="/designs" class="inline">'
                     f'{_hidden_token(form_token)}'
                     '<input type="hidden" name="retry" value="1">'
                     '<button type="submit">Try again</button></form></div></div>')
    if workshop["lost"]:
        parts.append('<p class="chat-note">Print Handy restarted since this design '
                     'asked what next, so the buttons are gone. Say a change, or '
                     '<em>slice it</em>, or <em>never mind</em>.</p>')
    if workshop["phase"] == "start":
        placeholder = "a 40x20x3 plate with two 4 mm holes 30 mm apart"
        label = "Describe a part" if session is None else "Describe a new part"
    else:
        placeholder = "make it 5 mm taller, slice it, or answer the question"
        label = "Message"
    parts.append('<form method="post" action="/designs" class="design-composer">'
                 f'{_hidden_token(form_token)}'
                 f'<div class="field"><label for="d-text">{label}</label>'
                 '<input id="d-text" name="text" autocomplete="off" '
                 f'placeholder="{_esc(placeholder)}"></div>'
                 '<button type="submit" class="accent">Send</button></form>')
    parts.append("</section>")
    return "".join(parts)


def render_designs_page(rows, *, theme=shell.DEFAULT_THEME, nav=None, workshop=None,
                        form_token="", notice=None, error=None):
    """The GET /designs page: the design conversation, then the catalog."""
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
                 "the design guide</a>). Opening one shows its conversation, source and "
                 "files; slicing and printing run through the normal confirmation flow.</p>"
                 "<p>Looking for a model to print instead? <a href=\"/sources\">Model "
                 "sources</a> lists every site a link can be dropped from.</p>")
    if not rows:
        parts.append("<p>No designs yet. Start one above, or describe a part with "
                     "<code>/print-design</code> in chat: every session is listed "
                     "here.</p>")
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
    parts.append('<div class="table-scroll"><table class="design-catalog"><tr>'
                 '<th>Design</th><th>Kind</th>'
                 "<th>State</th><th>Turns</th><th>Created</th><th>Updated</th>"
                 "<th>Preview</th></tr>" + "".join(body) + "</table></div>")
    return _page("designs", "".join(parts), theme=theme, nav=nav)


_CONTINUE_NOTES = {
    "finished": "This design is finished (handed off or cancelled), so it cannot take "
                "more changes. Start a new one from the Designs page.",
    "elsewhere": "This design was started in another chat (Discord, Telegram, Matrix, "
                 "the Chat page or another browser), so it carries on there.",
}


def render_design_detail(session, scad_text, *, has_stl, has_preview, has_base=False,
                         theme=shell.DEFAULT_THEME, nav=None, thread=(),
                         continue_as="elsewhere", form_token=""):
    """The GET /designs/<id> page: the conversation, the files, the source.

    ``thread`` is the session's conversation (:func:`session_thread`);
    ``continue_as`` is :func:`continue_state` for this browser, or
    ``current`` when the session is the browser's open design thread.
    """
    sid = _esc(session.get("id") or "")
    parts = [f"<h2>{_esc(session_name(session))}</h2>",
             f'<p class="muted">{_esc(session_kind(session))} · '
             f'{_esc(session_state(session))} · {int(session.get("turns") or 0)} turn(s) · '
             f'created {_esc(_when(session.get("created_at")))} · '
             f'updated {_esc(_when(session.get("updated_at")))}</p>']
    if continue_as == "current":
        parts.append('<p><a class="button accent" href="/designs">Back to the '
                     'conversation</a></p>')
    elif continue_as == "continue":
        parts.append(f'<form method="post" action="/designs/{sid}/continue" class="inline">'
                     f'{_hidden_token(form_token)}'
                     '<button type="submit" class="accent">Continue this design</button>'
                     '</form>')
    elif continue_as in _CONTINUE_NOTES:
        parts.append(f'<p class="muted">{_CONTINUE_NOTES[continue_as]}</p>')
    parts.append("<h3>Conversation</h3>")
    parts.append(transcript.render_log(
        list(thread), empty="<p>No conversation was kept for this design.</p>"))
    downloads = [f'<li><a href="/designs/{sid}/scad">Download the .scad source</a></li>']
    if has_base:
        downloads.append(
            f'<li><a href="/designs/{sid}/base">Download the base mesh (source.stl)</a>: '
            "the .scad source imports this file, so keep it next to the source "
            "to open the design in OpenSCAD</li>")
    if has_stl:
        downloads.append(f'<li><a href="/designs/{sid}/stl">Download the final .stl</a></li>')
    if has_preview:
        downloads.append(f'<li><a href="/designs/{sid}/preview">Download the preview PNG</a></li>')
    parts.append("<h3>Files</h3><ul>" + "".join(downloads) + "</ul>")
    parts.append("<h3>OpenSCAD source</h3>")
    if scad_text:
        parts.append(f"<details><summary>Show the source</summary><pre>{_esc(scad_text)}"
                     "</pre></details>")
    else:
        parts.append("<p>No source recorded for this session yet.</p>")
    return _page("design", "".join(parts), theme=theme, nav=nav)


__all__ = [
    "FILE_KINDS", "catalog_rows", "composer_message", "continue_state",
    "design_availability", "designs_root", "file_for", "load_detail",
    "owner_for_scope", "render_design_detail", "render_designs_page",
    "resume_lost", "retry_message", "session_kind", "session_name",
    "session_state", "session_thread", "start_message", "thread_of",
    "turn_preview_for", "workshop_model",
]
