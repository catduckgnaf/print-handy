"""A stored service key goes only to the host it was configured for (#1192).

Service URLs are settings, editable in the Web UI; the keys those services
need live in the environment (or the profile ``.env``). Without a binding,
whoever changes a URL decides where the stored key is sent. This module keeps
one small record in the state folder, ``credential-bindings.json`` (owner-only,
written atomically), mapping each key-carrying setting to the host its key was
bound to.

The interface is three calls:

* :func:`key_for` — a client asks for the key it is about to attach to a
  request for ``url``. It gets the key when the URL's host is the bound host,
  and ``""`` (with one log line per setting and host, never the key) when it
  is not. A setting with no record binds to the first host it is used with,
  so an install upgraded to this version keeps working.
* :func:`rebind` — the operator's explicit "send my key to this host".
* :func:`pending_rebinds` — for a settings save: the changes that would move
  a bound key to a new host. The Web UI refuses those unless the operator
  confirms each one.

Hosts compare by host name only (lowercased, trailing dot dropped), so a path
or port change on the same host behaves as before. One scheme change counts
as a host change: a key bound over https is never sent over plain http, even
to the same host. Keyless services are never
affected: with no key there is nothing to bind or withhold.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

RECORD_NAME = "credential-bindings.json"
PATH_ENV = "PRINT_HANDY_CREDENTIAL_BINDINGS"


def _setting(cfg, section, key) -> str:
    value = ((cfg or {}).get(section) or {}).get(key)
    return str(value or "").strip()


@dataclass(frozen=True)
class Slot:
    """One setting whose value says where a stored key is sent."""
    setting: str          # the catalog field id, "section.key"
    service: str          # plain-language name for messages
    key_names: Callable   # cfg -> environment variable names that may hold the key
    inherits: str = ""    # the setting used as the address while this one is empty


SLOTS = {slot.setting: slot for slot in (
    Slot("llm.chat_url", "chat endpoint",
         lambda cfg: (_setting(cfg, "llm", "api_key_env") or "LLM_API_KEY",)),
    Slot("design.chat_url", "design endpoint",
         lambda cfg: (_setting(cfg, "design", "api_key_env")
                      or _setting(cfg, "llm", "api_key_env") or "LLM_API_KEY",),
         inherits="llm.chat_url"),
    Slot("agent.chat_url", "agent endpoint",
         lambda cfg: tuple(n for n in (_setting(cfg, "agent", "api_key_env"),) if n)),
    Slot("services.vision_chat_url", "vision endpoint", lambda cfg: ("CLIPROXY_API_KEY",)),
    Slot("notify.ntfy_url", "ntfy", lambda cfg: ("NTFY_ACCESS_TOKEN",)),
    Slot("notify.mqtt_host", "MQTT broker", lambda cfg: ("MQTT_PASSWORD",)),
    Slot("matrix.homeserver_url", "Matrix",
         lambda cfg: (_setting(cfg, "matrix", "access_token_env") or "MATRIX_ACCESS_TOKEN",)),
    Slot("services.moonraker_url", "Moonraker", lambda cfg: ("MOONRAKER_API_KEY",)),
    Slot("services.octoprint_url", "OctoPrint", lambda cfg: ("OCTOPRINT_API_KEY",)),
    Slot("bambu.lan_host", "Bambu Lab LAN",
         lambda cfg: (_setting(cfg, "bambu", "lan_access_code_env") or "BAMBU_LAN_PASSWORD",)),
    Slot("prusa.link_host", "PrusaLink",
         lambda cfg: (_setting(cfg, "prusa", "api_key_env") or "PRUSA_LINK_API_KEY",)),
    Slot("flashforge_stock.host", "Flashforge", lambda cfg: ("AD5X_CHECK_CODE",)),
)}


def host_of(value) -> str:
    """The host name a URL (or a bare host field) points at; "" when none."""
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "//" + text
    try:
        host = urlsplit(text).hostname or ""
    except ValueError:
        return ""
    return host.rstrip(".").lower()


def _target(value) -> str:
    """What a binding records: ``https://host`` or ``http://host`` for a URL, the
    bare host for a host field; "" when there is no host."""
    host = host_of(value)
    if not host:
        return ""
    scheme = _scheme(value)
    return f"{scheme}://{host}" if scheme in ("http", "https") else host


def _scheme(value) -> str:
    text = str(value or "").strip()
    return text.split("://", 1)[0].lower() if "://" in text else ""


def _allows(bound, url) -> bool:
    """Whether a key bound to ``bound`` may go to ``url``: the same host, and never
    from https down to plain http."""
    if host_of(bound) != host_of(url):
        return False
    return not (_scheme(bound) == "https" and _scheme(url) == "http")


def _shown(bound, url) -> tuple:
    """(bound, new) for messages: host names, with the schemes when only they differ."""
    if host_of(bound) == host_of(url):
        return _target(bound), _target(url)
    return host_of(bound), host_of(url)


# ------------------------------------------------------------------ the record

_lock = threading.Lock()
_warned: set = set()


def record_path() -> Path:
    override = os.environ.get(PATH_ENV)
    if override:
        return Path(override)
    from .runtime import _config
    return Path(_config.cfg()["paths"]["state_home"]) / "state" / RECORD_NAME


def _read() -> dict:
    try:
        raw = json.loads(record_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    bindings = raw.get("bindings") if isinstance(raw, dict) else None
    if not isinstance(bindings, dict):
        return {}
    return {str(k): str(v) for k, v in bindings.items() if isinstance(v, str) and v}


def _write(bindings) -> None:
    target = record_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"bindings": dict(sorted(bindings.items()))}, indent=1) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        try:
            os.chmod(tmp, 0o600)
        except OSError:  # pragma: no cover - a filesystem without modes
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def bound_host(setting) -> str | None:
    """The host ``setting``'s key is bound to, or None when there is no record."""
    with _lock:
        bound = _read().get(setting)
    return None if bound is None else host_of(bound)


