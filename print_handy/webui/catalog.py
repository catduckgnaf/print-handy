"""The Web UI field catalog (#286, stage 1): one declarative entry per editable setting.

Every entry names a real ``[section] key`` the settings loader already
validates. The catalog is the single list a later stage renders as pickers,
so the Discord guided setup (``print_handy/bot/guided.py``), the CLI setup
wizard and the Web UI cannot drift apart: the guided-setup fields are all
here, printer-model choices come from the printer registry, platform
choices from ``print_handy.bot.platform``, and slicer/delivery choices
from the configured printer's vendor profile.

No secret ever appears here. A secret *value* is never a setting, and no
``*_env`` key (which only names the environment variable holding a secret)
is editable through the catalog either.

Per-printer fields are deliberately absent (#337, stage 2e decision): the
catalog addresses global fields only in v1 — a ``[[printers]]`` roster is
declared in the config file or through the setup surfaces, and the status
page shows it read-only. Per-printer catalog addressing is a future stage's
decision, not this one's; see docs/WEBUI.md.

Settings are read once at process start (``settings.current()`` is
cached), so every field is honestly marked ``restart_required``.
"""
from __future__ import annotations

from dataclasses import dataclass

from .. import printers, settings

# The kinds a later stage knows how to render.
KINDS = ("picker", "toggle", "number", "text", "url", "host", "id_list")

# Choices sources resolved by choices_for(): named, so a catalog entry
# stays declarative and new printer models appear without UI code changes.
SOURCE_PRINTER_REGISTRY = "printer_registry"
SOURCE_BOT_PLATFORMS = "bot_platforms"
SOURCE_VENDOR_SLICERS = "vendor_slicers"
SOURCE_VENDOR_DELIVERY = "vendor_delivery"
SOURCE_TIMEZONES = "timezones"


@dataclass(frozen=True)
class Field:
    """One editable setting, described for a picker UI."""

    section: str
    key: str
    label: str
    kind: str
    help: str = ""
    choices: tuple = ()
    choices_source: str | None = None
    validator: str = "settings.validate"
    restart_required: bool = True
    minimum: float | None = None
    maximum: float | None = None
    # The /config page group (stage 3); every field is in exactly one,
    # assigned from GROUP_OF below so the page order stays declarative.
    group: str = ""

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"field {self.section}.{self.key}: unknown kind {self.kind!r}")

    @property
    def id(self):
        return f"{self.section}.{self.key}"


