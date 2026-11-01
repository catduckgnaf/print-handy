"""Settings load from YAML, with TOML compatibility during migration.

Load order, later wins key by key:
  1. The printer's defaults: its vendor profile (print_handy/printers/<vendor>/__init__.py) under the
     printer model's own (print_handy/printers/<vendor>/<printer>.py)
  2. config/home.yaml, falling back to config/home.toml
  3. $PRINT_HANDY_CONFIG, else config/local.yaml, falling back to config/local.toml
  4. Environment: PRINT_HANDY__SECTION__KEY, or a legacy name from LEGACY_ENV. Lists stay file-only
     except ENV_LISTS, which take a comma- or space-separated value (#270).

Renamed keys (KEY_ALIASES, #672) accept their old spelling in every layer — file and
environment — and are stored under the current name; the old spelling is deprecated.

The model comes from environment, selected local configuration, then home configuration. [printer] model
is the full "vendor.printer" key, as every existing config has it, or the printer part with [printer]
vendor (printers.resolve_key); either way it is stored as the full key.
Unknown local keys and values with mismatched types fail loudly.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import tomllib
from urllib.parse import urlsplit

try:  # PyYAML preferred; the Hermes plugin runtime ships ruamel.yaml instead (#170)
    import yaml
    _yaml_load = yaml.safe_load
    _YAML_ERRORS = (yaml.YAMLError,)
except ImportError:  # pragma: no cover
    yaml = None
    try:
        from ruamel.yaml import YAML as _RuamelYAML
        from ruamel.yaml.error import YAMLError as _RuamelYAMLError
        _yaml_load = _RuamelYAML(typ="safe", pure=True).load
        _YAML_ERRORS = (_RuamelYAMLError,)
    except ImportError:
        _yaml_load = None
        _YAML_ERRORS = ()
from functools import lru_cache
from pathlib import Path

from . import printers as printer_registry

ROOT = Path(__file__).resolve().parent.parent
HOME = ROOT / "config" / "home.yaml"
LEGACY_HOME = ROOT / "config" / "home.toml"
LOCAL = ROOT / "config" / "local.yaml"
LEGACY_LOCAL = ROOT / "config" / "local.toml"
SHIPPED_DOCKER_CONFIGS = {(ROOT / "config" / "docker.yaml").resolve(), (ROOT / "config" / "docker.toml").resolve()}


class SettingsError(ValueError):
    pass


# Environment names the running home services already set. They keep working unchanged.
LEGACY_ENV = {
    "MOONRAKER_URL": ("services", "moonraker_url"),
    "MOONRAKER_WS_URL": ("services", "moonraker_ws_url"),
    "SPOOLMAN_URL": ("services", "spoolman_url"),
    "OPENVIKING_URL": ("services", "openviking_url"),
    "PRINT_VISION_CHAT_URL": ("services", "vision_chat_url"),
    "PRINT_DISCORD_CHAT_ID": ("discord", "chat_id"),
    "DISCORD_UPLOAD_MAX_BYTES": ("discord", "upload_max_bytes"),
    "DISCORD_POST_PRINT_QUIET_SECONDS": ("discord", "post_print_quiet_s"),
    "DISCORD_DAILY_RESET_HOUR": ("discord", "daily_reset_hour"),
    "MOONRAKER_LISTENER_TICK_SECONDS": ("listener", "tick_s"),
    "IFS_PRESENCE_POLL_SECONDS": ("listener", "ifs_presence_poll_s"),
    "SPOOL_NAG_SECONDS": ("listener", "spool_nag_s"),
    "PRINT_VISION_MODEL": ("vision", "model"),
    "PRINT_TIMELAPSE_ARCHIVE_HOST": ("timelapse", "archive_host"),
    "PRINT_TIMELAPSE_ARCHIVE_DIR": ("timelapse", "archive_dir"),
    "PRINT_TIMELAPSE_SSH_KEY": ("timelapse", "ssh_key"),
    "PRINT_TIMELAPSE_RENDER_WAIT_SECONDS": ("timelapse", "render_wait_s"),
    "PRINT_TIMELAPSE_RENDER_POLL_SECONDS": ("timelapse", "render_poll_s"),
    "PRINT_TIMELAPSE_MAX_BYTES": ("timelapse", "max_bytes"),
    "PRINT_TIMELAPSE_LOCAL_RETENTION_DAYS": ("timelapse", "local_retention_days"),
    "PRINT_TIMELAPSE_PLAN_CLIP_RETENTION_DAYS": ("timelapse", "plan_clip_retention_days"),
    "PRINT_TIMELAPSE_VERTICAL_SHORT": ("timelapse", "vertical_short"),
    "PRINT_TIMELAPSE_VERTICAL_SHORT_MODE": ("timelapse", "vertical_short_mode"),
    "PRINT_TIMELAPSE_VERTICAL_SHORT_OVERLAY": ("timelapse", "vertical_short_overlay"),
    "MAINTENANCE_QUICK_HOURS": ("maintenance", "quick_hours"),
    "MAINTENANCE_FULL_HOURS": ("maintenance", "full_hours"),
    "MAINTENANCE_FULL_DAYS": ("maintenance", "full_days"),
    "HERMES_BIN": ("paths", "hermes_bin"),
    "FFMPEG_BIN": ("paths", "ffmpeg"),
    "PRINT_HANDY_ARCHIVE": ("paths", "archive"),
    "PRINT_RECORDS_VAULT": ("paths", "records_vault"),
}
# Setting keys renamed since installs in the wild were written (#672). The loader
# accepts the old spelling — in a config file and as PRINT_HANDY__SECTION__KEY —
# and stores the value under the current name; within one layer the current name
# wins when both are set. The old spelling is a deprecated alias, not a second
# variable: it never enters the defaults tree or OPTIONAL_KEYS. ifs_slots became
# filament_slots once color_system (#656) carried the meaning the IFS name
# implied; the printer registry had always called the same fact filament_slots.
KEY_ALIASES = {("printer", "ifs_slots"): ("printer", "filament_slots")}


def _apply_key_aliases(layer):
    """Fold deprecated key spellings in one parsed config layer into their current names."""
    for (old_section, old_key), (new_section, new_key) in KEY_ALIASES.items():
        values = layer.get(old_section)
        if not isinstance(values, dict) or old_key not in values:
            continue
        target = layer.setdefault(new_section, {})
        if new_key in target:
            del values[old_key]  # both spellings in one layer: the current name wins
        else:
            target[new_key] = values.pop(old_key)
    return layer


# Sections whose keys are names you choose, not a fixed set.
OPEN_SECTIONS = {"cold_pull", "nozzle_temp_offsets", "material_temps"}
# Keys that can be optionally specified even if absent from base home.toml.
OPTIONAL_KEYS = {("printer", "nozzle_type"): str, ("printer", "tool_count"): int,
                 ("printer", "zmod_delivery_format"): str,
                 ("visual_watch", "enabled"): bool,
                 ("vision", "roast_enabled"): bool,
                 ("vision", "local_detector_enabled"): bool,
                 ("vision", "local_model_path"): str,
                 ("vision", "auto_light_off_minutes"): int,
                 ("vision", "light_object"): str,
                 ("spool", "prompt_brand"): bool,
                 ("services", "octoprint_url"): str,
                 ("bambu", "lan_host"): str, ("bambu", "lan_serial"): str,
                 ("bambu", "lan_access_code_env"): str,
                 ("bambu", "start_confirm_s"): int,
                 ("slicer", "flash_studio_path"): str,
                 ("webui", "enabled"): bool, ("webui", "host"): str,
                 ("webui", "port"): int, ("webui", "access_token_env"): str,
                 ("webui", "controls_enabled"): bool,
                 ("design", "enabled"): bool, ("design", "chat_url"): str,
                 ("design", "model"): str, ("design", "api_key_env"): str,
                 ("design", "timeout_s"): int, ("design", "repair_attempts"): int,
                 ("agent", "chat_url"): str, ("agent", "model"): str,
                 ("agent", "api_key_env"): str, ("agent", "timeout_s"): int,
                 ("timelapse", "mode"): str, ("timelapse", "encoder"): str,
                 ("services", "camera_source"): str,
                 ("live_stream", "enabled"): bool, ("live_stream", "voice_channel_id"): str,
                 ("live_stream", "auto_start_on_print"): bool, ("live_stream", "max_height"): int,
                 ("live_stream", "fps"): int, ("live_stream", "max_bitrate_kbps"): int,
                 ("live_stream", "encoder"): str, ("live_stream", "empty_timeout_s"): int,
                 ("live_stream", "activity_origin"): str,
                 ("flashforge_stock", "host"): str,
                 ("notify", "mqtt_enabled"): bool, ("notify", "mqtt_host"): str,
                 ("notify", "mqtt_port"): int, ("notify", "mqtt_topic"): str,
                 ("notify", "mqtt_username"): str,
                 ("notify", "ntfy_enabled"): bool, ("notify", "ntfy_url"): str,
                 ("notify", "ntfy_topic"): str,
                 ("prusa", "link_host"): str, ("prusa", "api_key_env"): str,
                 ("discord", "denied_user_ids"): str,
                 ("discord", "chat_llm_enabled"): bool,
                 ("discord", "clarify_ui"): bool,
                 ("elegoo_sdcp", "host"): str}
# List settings that may also come from the environment, as a comma- or space-separated value
# (PRINT_HANDY__DELIVERY__METHODS=discord,bambu_handoff,printer). Every other list or table is file
# only. [delivery] methods is here so a Docker operator can opt into the Bambu LAN print button without
# replacing the image's docker.yaml (#270); the value is validated exactly like the YAML list.
ENV_LISTS = {("delivery", "methods")}
# [[printers]] entry keys (#257 Stage 2a, #333; design docs/MULTI-PRINTER-DESIGN.md §2.1). The
# roster is a list, so it is file-only like every other list; scalar keys of a *declared* entry may
# be overridden from the environment with the indexed spelling PRINT_HANDY__PRINTERS__<ID>__<KEY>
# (see _from_env). moonraker_ws_url is an entry key even though the design's table names only the
# other three [services] URLs: it is a settable legacy [services] key today, and omitting it would
# make a config with an explicit websocket URL inexpressible in the roster shape.
PRINTER_ENTRY_STR_KEYS = ("name", "moonraker_url", "moonraker_ws_url", "octoprint_url",
                          "camera_snapshot_url", "lan_host", "lan_serial", "lan_access_code_env",
                          "link_host", "api_key_env", "host", "nozzle_type")
PRINTER_ENTRY_INT_KEYS = ("start_confirm_s", "tool_count")
PRINTER_ENTRY_KEYS = frozenset({"id", "model", "chat_id",
                                *PRINTER_ENTRY_STR_KEYS, *PRINTER_ENTRY_INT_KEYS})
PRINTER_ENTRY_ENV_TYPES = {key: str for key in ("chat_id", *PRINTER_ENTRY_STR_KEYS)}
PRINTER_ENTRY_ENV_TYPES.update({key: int for key in PRINTER_ENTRY_INT_KEYS})
PRINTER_ID = re.compile(r"[a-z0-9][a-z0-9_-]*")
DEFAULT_MATERIAL_TEMPS = {
    "PLA": {"nozzle": 210, "bed": 60}, "PETG": {"nozzle": 240, "bed": 85},
    "ABS": {"nozzle": 245, "bed": 100}, "ASA": {"nozzle": 260, "bed": 105},
    "TPU": {"nozzle": 230, "bed": 50}, "PA": {"nozzle": 260, "bed": 90},
    "PC": {"nozzle": 275, "bed": 110},
}


def _merge(base, extra):
    out = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _read(path):
    """Read YAML or a legacy TOML configuration file."""
    path = Path(path)
    try:
        if path.suffix.lower() == ".toml":
            with open(path, "rb") as f:
                return tomllib.load(f)
        if _yaml_load is None:
            raise SettingsError(f"{path}: PyYAML is required to read YAML config; install pyyaml or use a .toml file")
        with open(path, encoding="utf-8") as f:
            data = _yaml_load(f)
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise SettingsError(f"{path}: top-level config must be a mapping")
        return data
    except (tomllib.TOMLDecodeError, OSError, *_YAML_ERRORS) as exc:
        raise SettingsError(f"{path}: {exc}") from None


def _same_type(expected, value):
    if isinstance(expected, bool) or isinstance(value, bool):
        return isinstance(expected, bool) and isinstance(value, bool)
    if isinstance(expected, (int, float)):
        return isinstance(value, (int, float))
    return type(expected) is type(value)


def _unknown(source, section, key):
    hint = ""
    if (section, key) == ("printer", "family"):
        hint = ' ([printer] family was replaced by model, e.g. model = "klipper.generic"; see README "Printer models")'
    return SettingsError(f"{source}: unknown setting [{section}] {key}{hint}")


def _check_keys(home, extra, source):
    """Reject keys home.toml does not define, and values of the wrong type."""
    for section, values in extra.items():
        if section == "printers":
            # The roster is a list, not a table, and never appears in the defaults tree; its
            # entries are shape-checked in validate() (_check_roster).
            if not isinstance(values, list) or not all(isinstance(entry, dict) for entry in values):
                raise SettingsError(f"{source}: [printers] must be a list of printer entries "
                                    "([[printers]] in TOML, a list under printers: in YAML)")
            continue
        if section not in home or not isinstance(values, dict):
            if (section == "notify" and isinstance(values, dict)
                    and values and all((section, key) in OPTIONAL_KEYS for key in values)):
                for key, value in values.items():
                    expected_type = OPTIONAL_KEYS[(section, key)]
                    if not isinstance(value, expected_type):
                        raise SettingsError(f"{source}: [{section}] {key} should be {expected_type.__name__}, got {value!r}")
                continue
            raise SettingsError(f"{source}: unknown section [{section}]")
        for key, value in values.items():
            if key not in home[section]:
                if section in OPEN_SECTIONS:
                    continue
                if (section, key) in OPTIONAL_KEYS:
                    expected_type = OPTIONAL_KEYS[(section, key)]
                    if not isinstance(value, expected_type):
                        raise SettingsError(f"{source}: [{section}] {key} should be {expected_type.__name__}, got {value!r}")
                    continue
                raise _unknown(source, section, key)
            if not _same_type(home[section][key], value):
                raise SettingsError(f"{source}: [{section}] {key} should be like {home[section][key]!r}, got {value!r}")


def _coerce(expected, raw, name):
    try:
        if isinstance(expected, bool):
            value = raw.strip().lower()
            if value in ("1", "true", "yes", "on"):
                return True
            if value in ("0", "false", "no", "off"):
                return False
            raise SettingsError(f"${name} must be true or false, got {raw!r}")
        if isinstance(expected, int):
            return int(raw)
        if isinstance(expected, float):
            return float(raw)
    except SettingsError:
        raise
    except ValueError:
        raise SettingsError(f"${name} should be a number, got {raw!r}") from None
    if isinstance(expected, str):
        return raw
    raise SettingsError(f"${name}: [list] settings cannot come from the environment")


def _split_list(raw):
    """A comma- or space-separated environment value as a list of strings; empty entries are dropped."""
    return [part for part in re.split(r"[\s,]+", raw.strip()) if part]


def _from_env(home, environ, declared=None):
    out = {}
    # Deprecated environment spellings (#672, KEY_ALIASES): when both spellings of
    # a renamed key are set, the current name wins, so the old one is dropped
    # before anything reads it. The surviving old spelling is folded onto the
    # current key where PRINT_HANDY__ names are parsed below.
    for (old_section, old_key), (new_section, new_key) in KEY_ALIASES.items():
        old_name = f"PRINT_HANDY__{old_section.upper()}__{old_key.upper()}"
        new_name = f"PRINT_HANDY__{new_section.upper()}__{new_key.upper()}"
        if old_name in environ and new_name in environ:
            environ = {name: raw for name, raw in environ.items() if name != old_name}
    # Indexed [[printers]] overrides (#333): `declared` is the roster the config files declare.
    # The environment may retarget scalar keys of a declared entry, never create one — the same
    # refusal _from_env gives an unknown section/key, applied to entry ids.
    declared_ids = {entry.get("id") for entry in declared
                    if isinstance(entry, dict) and isinstance(entry.get("id"), str)} \
        if isinstance(declared, list) else set()
    roster = None
    for name, raw in environ.items():
        if name.startswith("PRINT_HANDY__"):
            parts = name[len("PRINT_HANDY__"):].lower().split("__")
            if len(parts) == 3 and parts[0] == "printers":
                pid, key = parts[1], parts[2]
                if pid not in declared_ids:
                    raise SettingsError(f"${name}: no printer with id {pid!r} is declared in the config "
                                        "file; the environment can override a declared [[printers]] "
                                        "entry but never create one")
                if key in ("id", "model"):
                    raise SettingsError(f"${name}: [printers] {key} is part of the entry's identity in "
                                        "the config file and cannot be overridden from the environment")
                if key not in PRINTER_ENTRY_ENV_TYPES:
                    raise SettingsError(f"${name}: unknown [printers] entry key {key!r} "
                                        f"(entry keys: {', '.join(sorted(PRINTER_ENTRY_KEYS))})")
                if roster is None:
                    roster = copy.deepcopy(declared)
                    out["printers"] = roster
                entry = next(entry for entry in roster
                             if isinstance(entry, dict) and entry.get("id") == pid)
                entry[key] = _coerce(PRINTER_ENTRY_ENV_TYPES[key](), raw, name)
                continue
            if len(parts) != 2:
                raise SettingsError(f"${name} must look like PRINT_HANDY__SECTION__KEY")
            section, key = parts
            section, key = KEY_ALIASES.get((section, key), (section, key))
        elif name in LEGACY_ENV:
            section, key = LEGACY_ENV[name]
        else:
            continue
        if (section, key) in ENV_LISTS:
            # An empty value changes nothing, like every other Docker feature variable left blank;
            # the list itself is checked by validate() (for [delivery] methods: _validate_vendor_choices).
            if raw.strip():
                out.setdefault(section, {})[key] = _split_list(raw)
            continue
        if key not in home.get(section, {}):
            if (section, key) in OPTIONAL_KEYS:
                expected_type = OPTIONAL_KEYS[(section, key)]
                expected = expected_type() if expected_type in (bool, int, float) else ""
                out.setdefault(section, {})[key] = _coerce(expected, raw, name)
                continue
            raise _unknown(f"${name}", section, key)
        out.setdefault(section, {})[key] = _coerce(home[section][key], raw, name)
    return out


# Part gaps and margins as one choice (layout.spacing). "minimum" is the smallest
# set measured safe with the real Orca CLI (tests/slicer/test_spacing_minimums_real.py):
#   arrange_gap_mm / merge_spacing_mm 2.0  Orca accepts 0.5 mm gaps (brims merge); 2 mm
#                                          keeps first-layer squish from bridging parts.
#   arrange_edge_mm / merge_edge_margin_mm 1.0  a part 0.5 mm from the edge slices and
#                                          passes the bed-bounds guard; Orca clips a brim at
#                                          the bed edge, and any brim policy adds its own
#                                          width on top (orca_adapter.required_feature_edge).
#   prime_tower.part_clearance_mm 3.5      Orca reports G-code conflicts at 2.5 mm, accepts 3.
#   prime_tower.edge_mm 4.0                the tower's brim/wipe passes the bed edge at 3 mm,
#                                          fits at 3.5.
#   prime_tower.merge_slack_mm 1.0         the reserved tower strip is a fast path; every tower
#                                          spot is still proved by the tower search.
# "medium" (Standard) is the pre-2.2 fixed set. "low" is halfway between minimum and
# medium and "high" is 1.5 x medium, rounded to 0.5 mm, for big brims or warping materials.
SPACING_KEYS = (("layout", "arrange_gap_mm"), ("layout", "arrange_edge_mm"),
                ("layout", "merge_spacing_mm"), ("layout", "merge_edge_margin_mm"),
                ("prime_tower", "part_clearance_mm"), ("prime_tower", "edge_mm"),
                ("prime_tower", "merge_slack_mm"))
_SPACING_MINIMUM = (2.0, 1.0, 2.0, 1.0, 3.5, 4.0, 1.0)
_SPACING_STANDARD = (5.0, 5.0, 10.0, 8.0, 8.0, 10.0, 5.0)


def _half_mm(value):
    return math.floor(value * 2 + 0.5) / 2


SPACING_PRESETS = {
    "minimum": dict(zip(SPACING_KEYS, _SPACING_MINIMUM)),
    "low": dict(zip(SPACING_KEYS, (_half_mm((a + b) / 2) for a, b in zip(_SPACING_MINIMUM, _SPACING_STANDARD)))),
    "medium": dict(zip(SPACING_KEYS, _SPACING_STANDARD)),
    "high": dict(zip(SPACING_KEYS, (_half_mm(b * 1.5) for b in _SPACING_STANDARD))),
}
SPACING_LABELS = {"minimum": "Minimum", "low": "Low", "medium": "Medium (Standard)", "high": "High"}


def _explicit_keys(*layers):
    """(section, key) pairs the operator set in any of the given layers."""
    return {(section, key) for layer in layers for section, values in (layer or {}).items()
            if isinstance(values, dict) for key in values}


def apply_spacing_preset(data, explicit=()):
    """Fill every spacing key from layout.spacing unless the operator set it explicitly."""
    preset = data.get("layout", {}).get("spacing", "minimum")
    if preset not in SPACING_PRESETS:
        raise SettingsError(f"[layout] spacing must be one of {', '.join(SPACING_PRESETS)}, got {preset!r}")
    for (section, key), value in SPACING_PRESETS[preset].items():
        if (section, key) not in explicit:
            data.setdefault(section, {})[key] = value
    return data


def spacing_summary(data):
    """The active preset and every resolved spacing value, for status output."""
    layout = data.get("layout", {})
    preset = layout.get("spacing", "minimum")
    values = {f"{section}.{key}": data.get(section, {}).get(key) for section, key in SPACING_KEYS}
    overridden = sorted(name for name, value in values.items()
                        if value != SPACING_PRESETS.get(preset, {}).get(tuple(name.split(".", 1))))
    return {"preset": preset, "label": SPACING_LABELS.get(preset, preset), "values": values,
            "overridden": overridden}


def _positive(data, section, key, kind=float):
    try:
        value = data[section][key]
    except KeyError:
        raise SettingsError(f"missing setting [{section}] {key}") from None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise SettingsError(f"[{section}] {key} must be a positive number, got {value!r}")
    if kind is int and value != int(value):
        raise SettingsError(f"[{section}] {key} must be a whole number, got {value!r}")
    return kind(value)


def _model_key(model, vendor=None):
    try:
        return printer_registry.resolve_key(model, vendor)
    except ValueError as exc:
        raise SettingsError(str(exc)) from None


def _validate_vendor_choices(data, model):
    """[printer] slicer and [delivery] methods must be ones the printer's vendor profile offers."""
    vendor = model.vendor
    slicer = data["printer"].setdefault("slicer", vendor.slicers[0])
    if slicer not in vendor.slicers:
        raise SettingsError(f"[printer] slicer {slicer!r} is not available for vendor {vendor.key!r} "
                            f"(available: {', '.join(vendor.slicers)})")
    methods = data.setdefault("delivery", {}).setdefault("methods", list(vendor.default_delivery))
    if not isinstance(methods, list) or not methods or not all(isinstance(m, str) for m in methods):
        raise SettingsError(f"[delivery] methods must be a non-empty list, like {list(vendor.default_delivery)!r}")
    unknown = [m for m in methods if m not in vendor.delivery]
    if unknown:
        raise SettingsError(f"[delivery] methods {unknown} are not offered for vendor {vendor.key!r} "
                            f"(available: {', '.join(vendor.delivery)})")
    # Vendor order, no repeats: the post-slice buttons follow it.
    data["delivery"]["methods"] = [m for m in vendor.delivery if m in methods]


