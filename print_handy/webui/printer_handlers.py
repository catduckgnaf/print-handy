"""Per-printer status, control and spools handlers (#1603 item 3).

Moved out of ``webui/server.py``: the ``/api/printers/*`` status and
POST routes, the dashboard E-stop post, the Spools index/page/post
handlers, the Printers page/post and the printer screen page.
``_Handler`` keeps thin delegates with the route-table names;
authorization is still enforced by the route policy in ``server.py``
before these run, and every status code and page output is unchanged.
"""
from __future__ import annotations

from urllib.parse import parse_qs, quote, unquote, urlparse

from .. import settings
from . import body as body_module
from . import controls as controls_module
from . import printers_page as printers_page_module
from . import screen as screen_module
from . import spools as spools_module


def printer_status_route(handler, rest):
    """GET /api/printers/<id>/status and /api/printers/<id>/camera/snapshot
    (#708): exactly one id segment plus a known tail; anything else
    under /api/printers/ is a 404 naming the routes that exist there."""
    segments = rest.split("/")
    if len(segments) == 2 and segments[0] and segments[1] == "status":
        code, payload = handler.server.printer_status_func(unquote(segments[0]))
        handler._json(code, payload)
        return
    if len(segments) == 2 and segments[0] and segments[1] == "spools":
        code, payload = handler.server.spools_view_func(unquote(segments[0]))
        handler._json(code, payload)
        return
    if len(segments) == 3 and segments[0] and segments[1:] == ["camera", "snapshot"]:
        code, body, content_type = handler.server.camera_snapshot_func(unquote(segments[0]))
        if code != 200:
            handler._json(code, body)
            return
        handler._send(200, body, content_type, headers=[("Cache-Control", "no-store")])
        return
    if len(segments) == 3 and segments[0] and segments[1:] == ["screen", "snapshot"]:
        code, body, content_type = handler.server.screen_snapshot_func(unquote(segments[0]))
        if code != 200:
            handler._json(code, body)
            return
        handler._send(200, body, content_type, headers=[("Cache-Control", "no-store")])
        return
    handler._json(404, {"ok": False,
                     "reason": "try /api/printers/<id>/status, "
                               "/api/printers/<id>/spools, "
                               "/api/printers/<id>/camera/snapshot or "
                               "/api/printers/<id>/screen/snapshot"})

def printer_post_route(handler, rest):
    """POST /api/printers/<id>/...: a spool action or a control verb,
    both behind the ``header`` policy (routes.py)."""
    segments = rest.split("/")
    if len(segments) >= 2 and segments[1] == "spools":
        handler._printer_spools_route(rest)
    else:
        handler._printer_control_route(rest)

def printer_control_route(handler, rest):
    """POST /api/printers/<id>/pause|resume|cancel|temperature|target.

    Also the ha-print-handy #1 verbs: emergency_stop,
    firmware_restart, server_restart, gcode and exclude.

    Authorization is the ``header`` policy (routes.py): the
    access-token header only (or no token configured); a
    login-session cookie never authorizes a control POST. The body must be JSON,
    so a cross-site HTML form cannot post here. The off-by-default
    switch, cancel's confirm field and the temperature limits live
    in controls.py; a 404 for an unknown printer keeps the status
    route's shape.
    """
    segments = rest.split("/")
    verbs = (*controls_module.ACTIONS, "temperature", "target",
             *controls_module.POWER_ACTIONS, "gcode", "exclude", "nozzle")
    if len(segments) != 2 or not segments[0] or segments[1] not in verbs:
        handler._json(404, {"ok": False, "reason": "try /api/printers/<id>/pause, resume, "
                                                "cancel, temperature, target, emergency_stop, "
                                                "firmware_restart, server_restart, gcode, "
                                                "exclude or nozzle"})
        return
    body, error = body_module.read_json(
        handler, limit=4096, noun="control", tail="Nothing sent.")
    if error:
        handler._json(error[0], {"ok": False, "reason": error[1]})
        return
    printer_id, verb = unquote(segments[0]), segments[1]
    code, payload = handler.server.printer_control_func(printer_id, verb, body)
    handler._json(code, payload)