# Page groups for the stage 3 /config page, keyed by field id. The page
# renders them in this order: printer, platform, services, endpoints,
# timelapse, images, live, slicing, logging, webui (pages.GROUPS
# carries the titles).
GROUP_OF = {
    "printer.model": "printer",
    "printer.slicer": "slicing",
    "printer.zmod_delivery_format": "printer",
    "delivery.methods": "slicing",
    "layout.spacing": "slicing",
    "bot.mode": "platform",
    "bot.platform": "platform",
    "discord.chat_id": "platform",
    "discord.allowed_user_ids": "platform",
    "discord.denied_user_ids": "platform",
    "discord.approval_user_id": "platform",
    "discord.timezone": "platform",
    "discord.chat_llm_enabled": "platform",
    "discord.clarify_ui": "platform",
    "telegram.chat_id": "platform",
    "telegram.allowed_user_ids": "platform",
    "telegram.approval_user_id": "platform",
    "matrix.homeserver_url": "platform",
    "matrix.room_id": "platform",
    "matrix.allowed_user_ids": "platform",
    "matrix.approval_user_id": "platform",
    "services.moonraker_url": "services",
    "services.octoprint_url": "services",
    "bambu.lan_host": "services",
    "bambu.lan_serial": "services",
    "prusa.link_host": "services",
    "flashforge_stock.host": "services",
    "elegoo_sdcp.host": "services",
    "services.spoolman_url": "services",
    "spool.prompt_brand": "services",
    "notify.mqtt_enabled": "services",
    "notify.mqtt_host": "services",
    "notify.mqtt_port": "services",
    "notify.mqtt_topic": "services",
    "notify.mqtt_username": "services",
    "notify.ntfy_enabled": "services",
    "notify.ntfy_url": "services",
    "notify.ntfy_topic": "services",
    "llm.chat_url": "endpoints",
    "llm.model": "endpoints",
    "design.chat_url": "endpoints",
    "design.model": "endpoints",
    "agent.chat_url": "endpoints",
    "agent.model": "endpoints",
    "services.camera_source": "images",
    "services.camera_snapshot_url": "images",
    "timelapse.capture_source_url": "images",
    "services.vision_chat_url": "images",
    "vision.model": "images",
    "vision.roast_model": "images",
    "vision.roast_enabled": "images",
    "vision.local_detector_enabled": "images",
    "vision.local_model_path": "images",
    "vision.auto_light_off_minutes": "images",
    "vision.light_object": "images",
    "visual_watch.enabled": "images",
    "visual_watch.sensitivity": "images",
    "timelapse.capture_enabled": "timelapse",
    "timelapse.mode": "timelapse",
    "timelapse.render_wait_s": "timelapse",
    "timelapse.render_poll_s": "timelapse",
    "timelapse.max_bytes": "timelapse",
    "timelapse.local_retention_days": "timelapse",
    "timelapse.plan_clip_retention_days": "timelapse",
    "timelapse.archive_host": "timelapse",
    "timelapse.archive_dir": "timelapse",
    "timelapse.ssh_key": "timelapse",
    "logging.level": "logging",
    "logging.format": "logging",
    "webui.enabled": "webui",
    "webui.host": "webui",
    "webui.port": "webui",
    "webui.controls_enabled": "webui",
    "live_stream.enabled": "live",
    "live_stream.voice_channel_id": "live",
    "live_stream.auto_start_on_print": "live",
    "live_stream.max_height": "live",
    "live_stream.fps": "live",
    "live_stream.max_bitrate_kbps": "live",
    "live_stream.encoder": "live",
    "live_stream.empty_timeout_s": "live",
    "live_stream.activity_origin": "live",
    "live_stream.port": "live",
}


def _field(section, key, label, kind, help, **kwargs):
    return Field(section, key, label, kind, help=help,
                 group=GROUP_OF[f"{section}.{key}"], **kwargs)


