"""Configuration, printer-settings and debug handlers (#1603 item 3).

Moved out of ``webui/server.py``: the Config page and its preview/save
posts, the Printer Settings page/save, and the ``/debug`` post.
``_Handler`` keeps thin delegates (including ``_field_values`` and
``_config_html``, which other handlers use); behavior, validation and
page output are unchanged.
"""
from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlparse

from .. import settings
from . import config as config_api
from . import data as data_module
from . import pages
from . import printer_settings as printer_settings_module
from . import walkthrough as walkthrough_module
from .status_handlers import _overall_job, render_status_html

log = logging.getLogger(__name__)


def field_values(handler):
    return config_api.read_fields(environ=handler.server.webui_environ)

def config_html(handler, field_values, **kwargs):
    return pages.render_config_page(
        field_values, data=handler.server.settings_data,
        form_token=handler._form_token(), **kwargs)

def config_page(handler):
    query = parse_qs(urlparse(handler.path).query)
    section = (query.get("section") or [None])[0]
    reveal = (query.get("setup") or [None])[0]
    handler._send(200, handler._config_html(handler._field_values(), theme=handler._theme(),
                                      section=section, reveal=reveal),
               "text/html; charset=utf-8")

def printer_settings_page(handler):
    """GET /printer-settings (?printer=<id>): one printer's settings (#1448)."""
    query = parse_qs(urlparse(handler.path).query)
    data = handler.server.settings_data
    printer_id = (query.get("printer") or [None])[0] \
        or settings.default_printer_id(data)
    try:
        settings.by_id(printer_id, data)
    except settings.SettingsError:
        handler._json(404, {"ok": False, "reason": "no such printer"})
        return
    model = printer_settings_module.build_model(
        data, printer_id, environ=handler.server.webui_environ)
    handler._send(200, printer_settings_module.render_page(
        model, form_token=handler._form_token(), theme=handler._theme(),
        nav=data_module.nav_for(data)), "text/html; charset=utf-8")

def printer_settings_save(handler, form):
    """POST /printer-settings: validate and save one printer's settings (#1448).

    All validation and writing live in
    :mod:`print_handy.webui.printer_settings`; a refusal re-renders
    the page with the submitted values and the refusal in place.
    """
    data = handler.server.settings_data
    printer_id = (form.get("printer") or [""])[0]
    try:
        settings.by_id(printer_id, data)
    except settings.SettingsError:
        handler._json(404, {"ok": False, "reason": "no such printer"})
        return
    outcome = printer_settings_module.save(
        data, printer_id, form, environ=handler.server.webui_environ,
        confirmed=tuple(form.get(pages.REBIND_FIELD) or ()))
    if outcome[0] == "result":
        _kind, changed, warnings = outcome
        handler._send(200, printer_settings_module.render_result(
            changed, printer_id=printer_id, warnings=warnings,
            theme=handler._theme(), nav=data_module.nav_for(data)),
            "text/html; charset=utf-8")
        return
    _kind, model, submitted, errors, moves = outcome
    handler._send(400 if errors else 200, printer_settings_module.render_page(
        model, form_token=handler._form_token(), theme=handler._theme(),
        nav=data_module.nav_for(data), errors=errors,
        submitted=submitted, rebinds=moves), "text/html; charset=utf-8")

def config_preview(handler, form):
    field_values = handler._field_values()
    changes, submitted, errors = pages.parse_form(form, field_values)
    if not errors and changes:
        field_errors, general = pages.validate_change_set(
            changes, environ=handler.server.webui_environ)
        errors.update(field_errors)
        if general:
            errors["_general"] = general
    if errors:
        handler._send(400, handler._config_html(
            field_values, errors=errors, submitted=submitted,
            theme=handler._theme(),
            section=(form.get(pages.SECTION_FIELD) or [None])[0],
            reveal=(form.get(pages.REVEAL_FIELD) or [None])[0]),
                   "text/html; charset=utf-8")
        return
    if not changes:
        handler._send(200, handler._config_html(
            field_values, submitted=submitted, theme=handler._theme(),
            section=(form.get(pages.SECTION_FIELD) or [None])[0],
            reveal=(form.get(pages.REVEAL_FIELD) or [None])[0],
            notice="No changes: every submitted value already matches "
                   "the effective settings."), "text/html; charset=utf-8")
        return
    effective = {fv.field: fv.value for fv in field_values}
    entries = [(field, effective[field], value) for field, value in changes.items()]
    handler._send(200, pages.render_preview_page(
        entries, submitted, form_token=handler._form_token(), theme=handler._theme(),
        warnings=handler._preview_warnings(changes),
        section=(form.get(pages.SECTION_FIELD) or [None])[0],
        reveal=(form.get(pages.REVEAL_FIELD) or [None])[0],
        rebinds=config_api.pending_rebinds(
            {field.id: value for field, value in changes.items()},
            environ=handler.server.webui_environ)),
        "text/html; charset=utf-8")

