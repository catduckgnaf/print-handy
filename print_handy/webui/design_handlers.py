"""Designs, sources, sliced and report handlers (#1603 item 3).

Moved out of ``webui/server.py``: the Sources page, the Designs
workshop page/posts and file route, the firmware page route, the
sliced downloads route and the settings/conversion/editable report
routes for archived jobs. ``_Handler`` keeps thin delegates with the
route-table names; behavior and output are unchanged.
"""
from __future__ import annotations

import html
import logging
import re
import secrets
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)
from . import chat as chat_module
from . import controls as controls_module
from . import data as data_module
from . import designs as designs_module
from . import shell
from . import upload as upload_module
from . import editable_download as editable_download_module
from . import external_intake_page as external_intake_module
from . import slice_result as slice_result_module
from . import firmware_page as firmware_page_module
from . import notifications as notifications_module
from . import settings_report as settings_report_module
from . import sliced as sliced_module
from . import sources as sources_module
from .sessions import REMEMBER_TTL_S


def sources_page(handler):
    handler._send(200, sources_module.render_sources_page(
        theme=handler._theme(), nav=data_module.nav_for(handler.server.settings_data),
        form_token=handler._form_token(),
        chat_enabled=handler._web_chat_enabled()),
        "text/html; charset=utf-8")

DESIGN_COOKIE = "webui_design"
_DESIGN_NONCE = re.compile(r"[0-9a-f]{12}")


def design_view(handler, issue=True):
    """This browser's design-thread view of the web chat (#1636).

    The browser's own chat scope plus the thread suffix its design cookie
    names (none: the browser's first thread), read without the shared
    posts, so the thread holds the design conversation and nothing else.
    """
    from ..bot import web
    base = handler._chat_view(issue)
    nonce = getattr(handler, "_new_design_thread", None)
    if nonce is None:
        nonce = handler._cookie_value(DESIGN_COOKIE)
        if not _DESIGN_NONCE.fullmatch(nonce or ""):
            nonce = ""
    return handler._chat_service().view(web.design_scope(base.scope, nonce), shared=False)


def _design_cookie_headers(handler):
    """The chat cookie (when issued) and the design-thread cookie (when changed)."""
    headers = list(handler._chat_cookie_headers())
    nonce = getattr(handler, "_new_design_thread", None)
    if nonce is not None:
        flags = "; Secure" if handler._cookie_secure() else ""
        headers.append(("Set-Cookie", f"{DESIGN_COOKIE}={nonce}; HttpOnly; "
                                      f"SameSite=Strict; Path=/; "
                                      f"Max-Age={REMEMBER_TTL_S}{flags}"))
    return headers


def designs_page(handler, notice=None, error=None):
    """Render the legacy /designs route; navigation directs users to /chat."""
    data = handler.server.settings_data
    rows = designs_module.catalog_rows(data)
    # The workshop submits through this browser's design-thread view, so
    # it exists only while web chat is on; with it off the page renders
    # the reason instead of a message box (designs_module).
    view = handler._design_view() if handler._web_chat_enabled() else None
    workshop = designs_module.workshop_model(data, view)
    handler._send(200, designs_module.render_designs_page(
        rows, theme=handler._theme(), nav=data_module.nav_for(data),
        workshop=workshop, form_token=handler._form_token(),
        notice=notice, error=error),
        "text/html; charset=utf-8", headers=_design_cookie_headers(handler))


def designs_state(handler):
    """Backward-compatible state endpoint, using the shared conversation."""
    handler._json(200, chat_module.chat_state(
        handler._design_view(issue=False) if handler._web_chat_enabled() else None))


