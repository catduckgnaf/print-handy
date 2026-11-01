"""Shell-facing probe: is the optional Web UI enabled with a port? (#286)

docker/supervise.sh runs ``python3 -m print_handy.webui_gate`` before
starting children (stage 4), mirroring print_handy.listener_gate (#256).
The decision comes from the resolved configuration ([webui] enabled and
port, via print_handy.settings), never from whether an environment
variable happens to be filled in, so a misconfiguration still fails
loudly in settings validation instead of silently changing which
children run. The gate only decides *whether* to start the server: the
server's own start rules (print_handy.webui.server.check_start — a
non-loopback bind requires the access token) still apply to the child
and are never bypassed here.

Exit codes: 0 = the Web UI is enabled and a port is chosen, start it
(``python3 -m print_handy.webui``); 3 = the Web UI is disabled, or
enabled without a port, so the supervisor starts nothing; 2 = the
configuration could not be resolved (the supervisor stops with the
printed error rather than guessing).

Before #708 a LAN bind with no access token was a third 3 case (#440):
the gate reported it and the container ran without the Web UI. Since
#708 such a bind starts in first-run setup instead
(print_handy.webui.access): the Web UI serves only its setup page until
the operator sets a password or affirmatively leaves it open, so it
still never runs unauthenticated by accident. ``start_blocked`` stays
for callers (docker/healthcheck.py) and now always answers None.
"""
from __future__ import annotations

import ipaddress
import sys

from . import settings

LOOPBACK_NAMES = frozenset({"localhost"})


def webui_config(data=None):
    """(enabled, host, port, access_token_env, require_auth) from the resolved settings.

    Settings problems propagate as SettingsError, the gate's unresolved
    case (#256). docker/healthcheck.py reuses this for its Web UI row so
    the probe and the supervisor can never disagree.
    """
    data = settings.load() if data is None else data
    webui = data["webui"]
    return (bool(webui["enabled"]), str(webui["host"]),
            int(webui["port"] or 0), str(webui["access_token_env"]),
            bool(webui.get("require_auth", True)))


def is_loopback(host):
    """True for loopback bind hosts only; mirrors webui.server.is_loopback."""
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def start_blocked(host, port, env_name, environ=None, require_auth=True):
    """Why an enabled Web UI must not start, or None when it may.

    Always None since #708: the one case that used to block (#440), a
    non-loopback bind with no access token, now starts in first-run setup
    and serves only the setup page. Kept so docker/healthcheck.py and the
    supervisor keep asking the same question.
    """
    return None


def setup_pending(host, data=None, environ=None):
    """True while a Web UI bound to ``host`` waits for its first-run setup choice."""
    from .webui import access
    data = settings.load() if data is None else data
    return access.resolve(data, str(host), environ).setup_pending


def decide(data=None, environ=None):
    """0 when the supervisor should start the Web UI, 3 when it should not."""
    enabled, host, port, env_name, require_auth = webui_config(data)
    if not (enabled and port > 0):
        return 3
    return 3 if start_blocked(host, port, env_name, environ, require_auth) else 0


def _setup_pending_quietly(host):
    """setup_pending for the gate's log line; an unreadable state never blocks the start."""
    try:
        return setup_pending(host)
    except Exception:
        return False


def main():
    try:
        enabled, host, port, env_name, require_auth = webui_config()
    except settings.SettingsError as exc:
        print(f"webui gate: configuration unresolved: {exc}", file=sys.stderr)
        return 2
    if enabled and port > 0:
        if not require_auth and not is_loopback(host):
            print(f"webui gate: enabled on {host}:{port} with authentication OFF "
                  f"([webui] require_auth = false): anyone who can reach the port "
                  f"can use the Web UI")
        elif _setup_pending_quietly(host):
            print(f"webui gate: enabled on {host}:{port}; first-run setup is waiting: "
                  f"open the Web UI in a browser to set a password or leave it open")
        else:
            print(f"webui gate: enabled on {host}:{port}")
        return 0
    if enabled:
        print("webui gate: enabled but no port chosen ([webui] port is 0); not starting it")
    else:
        print("webui gate: [webui] enabled is false; not starting it")
    return 3


if __name__ == "__main__":
    sys.exit(main())
