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

One more 3 case (#440): enabled with a port on a non-loopback host but
no access token in the environment. The server itself still refuses
that bind outright (print_handy.webui.server.check_start — a
LAN-reachable Web UI must never run unauthenticated), but in the
supervised container that refusal used to kill the whole container on
an otherwise healthy install. The gate now reports the blocked start
with the fix (set the token, or bind loopback) and the supervisor
carries on without the Web UI: the bot, listener and Spoolman are
unaffected, and the Web UI still never runs unauthenticated.
"""
from __future__ import annotations

import ipaddress
import os
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
    """Why an enabled Web UI must not start, or None when it may (#440).

    The one blocked case: a non-loopback bind with no access token in
    the environment variable ``env_name`` names. The server refuses
    that bind itself; the gate reports it first so the supervised
    container degrades to "no Web UI" with guidance instead of halting.
    docker/healthcheck.py mirrors this through the same helper so the
    probe and the supervisor can never disagree. ``require_auth`` false
    ([webui] require_auth, the Docker default since #708) allows the
    tokenless LAN start: nothing is blocked then, and a token that is
    set is still enforced by the server.
    """
    environ = os.environ if environ is None else environ
    if not require_auth or is_loopback(host) or (environ.get(env_name) or ""):
        return None
    return (f"webui gate: enabled on {host}:{port} but ${env_name} is empty: "
            f"a non-loopback Web UI requires an access token, so it was not "
            f"started. Set {env_name} to a long random value (Unraid: the "
            f"'Web UI access token' field) and restart the container, or bind "
            f"127.0.0.1 to keep the Web UI inside the container. The bot, "
            f"listener and Spoolman are unaffected.")


def decide(data=None, environ=None):
    """0 when the supervisor should start the Web UI, 3 when it should not."""
    enabled, host, port, env_name, require_auth = webui_config(data)
    if not (enabled and port > 0):
        return 3
    return 3 if start_blocked(host, port, env_name, environ, require_auth) else 0


def main():
    try:
        enabled, host, port, env_name, require_auth = webui_config()
    except settings.SettingsError as exc:
        print(f"webui gate: configuration unresolved: {exc}", file=sys.stderr)
        return 2
    if enabled and port > 0:
        blocked = start_blocked(host, port, env_name, require_auth=require_auth)
        if blocked:
            print(blocked)
            return 3
        if not require_auth and not is_loopback(host) and not (os.environ.get(env_name) or ""):
            print(f"webui gate: enabled on {host}:{port} with authentication OFF "
                  f"([webui] require_auth = false and ${env_name} is empty): anyone who "
                  f"can reach the port can use the Web UI. Set {env_name} to require "
                  f"a token")
            return 0
        print(f"webui gate: enabled on {host}:{port}")
        return 0
    if enabled:
        print("webui gate: enabled but no port chosen ([webui] port is 0); not starting it")
    else:
        print("webui gate: [webui] enabled is false; not starting it")
    return 3


if __name__ == "__main__":
    sys.exit(main())