CATALOG = (
    # Printer (guided setup: model, plus the vendor-offered slicer/delivery sets).
    _field("printer", "model", "Printer model", "picker",
           "The printer model key; sets every model default for that printer.",
           choices_source=SOURCE_PRINTER_REGISTRY, validator="guided model / settings model check"),
    _field("printer", "slicer", "Slicer", "picker",
           "Slicer for this printer; only the ones its vendor profile offers.",
           choices_source=SOURCE_VENDOR_SLICERS, validator="settings vendor choices"),
    _field("delivery", "methods", "Delivery methods", "picker",
           "Post-slice options offered, as a list; only the ones the printer's vendor profile offers.",
           choices_source=SOURCE_VENDOR_DELIVERY, validator="settings vendor choices"),
    _field("layout", "spacing", "Plate spacing", "picker",
           "How much room auto-layout leaves between parts it arranges on a plate: "
           "minimum, low, medium or high. A gap named explicitly in the settings "
           "file wins over the preset for that gap.",
           choices=tuple(settings.SPACING_PRESETS), validator="settings layout check"),
    _field("printer", "zmod_delivery_format", "ZMod delivery format", "picker",
           "Flashforge/ZMod printers only: auto sends the sliced .gcode.3mf when the printer "
           "reports ZMod 1.7.5+ and plain .gcode otherwise; gcode always sends .gcode; "
           "3mf always sends the package.",
           choices=("auto", "gcode", "3mf"), validator="settings zmod delivery format check"),
    # Bot and chat platform (guided setup: mode, plus bot platform from #164).
    _field("bot", "mode", "Bot mode", "picker",
           "standalone runs Print Handy's own bot; hermes runs it as a Hermes Agent profile.",
           choices=tuple(settings.BOT_MODES), validator="guided mode / settings bot check"),
    _field("bot", "platform", "Bot platform", "picker",
           "Chat platform the standalone bot talks to.",
           choices_source=SOURCE_BOT_PLATFORMS, validator="settings bot platform check"),
    _field("discord", "chat_id", "Discord print channel", "text",
           "Numeric ID of the print channel (Developer Mode > Copy Channel ID).",
           validator="guided chat ID"),
    _field("discord", "allowed_user_ids", "Discord allowed users", "id_list",
           "Optional whitelist: numeric Discord user IDs allowed to use Print Handy, separated "
           "by commas or spaces. Empty (the default) lets everyone in the print channel use it.",
           validator="guided user IDs / settings.user_ids"),
    _field("discord", "denied_user_ids", "Discord denied users", "id_list",
           "Optional deny list: numeric Discord user IDs refused even when listed or the "
           "approval user, separated by commas or spaces.",
           validator="settings.user_ids"),
    _field("discord", "approval_user_id", "Discord approval user", "text",
           "Numeric Discord user ID allowed to approve prints; always allowed.",
           validator="settings approval user check"),
    _field("discord", "timezone", "Timezone", "picker",
           "IANA timezone name used for display and the daily reset.",
           choices_source=SOURCE_TIMEZONES),
    _field("discord", "chat_llm_enabled", "Discord chat LLM", "toggle",
           "Free-text chat replies from the [llm] model (or the [agent] lane, when "
           "configured) on Discord. On by default; "
           "off runs Discord as a pure bot (unmatched text gets the short not-understood reply) "
           "while [llm] stays configured for /print-design. The roast and camera "
           "watchdog keep their own vision switches either way.",
           validator="settings discord check"),
    _field("discord", "clarify_ui", "Discord clarify UI", "toggle",
           "Who draws Print Handy's questions on a Hermes install (#524 stage 2). "
           "Off by default: Hermes' adapter renders them, as before. On: Print Handy "
           "posts its questions itself by direct Discord REST and resolves clicks and "
           "typed answers in its own prompt registry — one renderer in both modes, "
           "matching the standalone rendering. The standalone bot always uses its "
           "own renderer either way. Takes effect on restart.",
           validator="settings discord check"),
    _field("telegram", "chat_id", "Telegram chat", "text",
           "Telegram chat (group) ID; group and channel IDs are negative.",
           validator="settings telegram check"),
    _field("telegram", "allowed_user_ids", "Telegram allowed users", "id_list",
           "Telegram user IDs allowed to use the standalone bot, separated by commas or spaces.",
           validator="settings.user_ids (telegram)"),
    _field("telegram", "approval_user_id", "Telegram approval user", "text",
           "Telegram user ID allowed to approve prints; always allowed.",
           validator="settings telegram check"),
    _field("matrix", "homeserver_url", "Matrix homeserver URL", "url",
           "Matrix homeserver base URL, for example https://matrix.org.",
           validator="settings matrix check"),
    _field("matrix", "room_id", "Matrix room", "text",
           "Matrix room ID of the print room (starts with '!'); the room must be unencrypted.",
           validator="settings matrix check"),
    _field("matrix", "allowed_user_ids", "Matrix allowed users", "id_list",
           "Matrix user IDs allowed to use the standalone bot, full @user:server form, "
           "separated by commas or spaces.",
           validator="settings.matrix_user_ids"),
    _field("matrix", "approval_user_id", "Matrix approval user", "text",
           "Matrix user ID allowed to approve prints, full @user:server form; always allowed.",
           validator="settings matrix check"),
    # Services (guided setup: Moonraker and Spoolman URLs).
    _field("services", "moonraker_url", "Printer URL", "url",
           "The address Print Handy uses to talk to a Moonraker printer. That can be "
           "Moonraker on the printer, or Klipper on another computer. Example: "
           "http://printer.local:7125. Leave empty when this model does not use Moonraker.",
           validator="guided service URL / settings services check"),
    _field("services", "octoprint_url", "Printer URL", "url",
           "The address Print Handy uses to talk to OctoPrint, including an OctoPi "
           "that is not running on the printer. Example: http://octopi.local. "
           "Leave empty when this model does not use OctoPrint.",
           validator="settings services check"),
    _field("bambu", "lan_host", "Printer URL", "host",
           "Bambu Lab LAN hostname or IP. Not a full URL. Empty means no LAN connection.",
           validator="settings bambu check"),
    _field("bambu", "lan_serial", "Printer serial", "text",
           "Bambu Lab printer serial number. The LAN connection needs the host and "
           "the serial together — settings validation refuses one without the other. "
           "The LAN access code itself stays in the environment, never here.",
           validator="settings bambu check"),
    _field("prusa", "link_host", "Printer URL", "host",
           "PrusaLink hostname or IP. Not a full URL. Empty means no LAN connection.",
           validator="settings prusa check"),
    _field("flashforge_stock", "host", "Printer URL", "host",
           "Stock Flashforge LAN hostname or IP. Not a full URL. Empty means offline.",
           validator="settings flashforge stock check"),
    _field("elegoo_sdcp", "host", "Printer URL", "host",
           "Elegoo SDCP hostname or IP. Not a full URL. Empty means no LAN connection.",
           validator="settings elegoo sdcp check"),
    _field("services", "spoolman_url", "Spoolman URL", "url",
           "Spoolman URL for filament inventory; empty when Spoolman is not used.",
           validator="guided Spoolman URL / settings services check"),
    _field("spool", "prompt_brand", "Ask brand for new spools", "toggle",
           "Ask for the brand/vendor during the conversational new-spool "
           "walkthrough (#512). Off by default: the walkthrough skips brand "
           "and the spool is created without one; on inserts the brand step "
           "for operators who track brands.",
           validator="settings spool check"),
    # Event notifications (#622): print events out of chat, over MQTT
    # and/or ntfy. Neither channel's credential is a field: the MQTT
    # password is MQTT_PASSWORD and the ntfy access token is
    # NTFY_ACCESS_TOKEN, both environment only — the help texts say so,
    # matching the catalog's no-secrets rule.
    _field("notify", "mqtt_enabled", "MQTT events", "toggle",
           "Publish print events (started, paused, runout, complete, failed) to "
           "an MQTT broker (#622). Off by default; a failed publish never "
           "blocks chat or a print.",
           validator="settings notify check"),
    _field("notify", "mqtt_host", "MQTT broker host", "host",
           "MQTT broker hostname or IP; empty keeps MQTT off even with the "
           "switch on.",
           validator="settings notify check"),
    _field("notify", "mqtt_port", "MQTT broker port", "number",
           "MQTT broker port; 0 uses the default 1883.",
           validator="settings notify check", minimum=0, maximum=65535),
    _field("notify", "mqtt_topic", "MQTT topic root", "text",
           "Topic root each event is published under, as <topic root>/<event> — "
           "for example print-handy/print_started.",
           validator="settings notify check"),
    _field("notify", "mqtt_username", "MQTT username", "text",
           "MQTT broker username; empty connects anonymously. The password is "
           "MQTT_PASSWORD in the environment — it is never a setting and has "
           "no field here.",
           validator="settings notify check"),
    _field("notify", "ntfy_enabled", "ntfy events", "toggle",
           "Publish the same print events to an ntfy topic (#622). Off by "
           "default; a failed publish never blocks chat or a print.",
           validator="settings notify check"),
    _field("notify", "ntfy_url", "ntfy server URL", "url",
           "ntfy server URL, for example https://ntfy.sh. The topic below is "
           "appended to it, or name the topic in the URL itself and leave "
           "the topic empty.",
           validator="settings notify check"),
    _field("notify", "ntfy_topic", "ntfy topic", "text",
           "ntfy topic the events are published to. A protected topic's access "
           "token is NTFY_ACCESS_TOKEN in the environment — never a setting.",
           validator="settings notify check"),
    # Model endpoints: the OpenAI-compatible chat endpoints. Only URLs
    # and model names are fields — an endpoint's API key stays in the
    # environment, like every other secret.
    _field("llm", "chat_url", "Chat endpoint URL", "url",
           "OpenAI-compatible chat endpoint for plain-language replies; empty "
           "leaves the chat lane off. Its API key stays in the environment.",
           validator="settings llm check"),
    _field("llm", "model", "Chat model", "text",
           "Model the chat endpoint uses."),
    _field("design", "chat_url", "Design endpoint URL", "url",
           "OpenAI-compatible endpoint /print-design uses when it differs from "
           "the chat endpoint; empty reuses the chat endpoint. Its API key "
           "stays in the environment.",
           validator="settings design check"),
    _field("design", "model", "Design model", "text",
           "Model the design endpoint uses."),
    _field("agent", "chat_url", "Agent endpoint URL", "url",
           "OpenAI-compatible endpoint for the agent lane; empty leaves the "
           "agent lane unconfigured. Its API key stays in the environment.",
           validator="settings agent check"),
    _field("agent", "model", "Agent model", "text",
           "Model the agent endpoint uses."),
    # Images (the camera source type, the snapshot URL and the vision
    # endpoint the watchdog uses; visual_watch itself is the next group.
    # API keys stay in the environment: vision_chat_url names no secret,
    # and there is no key field.)
    _field("services", "camera_source", "Camera source", "picker",
           "Which camera still images come from (#294): auto follows the configured "
           "printer vendor's rule, or pin one source — moonraker = the Moonraker "
           "webcam snapshot only, bambu = the printer's own Bambu camera, go2rtc = "
           "the go2rtc fan-out only (put the camera's rtsp:// address in RTSP "
           "stream URL), none = no camera images anywhere. The watchdog stills, "
           "the timelapse capture source and the Bambu camera paths all honor "
           "the pin and name it when they refuse.",
           choices=tuple(settings.CAMERA_SOURCES), validator="settings camera source check"),
    _field("services", "camera_snapshot_url", "Camera snapshot URL", "url",
           "Camera snapshot URL for status images and the camera watchdog; empty derives "
           "it from the Moonraker host.",
           validator="settings services check"),
    _field("timelapse", "capture_source_url", "RTSP stream URL", "text",
           "An rtsp:// or rtsps:// address. Print Handy hands it to go2rtc, which holds "
           "the one camera connection and fans the stream out for timelapse and live view. "
           "A go2rtc ffmpeg: source is also accepted. Empty keeps the snapshot URL.",
           validator="settings timelapse check"),
    _field("services", "vision_chat_url", "Vision chat URL", "url",
           "OpenAI-compatible chat endpoint used for camera checks; empty disables them. "
           "Its API key stays in the environment, never here.",
           validator="settings services check"),
    _field("vision", "model", "Vision model", "text",
           "Model the vision endpoint uses for camera checks."),
    _field("vision", "roast_model", "Roast model", "text",
           "Model the vision endpoint uses for the one-line roast on print posts."),
    _field("vision", "roast_enabled", "Roast", "toggle",
           "The one-line roast on the print-started post only (one per print, "
           "added when printing begins; never on the completion post), on every "
           "platform (Telegram included). On by default: it runs whenever an "
           "LLM/vision endpoint is available; off turns roasts off without "
           "disabling camera checks.",
           validator="settings vision check"),
    _field("vision", "local_detector_enabled", "Local failure detector", "toggle",
           "Built-in ONNX failure detector for the camera watchdog (#455): "
           "judges camera frames on this machine when no vision endpoint "
           "is configured, so a no-LLM, no-cloud install still gets "
           "bad-print detection. Off by default. The pinned model "
           "(Obico failure detector, AGPL-3.0 weights, about 202 MB) "
           "downloads once and is SHA-256 verified. It detects failures "
           "only — roasts, spool-label reads and the empty-plate check "
           "still need a vision endpoint.",
           validator="settings vision check"),
    _field("vision", "local_model_path", "Local detector model path", "text",
           "Where the local failure detector's ONNX model file lives. "
           "Empty uses the default location (a models folder under the "
           "Hermes home) and the pinned model; name a model file you "
           "placed yourself to run fully offline.",
           validator="settings vision check"),
    _field("vision", "auto_light_off_minutes", "Auto light-off minutes", "number",
           "Auto light-off (#516): the printer light turns on when a print "
           "starts and off again after the camera has seen an empty plate "
           "for this many minutes. 0 = off (the default). Needs a vision "
           "endpoint, like the camera watchdog. Covers Moonraker, Bambu "
           "Lab and Elegoo SDCP printers; other families have no light "
           "to switch.",
           validator="settings vision check", minimum=0),
    _field("vision", "light_object", "Light object", "text",
           "The Moonraker light object the auto light-off switches, for "
           "example 'led chamber_led', 'neopixel sb_leds' or "
           "'output_pin caselight'. Empty auto-discovers: the printer's "
           "light object is used only when there is exactly one. Bambu "
           "Lab and Elegoo SDCP printers ignore this — their chamber "
           "light is a fixed protocol target.",
           validator="settings vision check"),
    # Camera watch.
    _field("visual_watch", "enabled", "Camera watch", "toggle",
           "Camera watchdog switch; the watchdog is alert-only and never controls the printer.",
           validator="settings visual_watch check"),
    _field("visual_watch", "sensitivity", "Camera watch sensitivity", "number",
           "Camera watchdog sensitivity, 1 (quietest) to 5 (most alerts).",
           validator="settings visual_watch check", minimum=1, maximum=5),
    # Timelapse (the mode type picker, render waiting, size cap, retention,
    # and the SSH archive target). ssh_key is a filesystem *path* — the
    # same plain string the YAML already stores — never key contents, so
    # it is a text field like any other path and no secret value is involved.
    _field("timelapse", "capture_enabled", "In-container capture", "toggle",
           "Print Handy captures the timelapse itself, polling camera frames "
           "in-container (#246). In rendered mode a finished capture is "
           "preferred over the printer-rendered clip; in container mode "
           "capture is the timelapse; mode off stops capture whatever "
           "this says.",
           validator="settings timelapse check"),
    _field("timelapse", "mode", "Timelapse mode", "picker",
           "How timelapses are made (#246/#295): rendered = the printer renders it "
           "(moonraker-timelapse), with in-container capture preferred when on; "
           "container = the in-container capture is the timelapse and the "
           "printer-rendered path is not used; off = no timelapses — capture "
           "and the rendered path both stop. A file value left empty derives "
           "rendered when a Moonraker URL is configured, else container when "
           "in-container capture is on, else off.",
           choices=tuple(settings.TIMELAPSE_MODES), validator="settings timelapse check"),
    _field("timelapse", "render_wait_s", "Timelapse render wait", "number",
           "How long to wait for a printer-rendered timelapse to appear after a print "
           "finishes, in seconds.",
           validator="settings type check", minimum=1),
    _field("timelapse", "render_poll_s", "Timelapse render poll", "number",
           "How often to check for the rendered timelapse while waiting, in seconds.",
           validator="settings type check", minimum=1),
    _field("timelapse", "max_bytes", "Timelapse max size", "number",
           "Largest timelapse file kept or sent, in bytes.",
           validator="settings type check", minimum=1),
    _field("timelapse", "local_retention_days", "Timelapse local retention", "number",
           "Days a rendered timelapse is kept locally before cleanup.",
           validator="settings type check", minimum=0),
    _field("timelapse", "plan_clip_retention_days", "Plan clip retention", "number",
           "Days an unfinished plan's timelapse clip store is kept before cleanup.",
           validator="settings type check", minimum=0),
    _field("timelapse", "archive_host", "Timelapse archive host", "host",
           "SSH host finished timelapses are copied to for archiving; empty turns archiving off.",
           validator="settings type check"),
    _field("timelapse", "archive_dir", "Timelapse archive directory", "text",
           "Directory on the archive host that receives timelapse files.",
           validator="settings type check"),
    _field("timelapse", "ssh_key", "Timelapse archive SSH key path", "text",
           "Filesystem path of the SSH private key for the archive host; only the path "
           "is stored here, never the key contents.",
           validator="settings type check"),
    # Logging.
    _field("logging", "level", "Log level", "picker",
           "Log verbosity.",
           choices=tuple(settings.LOG_LEVELS)),
    _field("logging", "format", "Log format", "picker",
           "text is one readable line per event; json is one JSON object per line.",
           choices=tuple(settings.LOG_FORMATS)),
    # The Web UI itself. access_token_env is deliberately not editable here:
    # no *_env key is in the catalog (see the module docstring).
    _field("webui", "enabled", "Web UI", "toggle",
           "Optional Web UI switch; off by default, and nothing listens while disabled.",
           validator="settings webui check"),
    _field("webui", "host", "Web UI host", "host",
           "Host the Web UI binds to when enabled; a plain IP address or hostname.",
           validator="settings webui check"),
    _field("webui", "port", "Web UI port", "number",
           "Port the Web UI binds to when enabled; 0 means no port has been chosen yet.",
           validator="settings webui check", minimum=0, maximum=65535),
    _field("webui", "controls_enabled", "Printer control API", "toggle",
           "Serve the Web UI control routes (#708): a caller holding the access "
           "token can pause, resume or cancel a print and set target temperatures "
           "through the API, with no chat confirmation. Off by default, and no "
           "control routes are served while disabled; chat control stays "
           "confirm-first either way. Takes effect at the next Web UI start.",
           validator="settings webui check"),
    # Live camera in Discord (#311): the [live_stream] section. No secret
    # is a field here — the Discord client secret and the Activity
    # session secret live only in the environment, and the tunnel token
    # belongs to the cloudflared sidecar; this group carries exactly the
    # settings the loader validates.
    _field("live_stream", "enabled", "Live camera in Discord", "toggle",
           "Live camera viewing in Discord (#311) switch; off by default, and "
           "/print-live only reports that while disabled.",
           validator="settings live_stream check"),
    _field("live_stream", "voice_channel_id", "Live voice channel", "text",
           "Numeric ID of the one private Discord voice channel the live "
           "Activity is launched in; the bot never names another channel.",
           validator="settings live_stream check"),
    _field("live_stream", "auto_start_on_print", "Auto-start on print", "toggle",
           "Start the live view automatically when a print starts and post "
           "the Activity pointer; off by default — /print-live start is the "
           "manual trigger. The session still ends itself after the empty "
           "timeout below.",
           validator="settings live_stream check"),
    _field("live_stream", "max_height", "Live feed height", "number",
           "Live feed height cap in pixels; width follows the camera's aspect. "
           "While all four feed values (height, frame rate, bitrate cap, "
           "encoder) hold their defaults the live view is the camera stream "
           "relayed as it arrives; changing any one makes the live view a "
           "transcoded feed with all four applied. Timelapse capture keeps "
           "the camera's own stream either way.",
           validator="settings live_stream check", minimum=144, maximum=2160),
    _field("live_stream", "fps", "Live feed frame rate", "number",
           "Live feed frame-rate cap, in frames per second; applied by the "
           "live transcode described under Live feed height.",
           validator="settings live_stream check", minimum=1, maximum=60),
    _field("live_stream", "max_bitrate_kbps", "Live feed bitrate cap", "number",
           "Live feed bitrate cap in kbit/s; applied by the live transcode "
           "described under Live feed height.",
           validator="settings live_stream check", minimum=100, maximum=20000),
    _field("live_stream", "encoder", "Live feed encoder", "picker",
           "Encoder preference for the live feed's transcode: auto picks the "
           "best encoder the machine's ffmpeg reports (hardware first, CPU "
           "last); a named encoder is preferred and falls back in that order "
           "when it is unavailable.",
           choices=tuple(settings.LIVE_STREAM_ENCODERS),
           validator="settings live_stream check"),
    _field("live_stream", "empty_timeout_s", "Live empty timeout", "number",
           "Seconds the live view may stay empty of viewers before the "
           "session ends itself.",
           validator="settings live_stream check", minimum=1),
    _field("live_stream", "activity_origin", "Live Activity origin", "url",
           "Public https origin the Discord Activity page is served from: "
           "the named Cloudflare Tunnel hostname mapped in the Developer "
           "Portal (see docs/DISCORD-BOT-SETUP.md).",
           validator="settings live_stream check"),
    _field("live_stream", "port", "Live Activity port", "number",
           "Loopback port the supervised Activity server binds inside the "
           "deployment; 0 means no port has been chosen yet, so the server "
           "does not start.",
           validator="settings live_stream check", minimum=0, maximum=65535),
)