def designs_post(handler, form, route="/designs"):
    """POST /designs: the message box, and Try again (#1636).

    One message to this browser's design thread. The first message of
    a thread (or one after its design finished or failed) goes as the
    /print-design command; every other goes as typed, so the design
    dispatch routes it the way it routes chat: the answer to the open
    question, a typed "slice it" or choice number, or the change the
    waiting design is after. ``retry`` sends a failed first turn's own
    description again. The page itself designs, slices and prints nothing.
    """
    if not handler._web_chat_enabled():
        handler._designs_page()
        return
    data = handler.server.settings_data
    view = handler._design_view()
    model = designs_module.workshop_model(data, view)
    if (form.get("retry") or [""])[0]:
        message = designs_module.retry_message(model)
        if message is None:
            handler._redirect(route, headers=_design_cookie_headers(handler))
            return
        text = model["session"]["description"]
    else:
        text = (form.get("text") or [""])[0].strip()
        if not text:
            handler._redirect(route, headers=_design_cookie_headers(handler))
            return
        message = designs_module.composer_message(model, text)
        designs_module.resume_lost(data, model)
    # The thread shows what was typed; the command wrapper is the page's.
    if not view.submit(message, shown=text):
        handler._designs_page(error="Too many messages in the last minute; "
                                 "give the bot a moment to catch up.")
        return
    handler._redirect(route, headers=_design_cookie_headers(handler))


def submit_uploaded_part(view, saved, change):
    """Submit one saved part upload to a chat view as a design edit (#1721).

    The message is the /print-design command (designs.upload_message)
    with the file riding it as the event's media, exactly the event a
    file dropped in Discord produces: the design dispatch seeds an
    edit session from the file, runs the change as its first turn when
    one was given, and otherwise shows the part as it is. Shared by the
    Designs page's upload form and the Chat page's, which differ only
    in the view (the design thread, or the browser's chat).
    """
    return view.submit(designs_module.upload_message(change),
                       media_urls=[str(saved)],
                       shown=designs_module.upload_shown(change, saved.name))


def part_upload_refused(handler, refused, back):
    """Render a refused part-upload receive the workflow upload's way.

    True when the receive was refused (the answer has been sent): the
    plain error page for ``back``, with Retry-After on a busy 503.
    """
    if not refused:
        return False
    headers = [("Retry-After", "30")] if refused[0] == 503 else []
    handler._send(refused[0], shell.page(
        "error", f'<p class="error">{html.escape(refused[1])}</p>'
                 f'<p><a href="{back}">Back</a></p>',
        path=back, theme=handler._theme()),
        "text/html; charset=utf-8", headers=headers)
    return True


def designs_upload_post(handler, form, route="/designs"):
    """POST /designs/upload: receive a part and start editing it here (#1721).

    The receive is the Upload page's own (_receive_upload: streamed to
    a spool file, size and quota checked before a byte lands); the save
    is save_upload into the inbox, with .scad allowed as the extra type
    a design source needs; then the saved file is submitted to this
    browser's design thread as a /print-design edit, the same flow the
    message box starts from a description.
    """
    if not handler._web_chat_enabled():
        handler._designs_page()
        return
    data = handler.server.settings_data
    ready, reason = designs_module.design_availability(data)
    if not ready:
        handler._designs_page(error=reason)
        return
    view = handler._design_view()

    def handle(fields, files):
        if not files:
            handler._html_refusal(400, "no file was attached", route)
            return
        change = (fields.get("change") or [""])[0].strip()
        saved, reason = upload_module.save_upload(
            data, files[0][0], files[0][1],
            extra_exts=designs_module.EDIT_UPLOAD_EXTS)
        if saved is None:
            handler._html_refusal(507 if reason.startswith(upload_module.INBOX_FULL)
                                  else 400, reason, route)
            return
        if not submit_uploaded_part(view, saved, change):
            handler._designs_page(error=f"{saved.name} is in the inbox, but the "
                                   "edit could not be started (too many messages "
                                   "in the last minute). Try again in a moment.")
            return
        handler._redirect(route, headers=_design_cookie_headers(handler))

    part_upload_refused(handler, handler._receive_upload(handle), route)


def designs_new_post(handler, form, route="/designs"):
    """POST /designs/new: a fresh design thread; the old session is left as it is.

    Only the browser's thread changes (a new suffix in its design
    cookie). Nothing is cancelled or deleted: the design it leaves stays
    in the catalog, and its page offers to continue it.
    """
    if handler._web_chat_enabled():
        handler._new_design_thread = secrets.token_hex(6)
    handler._redirect(route, headers=_design_cookie_headers(handler))


