"""The picker configuration page (#286, stage 3): server-rendered HTML
over the stage 1 catalog and read/write API.

``GET /config`` renders one form built from
:mod:`print_handy.webui.catalog`: every field shows its effective value,
a provenance badge (which layer supplied it) and its help text, with the
control its kind calls for — a ``<select>`` picker wherever a choice set
exists, a checkbox for toggles, a bounded numeric input for numbers, a
text input otherwise. A field the environment overrides renders disabled
with that fact stated; it is never written.

Saving is two steps, and nothing is written by the first:

1. ``POST /config/preview`` parses only catalog fields, converts them by
   kind, diffs them against the effective values, validates the change
   set against the real settings loader (on a throwaway copy of the
   settings file, via :func:`print_handy.webui.config.write_changes`),
   and renders the exact ``old -> new`` list. Conversion and validation
   errors re-render the form with the error against the field — the
   settings validator's own message — and the file stays byte-identical.
2. ``POST /config`` with ``confirm=yes`` (the preview page's form)
   performs the write through the stage 1 write API: validated again,
   atomic, with the timestamped backup. The result page lists the keys
   that changed and states that a restart is required — settings are
   read once at process start, so no change here claims to be live.

POST CSRF/auth (enforced by the server): the ``X-WebUI-Token`` header
(stage 2's rule) or the per-server form token embedded in every form as
``_csrf``. The form token is generated when the server starts, kept only
in memory, and is not the access token; on loopback with no access token
configured it is still required for every POST, so a random web page in
the operator's browser cannot post settings cross-origin.

No secret value is ever rendered: the catalog holds no secret fields,
and the access token never appears in a page (only the form token does).
"""
from __future__ import annotations

import html
import shutil
import tempfile
from pathlib import Path

from . import access as access_module
from . import config as config_api
from .catalog import choice_label, choices_for, config_page_visible, field_by_id

CSRF_FIELD = "_csrf"
CONFIRM_FIELD = "confirm"
# The login form's token field (POST /login). The submitted value is
# compared against the access token by the server and is never rendered
# back into any page, logged, or stored.
LOGIN_TOKEN_FIELD = "token"
# First-run setup form fields (POST /setup, #708). Passwords are hashed by
# webui.access and never rendered back.
SETUP_CHOICE_FIELD = "choice"
SETUP_PASSWORD_FIELD = "password"
SETUP_CONFIRM_FIELD = "password_again"
SETUP_OPEN_FIELD = "confirm_open"
# Hidden field on the confirm form listing the field ids the preview
# parsed; the save POST parses exactly those, no others.
FIELDS_FIELD = "_fields"

# Page groups in render order: (catalog group name, heading). The
# headings follow the docs' grouping of the same settings.
GROUPS = (
    ("printer", "Printer"),
    ("platform", "Platform and chat"),
    ("services", "Services"),
    ("endpoints", "Model endpoints"),
    ("timelapse", "Timelapse"),
    ("images", "Images and camera watch"),
    ("live", "Live camera (Discord)"),
    ("slicing", "Slicing and delivery"),
    ("logging", "Logging"),
    ("webui", "Web UI"),
)

PROVENANCE_LABELS = {
    config_api.MODEL_DEFAULT: "model default",
    config_api.HOME_CONFIG: "home config",
    config_api.SETTINGS_FILE: "settings file",
    config_api.ENVIRONMENT: "environment override",
    config_api.DEFAULT: "default",
}

_TRUTHY = {"true", "on", "1", "yes"}


def _esc(value):
    return html.escape(str(value), quote=True)


def _show(value):
    """A value as the pages display it: lists joined, booleans spelled,
    empty shown as (empty) so a diff never reads as a blank mystery."""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) or "(empty)"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None or value == "":
        return "(empty)"
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


# ---------------------------------------------------------------- rendering

def _current(field, field_value, submitted):
    """The value a control should show: the submitted one when the form is
    being re-rendered after an error, else the effective value."""
    if submitted is None or field.id not in submitted:
        return field_value.value
    raw = submitted[field.id]
    if field.kind == "toggle":
        return any(str(v).strip().lower() in _TRUTHY for v in raw)
    if isinstance(field_value.value, (list, tuple)):
        return list(raw)
    return raw[0] if raw else ""