_FIELDS = {field.id: field for field in CATALOG}


def field_by_id(field_id):
    """The catalog field for ``"section.key"`` (or a ``(section, key)`` pair), else None."""
    if isinstance(field_id, Field):
        return field_id
    if isinstance(field_id, tuple):
        field_id = f"{field_id[0]}.{field_id[1]}"
    return _FIELDS.get(str(field_id))


def choices_for(field, *, model_key=None, data=None):
    """The current choices for a picker field, resolved from its choices source.

    Vendor sources need the configured printer model: pass ``model_key``
    or a loaded settings mapping as ``data``.
    """
    field = field_by_id(field)
    if field is None:
        raise ValueError(f"unknown catalog field {field!r}")
    if field.choices_source is None:
        return tuple(field.choices)
    if field.choices_source == SOURCE_PRINTER_REGISTRY:
        return tuple(sorted(
            printers.MODELS,
            key=lambda key: (printers.MODELS[key].label.casefold(), key),
        ))
    if field.choices_source == SOURCE_BOT_PLATFORMS:
        from ..bot.platform import KNOWN_PLATFORMS
        return tuple(KNOWN_PLATFORMS)
    if field.choices_source == SOURCE_TIMEZONES:
        from zoneinfo import available_timezones
        return tuple(sorted(available_timezones()))
    if field.choices_source in (SOURCE_VENDOR_SLICERS, SOURCE_VENDOR_DELIVERY):
        if model_key is None and data is not None:
            model_key = (data.get("printer") or {}).get("model")
        if not model_key:
            raise ValueError(f"{field.id}: choices need the configured printer model")
        vendor = printers.MODELS[settings._model_key(model_key)].vendor
        return tuple(vendor.slicers if field.choices_source == SOURCE_VENDOR_SLICERS
                     else vendor.delivery)
    raise ValueError(f"{field.id}: unknown choices source {field.choices_source!r}")  # pragma: no cover


