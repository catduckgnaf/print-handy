"""The Web UI Printer settings tab (#1448): one printer's settings, in plain language.

The Settings page (/config) edits the whole install through the field
catalog. This tab is the friendly surface for one printer: pick the
printer, then its name, model, chat channel, connection address,
slicer, delivery methods and (on Flashforge/ZMod printers) the ZMod
delivery format, each with its effective value and the layer that
supplied it (model default, home config, settings file, your settings
file, environment override).

Nothing here is a second store. A legacy install (no declared
[[printers]] roster) edits the catalog fields through the same
read/write machinery /config uses (``config.read_fields`` /
``config.write_changes``, validated by the real settings loader). A
roster install edits the printer's declared entry in place through
``config.write_printer_entry`` (the write_printer_name discipline,
generalized), plus the install-wide [delivery] methods and, for the
default printer, the [printer] slicer and ZMod format, which stay
catalog fields. Environment-overridden values are locked, with the
variable named; a stored service key never follows a changed address
to a new host without the same explicit confirmation /config asks for
(#1192). Every change needs a restart to take effect, and the page
says so.
"""
from __future__ import annotations

import copy
import shutil
import tempfile
from dataclasses import dataclass, field as _dc_field
from pathlib import Path

from .. import credential_binding, printers, settings
from . import config as config_api
from . import pages, shell
from .catalog import (PRINTER_URL_BY_CONNECTION, choices_for, field_by_id)

_esc = pages._esc
_show = pages._show

# Catalog fields this tab shows for the default/legacy printer, in
# display order within their sections; the connection field for the
# printer's own connection kind is inserted after the model.
_NAME_MODEL = ("printer.name", "printer.model")
_SLICING = ("printer.slicer", "delivery.methods", "printer.zmod_delivery_format")

# The entry keys that carry a printer's connection, by vendor
# connection kind: (entry key, label, help). One connection, one
# address field (plus the Bambu serial its validator pairs with the
# host), mirroring catalog.PRINTER_URL_BY_CONNECTION for entries.
ENTRY_CONNECTION_FIELDS = {
    "moonraker": (("moonraker_url", "Moonraker URL",
                   "The address Print Handy uses to talk to this printer's "
                   "Moonraker, for example http://printer.local:7125."),),
    "octoprint": (("octoprint_url", "OctoPrint URL",
                   "The address Print Handy uses to talk to this printer's "
                   "OctoPrint, for example http://octopi.local."),),
    "bambulab_lan": (("lan_host", "Bambu Lab LAN host",
                      "Bambu Lab LAN hostname or IP. Not a full URL. Empty "
                      "means no LAN connection."),
                     ("lan_serial", "Printer serial",
                      "Bambu Lab printer serial number. The LAN connection "
                      "needs the host and the serial together. The LAN access "
                      "code itself stays in the environment, never here.")),
    "prusalink": (("link_host", "PrusaLink host",
                   "PrusaLink hostname or IP. Not a full URL. Empty means no "
                   "LAN connection."),),
    "flashforge_stock": (("host", "Flashforge stock host",
                          "Stock Flashforge LAN hostname or IP. Not a full "
                          "URL. Empty means offline."),),
    "elegoo_sdcp": (("host", "Elegoo SDCP host",
                     "Elegoo SDCP hostname or IP. Not a full URL. Empty "
                     "means no LAN connection."),),
}

# Entry connection key -> the catalog field id carrying the same
# address on the default view (the credential-binding slot id, where
# the service has one). "host" is vendor-shaped; it is resolved per
# printer in _entry_catalog_id.
_ENTRY_TO_CATALOG = {
    "moonraker_url": "services.moonraker_url",
    "octoprint_url": "services.octoprint_url",
    "lan_host": "bambu.lan_host",
    "lan_serial": "bambu.lan_serial",
    "link_host": "prusa.link_host",
}

_ENTRY_LABELS = {
    "name": ("Printer name",
             "The display name for this printer, used in chat, on the "
             "status page and in messages."),
    "model": ("Printer model",
              "The printer model. Changing it changes the model's "
              "defaults, its connection kind and its slicer and "
              "delivery choices, so check the connection fields after "
              "a change."),
    "chat_id": ("Chat channel",
                "The channel or room this printer answers in, on the "
                "platform the bot runs (Discord channel ID, Telegram "
                "chat ID, or Matrix room ID). Empty means the "
                "platform's shared channel."),
}

