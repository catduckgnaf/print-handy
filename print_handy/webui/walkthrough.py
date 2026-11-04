"""The first-run walkthrough (#813): /walkthrough.

The access gate (#708) decides *whether* this Web UI may be used; this
walkthrough is what a brand-new install does next — a real five-step
walk through the settings a first print needs, in the prototype's
order: Printer, Chat, Camera, Providers, Review. It was the missing
half of the first run: before #813 an install that had just chosen its
access landed on a bare status page and had to find the raw
configuration form on its own.

Who sees it: installs that resolved access through the setup page —
i.e. ``webui_access.json`` exists in the state folder — and have not
finished the walkthrough. Installs authorized any other way (an
environment token, a loopback bind, ``require_auth = false``) never see
it: they predate or bypass the setup page, and the walkthrough must not
spring on existing installs. While it is pending, browser navigations
to the status page redirect here (the same discipline as the setup
gate), every other route stays reachable, and the status page carries
a slim "finish setting up" banner until it is done. The access gate
still runs first: /setup until the access choice exists.

State is one small JSON file beside the access file,
``webui_walkthrough.json`` — ``{"step": <index>, "done": bool,
"skipped": bool}`` — written atomically at mode 0600 through the setup
wizard's writer. It records progress only; no setting lives in it.

Every step writes through the existing configuration path: the step's
fields are catalog fields, rendered by the config page's own field-row
renderer (provenance badges included; environment- and user-file-
overridden fields render disabled exactly as on /config) and POSTed
through ``pages.parse_form`` + ``config.write_changes``. The Providers
step writes nothing — site tokens live in files and the environment,
and the bot asks for one in chat the first time a link needs it — so
the step is a read-only list of which sites are connected. The Review
step is likewise read-only: the values currently in effect for the
fields the walkthrough covers.

The printer's NAME is editable on the Printer step wherever the
settings model genuinely allows it (#813, second pass). A legacy
install edits ``[printer] name`` — a catalog field since this pass, so
/config gained it too. An install with a declared ``[[printers]]``
roster edits the default entry's ``name`` key in place through
``config.write_printer_name``: one key of one entry, the ``id``
untouched (renaming is a display-name change only), and never a legacy
section alongside the roster — the loader refuses that mixture, so the
catalog field is not even parsed on a roster install. A name owned by
a layer the Web UI does not write — an indexed environment override,
the operator's own settings file, the home configuration — or living
in a TOML settings file renders disabled or read-only with the reason,
the same provenance discipline as every other field.
"""
from __future__ import annotations

import json
from pathlib import Path

from .. import settings
from ..core import site_accounts
from ..setup_wizard import _atomic_write
from . import access as access_module
from . import config as config_api
from . import pages
from . import shell
from .catalog import config_page_visible

STATE_FILE = "webui_walkthrough.json"

STEPS = (
    ("printer", "Printer"),
    ("chat", "Chat"),
    ("camera", "Camera"),
    ("login", "Login"),
    ("providers", "Providers"),
    ("review", "Review"),
)

STEP_LEADS = {
    "printer": "The printer Print Handy prepares prints for, and how it reaches the "
               "printer on the network. Everything here can be changed again later "
               "in Configuration.",
    "chat": "Where Print Handy talks. Drop a model link in the chat and the bot "
            "answers there, asks which design, slices it, and prints only after a "
            "confirmation. Only the chosen platform's fields are shown.",
    "camera": "The camera that watches the printer, and whether finished prints "
              "are recorded as timelapses.",
    "login": "Who may open this Web UI. Choose a username and password your "
             "browser can remember, or leave it open on a network you trust. "
             "Leave the fields empty to keep things as they are.",
    "providers": "Nothing to fill in here. The first time someone drops a link the "
                 "bot cannot download, it asks for that site's token in chat and "
                 "saves it. Printables needs no account.",
    "review": "What is in effect now for the settings the walkthrough covers. "
              "Each step already saved as you went; Finish just marks setup done "
              "and takes you to the status page. Like every Web UI save, changes "
              "need a restart of Print Handy to take effect.",
}