def choice_label(field, choice) -> str:
    """The text a picker shows for one stored choice. Model keys stay the values."""
    field = field_by_id(field)
    if field is not None and field.choices_source == SOURCE_PRINTER_REGISTRY:
        model = printers.MODELS.get(str(choice))
        if model is not None:
            return model.label
    return str(choice)


# One connection, one address field. The others stay in the form as hidden
# inputs so a save does not clear a URL that belongs to a different model.
PRINTER_URL_BY_CONNECTION = {
    "moonraker": "services.moonraker_url",
    "octoprint": "services.octoprint_url",
    "bambulab_lan": "bambu.lan_host",
    "prusalink": "prusa.link_host",
    "flashforge_stock": "flashforge_stock.host",
    "elegoo_sdcp": "elegoo_sdcp.host",
}


def _configured_model(data):
    if not isinstance(data, dict):
        return None
    raw = (data.get("printer") or {}).get("model")
    if not raw:
        return None
    try:
        return printers.MODELS[settings._model_key(raw)]
    except (KeyError, ValueError, AttributeError):
        return None


def config_page_visible(field, data) -> bool:
    """Whether /config draws this field. Hidden fields are still submitted."""
    field = field_by_id(field)
    if field is None:
        return True
    model = _configured_model(data)
    if field.id == "printer.zmod_delivery_format":
        return bool(model and model.vendor.key == "flashforge")
    if field.id == "bambu.lan_serial":
        # The serial belongs with the Bambu LAN address: it follows the
        # same per-model visibility as bambu.lan_host, so the pair the
        # validator requires is always shown (or hidden) together.
        return config_page_visible("bambu.lan_host", data)
    if field.id in PRINTER_URL_BY_CONNECTION.values():
        if model is None:
            return field.id == "services.moonraker_url"
        return field.id == PRINTER_URL_BY_CONNECTION.get(
            model.vendor.connection, "services.moonraker_url")
    return True
