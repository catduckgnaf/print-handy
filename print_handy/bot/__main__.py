"""python3 -m print_handy.bot [run | check | health]

run     start the standalone bot on the [bot] platform (the default). Refuses unless [bot] mode is standalone.
check   validate settings, token, channel, users and handlers offline. Exit 1 on a problem.
health  exit 0 only while a running bot reports a fresh heartbeat.
"""
import logging
import os
import sys

from .. import __version__, ai_routes, logs, settings

logger = logging.getLogger(__name__)


def checks(cfg):
    """[(label, ok, detail)]. Offline: nothing here contacts the chat platform."""
    from ..hermes import _config
    from . import agent, llm, platform
    from .runtime import allowed_users, discord_chat_llm_enabled
    rows = []

    def add(label, ok, detail):
        rows.append((label, ok, detail))

    mode = cfg["bot"]["mode"]
    add("mode", mode == "standalone", mode if mode == "standalone" else
        f"{mode}; the Hermes mode was removed — set [bot] mode to 'standalone'")
    name = str(cfg["bot"].get("platform") or "discord")
    implemented = platform.platform_module(name) is not None
    add("platform", implemented, name if implemented else
        f"{name}; platform {name!r} is not implemented yet (#164) — discord, telegram and matrix are available")
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
        from urllib.parse import urlparse
        agent_cfg = cfg.get("agent") or {}
        host = urlparse(str(agent_cfg.get("chat_url") or "")).hostname or agent_cfg.get("chat_url")
        model = str(agent_cfg.get("model") or "").strip()
        add("agent", True, f"{model} at {host}" if model else
            f"endpoint at {host} (no model set; the endpoint chooses)")
    else:
        add("agent", True, "not configured (set [agent] chat_url to let an agent endpoint "
                           "answer stray chat; unmatched messages use the llm row above)")
    # Question rendering: the bot always draws its own questions — there
    # is no host renderer anymore (the Hermes mode that drew them is gone).
    if name == "discord":
        add("clarify ui", True, "own renderer (Print Handy draws its questions itself)")
    # The roast is separate from the chat fallback above and needs no model
    # at all (#1002): one line per print from the local roast corpus, on the
    # completion post, the failure and cancel posts and a confirmed-spaghetti
    # alert, and on request ("roast my last print"), on every platform.
    # vision.roast_enabled (on by default) is the only switch.
    vision = cfg.get("vision") or {}
    # The vision endpoint the camera rows below need ([ai.small] is one too, #818).
    vision_url = str((cfg.get("services") or {}).get("vision_chat_url") or "")
    if ai_routes.explicit("small", cfg):
        vision_url = vision_url or cfg["ai"]["small"]["chat_url"]
    if not vision.get("roast_enabled", True):
        add("roast", True, "off (vision.roast_enabled is false)")
    else:
        try:
            from ..core import roast as roast_engine
            # The operator's custom bank (#1002 Phase 4) counts toward the
            # corpus when one is configured; its problems are named here
            # as well as in the log, so a bad bank is visible, not silent.
            custom_bank = str(vision.get("roast_custom_bank") or "").strip()
            size = roast_engine.corpus_size(custom_bank or None)
            problems = roast_engine.load_corpus(custom_bank or None)["custom_problems"] \
                if custom_bank else []
        except Exception as exc:
            add("roast", False, f"on, but the local roast corpus could not be read ({exc})")
        else:
            if problems:
                add("roast", False, f"on (local corpus, {size} lines, no LLM), but the "
                                    f"custom roast bank named by vision.roast_custom_bank "
                                    f"has problems: {problems[0]}")
            else:
                add("roast", True, f"on (local corpus, {size} lines, no LLM; "
                                   "set vision.roast_enabled=false to turn roasts off)")
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
    # Local empty-plate reference (#455): the idle checks can certify an
    # empty plate locally only when a fresh reference exists.
    from ..hermes import local_empty as _local_empty
    _ref_state, _ref_detail = _local_empty.reference_status(cfg)
    add("plate reference", _ref_state in ("off", "missing", "pending", "ready"),
        f"{_ref_state}: {_ref_detail}")
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
    elif _ref_state == "ready" and not vision_url \
            and not os.environ.get("PRINT_VISION_URL"):
        add("auto light", True, f"on (the local plate reference certifies the empty "
                                f"plate for the idle check; light off after the plate "
                                f"has been seen empty for {light_minutes} minutes; set "
                                f"vision.auto_light_off_minutes=0 to turn auto light off)")
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
    # the path (#524 Stage 3). A forced-on-but-unusable design fails the
    # check, like a missing token, so the misconfiguration is visible before
    # a user hits it mid-conversation; auto with no endpoint is a healthy off.
    import shutil as _shutil
    from ..core import design as _design
    design_cfg = cfg.get("design") or {}
    resolved = _design.resolve_http_config(cfg)
    endpoint_available = resolved is not None
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
    else:
        add("design", False, "enabled, but no model endpoint: set [design] chat_url and "
                             "model, or the [llm] values they inherit")
    return rows


