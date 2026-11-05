"""python3 -m print_handy.bot [run | check | health]

run     start the standalone bot on the [bot] platform (the default). Refuses unless [bot] mode is standalone.
check   validate settings, token, channel, users and handlers offline. Exit 1 on a problem.
health  exit 0 only while a running bot reports a fresh heartbeat with no Hermes loaded.
"""
import logging
import os
import sys
from pathlib import Path

from .. import __version__, ai_routes, logs, settings

logger = logging.getLogger(__name__)


def agent_identity_rows(cfg, probe_fn=None):
    """The #902 identity-layer rows for bot check.

    A legacy config (single shared endpoint, no identity layer) gets one
    deprecation row: that endpoint is a fallback identity every authorized
    channel user shares. An identity-layer config gets the counts and the
    fallback identity, a failing row when nobody is authorized, one
    auth-checked reachability row per identity (the probe sends the key
    as a bearer header and never prints it), and the Hermes capability
    warning for dangerous ``api_server`` toolsets a fallback-reachable
    profile exposes.
    """
    from . import agent_identity
    if not agent_identity.layer_active(cfg):
        return [("agent identities", True,
                 "legacy single endpoint: treated as the fallback identity for every authorized "
                 "channel user — anyone in the channel drives the one agent. Add "
                 "[agent.identities] and [agent.users] to give each user their own identity (#902)")]
    rows = []
    found = agent_identity.identities(cfg)
    mapped, allowlisted = agent_identity.authorized_counts(cfg)
    fallback = agent_identity.fallback_identity(cfg)
    rows.append(("agent identities", True,
                 f"{len(found)} endpoint(s) ({', '.join(sorted(found))}); {mapped} mapped user(s), "
                 f"{allowlisted} allowlisted; fallback identity: "
                 f"{fallback.name if fallback else 'none'}"))
    if agent_identity.nobody_authorized(cfg):
        rows.append(("agent authorization", False,
                     "nobody is authorized: the lane is enabled but no user maps to an identity "
                     "and no allowlisted user has a fallback identity, so stray chat gets no agent "
                     "call — add [agent.users], or [agent.allowlist] with a fallback_identity"))
    probe_fn = probe_fn or agent_identity.probe
    for identity in found.values():
        verdict, detail = probe_fn(identity)
        rows.append((f"agent identity {identity.name}", verdict == "ok", detail))
        level, cap_detail, _dangerous = agent_identity.capability_report(identity, cfg)
        if level == "unknown":
            continue
        guest = fallback is not None and identity.name == fallback.name
        if level == "warn" and guest:
            rows.append((f"agent capability {identity.name}", False,
                         f"WARNING: {cap_detail} — and the fallback identity reaches this "
                         "profile, so allowlisted users can use those tools through stray chat. "
                         "Restrict platform_toolsets.api_server on that Hermes profile "
                         "(see the agent lane section of docs/DISCORD-BOT-SETUP.md)"))
        else:
            rows.append((f"agent capability {identity.name}", level == "ok", cap_detail))
    return rows