def _ordered_choices(field, choices):
    """Picker options are A–Z by the text on screen. The stored value is unchanged."""
    return sorted(choices, key=lambda choice: (choice_label(field, choice).casefold(), choice))


def _control(field, value, *, data, disabled):
    name = _esc(field.id)
    dis = " disabled" if disabled else ""
    if field.kind == "toggle":
        checked = " checked" if value else ""
        return (f'<input type="checkbox" id="f-{name}" name="{name}" '
                f'value="true"{checked}{dis}>')
    if field.kind == "picker":
        choices = _ordered_choices(field, [str(choice) for choice in choices_for(field, data=data)])
        if isinstance(value, (list, tuple)):
            selected = {str(item) for item in value}
            options = "".join(
                f'<option value="{_esc(choice)}"{" selected" if choice in selected else ""}>'
                f'{_esc(choice_label(field, choice))}</option>' for choice in choices)
            size = min(max(len(choices), 2), 8)
            return (f'<select id="f-{name}" name="{name}" multiple size="{size}"{dis}>'
                    f'{options}</select>')
        current = "" if value is None else str(value)
        if current and current not in choices:
            choices = [current, *choices]  # show the real value, never a silent substitute
        choices = _ordered_choices(field, choices)
        options = "".join(
            f'<option value="{_esc(choice)}"{" selected" if choice == current else ""}>'
            f'{_esc(choice_label(field, choice))}</option>' for choice in choices)
        return f'<select id="f-{name}" name="{name}"{dis}>{options}</select>'
    if field.kind == "number":
        attrs = ""
        if field.minimum is not None:
            attrs += f' min="{field.minimum:g}"'
        if field.maximum is not None:
            attrs += f' max="{field.maximum:g}"'
        text = "" if value is None or value == "" else _show(value)
        return (f'<input type="number" id="f-{name}" name="{name}" '
                f'value="{_esc(text)}" step="any"{attrs}{dis}>')
    return (f'<input type="text" id="f-{name}" name="{name}" '
            f'value="{_esc("" if value is None else value)}"{dis}>')


def _form_value(value):
    """A value as parse_form reads it back: raw, never the display text
    (_show's "(empty)" would come back as a real value and fail to save)."""
    if value is None or value is False:
        return ""
    if value is True:
        return "true"
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def _hidden_inputs(field, value):
    """A field the page does not show for this model, kept in the form so a
    save does not clear it: one input per list item, raw values otherwise."""
    name = _esc(field.id)
    items = value if isinstance(value, (list, tuple)) else [value]
    return "".join(f'<input type="hidden" name="{name}" value="{_esc(_form_value(item))}">'
                   for item in items)


def _field_row(field_value, *, data, errors, submitted):
    field = field_value.field
    if not config_page_visible(field, data):
        return _hidden_inputs(field, _current(field, field_value, submitted))
    disabled = field_value.env_overridden
    badge = PROVENANCE_LABELS.get(field_value.provenance, field_value.provenance)
    parts = [f'<div class="field" id="row-{_esc(field.id)}">',
             f'<label for="f-{_esc(field.id)}">{_esc(field.label)} '
             f'<span class="badge">{_esc(badge)}</span></label> ',
             _control(field, _current(field, field_value, submitted),
                      data=data, disabled=disabled)]
    error = (errors or {}).get(field.id)
    if error:
        parts.append(f'<p class="error">{_esc(error)}</p>')
    if disabled:
        parts.append('<p class="note">Overridden by the environment '
                     f'(PRINT_HANDY__{field.section.upper()}__{field.key.upper()} or a '
                     'legacy name): change or unset that variable instead — this field '
                     'is not written to the settings file.</p>')
    if field.help:
        parts.append(f'<p class="help">{_esc(field.help)}</p>')
    parts.append('</div>')
    return "".join(parts)


def _page(title, body):
    return ("<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\">"
            f"<title>Print Handy — {_esc(title)}</title></head><body>"
            f"<h1>Print Handy {_esc(title)}</h1>{body}</body></html>\n")