# Startup check tiers (bootstrap): the credential tier is fatal — a bot
# with no platform token cannot connect at all, and the token arrives
# through the environment / Docker variable, never through chat. The
# configuration tier (no print channel/room yet, no allowed users, no
# Matrix homeserver on non-Matrix platforms) is not fatal: the bot
# starts in setup-only mode and holds exactly one conversation, the
# guided setup. Every other failing row stays fatal.
CREDENTIAL_TIER_LABELS = frozenset({"token"})
CONFIGURATION_TIER_LABELS = frozenset({"print channel", "allowed users", "print room"})


def split_check_rows(rows):
    """(credential, configuration, other) failing labels for check rows."""
    credential, configuration, other = [], [], []
    for label, ok, _detail in rows:
        if ok:
            continue
        if label in CREDENTIAL_TIER_LABELS:
            credential.append(label)
        elif label in CONFIGURATION_TIER_LABELS or label == "homeserver":
            configuration.append(label)
        else:
            other.append(label)
    return credential, configuration, other


def unconfigured_install(cfg, name):
    """True only for an install nobody has set up yet.

    Setup-only mode lets the first person who reaches the bot claim the
    install, so a configured install with one bad setting must fail its
    checks as before instead of reopening that door. A setup someone
    skipped was never finished, so it does not count as configured.
    """
    from .. import setup_state
    record = setup_state.load_completion(cfg)
    if record.get("done") and not record.get("skipped"):
        return False
    section = cfg.get(name) if isinstance(cfg.get(name), dict) else {}
    chat_key = "room_id" if name == "matrix" else "chat_id"
    for key in (chat_key, "allowed_user_ids", "approval_user_id"):
        value = section.get(key)
        if value.strip() if isinstance(value, str) else bool(value):
            return False
    return True


def setup_only_mode(cfg, rows):
    """True when only configuration-tier rows fail: start setup-only.

    Matrix is the exception for its homeserver row: without a
    homeserver the Matrix client cannot connect at all, so that row is
    credential-tier there even though it is configuration elsewhere.
    """
    credential, configuration, other = split_check_rows(rows)
    if credential or other or not configuration:
        return False
    name = str(cfg["bot"].get("platform") or "discord")
    if name == "matrix" and "homeserver" in configuration:
        return False
    return unconfigured_install(cfg, name)


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
    try:
        cfg = settings.current()
    except settings.SettingsError as exc:
        print(f"FAIL  settings: {exc}", file=sys.stderr)
        print("Not starting: fix the FAIL lines above.", file=sys.stderr)
        return 2
    rows = checks(cfg)
    failed = False
    for label, ok, detail in rows:
        failed |= not ok
        print(f"{'ok' if ok else 'FAIL':<5} {label}: {detail}", file=sys.stderr)
    if failed and not setup_only_mode(cfg, rows):
        print("Not starting: fix the FAIL lines above.", file=sys.stderr)
        return 2
    if failed:
        cfg["bot"]["setup_only"] = True
        print("Starting in setup-only mode: the bot connects and answers only the "
              "guided setup conversation (`/print-handy setup`) until setup is "
              "applied. The platform token still comes from the environment — "
              "chat setup starts once the bot can connect.", file=sys.stderr)
        logger.warning("setup-only mode: no chat configuration yet; only guided "
                       "setup is answered")
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