def request_is_https(handler):
    """Whether this request reached the Web UI over HTTPS. The server
    itself speaks plain HTTP; HTTPS arrives through a reverse proxy,
    which reports the original scheme as X-Forwarded-Proto."""
    forwarded = handler.headers.get("X-Forwarded-Proto") or ""
    return forwarded.split(",")[0].strip().lower() == "https"

def preview_warnings(handler, changes):
    """Plain-language warnings about a previewed change set. Turning
    the Secure session cookie on over plain HTTP strands browser
    sign-ins: browsers drop Secure cookies there, so the session
    cookie never comes back and there is no UI way back in — the
    preview is the place to say so before the save is confirmed."""
    warnings = []
    for field, value in changes.items():
        if field.id == "webui.cookie_secure" and value and not handler._request_is_https():
            warnings.append(
                "The Secure session cookie is being turned on, but this "
                "request arrived over plain HTTP. Browsers drop Secure "
                "cookies on plain HTTP, so browser sign-ins will not "
                "stick once this takes effect. Turn it on only when the "
                "Web UI is reached over HTTPS, for example behind an "
                "HTTPS reverse proxy.")
    return warnings

def config_save(handler, form):
    field_values = handler._field_values()
    if (form.get(pages.CONFIRM_FIELD) or [""])[0] != "yes":
        handler._html_refusal(400, "saving needs the preview step: POST to "
                                "/config/preview first, then confirm", "/config")
        return
    # The confirm form names the fields the preview parsed; re-parse
    # exactly those, so hidden-input absence keeps its preview meaning.
    named = (form.get(pages.FIELDS_FIELD) or [""])[0]
    only = {part for part in named.split(",") if part} or None
    changes, submitted, errors = pages.parse_form(form, field_values, only=only)
    if errors:
        handler._send(400, handler._config_html(
            field_values, errors=errors, submitted=submitted,
            theme=handler._theme(),
            section=(form.get(pages.SECTION_FIELD) or [None])[0],
            reveal=(form.get(pages.REVEAL_FIELD) or [None])[0]),
                   "text/html; charset=utf-8")
        return
    try:
        changed, warnings = config_api.write_changes_reporting(
            {field.id: value for field, value in changes.items()},
            environ=handler.server.webui_environ,
            rebind=form.get(pages.REBIND_FIELD) or ())
    except config_api.WebUIConfigError as exc:
        field_errors, general = pages.attribute_error(
            str(exc), changes, environ=handler.server.webui_environ)
        errors = dict(field_errors)
        if general:
            errors["_general"] = general
        handler._send(400, handler._config_html(
            field_values, errors=errors, submitted=submitted,
            theme=handler._theme(),
            section=(form.get(pages.SECTION_FIELD) or [None])[0],
            reveal=(form.get(pages.REVEAL_FIELD) or [None])[0]),
                   "text/html; charset=utf-8")
        return
    handler._send(200, pages.render_result_page(
        changed, warnings=warnings,
        theme=handler._theme()), "text/html; charset=utf-8")

FIELD_ID_FIELD = "_field"
CONFIRMED_FIELD = "_confirmed"