_SOURCE_LABELS = pages.PROVENANCE_LABELS


@dataclass
class Row:
    """One field on the page."""

    name: str  # the form field name
    label: str
    kind: str  # "text" | "picker" | "multi" | "static"
    help: str = ""
    value: object = ""
    choices: tuple = ()
    source: str = ""  # provenance label for the badge
    locked: bool = False
    lock_note: str = ""  # why it is locked, naming the variable or layer
    note: str = ""  # a plain extra note under the control


@dataclass
class PageModel:
    printer_id: str
    printers: list  # [{id, name, model_label, is_default}]
    shape: str  # config_api PrinterEntryState shape, or "legacy"
    sections: list  # [(heading, [Row])]
    roster_note: str = ""  # why entry fields are read-only, when they are
    catalog_values: dict = _dc_field(default_factory=dict)  # field id -> FieldValue
    entry_state: object = None  # PrinterEntryState, roster shapes only
    is_default: bool = True


def _printer_choices(data):
    out = []
    default_id = settings.default_printer_id(data)
    for entry in settings.printers(data):
        model = printers.MODELS.get(entry.get("model"))
        out.append({"id": entry["id"],
                    "name": str(entry.get("name") or entry["id"]),
                    "model_label": model.label if model else str(entry.get("model") or ""),
                    "is_default": entry["id"] == default_id})
    return out


def _model_of(data, printer_id):
    view = settings.view_for(printer_id, data)
    return printers.MODELS[settings._model_key(view["printer"]["model"])], view


def _catalog_row(field_value, view):
    field = field_value.field
    if isinstance(field_value.value, (list, tuple)):
        kind = "multi"
    elif field.kind == "picker":
        kind = "picker"
    else:
        kind = "text"
    choices = ()
    if field.kind == "picker":
        choices = tuple(str(c) for c in choices_for(field, data=view))
    locked = field_value.overridden
    lock_note = ""
    if locked:
        lock_note = (f"Overridden by the environment "
                     f"(PRINT_HANDY__{field.section.upper()}__{field.key.upper()} or a "
                     "legacy name) or set in your own settings file: change it there "
                     "instead; this field is not written to the settings file."
                     if field_value.provenance == config_api.ENVIRONMENT else
                     "Set in your own settings file, which overrides the settings "
                     "file this page writes: change it there instead.")
    return Row(name=field.id, label=field.label, kind=kind, help=field.help,
               value=field_value.value, choices=choices,
               source=_SOURCE_LABELS.get(field_value.provenance, field_value.provenance),
               locked=locked, lock_note=lock_note)


def _entry_row(key, state, *, model, writable_shape):
    label, help_text = _ENTRY_LABELS.get(key, (key, ""))
    key_state = state.keys[key]
    locked = key_state.env_overridden or not writable_shape
    lock_note = ""
    if key_state.env_overridden:
        lock_note = (f"Overridden by the environment (${key_state.env_var}): change "
                     "or unset that variable instead; this field is not written "
                     "to the settings file.")
    elif not writable_shape:
        lock_note = ""  # the section-level roster note carries the reason
    kind = "picker" if key == "model" else "text"
    choices = ()
    if key == "model":
        choices = tuple(sorted(printers.MODELS,
                               key=lambda k: (printers.MODELS[k].label.casefold(), k)))
    return Row(name=f"entry.{key}", label=label, kind=kind, help=help_text,
               value=key_state.value, choices=choices,
               source=_SOURCE_LABELS.get(key_state.source, key_state.source),
               locked=locked, lock_note=lock_note)


def _entry_connection_rows(state, *, model, writable_shape):
    rows = []
    for key, label, help_text in ENTRY_CONNECTION_FIELDS.get(model.vendor.connection, ()):
        key_state = state.keys[key]
        locked = key_state.env_overridden or not writable_shape
        lock_note = ""
        if key_state.env_overridden:
            lock_note = (f"Overridden by the environment (${key_state.env_var}): "
                         "change or unset that variable instead; this field is "
                         "not written to the settings file.")
        rows.append(Row(name=f"entry.{key}", label=label, kind="text",
                        help=help_text, value=key_state.value,
                        source=_SOURCE_LABELS.get(key_state.source, key_state.source),
                        locked=locked, lock_note=lock_note))
    return rows