def designs_continue_post(handler, rest, form, route="/designs"):
    """POST /designs/<id>/continue: make that session this browser's open thread.

    Only a live session one of this browser's own design threads started
    can be continued here; any other is refused with its page's reason.
    """
    data = handler.server.settings_data
    detail = designs_module.load_detail(data, rest) if rest and "/" not in rest else None
    if detail is None:
        handler._json(404, {"ok": False, "reason": "no such design session"})
        return
    session = detail[0]
    if not handler._web_chat_enabled():
        handler._designs_page(error="Web chat is turned off, so a design cannot be "
                                 "continued here.")
        return
    scope = handler._chat_view(issue=False).scope
    if designs_module.continue_state(session, scope) != "continue":
        handler._designs_page(error="That design cannot be continued from this browser: "
                                 "it is finished, or another chat started it.")
        return
    handler._new_design_thread = designs_module.thread_of(session)[1]
    handler._redirect(route, headers=_design_cookie_headers(handler))


def designs_choice_post(handler, form, route="/designs"):
    """Answer one open design question from the unified composer route."""
    if not handler._web_chat_enabled():
        handler._chat_page() if route == "/chat" else handler._designs_page()
        return
    clarify_id = (form.get("clarify_id") or [""])[0]
    try:
        index = int((form.get("index") or ["-1"])[0])
    except ValueError:
        index = -1
    if clarify_id and index >= 0:
        try:
            label = handler._design_view().answer(clarify_id, index)
        except Exception:
            log.warning("design page answer for %s did not reach the bot",
                        clarify_id, exc_info=True)
            page = handler._chat_page if route == "/chat" else handler._designs_page
            page(error="That answer didn't reach the bot in time. The question is still open, so try again.")
            return
        if label is None:
            page = handler._chat_page if route == "/chat" else handler._designs_page
            page(error="That answer wasn't accepted: the question may have closed, or the answer belongs to another step.")
            return
    handler._redirect(route)


def _detail_page(handler, session_id):
    data = handler.server.settings_data
    detail = designs_module.load_detail(data, session_id)
    if detail is None:
        handler._json(404, {"ok": False, "reason": "no such design session"})
        return
    session, scad_text, has_stl, has_preview, has_base = detail
    continue_as = "elsewhere"
    if handler._web_chat_enabled():
        browser = handler._chat_view(issue=False).scope
        continue_as = designs_module.continue_state(session, browser)
        if continue_as == "continue" and designs_module.owner_for_scope(
                handler._design_view(issue=False).scope) == session.get("owner_key"):
            continue_as = "current"
    handler._send(200, designs_module.render_design_detail(
        session, scad_text, has_stl=has_stl, has_preview=has_preview,
        has_base=has_base, theme=handler._theme(),
        nav=data_module.nav_for(data),
        thread=designs_module.session_thread(data, session),
        continue_as=continue_as, form_token=handler._form_token()),
        "text/html; charset=utf-8")


def design_route(handler, rest):
    """GET /designs/<id> (detail) and /designs/<id>/<kind> (a file).

    ``rest`` is the path after ``/designs/``: exactly one segment is
    a session id, exactly two is that session's ``scad`` / ``stl`` /
    ``preview`` / ``base`` file. Anything else (extra segments, ``..``,
    unknown ids or kinds) is a 404; the file itself is resolved by
    :func:`print_handy.core.design.session_file` from the session
    record, so nothing outside the session's own folder can be named.
    ``preview?turn=N`` is one turn's preview, as the design thread shows
    it, resolved by :func:`print_handy.core.design.turn_preview` from
    the record's turn count, the same containment.
    """
    segments = rest.split("/")
    data = handler.server.settings_data
    if len(segments) == 1 and segments[0]:
        _detail_page(handler, segments[0])
        return
    if len(segments) == 2 and segments[0] and segments[1] in designs_module.FILE_KINDS:
        turn = (parse_qs(urlparse(handler.path).query).get("turn") or [None])[0]
        if segments[1] == "preview" and turn is not None:
            found = designs_module.turn_preview_for(data, segments[0], turn)
        else:
            found = designs_module.file_for(data, segments[0], segments[1])
        if found is None:
            handler._json(404, {"ok": False, "reason": "no such design file"})
            return
        path, content_type, inline = found
        disposition = "inline" if inline else "attachment"
        handler._send_file(path, content_type,
                        headers=[("Content-Disposition",
                                  f'{disposition}; filename="{path.name}"')])
        return
    handler._json(404, {"ok": False, "reason": "no such design session"})