# The catalog fields each step owns, in display order. Fields a step
# does not list are never parsed from its POST, so a step cannot write
# outside its own surface; visibility still follows the catalog rules
# (one printer URL per connection family, the ZMod format only for
# Flashforge), exactly as on /config.
STEP_FIELDS = {
    # Every family's connection field is here; config_page_visible shows
    # exactly the one the configured model's connection uses (plus the
    # Bambu serial its validator pairs with the host), so a Bambu LAN,
    # PrusaLink, stock Flashforge or Elegoo SDCP install can configure
    # its connection on this step just like a Moonraker or OctoPrint one.
    "printer": ("printer.name", "printer.model", "services.moonraker_url",
                "services.octoprint_url", "bambu.lan_host", "bambu.lan_serial",
                "prusa.link_host", "flashforge_stock.host", "elegoo_sdcp.host",
                "printer.zmod_delivery_format"),
    "chat": ("bot.platform", "bot.mode", "discord.chat_id", "discord.allowed_user_ids",
             "telegram.chat_id", "telegram.allowed_user_ids", "matrix.homeserver_url",
             "matrix.room_id", "matrix.allowed_user_ids"),
    "camera": ("services.camera_source", "services.camera_snapshot_url",
               "timelapse.mode", "timelapse.capture_enabled"),
    "login": (),
    "providers": (),
    "review": (),
}

REVIEW_FIELDS = STEP_FIELDS["printer"] + STEP_FIELDS["chat"] + STEP_FIELDS["camera"]


def _esc(value):
    return pages._esc(value)


# ------------------------------------------------------------------- state

def state_path(data):
    """The walkthrough state file: beside the access file in the state folder."""
    return Path(settings.state_dir(data)) / STATE_FILE