def render_login_page(error=None, password=False):
    """The GET /login page: a single password field for the access token.

    A browser cannot send the ``X-WebUI-Token`` header, so this form is
    how a person signs in: a correct POST /login earns a session cookie
    (see server.py). ``error`` is a fixed message chosen by the server;
    the submitted token is never reflected here. ``password`` is true
    when the credential is the password chosen on the setup page (#708).
    """
    if password:
        intro = ('<p>This Web UI is protected by the password chosen during setup. '
                 'Enter it once and this browser stays signed in until the session '
                 'expires or the server restarts.</p>')
    else:
        intro = ('<p>This Web UI is protected by an access token (the value of the '
                 'environment variable the <code>[webui] access_token_env</code> '
                 'setting names). Enter it once and this browser stays signed in '
                 'until the session expires or the server restarts.</p>')
    parts = [intro]
    if error:
        parts.append(f'<p class="error">{_esc(error)}</p>')
    label = "Password" if password else "Access token"
    parts.append(
        f'<form method="post" action="/login">'
        f'<p><label for="f-{LOGIN_TOKEN_FIELD}">{label}</label> '
        f'<input type="password" id="f-{LOGIN_TOKEN_FIELD}" '
        f'name="{LOGIN_TOKEN_FIELD}" autocomplete="current-password" required></p>'
        '<p><button type="submit">Log in</button></p></form>')
    return _page("log in", "".join(parts))


def render_setup_page(form_token, error=None):
    """The GET /setup page (#708): set a password, or affirmatively leave it open.

    Shown only while no access rule exists for a LAN bind. Submitted
    passwords are hashed by the server and never rendered back.
    """
    parts = [
        '<p>Welcome. Before this Web UI shows anything, choose how it is protected. '
        'Anyone who can reach this page on your network can complete this step, '
        'so do it now.</p>',
    ]
    if error:
        parts.append(f'<p class="error">{_esc(error)}</p>')
    csrf = f'<input type="hidden" name="{CSRF_FIELD}" value="{_esc(form_token)}">'
    parts.append(
        '<h2>Set a password (recommended)</h2>'
        '<form method="post" action="/setup">' + csrf +
        f'<input type="hidden" name="{SETUP_CHOICE_FIELD}" value="password">'
        f'<p><label for="f-{SETUP_PASSWORD_FIELD}">Password</label> '
        f'<input type="password" id="f-{SETUP_PASSWORD_FIELD}" name="{SETUP_PASSWORD_FIELD}" '
        f'autocomplete="new-password" minlength="{access_module.MIN_PASSWORD_LENGTH}" required></p>'
        f'<p><label for="f-{SETUP_CONFIRM_FIELD}">Password again</label> '
        f'<input type="password" id="f-{SETUP_CONFIRM_FIELD}" name="{SETUP_CONFIRM_FIELD}" '
        f'autocomplete="new-password" required></p>'
        '<p>Home Assistant and other tools send this same password as the '
        f'<code>X-WebUI-Token</code> header.</p>'
        '<p><button type="submit">Save password</button></p></form>')
    parts.append(
        '<h2>Or leave it open</h2>'
        '<form method="post" action="/setup">' + csrf +
        f'<input type="hidden" name="{SETUP_CHOICE_FIELD}" value="open">'
        f'<p><input type="checkbox" id="f-{SETUP_OPEN_FIELD}" name="{SETUP_OPEN_FIELD}" '
        f'value="yes" required> <label for="f-{SETUP_OPEN_FIELD}">I trust everyone on my '
        'network: anyone who can reach this port can use the Web UI.</label></p>'
        '<p><button type="submit">Leave it open</button></p></form>')
    parts.append(
        '<p>To change this later, delete <code>webui_access.json</code> from the Print Handy '
        'state folder and restart, or set an access token in the environment.</p>')
    return _page("setup", "".join(parts))


