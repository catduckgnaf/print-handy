"""Read and write Print Handy settings for the Web UI (#286, stage 1).

Read: for every catalog field, the effective value (from the real
``settings.load()`` layering: printer-model defaults, home config,
settings file, environment overrides) and its provenance — which layer
supplied it. Nothing here reimplements that layering; the layer snapshots
below are built with settings' own ``_read``/``_merge``/``_from_env``
helpers and the effective values come from ``settings.load()`` itself.

Write: only the settings-file layer is ever written — the file
``$PRINT_HANDY_CONFIG`` names, else ``config/local.yaml`` (the resolution
``settings.load()`` uses). A field currently overridden by the
environment is refused, never shadow-written. The fully merged candidate
is validated through ``settings.load()`` before anything lands, the
previous file is kept as a timestamped backup, and the write itself is
atomic (temp file + ``os.replace``, the setup wizard's pattern). On any
validation failure the file on disk is left byte-identical. Validation
answers only for what the save changes: a closed-table violation the
file already carries (#1058's credential-shaped, [notify] and retired
[printer] family keys) is left untouched and recorded by name in
``last_write_warnings()`` instead of blocking every save, while a
violation a save adds or modifies still refuses the whole write. Lists and
tables outside the catalog are preserved as parsed mappings; comments in
a hand-edited file are not guaranteed to survive a save, the same
honesty as the setup wizard's merge.
"""
from __future__ import annotations

import copy
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .. import printers, settings
from ..setup_wizard import _atomic_write
from .catalog import CATALOG, Field, field_by_id

# Provenance labels for a field's effective value.
MODEL_DEFAULT = "model_default"
HOME_CONFIG = "home_config"
SETTINGS_FILE = "settings_file"
USER_FILE = "user_file"  # the operator's own file (settings.user_config_path), layered over SETTINGS_FILE
ENVIRONMENT = "environment"
DEFAULT = "default"  # supplied by validation's defaults; no file or env layer names it

_MISSING = object()


class WebUIConfigError(ValueError):
    """A Web UI config read or write was refused; nothing was written."""


@dataclass(frozen=True)
class FieldValue:
    """One catalog field's effective value and where it came from."""

    field: Field
    value: object
    provenance: str

    @property
    def id(self):
        return self.field.id

    @property
    def env_overridden(self):
        return self.provenance == ENVIRONMENT

    @property
    def overridden(self):
        """Set by a layer above the settings file the Web UI writes, so a value saved here would not apply."""
        return self.provenance in (ENVIRONMENT, USER_FILE)


def settings_file_path(environ=None):
    """The settings file ``settings.load()`` would read: $PRINT_HANDY_CONFIG,
    else config/local.yaml (falling back to the legacy local.toml when it
    is the file that exists)."""
    environ = os.environ if environ is None else environ
    named = environ.get("PRINT_HANDY_CONFIG")
    if named:
        return Path(named)
    if settings.LOCAL.is_file():
        return settings.LOCAL
    if settings.LEGACY_LOCAL.is_file():
        return settings.LEGACY_LOCAL
    return settings.LOCAL


def _home_path(home):
    if home is not None:
        return Path(home)
    return settings.HOME if settings.HOME.is_file() else settings.LEGACY_HOME


def _file_path(local, environ):
    """The settings-file layer for an explicit ``local``, mirroring settings.load:
    None resolves like settings_file_path, False means no settings file."""
    if local is None:
        return settings_file_path(environ)
    if not local:
        return None
    return Path(local)


