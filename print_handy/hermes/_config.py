"""Shared access to Print Handy settings, state files and secrets for the Hermes helpers.

Every hardcoded host, path and interval of the original scripts is a setting in the home configuration.
Tests install an override with set_override() so no code path reads a live setup.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from print_handy import settings

_override: dict[str, Any] | None = None


def set_override(data: dict[str, Any] | None) -> None:
    """Use this settings dict instead of settings.current() (tests only)."""
    global _override
    _override = data


def cfg() -> dict[str, Any]:
    return _override if _override is not None else settings.current()


def hermes_home() -> Path:
    return Path(cfg()["paths"]["hermes_home"])


def profile_config_path() -> Path:
    """The Hermes profile's own config file: <hermes_home>/config.yaml."""
    return hermes_home() / "config.yaml"


def profile_platform_state(platform: str = "discord", path: Path | None = None) -> tuple[str, Path]:
    """(state, path): the gateway platform switch in the Hermes profile config.

    state is "enabled", "disabled" (platforms.<platform>.enabled is explicitly
    false: the gateway will not connect that platform even when credentials are
    set — environment credentials do not override an explicit platform disable),
    "unset" (no explicit switch; the gateway default applies), "missing" (no
    profile config at all) or "unreadable" (the file cannot be parsed, so the
    switch cannot be verified). Print Handy never writes this file — the
    installer renders it once from hermes/profile/config.yaml.template and the
    Hermes gateway owns it after that — so this only ever reads (#384).
    ``path`` overrides the profile config location for callers holding their
    own settings dict (the doctor); the default is the live profile's.
    """
    path = Path(path) if path is not None else profile_config_path()
    if not path.is_file():
        return "missing", path
    import yaml
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return "unreadable", path
    if not isinstance(data, dict):
        return "unreadable", path
    platforms = data.get("platforms")
    section = platforms.get(platform) if isinstance(platforms, dict) else None
    if not isinstance(section, dict) or "enabled" not in section:
        return "unset", path
    value = section["enabled"]
    if value is False or (isinstance(value, str) and value.strip().casefold() in ("false", "no", "off")):
        return "disabled", path
    return "enabled", path


_pinned: dict[str, Any] | None = None


def pinned_hermes_home() -> Path:
    """The profile home from config files only, ignoring $HERMES_HOME.

    A cron job's HERMES_HOME is the launching profile, not the print profile, so anything that
    must find the print profile reads this instead.
    """
    global _pinned
    if _override is not None:
        return Path(_override["paths"]["hermes_home"])
    if _pinned is None:
        _pinned = settings.load(environ={})
    return Path(_pinned["paths"]["hermes_home"])


# The shipped default profile name, and the standalone bot's identity label
# (print_handy.bot.runtime.PROFILE stamps it on the bot's own sources). It is
# the fallback identity when no hosting profile can be derived — standalone
# runtimes, which have no Hermes profile at all (#574).
DEFAULT_PROFILE = "3dprinting"


def hosting_profile(plugin_file=None, home=None, mode=None) -> tuple[str, bool]:
    """(name, derived): the Hermes profile identity this install answers for (#574).

    Hermes mode requires a dedicated profile (#566), but its name is the
    operator's choice, so the identity the print-feedback plugin gates on is
    derived from the install itself, never from a setting of its own:

    * ``paths.hermes_home`` inside a Hermes profiles layout
      (``~/.hermes/profiles/<name>``) — the path the installer targets; its
      basename is the profile name. A hermes_home outside that layout
      (Docker's ``/data/hermes``) names a data root, not a profile, and is
      skipped; ``$HERMES_HOME`` is never consulted either, for the
      pinned_hermes_home reason (a cron job's names the launching profile).
    * the plugin's own install location, when ``plugin_file``'s folder is
      the symlink scripts/install-hermes.sh creates at
      ``<profile>/plugins/print-feedback``.

    When neither derives a profile, returns (DEFAULT_PROFILE, False): the
    standalone bot's stamped identity, which is the only event identity a
    standalone runtime ever presents.

    Derivation applies only in Hermes mode (``mode``, default ``[bot] mode``).
    A standalone runtime stamps every event DEFAULT_PROFILE, so deriving
    "printshop" from a standalone install whose hermes_home happens to sit
    in a profiles layout made the gates refuse every command.
    """
    if mode is None:
        try:
            mode = str((cfg().get("bot") or {}).get("mode") or "")
        except Exception:
            mode = ""
    if mode != "hermes":
        return DEFAULT_PROFILE, False
    if home is None:
        try:
            home = hermes_home()
        except Exception:
            home = None
    if home is not None:
        home = Path(home)
        if home.name and home.parent.name == "profiles":
            return home.name, True
    if plugin_file is not None:
        folder = Path(plugin_file).parent
        if folder.parent.name == "plugins" and folder.is_symlink():
            name = folder.parent.parent.name
            if name:
                return name, True
    return DEFAULT_PROFILE, False


def bot_name() -> str:
    """The name the bot uses for itself: [discord] bot_name."""
    return str(cfg()["discord"]["bot_name"]).strip() or "Print Handy"


def state_dir() -> Path:
    return settings.state_dir(cfg())


