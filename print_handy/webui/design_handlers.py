"""Designs, sources, sliced and report handlers (#1603 item 3).

Moved out of ``webui/server.py``: the Sources page, the Designs
workshop page/posts and file route, the firmware page route, the
sliced downloads route and the settings/conversion/editable report
routes for archived jobs. ``_Handler`` keeps thin delegates with the
route-table names; behavior and output are unchanged.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import logging

log = logging.getLogger(__name__)
from . import controls as controls_module
from . import data as data_module
from . import designs as designs_module
from . import editable_download as editable_download_module
from . import firmware_page as firmware_page_module
from . import notifications as notifications_module
from . import settings_report as settings_report_module
from . import sliced as sliced_module
from . import sources as sources_module


def sources_page(handler):
    handler._send(200, sources_module.render_sources_page(
        theme=handler._theme(), nav=data_module.nav_for(handler.server.settings_data),
        form_token=handler._form_token(),
        chat_enabled=handler._web_chat_enabled()),
        "text/html; charset=utf-8")

def designs_page(handler, notice=None, error=None):
    data = handler.server.settings_data
    rows = designs_module.catalog_rows(data)
    # The workshop submits through this browser's web chat view, so
    # it exists only while web chat is on; with it off the page
    # renders the reason instead of a form (designs_module).
    view = handler._chat_view() if handler._web_chat_enabled() else None
    workshop = designs_module.workshop_model(data, view)
    handler._send(200, designs_module.render_designs_page(
        rows, theme=handler._theme(), nav=data_module.nav_for(data),
        workshop=workshop, form_token=handler._form_token(),
        notice=notice, error=error),
        "text/html; charset=utf-8", headers=handler._chat_cookie_headers())

def designs_post(handler, form):
    """POST /designs: the workshop's start and follow-up forms (#1449).

    Both submit one message to this browser's web chat view: a start
    submits the /print-design command for the description, a follow-up
    submits its text as-is (the design dispatch routes it: the answer
    to the open question, or the revision the session waits for).
    The page itself designs, slices and prints nothing.
    """
    if not handler._web_chat_enabled():
        handler._designs_page()
        return
    text = (form.get("text") or [""])[0].strip()
    if not text:
        handler._redirect("/designs")
        return
    mode = (form.get("mode") or ["start"])[0]
    message = designs_module.start_message(text) if mode == "start" else text
    if not handler._chat_view().submit(message):
        handler._designs_page(error="Too many messages in the last minute; "
                                 "give the bot a moment to catch up.")
        return
    handler._redirect("/designs", headers=handler._chat_cookie_headers())

def designs_choice_post(handler, form):
    """POST /designs/choice: one button of the design's open question.

    The same press as /chat/choice (the prompt's own button, resolved
    through the adapter with the prompt's own approval rules), only
    landing back on the designs page.
    """
    if not handler._web_chat_enabled():
        handler._designs_page()
        return
    clarify_id = (form.get("clarify_id") or [""])[0]
    try:
        index = int((form.get("index") or ["-1"])[0])
    except ValueError:
        index = -1
    if clarify_id and index >= 0:
        try:
            label = handler._chat_view().answer(clarify_id, index)
        except Exception:
            log.warning("design page answer for %s did not reach the bot",
                        clarify_id, exc_info=True)
            handler._designs_page(error="That answer didn't reach the bot "
                                     "in time. The question is still "
                                     "open — try again.")
            return
        if label is None:
            handler._designs_page(error="That answer wasn't accepted — "
                                     "the question may have closed, or "
                                     "the answer belongs to another step.")
            return
    handler._redirect("/designs")

def design_route(handler, rest):
    """GET /designs/<id> (detail) and /designs/<id>/<kind> (a file).

    ``rest`` is the path after ``/designs/``: exactly one segment is
    a session id, exactly two is that session's ``scad`` / ``stl`` /
    ``preview`` / ``base`` file. Anything else — extra segments, ``..``, unknown
    ids or kinds — is a 404; the file itself is resolved by
    :func:`print_handy.core.design.session_file` from the session
    record, so nothing outside the session's own folder can be named.
    """
    segments = rest.split("/")
    data = handler.server.settings_data
    if len(segments) == 1 and segments[0]:
        detail = designs_module.load_detail(data, segments[0])
        if detail is None:
            handler._json(404, {"ok": False, "reason": "no such design session"})
            return
        session, scad_text, has_stl, has_preview, has_base = detail
        handler._send(200, designs_module.render_design_detail(
            session, scad_text, has_stl=has_stl, has_preview=has_preview,
            has_base=has_base, theme=handler._theme()),
            "text/html; charset=utf-8")
        return
    if len(segments) == 2 and segments[0] and segments[1] in designs_module.FILE_KINDS:
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
        observe=lambda pid, name, status:
            notifications_module.record_observation(data, pid, name, status)),
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