def _layers(home, local, environ):
    """(model_defaults, home_data, file_data, user_data, env_data) snapshots, built with
    settings' own merge/env helpers in settings.load's order."""
    # Every file layer goes through the alias step load() applies, so a deprecated spelling
    # shows (and is attributed) under its current name (2.2.5 review of #783).
    home_data = settings._apply_key_aliases(settings._read(_home_path(home)))
    file_path = _file_path(local, environ)
    file_data = (settings._apply_key_aliases(settings._read(file_path))
                 if file_path is not None and file_path.is_file() else {})
    user_path = settings.user_config_path(environ)
    user_data = (settings._apply_key_aliases(settings._read(user_path))
                 if user_path is not None and user_path.is_file() else {})

    def pick(name):
        return (environ.get(f"PRINT_HANDY__PRINTER__{name.upper()}")
                or user_data.get("printer", {}).get(name)
                or file_data.get("printer", {}).get(name)
                or home_data.get("printer", {}).get(name))

    key = settings._model_key(pick("model"), pick("vendor"))
    model_defaults = printers.MODELS[key].defaults()
    base = copy.deepcopy(settings._merge(model_defaults, home_data))
    # The declared roster, resolved exactly as settings.load resolves it:
    # _from_env needs it to apply indexed PRINT_HANDY__PRINTERS__<ID>__<KEY>
    # overrides to their entries — without it every such override reads as
    # "no printer with id ... is declared" and the read raises (#813).
    declared = settings._merge(home_data, settings._merge(file_data, user_data)).get("printers")
    env_data = settings._from_env(base, environ, declared)
    return model_defaults, home_data, file_data, user_data, env_data


def _provenance(field, layers):
    model_defaults, home_data, file_data, user_data, env_data = layers
    for provenance, layer in ((ENVIRONMENT, env_data), (USER_FILE, user_data), (SETTINGS_FILE, file_data),
                              (HOME_CONFIG, home_data), (MODEL_DEFAULT, model_defaults)):
        if field.key in (layer.get(field.section) or {}):
            return provenance
    return DEFAULT


def read_fields(*, home=None, local=None, environ=None):
    """[FieldValue] for every catalog field: the effective value from
    ``settings.load()`` and the layer that supplied it."""
    environ = os.environ if environ is None else environ
    effective = settings.load(home=home, local=local, environ=environ)
    layers = _layers(home, local, environ)
    out = []
    for field in CATALOG:
        value = copy.deepcopy((effective.get(field.section) or {}).get(field.key))
        out.append(FieldValue(field, value, _provenance(field, layers)))
    return out


def read_field(field_id, **kwargs):
    """The single FieldValue for ``field_id``, else None."""
    wanted = field_by_id(field_id)
    if wanted is None:
        return None
    for item in read_fields(**kwargs):
        if item.field is wanted:
            return item
    return None  # pragma: no cover - every catalog field is read above


def _dump(mapping):
    try:
        import yaml
    except ImportError:  # pragma: no cover - PyYAML is a runtime dependency
        raise WebUIConfigError("PyYAML is required to write YAML config") from None
    return yaml.safe_dump(mapping, sort_keys=False, default_flow_style=None)


# ------------------------------------------------------- candidate validation
#
# The candidate is validated through the real settings loader before
# anything lands. The loader refuses a whole file over a closed-table
# violation (#1058/#1061): a credential-shaped unknown key, a
# non-optional key in the closed [notify] table, or the retired
# [printer] family key, wherever in the file it sits. Those refusals
# are the loader's own rules and stay exactly as they are — but a save
# must only answer for what it changes. When the loader names such a
# violation that the settings file (or the operator's own file layered
# over it) ALREADY carries, unchanged by this save, validation retries
# with that key left out of the staged validation copies, and the save
# proceeds with the violation recorded by name (see
# last_write_warnings). A violation the save ADDS or MODIFIES — a new
# closed-table key, or an existing one whose value this candidate
# changes — still fails closed with the loader's own message, and a
# violation arriving through the environment is never tolerated: the
# save cannot fix the environment, so it must not silently pass it.
# Only key NAMES are ever recorded or reported here, never values: the
# credential-shaped keys this exists for must not echo their secrets
# into a page or a log.

_VIOLATION_KEY_RE = re.compile(r"unknown setting \[([^\]]+)\] ([^\s(]+)")
_VIOLATION_SECTION_RE = re.compile(r"unknown section \[([^\]]+)\]")
_MAX_VALIDATION_RETRIES = 100