def state_file(env_var: str | None, name: str, *, per_instance: bool = False) -> Path:
    """A state file: the legacy env var if set (tests rely on it), else <state_dir>/<name>.

    With per_instance, a process serving a non-default roster printer (a
    per-printer listener instance, #336) gets its own copy under
    <state_dir>/printers/<id>/ — #334's layout — so sibling listener
    instances never share operational state (P1-82). The default printer
    keeps the legacy path: its state was always the singleton's, so
    nothing is orphaned at upgrade. A process serving no instance (the
    bot, one-shot tools) is unchanged, and an env override always wins.
    Files that are cross-process channels by design (the start claims
    print-now writes and the listeners read) stay shared: callers opt in
    per file.
    """
    if env_var and os.environ.get(env_var):
        return Path(os.environ[env_var])
    if per_instance:
        namespaced = _instance_state_path(name)
        if namespaced is not None:
            return namespaced
    return state_dir() / name


def _instance_state_path(name: str) -> Path | None:
    """<state>/printers/<id>/<name> for a non-default instance printer, else None."""
    try:
        from print_handy import instance

        printer_id = instance.printer_id()
        if not printer_id:
            return None
        data = cfg()
        if printer_id == settings.default_printer_id(data):
            return None
        return settings.printer_state_dir(printer_id, data) / name
    except Exception:
        return None


def service_url(key: str) -> str:
    """A [services] URL without a trailing slash. Empty when not configured."""
    return str(cfg()["services"].get(key) or "").rstrip("/")


def require(url: str, setting: str) -> str:
    """Return url, or fail clearly when the setting is empty."""
    if not url:
        raise RuntimeError(f"{setting} is not configured. Set it in config/local.yaml (or legacy config/local.toml).")
    return url


def moonraker_url() -> str:
    return service_url("moonraker_url")


def archive_dir() -> Path:
    return Path(cfg()["paths"]["archive"])


def records_vault() -> Path:
    return Path(cfg()["paths"]["records_vault"])


def hermes_bin() -> str:
    return str(cfg()["paths"]["hermes_bin"])


def ffmpeg_bin() -> str:
    return str(cfg()["paths"]["ffmpeg"])


def cache_dir() -> Path:
    return Path(cfg()["paths"]["cache"])


def timezone_name() -> str:
    return str(cfg()["discord"]["timezone"])


def tz():
    """The configured display timezone (discord.timezone)."""
    from zoneinfo import ZoneInfo
    return ZoneInfo(timezone_name())


def tz_label(dt=None) -> str:
    """Short zone label for messages: ET for America/New_York, else the zone's own abbreviation."""
    if timezone_name() == "America/New_York":
        return "ET"
    from datetime import datetime
    return (dt or datetime.now(tz())).tzname() or timezone_name()


def discord_chat_id() -> str:
    return str(cfg()["discord"]["chat_id"] or "")


def bot_mode() -> str:
    """standalone (python3 -m print_handy.bot, the default) or hermes (the gateway runs the plugin)."""
    return str((cfg().get("bot") or {}).get("mode") or "standalone")


def standalone() -> bool:
    return bot_mode() == "standalone"


def discord_token() -> str:
    """The Discord bot token: the [discord] token_env variable, then the profile .env files."""
    name = str((cfg().get("discord") or {}).get("token_env") or "DISCORD_BOT_TOKEN")
    return profile_env_value(name)


def clarify_ui_enabled() -> bool:
    """[discord] clarify_ui: Print Handy's own renderer draws handler questions under Hermes (#524 stage 2).

    Off by default — Hermes' adapter keeps rendering, exactly as before the
    switch existed. On, the plugin activates its own prompt registry, posts
    questions by direct Discord REST (print_handy.hermes.clarify_ui) and
    resolves clicks and typed answers itself: one renderer in both modes.
    Read at plugin register/attach time; changing it takes effect on restart.
    """
    return bool((cfg().get("discord") or {}).get("clarify_ui", False))


def question_timeout_s() -> int:
    """How long a Discord question waits for a button press."""
    return int(cfg()["discord"]["question_timeout_s"])


def safety_confirm_timeout_s() -> int:
    """How long a safety confirmation (stop, resume, reboot, cold-pull go-ahead) waits."""
    return int(cfg()["discord"]["safety_confirm_timeout_s"])


def upload_wait_s() -> int:
    """How long an upload-wait freeze stays active before it expires."""
    return int(cfg()["discord"]["upload_wait_s"])


def cold_pull_outcome_s() -> int:
    """How long a cold-pull outcome prompt stays open."""
    return int(cfg()["discord"]["cold_pull_outcome_s"])


def _env_file_value(path: Path, name: str) -> str:
    try:
        for line in path.read_text().splitlines():
            if line.startswith(f"{name}="):
                found = line.split("=", 1)[1].strip().strip("'\"")
                if found:
                    return found
    except OSError:
        pass
    return ""


def profile_env_value(name: str, *, include_global: bool = True) -> str:
    """A secret: the environment, then the profile .env, then the global .env next to the hermes root."""
    value = os.environ.get(name)
    if value:
        return value
    home = hermes_home()
    paths = [home / ".env"]
    if include_global:
        paths.append(home.parent.parent / ".env")
    for path in paths:
        found = _env_file_value(path, name)
        if found:
            return found
    return ""