def checks(cfg):
    """[(label, ok, detail)]. Offline: nothing here contacts the chat platform."""
    from ..hermes import _config
    from . import agent, health, llm, platform
    from .runtime import allowed_users, discord_chat_llm_enabled
    rows = []

    def add(label, ok, detail):
        rows.append((label, ok, detail))

    mode = cfg["bot"]["mode"]
    add("mode", mode == "standalone", mode if mode == "standalone" else
        f"{mode}; set PRINT_HANDY__BOT__MODE=standalone (or [bot] mode) to run without Hermes")
    if mode == "hermes":
        # #574: in Hermes mode the plugin answers events of the dedicated
        # profile it is installed in (#566); the name is derived from
        # paths.hermes_home, never configured. Name the identity the gates
        # will use, and fail the row when it cannot be derived: the
        # fallback answers only the default profile's events, which on a
        # renamed install means the bot silently ignores its channel.
        profile_name, derived = _config.hosting_profile(home=Path(cfg["paths"]["hermes_home"]), mode=mode)
        if derived:
            add("hosting profile", True,
                f"{profile_name} (derived from paths.hermes_home {cfg['paths']['hermes_home']})")
        else:
            add("hosting profile", False,
                f"cannot derive the hosting profile from paths.hermes_home "
                f"({cfg['paths']['hermes_home']}); the plugin will answer only "
                f"'{profile_name}' events — point paths.hermes_home at the dedicated "
                "profile directory (~/.hermes/profiles/<name>)")
    name = str(cfg["bot"].get("platform") or "discord")
    implemented = platform.platform_module(name) is not None
    add("platform", implemented, name if implemented else
        f"{name}; platform {name!r} is not implemented yet (#164) — discord, telegram and matrix are available")
    if mode == "hermes" and name == "discord":
        # #384: in Hermes mode the gateway owns the Discord connection, and an
        # explicit platforms.discord.enabled: false in the profile config
        # silently removes attachment intake — credentials do not override it
        # and the gateway only logs a warning. Name it here, with the remedy.
        state, profile_config = _config.profile_platform_state(
            "discord", path=Path(cfg["paths"]["hermes_home"]) / "config.yaml")
        remedy = (f"set platforms.discord.enabled: true in {profile_config} and restart the Hermes gateway; "
                  "environment credentials do not override an explicit platform disable")
        if state == "disabled":
            add("hermes discord platform", False,
                f"disabled in {profile_config}: platforms.discord.enabled is false, so the gateway never "
                f"connects Discord and attachment intake is silently absent; {remedy}")
        elif state == "enabled":
            add("hermes discord platform", True, f"enabled in {profile_config}")
        elif state == "unset":
            add("hermes discord platform", True,
                f"no explicit platforms.discord.enabled in {profile_config}; the gateway default applies")
        elif state == "missing":
            add("hermes discord platform", True,
                f"no profile config at {profile_config} (not installed; no platform switch to disable)")
        else:
            add("hermes discord platform", False,
                f"cannot read {profile_config} to verify platforms.discord.enabled; "
                "fix or remove it, then re-run scripts/install-hermes.sh")
    if name == "discord":
        token_env = cfg["discord"]["token_env"]
        add("token", bool(_config.discord_token()), f"${token_env} is set" if _config.discord_token()
            else f"${token_env} is empty; put the Discord bot token in that environment variable")
        chat = str(cfg["discord"]["chat_id"] or "")
        add("print channel", chat.isdigit(), chat or "discord.chat_id is empty")
        # Discord access (#386): an empty allow list is a valid mode (the
        # channel is the boundary), never a failure. The row names the mode
        # so a check run states who can use the bot.
        from .platform import describe_access
        from .runtime import denied_users, listed_users
        add("access", True, describe_access(len(allowed_users(cfg)), len(denied_users(cfg)),
                                            not listed_users(cfg)))
    elif name == "telegram":
        from .telegram import telegram_token
        token_env = cfg["telegram"]["token_env"]
        add("token", bool(telegram_token(cfg)), f"${token_env} is set" if telegram_token(cfg)
            else f"${token_env} is empty; put the Telegram bot token in that environment variable")
        chat = str(cfg["telegram"]["chat_id"] or "")
        add("print channel", bool(chat) and chat.lstrip("-").isdigit() and chat != "-",
            chat or "telegram.chat_id is empty")
        users = allowed_users(cfg, "telegram")
        add("allowed users", bool(users), f"{len(users)} user(s)" if users
            else "none: set telegram.allowed_user_ids (or approval_user_id); everyone is refused")
    elif name == "matrix":
        from .matrix import matrix_token
        token_env = cfg["matrix"]["access_token_env"]
        add("token", bool(matrix_token(cfg)), f"${token_env} is set" if matrix_token(cfg)
            else f"${token_env} is empty; put the Matrix access token in that environment variable")
        homeserver = str(cfg["matrix"]["homeserver_url"] or "")
        add("homeserver", homeserver.startswith(("http://", "https://")),
            homeserver or "matrix.homeserver_url is empty")
        room = str(cfg["matrix"]["room_id"] or "")
        add("print room", room.startswith("!"), room or "matrix.room_id is empty")
        users = allowed_users(cfg, "matrix")
        add("allowed users", bool(users), f"{len(users)} user(s)" if users
            else "none: set matrix.allowed_user_ids (or approval_user_id); everyone is refused")
    # Debug log mode (#478): a visible row while a session is on, so a
    # check run never hides that DEBUG capture is running.
    try:
        from .. import debug_mode
        add("debug mode", True, debug_mode.check_row(cfg) or "off")
    except Exception as exc:
        add("debug mode", True, f"could not check: {exc}")
    label, available, detail = platform.library_status(name)
    add(label, available, detail)
    try:
        from .router import Router
        names = Router().handler_names()
        add("handlers", bool(names), f"{len(names)}: {', '.join(names)}")
    except Exception as exc:
        add("handlers", False, f"plugin failed to load: {exc}")
    loaded = health.hermes_modules_loaded()
    add("no hermes", not loaded, "no Hermes module imported" if not loaded else ", ".join(loaded))
    try:
        state = _config.state_dir()
        state.mkdir(parents=True, exist_ok=True)
        probe = state / ".write-test"
        probe.write_text("")
        probe.unlink()
        add("state dir", True, str(state))
    except OSError as exc:
        add("state dir", False, f"not writable: {exc}")
    if name in ("telegram", "matrix"):
        # Telegram and Matrix are pure bots (#164 stages 2-3, #73): the LLM
        # fallback is never wired for them, whatever [llm] says elsewhere.
        add("llm", True, f"off ({name.capitalize()} is a pure bot: no LLM; unmatched messages get the short not-understood reply)")
    elif name == "discord" and not discord_chat_llm_enabled(cfg):
        # Discord's own no-LLM mode (#466): the operator turned the chat LLM
        # off by name, so the row names the switch — [llm] may still be
        # configured for /print-design, which the design row below reports.
        add("llm", True, "off (discord.chat_llm_enabled is false: no chat LLM; "
                         "unmatched messages get the short not-understood reply)")
    else:
        add("llm", True, ai_routes.describe("chat", cfg) if llm.configured(cfg)
            else "off (unmatched messages get the short not-understood reply)")
    # The stray-chat agent lane (#524 stage 3): a configured [agent]
    # endpoint answers Discord free text ahead of the [llm] call above.
    # Like the llm row it is a healthy-off row in every unconfigured or
    # disabled state — the lane ships dark until an operator sets it.
    if name in ("telegram", "matrix"):
        add("agent", True, f"off ({name.capitalize()} is a pure bot: no agent lane; "
                           "unmatched messages get the short not-understood reply)")
    elif name == "discord" and not discord_chat_llm_enabled(cfg):
        add("agent", True, "off (discord.chat_llm_enabled is false: no stray-chat model call "
                           "of any kind, the agent lane included)")
    elif agent.configured(cfg):
        from . import agent_identity
        if agent_identity.layer_active(cfg):
            mapped, allowlisted = agent_identity.authorized_counts(cfg)
            add("agent", True, f"identity layer on: {mapped} mapped, {allowlisted} "
                               f"allowlisted user(s); everyone else gets no agent call (#902)")
        else:
            from urllib.parse import urlparse
            agent_cfg = cfg.get("agent") or {}
            host = urlparse(str(agent_cfg.get("chat_url") or "")).hostname or agent_cfg.get("chat_url")
            model = str(agent_cfg.get("model") or "").strip()
            add("agent", True, f"{model} at {host}" if model else
                f"endpoint at {host} (no model set; the endpoint chooses)")
        # The identity layer (#902): who may use the lane, as what, and
        # whether each identity's endpoint actually answers its key.
        for label, ok, detail in agent_identity_rows(cfg):
            add(label, ok, detail)
    else:
        add("agent", True, "not configured (set [agent] chat_url to let an agent endpoint "
                           "answer stray chat; unmatched messages use the llm row above)")
    # Question rendering (#524 stage 2): the standalone bot always draws
    # its own questions; on a Hermes install the host adapter draws them
    # unless the operator turns on Print Handy's own renderer, so Hermes
    # mode renders exactly what standalone (and the README mockups) show.
    # A healthy-off row in both states, like the agent row above. The
    # setting is read at plugin register/attach time (restart to change).
    if name == "discord":
        if bool((cfg.get("discord") or {}).get("clarify_ui", False)):
            add("clarify ui", True, "own renderer (discord.clarify_ui is on: in Hermes mode "
                                    "Print Handy draws its questions itself, one renderer in both modes)")
        else:
            add("clarify ui", True, "host renderer (in Hermes mode Hermes draws the questions; "
                                    "set discord.clarify_ui=true for Print Handy's own renderer)")
    # The roast is separate from the chat fallback above: the print-started
    # post gains a one-line roast (exactly one per print, when printing
    # begins; the completion post never does) on every platform (Telegram
    # and Matrix included) whenever [vision] roast_enabled is on — the
    # default — and an LLM/vision endpoint is available.
    vision = cfg.get("vision") or {}
    vision_url = str((cfg.get("services") or {}).get("vision_chat_url") or "")
    small_route = ai_routes.describe("small", cfg, purpose="roast") if ai_routes.explicit("small", cfg) else ""
    if small_route:  # [ai.small] is a vision endpoint too (#818)
        vision_url = vision_url or cfg["ai"]["small"]["chat_url"]
    if not vision.get("roast_enabled", True):
        add("roast", True, "off (vision.roast_enabled is false)")
    elif small_route:
        add("roast", True, f"on ({small_route}; set vision.roast_enabled=false to turn roasts off)")
    elif vision_url:
        add("roast", True, f"on ({vision.get('roast_model') or 'luna-latest'} at {vision_url}; "
                           "set vision.roast_enabled=false to turn roasts off)")
    else:
        add("roast", True, "on by default, but no LLM/vision endpoint is available "
                           "(services.vision_chat_url is empty); set it to enable roasts")
    # Local failure detector (#455): the built-in ONNX route for the camera
    # watchdog on installs with no vision endpoint. Off by default; when on
    # it judges frames only while no endpoint route is configured (endpoint
    # routes keep precedence), and it never answers roasts or label reads.
    from ..hermes import local_vision as _local_vision
    _detector_state, _detector_detail = _local_vision.detector_status(cfg)
    if _detector_state == "off":
        add("local detector", True, f"off ({_detector_detail})")
    elif _detector_state == "ready":
        add("local detector", True, f"on ({_detector_detail}; used by the camera watchdog "
                                    "only when no vision endpoint is configured)")
    else:
        add("local detector", False, f"on, but unavailable: {_detector_detail}")
    # Auto light-off (#516): the listener turns the printer's light on at
    # print start and off again after the camera verdict has seen an empty
    # plate for vision.auto_light_off_minutes. Off by default (0). Like
    # the watchdog it needs a vision endpoint (PRINT_VISION_URL counts);
    # the light target is the named vision.light_object, or the printer's
    # single light object auto-discovered by the listener — bot check is
    # offline, so an unnamed target is reported as unresolved here rather
    # than discovered. The Bambu/SDCP follow-up made the target
    # family-shaped: Bambu's chamber light is a fixed protocol target,
    # SDCP's is its Cmd 403 light, and the remaining families have no
    # light at all — the row names the configured printer's family,
    # resolved from the model in cfg without contacting the printer.
    light_minutes = vision.get("auto_light_off_minutes", 0)
    light_object = str(vision.get("light_object") or "").strip()
    light_connection = None
    try:
        from .. import printers as _printers
        _light_model = _printers.MODELS.get(str((cfg.get("printer") or {}).get("model") or ""))
        if _light_model is not None:
            light_connection = getattr(getattr(_light_model, "vendor", None), "connection", None)
    except Exception:
        light_connection = None
    if not light_minutes:
        add("auto light", True, "off (vision.auto_light_off_minutes is 0; set it, "
                                "e.g. 30, to turn the printer light off after the "
                                "plate has been seen empty that long)")
    elif light_connection == "elegoo_sdcp":
        add("auto light", True, f"on (SDCP chamber light, Cmd 403; light on at print "
                                f"start; auto-off cannot run yet — no SDCP camera "
                                f"source is verified, so the empty-plate check has no "
                                f"frame to judge and the light stays on; set "
                                f"vision.auto_light_off_minutes=0 to turn auto light off)")
    elif light_connection not in (None, "moonraker", "bambulab_lan"):
        add("auto light", True, f"on, but unavailable for this printer: connection "
                                f"{light_connection!r} has no light control (auto "
                                f"light-off covers Moonraker, Bambu Lab and Elegoo "
                                f"SDCP printers); the setting has no effect here")
    elif vision.get("local_detector_enabled") and not vision_url \
            and not os.environ.get("PRINT_VISION_URL"):
        add("auto light", True, "on, but the local failure detector cannot certify an "
                                "empty plate (it never answers empty_idle), so the light "
                                "will stay on; auto light-off needs a vision endpoint "
                                "(services.vision_chat_url or PRINT_VISION_URL)")
    elif not vision_url and not os.environ.get("PRINT_VISION_URL"):
        add("auto light", True, "on, but no vision endpoint is available "
                                "(services.vision_chat_url is empty and PRINT_VISION_URL "
                                "is not set); the empty-plate check needs one")
    elif light_connection == "bambulab_lan":
        add("auto light", True, f"on (Bambu chamber light; light off after the plate has been "
                                f"seen empty for {light_minutes} minutes; a model whose own "
                                f"telemetry reports no chamber light is never switched — "
                                f"the listener logs it unavailable; vision.light_object "
                                f"applies to Moonraker printers only; set "
                                f"vision.auto_light_off_minutes=0 to turn auto light off)")
    elif light_object:
        add("auto light", True, f"on ({light_object}; light off after the plate has been "
                                f"seen empty for {light_minutes} minutes; set "
                                f"vision.auto_light_off_minutes=0 to turn auto light off)")
    else:
        add("auto light", True, "on, but no light target is named (vision.light_object "
                                "is empty); the listener auto-discovers only when the "
                                "printer has exactly one light object — set "
                                "vision.light_object to name the target for sure")
    # AI design (#388): tri-state enable — explicit design.enabled=false is
    # off; explicit true is on; unset is auto (on whenever a design/LLM
    # endpoint resolves — the roast posture). When on it needs OpenSCAD (the
    # design engine) and a model: the [design] endpoint inheriting [llm] is
    # the normal path in every mode (#524 Stage 3); in Hermes mode with no
    # endpoint configured, the profile's auxiliary.print_design task is the
    # fallback lane. A forced-on-but-unusable design fails the check, like
    # a missing token, so the misconfiguration is visible before a user
    # hits it mid-conversation; auto with no endpoint is a healthy off.
    import shutil as _shutil
    from ..core import design as _design
    design_cfg = cfg.get("design") or {}
    resolved = _design.resolve_http_config(cfg)
    endpoint_available = resolved is not None or mode == "hermes"
    design_mode = _design.mode_name(cfg, endpoint_available=endpoint_available)
    if design_mode == "off":
        add("design", True, "off (design.enabled is false; /print-design and /design are unavailable)")
    elif design_mode == "auto-off":
        add("design", True, "auto-off (no design/LLM endpoint configured; /print-design answers "
                            "'not set up' until one is — set [design] or [llm] chat_url/model)")
    elif not _shutil.which("openscad"):
        add("design", False, "enabled, but openscad is not on PATH; install OpenSCAD "
                             "(it is in the Docker image) or set design.enabled=false")
    elif resolved:
        add("design", True, f"{design_mode} ({resolved['model']} at {resolved['chat_url']})")
    elif mode == "hermes":
        add("design", True, f"{design_mode} (Hermes print_design auxiliary task; pin its model in the "
                            "profile's auxiliary.print_design block)")
    else:
        add("design", False, "enabled, but no model endpoint: set [design] chat_url and "
                             "model, or the [llm] values they inherit")
    return rows