_last_write_warnings = []


def last_write_warnings():
    """The pre-existing closed-table violations the most recent
    successful write tolerated, as ``"[section] key"`` names.

    A tolerated violation is a key the settings loader refuses — a
    credential-shaped unknown key, a non-optional [notify] key, the
    retired [printer] family key — that was already in the settings
    file (or the operator's own file) before the save and that the save
    did not touch. The write left it in the file, byte for byte; the
    names are also noted for this process's
    ``settings.unknown_settings_warning()``.
    Names only, never values."""
    return list(_last_write_warnings)


def _note_tolerated(target, tolerated):
    """Record a landed write's tolerated violations: the module-level
    list behind last_write_warnings(), and this process's in-memory
    settings registry (settings._note_unknown, the same channel #1058's
    warn-and-drop keys use), so the names show in this process's
    unknown-settings note on the save path. The note does not reach a
    fresh startup or doctor process: the tolerated keys stay in the
    file, and a process that reloads the file refuses to load it until
    they are removed by hand."""
    global _last_write_warnings
    _last_write_warnings = [f"[{section}] {key}" for section, key in tolerated]
    for section, key in tolerated:
        settings._note_unknown(
            f"{target}: unknown setting [{section}] {key} — already in the "
            "settings file before the Web UI save; the save left it untouched. "
            "This key is refused when a save introduces it, and the settings "
            "will not load until it is removed by hand")


def _clear_write_warnings():
    """Forget the previous write's tolerated violations: a successful
    save that writes nothing (the file already held the submitted
    values) tolerated nothing, so last_write_warnings() reports that
    save's empty set instead of an earlier save's names."""
    global _last_write_warnings
    _last_write_warnings = []


def _section_violation_keys(section, values):
    """The closed-table keys of one section of one layer: the keys the
    loader refuses in a section it does not know — the non-optional
    keys of the closed [notify] table, or the credential-shaped keys of
    any other unknown section. Empty when the section cannot be the
    source of an "unknown section" refusal."""
    if not isinstance(values, dict):
        return []
    if section == "notify":
        return [key for key in values if (section, key) not in settings.OPTIONAL_KEYS]
    return [key for key in values if settings._credential_shaped(key)]


def _offender(message, *, candidate_source, user_sources):
    """The closed-table violation a loader refusal names, as
    ``(layer, section, keys)``: layer is "candidate" (the settings file
    being written) or "user" (the operator's own file, by its real path
    or by a staged copy's); keys is the one named key, or None for an
    "unknown section" refusal — the caller resolves those against the
    layer's working copy. ``(None, None, [])`` when the refusal names
    no tolerable violation (a wrong type, a malformed roster, an
    environment variable's refusal): those fail as they are.
    """
    layer = None
    match = re.match(r"(?P<source>.*): unknown (?:setting|section) \[", message)
    if match:
        source = match.group("source")
        if source == candidate_source:
            layer = "candidate"
        elif source in user_sources:
            layer = "user"
        else:
            return None, None, []
    key_match = _VIOLATION_KEY_RE.search(message)
    if key_match:
        return layer, key_match.group(1), [key_match.group(2)]
    section_match = _VIOLATION_SECTION_RE.search(message)
    if section_match:
        return layer, section_match.group(1), None
    return None, None, []


