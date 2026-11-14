"""Shell-facing probe: is the Discord live-view Activity ready to serve? (#311 stage 4)

docker/supervise.sh runs ``python3 -m print_handy.live_gate`` before
starting children, mirroring print_handy.webui_gate (#286) and
print_handy.go2rtc (#246). The decision comes from the resolved
configuration (the ``[live_stream]`` section, via print_handy.settings)
plus the presence of the deployment environment the Activity server
needs, never from whether one variable happens to be filled in, so the
supervisor and the docker healthcheck (which reuses :func:`live_config`)
can never disagree.

The Activity server (:mod:`print_handy.live_activity`) serves the page
only when the feature is *enabled and configured*:

- ``[live_stream] enabled`` is true;
- a ``voice_channel_id`` and an https ``activity_origin`` are set (the
  same pair :func:`print_handy.live_stream.start_problem` requires
  before ``/print-live start`` will point anyone at the page);
- a loopback ``port`` has been chosen (0 = not chosen yet);
- the Discord application ID is in the environment
  (:data:`CLIENT_ID_ENV`, a deployment variable, not a setting and not
  a secret);
- both secrets are present in the environment — the Discord client
  secret (:data:`print_handy.live_activity.CLIENT_SECRET_ENV`) and the
  Activity session secret
  (:data:`print_handy.live_activity.SESSION_SECRET_ENV`). Only their
  presence is ever reported, never a value.

Exit codes: 0 = enabled and configured, start the server
(``python3 -m print_handy.live_activity``); 3 = not started (disabled,
or enabled but incompletely configured — a healthy, reported state,
like the go2rtc no-source case); 2 = the configuration could not be
resolved (the supervisor stops rather than guessing).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

from . import live_activity, settings

#: The Discord application (client) ID environment variable. It is a
#: public identifier (it appears in every invite URL), so it is a plain
#: deployment variable — but it is not a setting either, because the
#: same application already owns the bot token; the operator supplies
#: it once, beside the secrets the Activity server needs.
CLIENT_ID_ENV = "DISCORD_CLIENT_ID"


@dataclass(frozen=True)
class LiveConfig:
    """The resolved live-view deployment state the gate and healthcheck share."""

    enabled: bool
    port: int
    activity_origin: str
    client_id: str
    missing: tuple = field(default_factory=tuple)

    @property
    def ready(self) -> bool:
        """True when the supervisor should start the Activity server."""
        return self.enabled and not self.missing


def live_config(data=None, environ=None) -> LiveConfig:
    """Resolve the live-view deployment state from settings + environment.

    Settings problems propagate as SettingsError (the gate's unresolved
    case). ``missing`` names what an enabled-but-incomplete deployment
    still needs, in operator terms; secret entries name the environment
    variable only, never a value.
    """
    data = settings.load() if data is None else data
    environ = os.environ if environ is None else environ
    live = data["live_stream"]
    enabled = bool(live["enabled"])
    missing = []
    if enabled:
        if not str(live["voice_channel_id"] or "").strip():
            missing.append("[live_stream] voice_channel_id is not set")
        if not str(live["activity_origin"] or "").strip():
            missing.append("[live_stream] activity_origin is not set")
        if not int(live["port"] or 0):
            missing.append("[live_stream] port is 0 (no port chosen)")
        if not (environ.get(CLIENT_ID_ENV) or "").strip():
            missing.append(f"{CLIENT_ID_ENV} is not set")
        if not live_activity.client_secret(environ):
            missing.append(f"{live_activity.CLIENT_SECRET_ENV} is not set")
        if not live_activity.session_secret(environ):
            missing.append(f"{live_activity.SESSION_SECRET_ENV} is not set")
    return LiveConfig(enabled=enabled, port=int(live["port"] or 0),
                      activity_origin=str(live["activity_origin"] or ""),
                      client_id=(environ.get(CLIENT_ID_ENV) or "").strip(),
                      missing=tuple(missing))


def decide(data=None, environ=None) -> int:
    """0 when the supervisor should start the Activity server, 3 when not."""
    return 0 if live_config(data, environ).ready else 3


def main() -> int:
    try:
        config = live_config()
    except settings.SettingsError as exc:
        print(f"live gate: configuration unresolved: {exc} [PH_GATE_CONFIGURATION]", file=sys.stderr)
        return 2
    warning = settings.unknown_settings_warning()
    if warning:
        print(f"live gate: WARNING {warning}", file=sys.stderr)
    if config.ready:
        print(f"live gate: enabled and configured; Activity server on "
              f"127.0.0.1:{config.port} for {config.activity_origin}")
        return 0
    if not config.enabled:
        print("live gate: [live_stream] enabled is false; not starting the Activity server")
    else:
        print("live gate: live view is enabled but not configured; not starting the "
              "Activity server: " + "; ".join(config.missing))
    return 3


if __name__ == "__main__":
    sys.exit(main())