def build_model(data, printer_id, *, environ=None):
    """The PageModel for one printer of loaded settings ``data``."""
    entry_state = config_api.printer_entry_state(data, printer_id, environ=environ)
    model, view = _model_of(data, printer_id)
    is_default = printer_id == settings.default_printer_id(data)
    all_values = {fv.id: fv for fv in config_api.read_fields(environ=environ)}
    sections = []
    if entry_state.shape == "legacy":
        identity = [_catalog_row(all_values[fid], view) for fid in _NAME_MODEL]
        identity.append(Row(
            name="", label="Chat channel", kind="static",
            help="",
            value="This printer answers in the bot platform's channel; a "
                  "single-printer install has no per-printer channel. "
                  "Per-printer channels exist on installs with a declared "
                  "printer roster.",
            source=""))
        connection_ids = [PRINTER_URL_BY_CONNECTION[model.vendor.connection]]
        if model.vendor.connection == "bambulab_lan":
            connection_ids.append("bambu.lan_serial")
        connection = [_catalog_row(all_values[fid], view) for fid in connection_ids]
        slicing = []
        for fid in _SLICING:
            if fid == "delivery.methods":
                row = _catalog_row(all_values[fid], view)
                row.note = "Shared by every printer on this install."
                slicing.append(row)
            elif fid == "printer.zmod_delivery_format":
                if model.vendor.key == "flashforge":
                    slicing.append(_catalog_row(all_values[fid], view))
            else:
                slicing.append(_catalog_row(all_values[fid], view))
        sections = [("Printer", identity), ("Connection", connection),
                    ("Slicing and delivery", slicing)]
        return PageModel(printer_id, _printer_choices(data), "legacy", sections,
                         catalog_values=all_values, is_default=is_default)
    # Roster shapes: the entry carries identity, channel and connection.
    writable_shape = entry_state.writable
    identity = [_entry_row("name", entry_state, model=model, writable_shape=writable_shape),
                _entry_row("model", entry_state, model=model, writable_shape=writable_shape),
                _entry_row("chat_id", entry_state, model=model, writable_shape=writable_shape)]
    connection = _entry_connection_rows(entry_state, model=model,
                                        writable_shape=writable_shape)
    slicing = []
    delivery_row = _catalog_row(all_values["delivery.methods"], view)
    delivery_row.note = "Delivery methods are install-wide ([delivery] stays " \
                        "shared by every printer); edited here for convenience."
    slicing.append(delivery_row)
    if is_default:
        slicing.append(_catalog_row(all_values["printer.slicer"], view))
        if model.vendor.key == "flashforge":
            slicing.append(_catalog_row(all_values["printer.zmod_delivery_format"], view))
    else:
        slicing.append(Row(
            name="", label="Slicer", kind="static",
            value=str(view["printer"].get("slicer") or ""),
            note="Set by this printer's model; not a per-printer setting."))
        if model.vendor.key == "flashforge":
            slicing.append(Row(
                name="", label="ZMod delivery format", kind="static",
                value=str(view["printer"].get("zmod_delivery_format") or ""),
                note="Follows the default printer's setting on a roster "
                     "install; edit it on the default printer."))
    roster_note = ""
    if entry_state.shape == "user_file":
        roster_note = ("The printer roster is declared in your own settings "
                       "file, which overrides the settings file this page "
                       "writes, so this printer's own fields are read-only "
                       "here: change them in that file.")
    elif entry_state.shape == "home":
        roster_note = ("The printer roster is declared in the home "
                       "configuration, so this printer's own fields are "
                       "read-only here: change them in that file.")
    elif entry_state.shape == "roster_toml":
        roster_note = ("The settings file is TOML, and this page writes "
                       "YAML settings files, so this printer's own fields "
                       "are read-only here: edit the TOML file by hand, or "
                       "point PRINT_HANDY_CONFIG at a .yaml file.")
    sections = [("Printer", identity), ("Connection", connection),
                ("Slicing and delivery", slicing)]
    return PageModel(printer_id, _printer_choices(data), entry_state.shape,
                     sections, roster_note=roster_note,
                     catalog_values=all_values, entry_state=entry_state,
                     is_default=is_default)