def dashboard_estop_post(handler, printer_id, form):
    """POST /dashboard/estop/<id>: the Status page's E-stop button.

    A browser cannot send the ``X-WebUI-Token`` header the
    ``/api/printers/<id>/emergency_stop`` route requires, so on a
    claimed install that route refused every press from the page
    with a 401. This route is ``session+csrf`` like every other
    browser form: a live login session plus the session-bound
    ``_csrf`` value the page embeds. The stop itself is the API's
    own (``printer_control_func``, ``emergency_stop``, confirmed),
    so the controls_enabled switch and every refusal are unchanged.
    """
    code, payload = handler.server.printer_control_func(
        printer_id, "emergency_stop", {"confirm": True})
    handler._json(code, payload)

def printer_spools_route(handler, rest):
    """POST /api/printers/<id>/spools/<action> (and purchase/<step>).

    Authorization and body rules mirror the control route exactly:
    the access-token header only (a login-session cookie never
    authorizes a spool POST), a JSON body only, so a cross-site
    HTML form cannot post here. The controls_enabled guard, the
    named-printer rule and every refusal live in spools.py.
    """
    segments = rest.split("/")
    actions = (*spools_module.ACTIONS, "purchase/propose", "purchase/create")
    action = None
    if len(segments) == 3 and segments[0] and segments[1] == "spools" \
            and segments[2] in spools_module.ACTIONS:
        action = segments[2]
    elif len(segments) == 4 and segments[0] and segments[1] == "spools" \
            and segments[2] == "purchase" and segments[3] in ("propose", "create"):
        action = f"purchase/{segments[3]}"
    if action is None:
        handler._json(404, {"ok": False, "reason": "try /api/printers/<id>/spools/"
                                                + ", ".join(actions)})
        return
    body, error = body_module.read_json(
        handler, limit=65536, noun="spool", tail="Nothing changed.")
    if error:
        handler._json(error[0], {"ok": False, "reason": error[1]})
        return
    code, payload = handler.server.spools_action_func(unquote(segments[0]), action, body)
    handler._json(code, payload)

def spools_error_page(handler, printer_id, code, reason):
    handler._send(code, spools_module.render_spools_error(
        printer_id, code, reason, theme=handler._theme()),
        "text/html; charset=utf-8")

def printers_page(handler, notice=None, error=None, code=200):
    """GET /printers: the roster with actions and Add a printer (#875)."""
    done = (parse_qs(urlparse(handler.path).query).get("done") or [""])[0]
    handler._send(code, printers_page_module.render(
        settings.printers(handler._walkthrough_data()), form_token=handler._form_token(),
        notice=notice or done or None, error=error, theme=handler._theme()),
        "text/html; charset=utf-8")

def printers_post(handler, form):
    """POST /printers: append one printer through the roster writer."""
    ok, message = printers_page_module.add(
        form, settings.printers(handler._walkthrough_data()),
        environ=handler.server.webui_environ)
    if ok:
        handler._redirect("/printers?done=" + quote(message, safe=""))
    else:
        handler._printers_page(error=message, code=400)

def screen_page(handler, printer_id):
    """GET /printers/<id>/screen: the printer screen view (#1303).

    Availability is probed (discovery only, no frame is pulled);
    the page renders the labeled read-only view when a screen is
    published and the reason when it is not, so an unsupported or
    unavailable screen is an explanation, never a broken image.
    An unknown id is a 404 in the same shape.
    """
    if not printer_id or "/" in printer_id:
        handler._json(404, {"ok": False,
                         "reason": "that printer id does not name a "
                                   "printer screen page"})
        return
    try:
        entry = settings.by_id(printer_id, handler.server.settings_data)
    except settings.SettingsError:
        handler._json(404, {"ok": False,
                         "reason": f"no printer with id {printer_id!r} "
                                   f"is configured"})
        return
    ok, reason = handler.server.screen_probe_func(printer_id)
    handler._send(200, screen_module.render_page(
        entry.get("name") or printer_id, printer_id,
        ok=ok, reason=reason, theme=handler._theme(),
        touch=controls_module.controls_enabled(handler.server.settings_data),
        form_token=handler._form_token()),
        "text/html; charset=utf-8")