def firmware_route(handler):
    # Firmware and updates per printer (#1280 section I).
    # ?printer=<id> reads that one printer's installed versions; the
    # read also records the update observation the Web UI's toast
    # notifications carry across pages (#1610), and update buttons
    # show there when Web UI printer controls are on.
    data = handler.server.settings_data
    checked = (parse_qs(urlparse(handler.path).query).get("printer") or [None])[0]
    handler._send(200, firmware_page_module.render_firmware_page(
        data, theme=handler._theme(), checked=checked,
        nav=data_module.nav_for(data),
        controls=controls_module.controls_enabled(data),
        observe=lambda pid, name, status, entry:
            notifications_module.record_observation(data, pid, name, status, entry)),
        "text/html; charset=utf-8")

def settings_report_route(handler, rest=""):
    """Download the evidence-backed conversion decisions for an archived job."""
    segments = [segment for segment in rest.split("/") if segment]
    archive = data_module.archive_root(handler.server.settings_data)
    payload = (settings_report_module.report_payload(archive, segments[0])
               if len(segments) == 1 else None)
    if payload is None:
        handler._json(404, {"ok": False, "reason": "no recorded settings-decision report for this job"})
        return
    name = f"{segments[0]}-settings-decisions.json"
    handler._send(200, settings_report_module.render_json(payload), "application/json; charset=utf-8",
               headers=[("Content-Disposition", f'attachment; filename="{name}"'),
                        ("Cache-Control", "no-store")])

def conversion_report_route(handler, rest=""):
    """The readable conversion report for an archived job (#1525).

    Rendered from the same conversion provenance as the JSON
    settings report; ?format=text serves the chat form.
    """
    segments = [segment for segment in rest.split("/") if segment]
    archive = data_module.archive_root(handler.server.settings_data)
    rendered = (settings_report_module.conversion_report(archive, segments[0])
                if len(segments) == 1 else None)
    if rendered is None:
        handler._json(404, {"ok": False, "reason": "no recorded conversion provenance for this job"})
        return
    html_body, text_body = rendered
    if "text" in (parse_qs(urlparse(handler.path).query).get("format") or []):
        handler._send(200, text_body.encode("utf-8"), "text/plain; charset=utf-8",
                   headers=[("Cache-Control", "no-store")])
        return
    handler._send(200, html_body.encode("utf-8"), "text/html; charset=utf-8",
               headers=[("Cache-Control", "no-store")])

def editable_export_route(handler, rest=""):
    """Download an editable destination 3MF export for an archived job (#1525).

    The export is built from the job's own named source by
    core.editable_export: it starts no print and touches no spool.
    A refusal (for example a sliced-only source) is the answer, in
    the export's own words.
    """
    segments = [segment for segment in rest.split("/") if segment]
    archive = data_module.archive_root(handler.server.settings_data)
    path, report = (editable_download_module.export_for_job(archive, segments[0])
                    if len(segments) == 1 else (None, "no such job"))
    if path is None:
        handler._json(404, {"ok": False, "reason": report})
        return
    handler._send_file(path, "model/3mf",
                    headers=[("Content-Disposition",
                              f'attachment; filename="{path.name}"'),
                             ("Cache-Control", "no-store")])

def slice_result_route(handler, rest=""):
    """The slice-result card for an archived job (#1525, tranche G wiring).

    Read-only: built by core.slice_result from the job's own plate files.
    """
    segments = [segment for segment in rest.split("/") if segment]
    if len(segments) == 3 and segments[1] == "thumbnail":
        slice_result_thumbnail_route(handler, segments[0], segments[2])
        return
    archive = data_module.archive_root(handler.server.settings_data)
    artifacts = (slice_result_module.result_artifacts(
        handler.server.settings_data, archive, segments[0])
        if len(segments) == 1 else None)
    if artifacts is None:
        handler._json(404, {"ok": False, "reason": "no sliced plate files recorded for this job"})
        return
    from print_handy.core import slice_result as _core
    loaded = _core.load_job(archive, segments[0])
    has_source = bool(loaded and _core.source_path(loaded[0], loaded[1]))
    handler._send(200, slice_result_module.render_result_page(
        artifacts, segments[0], theme=handler._theme(),
        nav=data_module.nav_for(handler.server.settings_data),
        has_source=has_source), "text/html; charset=utf-8")