# ------------------------------------------------------------------ rendering

def _control(row, value, *, disabled):
    name = _esc(row.name)
    dis = " disabled" if disabled else ""
    if row.kind == "static":
        return f"<span>{_esc(_show(value))}</span>"
    if row.kind == "picker":
        choices = [str(c) for c in row.choices]
        current = "" if value is None else str(value)
        if current and current not in choices:
            choices = [current, *choices]
        options = "".join(
            f'<option value="{_esc(choice)}"{" selected" if choice == current else ""}>'
            f'{_esc(_choice_label(row, choice))}</option>' for choice in choices)
        return f'<select id="f-{name}" name="{name}"{dis}>{options}</select>'
    if row.kind == "multi":
        selected = {str(item) for item in (value or [])}
        options = "".join(
            f'<option value="{_esc(choice)}"{" selected" if choice in selected else ""}>'
            f'{_esc(choice)}</option>' for choice in (str(c) for c in row.choices))
        size = min(max(len(row.choices), 2), 8)
        return (f'<select id="f-{name}" name="{name}" multiple size="{size}"{dis}>'
                f'{options}</select>')
    return (f'<input type="text" id="f-{name}" name="{name}" '
            f'value="{_esc("" if value is None else value)}"{dis}>')


def _choice_label(row, choice):
    if row.name in ("printer.model", "entry.model"):
        model = printers.MODELS.get(str(choice))
        if model is not None:
            return model.label
    return str(choice)


def _row_html(row, *, submitted, errors):
    value = row.value
    if submitted is not None and row.name in submitted:
        raw = submitted[row.name]
        value = list(raw) if row.kind == "multi" else (raw[0] if raw else "")
    parts = [f'<div class="field" id="row-{_esc(row.name)}">']
    if row.kind == "static":
        parts.append(f"<label>{_esc(row.label)}</label> "
                     + _control(row, value, disabled=True))
    else:
        badge = (f' <span class="badge{" warn" if row.locked else ""}">'
                 f'{"Locked: " if row.locked else ""}{_esc(row.source)}</span>'
                 if row.source else "")
        parts.append(f'<label for="f-{_esc(row.name)}">{_esc(row.label)}{badge}</label> ')
        parts.append(_control(row, value, disabled=row.locked))
    error = (errors or {}).get(row.name)
    if error:
        parts.append(f'<p class="error">{_esc(error)}</p>')
    if row.locked and row.lock_note:
        parts.append(f'<p class="note">{_esc(row.lock_note)}</p>')
    if row.note:
        parts.append(f'<p class="note">{_esc(row.note)}</p>')
    if row.help:
        parts.append(f'<p class="help">{_esc(row.help)}</p>')
    parts.append("</div>")
    return "".join(parts)


def render_page(model, *, form_token, theme=shell.DEFAULT_THEME, nav=None,
                errors=None, submitted=None, notice=None, rebinds=()):
    """The GET /printer-settings page (also the save re-render target)."""
    parts = []
    if len(model.printers) > 1:
        links = "".join(
            f'<a href="/printer-settings?printer={_esc(p["id"])}"'
            + (' aria-current="page"' if p["id"] == model.printer_id else "")
            + f'>{_esc(p["name"])}</a>' for p in model.printers)
        parts.append(f'<nav class="pills config-tabs" aria-label="Printers">{links}</nav>')
    parts.append(
        "<p>This is your printer's page: its name, how it connects, and how "
        "it prints, in plain language. It writes the same settings file the "
        "Settings page edits, through the same validation, so nothing here "
        "can save a value the settings loader would refuse. <strong>Every "
        "change needs a restart</strong> of Print Handy (or the container) "
        "to take effect: settings are read once at process start.</p>")
    if model.roster_note:
        parts.append(f'<p class="notice">{_esc(model.roster_note)}</p>')
    if notice:
        parts.append(f'<p class="notice">{_esc(notice)}</p>')
    general = (errors or {}).get("_general")
    if general:
        parts.append(f'<p class="error">{_esc(general)}</p>')
    for move in rebinds:
        parts.append(
            f'<p class="notice">This change moves the {_esc(move.service)} address '
            f'from {_esc(move.old_host)} to {_esc(move.new_host)}. Your stored '
            f'{_esc(move.service)} key is sent only to {_esc(move.old_host)}; the '
            'save is refused unless you confirm the new host.<br>'
            f'<label><input type="checkbox" form="printer-settings-form" '
            f'name="{pages.REBIND_FIELD}" value="{_esc(move.setting)}"> '
            f'{_esc(move.confirmation)}</label></p>')
    parts.append(f'<form method="post" action="/printer-settings" id="printer-settings-form">'
                 f'<input type="hidden" name="{pages.CSRF_FIELD}" value="{_esc(form_token)}">'
                 f'<input type="hidden" name="printer" value="{_esc(model.printer_id)}">')
    for heading, rows in model.sections:
        parts.append(f"<section><h2>{_esc(heading)}</h2>")
        parts.extend(_row_html(row, submitted=submitted, errors=errors) for row in rows)
        parts.append("</section>")
    parts.append('<p><button type="submit">Save changes</button></p></form>')
    # No nav tab of its own (#1625): the page opens from each printer's
    # card on the Printers page, so the Printers tab stays highlighted.
    parts.insert(0, '<p><a href="/printers">&larr; All printers</a></p>')
    return shell.page("printer settings", "".join(parts), path="/printers",
                      theme=theme, heading="Printer settings", nav=nav)


