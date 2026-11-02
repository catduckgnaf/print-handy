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
validation failure the file on disk is left byte-identical. Lists and
tables outside the catalog are preserved as parsed mappings; comments in
a hand-edited file are not guaranteed to survive a save, the same
honesty as the setup wizard's merge.
"""
from __future__ import annotations

import copy
import os
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
    home_data = settings._read(_home_path(home))
    file_path = _file_path(local, environ)
    file_data = settings._read(file_path) if file_path is not None and file_path.is_file() else {}
    user_path = settings.user_config_path(environ)
    user_data = settings._read(user_path) if user_path is not None and user_path.is_file() else {}

    def pick(name):
        return (environ.get(f"PRINT_HANDY__PRINTER__{name.upper()}")
                or user_data.get("printer", {}).get(name)
                or file_data.get("printer", {}).get(name)
                or home_data.get("printer", {}).get(name))

    key = settings._model_key(pick("model"), pick("vendor"))
    model_defaults = printers.MODELS[key].defaults()
    base = copy.deepcopy(settings._merge(model_defaults, home_data))
    env_data = settings._from_env(base, environ)
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


def _validate_candidate(candidate, target, *, home, environ):
    """Run the candidate settings-file mapping through the real settings
    loader (written to a temp file settings.load reads), raising
    WebUIConfigError with the loader's own message on any failure."""
    text = _dump(candidate)
    fd, tmp = tempfile.mkstemp(prefix=".webui-candidate.", suffix=".yaml",
                               dir=str(target.parent) if str(target.parent) else ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        settings.load(home=home, local=Path(tmp), environ=environ)
    except settings.SettingsError as exc:
        raise WebUIConfigError(str(exc)) from None
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return text


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
        return []
    target.parent.mkdir(parents=True, exist_ok=True)
    text = _validate_candidate(candidate, target, home=home, environ=environ)
    stamp = timestamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    _backup(target, stamp)
    _atomic_write(target, text, 0o644)
    return changed