def render_config_page(field_values, *, data, form_token, errors=None,
                       submitted=None, notice=None):
    """The GET /config form (also the error/notice re-render target)."""
    by_group = {}
    for field_value in field_values:
        by_group.setdefault(field_value.field.group, []).append(field_value)
    parts = [
        '<p><a href="/">Status</a></p>',
        '<p>These settings live in the YAML settings file; this page is an '
        'editor for that file, not a second source of truth. Saving writes '
        'only the settings file layer, after validation by the real '
        'settings loader. Comments in a hand-edited file are not '
        'guaranteed to survive a save. <strong>Every change needs a '
        'restart</strong> of Print Handy (or the container) to take '
        'effect: settings are read once at process start.</p>',
    ]
    if notice:
        parts.append(f'<p class="notice">{_esc(notice)}</p>')
    general = (errors or {}).get("_general")
    if general:
        parts.append(f'<p class="error">{_esc(general)}</p>')
    parts.append(f'<form method="post" action="/config/preview">'
                 f'<input type="hidden" name="{CSRF_FIELD}" value="{_esc(form_token)}">')
    known = {name for name, _heading in GROUPS}
    for name, heading in GROUPS:
        rows = by_group.get(name)
        if not rows:
            continue
        parts.append(f"<h2>{_esc(heading)}</h2>")
        parts.extend(_field_row(fv, data=data, errors=errors, submitted=submitted)
                     for fv in rows)
    for name, rows in by_group.items():  # a group this page does not know yet
        if name in known:
            continue
        parts.append(f"<h2>{_esc(name or 'Other')}</h2>")
        parts.extend(_field_row(fv, data=data, errors=errors, submitted=submitted)
                     for fv in rows)
    parts.append('<p><button type="submit">Preview changes</button></p></form>')
    return _page("configuration", "".join(parts))


def render_preview_page(entries, submitted, *, form_token):
    """The diff preview: exact (field, old, new) rows plus the confirm form.

    ``entries`` is [(field, old, new)]; ``submitted`` carries the raw form
    values the confirm form reposts as hidden inputs, so what is confirmed
    is byte-for-byte what was previewed.
    """
    rows = "".join(
        f"<tr><th>{_esc(field.label)} ({_esc(field.id)})</th>"
        f"<td>{_esc(_show(old))}</td><td>{_esc(_show(new))}</td></tr>"
        for field, old, new in entries)
    hidden = [f'<input type="hidden" name="{CSRF_FIELD}" value="{_esc(form_token)}">',
              f'<input type="hidden" name="{CONFIRM_FIELD}" value="yes">',
              f'<input type="hidden" name="{FIELDS_FIELD}" '
              f'value="{_esc(",".join((submitted or {}).keys()))}">']
    # The whole parsed form state rides along, not just the changed keys:
    # the confirm POST parses exactly the fields the preview parsed (the
    # FIELDS_FIELD list), so an absent toggle or multi-select there means
    # "submitted empty", never "not part of this save". A toggle submitted
    # unchecked has no raw value; its hidden input spells the false out.
    # An emptied multi-select emits no hidden input at all — its presence
    # in FIELDS_FIELD is what carries the empty list.
    for field_id, raws in (submitted or {}).items():
        if not raws:
            field = field_by_id(field_id)
            if field is not None and field.kind == "toggle":
                hidden.append(f'<input type="hidden" name="{_esc(field_id)}" '
                              'value="false">')
            continue
        for raw in raws:
            hidden.append(f'<input type="hidden" name="{_esc(field_id)}" '
                          f'value="{_esc(raw)}">')
    return _page("review changes", "".join([
        '<p>Nothing has been written yet. Saving writes only these keys to '
        'the settings file (a timestamped backup of the current file is '
        'kept), and every change needs a restart to take effect.</p>',
        f"<table><tr><th>Setting</th><th>Current</th><th>New</th></tr>{rows}</table>",
        '<form method="post" action="/config">', "".join(hidden),
        '<p><button type="submit">Save changes</button> '
        '<a href="/config">Back to the form</a></p></form>',
    ]))


def render_result_page(changed):
    """The post-save page: the keys write_changes actually changed."""
    if changed:
        listing = "".join(f"<li>{_esc(section)}.{_esc(key)}</li>"
                          for section, key in changed)
        body = (f"<p>Saved to the settings file:</p><ul>{listing}</ul>"
                "<p><strong>Restart required:</strong> settings are read once "
                "at process start, so restart Print Handy (or the container) "
                "for these changes to take effect. Nothing was applied "
                "live.</p>")
    else:
        body = "<p>Nothing changed: the settings file already held these values.</p>"
    return _page("configuration saved", body +
                 '<p><a href="/config">Back to configuration</a> · '
                 '<a href="/">Status</a></p>')