def _load_staged(candidate, user_copy, target, *, home, environ):
    """One validation attempt: stage the candidate (and a staged copy
    of the operator's own file, when user_copy is not None) and run the
    real settings loader over them. On failure the raised
    WebUIConfigError carries this attempt's ``candidate_source`` and
    ``user_source`` — the source names refusals from each staged layer
    carry, which change with every staging."""
    directory = str(target.parent) if str(target.parent) else "."
    fd, tmp = tempfile.mkstemp(prefix=".webui-candidate.", suffix=".yaml",
                               dir=directory)
    user_tmp = None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_dump(candidate))
        if user_copy is not None:
            fd, user_tmp = tempfile.mkstemp(prefix=".webui-user.", suffix=".yaml",
                                            dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(_dump(user_copy))
        settings.load(home=home, local=Path(tmp), environ=environ,
                      user=Path(user_tmp) if user_tmp is not None else None)
    except settings.SettingsError as exc:
        error = WebUIConfigError(str(exc))
        error.candidate_source = tmp
        resolved_user = settings.user_config_path(environ)
        error.user_source = user_tmp or (str(resolved_user)
                                         if resolved_user is not None else None)
        raise error from None
    finally:
        for staged in (tmp, user_tmp):
            if staged and os.path.exists(staged):
                os.unlink(staged)


def _validate_candidate(candidate, target, *, home, environ):
    """Run the candidate settings-file mapping through the real settings
    loader (staged copies settings.load reads), raising WebUIConfigError
    with the loader's own message on any failure the save is answerable
    for. Returns ``(text, tolerated)``: the text to write — the dump of
    the candidate exactly as given, tolerated keys included — and the
    ``(section, key)`` closed-table violations that were already in the
    settings file or the operator's own file, unchanged, and were left
    out of the staged copies so the rest of the save could validate."""
    text = _dump(candidate)
    # The baselines a violation is judged pre-existing against: the
    # settings file as it stands (the candidate was built from it), and
    # the operator's own file, both alias-folded the way the loader
    # reads them. A key is pre-existing only with its value unchanged.
    baseline = (settings._apply_key_aliases(settings._read(target))
                if target.is_file() else {})
    user_path = settings.user_config_path(environ)
    user_baseline = (settings._apply_key_aliases(settings._read(user_path))
                     if user_path is not None and user_path.is_file() else {})
    working = copy.deepcopy(candidate)
    user_working = copy.deepcopy(user_baseline) if user_path is not None \
        and user_path.is_file() else None
    user_staged = None  # the user working copy, once a violation needs it staged
    user_sources = {str(user_path)} if user_path is not None else set()
    tolerated = []
    for _attempt in range(_MAX_VALIDATION_RETRIES):
        try:
            _load_staged(working, user_staged, target, home=home, environ=environ)
            return text, tolerated
        except WebUIConfigError as exc:
            message = str(exc)
            if getattr(exc, "user_source", None):
                user_sources.add(exc.user_source)
            layer, section, keys = _offender(
                message,
                candidate_source=getattr(exc, "candidate_source", None),
                user_sources=user_sources)
            if layer is None or section is None:
                raise
            if layer == "candidate":
                layer_working, layer_baseline = working, baseline
            else:
                layer_working, layer_baseline = user_working, user_baseline
            if layer_working is None:
                raise
            values = layer_working.get(section)
            if keys is None:
                keys = _section_violation_keys(section, values)
            if not keys:
                raise
            for key in keys:
                if not isinstance(values, dict) or key not in values:
                    raise exc
                if layer == "candidate" and (
                        not isinstance(layer_baseline.get(section), dict)
                        or layer_baseline[section].get(key, _MISSING) != values[key]):
                    # The save adds this violation, or changes the value
                    # of one already there: fail closed, exactly as the
                    # loader does today.
                    raise exc
                # The operator's own file is never written by a save, so
                # anything in it is pre-existing for this save by
                # construction; the settings file must match by value.
            for key in keys:
                del values[key]
                tolerated.append((section, key))
            if layer == "user":
                user_staged = user_working
    raise WebUIConfigError(  # pragma: no cover - each retry strips a key
        "settings validation did not settle; nothing was written")


def _check_discord_chat_id(value):
    """The content rule guided setup applies (``guided._validate_chat_id``):
    exactly one numeric channel ID. The settings loader checks only the
    type, so without this a value like ``not-a-channel`` is saved and the
    bot can then never match incoming channels."""
    text = str(value or "").strip()
    if not text:
        return
    from ..setup_wizard import _ids
    error = _ids(text)
    if error:
        raise WebUIConfigError(f"discord.chat_id: {error}")
    if len([part for part in re.split(r"[,\s]+", text) if part]) != 1:
        raise WebUIConfigError("discord.chat_id: exactly one numeric channel ID")


def _check_discord_timezone(value):
    """The timezone must name a real IANA zone: ``_config.tz()`` builds a
    ``ZoneInfo`` from it at import time in the print-summary and
    print-record modules, so an unknown name saved here breaks them."""
    text = str(value or "").strip()
    if not text:
        return
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError):
        raise WebUIConfigError(
            f"discord.timezone: {text!r} is not an IANA timezone name") from None