def slice_result_thumbnail_route(handler, job_id, index_raw):
    """The plate's own thumbnail, inside the /slice-result/* route.

    The PNG bytes the slicer embedded in the plate file, served as an
    image; a plate without one is a plain 404, never a rendered stand-in.
    """
    try:
        index = int(index_raw)
    except (TypeError, ValueError):
        handler._json(404, {"ok": False, "reason": "no such thumbnail"})
        return
    archive = data_module.archive_root(handler.server.settings_data)
    from print_handy.core import slice_result as _core
    png = _core.thumbnail_png(archive, job_id, index)
    if png is None:
        handler._json(404, {"ok": False, "reason": "this plate carries no thumbnail"})
        return
    handler._send(200, png, "image/png")


def u1_feasibility_route(handler, rest=""):
    """The U1 feasibility report for an archived job's source (#1525).

    Read-only; the report keeps its unverified-hardware wording.
    """
    segments = [segment for segment in rest.split("/") if segment]
    archive = data_module.archive_root(handler.server.settings_data)
    from print_handy.core import slice_result as _core
    report = (_core.u1_report_for_job(archive, segments[0])
              if len(segments) == 1 else None)
    if report is None:
        handler._json(404, {"ok": False, "reason": "no source 3MF recorded for this job"})
        return
    handler._send(200, slice_result_module.render_u1_page(
        report, segments[0], theme=handler._theme(),
        nav=data_module.nav_for(handler.server.settings_data)),
        "text/html; charset=utf-8")


def external_intake_route(handler, notice=None, error=None):
    """GET /external-intake: draft cards from the watched folder (#1525)."""
    data = handler.server.settings_data
    cards = external_intake_module.scan_cards(data)
    handler._send(200, external_intake_module.render_page(
        data, cards, theme=handler._theme(), nav=data_module.nav_for(data),
        form_token=handler._form_token(), notice=notice, error=error),
        "text/html; charset=utf-8")


def external_intake_confirm_post(handler, form):
    """POST /external-intake/confirm: the Mark-as-printed confirmation."""
    data = handler.server.settings_data

    def field(name):
        return (form.get(name) or [""])[0].strip()

    spool_raw, grams_raw = field("spool_id"), field("grams")
    try:
        spool_id = int(spool_raw) if spool_raw else None
        grams = float(grams_raw) if grams_raw else None
    except ValueError:
        external_intake_route(handler, error="Spool id and grams must be numbers.")
        return
    if (spool_id is None) != (grams is None):
        external_intake_route(handler, error="Give both a spool id and its grams, "
                              "or neither — a print with no declared spool usage is "
                              "recorded without a spool charge.")
        return
    from print_handy.core import external_intake as _intake
    try:
        result = external_intake_module.confirm_card(
            data, sha256=field("sha256"), path=field("path"),
            printer_id=field("printer_id"), print_id=field("print_id"),
            spool_id=spool_id, grams=grams)
    except _intake.IntakeRefused as exc:
        external_intake_route(handler, error=str(exc))
        return
    external_intake_route(handler, notice=f"Recorded as printed ({result['identity']}): "
                          f"{result['accounting'].get('spool_charge', 'recorded')}.")


def sliced_route(handler, rest=""):
    """GET /sliced (the downloads page) and /sliced/<job>/<n> (one plate file, #1443).

    The file itself is resolved by :mod:`print_handy.webui.sliced` from
    the job's own JOB.json inside the archive — the request names a job
    and a plate position, never a path — and is sent as an attachment,
    the way the designs route sends a session's files.
    """
    data = handler.server.settings_data
    archive = data_module.archive_root(data)
    if not rest:
        handler._send(200, sliced_module.render_sliced_page(
            sliced_module.list_sliced(archive), theme=handler._theme(),
            nav=data_module.nav_for(data)), "text/html; charset=utf-8")
        return
    segments = [segment for segment in rest.split("/") if segment != ""]
    path = (sliced_module.resolve_download(archive, segments[0], segments[1])
            if len(segments) == 2 else None)
    if path is None:
        handler._json(404, {"ok": False, "reason": "no such sliced file"})
        return
    handler._send_file(path, sliced_module.content_type(path),
                    headers=[("Content-Disposition",
                              f'attachment; filename="{path.name}"'),
                             ("Cache-Control", "no-store")])