def screen_touch_post(handler, printer_id, form):
    """POST /printers/<id>/screen/touch: one tap on the printer screen.

    ``session+csrf`` like the dashboard E-stop (a browser cannot send
    the ``X-WebUI-Token`` header the API routes require): a live login
    session plus the session-bound ``_csrf`` value the screen page
    embeds. The form carries only the tap coordinates; the controls
    gate, discovery and every refusal live in ``screen.touch``, and
    the printer's address never reaches the browser.
    """
    if not printer_id or "/" in printer_id:
        handler._json(404, {"ok": False,
                         "reason": "that printer id does not name a "
                                   "printer screen page"})
        return
    x = (form.get("x") or [""])[0]
    y = (form.get("y") or [""])[0]
    code, payload = handler.server.screen_touch_func(printer_id, x, y)
    handler._json(code, payload)

def spools_page(handler, printer_id):
    """GET /spools/<id>: the per-printer Spools page (#789).

    A failed view (Spoolman down, unknown id) renders the themed
    error page at the same status code (#840); the JSON shape is
    kept for ``/api/printers/<id>/spools`` only. A ``?done=`` from
    a form post's redirect (#840's Post/Redirect/Get) reads back
    as the page notice.
    """
    if not printer_id or "/" in printer_id:
        handler._spools_error_page(printer_id, 404,
                                "That printer id does not name a spools page.")
        return
    code, view = handler.server.spools_view_func(printer_id)
    if code != 200:
        handler._spools_error_page(printer_id, code, view.get("reason", ""))
        return
    query = parse_qs(urlparse(handler.path).query)
    done = (query.get("done") or [""])[0]
    handler._send(200, spools_module.render_spools_page(
        view, form_token=handler._form_token(), theme=handler._theme(),
        notice=done or None,
        show_archived=(query.get("archived") or [""])[0] == "1",
        purchase_ready=spools_module.purchase_available(),
        controls_enabled=controls_module.controls_enabled(handler.server.settings_data),
        printers=settings.printers(handler.server.settings_data)),
        "text/html; charset=utf-8")

def spools_page_post(handler, printer_id, form):
    """POST /spools/<id>: one picker form from the Spools page.

    Same ``session+csrf`` policy as a config POST (routes.py; a
    login session alone never authorizes it). The form converts to the same action the JSON API takes,
    so the page and the API can never diverge. Success answers
    with Post/Redirect/Get (#840): a 303 to the page with the
    action's detail in ``?done=``, so a reload never repeats the
    action. Refusals keep the direct render, so the error and the
    submitted values stay on screen, and ``purchase/propose`` keeps
    its direct render too — its proposed lines belong on this page
    and resubmitting it creates nothing.
    """
    back = f"/spools/{quote(printer_id, safe='')}"
    if not printer_id or "/" in printer_id:
        handler._spools_error_page(printer_id, 404,
                                "That printer id does not name a spools page.")
        return
    action_name = (form.get("spool_action") or [""])[0]
    action = None
    proposed = None
    try:
        action, body = spools_module.form_body(action_name, form)
    except spools_module.SpoolsError as refusal:
        code, payload = refusal.code, {"ok": False, "reason": refusal.reason}
    else:
        code, payload = handler.server.spools_action_func(printer_id, action, body)
        if code == 200 and action == "purchase/propose":
            proposed = payload.get("lines")
    if code == 200 and action != "purchase/propose":
        handler._redirect(f"{back}?done=" + quote(payload.get("detail", ""), safe=""))
        return
    view_code, view = handler.server.spools_view_func(printer_id)
    if view_code != 200:
        handler._spools_error_page(printer_id, view_code, view.get("reason", ""))
        return
    handler._send(code if code != 200 else 200,
               spools_module.render_spools_page(
                   view, form_token=handler._form_token(), theme=handler._theme(),
                   notice=payload.get("detail") if code == 200 else None,
                   error=payload.get("reason") if code != 200 else None,
                   proposed=proposed,
                   purchase_ready=spools_module.purchase_available()),
               "text/html; charset=utf-8")

def spools_index(handler):
    entries = settings.printers(handler.server.settings_data)
    if len(entries) == 1:
        # One printer: no picker step in the way (#875).
        handler._redirect("/spools/" + quote(str(entries[0]["id"]), safe=""))
        return
    handler._send(200, spools_module.render_spools_index(entries, theme=handler._theme()),
               "text/html; charset=utf-8")