def key_for(setting, url, key) -> str:
    """``key`` when ``url``'s host is the host ``setting``'s key is bound to, else "".

    No record yet binds the key to this host (the upgrade path). A mismatch is
    logged once per setting and host, naming the hosts and never the key.
    """
    if not key:
        return ""
    target = _target(url)
    if not target:
        return ""
    with _lock:
        bindings = _read()
        bound = bindings.get(setting)
        # No record binds (the upgrade path); a same-host http binding used
        # over https is tightened to https.
        if bound is None or (_allows(bound, url) and _scheme(url) == "https"
                             and _scheme(bound) != "https"):
            bindings[setting] = target
            try:
                _write(bindings)
            except OSError as exc:
                log.warning("could not record the %s key's host (%s); it is sent this time",
                            setting, type(exc).__name__)
            return key
    if _allows(bound, url):
        return key
    if (setting, target) not in _warned:
        _warned.add((setting, target))
        shown_bound, shown_new = _shown(bound, url)
        log.warning("not sending the stored key for %s to %s: it is bound to %s. Confirm the "
                    "new address on the Web UI settings page (or run python -m "
                    "print_handy.credential_binding rebind %s <url>) to send it there.",
                    setting, shown_new, shown_bound, setting)
    return ""


def rebind(setting, url) -> str:
    """Bind ``setting``'s key to ``url``'s host (the operator's explicit confirmation).

    An empty URL removes the record, so the next use binds afresh. Returns the host."""
    host = host_of(url)
    with _lock:
        bindings = _read()
        if host:
            bindings[setting] = _target(url)
        else:
            bindings.pop(setting, None)
        _write(bindings)
    _warned.difference_update({w for w in _warned if w[0] == setting})
    return host


# ------------------------------------------------------------- the save guard

@dataclass(frozen=True)
class Rebind:
    """A save that would move a bound key to a new host."""
    setting: str
    service: str
    old_host: str
    new_host: str

    @property
    def confirmation(self) -> str:
        return f"Send my {self.service} key to {self.new_host}"

    @property
    def refusal(self) -> str:
        return (f"{self.setting}: the stored {self.service} key is bound to {self.old_host}. "
                f"Saving {self.new_host} would send that key to a new host (or over plain "
                f"http instead of https), so nothing was "
                f"written. To save it, tick \"{self.confirmation}\" on the Settings page review step, or "
                f"leave the address on {self.old_host}.")


def _key_present(slot, cfg, environ) -> bool:
    from .runtime import _config
    for name in slot.key_names(cfg):
        if (environ or {}).get(name):
            return True
        try:
            if _config.profile_env_value(name):
                return True
        except Exception:
            continue
    return False


def pending_rebinds(changes, old_values, cfg=None, environ=None) -> list:
    """[Rebind] for the ``{setting: new value}`` changes that move a bound key.

    ``old_values`` maps settings to their values before the save (the inherited
    setting included, for a slot that inherits). A setting with no record counts
    as bound to its current host when its key is present, so a key is protected
    from the first save after an upgrade. Clearing a URL, keeping its host, a
    first address, or a service with no key are never pending.
    """
    out = []
    bindings = None
    for setting, new in (changes or {}).items():
        slot = SLOTS.get(setting)
        if slot is None:
            continue
        if not host_of(new):
            continue
        if bindings is None:
            with _lock:
                bindings = _read()
        bound = bindings.get(setting)
        if bound is None:
            old = old_values.get(setting) or (old_values.get(slot.inherits) if slot.inherits else "")
            source = setting if old_values.get(setting) or not slot.inherits else slot.inherits
            bound = bindings.get(source)
            if bound is None and host_of(old) and _key_present(slot, cfg, environ):
                bound = _target(old)
        if bound is None or _allows(bound, new):
            continue
        shown_bound, shown_new = _shown(bound, new)
        out.append(Rebind(setting, slot.service, shown_bound, shown_new))
    return out


def _main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog="python -m print_handy.credential_binding",
        description="Show or change which host each stored service key is sent to.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show every binding")
    one = sub.add_parser("rebind", help="send SETTING's key to URL's host from now on")
    one.add_argument("setting", choices=sorted(SLOTS))
    one.add_argument("url")
    args = parser.parse_args(argv)
    if args.command == "list":
        for setting, bound in sorted(_read().items()):
            print(f"{setting}: {bound}")
        return 0
    host = rebind(args.setting, args.url)
    print(f"{args.setting}: {host or 'unbound (binds on next use)'}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