# Catalog fields whose *contents* the settings loader does not validate
# (it checks types only). write_changes runs these for every changed
# field before the candidate is written; every other catalog field's
# content rule already lives in the loader (telegram/matrix IDs, URLs,
# user ID lists, choices, ranges).
_CONTENT_CHECKS = {
    "discord.chat_id": _check_discord_chat_id,
    "discord.timezone": _check_discord_timezone,
}


def _backup(target, stamp):
    """A timestamped copy of the current file next to it; its path, or None."""
    if not target.is_file():
        return None
    backup = target.with_name(f"{target.name}.bak-{stamp}")
    counter = 1
    while backup.exists():
        backup = target.with_name(f"{target.name}.bak-{stamp}-{counter}")
        counter += 1
    shutil.copyfile(target, backup)
    return backup


# ------------------------------------------------------- the printer's name
#
# The default printer's display name lives in one of two shapes (#813):
# the legacy ``[printer] name`` — a plain catalog field, written by
# write_changes above — or a declared ``[[printers]]`` entry's ``name``
# key. The two shapes must never mix: the settings loader refuses a file
# that declares a roster alongside the legacy per-printer sections, so a
# roster entry's name is edited in place, in the one file layer that
# declares the roster, and only when that layer is the settings file the
# Web UI writes. The entry's ``id`` is never touched: renaming is a
# display-name change only (docs/MULTI-PRINTER-DESIGN.md §2.2). Where
# the name is owned by a layer the Web UI does not write — an indexed
# environment override, the operator's own settings file, the home
# configuration — or the settings file is TOML, the name stays read-only
# with the reason, exactly the discipline the catalog fields follow.


@dataclass(frozen=True)
class PrinterNameState:
    """Where the default printer's name lives and whether the Web UI may write it."""

    shape: str  # "legacy" | "roster" | "roster_toml" | "environment" | "user_file" | "home"
    printer_id: str
    name: str  # the explicit name in the declaring layer ("" when it sets none)
    env_var: str = ""  # the indexed override variable, when shape is "environment"


def _printer_name_env_var(environ, printer_id):
    """The PRINT_HANDY__PRINTERS__<ID>__NAME variable overriding this entry's
    name, or "" — parsed with the loader's own rule (the name is lowercased
    and split on "__"; entry ids are lowercase slugs)."""
    for raw_name, raw in environ.items():
        if not raw_name.startswith("PRINT_HANDY__") or not raw:
            continue
        parts = raw_name[len("PRINT_HANDY__"):].lower().split("__")
        if len(parts) == 3 and parts[0] == "printers" and parts[1] == printer_id \
                and parts[2] == "name":
            return raw_name
    return ""


def _raw_layer(path):
    if path is None or not Path(path).is_file():
        return {}
    return settings._apply_key_aliases(settings._read(path))