def check(out=None):
    out = out or sys.stdout
    try:
        cfg = settings.current()
    except settings.SettingsError as exc:
        print(f"FAIL  settings: {exc}", file=out)
        return 1
    failed = False
    for label, ok, detail in checks(cfg):
        failed |= not ok
        print(f"{'ok' if ok else 'FAIL':<5} {label}: {detail}", file=out)
    return 1 if failed else 0


def run():
    logs.configure()
    logger.info("standalone bot starting (Print Handy %s)", __version__)
    if check(sys.stderr):
        print("Not starting: fix the FAIL lines above.", file=sys.stderr)
        return 2
    cfg = settings.current()
    name = str(cfg["bot"].get("platform") or "discord")
    if name == "discord":
        from .client import run_bot
        return run_bot(cfg)
    if name == "telegram":
        from .telegram import run_bot
        return run_bot(cfg)
    if name == "matrix":
        from .matrix import run_bot
        return run_bot(cfg)
    print(f"Platform {name!r} is not implemented yet (#164).", file=sys.stderr)  # unreachable while checks report it, kept as a guard
    return 2


def main(argv):
    from ..core import resources
    resources.apply_to_environment()  # before numpy/onnxruntime load: pools follow the CPU quota
    command = argv[0] if argv else "run"
    if command == "run":
        return run()
    if command == "check":
        return check()
    if command == "health":
        from . import health
        return health.check()
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