def _validate_zmod_delivery_format(data, model):
    """[printer] zmod_delivery_format (#564): the ZMod family's print-file format choice.

    "gcode" is today's plain .gcode delivery, "3mf" always delivers the sliced
    .gcode.3mf package, and "auto" (the Flashforge vendor default) delivers
    3MF only when the printer reports ZMod 1.7.5+ at print time. The key is
    meaningless on other vendors — their formats are their own profiles' —
    so, like the [bambu] LAN keys, setting it there is refused, not ignored.
    """
    value = data["printer"].get("zmod_delivery_format")
    if value is None:
        return
    if model.vendor.key != "flashforge":
        raise SettingsError("[printer] zmod_delivery_format only applies to Flashforge/ZMod printers")
    if value not in ("gcode", "3mf", "auto"):
        raise SettingsError(f"[printer] zmod_delivery_format must be \"gcode\", \"3mf\" or \"auto\", got {value!r}")


def _validate_bambu_lan(data, model):
    """[bambu] LAN keys (#187 stage b): host and serial together, Bambu models only; start_confirm_s (#271).

    The access code itself is never a setting: [bambu] lan_access_code_env names the environment
    variable that holds it, the same indirection as [discord] token_env and [llm] api_key_env.
    """
    bambu = data.setdefault("bambu", {})
    host = str(bambu.setdefault("lan_host", "") or "").strip()
    serial = str(bambu.setdefault("lan_serial", "") or "").strip().upper()
    env_name = str(bambu.setdefault("lan_access_code_env", "BAMBU_LAN_PASSWORD") or "BAMBU_LAN_PASSWORD")
    if (host or serial) and model.vendor.key != "bambulab":
        raise SettingsError("[bambu] lan_host and lan_serial only apply to Bambu Lab printers")
    if bool(host) != bool(serial):
        raise SettingsError("[bambu] lan_host and lan_serial must be set together (the printer's LAN address "
                            "and the serial number printed on it and shown in Settings > WLAN)")
    if host and not re.fullmatch(r"[A-Za-z0-9._-]+", host):
        raise SettingsError(f"[bambu] lan_host must be a plain IP address or hostname without scheme or port, got {host!r}")
    if serial and not re.fullmatch(r"[A-Z0-9]+", serial):
        raise SettingsError(f"[bambu] lan_serial must be the printer's alphanumeric serial number, got {serial!r}")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
        raise SettingsError(f"[bambu] lan_access_code_env must be an environment variable name, got {env_name!r}")
    bambu["lan_host"], bambu["lan_serial"], bambu["lan_access_code_env"] = host, serial, env_name
    # How long a LAN print start is polled for the printer to report the file (#271). Whole seconds,
    # 1-300: the bot gives the whole print-now child 420 s including the upload, so the window
    # must leave room for a large package.
    bambu.setdefault("start_confirm_s", 90)
    window = _positive(data, "bambu", "start_confirm_s", int)
    if window > 300:
        raise SettingsError(f"[bambu] start_confirm_s must be at most 300 seconds, got {window!r}")
    bambu["start_confirm_s"] = window