def printer_name_state(data, *, home=None, local=None, environ=None):
    """The PrinterNameState for the default printer of loaded settings ``data``.

    Layer resolution mirrors settings.load: the roster the loader sees is
    declared by the highest layer carrying a ``printers`` list (the
    operator's own file over the settings file over the home
    configuration), and an indexed environment variable overrides any
    declared entry's name.
    """
    environ = os.environ if environ is None else environ
    printer_id = settings.default_printer_id(data)
    env_var = _printer_name_env_var(environ, printer_id)
    if env_var:
        entry = settings.by_id(printer_id, data)
        return PrinterNameState("environment", printer_id,
                                str(entry.get("name") or ""), env_var)
    file_path = _file_path(local, environ)
    layers = (("user_file", _raw_layer(settings.user_config_path(environ))),
              ("file", _raw_layer(file_path)),
              ("home", _raw_layer(_home_path(home))))
    for layer_name, layer in layers:
        entries = layer.get("printers")
        if not isinstance(entries, list):
            continue
        entry = next((item for item in entries
                      if isinstance(item, dict) and item.get("id") == printer_id), {})
        name = entry.get("name") if isinstance(entry.get("name"), str) else ""
        if layer_name == "user_file":
            return PrinterNameState("user_file", printer_id, name)
        if layer_name == "home":
            return PrinterNameState("home", printer_id, name)
        if file_path is not None and file_path.suffix.lower() == ".toml":
            return PrinterNameState("roster_toml", printer_id, name)
        return PrinterNameState("roster", printer_id, name)
    return PrinterNameState("legacy", printer_id,
                            str((data.get("printer") or {}).get("name") or ""))


def _prepare_printer_name(printer_id, name, *, config_path, home, environ):
    """(target, validated_text) for a roster-entry rename, or None when the
    entry already carries ``name``. Raises WebUIConfigError — writing
    nothing — for every shape the writer refuses. The candidate is run
    through the real settings loader before anything lands, exactly as
    write_changes validates, so the post-write file is known to load."""
    if not isinstance(name, str):
        raise WebUIConfigError("printer name: the name must be text")
    name = name.strip()
    if "\n" in name or "\r" in name:
        raise WebUIConfigError("printer name: the name must fit on one line")
    target = Path(config_path) if config_path is not None else settings_file_path(environ)
    if target.suffix.lower() == ".toml":
        raise WebUIConfigError(f"{target}: the Web UI writes YAML settings files; "
                               "point PRINT_HANDY_CONFIG at a .yaml file or edit the TOML file by hand")
    env_var = _printer_name_env_var(environ, printer_id)
    if env_var:
        raise WebUIConfigError(
            f"the printer name is overridden by the environment (${env_var}); "
            "change or unset that variable instead — the value was not written to the file")
    user_path = settings.user_config_path(environ)
    if user_path is not None and user_path.is_file():
        if isinstance(_raw_layer(user_path).get("printers"), list):
            raise WebUIConfigError(
                f"the printer roster is declared in your own settings file ({user_path}), "
                "which overrides this one; change the name there — the value was not "
                "written to the file")
    current = settings._read(target) if target.is_file() else {}
    if not isinstance(current, dict):  # pragma: no cover - settings._read enforces this
        raise WebUIConfigError(f"{target}: top-level config must be a mapping")
    if not isinstance(current.get("printers"), list):
        raise WebUIConfigError(f"{target}: no [[printers]] roster is declared in this "
                               "file, so there is no roster entry name to write")
    candidate = copy.deepcopy(current)
    entry = next((item for item in candidate["printers"]
                  if isinstance(item, dict) and item.get("id") == printer_id), None)
    if entry is None:
        raise WebUIConfigError(f"{target}: no [[printers]] entry with id {printer_id!r} "
                               "is declared in this file")
    if name:
        if entry.get("name") == name:
            return None
        entry["name"] = name
    else:
        if "name" not in entry:
            return None
        del entry["name"]  # a blank name clears back to the model's own name
    text, tolerated = _validate_candidate(candidate, target, home=home, environ=environ)
    return target, text, tolerated


def check_printer_name(printer_id, name, *, config_path=None, home=None, environ=None):
    """Every refusal write_printer_name would raise, without writing:
    the walkthrough's pre-flight, so a step never half-saves."""
    environ = os.environ if environ is None else environ
    _prepare_printer_name(printer_id, name, config_path=config_path,
                          home=home, environ=environ)