def load_state(data):
    """The saved walkthrough state, {} when absent or unreadable."""
    try:
        state = json.loads(state_path(data).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def save_state(data, state):
    path = state_path(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(state, indent=1) + "\n", 0o600)


def pending(data):
    """True while this install owes the walkthrough: access was resolved
    through the setup page (the access file exists) and the walkthrough
    is not done. Any other install never sees it."""
    try:
        if not access_module.access_path(data).is_file():
            return False
    except Exception:  # settings too broken to locate the state folder
        return False
    return not load_state(data).get("done")


def _step_index(state):
    try:
        index = int(state.get("step") or 0)
    except (TypeError, ValueError):
        index = 0
    return min(max(index, 0), len(STEPS) - 1)


def _step_values(field_values, step_id):
    """The step's FieldValues, in the step's own order."""
    by_id = {field_value.field.id: field_value for field_value in field_values}
    return [by_id[field_id] for field_id in STEP_FIELDS[step_id] if field_id in by_id]


# --------------------------------------------------------------- rendering

def _pills(current):
    items = []
    for index, (_step_id, label) in enumerate(STEPS):
        cls = "done" if index < current else "current" if index == current else ""
        items.append(f'<li class="{cls}">{index + 1}. {_esc(label)}</li>')
    return '<ol class="steps">' + "".join(items) + "</ol>"


def _name_block(data, environ, submitted=None):
    """The Printer step's name control, shaped by where the name lives.

    Legacy installs get nothing here — ``printer.name`` is a catalog
    field and renders as a normal field row. Every other shape is a
    roster install: the name is the default entry's ``name`` key, edited
    in place when the settings file declares the roster, and disabled
    or read-only with the reason when another layer owns it. A state
    that cannot even be determined falls back to showing the name,
    which is the worst case the first pass shipped.
    """
    try:
        state = config_api.printer_name_state(data, environ=environ)
    except Exception:  # a name we cannot place is shown, never a failed page
        state = None
    if state is None or state.shape == "legacy":
        return ""
    resolved = str((data.get("printer") or {}).get("name") or "").strip()
    if state.shape == "roster":
        posted = (submitted or {}).get("printer_name")
        value = posted[0] if posted else state.name
        badge = pages.PROVENANCE_LABELS[config_api.SETTINGS_FILE]
        return ('<div class="field"><label for="f-printer_name">Printer name '
                f'<span class="badge">{_esc(badge)}</span></label> '
                f'<input type="text" id="f-printer_name" name="printer_name" '
                f'value="{_esc(value)}" placeholder="{_esc(resolved)}">'
                '<p class="help">The name lives in this printer\'s entry in the '
                "settings file's printer roster. Renaming changes the display "
                f"name only — the printer's id ({_esc(state.printer_id)}) does "
                "not change. Leave it blank to use the model's own name.</p></div>")
    if state.shape == "environment":
        badge = pages.PROVENANCE_LABELS[config_api.ENVIRONMENT]
        return ('<div class="field"><label for="f-printer_name">Printer name '
                f'<span class="badge">{_esc(badge)}</span></label> '
                f'<input type="text" id="f-printer_name" value="{_esc(resolved)}" disabled>'
                '<p class="note">Overridden by the environment '
                f'(${_esc(state.env_var)}): change or unset that variable '
                "instead — this field is not written to the settings file.</p></div>")
    if state.shape == "user_file":
        badge = pages.PROVENANCE_LABELS[config_api.USER_FILE]
        return ('<div class="field"><label for="f-printer_name">Printer name '
                f'<span class="badge">{_esc(badge)}</span></label> '
                f'<input type="text" id="f-printer_name" value="{_esc(resolved)}" disabled>'
                '<p class="note">The printer roster is declared in your own '
                "settings file, which overrides the settings file this page "
                "writes; change the name there.</p></div>")
    if state.shape == "roster_toml":
        note = ("The name lives in the printer roster in the settings file, but "
                "that file is TOML and the Web UI writes YAML settings files. "
                "To change it, edit the roster in the settings file or run the "
                "setup wizard.")
    else:  # "home"
        note = ("The name comes from the printer roster in the home "
                "configuration, which the Web UI does not write. To change it, "
                "edit the roster there or run the setup wizard.")
    shown = _esc(resolved) if resolved else "(the model's name is used)"
    return ('<div class="field"><span class="flabel">Printer name</span>'
            f"<p>{shown}</p>"
            f'<p class="help">{_esc(note)}</p></div>')


def _provider_rows(environ):
    rows = []
    for site in site_accounts.LOGIN_SITES.values():
        try:
            value, _where = site_accounts.stored(site.site, environ=environ)
        except Exception:  # an unreadable token store reads as not connected
            value = None
        badge = ('<span class="badge ok">connected</span>' if value
                 else '<span class="badge">not connected</span>')
        rows.append(f"<tr><th>{_esc(site.label)}</th><td>{badge}</td></tr>")
    rows.append('<tr><th>Printables</th><td><span class="badge ok">no account '
                "needed</span></td></tr>")
    return "".join(rows)


def _providers_html(environ):
    return ("<table><tr><th>Model site</th><th>State</th></tr>"
            + _provider_rows(environ) + "</table>"
            '<p class="note">Tokens live in files on this machine or in environment '
            "variables, never in the settings file — that is why there is nothing "
            "to type here. The bot asks in chat when one is first needed.</p>")


def _connected_count(environ):
    connected = 0
    for site in site_accounts.LOGIN_SITES.values():
        try:
            value, _where = site_accounts.stored(site.site, environ=environ)
        except Exception:
            value = None
        connected += bool(value)
    return connected, len(site_accounts.LOGIN_SITES)


def _review_html(field_values, data, environ):
    by_id = {field_value.field.id: field_value for field_value in field_values}
    rows = []
    for field_id in REVIEW_FIELDS:
        field_value = by_id.get(field_id)
        if field_value is None or not config_page_visible(field_value.field, data):
            continue
        rows.append(f"<tr><th>{_esc(field_value.field.label)}</th>"
                    f"<td>{_esc(pages._show(field_value.value))}</td></tr>")
    connected, total = _connected_count(environ)
    rows.append(f"<tr><th>Model sites</th><td>{connected} of {total} connected; "
                "Printables needs no account</td></tr>")
    return '<table class="rows">' + "".join(rows) + "</table>"


def _login_html(data, environ, submitted_username=""):
    """The Login step (#875): the current rule and a form to change it."""
    try:
        current = access_module.resolve(data, "0.0.0.0", environ)
    except Exception:  # settings too broken to resolve: show the form only
        current = None
    mode = getattr(current, "mode", None)
    if not data["webui"].get("require_auth", True):
        status = ("Login is turned off by <code>[webui] require_auth = false</code>: "
                  "anyone on the network can use this Web UI. A login saved here "
                  "takes effect only once that setting is back on.")
    elif mode == access_module.PASSWORD and current.username:
        status = f"Signed in with the username <strong>{_esc(current.username)}</strong>."
    elif mode == access_module.PASSWORD:
        status = ("A password protects this Web UI, with no username yet. Add one "
                  "below so your browser can save the pair.")
    elif mode == access_module.TOKEN:
        status = ("An access token from the environment protects this Web UI. "
                  "A username and password saved here is easier to type; the token "
                  "keeps working for Home Assistant and other tools.")
    else:
        status = "This Web UI is open: anyone on your network can use it."
    parts = [f'<p>{status}</p>', pages.login_rows(submitted_username)]
    if mode != access_module.OPEN:
        parts.append(
            f'<p><input type="checkbox" id="f-{pages.SETUP_OPEN_FIELD}" '
            f'name="{pages.SETUP_OPEN_FIELD}" value="yes"> '
            f'<label for="f-{pages.SETUP_OPEN_FIELD}">Remove the login: I trust everyone '
            'on my network.</label></p>')
    parts.append('<p class="muted">To skip logins for good on a home network, set '
                 '<code>[webui] require_auth = false</code> in Configuration.</p>')
    return "".join(parts)


def _apply_login(data, form):
    """Save the Login step. Returns None when nothing changed, "saved"
    when a new login or the open choice was written; raises AccessError."""
    def field(name):
        return (form.get(name) or [""])[0]
    username, password, confirm = (field(pages.SETUP_USER_FIELD),
                                   field(pages.SETUP_PASSWORD_FIELD),
                                   field(pages.SETUP_CONFIRM_FIELD))
    if field(pages.SETUP_OPEN_FIELD) == "yes":
        if username or password:
            raise access_module.AccessError(
                "Either choose a new login or remove it, not both.")
        access_module.save_open(data, True)
        return "saved"
    if not (username.strip() or password or confirm):
        return None
    access_module.save_password(data, password, confirm, username=username)
    return "saved"


def _actions(index, step_id):
    parts = ['<div class="step-actions">']
    if index > 0:
        parts.append('<button type="submit" name="action" value="back" '
                     'class="secondary">Back</button>')
    if step_id != "review":
        parts.append('<button type="submit" name="action" value="skip_setup" '
                     'class="secondary">Skip setup</button>')
    parts.append('<span class="spacer"></span>')
    if step_id == "review":
        parts.append('<button type="submit" name="action" value="finish" '
                     'class="accent">Finish setup</button>')
    else:
        if STEP_FIELDS[step_id]:
            parts.append('<button type="submit" name="action" value="skip" '
                         'class="secondary">Skip this step</button>')
        parts.append('<button type="submit" name="action" value="continue" '
                     'class="accent">Continue</button>')
    parts.append("</div>")
    return "".join(parts)


def render_page(data, field_values, *, form_token, theme=shell.DEFAULT_THEME,
                environ=None, errors=None, submitted=None, general_error=None,
                step_index=None):
    """The walkthrough page for the saved (or given) step."""
    index = _step_index(load_state(data)) if step_index is None else step_index
    step_id, step_label = STEPS[index]
    parts = [_pills(index)]
    if general_error:
        parts.append(f'<p class="error">{_esc(general_error)}</p>')
    parts.append(f'<p class="muted">{_esc(STEP_LEADS[step_id])}</p>')
    parts.append('<form method="post" action="/walkthrough">'
                 f'<input type="hidden" name="{pages.CSRF_FIELD}" '
                 f'value="{_esc(form_token)}">'
                 f'<input type="hidden" name="step" value="{_esc(step_id)}">')
    name_block = _name_block(data, environ, submitted) if step_id == "printer" else ""
    if name_block:
        parts.append(name_block)
    if STEP_FIELDS[step_id]:
        values = _step_values(field_values, step_id)
        if name_block:
            # A roster install: printer.name must not render as a catalog
            # row — its name is the block above, and writing the legacy
            # key beside a declared roster is the refused mixture.
            values = [value for value in values if value.field.id != "printer.name"]
        for field_value in values:
            parts.append(pages._field_row(field_value, data=data, errors=errors,
                                          submitted=submitted))
    elif step_id == "login":
        parts.append(_login_html(data, environ, (submitted or {}).get("username", "")))
    elif step_id == "providers":
        parts.append(_providers_html(environ))
    elif step_id == "review":
        parts.append(_review_html(field_values, data, environ))
    parts.append(_actions(index, step_id))
    parts.append("</form>")
    return shell.page("walkthrough", "".join(parts), path="/walkthrough",
                      theme=theme, heading=step_label, eyebrow="First boot")


# -------------------------------------------------------------------- POST

def apply_post(data, environ, form, field_values, *, form_token,
               theme=shell.DEFAULT_THEME):
    """Handle one walkthrough POST.

    Returns ``("redirect", location)``, ``("page", html)`` (the step
    re-rendered with its errors) or ``("access", location)`` when the Login
    step saved a new access rule: the server re-resolves access and signs
    this browser in before redirecting. The caller has already authorized the
    POST and checked the form token, exactly as for /config.
    """
    state = load_state(data)
    posted = (form.get("step") or [""])[0]
    index = next((i for i, (step_id, _label) in enumerate(STEPS) if step_id == posted),
                 _step_index(state))
    step_id = STEPS[index][0]
    action = (form.get("action") or ["continue"])[0]
    if action == "skip_setup":
        save_state(data, {**state, "step": index, "done": True, "skipped": True})
        return "redirect", "/"
    if action == "back":
        save_state(data, {**state, "step": max(0, index - 1)})
        return "redirect", "/walkthrough"
    if action == "skip":
        save_state(data, {**state, "step": min(index + 1, len(STEPS) - 1)})
        return "redirect", "/walkthrough"
    if action == "finish" or step_id == "review":
        save_state(data, {**state, "step": index, "done": True})
        return "redirect", "/"
    if step_id == "login":
        try:
            outcome = _apply_login(data, form)
        except access_module.AccessError as exc:
            username = (form.get(pages.SETUP_USER_FIELD) or [""])[0]
            return "page", render_page(
                data, field_values, form_token=form_token, theme=theme, environ=environ,
                submitted={"username": username}, general_error=str(exc),
                step_index=index)
        save_state(data, {**state, "step": index + 1})
        return ("access" if outcome else "redirect"), "/walkthrough"
    if step_id == "providers":  # Continue: nothing to write on this step
        save_state(data, {**state, "step": index + 1})
        return "redirect", "/walkthrough"
    # The Printer step's name: on a roster install the catalog's
    # printer.name is never parsed (writing the legacy key beside a
    # declared roster is the refused mixture); the entry name travels as
    # the form's printer_name and is written by its own helper. Only a
    # positively legacy shape parses the catalog field.
    name_state = None
    if step_id == "printer":
        try:
            name_state = config_api.printer_name_state(data, environ=environ)
        except Exception:  # an unplaceable name is never written from here
            name_state = config_api.PrinterNameState("unknown", "", "")
    values = _step_values(field_values, step_id)
    if name_state is not None and name_state.shape != "legacy":
        values = [value for value in values if value.field.id != "printer.name"]
    changes, submitted, errors = pages.parse_form(form, values)
    general = None
    if not errors and changes:
        field_errors, general = pages.validate_change_set(changes, environ=environ)
        errors.update(field_errors)
    name_change = None
    if not errors and not general and name_state is not None \
            and name_state.shape == "roster":
        posted_name = (form.get("printer_name") or [""])[0]
        if posted_name.strip() != name_state.name:
            try:  # pre-flight: the step must not half-save
                config_api.check_printer_name(name_state.printer_id, posted_name,
                                              environ=environ)
                name_change = posted_name
            except config_api.WebUIConfigError as exc:
                general = str(exc)
    if not errors and not general and changes:
        try:
            config_api.write_changes(
                {field.id: value for field, value in changes.items()}, environ=environ)
        except config_api.WebUIConfigError as exc:
            errors, general = pages.attribute_error(str(exc), changes, environ=environ)
    if not errors and not general and name_change is not None:
        try:
            config_api.write_printer_name(name_state.printer_id, name_change,
                                          environ=environ)
        except config_api.WebUIConfigError as exc:
            general = str(exc)
    if errors or general:
        if step_id == "printer" and form.get("printer_name"):
            submitted = {**submitted, "printer_name": form["printer_name"]}
        return "page", render_page(
            data, field_values, form_token=form_token, theme=theme, environ=environ,
            errors=errors, submitted=submitted, general_error=general,
            step_index=index)
    save_state(data, {**state, "step": min(index + 1, len(STEPS) - 1)})
    return "redirect", "/walkthrough"


__all__ = [
    "REVIEW_FIELDS", "STATE_FILE", "STEPS", "STEP_FIELDS", "STEP_LEADS",
    "apply_post", "load_state", "pending", "render_page", "save_state",
    "state_path",
]