def render_result(changed, *, printer_id, warnings=(), theme=shell.DEFAULT_THEME,
                  nav=None):
    """The post-save page: what was written, and the restart reminder."""
    if changed:
        listing = "".join(f"<li>{_esc(label)}</li>" for label in changed)
        body = (f"<p>Saved to the settings file:</p><ul>{listing}</ul>"
                "<p><strong>Restart required:</strong> settings are read once "
                "at process start, so restart Print Handy (or the container) "
                "for these changes to take effect. Nothing was applied "
                "live.</p>")
    else:
        body = "<p>Nothing changed: the settings file already held these values.</p>"
    if warnings:
        lines = "".join(
            f"<li>{_esc(name)} is still in the settings file: the next "
            "restart will fail until it is removed.</li>" for name in warnings)
        body += f'<ul class="error">{lines}</ul>'
    body += (f'<p><a href="/printer-settings?printer={_esc(printer_id)}">'
             "Back to this printer</a></p>")
    return shell.page("printer settings saved", body, path="/printer-settings",
                      theme=theme, heading="Printer settings", nav=nav)


# ------------------------------------------------------------------ saving

def _entry_catalog_id(key, model):
    if key == "host":
        return ("flashforge_stock.host" if model.vendor.key == "flashforge_stock"
                else "elegoo_sdcp.host")
    return _ENTRY_TO_CATALOG.get(key, "")


def parse_submission(form, model):
    """(entry_changes, catalog_changes, submitted, errors) for one POST.

    Catalog fields go through pages.parse_form against the page's field
    subset, so conversion and diffing match /config exactly. Entry keys
    are plain text (the model picker included); locked rows are skipped.
    """
    submitted, errors = {}, {}
    # pages.parse_form reads an absent field as "submitted empty" (the
    # /config confirm flow relies on that), so parse only the fields this
    # submission actually carries, plus the checkbox groups where an
    # absence is the operator clearing every box.
    catalog_ids = [row.name for _heading, rows in model.sections for row in rows
                   if row.name and not row.name.startswith("entry.")
                   and row.kind != "static"
                   and (row.name in form or row.kind == "multi")]
    subset = [model.catalog_values[fid] for fid in catalog_ids
              if fid in model.catalog_values]
    catalog_changes, cat_submitted, cat_errors = pages.parse_form(form, subset)
    submitted.update(cat_submitted)
    errors.update(cat_errors)
    entry_changes = {}
    if model.entry_state is not None and model.entry_state.shape != "legacy":
        writable = model.entry_state.writable
        for _heading, rows in model.sections:
            for row in rows:
                if not row.name.startswith("entry."):
                    continue
                key = row.name[len("entry."):]
                raw = form.get(row.name)
                submitted[row.name] = list(raw) if raw is not None else []
                if row.locked or not writable:
                    continue
                value = (raw[0] if raw else "").strip()
                if value != model.entry_state.keys[key].value:
                    entry_changes[key] = value
    return entry_changes, catalog_changes, submitted, errors