# ------------------------------------------------------------------ parsing

def _parse_number(field, text):
    if not text:
        return None, f"{field.label}: enter a number."
    try:
        value = int(text)
    except ValueError:
        try:
            value = float(text)
        except ValueError:
            return None, f"{field.label}: {text!r} is not a number."
    if isinstance(value, float) and value != value:  # NaN parses as a float
        return None, f"{field.label}: {text!r} is not a number."
    if field.minimum is not None and value < field.minimum:
        return None, f"{field.label}: must be at least {field.minimum:g}."
    if field.maximum is not None and value > field.maximum:
        return None, f"{field.label}: must be at most {field.maximum:g}."
    return value, None


def parse_form(form, field_values, *, only=None):
    """Parse a submitted form against the catalog.

    ``form`` is a parse_qs mapping {name: [values]}. Only catalog fields
    are read — any other key is ignored, never written — and fields the
    environment overrides are skipped entirely. ``only`` restricts the
    parse to a set of field ids: the confirm POST passes the preview's
    FIELDS_FIELD list, so it re-parses exactly the state the preview
    showed (where an absent toggle or multi-select means "submitted
    empty"). Returns ``(changes, submitted, errors)``: the
    {Field: converted value} diff against the effective values, the raw
    values per field id (for re-rendering and for the preview's hidden
    inputs), and {field_id: message} conversion errors.
    """
    changes, submitted, errors = {}, {}, {}
    for field_value in field_values:
        field = field_value.field
        if field_value.env_overridden:
            continue
        if only is not None and field.id not in only:
            continue
        raw = form.get(field.id)
        submitted[field.id] = list(raw) if raw is not None else []
        if field.kind == "toggle":
            value = bool(raw) and any(str(v).strip().lower() in _TRUTHY for v in raw)
        elif isinstance(field_value.value, (list, tuple)):
            value = [str(v).strip() for v in raw or [] if str(v).strip()]
        elif field.kind == "number":
            value, error = _parse_number(field, (raw[0] if raw else "").strip())
            if error:
                errors[field.id] = error
                continue
        else:
            value = (raw[0] if raw else "").strip()
        # An optional key absent from every layer (None) submitted empty is not a change.
        if value != field_value.value and not (value == "" and field_value.value is None):
            changes[field] = value
    return changes, submitted, errors


# --------------------------------------------------------------- validation

def _try_write_copy(changes, environ):
    """Validate a change set by running the real write API against a
    throwaway copy of the settings file. Returns the refusal message, or
    None when the set validates. The real file is never touched."""
    target = config_api.settings_file_path(environ)
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / target.name
        if target.is_file():
            shutil.copyfile(target, copy)
        try:
            config_api.write_changes(
                {field.id: value for field, value in changes.items()},
                config_path=copy, environ=environ)
        except config_api.WebUIConfigError as exc:
            return str(exc)
    return None


def _field_errors(changes, environ):
    """{field_id: message} for the fields that fail validation on their own,
    each with the settings loader's own message for that single change."""
    errors = {}
    for field, value in changes.items():
        message = _try_write_copy({field: value}, environ)
        if message is not None:
            errors[field.id] = message
    return errors


def validate_change_set(changes, *, environ=None):
    """(field_errors, general_error) for a parsed change set, validated
    through the real settings loader without writing anything."""
    if not changes:
        return {}, None
    message = _try_write_copy(changes, environ)
    if message is None:
        return {}, None
    field_errors = _field_errors(changes, environ)
    if field_errors:
        return field_errors, None
    return {}, message


def attribute_error(message, changes, *, environ=None):
    """Split a write refusal into (field_errors, general_error): blame the
    fields that fail on their own; anything else stays a general error
    carrying the write API's message."""
    field_errors = _field_errors(changes, environ) if changes else {}
    if field_errors:
        return field_errors, None
    return {}, message


__all__ = [
    "CONFIRM_FIELD", "CSRF_FIELD", "FIELDS_FIELD", "GROUPS",
    "LOGIN_TOKEN_FIELD", "PROVENANCE_LABELS", "attribute_error", "parse_form",
    "render_config_page", "render_login_page", "render_preview_page",
    "render_result_page", "validate_change_set",
]