def write_printer_name(printer_id, name, *, config_path=None, home=None,
                       environ=None, timestamp=None):
    """Set one declared roster entry's display ``name`` in the settings file.

    Surgical: exactly one key of exactly one entry changes; the entry's
    ``id`` and every other key are untouched, no legacy section is added,
    and the previous file is kept as a timestamped backup. Returns
    ``[("printers", printer_id)]`` when a write landed, [] when the entry
    already carried the name.
    """
    environ = os.environ if environ is None else environ
    prepared = _prepare_printer_name(printer_id, name, config_path=config_path,
                                     home=home, environ=environ)
    if prepared is None:
        _clear_write_warnings()
        return []
    target, text, tolerated = prepared
    target.parent.mkdir(parents=True, exist_ok=True)
    stamp = timestamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    _backup(target, stamp)
    _atomic_write(target, text, 0o644)
    _note_tolerated(target, tolerated)
    return [("printers", printer_id)]


def write_changes(changes, *, config_path=None, home=None, environ=None, timestamp=None):
    """Apply ``{field: value}`` changes to the settings file and return the
    changed ``(section, key)`` pairs, in the order given.

    ``field`` may be a catalog Field, a ``(section, key)`` pair or a
    ``"section.key"`` string. Only catalog fields may change. Refuses —
    writing nothing — when a field is unknown, is currently overridden by
    the environment, the target is a legacy TOML file, or the merged
    result fails the real settings validation.
    """
    environ = os.environ if environ is None else environ
    items = []
    for raw_field, value in dict(changes or {}).items():
        field = field_by_id(raw_field)
        if field is None:
            raise WebUIConfigError(f"{raw_field!r} is not a Web UI catalog field; nothing was written")
        items.append((field, list(value) if isinstance(value, tuple) else value))
    if not items:
        return []
    target = Path(config_path) if config_path is not None else settings_file_path(environ)
    if target.suffix.lower() == ".toml":
        raise WebUIConfigError(f"{target}: the Web UI writes YAML settings files; "
                               "point PRINT_HANDY_CONFIG at a .yaml file or edit the TOML file by hand")
    layers = _layers(home, target, environ)
    for field, _value in items:
        if _provenance(field, layers) == ENVIRONMENT:
            raise WebUIConfigError(
                f"{field.id} is overridden by the environment "
                f"(PRINT_HANDY__{field.section.upper()}__{field.key.upper()} or a legacy name); "
                "change or unset that variable instead — the value was not written to the file")
        if _provenance(field, layers) == USER_FILE:
            raise WebUIConfigError(
                f"{field.id} is set in your own settings file ({settings.user_config_path(environ)}), "
                "which overrides this one; change it there — the value was not written to the file")
    current = settings._read(target) if target.is_file() else {}
    if not isinstance(current, dict):  # pragma: no cover - settings._read enforces this
        raise WebUIConfigError(f"{target}: top-level config must be a mapping")
    candidate = copy.deepcopy(current)
    changed = []
    for field, value in items:
        section = candidate.setdefault(field.section, {})
        if not isinstance(section, dict):
            raise WebUIConfigError(f"{target}: [{field.section}] is not a mapping; nothing was written")
        if section.get(field.key, _MISSING) != value:
            changed.append((field.section, field.key))
        section[field.key] = value
    if not changed:
        _clear_write_warnings()
        return []
    for field, value in items:
        check = _CONTENT_CHECKS.get(field.id)
        if check is not None:
            check(value)
    target.parent.mkdir(parents=True, exist_ok=True)
    text, tolerated = _validate_candidate(candidate, target, home=home, environ=environ)
    stamp = timestamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    _backup(target, stamp)
    _atomic_write(target, text, 0o644)
    _note_tolerated(target, tolerated)
    return changed