def _validate_stock_lan(data, model):
    """[flashforge_stock] host: a plain IP or hostname, and only on the stock model.

    The serial and check code are never settings. They stay in AD5X_SERIAL and
    AD5X_CHECK_CODE, the same environment variables the ZMod /detail reader uses.
    """
    section = data.setdefault("flashforge_stock", {})
    host = str(section.setdefault("host", "") or "").strip()
    if host and model.vendor.key != "flashforge_stock":
        raise SettingsError("[flashforge_stock] host only applies to the stock Flashforge printer")
    if host and not re.fullmatch(r"[A-Za-z0-9._-]+", host):
        raise SettingsError(f"[flashforge_stock] host must be a plain IP address or hostname without scheme or port, got {host!r}")
    section["host"] = host


def _validate_prusa_link(data, model):
    """[prusa] PrusaLink keys (#242): a plain host, Prusa models only.

    The API key itself is never a setting: [prusa] api_key_env names the environment variable
    that holds it, the same indirection as [bambu] lan_access_code_env.
    """
    prusa = data.setdefault("prusa", {})
    host = str(prusa.setdefault("link_host", "") or "").strip()
    env_name = str(prusa.setdefault("api_key_env", "PRUSA_LINK_API_KEY") or "PRUSA_LINK_API_KEY")
    if host and model.vendor.key != "prusa":
        raise SettingsError("[prusa] link_host only applies to Prusa printers")
    if host and not re.fullmatch(r"[A-Za-z0-9._-]+", host):
        raise SettingsError(f"[prusa] link_host must be a plain IP address or hostname without scheme or port, got {host!r}")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
        raise SettingsError(f"[prusa] api_key_env must be an environment variable name, got {env_name!r}")
    prusa["link_host"], prusa["api_key_env"] = host, env_name


def _validate_elegoo_sdcp(data, model):
    """[elegoo_sdcp] host (#243): a plain host, Elegoo SDCP models only. SDCP has no
    credentials at all, so there is no secret to indirect through the environment."""
    section = data.setdefault("elegoo_sdcp", {})
    host = str(section.setdefault("host", "") or "").strip()
    if host and model.vendor.key != "elegoo_sdcp":
        raise SettingsError("[elegoo_sdcp] host only applies to Elegoo SDCP printers (the Centauri line)")
    if host and not re.fullmatch(r"[A-Za-z0-9._-]+", host):
        raise SettingsError(f"[elegoo_sdcp] host must be a plain IP address or hostname without scheme or port, got {host!r}")
    section["host"] = host


def _check_service_urls(services):
    for key, value in services.items():
        if key.endswith("_url") and value and not value.startswith(("ws://", "wss://") if key == "moonraker_ws_url" else ("http://", "https://")):
            raise SettingsError(f"[services] {key} must be a URL, got {value!r}")


def _check_connection_urls(services, model):
    if model.vendor.connection in ("offline", "bambulab_lan", "flashforge_stock", "prusalink", "elegoo_sdcp") and (services["moonraker_url"] or services.get("octoprint_url")):
        # The listener, /print-stop and /print-resume would otherwise act on whatever answers at that URL.
        raise SettingsError(f"[services] moonraker_url and octoprint_url must be empty for model {model.key!r}: "
                            "Print Handy has no Moonraker or OctoPrint connection to it")


def _derive_service_urls(services):
    if services["moonraker_url"]:
        base = services["moonraker_url"].rstrip("/")
        host = base.split("://", 1)[1].split("/", 1)[0].rsplit(":", 1)[0]
        services["moonraker_ws_url"] = services["moonraker_ws_url"] or base.replace("http", "ws", 1) + "/websocket"
        services["camera_snapshot_url"] = services["camera_snapshot_url"] or f"http://{host}/webcam/?action=snapshot"


# ---------------------------------------------------------------------------
# [[printers]]: the multi-printer roster (#257 Stage 2a, #333).
#
# A config file may declare its printers as a list of entries instead of the
# legacy singleton sections. Loading resolves the roster AND keeps the legacy
# singleton view (data["printer"], the [services] URLs, the vendor sections)
# populated from the first entry, so every existing consumer sees exactly what
# it sees today; with no [[printers]] in the file, validate() synthesises a
# one-entry roster from the legacy sections. Migration is in memory only —
# config files are never rewritten by loading. Design:
# docs/MULTI-PRINTER-DESIGN.md §2.1.