def config_field_save(handler, form):
    """POST /config/field: save one setting as soon as it changes (autosave).

    The Settings page posts one field at a time, form-encoded, with the
    page's CSRF token: ``_field`` names it and the field's own inputs carry
    its value, exactly as the whole form would. It goes through the same
    path as the preview and confirm steps: parse against the catalog,
    validate the change through the real settings loader, then write only
    that key to the settings file layer. A change that needs the operator's
    say-so first (a stored service key moving to a new host, the Secure
    cookie over plain HTTP) is answered 409 with the question; the page asks
    it and posts again with ``_confirmed=yes``. Always answers JSON:
    ``{"ok": true, "changed": bool, "saved": [...], "warnings": [...]}`` or
    ``{"ok": false, "error": "..."}``.
    """
    field_id = (form.get(FIELD_ID_FIELD) or [""])[0]
    field_values = handler._field_values()
    current = next((fv for fv in field_values if fv.field.id == field_id), None)
    if current is None:
        handler._json(404, {"ok": False, "error": "That setting does not exist here. Nothing saved."})
        return
    if current.overridden:
        handler._json(409, {"ok": False, "error": "This setting comes from the environment, "
                                                  "so it cannot be changed here. Nothing saved."})
        return
    changes, _submitted, errors = pages.parse_form(form, field_values, only={field_id})
    if errors:
        handler._json(400, {"ok": False, "error": errors.get(field_id) or "That value is not valid."})
        return
    if not changes:
        handler._json(200, {"ok": True, "changed": False, "saved": [], "warnings": []})
        return
    environ = handler.server.webui_environ
    field_errors, general = pages.validate_change_set(changes, environ=environ)
    if field_errors or general:
        handler._json(400, {"ok": False,
                            "error": field_errors.get(field_id) or general
                            or next(iter(field_errors.values()))})
        return
    by_id = {field.id: value for field, value in changes.items()}
    rebinds = config_api.pending_rebinds(by_id, environ=environ)
    questions = handler._preview_warnings(changes) + [
        f"This moves the {move.service} address from {move.old_host} to {move.new_host}. "
        f"Your stored {move.service} key will be sent to {move.new_host}." for move in rebinds]
    confirmed = (form.get(CONFIRMED_FIELD) or [""])[0] == "yes"
    if questions and not confirmed:
        handler._json(409, {"ok": False, "confirm": " ".join(questions) + " Save anyway?"})
        return
    try:
        changed, warnings = config_api.write_changes_reporting(
            by_id, environ=environ, rebind=[move.setting for move in rebinds])
    except config_api.WebUIConfigError as exc:
        field_errors, general = pages.attribute_error(str(exc), changes, environ=environ)
        handler._json(400, {"ok": False,
                            "error": field_errors.get(field_id) or general or str(exc)})
        return
    handler._json(200, {"ok": True, "changed": bool(changed),
                        "saved": [f"{section}.{key}" for section, key in changed],
                        "warnings": [f"{name} is kept in the file but the next restart will "
                                     "refuse it; remove it from the settings file."
                                     for name in (warnings or ())]})


def debug_post(handler, form):
    """POST /debug (#478): turn debug log mode on or off.

    Same ``session+csrf`` policy as a config POST (routes.py). The
    mode is shared state in the state folder, so this one write
    reaches the bot and every listener process; nothing about
    printing changes.
    """
    from .. import debug_mode
    action = (form.get("action") or [""])[0]
    data = handler.server.settings_data
    if action == "on":
        raw = (form.get("minutes") or [""])[0].strip()
        try:
            minutes = int(raw) if raw else None
        except ValueError:
            handler._debug_error("minutes must be a whole number")
            return
        debug_mode.start(data, minutes=minutes)
    elif action == "off":
        debug_mode.end(data)
    else:
        handler._debug_error("action must be on or off")
        return
    handler._redirect("/")

def debug_error(handler, message):
    """A /debug validation refusal (#840): the status page itself
    re-rendered at 400 with the error on it, so the debug form and
    its current state stay on screen instead of a JSON dump."""
    snap = handler.server.snapshot_func()
    handler._send(400, render_status_html(
        snap, form_token=handler._form_token(), theme=handler._theme(),
        walkthrough_pending=walkthrough_module.pending(handler.server.settings_data),
        job=_overall_job(handler.server.settings_data), error=message),
        "text/html; charset=utf-8")