def _entry_write_errors(printer_id, changes, *, environ):
    """({row name: message}, general) for an entry change set, validated
    by running the real entry writer against a throwaway copy of the
    settings file. The real file is never touched."""
    target = config_api.settings_file_path(environ)

    def attempt(subset):
        with tempfile.TemporaryDirectory() as tmp:
            copy_path = Path(tmp) / target.name
            if target.is_file():
                shutil.copyfile(target, copy_path)
            try:
                config_api.write_printer_entry(printer_id, subset,
                                               config_path=copy_path,
                                               environ=environ)
            except config_api.WebUIConfigError as exc:
                return str(exc)
        return None

    combined = attempt(changes)
    if combined is None:
        return {}, None
    per_key = {}
    for key, value in changes.items():
        message = attempt({key: value})
        if message is not None:
            per_key[f"entry.{key}"] = message
    if per_key:
        return per_key, None
    return {}, combined


def _pending_moves(data, model, entry_changes, catalog_changes, *, environ):
    """[credential_binding.Rebind] a save would need confirmed: catalog
    changes through config.pending_rebinds (as /config computes them),
    the default printer's entry connection changes translated to their
    catalog slots, and other printers' entry host moves built the same
    way (the binding store tracks the default view's slots only, but a
    stored key is sent to a non-default printer's host too, so the
    confirmation discipline is the same)."""
    moves = []
    if catalog_changes:
        moves.extend(config_api.pending_rebinds(
            {field.id: value for field, value in catalog_changes.items()},
            environ=environ))
    if entry_changes:
        printer_model, _view = _model_of(data, model.printer_id)
        slot_changes = {}
        for key, value in entry_changes.items():
            catalog_id = _entry_catalog_id(key, printer_model)
            if catalog_id in credential_binding.SLOTS:
                slot_changes[catalog_id] = (key, value)
        if slot_changes and model.is_default:
            moves.extend(config_api.pending_rebinds(
                {cid: value for cid, (_key, value) in slot_changes.items()},
                environ=environ))
        elif slot_changes:
            entry = settings.by_id(model.printer_id, data)
            for catalog_id, (key, new) in slot_changes.items():
                old = str(entry.get(key) or "")
                old_host = credential_binding.host_of(old)
                new_host = credential_binding.host_of(new)
                if not old_host or not new_host or old_host == new_host:
                    continue
                slot = credential_binding.SLOTS[catalog_id]
                moves.append(credential_binding.Rebind(
                    f"entry.{key}", slot.service, old_host, new_host))
    return moves


def save(data, printer_id, form, *, environ=None, confirmed=()):
    """Validate and apply one Printer settings submission.

    Returns ("form", model, submitted, errors, moves) when the page
    must be re-rendered (conversion or validation errors, or an
    unconfirmed credential rebind), or ("result", changed_labels,
    warnings) after a write. Nothing is written unless every part of
    the submission validates first.
    """
    model = build_model(data, printer_id, environ=environ)
    entry_changes, catalog_changes, submitted, errors = parse_submission(form, model)
    if errors:
        return "form", model, submitted, errors, ()
    moves = _pending_moves(data, model, entry_changes, catalog_changes,
                           environ=environ)
    unconfirmed = [move for move in moves if move.setting not in set(confirmed)]
    if unconfirmed:
        return "form", model, submitted, {}, moves
    changed, warnings = [], []
    try:
        pairs, warnings, entry_pairs = config_api.write_printer_settings(
            printer_id, entry_changes,
            {field.id: value for field, value in catalog_changes.items()},
            environ=environ, rebind=confirmed)
    except config_api.WebUIConfigError as exc:
        errors.update({"_general": str(exc)})
        return "form", model, submitted, errors, ()
    changed.extend(f"{section}.{key}" for section, key in pairs)
    changed.extend(f"{pid} {key}" for pid, key in entry_pairs)
    if entry_changes:
        # Confirmed moves of the default printer's entry addresses bind
        # the stored keys to the new hosts, as a catalog save does.
        printer_model, _view = _model_of(data, printer_id)
        values = {_entry_catalog_id(k, printer_model): v
                  for k, v in entry_changes.items()}
        for move in moves:
            if move.setting in values and move.setting in set(confirmed):
                credential_binding.rebind(move.setting, values[move.setting])
    return "result", changed, warnings


__all__ = ["build_model", "parse_submission", "render_page", "render_result", "save"]