def _printer_id_slug(name):
    """The synthesised entry id for a legacy config: slug of [printer] name, else "printer"."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", str(name or "").lower()).strip("-_")
    return slug or "printer"


def _check_roster(raw):
    """Shape-check a [[printers]] list from a config file; return entry stubs.

    A stub carries only the keys the file actually set (chat_id normalised to
    a string, model resolved to its full registry key). _project_entry folds
    the first stub into the legacy sections; _validate_entry checks the rest.
    Every refusal names the entry.
    """
    if not isinstance(raw, list):
        raise SettingsError("[printers] must be a list of printer entries "
                            "([[printers]] in TOML, a list under printers: in YAML)")
    if not raw:
        raise SettingsError("[printers] must name at least one printer")
    stubs, seen = [], set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise SettingsError(f"[printers] entry {index + 1} must be a table of keys, got {entry!r}")
        pid = entry.get("id")
        label = f"[printers] entry {pid!r}" if isinstance(pid, str) and pid else f"[printers] entry {index + 1}"
        unknown = sorted(set(entry) - PRINTER_ENTRY_KEYS)
        if unknown:
            raise SettingsError(f"{label}: unknown key(s) {', '.join(unknown)} "
                                f"(entry keys: {', '.join(sorted(PRINTER_ENTRY_KEYS))})")
        if not isinstance(pid, str) or not PRINTER_ID.fullmatch(pid):
            raise SettingsError(f"[printers] entry {index + 1}: id is required and must be a slug "
                                f"(a-z, 0-9, '-' and '_', starting with a letter or digit), got {pid!r}")
        if pid in seen:
            raise SettingsError(f"[printers] duplicate printer id {pid!r}")
        seen.add(pid)
        if "model" not in entry:
            raise SettingsError(f"[printers] entry {pid!r}: model is required")
        stub = {"id": pid, "model": _model_key(entry["model"])}
        model = printer_registry.MODELS[stub["model"]]
        if entry.get("host") and model.vendor.key not in ("elegoo_sdcp", "flashforge_stock"):
            raise SettingsError(f"[printers] entry {pid!r}: host only applies to Elegoo SDCP and stock "
                                "Flashforge printers (it is the [elegoo_sdcp] / [flashforge_stock] host)")
        for key, value in entry.items():
            if key in ("id", "model"):
                continue
            if key == "chat_id":
                if isinstance(value, bool) or not isinstance(value, (str, int)):
                    raise SettingsError(f"[printers] entry {pid!r}: chat_id must be a channel ID, got {value!r}")
                stub[key] = str(value)
            else:
                stub[key] = value
        stubs.append(stub)
    return stubs


def _project_entry(data, stub):
    """Fold the default (first) roster entry into the legacy singleton sections.

    After this, validate()'s existing checks run on the entry exactly as they
    run on a legacy config, and every legacy consumer reads the entry's values.
    Connection keys the entry does not set are cleared in the vendor sections
    (not left at a value inherited from elsewhere); the vendor validators
    re-apply their own scalar defaults.
    """
    model = printer_registry.MODELS[stub["model"]]
    printer = data.setdefault("printer", {})
    printer["model"], printer["vendor"] = stub["model"], model.vendor.key
    if "name" in stub:
        printer["name"] = stub["name"]
    # nozzle_type / tool_count are [printer] overrides: set only when the entry sets them. The
    # defaults base is built from this entry's model (see load()), so a value already there is
    # that model's own default — the same value a legacy config for the model resolves to.
    for key in ("nozzle_type", "tool_count"):
        if key in stub:
            printer[key] = stub[key]
    services = data.setdefault("services", {})
    for key in ("moonraker_url", "moonraker_ws_url", "camera_snapshot_url"):
        services[key] = stub.get(key, "")
    # octoprint_url is an optional key, absent from the defaults tree: the legacy view only
    # carries it when it is set, so the projection must not invent it for an entry without one.
    if "octoprint_url" in stub:
        services["octoprint_url"] = stub["octoprint_url"]
    else:
        services.pop("octoprint_url", None)
    bambu = data.setdefault("bambu", {})
    bambu["lan_host"], bambu["lan_serial"] = stub.get("lan_host", ""), stub.get("lan_serial", "")
    for key in ("lan_access_code_env", "start_confirm_s"):
        if key in stub:
            bambu[key] = stub[key]
        else:
            bambu.pop(key, None)
    prusa = data.setdefault("prusa", {})
    prusa["link_host"] = stub.get("link_host", "")
    if "api_key_env" in stub:
        prusa["api_key_env"] = stub["api_key_env"]
    else:
        prusa.pop("api_key_env", None)
    host = stub.get("host", "")
    data.setdefault("elegoo_sdcp", {})["host"] = host if model.vendor.key == "elegoo_sdcp" else ""
    data.setdefault("flashforge_stock", {})["host"] = host if model.vendor.key == "flashforge_stock" else ""


def _capture_entry(data, stub):
    """The resolved roster entry for a fully validated settings-shaped view.

    `data` is the real settings for the default entry (or a legacy config's
    one synthesised entry, stub=None) and a scratch view for the rest; either
    way the per-printer values are read back out of the normalised legacy
    sections, so the entry and the legacy view can never disagree.
    """
    model = printer_registry.MODELS[data["printer"]["model"]]
    printer, services = data["printer"], data["services"]
    bambu, prusa = data.get("bambu", {}), data.get("prusa", {})
    if model.vendor.key == "elegoo_sdcp":
        host = data.get("elegoo_sdcp", {}).get("host", "")
    elif model.vendor.key == "flashforge_stock":
        host = data.get("flashforge_stock", {}).get("host", "")
    else:
        host = ""
    entry = {
        "id": stub["id"] if stub else _printer_id_slug(printer.get("name")),
        "model": data["printer"]["model"],
        "name": printer.get("name") or model.label,
        "chat_id": stub.get("chat_id", "") if stub else "",
        "moonraker_url": services.get("moonraker_url", ""),
        "moonraker_ws_url": services.get("moonraker_ws_url", ""),
        "octoprint_url": services.get("octoprint_url", ""),
        "camera_snapshot_url": services.get("camera_snapshot_url", ""),
        "lan_host": bambu.get("lan_host", ""),
        "lan_serial": bambu.get("lan_serial", ""),
        "lan_access_code_env": bambu.get("lan_access_code_env", "BAMBU_LAN_PASSWORD"),
        "start_confirm_s": bambu.get("start_confirm_s", 90),
        "link_host": prusa.get("link_host", ""),
        "api_key_env": prusa.get("api_key_env", "PRUSA_LINK_API_KEY"),
        "host": host,
    }
    for key in ("nozzle_type", "tool_count"):
        if key in printer:
            entry[key] = printer[key]
    return entry


def _entry_default_name(model):
    """An entry's display name when the file does not set one: the name the model's own
    defaults give [printer] (the same string a legacy config for that model resolves to),
    falling back to the registry label."""
    return (model.defaults().get("printer") or {}).get("name") or model.label


def _validate_entry_overrides(scratch, model):
    """An entry's nozzle_type / tool_count overrides, checked as [printer]'s are in validate()."""
    printer = scratch["printer"]
    nozzle_type = printer.get("nozzle_type")
    if nozzle_type is not None:
        from .core.nozzle import NozzleError, normalize_nozzle_type
        try:
            norm_type = normalize_nozzle_type(nozzle_type)
        except NozzleError as exc:
            raise SettingsError(f"[printer] nozzle_type: {exc}") from None
        if norm_type not in model.supported_nozzle_types:
            supported_types = ", ".join(sorted(model.supported_nozzle_types))
            raise SettingsError(f"[printer] nozzle_type {nozzle_type!r} is not supported by model {model.key!r} (supported: {supported_types})")
        printer["nozzle_type"] = norm_type
    if "tool_count" in printer:
        printer["tool_count"] = _positive(scratch, "printer", "tool_count", int)
        if printer["tool_count"] != 1 and model.color_system != "multi_tool":
            raise SettingsError(f"[printer] tool_count must be 1 for model {model.key!r} (single tool)")


def _validate_entry(stub):
    """Validate one non-default [[printers]] entry against its own model.

    The entry is rebuilt as a scratch view shaped like today's singleton
    sections, so the existing vendor validators (which already take a model)
    check it unchanged; the normalised entry is captured back out of the
    scratch view, exactly as the default entry is captured from the real one.
    """
    model = printer_registry.MODELS[stub["model"]]
    scratch = {
        "printer": {"model": stub["model"], "vendor": model.vendor.key,
                    "name": stub.get("name") or _entry_default_name(model)},
        "services": {key: stub.get(key, "") for key in
                     ("moonraker_url", "moonraker_ws_url", "octoprint_url", "camera_snapshot_url")},
        "bambu": {"lan_host": stub.get("lan_host", ""), "lan_serial": stub.get("lan_serial", "")},
        "prusa": {"link_host": stub.get("link_host", "")},
        "elegoo_sdcp": {"host": stub.get("host", "") if model.vendor.key == "elegoo_sdcp" else ""},
        "flashforge_stock": {"host": stub.get("host", "") if model.vendor.key == "flashforge_stock" else ""},
    }
    for key in ("lan_access_code_env", "start_confirm_s"):
        if key in stub:
            scratch["bambu"][key] = stub[key]
    if "api_key_env" in stub:
        scratch["prusa"]["api_key_env"] = stub["api_key_env"]
    for key in ("nozzle_type", "tool_count"):
        if key in stub:
            scratch["printer"][key] = stub[key]
    try:
        _check_service_urls(scratch["services"])
        _check_connection_urls(scratch["services"], model)
        _validate_bambu_lan(scratch, model)
        _validate_stock_lan(scratch, model)
        _validate_prusa_link(scratch, model)
        _validate_elegoo_sdcp(scratch, model)
        _derive_service_urls(scratch["services"])
        _validate_entry_overrides(scratch, model)
    except SettingsError as exc:
        raise SettingsError(f"[printers] entry {stub['id']!r}: {exc}") from None
    return _capture_entry(scratch, stub)


def _check_chat_claims(data, entries):
    """Shared claims are legal (#536): two entries claiming the same chat make it a
    shared channel, where printer-bound handlers ask which printer instead of the
    channel deciding. There is nothing to refuse here; the claim model itself lives
    in print_handy.bot.routing (channel_claims / shared_channel_map). Kept as a
    named validation step so the roster pass reads the same either way."""


# Legacy per-printer locations a declared roster replaces, as environment names.
_MIXED_ENV_NAMES = ("PRINT_HANDY__PRINTER__MODEL", "PRINT_HANDY__PRINTER__NAME",
                    "PRINT_HANDY__PRINTER__VENDOR", "PRINT_HANDY__PRINTER__NOZZLE_TYPE",
                    "PRINT_HANDY__PRINTER__TOOL_COUNT", "PRINT_HANDY__SERVICES__MOONRAKER_URL",
                    "PRINT_HANDY__SERVICES__MOONRAKER_WS_URL", "PRINT_HANDY__SERVICES__OCTOPRINT_URL",
                    "PRINT_HANDY__SERVICES__CAMERA_SNAPSHOT_URL", "PRINT_HANDY__BAMBU__LAN_HOST",
                    "PRINT_HANDY__BAMBU__LAN_SERIAL", "PRINT_HANDY__BAMBU__START_CONFIRM_S",
                    "PRINT_HANDY__PRUSA__LINK_HOST", "PRINT_HANDY__ELEGOO_SDCP__HOST",
                    "PRINT_HANDY__FLASHFORGE_STOCK__HOST", "MOONRAKER_URL", "MOONRAKER_WS_URL")
_MIXED_ENV_DEFAULTED = (("PRINT_HANDY__BAMBU__LAN_ACCESS_CODE_ENV", "BAMBU_LAN_PASSWORD"),
                        ("PRINT_HANDY__PRUSA__API_KEY_ENV", "PRUSA_LINK_API_KEY"))


def _check_mixed_shape(extra, environ):
    """Refuse a config that declares [[printers]] AND sets legacy per-printer locations.

    Only the operator's own layers are checked — the local file and the
    environment. The shipped defaults tree (config/home.yaml) always carries
    legacy values; those are the base the roster replaces, not a second
    roster. The refusal names every offending location.
    """
    def section(name):
        values = extra.get(name)
        return values if isinstance(values, dict) else {}

    offenders = []
    printer = section("printer")
    for key in ("model", "name", "vendor"):
        if printer.get(key):
            offenders.append(f"[printer] {key}")
    for key in ("nozzle_type", "tool_count"):
        if printer.get(key) is not None:
            offenders.append(f"[printer] {key}")
    services = section("services")
    for key in ("moonraker_url", "moonraker_ws_url", "octoprint_url", "camera_snapshot_url"):
        if services.get(key):
            offenders.append(f"[services] {key}")
    bambu = section("bambu")
    for key in ("lan_host", "lan_serial"):
        if bambu.get(key):
            offenders.append(f"[bambu] {key}")
    if bambu.get("lan_access_code_env") not in (None, "", "BAMBU_LAN_PASSWORD"):
        offenders.append("[bambu] lan_access_code_env")
    if bambu.get("start_confirm_s") is not None:
        offenders.append("[bambu] start_confirm_s")
    prusa = section("prusa")
    if prusa.get("link_host"):
        offenders.append("[prusa] link_host")
    if prusa.get("api_key_env") not in (None, "", "PRUSA_LINK_API_KEY"):
        offenders.append("[prusa] api_key_env")
    if section("elegoo_sdcp").get("host"):
        offenders.append("[elegoo_sdcp] host")
    if section("flashforge_stock").get("host"):
        offenders.append("[flashforge_stock] host")
    for name in _MIXED_ENV_NAMES:
        if environ.get(name):
            offenders.append(f"${name}")
    for name, default in _MIXED_ENV_DEFAULTED:
        value = environ.get(name)
        if value and value != default:
            offenders.append(f"${name}")
    if offenders:
        raise SettingsError("[[printers]] is declared, so the legacy per-printer settings must be "
                            "absent or empty; move these into the [[printers]] entry they belong to: "
                            + ", ".join(offenders))


BOT_MODES = ("hermes", "standalone")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
LOG_FORMATS = ("text", "json")
# [timelapse] mode (#246/#295): "rendered" keeps the printer-rendered
# moonraker-timelapse path, "container" is #246's in-container capture,
# "off" disables timelapses. An empty file value derives in validate():
# "rendered" when a Moonraker URL is configured, else "container" when
# in-container capture is enabled, else "off". The mode is resolved
# here; the gates that consume it live in print_handy.go2rtc
# (capture_active / rendered_active, 2.2.2): "off" vetoes both paths,
# "container" opens the capture gate and closes the rendered path, and
# "rendered" leaves the capture gate to capture_enabled exactly as
# before.
TIMELAPSE_MODES = ("rendered", "container", "off")
# [timelapse] capture_trigger (#246 stage 2): how in-container frame capture
# decides a frame is due. "interval" is the default (printer-agnostic);
# "layer" captures on Moonraker layer changes (moonraker-timelapse
# semantics); "both" fires on either. The owner's trigger preference on
# #246 is still open, so the trigger is a setting, not a hard-coded choice.
CAPTURE_TRIGGERS = ("interval", "layer", "both")
# [timelapse] capture_encoder (#246): the Stage 3 encode preference,
# validated now so Stage 3 needs no settings migration. Capture itself
# (Stage 2) never encodes; "auto" will probe in the order vaapi, nvenc,
# qsv and fall back to cpu.
CAPTURE_ENCODERS = ("auto", "vaapi", "nvenc", "qsv", "cpu")
# [timelapse] encoder (#314): which ffmpeg encoder the timelapse sizing
# shrink uses. "cpu" (libx264) is the default and always available; the
# hardware choices are live_stream's vendor names (#311) and need the
# GPU passed through to the container (docs/DOCKER.md). A hardware
# encode that fails falls back to CPU in the listener — it never fails
# a completion post.
TIMELAPSE_ENCODERS = ("cpu", "vaapi", "qsv", "nvenc")
# [services] camera_source (#294): which camera the still images come
# from. "auto" follows the printer vendor's rule; the explicit values pin
# one source; "none" turns camera images off. The pin is consumed by
# print_handy.go2rtc.resolve_source (the fan-out source selection), by
# the stills fetch in the Moonraker listener, and by the Bambu adapter's
# camera paths (2.2.2).
CAMERA_SOURCES = ("auto", "moonraker", "bambu", "go2rtc", "none")


def user_ids(value, section="discord", platform_label="Discord", key="allowed_user_ids"):
    """User IDs from a comma or space separated string. A non-numeric entry is an error."""
    ids = [part for part in re.split(r"[\s,]+", str(value or "").strip()) if part]
    for part in ids:
        if not part.isdigit():
            raise SettingsError(f"[{section}] {key} must be numeric {platform_label} user IDs, got {part!r}")
    return ids


def _validate_telegram(data):
    """The [telegram] section (#164 stage 2). Absent in an older config means the defaults.

    Telegram group and channel IDs are negative, so chat_id is a signed
    integer kept as a string; user IDs are positive integers. The token
    itself is never a setting: token_env names the environment variable
    holding it, the same indirection as [discord] token_env.
    """
    telegram = data.setdefault("telegram", {})
    telegram.setdefault("token_env", "TELEGRAM_BOT_TOKEN")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(telegram["token_env"])):
        raise SettingsError(f"[telegram] token_env must be an environment variable name, got {telegram['token_env']!r}")
    chat = str(telegram.setdefault("chat_id", "") or "").strip()
    if chat and not re.fullmatch(r"-?\d+", chat):
        raise SettingsError(f"[telegram] chat_id must be a numeric Telegram chat ID "
                            f"(group and channel IDs are negative), got {chat!r}")
    telegram["chat_id"] = chat
    user_ids(telegram.setdefault("allowed_user_ids", ""), "telegram", "Telegram")
    approver = str(telegram.setdefault("approval_user_id", "") or "").strip()
    if approver and not approver.isdigit():
        raise SettingsError(f"[telegram] approval_user_id must be a numeric Telegram user ID, got {approver!r}")
    telegram["approval_user_id"] = approver
    telegram.setdefault("upload_max_bytes", 50_000_000)
    telegram["upload_max_bytes"] = _positive(data, "telegram", "upload_max_bytes", int)


MATRIX_ID = r"@[A-Za-z0-9._=+/\-]+:[A-Za-z0-9.\-]+(?::[0-9]+)?"


def matrix_user_ids(value, section="matrix"):
    """Matrix user IDs from a comma or space separated string of full @user:server IDs."""
    ids = [part for part in re.split(r"[\s,]+", str(value or "").strip()) if part]
    for part in ids:
        if not re.fullmatch(MATRIX_ID, part):
            raise SettingsError(f"[{section}] allowed_user_ids must be full Matrix user IDs "
                                f"like '@name:matrix.org', got {part!r}")
    return ids


def _validate_matrix(data):
    """The [matrix] section (#164 stage 3). Absent in an older config means the defaults.

    Matrix user IDs are full ``@user:server`` IDs, not numbers, and a room ID
    starts with ``!``. The access token itself is never a setting:
    access_token_env names the environment variable holding it, the same
    indirection as [discord] token_env.
    """
    matrix = data.setdefault("matrix", {})
    url = str(matrix.setdefault("homeserver_url", "") or "").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        raise SettingsError(f"[matrix] homeserver_url must be an http(s) URL, got {url!r}")
    matrix["homeserver_url"] = url
    matrix.setdefault("access_token_env", "MATRIX_ACCESS_TOKEN")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(matrix["access_token_env"])):
        raise SettingsError(f"[matrix] access_token_env must be an environment variable name, "
                            f"got {matrix['access_token_env']!r}")
    room = str(matrix.setdefault("room_id", "") or "").strip()
    if room and not room.startswith("!"):
        raise SettingsError(f"[matrix] room_id must be a Matrix room ID starting with '!', got {room!r}")
    matrix["room_id"] = room
    matrix_user_ids(matrix.setdefault("allowed_user_ids", ""))
    for key in ("approval_user_id", "bot_user_id"):
        value = str(matrix.setdefault(key, "") or "").strip()
        if value and not re.fullmatch(MATRIX_ID, value):
            raise SettingsError(f"[matrix] {key} must be a full Matrix user ID like "
                                f"'@name:matrix.org', got {value!r}")
        matrix[key] = value
    matrix.setdefault("upload_max_bytes", 50_000_000)
    matrix["upload_max_bytes"] = _positive(data, "matrix", "upload_max_bytes", int)


def _validate_webui(data):
    """The [webui] section (#286). Absent in an older config means the defaults.

    The Web UI is optional and off by default. ``port`` 0 means no port has
    been chosen yet (the same convention as [bot] health_port); a later
    stage's server only listens when enabled is true. The access token
    itself is never a setting: access_token_env names the environment
    variable holding it, the same indirection as [discord] token_env.
    """
    webui = data.setdefault("webui", {})
    enabled = webui.setdefault("enabled", False)
    if not isinstance(enabled, bool):
        raise SettingsError(f"[webui] enabled must be true or false, got {enabled!r}")
    host = str(webui.setdefault("host", "127.0.0.1") or "").strip()
    if not host:
        host = "127.0.0.1"
    if not re.fullmatch(r"[A-Za-z0-9._-]+", host):
        raise SettingsError(f"[webui] host must be a plain IP address or hostname without scheme or port, got {host!r}")
    webui["host"] = host
    port = webui.setdefault("port", 0)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise SettingsError(f"[webui] port must be a port number from 0 to 65535, got {port!r}")
    env_name = str(webui.setdefault("access_token_env", "WEBUI_ACCESS_TOKEN") or "WEBUI_ACCESS_TOKEN")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
        raise SettingsError(f"[webui] access_token_env must be an environment variable name, got {env_name!r}")
    webui["access_token_env"] = env_name
    # require_auth = false is the explicit opt-out for a trusted network:
    # the access token is then neither required nor checked, even on a
    # non-loopback bind. On by default; a missing key means true.
    require_auth = webui.setdefault("require_auth", True)
    if not isinstance(require_auth, bool):
        raise SettingsError(f"[webui] require_auth must be true or false, got {require_auth!r}")
    # controls_enabled gates the Web UI control API (#708): off by
    # default, and while false the server serves no control routes at
    # all. On by explicit operator choice only — a token holder can
    # then pause / resume / cancel and set temperatures with no chat
    # confirmation (chat control stays confirm-first either way).
    controls_enabled = webui.setdefault("controls_enabled", False)
    if not isinstance(controls_enabled, bool):
        raise SettingsError(f"[webui] controls_enabled must be true or false, got {controls_enabled!r}")


VERTICAL_SHORT_MODES = ("pad", "crop")


def _validate_timelapse(data):
    """The [timelapse] section: the capture mode (#246/#295), the
    vertical-short switches (#317) and the sizing encoder (#314).
    Absent keys get the defaults.

    The mode derives from the configured services when absent or empty:
    "rendered" (the printer-rendered moonraker-timelapse path) when a
    Moonraker URL is set, else "container" when in-container capture is
    enabled, else "off". The middle case matters since 2.2.2, when the
    mode started gating the paths (print_handy.go2rtc.capture_active /
    rendered_active): a non-Moonraker install with capture on IS in
    container mode, and deriving "off" there would have vetoed the
    capture it explicitly enabled. An explicit value always wins,
    including "off" over an enabled capture."""
    timelapse = data.setdefault("timelapse", {})
    timelapse.setdefault("capture_enabled", False)
    raw = timelapse.setdefault("mode", "")
    mode = str(raw or "").strip().lower()
    if not mode:
        if data["services"].get("moonraker_url"):
            mode = "rendered"
        elif timelapse.get("capture_enabled"):
            mode = "container"
        else:
            mode = "off"
    if mode not in TIMELAPSE_MODES:
        raise SettingsError(f"[timelapse] mode must be one of {', '.join(TIMELAPSE_MODES)}, got {raw!r}")
    timelapse["mode"] = mode
    for key, default in (("vertical_short", False), ("vertical_short_overlay", True)):
        value = timelapse.setdefault(key, default)
        if not isinstance(value, bool):
            raise SettingsError(f"[timelapse] {key} must be true or false, got {value!r}")
    short_mode = str(timelapse.setdefault("vertical_short_mode", "pad") or "").strip().lower()
    if short_mode not in VERTICAL_SHORT_MODES:
        raise SettingsError(
            f"[timelapse] vertical_short_mode must be one of {', '.join(VERTICAL_SHORT_MODES)}, got {short_mode!r}")
    timelapse["vertical_short_mode"] = short_mode
    # In-container capture (#246 stages 1-2). capture_enabled and
    # capture_source_url arrived with Stage 1 (the go2rtc gate reads them
    # with its own defaults); the Stage 2 keys are normalised here so a
    # config file that predates them — including config/docker.yaml, which
    # carries only [timelapse] mode — still loads with the defaults.
    # (capture_enabled itself is setdefault'd above: the mode derivation
    # reads it.)
    timelapse.setdefault("capture_source_url", "")
    trigger = str(timelapse.setdefault("capture_trigger", "interval") or "interval").strip().lower()
    if trigger not in CAPTURE_TRIGGERS:
        raise SettingsError(
            f"[timelapse] capture_trigger must be one of {', '.join(CAPTURE_TRIGGERS)}, got {trigger!r}")
    timelapse["capture_trigger"] = trigger
    encoder = str(timelapse.setdefault("capture_encoder", "auto") or "auto").strip().lower()
    if encoder not in CAPTURE_ENCODERS:
        raise SettingsError(
            f"[timelapse] capture_encoder must be one of {', '.join(CAPTURE_ENCODERS)}, got {encoder!r}")
    timelapse["capture_encoder"] = encoder
    interval = timelapse.setdefault("capture_interval_s", 5.0)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) \
            or not 0 < float(interval) <= 3600:
        raise SettingsError(
            f"[timelapse] capture_interval_s must be a number of seconds above 0 and at most 3600, "
            f"got {interval!r}")
    timelapse["capture_interval_s"] = float(interval)
    for key, default in (("capture_max_frames", 3600), ("capture_max_bytes", 512 * 1024 * 1024)):
        value = timelapse.setdefault(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SettingsError(
                f"[timelapse] {key} must be a whole number, 0 or more (0 disables the cap), got {value!r}")
    raw_encoder = timelapse.setdefault("encoder", "cpu")
    encoder = str(raw_encoder or "").strip().lower()
    if encoder not in TIMELAPSE_ENCODERS:
        raise SettingsError(
            f"[timelapse] encoder must be one of {', '.join(TIMELAPSE_ENCODERS)}, got {raw_encoder!r}")
    timelapse["encoder"] = encoder


def _validate_camera_source(data):
    """The [services] camera_source (#294): "auto" follows the configured
    printer vendor's camera rule; an explicit value pins one source."""
    services = data.setdefault("services", {})
    raw = services.setdefault("camera_source", "auto")
    source = str(raw or "auto").strip().lower()
    if source not in CAMERA_SOURCES:
        raise SettingsError(f"[services] camera_source must be one of {', '.join(CAMERA_SOURCES)}, got {raw!r}")
    services["camera_source"] = source


LIVE_STREAM_ENCODERS = ("auto", "vaapi", "qsv", "nvenc", "cpu")


def _validate_live_stream(data):
    """The [live_stream] section (#311 stage 3). Absent in an older config means the defaults.

    Live camera viewing in Discord is optional and off by default; while
    enabled is false the bot's /print-live command only reports that.
    voice_channel_id is the one Discord voice channel the live Activity is
    launched in — a snowflake kept as a string, empty until the operator
    picks the channel; the bot never names another channel. The encoder
    preference reuses the stage 2 plan's names in print_handy.live_stream.
    activity_origin is the public https origin the Discord Activity page
    is served from (reached through the named Cloudflare Tunnel and the
    Developer Portal URL mapping, stage 4); port is the loopback port
    the supervised Activity server binds inside the deployment, 0 when
    no port has been chosen yet. No secret is a setting here: the
    Discord client secret and the Activity session secret live only in
    the environment (print_handy.live_activity).
    """
    live = data.setdefault("live_stream", {})
    enabled = live.setdefault("enabled", False)
    if not isinstance(enabled, bool):
        raise SettingsError(f"[live_stream] enabled must be true or false, got {enabled!r}")
    auto = live.setdefault("auto_start_on_print", False)
    if not isinstance(auto, bool):
        raise SettingsError(f"[live_stream] auto_start_on_print must be true or false, got {auto!r}")
    channel = str(live.setdefault("voice_channel_id", "") or "").strip()
    if channel and not channel.isdigit():
        raise SettingsError(f"[live_stream] voice_channel_id must be a numeric Discord channel ID, got {channel!r}")
    live["voice_channel_id"] = channel
    live.setdefault("max_height", 720)
    height = _positive(data, "live_stream", "max_height", int)
    if not 144 <= height <= 2160:
        raise SettingsError(f"[live_stream] max_height must be between 144 and 2160, got {height!r}")
    live["max_height"] = height
    live.setdefault("fps", 15)
    fps = _positive(data, "live_stream", "fps", int)
    if fps > 60:
        raise SettingsError(f"[live_stream] fps must be at most 60, got {fps!r}")
    live["fps"] = fps
    live.setdefault("max_bitrate_kbps", 2500)
    bitrate = _positive(data, "live_stream", "max_bitrate_kbps", int)
    if not 100 <= bitrate <= 20000:
        raise SettingsError(f"[live_stream] max_bitrate_kbps must be between 100 and 20000, got {bitrate!r}")
    live["max_bitrate_kbps"] = bitrate
    encoder = str(live.setdefault("encoder", "auto") or "auto").strip().lower()
    if encoder not in LIVE_STREAM_ENCODERS:
        raise SettingsError(f"[live_stream] encoder must be one of {', '.join(LIVE_STREAM_ENCODERS)}, "
                            f"got {live['encoder']!r}")
    live["encoder"] = encoder
    live.setdefault("empty_timeout_s", 300)
    live["empty_timeout_s"] = _positive(data, "live_stream", "empty_timeout_s", int)
    live.setdefault("port", 0)
    port = live["port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise SettingsError(f"[live_stream] port must be a whole number from 0 to 65535, got {port!r}")
    origin = str(live.setdefault("activity_origin", "") or "").strip().rstrip("/")
    if origin and not origin.startswith("https://"):
        raise SettingsError(f"[live_stream] activity_origin must be an https URL or empty, got {origin!r}")
    live["activity_origin"] = origin


def _validate_vision(data):
    """The [vision] section: camera-check and roast models, and the roast switch.

    ``roast_enabled`` is on by default: whenever an LLM/vision endpoint is
    available (``services.vision_chat_url`` set, with its ``CLIPROXY_API_KEY``),
    the print-started post gains the one-line roast — exactly one per print,
    added when printing begins, never on the completion post — on
    every platform — Telegram included, even though Telegram never uses the
    ``[llm]`` chat fallback. Set it false to turn roasts off without
    disabling the camera watchdog, which shares the same endpoint. Absent
    in an older config means the default (on).
    """
    vision = data.setdefault("vision", {})
    enabled = vision.setdefault("roast_enabled", True)
    if not isinstance(enabled, bool):
        raise SettingsError(f"[vision] roast_enabled must be true or false, got {enabled!r}")
    # Local failure detector (#455): off by default. When on, the camera
    # watchdog's verdicts can come from the built-in ONNX detector
    # (hermes.local_vision) on installs with no vision endpoint route
    # configured; endpoint routes keep precedence. local_model_path names
    # the weights file when the operator supplies their own — empty uses
    # the pinned model, downloaded once into the model directory.
    local_detector = vision.setdefault("local_detector_enabled", False)
    if not isinstance(local_detector, bool):
        raise SettingsError(f"[vision] local_detector_enabled must be true or false, "
                            f"got {local_detector!r}")
    local_model_path = vision.setdefault("local_model_path", "")
    if not isinstance(local_model_path, str):
        raise SettingsError(f"[vision] local_model_path must be a path string or empty, "
                            f"got {local_model_path!r}")
    vision["local_model_path"] = local_model_path.strip()
    # Auto light-off (#516): off by default (0). When set above zero, the
    # printer's light turns on at print start and, after a print ends,
    # off again once the camera verdict has seen an empty plate for
    # that many minutes. It needs a vision endpoint (the same
    # vision_ready gate as the camera watchdog) and a light target,
    # which is family-shaped: on Moonraker, light_object names one
    # ("led chamber_led") or empty auto-discovers (discovery only acts
    # when the printer lists exactly one light object); on Bambu Lab
    # the target is the chamber light and on Elegoo SDCP its Cmd 403
    # light — light_object does not apply to either. The strict
    # Moonraker name validation lives in hermes.light_control (the
    # listener refuses an unusable name there); here the shape is just
    # int / string.
    minutes = vision.setdefault("auto_light_off_minutes", 0)
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 0:
        raise SettingsError(f"[vision] auto_light_off_minutes must be a whole number of minutes "
                            f"from 0 (off) up, got {minutes!r}")
    light_object = vision.setdefault("light_object", "")
    if not isinstance(light_object, str):
        raise SettingsError(f"[vision] light_object must be a Moonraker light object name "
                            f"like 'led chamber_led' or empty, got {light_object!r}")
    vision["light_object"] = light_object.strip()


def _validate_notify(data):
    """The [notify] section (#622): print events out to MQTT and ntfy.

    Both channels are off by default and independent: either, both or
    neither may be on. The section joined the defaults tree in 2.2.2
    (it was OPTIONAL_KEYS-only before, so the section appeared in loaded
    settings only when a file set it). Neither channel's credential is a
    setting: the MQTT password is MQTT_PASSWORD in the environment and
    the ntfy access token is NTFY_ACCESS_TOKEN, the same env-only rule
    as every other secret. mqtt_port 0 reads as the default 1883 at
    publish time (notify.py), the port convention the shipped docs
    already promised.
    """
    notify = data.setdefault("notify", {})
    for key in ("mqtt_enabled", "ntfy_enabled"):
        value = notify.setdefault(key, False)
        if not isinstance(value, bool):
            raise SettingsError(f"[notify] {key} must be true or false, got {value!r}")
    host = notify.setdefault("mqtt_host", "")
    if not isinstance(host, str):
        raise SettingsError(f"[notify] mqtt_host must be a broker hostname or empty, got {host!r}")
    notify["mqtt_host"] = host.strip()
    port = notify.setdefault("mqtt_port", 1883)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise SettingsError(f"[notify] mqtt_port must be a port number from 0 to 65535 "
                            f"(0 uses 1883), got {port!r}")
    for key, default in (("mqtt_topic", "print-handy"), ("mqtt_username", ""), ("ntfy_topic", "")):
        value = notify.setdefault(key, default)
        if not isinstance(value, str):
            raise SettingsError(f"[notify] {key} must be a string, got {value!r}")
        notify[key] = value.strip()
    notify["mqtt_topic"] = notify["mqtt_topic"].strip("/") or "print-handy"
    url = str(notify.setdefault("ntfy_url", "") or "").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        raise SettingsError(f"[notify] ntfy_url must be an http(s) URL or empty, got {url!r}")
    notify["ntfy_url"] = url
    if notify["ntfy_enabled"] and not url:
        raise SettingsError("[notify] ntfy_enabled is true but ntfy_url is empty; "
                            "set the ntfy server (for example https://ntfy.sh) or turn ntfy off")
    if notify["ntfy_enabled"] and not notify["ntfy_topic"] and not urlsplit(url).path.strip("/"):
        raise SettingsError("[notify] ntfy_enabled is true but no ntfy topic is set; "
                            "set ntfy_topic, or include the topic in ntfy_url "
                            "(for example https://ntfy.sh/my-topic), or turn ntfy off")


def _validate_spool(data):
    """The [spool] section (#512): new-spool intake preferences.

    ``prompt_brand`` is off by default: the conversational new-spool
    walkthrough skips the brand/vendor question entirely (the spool is
    created with no brand) unless the operator sets it true because they
    track brands. Absent in an older config means the default (off).
    """
    spool = data.setdefault("spool", {})
    prompt_brand = spool.setdefault("prompt_brand", False)
    if not isinstance(prompt_brand, bool):
        raise SettingsError(f"[spool] prompt_brand must be true or false, got {prompt_brand!r}")


def _validate_design(data):
    """The [design] section (#388 Stage 1; auto-on posture from the close-out ruling).

    ``enabled`` is tri-state. Absent (None here) means AUTO: design turns
    on whenever a design/LLM endpoint is resolvable — every mode resolves
    [design] inheriting [llm] first; Hermes mode additionally counts the
    plugin's ``print_design`` auxiliary-task lane as available — the same
    posture roasts have via ``vision.roast_enabled`` (on whenever an
    endpoint is available).
    ``enabled = true`` forces it on (an unconfigured endpoint is then a
    visible failure, not a silent off), and ``enabled = false`` turns it
    off in all cases. ``chat_url``, ``model`` and ``api_key_env`` are empty
    to inherit the [llm] values, so the common single-endpoint setup only
    sets ``design.model`` (a stronger model for design turns). The Hermes
    host's own LLM lane (with its registered ``print_design`` auxiliary
    task, pinned in the profile's ``auxiliary.print_design`` block) is
    only the fallback for a Hermes install with no endpoint configured
    (#524 Stage 3; see docs/DESIGN.md). The shipped
    config files deliberately carry no ``enabled`` line, so a config that
    never mentions it gets the auto posture; the effective state is
    resolved by ``print_handy.core.design.effective_enabled``.
    """
    design = data.setdefault("design", {})
    enabled = design.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise SettingsError(f"[design] enabled must be true or false, got {enabled!r}")
    design["enabled"] = enabled
    chat_url = str(design.setdefault("chat_url", "") or "").strip()
    if chat_url and not chat_url.startswith(("http://", "https://")):
        raise SettingsError(f"[design] chat_url must be a URL, got {chat_url!r}")
    design["chat_url"] = chat_url
    model = design.setdefault("model", "")
    if not isinstance(model, str):
        raise SettingsError(f"[design] model must be a model name or empty, got {model!r}")
    design["model"] = model.strip()
    env_name = str(design.setdefault("api_key_env", "") or "").strip()
    if env_name and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
        raise SettingsError(f"[design] api_key_env must be an environment variable name, got {env_name!r}")
    design["api_key_env"] = env_name
    design.setdefault("timeout_s", 180)
    design["timeout_s"] = _positive(data, "design", "timeout_s", int)
    attempts = design.setdefault("repair_attempts", 3)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 0 <= attempts <= 10:
        raise SettingsError(f"[design] repair_attempts must be a whole number from 0 to 10, got {attempts!r}")
    design["repair_attempts"] = attempts


def _validate_logging(data):
    """The [logging] section (#150). Absent in a hand-made home file means the defaults."""
    logging_cfg = data.setdefault("logging", {})
    level = str(logging_cfg.setdefault("level", "INFO")).upper()
    if level not in LOG_LEVELS:
        raise SettingsError(f"[logging] level must be one of {', '.join(LOG_LEVELS)}, got {logging_cfg['level']!r}")
    logging_cfg["level"] = level
    kind = str(logging_cfg.setdefault("format", "text")).lower()
    if kind not in LOG_FORMATS:
        raise SettingsError(f"[logging] format must be one of {', '.join(LOG_FORMATS)}, got {logging_cfg['format']!r}")
    logging_cfg["format"] = kind
    path = logging_cfg.setdefault("file", "")
    if not isinstance(path, str):
        raise SettingsError(f"[logging] file must be a path or empty, got {path!r}")
    # Per-subsystem verbosity (#390 stage 4): {logger name: level}, so one
    # noisy subsystem can be raised without a wall of debug output.
    levels = logging_cfg.setdefault("levels", {})
    if not isinstance(levels, dict):
        raise SettingsError(f"[logging] levels must be a table of logger name to level, got {levels!r}")
    for name, value in levels.items():
        normalised = str(value).upper()
        if normalised not in LOG_LEVELS:
            raise SettingsError(f"[logging] levels.{name} must be one of "
                                f"{', '.join(LOG_LEVELS)}, got {value!r}")
        levels[name] = normalised


def _validate_bot(data):
    """The standalone bot sections (#73). Absent sections in a hand-made home file get the defaults."""
    bot = data.setdefault("bot", {})
    bot.setdefault("mode", "standalone")
    if bot["mode"] not in BOT_MODES:
        raise SettingsError(f"[bot] mode must be one of {', '.join(BOT_MODES)}, got {bot['mode']!r}")
    # The chat platform the standalone bot talks to (#164). The known set is
    # owned by print_handy.bot.platform; a config without the key (every
    # config written before #164) keeps the Discord default.
    from .bot.platform import KNOWN_PLATFORMS
    platform = str(bot.setdefault("platform", "discord")).strip().lower()
    if platform not in KNOWN_PLATFORMS:
        raise SettingsError(f"[bot] platform must be one of {', '.join(KNOWN_PLATFORMS)}, got {bot['platform']!r}")
    bot["platform"] = platform
    port = bot.setdefault("health_port", 0)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise SettingsError(f"[bot] health_port must be a port number from 0 to 65535, got {port!r}")
    bot.setdefault("health_stale_s", 120)
    bot["health_stale_s"] = _positive(data, "bot", "health_stale_s", int)
    bot.setdefault("sync_commands", True)
    discord = data["discord"]
    discord.setdefault("token_env", "DISCORD_BOT_TOKEN")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(discord["token_env"])):
        raise SettingsError(f"[discord] token_env must be an environment variable name, got {discord['token_env']!r}")
    user_ids(discord.setdefault("allowed_user_ids", ""))
    user_ids(discord.setdefault("denied_user_ids", ""), key="denied_user_ids")
    # Discord's chat-LLM switch (#466): on by default, so a configured [llm]
    # words free-text replies on Discord exactly as before. False runs
    # Discord as a pure bot — unmatched text gets the short reply — while
    # [llm] stays configured for the features that inherit it (/print-design)
    # and the separately gated roast/vision features keep their own switches.
    chat_llm_enabled = discord.setdefault("chat_llm_enabled", True)
    if not isinstance(chat_llm_enabled, bool):
        raise SettingsError(f"[discord] chat_llm_enabled must be true or false, got {chat_llm_enabled!r}")
    # Print Handy's own question renderer on a Hermes install (#524 stage 2):
    # off by default, so Hermes' adapter keeps drawing the questions exactly
    # as before; on makes the plugin activate its own prompt registry and
    # render questions itself, one renderer in both modes.
    clarify_ui = discord.setdefault("clarify_ui", False)
    if not isinstance(clarify_ui, bool):
        raise SettingsError(f"[discord] clarify_ui must be true or false, got {clarify_ui!r}")
    _validate_telegram(data)
    _validate_matrix(data)
    llm = data.setdefault("llm", {})
    for key, default in (("chat_url", ""), ("model", ""), ("api_key_env", "LLM_API_KEY")):
        llm.setdefault(key, default)
    if llm["chat_url"] and not llm["chat_url"].startswith(("http://", "https://")):
        raise SettingsError(f"[llm] chat_url must be a URL, got {llm['chat_url']!r}")
    if llm["chat_url"] and not llm["model"]:
        raise SettingsError("[llm] model is required when chat_url is set")
    llm.setdefault("timeout_s", 20)
    llm["timeout_s"] = _positive(data, "llm", "timeout_s")
    # The stray-chat agent lane (#524 stage 3): an operator-configured agent
    # endpoint (any OpenAI-compatible chat completions URL, e.g. a Hermes
    # agent behind its API) that answers Discord free text the handlers did
    # not take, ahead of the bounded [llm] call. A separate section, not an
    # [llm] extension: [llm]'s bounded, stateless, untrusted-output meaning
    # is load-bearing for #466, while an agent endpoint is stateful and
    # answers in its own voice. model is optional, unlike [llm]: an agent
    # endpoint may not be model-addressable, and empty omits it from the
    # request. timeout_s defaults to 120, not [llm]'s 20: an agent turn
    # does real work, but the call stays bounded all the same.
    agent = data.setdefault("agent", {})
    for key, default in (("chat_url", ""), ("model", ""), ("api_key_env", "AGENT_API_KEY")):
        agent.setdefault(key, default)
    if agent["chat_url"] and not agent["chat_url"].startswith(("http://", "https://")):
        raise SettingsError(f"[agent] chat_url must be a URL, got {agent['chat_url']!r}")
    agent.setdefault("timeout_s", 120)
    agent["timeout_s"] = _positive(data, "agent", "timeout_s")


def validate(data):
    """Normalize types and fail loudly on a typo instead of slicing with a bad bed."""
    # [[printers]] (#333): an explicit roster projects its first (default) entry into the legacy
    # singleton sections, so every check below validates that entry exactly as it validates a
    # legacy config; the resolved roster itself is captured once those sections are normalised.
    raw_roster = data.pop("printers", None)
    stubs = _check_roster(raw_roster) if raw_roster is not None else None
    if stubs is not None:
        _project_entry(data, stubs[0])
    material_temps = data.setdefault("material_temps", {})
    for family, defaults in DEFAULT_MATERIAL_TEMPS.items():
        profile = material_temps.setdefault(family, defaults.copy())
        if not isinstance(profile, dict) or set(profile) != {"nozzle", "bed"}:
            raise SettingsError(f"[material_temps] {family} must define exactly nozzle and bed")
        for key, value in profile.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 300:
                raise SettingsError(f"[material_temps] {family}.{key} must be between 1 and 300 °C, got {value!r}")
    bed = data.get("printer", {}).get("bed_mm")
    if (not isinstance(bed, list) or len(bed) != 2
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in bed)):
        raise SettingsError(f"[printer] bed_mm must be two positive numbers, got {bed!r}")
    data["printer"]["bed_mm"] = [float(v) for v in bed]
    for key in ("max_print_height_mm", "nozzle_diameter_mm"):
        data["printer"][key] = _positive(data, "printer", key)
    data["printer"]["filament_slots"] = _positive(data, "printer", "filament_slots", int)
    for name, raw_offset in data.get("nozzle_temp_offsets", {}).items():
        if name not in ("brass", "hardened_steel", "stainless_steel", "ruby", "tungsten_carbide", "diamond"):
            raise SettingsError(f"[nozzle_temp_offsets] unknown nozzle type {name!r}")
        if isinstance(raw_offset, bool) or not isinstance(raw_offset, (int, float)) or not -10 <= raw_offset <= 15:
            raise SettingsError(f"[nozzle_temp_offsets] {name} must be between -10 and +15 °C, got {raw_offset!r}")
        data["nozzle_temp_offsets"][name] = float(raw_offset)
    key = _model_key(data["printer"].get("model"), data["printer"].get("vendor"))
    model = printer_registry.MODELS[key]
    data["printer"]["model"], data["printer"]["vendor"] = key, model.vendor.key
    nozzle_size = data["printer"]["nozzle_diameter_mm"]
    if nozzle_size not in model.supported_nozzle_sizes:
        supported = ", ".join(f"{s:g}" for s in sorted(model.supported_nozzle_sizes))
        raise SettingsError(f"[printer] nozzle_diameter_mm {nozzle_size:g} is not supported by model {key!r} (supported: {supported})")
    nozzle_type = data["printer"].get("nozzle_type")
    if nozzle_type is not None:
        from .core.nozzle import NozzleError, normalize_nozzle_type
        try:
            norm_type = normalize_nozzle_type(nozzle_type)
        except NozzleError as exc:
            raise SettingsError(f"[printer] nozzle_type: {exc}") from None
        if norm_type not in model.supported_nozzle_types:
            supported_types = ", ".join(sorted(model.supported_nozzle_types))
            raise SettingsError(f"[printer] nozzle_type {nozzle_type!r} is not supported by model {key!r} (supported: {supported_types})")
        data["printer"]["nozzle_type"] = norm_type
    if model.color_system != "multi_slot" and data["printer"]["filament_slots"] != 1:
        # The class carries this now: both AD5X profiles are multi_slot (one tool,
        # several IFS bays), so no per-model exception is needed here. Stock
        # Flashforge keeps one tool and still has four physical IFS bays;
        # occupancy is checked at print start; this is not a multicolor profile.
        hint = ""
        if key == "klipper.generic":
            hint = ("; if this is a Flashforge AD5X, set PRINT_HANDY__PRINTER__MODEL=flashforge.ad5x "
                    "(the Docker default changed to klipper.generic in 2.2.2)")
        raise SettingsError(f"[printer] filament_slots must be 1 for model {key!r} (single extruder){hint}")
    if "tool_count" in data["printer"]:
        # Independent toolheads (a tool changer) are tools, not IFS slots: the count
        # is only meaningful on a multi-tool model, where filament_slots still stays 1.
        data["printer"]["tool_count"] = _positive(data, "printer", "tool_count", int)
        if data["printer"]["tool_count"] != 1 and model.color_system != "multi_tool":
            raise SettingsError(f"[printer] tool_count must be 1 for model {key!r} (single tool)")
    _validate_vendor_choices(data, model)
    for key, default in (("start_macro", "PRINT_START"), ("end_macro", "PRINT_END")):
        macro = data["printer"].setdefault(key, default)
        if not isinstance(macro, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", macro):
            raise SettingsError(f"[printer] {key} must be a Klipper macro name, got {macro!r}")
    if data["layout"].get("spacing", "minimum") not in SPACING_PRESETS:
        raise SettingsError(f"[layout] spacing must be one of {', '.join(SPACING_PRESETS)}, "
                            f"got {data['layout'].get('spacing')!r}")
    for key in ("single_copy_edge_mm", "multi_copy_edge_mm", "merge_edge_margin_mm", "merge_spacing_mm",
                "arrange_gap_mm", "arrange_edge_mm"):
        data["layout"][key] = _positive(data, "layout", key)
    data["layout"]["max_merged_hours"] = _positive(data, "layout", "max_merged_hours")
    for key in ("size_mm", "part_clearance_mm", "edge_mm", "merge_slack_mm"):
        data["prime_tower"][key] = _positive(data, "prime_tower", key)
    for section in ("orca", "bambu"):
        command = data.get(section, {}).get("command")
        if not isinstance(command, list) or not command or not all(isinstance(v, str) and v for v in command):
            raise SettingsError(f"[{section}] command must be a list of strings, like [\"flatpak\", \"run\", \"com.orcaslicer.OrcaSlicer\"]")
    services = data["services"]
    _check_service_urls(services)
    _check_connection_urls(services, model)
    _validate_bambu_lan(data, model)
    _validate_zmod_delivery_format(data, model)
    _validate_stock_lan(data, model)
    _validate_prusa_link(data, model)
    _validate_elegoo_sdcp(data, model)
    _derive_service_urls(services)
    # The legacy per-printer sections are now fully normalised, so the roster is captured from
    # them: the default entry IS the legacy view, further entries are validated against their
    # own models, and a legacy config synthesises its single entry here (#333).
    if stubs is not None:
        entries = [_capture_entry(data, stubs[0])]
        entries.extend(_validate_entry(stub) for stub in stubs[1:])
        _check_chat_claims(data, entries)
    else:
        entries = [_capture_entry(data, None)]
    data["printers"] = entries
    for key, value in data["paths"].items():
        if key == "ffmpeg" and "/" not in value:
            continue  # a bare name is looked up on PATH
        path = Path(value).expanduser()
        data["paths"][key] = path if path.is_absolute() else ROOT / path
    enabled = data["visual_watch"].get("enabled", True)
    if not isinstance(enabled, bool):
        raise SettingsError("[visual_watch] enabled must be true or false")
    data["visual_watch"]["enabled"] = enabled
    level = data["visual_watch"]["sensitivity"]
    if isinstance(level, bool) or not isinstance(level, (int, float)) or level != int(level) or not 1 <= level <= 5:
        raise SettingsError(f"[visual_watch] sensitivity must be a whole number from 1 to 5, got {level!r}")
    data["visual_watch"]["sensitivity"] = int(level)
    for key in ("question_timeout_s", "safety_confirm_timeout_s", "upload_wait_s", "cold_pull_outcome_s"):
        data["discord"][key] = _positive(data, "discord", key, int)
    _validate_bot(data)
    _validate_design(data)
    _validate_vision(data)
    _validate_spool(data)
    _validate_notify(data)
    _validate_logging(data)
    _validate_webui(data)
    _validate_timelapse(data)
    _validate_camera_source(data)
    _validate_live_stream(data)
    templates = data.get("orca", {}).get("templates")
    if templates == "" and model.generated_templates:
        templates = str(generated_templates_dir(model, data))
    if not isinstance(templates, str) or not templates:
        raise SettingsError("[orca] templates must be a folder path")
    path = Path(templates).expanduser()
    data["orca"]["templates"] = path if path.is_absolute() else ROOT / path
    return data


def load(home=None, local=None, environ=None):
    """Read and validate settings. Prefer YAML and retain TOML during migration.

    Load order: model defaults, home configuration, explicit PRINT_HANDY_CONFIG (or
    local YAML/TOML), then environment. If home.yaml is absent, home.toml is used.
    If local.yaml is absent, local.toml is used. PRINT_HANDY_CONFIG selects one file.
    Pass environ={} to ignore the process environment.
    """
    environ = os.environ if environ is None else environ
    if home is None:
        home = HOME if HOME.is_file() else LEGACY_HOME
    home_data = _apply_key_aliases(_read(home))
    if local is None:
        env = environ.get("PRINT_HANDY_CONFIG")
        if env:
            local = Path(env)
            if not local.is_file():
                raise SettingsError(f"PRINT_HANDY_CONFIG points to a missing file: {local}")
        elif LOCAL.is_file():
            local = LOCAL
        elif LEGACY_LOCAL.is_file():
            local = LEGACY_LOCAL
    extra = _apply_key_aliases(_read(local)) if local else {}
    declared = _merge(home_data, extra).get("printers")
    if declared is not None:
        # A declared roster replaces the legacy per-printer locations; setting both is refused
        # with the offenders named (#333). Only the operator's layers are checked: home.yaml's
        # own legacy values are the defaults base, not a second roster.
        _check_mixed_shape(extra, environ)
    def pick(name):
        return (environ.get(f"PRINT_HANDY__PRINTER__{name.upper()}") or extra.get("printer", {}).get(name)
                or home_data.get("printer", {}).get(name))
    first = declared[0] if isinstance(declared, list) and declared and isinstance(declared[0], dict) else None
    if first is not None and first.get("model"):
        # The defaults base (bed geometry, vendor sections) belongs to the default entry's model.
        key = _model_key(first["model"])
    else:
        key = _model_key(pick("model"), pick("vendor"))
    data = _merge(printer_registry.MODELS[key].defaults(), home_data)
    base = copy.deepcopy(data)
    global _model_defaulted
    shipped = bool(local) and Path(local).resolve() in SHIPPED_DOCKER_CONFIGS
    _model_defaulted = bool(shipped and first is None and not environ.get("PRINT_HANDY__PRINTER__MODEL")
                            and not environ.get("PRINT_HANDY__PRINTER__VENDOR"))
    if local:
        _check_keys(base, extra, local)
        data = _merge(data, extra)
    env_layer = _from_env(base, environ, declared)
    data = _merge(data, env_layer)
    # layout.spacing fills the gap/margin keys; a key the operator set anywhere wins.
    apply_spacing_preset(data, _explicit_keys(home_data, extra, env_layer))
    return validate(data)


_override = None


# Set by load(): the model came only from the image's own config/docker.* (no
# environment variable, no operator file, no roster). See defaulted_model_warning.
_model_defaulted = False


def defaulted_model_warning(data=None):
    """A startup warning when a Docker install may have been moved to Generic Klipper by 2.2.2.

    The image default changed from flashforge.ad5x to klipper.generic. An AD5X
    owner who never set PRINT_HANDY__PRINTER__MODEL is now on the generic model
    silently; the tell is a hardware record from the old model (several loaded
    slots or a zmod_* entry). Returns the message, or None when nothing looks wrong.
    """
    data = data or current()
    if not _model_defaulted or data["printer"]["model"] != "klipper.generic":
        return None
    try:
        record = json.loads((state_dir(data) / "printer_hardware.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    slots = record.get("filament_slots", record.get("ifs_slots"))
    multi_slot = isinstance(slots, dict) and len(slots) > 1
    zmod = any(key.startswith("zmod_") and value not in (None, "", False, 0) for key, value in record.items())
    if not (multi_slot or zmod):
        return None
    return ("[printer] model was not set, so this install uses the Docker default klipper.generic (changed from "
            "flashforge.ad5x in 2.2.2), but its hardware record carries AD5X multi-slot or ZMod data. If this is a "
            "Flashforge AD5X, set PRINT_HANDY__PRINTER__MODEL=flashforge.ad5x")


def set_override(data):
    """Install `data` as this process's settings, replacing current().

    A listener instance process (#336) serves exactly one roster printer:
    the registry launcher resolves that printer's view (view_for) and
    installs it here, so every settings consumer in the process — adapters,
    the Hermes _config fallback, the healthcheck — reads the printer it
    serves. Tests use it the same way hermes._config.set_override is used.
    Pass None to clear.
    """
    global _override
    _override = data


@lru_cache(maxsize=1)
def _load_cached():
    return load()


def current():
    """Settings for this process, read once."""
    if _override is not None:
        return _override
    return _load_cached()


def printers(data=None):
    """The resolved [[printers]] roster (#333): one entry per configured printer.

    The first entry is the default printer — the one the legacy singleton
    view (data["printer"], the [services] URLs, printers.current()) is
    populated from. Nothing consumes the roster at runtime yet; it is the
    config model the later multi-printer stages (#334 onward) build on.
    """
    return list((data or current())["printers"])


def by_id(printer_id, data=None):
    """The roster entry with id `printer_id`; an unknown id is a SettingsError naming it."""
    for entry in printers(data):
        if entry["id"] == printer_id:
            return entry
    raise SettingsError(f"no printer with id {printer_id!r} is configured")


def state_dir(data=None):
    """Where runtime state files live: <hermes_home>/state."""
    return (data or current())["paths"]["hermes_home"] / "state"


def default_printer_id(data=None):
    """The roster id of the default (first) printer — the legacy singleton's identity (#334)."""
    return printers(data)[0]["id"]


def printer_state_dir(printer_id=None, data=None):
    """One printer's state folder: <state>/printers/<id> (design §2.4, #334).

    Everything that is about one machine — its hardware record, its launch
    database, its maintenance record — lives under here from Stage 2b on, so
    two printers sharing one instance never share a lease, an approval table
    or a hardware record. The default is the default printer's folder.
    """
    return state_dir(data) / "printers" / (printer_id or default_printer_id(data))


def view_for(printer_id, data=None):
    """A settings-shaped view of one roster entry (#334).

    A deep copy of the resolved settings with the entry projected into the
    legacy singleton sections (the same projection validate() applies to the
    first entry), so an adapter built on the view reads that printer's model,
    geometry and connection keys. Global sections ([paths], [discord],
    [services] spoolman, [delivery]) stay shared, exactly as the design
    keeps them global; the roster itself is carried over unchanged. The
    default printer's view is the resolved settings themselves.
    """
    data = data or current()
    entry = by_id(printer_id, data)
    if entry["id"] == default_printer_id(data):
        return data
    view = copy.deepcopy(data)
    model = printer_registry.MODELS[entry["model"]]
    defaults = model.defaults()
    for section in ("printer", "layout", "prime_tower", "cold_pull"):
        if section in defaults:
            view[section] = copy.deepcopy(defaults[section])
    _project_entry(view, dict(entry))
    _derive_service_urls(view["services"])
    templates = defaults.get("orca", {}).get("templates", "")
    if model.generated_templates:
        view["orca"]["templates"] = generated_templates_dir(model, view)
    elif templates:
        path = Path(templates).expanduser()
        view["orca"]["templates"] = path if path.is_absolute() else ROOT / path
    view["printers"] = data["printers"]
    return view


def generated_templates_dir(model, data=None):
    """Where a model's generated Orca templates live (never the repository): <state>/orca-profiles/<templates>."""
    return state_dir(data) / "orca-profiles" / model.templates


def _main(argv):
    """python3 -m print_handy.settings get services.moonraker_url  (for shell scripts)"""
    if len(argv) != 2 or argv[0] != "get" or argv[1].count(".") != 1:
        raise SystemExit("usage: python3 -m print_handy.settings get SECTION.KEY")
    section, key = argv[1].split(".")
    try:
        value = current()[section][key]
    except KeyError:
        raise SystemExit(f"unknown setting {argv[1]}") from None
    print(" ".join(map(str, value)) if isinstance(value, list) else value)


if __name__ == "__main__":
    import sys
    _main(sys.argv[1:])
