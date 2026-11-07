# Settings reference

Every user-adjustable setting, its YAML key, its environment variable and its value in the Docker configuration (`config/docker.yaml` over `config/home.yaml` and the printer model). `tests/test_docker_release.py` derives the inventory from the settings schema and fails when a setting lacks a row here or an `.env.example` line, or when a documented name is not a real setting.

* **YAML**: put keys in a file and select it with `PRINT_HANDY_CONFIG`. In Docker, mount the file and point `PRINT_HANDY_CONFIG` at its container path.
* **Your own file**: keys in `~/.config/print-handy/settings.yaml` (Docker: `/data/.config/print-handy/settings.yaml`, appdata `dot-config/print-handy/settings.yaml`) layer over the selected file, so personal values such as `material_tuning` survive image updates. `PRINT_HANDY_USER_CONFIG` names a different path.
* **Environment**: `PRINT_HANDY__SECTION__KEY=value` overrides any file. Booleans accept `true/false`, `1/0`, `yes/no`, `on/off`. Integer settings need integer spelling (`12`, not `12.0`). A blank value for a text setting is ignored, so the file value stays; the environment cannot clear a file-set string (P1-64: an unfilled Compose variable must not erase a mounted config's value), and a blank number or boolean is still an error. Rows marked *file only* are lists or tables and can only come from YAML or legacy TOML; the one list exception is `delivery.methods`, which also accepts a comma- or space-separated environment value (`PRINT_HANDY__DELIVERY__METHODS=discord,bambu_handoff,printer`, #270).
* **Precedence**, later wins: vendor and printer model defaults (*model*), `config/home.yaml`, the selected config file, environment. Settings are read once at start, so restart the service after a change.
* A typo or invalid value stops startup and names the setting. YAML holds no secrets; see [Secrets](#secrets).

Schema, validation limits and TOML migration: [CONFIGURATION-YAML.md](CONFIGURATION-YAML.md). Standalone bot settings: [STANDALONE.md](STANDALONE.md). Printer vendor profiles: [PRINTER-PROFILES.md](PRINTER-PROFILES.md).

| YAML key | Environment variable | Docker value | What it does |
|---|---|---|---|
| `layout.single_copy_edge_mm` | `PRINT_HANDY__LAYOUT__SINGLE_COPY_EDGE_MM` | `1.0` *model* | Bed-edge clearance for one copy |
| `layout.multi_copy_edge_mm` | `PRINT_HANDY__LAYOUT__MULTI_COPY_EDGE_MM` | `3.0` *model* | Bed-edge clearance for several copies |
| `layout.spacing` | `PRINT_HANDY__LAYOUT__SPACING` | `minimum` | Part gap and margin preset: `minimum`, `low`, `medium` (Standard) or `high`; fills the seven gap keys below (see [Part spacing presets](#part-spacing-presets)) |
| `layout.arrange_gap_mm` | `PRINT_HANDY__LAYOUT__ARRANGE_GAP_MM` | `2.0` *preset* | Gap between parts rearranged onto one plate |
| `layout.arrange_edge_mm` | `PRINT_HANDY__LAYOUT__ARRANGE_EDGE_MM` | `1.0` *preset* | Bed-edge margin for parts rearranged onto one plate (a job's brim is added on top) |
| `layout.merge_edge_margin_mm` | `PRINT_HANDY__LAYOUT__MERGE_EDGE_MARGIN_MM` | `1.0` *preset* | Edge margin when merging plates |
| `layout.merge_spacing_mm` | `PRINT_HANDY__LAYOUT__MERGE_SPACING_MM` | `2.0` *preset* | Gap between merged plates' parts |
| `layout.max_merged_hours` | `PRINT_HANDY__LAYOUT__MAX_MERGED_HOURS` | `12` | Merged plates longer than this stay separate |
| `prime_tower.size_mm` | `PRINT_HANDY__PRIME_TOWER__SIZE_MM` | `45.0` *model* | Prime tower size |
| `prime_tower.part_clearance_mm` | `PRINT_HANDY__PRIME_TOWER__PART_CLEARANCE_MM` | `3.5` *preset* | Prime tower to part clearance |
| `prime_tower.edge_mm` | `PRINT_HANDY__PRIME_TOWER__EDGE_MM` | `4.0` *preset* | Prime tower to bed edge clearance |
| `prime_tower.merge_slack_mm` | `PRINT_HANDY__PRIME_TOWER__MERGE_SLACK_MM` | `1.0` *preset* | Extra room kept for the tower when merging |
| `purge.mode` | `PRINT_HANDY__PURGE__MODE` | `auto` | ZMod color-change purge: `auto` (from the printer's `use_trash_on_print`), `printer_chute`, `prime_tower` or `slicer_chute` (see [Purge and ZMod plugins](#purge-and-zmod-plugins)) |
| `purge.prime_tower` | `PRINT_HANDY__PURGE__PRIME_TOWER` | `auto` | Slice and reserve room for a prime tower: `auto` (off for `printer_chute`, on otherwise), `on` or `off` |
| `purge.flush_multiplier` | `PRINT_HANDY__PURGE__FLUSH_MULTIPLIER` | unset | Optional Orca flush volume multiplier (0.05 to 5, QuickSwap recommends about 0.2); unset leaves the profile unchanged |
| `zmod.plugins` | `PRINT_HANDY__ZMOD__PLUGINS` | `auto` | `auto` detects the printer's enabled ZMod plugins; a list (`quickswap, ifs_jacker`) overrides detection |
| `printer.firmware` | `PRINT_HANDY__PRINTER__FIRMWARE` | `Klipper` *model* | Firmware label shown in job summaries |
| `printer.start_macro` | `PRINT_HANDY__PRINTER__START_MACRO` | `PRINT_START` *model* | Klipper start macro for stock Klipper models |
| `printer.end_macro` | `PRINT_HANDY__PRINTER__END_MACRO` | `PRINT_END` *model* | Klipper end macro for stock Klipper models |
| `printer.name` | `PRINT_HANDY__PRINTER__NAME` | `Generic Klipper` *model* | Printer name shown in job summaries |
| `printer.bed_mm` | file only | `[220.0, 220.0]` *model* | Usable print area `[x, y]` in mm |
| `printer.max_print_height_mm` | `PRINT_HANDY__PRINTER__MAX_PRINT_HEIGHT_MM` | `250.0` *model* | Tallest allowed print |
| `printer.nozzle_diameter_mm` | `PRINT_HANDY__PRINTER__NOZZLE_DIAMETER_MM` | `0.4` *model* | Installed nozzle; slicing refuses any other |
| `printer.filament_slots` | `PRINT_HANDY__PRINTER__FILAMENT_SLOTS` | `1` *model* | Filament slots; 1 for a single extruder — on a `multi_slot` model the feeder's slots (IFS bays, or an AMS unit's trays on the single-nozzle Bambu models, #671). Renamed from `printer.ifs_slots` (#672): the old key in a config file and the old environment spelling (ending `__IFS_SLOTS`) still load as a deprecated alias, and this name wins when both spellings are set |
| `printer.tool_count` | `PRINT_HANDY__PRINTER__TOOL_COUNT` | `1` *model* | Independent toolheads on a tool-changer model (Creator 5 / 5 Pro: 4; Prusa XL: 5; Snapmaker U1: 4); slicer tool *i* is toolhead *i*, never an IFS slot |
| `printer.model` | `PRINT_HANDY__PRINTER__MODEL` | `klipper.generic` | Printer brand and model: `klipper.generic`, `flashforge.ad5x`, `sovol.sv06`, `octoprint.marlin`, `bambulab.a1`, `bambulab.p1s`, `snapmaker.u1` (Bambu Lab models are offline only). Sets every model default marked *model* |
| `printer.vendor` | `PRINT_HANDY__PRINTER__VENDOR` | `klipper` *model* | Vendor part of the model key, set by the model; `vendor` plus a bare printer name is also accepted |
| `printer.slicer` | `PRINT_HANDY__PRINTER__SLICER` | `orca` *model* | Slicer: `orca` everywhere; Bambu Lab printers default to `bambu_studio` and may pick `orca` |
| `printer.zmod_delivery_format` | `PRINT_HANDY__PRINTER__ZMOD_DELIVERY_FORMAT` | `auto` *model* (Flashforge/ZMod only) | Print-file format sent to a ZMod printer: `auto` sends the sliced `.gcode.3mf` when the printer reports ZMod 1.7.5+ (instant colour mapping, no MD5 preprocessor, instant KAMP data) and plain `.gcode` when the version is older or unknown; `gcode` always sends `.gcode`; `3mf` always sends the package. Setting it for another vendor is refused |
| `slicer.flash_studio_path` | `PRINT_HANDY__SLICER__FLASH_STUDIO_PATH` | empty | Optional path to a Flash Studio executable the user already installed. Empty keeps OrcaSlicer. A set but missing or non-executable path is an error, not a fallback. Never downloaded or bundled. |
| `printer.nozzle_type` | `PRINT_HANDY__PRINTER__NOZZLE_TYPE` | unset | Optional nozzle material, limited to the model's supported types |
| `orca.templates` | `PRINT_HANDY__ORCA__TEMPLATES` | the model's folder *model* | Folder with the Orca machine, process and filament JSON |
| `orca.command` | file only | `["/usr/local/bin/orca-slicer"]` | How to start the OrcaSlicer CLI; Print Handy ships one bundled OrcaSlicer and `PRINT_HANDY_ORCA_CHANNEL=stable` (default) selects it (`preview` and `alpha` start on the same single build with a startup notice; the preview channel was removed); the binary exists only when `WITH_ORCA=true` |
| `delivery.methods` | `PRINT_HANDY__DELIVERY__METHODS` | `["printer"]` *model* | Post-slice options offered, from the vendor profile (for example `printer`); comma- or space-separated in the environment, an empty value keeps the file value |
| `bambu.command` | file only | `["/usr/bin/bambu-studio"]` | Bambu Studio CLI: the default slicer for Bambu Lab printers, Orca's one-shot fallback elsewhere; exists only in a `WITH_BAMBU=true` image |
| `bambu.lan_host` | `PRINT_HANDY__BAMBU__LAN_HOST` | `""` | Bambu Lab printer LAN address for read-only status (#187); must be set together with `lan_serial`; empty disables LAN status |
| `bambu.lan_serial` | `PRINT_HANDY__BAMBU__LAN_SERIAL` | `""` | The Bambu Lab printer's serial number (stored upper case); used in the MQTT topics, not as a credential |
| `bambu.lan_access_code_env` | `PRINT_HANDY__BAMBU__LAN_ACCESS_CODE_ENV` | `BAMBU_LAN_PASSWORD` | Name of the environment variable holding the printer's LAN access code; the code itself is never a setting |
| `bambu.start_confirm_s` | `PRINT_HANDY__BAMBU__START_CONFIRM_S` | `90` | Bambu Lab LAN print start (#271): how long, in whole seconds (1-300), the printer is polled every 10 s to report the submitted file as preparing or running before the start is left as uncertain; keep it well under the bot's 420 s start budget, which also covers the upload |
| `flashforge_stock.host` | `PRINT_HANDY__FLASHFORGE_STOCK__HOST` | `""` | Stock Flashforge printer LAN address; empty leaves the printer offline. The serial and check code stay in `AD5X_SERIAL` and `AD5X_CHECK_CODE`, never in a setting |
| `prusa.link_host` | `PRINT_HANDY__PRUSA__LINK_HOST` | `""` | Prusa printer LAN address for PrusaLink; empty leaves the printer offline (slicing, checks and Discord delivery only). Prusa models only |
| `elegoo_sdcp.host` | `PRINT_HANDY__ELEGOO_SDCP__HOST` | `""` | Centauri printer LAN address for SDCP; empty leaves the printer offline (slicing, checks and Discord delivery only). Elegoo SDCP models only. SDCP has no authentication; there is no key to configure |
| `prusa.api_key_env` | `PRINT_HANDY__PRUSA__API_KEY_ENV` | `"PRUSA_LINK_API_KEY"` | Name of the environment variable holding the PrusaLink API key. The key itself is never a setting |
| `paths.hermes_home` | `PRINT_HANDY__PATHS__HERMES_HOME` | `/data/state` | State folder (`state/`, `logs/`); the key name is historical, Hermes is not required. `/data/hermes` was the Docker path before 2.2.5 and stays accepted (#914): the entrypoint links `/data/state` to it when only the legacy volume holds data |
| `paths.archive` | `PRINT_HANDY__PATHS__ARCHIVE` | `/data/archive` | Job archive |
| `paths.records_vault` | `PRINT_HANDY__PATHS__RECORDS_VAULT` | `/data/records` | Print records: one JSON file per print in `Prints/` |
| `paths.cache` | `PRINT_HANDY__PATHS__CACHE` | `/data/cache` | Regenerable cache |
| `paths.hermes_bin` | `PRINT_HANDY__PATHS__HERMES_BIN` | `/usr/local/bin/hermes` | Hermes CLI; Hermes installs only, not in the image |
| `paths.ffmpeg` | `PRINT_HANDY__PATHS__FFMPEG` | `ffmpeg` | ffmpeg binary or name on `PATH` |
| `paths.piper` | `PRINT_HANDY__PATHS__PIPER` | `piper` | Piper text-to-speech binary or name on `PATH`, for Wrapped narration (#981). Also accepted as the legacy name `PIPER_BIN` |
| `paths.piper_model` | `PRINT_HANDY__PATHS__PIPER_MODEL` | empty | Piper voice model file (the `--model` file Piper needs in order to speak), for Wrapped narration (#981). Empty means no model is configured; a piper narration run without one is a named note in the video result, not a failure. Also accepted as the legacy name `PIPER_MODEL` |
| `paths.hermes_agent` | `PRINT_HANDY__PATHS__HERMES_AGENT` | `/data/state/hermes-agent` | Hermes agent checkout (Hermes installs only) |
| `paths.hermes_restart_helper` | `PRINT_HANDY__PATHS__HERMES_RESTART_HELPER` | `/data/state/restart_hermes.py` | Hermes restart helper (Hermes installs only) |
| `services.moonraker_url` | `PRINT_HANDY__SERVICES__MOONRAKER_URL` | empty | Printer URL for a Moonraker connection. Klipper may be on another computer. Example: `http://printer.example:7125`. Required by the listener |
| `services.moonraker_ws_url` | `PRINT_HANDY__SERVICES__MOONRAKER_WS_URL` | empty | Moonraker websocket; derived from `moonraker_url` when empty |
| `services.camera_source` | `PRINT_HANDY__SERVICES__CAMERA_SOURCE` | `auto` | Which camera still images come from (#294): `auto` follows the printer vendor's rule, or pin one source. The pin is honored by the go2rtc fan-out source selection, the camera watchdog's stills and the Bambu camera paths: `moonraker` uses the Moonraker webcam snapshot only, `bambu` the printer's own Bambu camera, `go2rtc` the go2rtc fan-out only (a stream source, not a stills-only URL), and `none` turns camera images off — no stills, no timelapse capture, no fan-out. A path the pin excludes refuses and names the pin instead of silently using another source |
| `services.camera_snapshot_url` | `PRINT_HANDY__SERVICES__CAMERA_SNAPSHOT_URL` | empty | Camera snapshot URL; derived from the Moonraker host when empty |
| `services.spoolman_url` | `PRINT_HANDY__SERVICES__SPOOLMAN_URL` | empty | Spoolman URL. The Docker config and Compose default to `http://127.0.0.1:8000`, the bundled Spoolman API in the same container. Set another URL, plus `PRINT_HANDY_BUNDLED_SPOOLMAN=false`, to use an existing external Spoolman instead |
| `services.vision_chat_url` | `PRINT_HANDY__SERVICES__VISION_CHAT_URL` | empty | OpenAI-compatible endpoint for camera checks; empty disables them (roasts are local and never use it, #1002). Needs a vision-capable model. See [Model endpoints](#model-endpoints) |
| `services.stock_api_port` | `PRINT_HANDY__SERVICES__STOCK_API_PORT` | `8898` | Flashforge stock API port for IFS presence |
| `services.octoprint_url` | `PRINT_HANDY__SERVICES__OCTOPRINT_URL` | empty | Printer URL for OctoPrint, including Sovol Marlin keys and an OctoPi that is not the printer. API key: `OCTOPRINT_API_KEY` secret |
| `discord.chat_id` | `PRINT_HANDY__DISCORD__CHAT_ID` | empty | Discord channel ID |
| `discord.approval_user_id` | `PRINT_HANDY__DISCORD__APPROVAL_USER_ID` | empty | Discord user IDs allowed to approve, comma or space separated. Several household members can be listed |
| `discord.approval_all_users` | `PRINT_HANDY__DISCORD__APPROVAL_ALL_USERS` | `false` | `true` lets anyone allowed to use the bot approve prints. A user on `denied_user_ids` is still refused |
| `discord.upload_max_bytes` | `PRINT_HANDY__DISCORD__UPLOAD_MAX_BYTES` | `9500000` | Largest upload per file on Discord: video is shrunk to it, and a sliced file or Bambu package over it is reported with its host path instead of offered for sending (see [PRINTER-PROFILES.md](PRINTER-PROFILES.md)). Keep it under your server's Discord cap (10 MB unboosted). This row governs Discord only — Telegram and Matrix posts are sized to their own `upload_max_bytes` rows below |
| `discord.post_print_quiet_s` | `PRINT_HANDY__DISCORD__POST_PRINT_QUIET_S` | `7200` | Retired (#524): the bot no longer resets chat sessions after prints. Kept in the schema so existing configs still load; session resets are the agent host's own setting |
| `discord.daily_reset_hour` | `PRINT_HANDY__DISCORD__DAILY_RESET_HOUR` | `4` | Retired (#524): see `post_print_quiet_s`. The shipped Hermes profile template still resets its sessions daily at 4 AM on its own config |
| `discord.timezone` | `PRINT_HANDY__DISCORD__TIMEZONE` | `America/New_York` | Display and reset time zone (IANA name) |
| `discord.bot_name` | `PRINT_HANDY__DISCORD__BOT_NAME` | `Print Handy` | Name the bot uses for itself |
| `discord.question_timeout_s` | `PRINT_HANDY__DISCORD__QUESTION_TIMEOUT_S` | `1800` | How long a Discord question waits for a button |
| `discord.safety_confirm_timeout_s` | `PRINT_HANDY__DISCORD__SAFETY_CONFIRM_TIMEOUT_S` | `600` | How long a safety confirmation waits |
| `discord.upload_wait_s` | `PRINT_HANDY__DISCORD__UPLOAD_WAIT_S` | `3600` | How long an upload-wait freeze stays active |
| `discord.cold_pull_outcome_s` | `PRINT_HANDY__DISCORD__COLD_PULL_OUTCOME_S` | `1800` | How long a cold-pull outcome prompt stays open |
| `discord.token_env` | `PRINT_HANDY__DISCORD__TOKEN_ENV` | `DISCORD_BOT_TOKEN` | Name of the environment variable holding the bot token |
| `discord.allowed_user_ids` | `PRINT_HANDY__DISCORD__ALLOWED_USER_IDS` | empty | Optional whitelist of Discord user IDs for the standalone bot, comma or space separated. Empty (the default) lets everyone in the print channel use the bot — the channel is the access boundary (#386) |
| `discord.denied_user_ids` | `PRINT_HANDY__DISCORD__DENIED_USER_IDS` | empty | Optional deny list of Discord user IDs, comma or space separated. A denied user is refused even when whitelisted or the approval user; a refusal is logged at INFO and answered in channel once per user per hour. Applies to bot messages, commands and button presses; the live camera Activity keeps its own allowlist (`approval_user_id` plus `allowed_user_ids`) |
| `discord.chat_llm_enabled` | `PRINT_HANDY__DISCORD__CHAT_LLM_ENABLED` | `true` | Discord's chat-LLM switch (#466). `false` runs Discord as a pure bot: no stray-chat model call of any kind (no `[llm]` call, no `[agent]` lane call), unmatched messages get the short not-understood reply, even while `[llm]` stays configured for `/print-design` (which inherits it). The roast and the camera watchdog use the separate vision endpoint and their own switches, so they are unaffected. `bot check` names this switch on its llm and agent rows when it is off. The switch means the same thing in Hermes mode (#524 stage 3): a final plugin hook claims unmatched Discord text for the bot's own lanes — with the switch off the Hermes profile agent never sees the text, and with a lane configured the bot's lane answers instead of the in-process agent; with the switch on and no lane configured, the profile agent keeps answering as before |
| `discord.clarify_ui` | `PRINT_HANDY__DISCORD__CLARIFY_UI` | `false` | Who draws Print Handy's questions on a Hermes install (#524 stage 2). `false` (the default): Hermes' adapter renders them, exactly as before. `true`: Print Handy activates its own prompt registry and posts questions itself by direct Discord REST, resolving clicks and typed answers itself — one renderer in both modes, matching the standalone rendering and the README mockups. The standalone bot always uses its own renderer either way. Read when the plugin registers; restart the gateway to change it. `bot check` reports the renderer on its clarify ui row |
| `telegram.token_env` | `PRINT_HANDY__TELEGRAM__TOKEN_ENV` | `TELEGRAM_BOT_TOKEN` | Name of the environment variable holding the Telegram bot token |
| `telegram.chat_id` | `PRINT_HANDY__TELEGRAM__CHAT_ID` | empty | Telegram chat (group) ID; group and channel IDs are negative |
| `telegram.allowed_user_ids` | `PRINT_HANDY__TELEGRAM__ALLOWED_USER_IDS` | empty | Telegram user IDs allowed to use the standalone bot, comma or space separated |
| `telegram.approval_user_id` | `PRINT_HANDY__TELEGRAM__APPROVAL_USER_ID` | empty | Telegram user IDs allowed to approve, comma or space separated |
| `telegram.upload_max_bytes` | `PRINT_HANDY__TELEGRAM__UPLOAD_MAX_BYTES` | `50000000` | Largest file the Telegram bot uploads (the Bot API cloud limit); timelapse video is shrunk to this value, not to Discord's smaller cap |
| `matrix.homeserver_url` | `PRINT_HANDY__MATRIX__HOMESERVER_URL` | empty | Matrix homeserver base URL, for example `https://matrix.org` or a self-hosted server |
| `matrix.access_token_env` | `PRINT_HANDY__MATRIX__ACCESS_TOKEN_ENV` | `MATRIX_ACCESS_TOKEN` | Name of the environment variable holding the Matrix access token |
| `matrix.room_id` | `PRINT_HANDY__MATRIX__ROOM_ID` | empty | Matrix room ID of the print room (starts with `!`); the room must be unencrypted |
| `matrix.allowed_user_ids` | `PRINT_HANDY__MATRIX__ALLOWED_USER_IDS` | empty | Matrix user IDs allowed to use the standalone bot, full `@user:server` form, comma or space separated |
| `matrix.approval_user_id` | `PRINT_HANDY__MATRIX__APPROVAL_USER_ID` | empty | Matrix user ID allowed to approve |
| `matrix.bot_user_id` | `PRINT_HANDY__MATRIX__BOT_USER_ID` | empty | The bot's own Matrix user ID; empty asks the homeserver (whoami) at startup |
| `matrix.upload_max_bytes` | `PRINT_HANDY__MATRIX__UPLOAD_MAX_BYTES` | `50000000` | Largest file the Matrix bot uploads; lowered to the homeserver's media limit when the server reports a smaller one. Timelapse video is shrunk to that resulting limit; if even the smallest shrink cannot fit it, the completion post says the timelapse was recorded but is too large for the server instead of attaching the video |
| `bot.mode` | `PRINT_HANDY__BOT__MODE` | `standalone` | `standalone` or `hermes`; an explicit `hermes` in an existing config is unchanged — only an unset mode defaults to `standalone` (#524) |
| `bot.platform` | `PRINT_HANDY__BOT__PLATFORM` | `discord` | Chat platform for the standalone bot (#164): `discord`, `telegram` or `matrix` |
| `bot.health_port` | `PRINT_HANDY__BOT__HEALTH_PORT` | `0` | Serve `GET /healthz` on this port; `0` turns the HTTP check off |
| `bot.health_stale_s` | `PRINT_HANDY__BOT__HEALTH_STALE_S` | `120` | Bot health fails when its heartbeat is older than this |
| `bot.sync_commands` | `PRINT_HANDY__BOT__SYNC_COMMANDS` | `true` | Register the slash commands with Discord at startup |
| `llm.chat_url` | `PRINT_HANDY__LLM__CHAT_URL` | empty | Optional OpenAI-compatible chat URL; empty: unmatched text gets the short "didn't understand" reply. See [Model endpoints](#model-endpoints) |
| `llm.model` | `PRINT_HANDY__LLM__MODEL` | empty | LLM model name, required when `chat_url` is set |
| `llm.api_key_env` | `PRINT_HANDY__LLM__API_KEY_ENV` | `LLM_API_KEY` | Name of the environment variable holding the LLM API key |
| `llm.timeout_s` | `PRINT_HANDY__LLM__TIMEOUT_S` | `20` | LLM request timeout |
| `agent.chat_url` | `PRINT_HANDY__AGENT__CHAT_URL` | empty | Stray-chat agent lane (#524 stage 3): an agent endpoint — any OpenAI-compatible chat completions URL, e.g. a Hermes agent behind its API — answers Discord free text the handlers did not take, ahead of the `[llm]` call. Empty: no agent lane; stray chat uses `[llm]` if configured, else the short not-understood reply. The endpoint answers in its own voice; its replies are posted as-is and can never run a command. `discord.chat_llm_enabled=false` turns this lane off too |
| `agent.model` | `PRINT_HANDY__AGENT__MODEL` | empty | Agent model name; optional, unlike `llm.model` — empty omits `model` from the request, for endpoints that are not model-addressable |
| `agent.api_key_env` | `PRINT_HANDY__AGENT__API_KEY_ENV` | `AGENT_API_KEY` | Name of the environment variable holding the agent endpoint's API key |
| `agent.timeout_s` | `PRINT_HANDY__AGENT__TIMEOUT_S` | `120` | Agent request timeout; one attempt, no retries. Longer than `llm.timeout_s` because an agent turn does real work |
| `ai.allowed_users` | file only | empty | Who may use the AI edit lane (#902): a list of chat user IDs. Empty keeps the lane open to every user the gateway already authorizes. A refused user gets the edit vocabulary; nothing is sent to the AI endpoint |
| `ai.edit_requests_per_hour` | `PRINT_HANDY__AI__EDIT_REQUESTS_PER_HOUR` | 20 | Per-user limit on AI edit-lane requests in any rolling hour (#902). 0 turns the limit off. Over the limit, the request falls back to the edit vocabulary |
| `ai.small` | file only | empty | Optional small AI tier (#818): the camera and image check and spool label reading (roasts are local since #1002). A table with `chat_url`, `model`, `fallback_model`, `fallback_url`, `api_key_env`, `fallback_api_key_env` and `timeout_s`; every key is optional and empty keeps the settings the install already had. See [AI tiers](#ai-tiers) |
| `ai.chat` | file only | empty | Optional chat AI tier (#818): questions the scripted handlers cannot answer. Same keys as `ai.small`; empty keeps `llm.*` |
| `ai.heavy` | file only | empty | Optional heavy AI tier (#818): model-file editing and repair, deep image work. Same keys as `ai.small`; empty uses `design.*`, which inherits `llm.*`. The "Fix this for my printer" repair (#978) calls this tier; with no AI configured anywhere in the chain the fix is not offered and intake behaves as before, with a one-line note |
| `design.enabled` | `PRINT_HANDY__DESIGN__ENABLED` | unset (auto) | `/print-design` / `/design` AI design (#388). Unset = auto: on whenever a design/LLM endpoint is configured; `false` forces off (the command answers "not set up"); `true` forces on (an unconfigured endpoint is then reported, not silent) |
| `design.chat_url` | `PRINT_HANDY__DESIGN__CHAT_URL` | empty (inherit) | Design model endpoint; empty uses `llm.chat_url`. Wins in Hermes mode too — the profile's `auxiliary.print_design` block is the fallback only when no endpoint is configured |
| `design.model` | `PRINT_HANDY__DESIGN__MODEL` | empty (inherit) | Design model name; empty uses `llm.model`. Point this at a stronger model than chat if the endpoint offers one |
| `design.api_key_env` | `PRINT_HANDY__DESIGN__API_KEY_ENV` | empty (inherit) | Environment variable holding the design endpoint's key; empty uses `llm.api_key_env` |
| `design.timeout_s` | `PRINT_HANDY__DESIGN__TIMEOUT_S` | `180` | Design model-call timeout in seconds; also the OpenSCAD render wall-clock limit |
| `design.repair_attempts` | `PRINT_HANDY__DESIGN__REPAIR_ATTEMPTS` | `3` | How many times a failed render is sent back to the model with the error before the turn gives up |
| `listener.tick_s` | `PRINT_HANDY__LISTENER__TICK_S` | `600` | Listener housekeeping interval |
| `listener.ifs_presence_poll_s` | `PRINT_HANDY__LISTENER__IFS_PRESENCE_POLL_S` | `15` | IFS spool presence poll |
| `listener.spool_nag_s` | `PRINT_HANDY__LISTENER__SPOOL_NAG_S` | `600` | Reminder interval for an unanswered spool question |
| `listener.start_claim_ttl_s` | `PRINT_HANDY__LISTENER__START_CLAIM_TTL_S` | `1800` | How long a start button stays claimed |
| `vision.model` | `PRINT_HANDY__VISION__MODEL` | `gemini-latest` | Model for camera checks. The default is a CLIProxyAPI alias: set your own server's vision model name |
| `vision.roast_model` | `PRINT_HANDY__VISION__ROAST_MODEL` | `luna-latest` | Legacy, unused: roasts come from the local roast corpus with no model since #1002. Kept so older configs load unchanged |
| `vision.roast_enabled` | `PRINT_HANDY__VISION__ROAST_ENABLED` | `true` | The one-line roast: exactly one per print on the completion post, one on the failure and cancel posts and on a confirmed-spaghetti camera alert, plus on request in chat ("roast my last print", "roast print 84"), on every platform. Drawn from the local roast corpus (`print_handy/data/roasts/`) with no LLM and no endpoint (#1002). `false` turns roasts off without disabling camera checks |
| `vision.local_detector_enabled` | `PRINT_HANDY__VISION__LOCAL_DETECTOR_ENABLED` | `false` | Built-in local failure detector for the camera watchdog (#455): when `true`, frames are judged by an ONNX detector on this machine whenever no vision endpoint route is configured, so a no-LLM, no-cloud install still gets bad-print detection. Off by default; see [Local failure detector](#local-failure-detector) |
| `vision.local_model_path` | `PRINT_HANDY__VISION__LOCAL_MODEL_PATH` | empty | Path of the local detector's ONNX model file. Empty uses the default location (a `models` folder under the Hermes home) and the pinned model; set it to a file you placed yourself to run fully offline |
| `vision.auto_light_off_minutes` | `PRINT_HANDY__VISION__AUTO_LIGHT_OFF_MINUTES` | `0` | Auto light-off (#516) for Moonraker, Bambu Lab and Elegoo SDCP printers: `0` is off (the default). When set (30 is the suggested value), the light turns on at print start and off again after the plate has been seen empty for that many minutes. Needs a vision endpoint, like the camera watchdog — the local failure detector does not count, because it cannot certify an empty plate; on SDCP the turn-on works but auto-off waits on a verified camera source; see [Auto light-off](#auto-light-off) |
| `vision.light_object` | `PRINT_HANDY__VISION__LIGHT_OBJECT` | empty | The Moonraker light object the auto light-off switches: `led <name>`, `neopixel <name>` or `output_pin <name>` (for example `led chamber_led`). Empty auto-discovers: the printer's light object is used only when it lists exactly one; with several, the listener logs the candidates and the feature stays off until one is named here |
| `visual_watch.sensitivity` | `PRINT_HANDY__VISUAL_WATCH__SENSITIVITY` | `3` | Camera watchdog sensitivity, 1 (quietest) to 5 (most alerts); also sets the periodic check interval and stall threshold, see the [project reference](PROJECT-REFERENCE.md#camera-stall-sensitivity) |
| `visual_watch.enabled` | `PRINT_HANDY__VISUAL_WATCH__ENABLED` | `true` | Camera monitoring switch |
| `visual_watch.early_layers` | `PRINT_HANDY__VISUAL_WATCH__EARLY_LAYERS` | `10` | Early-layer window for risky prints (#913): checks every layer change and at least every `early_interval_s` |
| `visual_watch.early_interval_s` | `PRINT_HANDY__VISUAL_WATCH__EARLY_INTERVAL_S` | `120` | Minimum check interval in seconds during early-layer mode for risky prints (#913) |
| `visual_watch.auto_pause_early` | `PRINT_HANDY__VISUAL_WATCH__AUTO_PAUSE_EARLY` | `false` | Optional printer control: pauses after two consecutive suspicious early-layer checks when enabled. Off by default (#913) |
| `timelapse.mode` | `PRINT_HANDY__TIMELAPSE__MODE` | derived | Timelapse mode (#246/#295): `rendered` (the printer renders it, moonraker-timelapse), `container` (in-container capture) or `off`. Empty in a file derives: `rendered` when a Moonraker URL is configured, else `container` when in-container capture is enabled, else `off`. The mode gates both paths: `off` stops in-container capture and the printer-rendered attach whatever `timelapse.capture_enabled` says; `container` opens capture on its own and the printer-rendered path is not used; `rendered` leaves the capture gate to `timelapse.capture_enabled` exactly as before |
| `timelapse.encoder` | `PRINT_HANDY__TIMELAPSE__ENCODER` | `cpu` | ffmpeg encoder for the timelapse sizing shrink (#314): `cpu` (libx264), `vaapi`, `qsv` or `nvenc`. Hardware choices need the GPU passed through to the container (see [DOCKER.md](DOCKER.md)); a hardware encode that fails falls back to `cpu` automatically, so a completion post is never lost to the encoder |
| `timelapse.archive_host` | `PRINT_HANDY__TIMELAPSE__ARCHIVE_HOST` | empty | SSH host for the printer-rendered timelapse archive; empty disables the printer-rendered path. Printer-rendered path only: when set (with `archive_dir`), the listener waits for the rendered video in that archive and attaches the archived copy to the completion post; with either setting unset, the lookup is skipped outright and no printer-rendered video is attached. In-container capture (`timelapse.capture_enabled`) never uses it |
| `timelapse.archive_dir` | `PRINT_HANDY__TIMELAPSE__ARCHIVE_DIR` | empty | Archive folder on that host (see `timelapse.archive_host`) |
| `timelapse.ssh_key` | `PRINT_HANDY__TIMELAPSE__SSH_KEY` | empty | Path of the SSH key in the container (mount it read-only) |
| `timelapse.render_wait_s` | `PRINT_HANDY__TIMELAPSE__RENDER_WAIT_S` | `900` | Wait for the printer to render the timelapse. Delivery contract (#373): when archiving is on and the rendered video doesn't arrive within the wait (or the marker can't be matched to the job), the completion post itself carries a "Timelapse not attached" line with the reason instead of the video vanishing silently; and if the listener restarts during the wait, the pending completion re-drives the retrieval for the persisted job rather than posting the text without the video |
| `timelapse.render_poll_s` | `PRINT_HANDY__TIMELAPSE__RENDER_POLL_S` | `10` | Poll interval while waiting |
| `timelapse.max_bytes` | `PRINT_HANDY__TIMELAPSE__MAX_BYTES` | `209715200` | Largest timelapse kept |
| `timelapse.local_retention_days` | `PRINT_HANDY__TIMELAPSE__LOCAL_RETENTION_DAYS` | `1` | Local copy retention |
| `timelapse.plan_clip_retention_days` | `PRINT_HANDY__TIMELAPSE__PLAN_CLIP_RETENTION_DAYS` | `14` | Plan clip retention |
| `timelapse.vertical_short` | `PRINT_HANDY__TIMELAPSE__VERTICAL_SHORT` | `false` | Also post a 9:16 vertical version of each finished timelapse (#317), alongside the timelapse itself |
| `timelapse.vertical_short_mode` | `PRINT_HANDY__TIMELAPSE__VERTICAL_SHORT_MODE` | `pad` | `pad` fills the 9:16 frame with a blurred background; `crop` centre-crops |
| `timelapse.vertical_short_overlay` | `PRINT_HANDY__TIMELAPSE__VERTICAL_SHORT_OVERLAY` | `true` | Burn the print title / printer / filament / time overlay into the vertical short |
| `timelapse.wrapped_enabled` | `PRINT_HANDY__TIMELAPSE__WRAPPED_ENABLED` | `false` | Data tab and Print Handy Wrapped (#956): read-only statistics over the print JSON records (hours, filament, failures, breakdowns by printer, material, color, vendor, spool and nozzle) plus the once-a-year Wrapped recap in the Web UI. Off by default; while off, the Data and Wrapped pages are not in the navigation and their routes answer a named disabled refusal. Also accepted as the legacy name `PRINT_TIMELAPSE_WRAPPED_ENABLED` |
| `timelapse.wrapped_ai_video` | `PRINT_HANDY__TIMELAPSE__WRAPPED_AI_VIDEO` | `false` | Wrapped video (#956): the on-demand local video compile on the Wrapped page — the local deterministic commentary (never rewritten by an AI model; the key keeps its original name), the slides, and the year's timelapses where found. Off by default and needs `timelapse.wrapped_enabled` on. The video is generated only when the operator asks, is saved locally, and is never sent anywhere. Spoken narration is governed separately by `timelapse.wrapped_tts_engine`. Also accepted as the legacy name `PRINT_TIMELAPSE_WRAPPED_AI_VIDEO` |
| `timelapse.wrapped_tts_engine` | `PRINT_HANDY__TIMELAPSE__WRAPPED_TTS_ENGINE` | `none` | Wrapped narration (#981): `none` (default; no spoken narration), or `piper` (local Piper, `paths.piper` with `paths.piper_model`). There is no cloud or AI-model narration: nothing is sent anywhere. Also accepted as the legacy name `PRINT_TIMELAPSE_WRAPPED_TTS_ENGINE` |
| `timelapse.wrapped_music_file` | `PRINT_HANDY__TIMELAPSE__WRAPPED_MUSIC_FILE` | empty | An audio file you supply, used as the Wrapped video music bed instead of the synthesized bed (#981). Empty keeps the synthesized bed; a missing file is named in the video result and the synthesized bed is used. Also accepted as the legacy name `PRINT_TIMELAPSE_WRAPPED_MUSIC_FILE` |
| `timelapse.capture_enabled` | `PRINT_HANDY__TIMELAPSE__CAPTURE_ENABLED` | `false` | In-container timelapse capture (#246): starts the bundled go2rtc (full Docker image only) to normalise the camera source on loopback. Off by default and opt-in; off keeps today's printer-rendered behavior. `timelapse.mode` interacts: mode `container` opens capture without this switch, and mode `off` stops capture even with it on. When both the file value and `PRINT_HANDY__TIMELAPSE__CAPTURE_ENABLED` are set, the environment variable wins (the settings load order ends with the environment). On stock Flashforge there is no go2rtc leg: the launched-print monitor captures from the camera source the printer advertises in `/detail` (see [FLASHFORGE-STOCK.md](FLASHFORGE-STOCK.md)) |
| `timelapse.capture_source_url` | `PRINT_HANDY__TIMELAPSE__CAPTURE_SOURCE_URL` | empty | RTSP stream URL (`rtsp://` or `rtsps://`) or a go2rtc `ffmpeg:` source. go2rtc ingests it. Empty derives the camera from `services.camera_snapshot_url` / the Moonraker webcam |
| `timelapse.capture_trigger` | `PRINT_HANDY__TIMELAPSE__CAPTURE_TRIGGER` | `interval` | Stage 2 frame capture trigger (#246): `interval` (a frame every `capture_interval_s`), `layer` (a frame on each Moonraker layer change) or `both` |
| `timelapse.capture_interval_s` | `PRINT_HANDY__TIMELAPSE__CAPTURE_INTERVAL_S` | `5.0` | Seconds between captured frames for the interval trigger (above 0, at most 3600) |
| `timelapse.target_length_s` | `PRINT_HANDY__TIMELAPSE__TARGET_LENGTH_S` | `20` | Length the finished timelapse aims for, in seconds (above 0, at most 3600) (#906) |
| `timelapse.min_fps` | `PRINT_HANDY__TIMELAPSE__MIN_FPS` | `10` | Lowest playback fps; a longer result is decimated to the target length (#906) |
| `timelapse.max_fps` | `PRINT_HANDY__TIMELAPSE__MAX_FPS` | `60` | Highest playback fps (at least `min_fps`); a shorter result is held to `min_length_s` (#906) |
| `timelapse.min_length_s` | `PRINT_HANDY__TIMELAPSE__MIN_LENGTH_S` | `5` | Shortest finished timelapse in seconds; very short captures hold frames to reach it (0 = off) (#906) |
| `timelapse.hold_last_frame_s` | `PRINT_HANDY__TIMELAPSE__HOLD_LAST_FRAME_S` | `1` | Seconds the last frame is held at the end (0 = off) (#906) |
| `timelapse.capture_max_frames` | `PRINT_HANDY__TIMELAPSE__CAPTURE_MAX_FRAMES` | `3600` | Per-job cap on captured frames; capture stops at the cap and keeps the frames already taken. `0` disables the cap |
| `timelapse.capture_max_bytes` | `PRINT_HANDY__TIMELAPSE__CAPTURE_MAX_BYTES` | `536870912` | Per-job cap on captured frame bytes; capture stops at the cap and keeps the frames already taken. `0` disables the cap |
| `timelapse.capture_encoder` | `PRINT_HANDY__TIMELAPSE__CAPTURE_ENCODER` | `auto` | Encoder preference for the Stage 3 assembly encode (#246): `auto` (VAAPI → NVENC → QSV → CPU), `vaapi`, `nvenc`, `qsv` or `cpu`. A failing or unavailable hardware encoder falls back to CPU, and the log names why (#455). A GPU counts only when its device is usable: an accessible `/dev/dri/renderD*` node (Intel/AMD) or a `/dev/nvidia*` device (NVIDIA). Passthrough steps for Unraid and Compose: docs/DOCKER.md, GPU passthrough. GPU paths are not verified on real hardware |
| `webui.enabled` | `PRINT_HANDY__WEBUI__ENABLED` | `false` | Optional Web UI (#286) switch; off by default, and nothing listens while disabled |
| `notify.mqtt_enabled` | `PRINT_HANDY__NOTIFY__MQTT_ENABLED` | `false` | Publish print events to MQTT for Home Assistant (#622). Off by default. A failed publish does not block chat or the print |
| `notify.mqtt_host` | `PRINT_HANDY__NOTIFY__MQTT_HOST` | `` | Broker host. Empty leaves MQTT off even when enabled |
| `notify.mqtt_port` | `PRINT_HANDY__NOTIFY__MQTT_PORT` | `1883` | Broker port. 0 uses 1883 |
| `notify.mqtt_topic` | `PRINT_HANDY__NOTIFY__MQTT_TOPIC` | `print-handy` | Topic root. Events are `<root>/print_started`, `paused`, `runout`, `watchdog_offer`, `complete`, `failed`, `spool_added` |
| `notify.mqtt_username` | `PRINT_HANDY__NOTIFY__MQTT_USERNAME` | `` | Optional username. The password is `MQTT_PASSWORD` in the environment, never in this file |
| `notify.ntfy_enabled` | `PRINT_HANDY__NOTIFY__NTFY_ENABLED` | `false` | Publish the same print events to an ntfy topic (#622). Off by default. A failed publish does not block chat or the print |
| `notify.ntfy_url` | `PRINT_HANDY__NOTIFY__NTFY_URL` | `` | ntfy server URL, for example `https://ntfy.sh` (a self-hosted server works the same). The topic below is appended to it; the URL may instead name the topic itself. Settings validation refuses `ntfy_enabled` with an empty URL, and with no topic in either `ntfy_topic` or the URL path |
| `notify.ntfy_topic` | `PRINT_HANDY__NOTIFY__NTFY_TOPIC` | `` | ntfy topic the events are published to. Required when `ntfy_enabled` is true unless `ntfy_url` already names the topic; validation refuses an enabled config with neither. It is appended to `ntfy_url` unless the URL already ends with it. A protected topic's access token is `NTFY_ACCESS_TOKEN` in the environment, never in this file |
| `webui.host` | `PRINT_HANDY__WEBUI__HOST` | `127.0.0.1` | Host the Web UI binds to when enabled; a plain IP address or hostname |
| `webui.port` | `PRINT_HANDY__WEBUI__PORT` | `0` | Port the Web UI binds to when enabled; `0` means no port has been chosen yet |
| `webui.access_token_env` | `PRINT_HANDY__WEBUI__ACCESS_TOKEN_ENV` | `WEBUI_ACCESS_TOKEN` | Name of the environment variable holding the optional Web UI access token; the token itself is never a setting |
| `webui.require_auth` | `PRINT_HANDY__WEBUI__REQUIRE_AUTH` | `true` | `false` turns the access token off entirely, even on a LAN bind: anyone who can reach the port can use the Web UI, printer controls included. Only for a network you trust; not editable from the Web UI With `true` and no access token, a LAN bind starts in first-run setup (#708): the setup page asks for a password (stored hashed in the state folder) or an explicit choice to leave it open; see [Web UI](WEBUI.md#first-run-setup). |
| `webui.controls_enabled` | `PRINT_HANDY__WEBUI__CONTROLS_ENABLED` | `false` | `true` turns on the Web UI printer control routes (`POST /api/printers/<id>/pause`, `resume`, `cancel`, `temperature`, `target`, #708; plus `emergency_stop`, `firmware_restart`, `server_restart`, `gcode`, `exclude`, ha-print-handy #1). While `false` they answer 403 and send nothing. Not editable from the Web UI: arming controls is a file or environment decision |
| `webui.cookie_secure` | `PRINT_HANDY__WEBUI__COOKIE_SECURE` | `false` | `true` marks the browser session and theme cookies `Secure`, so browsers send them only over HTTPS. Leave `false` for plain HTTP on a trusted LAN or VPN; a browser will not store a `Secure` cookie from an HTTP page. Set `true` only when an HTTPS reverse proxy terminates TLS in front of the Web UI. Not editable from the Web UI. A file key spelled `secure_cookie` is accepted as the same setting |
| `live_stream.enabled` | `PRINT_HANDY__LIVE_STREAM__ENABLED` | `false` | Live camera viewing in Discord (#311) switch; off by default, and `/print-live` only reports that while disabled |
| `live_stream.voice_channel_id` | `PRINT_HANDY__LIVE_STREAM__VOICE_CHANNEL_ID` | empty | The one Discord voice channel the live Activity is launched in; the bot never names another channel. Keep it a private channel (see DISCORD-BOT-SETUP.md) |
| `live_stream.auto_start_on_print` | `PRINT_HANDY__LIVE_STREAM__AUTO_START_ON_PRINT` | `false` | Offer the live view automatically when a print starts; the viewing session itself still starts only from Discord |
| `live_stream.max_height` | `PRINT_HANDY__LIVE_STREAM__MAX_HEIGHT` | `720` | Live feed height cap in pixels (144-2160); width follows the camera's aspect. Applies through the live transcode: while all four feed values (`max_height`, `fps`, `max_bitrate_kbps`, `encoder`) hold their defaults, the live view is the camera stream relayed as it arrives; changing any one makes go2rtc publish a second, transcoded stream for the live view with all four values applied (ffmpeg re-encodes the loopback restream, so the camera still gets one upstream pull). Timelapse capture and watchdog stills keep the camera's own stream either way |
| `live_stream.fps` | `PRINT_HANDY__LIVE_STREAM__FPS` | `15` | Live feed frame-rate cap (1-60); applied by the live transcode described under `live_stream.max_height` |
| `live_stream.max_bitrate_kbps` | `PRINT_HANDY__LIVE_STREAM__MAX_BITRATE_KBPS` | `2500` | Live feed bitrate cap in kbit/s (100-20000); applied by the live transcode described under `live_stream.max_height` |
| `live_stream.encoder` | `PRINT_HANDY__LIVE_STREAM__ENCODER` | `auto` | Encoder preference for the live feed's transcode: `auto`, `vaapi`, `qsv`, `nvenc` or `cpu`. The encoder is picked from a probe of the machine's ffmpeg in that preference order (hardware first, CPU last), the same candidate order as the timelapse assembly encode |
| `live_stream.empty_timeout_s` | `PRINT_HANDY__LIVE_STREAM__EMPTY_TIMEOUT_S` | `300` | Seconds the voice channel may stay empty of viewers before the live session ends itself |
| `live_stream.activity_origin` | `PRINT_HANDY__LIVE_STREAM__ACTIVITY_ORIGIN` | empty | Public https origin the Discord Activity page is served from: the named Cloudflare Tunnel's hostname, mapped in the Discord Developer Portal (see DISCORD-BOT-SETUP.md); empty until deployed |
| `live_stream.port` | `PRINT_HANDY__LIVE_STREAM__PORT` | `0` | Loopback port the supervised Activity server binds inside the deployment; `0` means no port has been chosen yet, so the server does not start |
| `logging.level` | `PRINT_HANDY__LOGGING__LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `logging.format` | `PRINT_HANDY__LOGGING__FORMAT` | `text` | `text` (one readable line per event) or `json` (one JSON object per line) |
| `logging.file` | `PRINT_HANDY__LOGGING__FILE` | empty (Docker image: `/data/state/logs/print-handy.log`) | Also write daily log files beside this path (`print-handy.log` becomes `print-handy-YYYY-MM-DD.log`); empty logs to stderr only. See [Logs and troubleshooting](DOCKER.md#logs-and-troubleshooting) |
| `logging.retention_days` | `PRINT_HANDY__LOGGING__RETENTION_DAYS` | `14` | Days of daily log files kept (today counts as one); older files are deleted. A whole number, 1 or more |
| `logging.levels` | file only | empty | Per-subsystem verbosity (#390): a table of logger name to level, for example `levels = { "print_handy.core.orca_adapter" = "DEBUG" }` in TOML, so one subsystem can be raised without a wall of debug output; applied at process start |
| `maintenance.quick_hours` | `PRINT_HANDY__MAINTENANCE__QUICK_HOURS` | `25` | Quick maintenance due after these print hours |
| `maintenance.full_hours` | `PRINT_HANDY__MAINTENANCE__FULL_HOURS` | `75` | Full maintenance due after these print hours |
| `maintenance.full_days` | `PRINT_HANDY__MAINTENANCE__FULL_DAYS` | `30` | Full maintenance due after these days |
| `maintenance.bed_separate_mm` | `PRINT_HANDY__MAINTENANCE__BED_SEPARATE_MM` | `20` | How far `/print-bed-down` (prepare to clean) moves the bed and nozzle apart, clamped to 1 to 50 mm and never past the Z maximum |
| `nozzle_temp_offsets.<type>` | `PRINT_HANDY__NOZZLE_TEMP_OFFSETS__<TYPE>` | per type: `hardened_steel` and `stainless_steel` `5.0`, others `0.0` | Temperature offset, −10 to +15 °C, for `brass`, `hardened_steel`, `stainless_steel`, `ruby`, `tungsten_carbide`, `diamond`. The steel defaults compensate for lower thermal conductivity and apply on top of every filament's baseline temperature (all materials, not one); set a type here to override its default |
| `material_temps.<MATERIAL>` | file only | see `config/home.yaml` | `{nozzle, bed}` fallback temperatures for PLA, PETG, ABS, ASA, TPU, PA, PC; Spoolman filament values win |
| `material_tuning.<MATERIAL>` | file only | PLA: `{flow_ratio: 0.98, max_volumetric_speed: 12, min_layer_time: 8, outer_wall_speed_max: 60}` | Slicing tuning per material family: the flow ratio Orca receives (replaces the nozzle flow multiplier), volumetric-speed cap in mm³/s, minimum layer time in s, outer-wall speed cap in mm/s. A key you set wins over the shipped value; `null` keeps the printer template value. Vendor filament profiles (Bambu Lab) are not changed |
| `spool.prompt_brand` | `PRINT_HANDY__SPOOL__PROMPT_BRAND` | `false` | Ask for the brand/vendor during the conversational new-spool walkthrough (#512). Off by default: the walkthrough skips brand entirely and the spool is created without one. Set `true` only if you track brands |

**Material variant adjustments (#512).** A named variant within a family — Silk PLA, PLA+, Matte PLA, and the fibre-reinforced *-CF / *-GF variants of any family — adjusts the family fallback above: Silk PLA +10 °C nozzle, 0.7× outer-wall speed capped at 60 mm/s, at least an 8 s minimum layer time, and at most 12 mm³/s max volumetric speed (Flashforge's own Generic PLA-Silk value) when slicing (its gloss needs the slower wall; the cap and layer time are OrcaSlicer's own Silk profile values, and the cap matters because 0.7× a 200 mm/s template is still 140 mm/s), PLA+ / Matte PLA / reinforced variants +5 °C nozzle (PA-CF +10 °C), Matte PLA 0.9× outer-wall speed. The adjustment applies only to the material-default fallback: a temperature recorded on the Spoolman filament itself is explicit data and is never adjusted, and plain family labels (PLA, PETG, ABS, ASA, TPU, PA, PC) are never adjusted.

**Material labels.** A Spoolman material may carry surface-finish words around the polymer (Silk, Matte, Shiny, Glossy, Satin, Basic, Pro, Plus, Tough) in any order or in parentheses: "Shiny Silk PLA" and "PLA (Silk)" resolve to PLA with the Silk PLA adjustment. Speed or flow grades (High Speed, HS, HF), fill materials such as Glow or Wood, blends written with a slash (PLA/PHA), and unknown names are refused rather than guessed: set the Spoolman material to the family it prints as, or record the filament's temperatures in Spoolman. Names with spaces are safe in sliced G-code: OrcaSlicer quotes them (`; filament_type = "Silk PLA"`) and Print Handy reads the quoted form.

**Photo label intake (#514).** When any label-reading route is available, the new-spool walkthrough's first step also offers **Use a photo of the label**. The operator sends one photo of the spool's box or sticker as the next message in the channel, and the routes are tried in a fixed order: (1) the label's own **barcode/QR code**, decoded locally with the zxing-cpp library (a runtime dependency) — deterministic data the manufacturer printed; (2) the **vision endpoint** — the same availability rule the camera watchdog uses: `PRINT_VISION_URL` set, or `services.vision_chat_url` with its key; (3) **local OCR** with the tesseract binary (installed in the shipped container image; elsewhere, install `tesseract-ocr` to enable it), matched against the known filament types and the material/colour/brand vocabulary labels are printed from. The first route that reads a complete spec wins; partial results are never merged across routes. Every route extracts the same fields — brand, material (with sub-type), colour/blend, net weight and, when printed, the recommended nozzle/bed temperatures — and the values pre-fill the walkthrough at its confirmation step, which names the route that read them. Nothing is created from a photo without that confirmation, and Edit details walks the normal steps instead. A photo no route can use refuses with the reason and falls back to the manual steps. Label temperatures are recorded on the Spoolman filament the spool is created with, so they win over material defaults exactly like temperatures entered in Spoolman itself, for every material; a filament type that already exists keeps the temperatures it already has. The photo itself is never stored or recorded: it goes only to the one route that reads it (a local decoder, a local binary, or the configured vision endpoint). With no route available the button is not offered and the walkthrough is unchanged. The guided `/spool` flow does not offer the photo step: its gateway prompts answer with buttons or text and cannot carry an attachment.

**Purchase intake (#801).** When a vision/AI route is configured (`PRINT_VISION_URL`, or `services.vision_chat_url` with its key — the same route the label photo uses), a shop or order link, or a screenshot of a product page, cart, order confirmation or receipt, dropped in the printer channel earns one confirm-first offer: **Add spools from this purchase?** Nothing is read or created before the operator says yes, and model links are never purchase offers. A link is fetched once, server side, with a short timeout and a size cap: JSON-LD `Product` / Open Graph data resolves the purchase with no AI call when it is complete, and only incomplete structured data falls back to sending the trimmed page text to the AI route. A blocked or bot-walled page (common on large shops) refuses plainly and asks for a screenshot instead. A screenshot is read by the vision endpoint with a purchase prompt, returning one line per product: brand, material with sub-type, color name and hex when shown, net weight per roll, quantity, price when shown, and nozzle/bed temperatures when shown. The confirmation lists every roll as color · material · brand · weight × quantity with **Create all**, **Edit** and **Cancel**; Edit creates nothing and names the normal `/spool new` form for each line. Create all reuses the normal spool creation: the filament type is matched by brand + material + color and created only when new, one spool per roll lands on the Shelf, and each spool records its per-roll price and the purchase URL. Multi-color pack variants ("4 × 1 kg, Black/White/Gray/Red") become one line per color. The same material validation as the label intake applies: unknown materials are refused, and PLA+ is PLA with the +5 °C variant adjustment. With no AI route configured, no offer appears and nothing changes.
| `cold_pull.<profile>` | file only | from the vendor profile *model* | Hot and cold cold-pull temperatures; profile names are yours |

## Model endpoints

Every model feature talks to an **OpenAI-compatible chat completions** endpoint
(`POST .../v1/chat/completions`), so any provider or local server that speaks
that API works: OpenAI, OpenRouter, Cloudflare Workers AI, a CLIProxyAPI
instance, or a local server such as Ollama, LM Studio, llama.cpp or vLLM.
Nothing requires Hermes; the standalone bot (Discord, Telegram or Matrix)
reads these settings directly.

| Feature | URL | Model | Key |
| --- | --- | --- | --- |
| Plain-language chat (Discord) | `llm.chat_url` | `llm.model` | the variable named by `llm.api_key_env` (default `LLM_API_KEY`), optional |
| AI design (`/print-design`) | `design.chat_url`, else `llm.chat_url` | `design.model`, else `llm.model` | `design.api_key_env`, else `llm.api_key_env` |
| Camera watchdog, spool label photos | `services.vision_chat_url` | `vision.model` | `CLIPROXY_API_KEY` |

### AI tiers

Three optional tiers let one install use different models for different jobs
(#818). Nothing ships filled in, and an install that configures none of them
behaves exactly as before. Each tier is a table under `ai` in the YAML (or
`[ai.small]` in legacy TOML); the tables are file only.

| Tier | Used for | Default route when the tier is empty |
| --- | --- | --- |
| `small` | camera and image check, spool label reading | `services.vision_chat_url`, `vision.model`, `CLIPROXY_API_KEY` |
| `chat` | questions the scripted handlers cannot answer | `llm.*` |
| `heavy` | model-file editing and repair, deep image work (route only, not used yet) | `design.*`, inheriting `llm.*` |

Keys of a tier table: `chat_url` and `model` (set together), `fallback_model`,
`fallback_url`, `api_key_env`, `fallback_api_key_env` and `timeout_s`. A value
you set replaces the default route one field at a time, so `model` alone
changes only the model.

- **One retry, on the fallback.** When the primary request fails or times out,
  the same request is sent once to `fallback_model` on `fallback_url` (the
  same endpoint when `fallback_url` is empty). If that fails too, the call
  fails exactly as it would without a fallback. A fallback on another host
  never receives the primary's key; give it its own through
  `fallback_api_key_env`.
- **Who answered is recorded.** The log names the model that served each call.
- **Keys.** `api_key_env` names an environment variable (or a profile `.env`
  entry), never the key itself. The `small` tier keeps its rule that a hosted
  endpoint needs a key and a local or LAN one does not; `chat` and `heavy`
  send without a key when none is set.
- **Stray-chat agent lane.** `agent.*` is unchanged and still answers first;
  it makes one attempt on purpose, so it has no fallback.

Things to know:

- **Keys are optional where the server needs none.** Chat and design send a
  key only when one is set. The vision route needs `CLIPROXY_API_KEY` for a
  hosted endpoint, but not for one on this machine or your LAN (loopback or
  private address, `localhost`, a Docker service name like `ollama`, or a
  `.local` / `.lan` / `.home.arpa` name), since a local server usually takes
  no key. A hosted vision URL without its key keeps the camera watch off
  rather than posting a false alert at every check.
- **Set the model names.** `vision.model` and `vision.roast_model` default to
  `gemini-latest` and `luna-latest`, aliases that only a CLIProxyAPI setup
  knows. With any other server, set them to that server's model names, and
  pick a vision-capable model for `vision.model`.
- **The URL is the full chat completions path**, for example
  `http://ollama:11434/v1/chat/completions` for an Ollama container or
  `https://api.openai.com/v1/chat/completions`.
- Running all of this on Cloudflare's free tier is covered in
  [Cloudflare](CLOUDFLARE.md).

## Part spacing presets

`layout.spacing` picks every part gap and margin at once. Every fit check, the automatic one-plate rearrangement, the multicolor merge (with its prime tower) and slicing use the same resolved values, so Print Handy only offers a layout the slicer will produce. A key set explicitly (in the YAML file or as its environment variable) wins over the preset; the others follow the preset.

| Preset | arrange gap / edge | merge spacing / edge margin | tower to part / bed edge | tower slack |
|---|---|---|---|---|
| `minimum` (default) | 2.0 / 1.0 | 2.0 / 1.0 | 3.5 / 4.0 | 1.0 |
| `low` | 3.5 / 3.0 | 6.0 / 4.5 | 6.0 / 7.0 | 3.0 |
| `medium` (Standard) | 5.0 / 5.0 | 10.0 / 8.0 | 8.0 / 10.0 | 5.0 |
| `high` | 7.5 / 7.5 | 15.0 / 12.0 | 12.0 / 15.0 | 7.5 |

Columns, in mm: `layout.arrange_gap_mm` / `layout.arrange_edge_mm`, `layout.merge_spacing_mm` / `layout.merge_edge_margin_mm`, `prime_tower.part_clearance_mm` / `prime_tower.edge_mm`, `prime_tower.merge_slack_mm`.

`medium` is the fixed set Print Handy used before presets existed. `low` is halfway between `minimum` and `medium`, and `high` is 1.5 × `medium`, both rounded to 0.5 mm; use `high` for wide brims or warping materials.

How `minimum` was measured: real Orca slices through Print Handy's own slice path, each checked by the offline G-code guard (`tests/slicer/test_spacing_minimums_real.py` re-checks them on every slicer CI run).

- **Part gaps (2 mm):** Orca slices parts 0.5 mm apart, even with brims, which it merges. 2 mm keeps the first-layer squish from bridging neighboring parts.
- **Bed edge (1 mm):** a part 0.5 mm from the edge slices and stays on the bed. Orca clips a brim at the bed edge instead of refusing the plate. When a job has a brim, its width is added to the margin so the whole brim stays on the bed.
- **Prime tower to part (3.5 mm):** Orca reports a G-code conflict at 2.5 mm and accepts 3 mm.
- **Prime tower to bed edge (4 mm):** the tower's brim and wipe moves leave the bed at 3 mm and fit at 3.5 mm.
- **Tower slack (1 mm):** the reserved strip only speeds up packing; every tower position is still searched and proved.

Skirts are off in the shipped profiles. If you enable one, raise the edge keys by the skirt distance plus its loops.

## Purge and ZMod plugins

On a ZMod printer (the AD5X family) the color-change purge is one ZMod setting, `USE_TRASH_ON_PRINT`, and Print Handy follows it:

| Printer value | `purge.mode` | Who purges | Prime tower |
|---|---|---|---|
| `1` (ZMod default) | `printer_chute` | the printer, into the chute | none: the slice has no tower and the fit check reserves no room for one |
| `0` (Nopoop) | `prime_tower` | the slicer, into the tower | yes, as before |
| `2` (slicer-controlled) | `slicer_chute` | the slicer's filament-change G-code | refused for now: Print Handy has no such profile yet |

- `purge.mode` `auto` reads the value from the printer (Moonraker `save_variables`) at intake and the doctor; `printer_chute`, `prime_tower` or `slicer_chute` fixes it and is honored even when the printer cannot be read.
- `purge.prime_tower` `auto` means off for `printer_chute` and on for everything else, including a printer whose mode could not be read (the long-standing behavior). `on` and `off` force it.
- `purge.flush_multiplier` is optional and applies Orca's flush volume multiplier. QuickSwap recommends about `0.2` with very low flush volumes. Unset leaves the profile as it is.
- `zmod.plugins` `auto` reads the enabled plugins from the printer's `mod_data/plugins.cfg` (falling back to known macro names); a list such as `[quickswap, ifs_jacker]` overrides detection.

Detection is read-only: Print Handy never changes a ZMod setting. If the printer cannot be read, intake and the doctor say so and the old behavior (a prime tower) is kept. Before a multicolor start, a G-code sliced without a prime tower is refused only when the printer positively reports `use_trash_on_print = 0`, where nothing would purge; any other value starts, and an unreadable value adds a note to the start post. QuickSwap with Bambufy or lessWaste is shown as a note in the doctor and the status page, never as a refusal. IFS Jacker is detected; a source with more colors than the 4 slots Print Handy models is refused with a message saying so (full support is a follow-up).

These keys apply to every ZMod printer in the roster (per-printer values are not supported yet), are read only from the settings file or the environment, and are safe to keep in your user settings file (`$PRINT_HANDY_USER_CONFIG`, or `~/.config/print-handy/settings.yaml`), which updates never overwrite:

```yaml
purge:
  mode: printer_chute
  flush_multiplier: 0.2
```

## Custom vision endpoint (PRINT_VISION_URL)

The camera watchdog normally judges frames through the OpenAI-compatible `services.vision_chat_url` endpoint (with `CLIPROXY_API_KEY`). Setting the plain environment variable `PRINT_VISION_URL` — it is not a `PRINT_HANDY__` setting and has no config-file form — points the watchdog at your own detector instead: a Frigate-adjacent sidecar, an Obico-style adapter, a small ONNX service, anything that can answer one HTTP call. When it is set, it is the route used for every camera check: the OpenAI-compatible route is not consulted, no API key is needed, and the camera watch counts as having a vision endpoint. It serves camera checks and spool-label photo reads (#514). Roasts use no endpoint at all: they come from the local roast corpus (#1002).

The contract is deliberately small:

- **Request:** `POST` to the `PRINT_VISION_URL` URL with header `Content-Type: application/json` and no `Authorization` header. The body is a JSON object: `{"image": "<base64>", "prompt": "<text>"}`. `image` is the camera frame's JPEG bytes, standard base64, no `data:` prefix. `prompt` is the watchdog's question text (today: "Is there plastic on the bed under the nozzle, or is there spaghetti/detachment?"). Unlike the OpenAI-compatible route, no format instructions are prepended — the endpoint itself maps its analysis onto the verdict vocabulary below.
- **Response:** HTTP 200 with a JSON object body: `{"verdict": "...", "reason": "..."}`. `verdict` is matched case-insensitively (Print Handy lowercases it). The vocabulary is `ok`, `air`, `spaghetti`, `detached`, `empty_idle`, `uncertain`; the watch also recognises `safe` (healthy, like `ok`) and `no` / `unsafe` (suspicious). A missing `verdict` counts as `uncertain`; any other value is passed through and matches neither the alert nor the healthy set. `reason` is free text shown with the alert; a missing one counts as empty, and it is truncated to 300 characters.
- **How verdicts map:** `ok` / `safe` are healthy (a healthy verdict vetoes a pixel-diff stall alert: at every `visual_watch.sensitivity` level on the Moonraker watchdog, at 1–2 on the Bambu monitor). An `uncertain` verdict whose reason says the camera feed is dark gets the once-per-print dark-camera notice on the Moonraker watchdog instead of an alert. `air`, `spaghetti` and `detached` alert. `uncertain` also alerts — a frame that cannot be judged is treated as needing review, never as a pass. `empty_idle` while printing (#913) is treated as suspicious: an empty bed while the printer claims to be printing schedules a recheck within 60s; if the recheck is still `empty_idle`, it raises an operator alert.
- **Auto-pause early layers (#913):** `visual_watch.auto_pause_early = false` by default. When enabled, two consecutive suspicious verdicts (`spaghetti`, `detached`, `air`) within the early-layer window send a PAUSE command to the printer via the existing alert pause path. It never cancels. The alert retains its resume and stop buttons for the operator.
- **Cadence independent of layer events (#913):** Periodic camera checks are driven by a timer rather than layer status events, ensuring prints with long layers (e.g. 40+ minutes) still receive timely periodic checks.
- **Timeout and failures:** Print Handy waits at most **30 seconds** for the answer. Anything that goes wrong — endpoint unreachable, timeout, an HTTP error status, or a body that is not a JSON object — degrades to verdict `uncertain` with the detail "the vision check could not be completed", so a broken endpoint produces visible alerts instead of silently passing frames. The underlying error is written to the log (`vision check failed via PRINT_VISION_URL: ...`), which is where a mis-configured URL is diagnosed.
- **Label reads (#514):** the same endpoint may also receive the spool-label prompt (the photo intake above): the request shape is identical, the `image` is the operator's label photo, and the response body should be the JSON object the prompt describes (brand / material / color / grams / nozzle_temp / bed_temp) instead of a verdict object. An endpoint that only answers verdicts will simply fail the label read — the walkthrough refuses with the reason and falls back to its manual steps.
- **Reference implementation:** [`examples/cloudflare/vision-verdict`](../examples/cloudflare/vision-verdict/README.md) is a small Cloudflare Worker that answers this contract with a Workers AI vision model (verdicts only). Setup, free-tier usage and privacy are in [Running the AI features on Cloudflare's free tier](CLOUDFLARE.md).

## Semantic lookup endpoint (PRINT_FUZZY_URL)

`/print-lookup` matches locally first — exact, substring, then the deterministic typo tolerance (#468 stages 1–3) — and those results never change. The local matcher folds common variants before scoring, all offline (with a built-in 1,000+ entry domain vocabulary covering printer brands, filament materials, slicing parameters, and common mobile typos): number words and digits are the same number (`two` matches `2`, `second` matches `2nd`, `twenty one` matches `21`), plurals and word forms fold together, technical variables and abbreviations fold together (`var`/`variable`, `param`/`parameter`, `accel`/`acceleration`, `cfg`/`config`, `temp`/`temperature`, `fw`/`firmware`), units fold together (`mm`/`millimeter`, `cm`, `m`, `um`/`micron`, `kg`, `g`, `s`, `min`, `hr`, `c`, `deg`, `pct`), compound terms match their spaced forms (`hot end`/`hotend`, `heat bed`/`heatbed`, `lead screw`/`leadscrew`, `part cooling`/`partcooling`, `input shaper`/`inputshaper`), Commonwealth and British spelling variants equate (`color`/`colour`, `gray`/`grey`, `aluminum`/`aluminium`, `fiber`/`fibre`, `matte`/`matt`), and accents fold away (`café` reads as `cafe`). <!-- us-spelling: allow colour grey aluminium fibre --> That only widens what a lookup can find — a fuzzy hit still only ever offers choices; it never selects or runs anything by itself. Where several candidates score close together, the surfaces ask with a "Did you mean…?" list instead of silently taking the best guess. What local matching cannot do is meaning: "bed won't stick" shares no characters worth counting with the "bed adhesion" entries. Setting the plain environment variable `PRINT_FUZZY_URL` — not a `PRINT_HANDY__` setting, no config-file form — adds an **opt-in, stateless** semantic layer *behind* the local matcher: only when a lookup finds nothing locally is the query offered to your endpoint, and its answers come back labelled as endpoint suggestions in the same did-you-mean spirit. This is an operator option for installs that want it (the reference implementation is a small Cloudflare Worker over an embeddings model); nothing about a default install changes, and no lookup data is stored by Print Handy or, under this contract, by the endpoint.

The contract is deliberately small:

- **Request:** `POST` to the `PRINT_FUZZY_URL` URL with header `Content-Type: application/json` and no `Authorization` header (the URL carries its secret in the path, like `PRINT_VISION_URL`). The body is `{"query": "...", "candidates": [{"id": "...", "text": "..."}], "threshold": 0.55, "limit": 5}`. Candidates are Print Handy's own public registry entries — firmware topics, symptoms, guided flows, printer models (ids like `symptom:first-layer-not-sticking`, text the display name). Print numbers, spool IDs and per-install data (print-history titles) are never sent as candidates; only the query text you typed and those public entries leave the machine.
- **Response:** HTTP 200 with `{"matches": [{"id": "...", "text": "...", "score": 0.0–1.0}], "degraded": false}` — cosine similarities, best first. An endpoint that cannot answer says `{"matches": [], "degraded": true, "reason": "..."}`. Matches whose id is not one of the candidates sent are ignored.
- **Threshold:** the `0.55` sent today is the reference spike's placeholder, chosen before a live corpus existed; expect to tune it against your own lookups. Scores below it are the endpoint's to drop — and the client enforces the same threshold itself rather than trusting the endpoint: scores that are not finite numbers are rejected, out-of-range scores are clamped to 0–1, duplicate ids keep only their best score, and the results are sorted by score before anything reads them.
- **Timeout and failures:** the whole exchange — connect, send and read together — has one **10-second deadline**, and the response body is capped at 256 KiB; an answer that overruns either counts as a failed lookup. Anything that goes wrong — unreachable, timeout, an HTTP error status, a malformed body, `degraded: true` — silently keeps the local behavior (the usual "Nothing in the registries matches" reply), and the failure is written to the log (`semantic lookup via PRINT_FUZZY_URL failed, ...`). The URL must be `http` or `https`; any other scheme is a configuration error (logged once, and the layer behaves as unset). Plain `http` to a non-local address works but logs a cleartext warning, because the query text travels unencrypted to such an endpoint; local and LAN endpoints over `http` are a supported pattern and are not warned about.
- **Reference implementation:** [`examples/cloudflare/fuzzy-lookup`](../examples/cloudflare/fuzzy-lookup/README.md) is the Cloudflare Worker this contract was designed around (Workers AI embeddings, stateless, secret in the path). Setup, free-tier usage and privacy are in [Running the AI features on Cloudflare's free tier](CLOUDFLARE.md).

## Local failure detector

Setting `vision.local_detector_enabled` to `true` gives the camera watchdog a third verdict route (#455): a built-in ONNX failure detector that runs **on this machine**. It exists for installs with no LLM endpoint and no cloud service — before it, those installs got no bad-print detection at all, because every verdict route needed an endpoint. Nothing leaves the house and no API key is involved.

- **Route order.** The endpoint routes keep precedence, exactly as before: `PRINT_VISION_URL` first, then `services.vision_chat_url` with its key. The local detector is consulted only when neither endpoint route is configured, so an install that already has an endpoint sees no change. A configured endpoint that *fails* still degrades to `uncertain` with the error logged — it does not silently fall through to the local detector, except in early-layer mode (#913): when early-layer monitoring is active and the local detector model weights are already present on disk, a failed or timed-out cloud vision call falls back to the local detector. It never downloads the weights dynamically during early-layer fallback.
- **What it can answer.** The detector answers one question — does this frame show a print failure? A strong detection (confidence at or above 0.5 on a single frame) returns verdict `spaghetti`; a weaker positive (0.08–0.5, the range Obico's own serving code treats as a detection) returns `uncertain`, which alerts with the photo attached; nothing detected returns `ok`. It can never return `air`, `detached` or `empty_idle`, and it is never used for spool-label photo reads (#514) — those still need a vision endpoint. Roasts never use it either, or any endpoint: they are local (#1002).
- **Temporal escalation.** The print watch also remembers the detector's recent checks for the current print, because a single frame is weak evidence: a weak positive is `uncertain` on its first check, but if the next check sees a positive too, the verdict escalates to `spaghetti` — watchdog checks are minutes apart, so two positive checks in a row are a sustained failure, not one odd frame (this is the multi-frame escalation Obico's own serving applies). A strong detection is `spaghetti` immediately, as before; when it follows earlier positive checks the alert text says it was *confirmed across N consecutive checks* rather than a single frame. A clean frame clears the run, and the run resets when the print ends. A failed check (detector error) neither builds nor clears the run — it degrades to `uncertain` as always. Idle plate checks (auto light-off) judge single frames and never touch the run.
- **What it cannot do.** Because it cannot certify an empty plate, [auto light-off](#auto-light-off) still requires a vision endpoint; with only the local detector configured the light stays on and `bot check` says so. Inference takes about a quarter of a second per frame on CPU on a typical host.
- **GPU acceleration.** When the model loads, the detector asks ONNX Runtime which execution providers this host offers and uses the most preferred accelerated one — preference order: CUDA, TensorRT, OpenVINO (Intel/AMD integrated GPUs), CoreML, DirectML, ROCm — with CPU always present as the fallback; if the accelerated provider cannot initialise, it retries on CPU and logs the fallback. Selection is automatic: there is no provider setting to configure. The active provider is logged once when the session is created and shown on `bot check`'s **local detector** row. The selection logic is covered by offline tests against simulated provider lists; **live GPU proof is unverified** — no project host with a GPU has run it yet, so treat the GPU path as wired and selection-tested, with the first accelerated run still owed. The published container image installs the CPU build of ONNX Runtime, so inside it the detector always runs on `CPUExecutionProvider`, even with a GPU passed through for timelapse encoding. The `bot check` row names that case (#455), and docs/DOCKER.md covers it.
- **The model.** The Obico / The Spaghetti Detective failure-detection model (YOLOv2-style region detector, single `failure` class), pinned: source `https://www.obico.io/static_pub/ml-models/model-weights-5a6b1be1fa.onnx`, SHA-256 `0a6ebd8e30dbf6a450c50f9c0a5406f04ba7eb1c99fd5996e888c78bb383b9aa`, licence AGPL-3.0 (the obico-server project's licence). The weights are **202 MB** — much larger than the 15–30 MB the #455 research estimated — so they are not baked into the container image: on first use the detector downloads them once from the pinned URL into a `models` folder under the Hermes home (`/data/state/models` in the container, a persistent volume), verifies the SHA-256, and refuses the file if it does not match. To run fully offline, place a model file yourself and point `vision.local_model_path` at it; a file you name there is used as-is (its output contract must match: boxes + confidences, as documented in `print_handy/hermes/local_vision.py`).
- **Failure behaviour.** A missing `onnxruntime`, a failed download, a corrupt model or an inference error degrades the check to `uncertain` (which alerts) with the real error in the log — never a crash, never a silent pass, and never a blocked print. A failure is remembered for the rest of the process run, so a broken setup logs once instead of retrying a 202 MB download at every camera check; restart the listener/bot to retry. `bot check` reports the detector's state on its **local detector** row.
- **Honest limits.** The detector is covered by offline tests against fixture frames (a synthetic stand-in model with the same I/O contract, plus blank-frame checks against the real weights). Its accuracy on real printer cameras is **unverified** — no live camera proof exists yet. Treat its `ok` as "no failure detected in this frame", not as a guarantee, exactly as with the endpoint routes.
- **The hosted alternative.** A hosted endpoint remains available as a documented operator option: the [custom vision endpoint](#custom-vision-endpoint-print_vision_url) contract above is how the #455 research's Cloudflare Workers AI route is used (a Worker fronting Workers AI, its secret carried in the URL path, since the contract sends no `Authorization` header). That route is hosted — frames leave the house — which is why it is an operator option and not the built-in default; the local detector is the strictly-local track.

## Auto light-off

`vision.auto_light_off_minutes` (#516) is an opt-in for Moonraker, Bambu Lab and Elegoo SDCP printers: **the printer's light turns on when a print starts, and off again once the plate has been seen empty for that many minutes after a print ends.** It is off by default (`0`); 30 is the suggested value. It is the one deliberate exception to the camera watchdog's alert-only posture (#455), and it extends to exactly one accessory — the light. It never touches motion, heaters, pause/stop or a print in progress. The other families (PrusaLink, OctoPrint, stock FlashForge, offline) have no light to switch: for those printers the setting has no effect and `bot check` says so.

How it works:

- **On is a state event, never a vision call.** The listener's print-start transition sends the on command. That has to be so: once the light is off the camera frame is dark, so vision could never be the on-trigger.
- **Off needs both a state gate and sustained vision.** After a print reaches a terminal state, an idle window opens. While the printer stays idle, the plate is checked about every 5 minutes through the same camera snapshot and verdict path as the watchdog. Only verdicts of `empty_idle`, sustained across the full configured minutes, turn the light off. Any other verdict resets the wait — `uncertain` included, so darkness or ambiguity never turns a light off, and a part left on the plate keeps the light on indefinitely (there is no long-idle override). A new print cancels the window. After lights-out, checking stops until the next print.
- **A verdict endpoint is required** — the same gate as the camera watchdog (`services.vision_chat_url` plus its key, or `PRINT_VISION_URL`). The local failure detector (#455) does not satisfy it: it detects failures and never answers `empty_idle`, so with only the local detector configured the light stays on and `bot check` says so on its auto light row. With no endpoint configured at all the feature stays off, and `bot check` says so on its auto light row.
- **The light target is discovered, never hardcoded.** On Moonraker, the listener reads the printer's object list and considers `led`, `neopixel` and `output_pin` objects. Name one explicitly with `vision.light_object` (for example `led chamber_led`); with the setting empty, auto-discovery acts only when the printer lists exactly one candidate — with several, the listener logs the candidate names once and the feature stays off rather than guessing which light is "the" light.
- **On Bambu Lab the target is the chamber light** — a fixed protocol target (MQTT `system/ledctrl`, `led_node: chamber_light`), so `vision.light_object` does not apply. Whether a model has one is read from the printer's own telemetry (`lights_report`): a model that reports no chamber light is never switched, and the listener logs the feature as unavailable for it once.
- **On Elegoo SDCP the target is the chamber light too** (Cmd 403, `LightStatus.SecondLight`), and `vision.light_object` does not apply. Print-start turn-on works, but **auto-off cannot run on SDCP yet**: no SDCP camera source is verified (see [ELEGOO.md](ELEGOO.md)), so the idle empty-plate check has no frame to judge and the light stays on after a print until it is switched some other way. `bot check` names this on its auto light row rather than letting the setting look fully on.
- **Switching on Moonraker happens in Moonraker** (`SET_LED` / `SET_PIN` through Moonraker's G-code endpoint). Home Assistant's Moonraker integration exposes the same light, so its state in Home Assistant follows automatically — Print Handy does not talk to Home Assistant for this. Flipping the light in Home Assistant is just a Moonraker state change like any other; the automation never polls or fights it — it acts only at print start and at the end of an empty window.
- **Failures are quiet and final for that transition.** A failed light command is logged once and never blocks, delays or fails a print.

## Printers list

By default Print Handy runs one printer, described by the `[printer]`, `[services]`, `[bambu]`, `[prusa]`, `[elegoo_sdcp]` and `[flashforge_stock]` settings above. A config file may instead declare a **list of printers** — `printers:` in YAML, `[[printers]]` in TOML — one entry per printer, each carrying its own connection keys (#333; design: [MULTI-PRINTER-DESIGN.md](MULTI-PRINTER-DESIGN.md)). The list itself is file only: it cannot come from the environment.

The **first entry is the default printer**: the legacy single-printer settings are populated from it, so everything that reads `[printer]`, the `[services]` URLs or a vendor section sees the first entry. When a list is declared, the legacy per-printer locations must be absent or empty in the same file and in the environment — setting both is refused at startup, with the offending locations named. A config with no list behaves exactly as before: loading synthesises a one-entry roster from the legacy settings, its `id` the slug of `printer.name` (or `printer` when there is no name).

Each entry is validated against its own model with the same rules as the legacy sections: a Bambu entry may not carry a Moonraker URL, `lan_host` and `lan_serial` come as a pair, a `host` belongs only to Elegoo SDCP and stock Flashforge entries, and so on. Two entries may not share an `id`, and two entries may not claim the same `chat_id` (an entry without one falls back to `discord.chat_id`).

Scalar keys of a *declared* entry can be overridden from the environment with the indexed spelling `PRINT_HANDY__PRINTERS__<ID>__<KEY>` — the entry's `id` uppercased in the `<ID>` position, the key in the `<KEY>` position. The environment can never create an entry, and `id` and `model` cannot be overridden: they are the entry's identity in the file.

Each printer's runtime state lives in its own folder, `state/printers/<id>/` under the profile's state directory (#334): its hardware record (`printer_hardware.json`), its launch database (`jobs.db` — the print lease and consumed bed-clear approvals), and its maintenance record (`maintenance.json`). Job folders stay in the shared archive; a job's `JOB.json` records the `printer_id` it belongs to. On the first start after upgrading, the default printer adopts the previous root-level state files — a held lease and the hardware record carry over intact, new writes go to the per-printer folder, and the old files are left in place for one release so a rollback still finds them.

**Renaming a printer.** The display `name` is free to change; the `id` is the identity that names the state folder, so it must not change by accident.

- **With a `printers:` list**, the `id` is written in your file and never changes unless you edit it. Edit `name` whenever you like.
- **Without a list** (the single-printer default), the `id` is derived from `printer.name` on every start, so renaming `printer.name` silently moves the printer to a new, empty state folder: the held lease, the hardware record (loaded spool, nozzle) and maintenance record stay behind in the old folder. To rename safely, first pin the current id: add a one-entry list whose `id` is the slug of the old name (the folder name under `state/printers/`), move the printer's connection keys into it, then change `name`. A config with a list must not also set the legacy `[printer]` connection keys.
- **To change an `id` on purpose**, stop the bot first, and only while no print is running. Stopping is not enough on its own: a launch that was sent but never confirmed leaves a lease row in `jobs.db`, and that row (and its history baseline) is keyed by the old id. Merely renaming the folder keeps the row, and the new id never sees it — a later start is not refused, and `reconcile-launch` reports nothing pending, because both look the lease up by the current id. Before you start the bot again, rekey those rows to the new id:

  ```
  sqlite3 state/printers/<old-id>/jobs.db "
    UPDATE leases SET printer='<new-id>' WHERE printer!='<new-id>';
    UPDATE lease_baselines SET printer='<new-id>' WHERE printer!='<new-id>';"
  ```

  Then rename the folder `state/printers/<old-id>/` to `state/printers/<new-id>/`, update the `id` in the config (and any `PRINT_HANDY__PRINTERS__<ID>__` variables, which use the id uppercased), and start. If `sqlite3` says there is no such table, that printer has never held a lease and there is nothing to rekey. Keep a copy of the old folder until the printer has been seen working. Job folders in the archive keep the `printer_id` they were created with; finished jobs are not rewritten.
- An `id` must be a slug (`a-z`, `0-9`, `-`, `_`, starting with a letter or digit) and unique; anything else, including path characters, is refused at startup.

**One printer per process.** Each printer's listener runs in its own process on that printer's settings view (`settings.view_for`), installed process-wide through `settings.set_override`. A process therefore serves exactly one printer; do not run two printers in one process. The view carries only that printer's model, geometry and connection keys; `[paths]`, the chat platforms, Spoolman and `[delivery]` stay global and shared.

**Channel routing (#335).** Each printer is driven from its own channel. An entry's `chat_id` is that printer's channel on whichever platform the bot runs — a Discord channel ID, a Telegram chat ID, or a Matrix room ID — and an entry without one uses the platform's global chat (`discord.chat_id`, `telegram.chat_id` or `matrix.room_id`), which is therefore the default printer's channel unless the default entry sets its own. A message, command or button press in a printer's channel is handled for that printer end to end: intake stamps the job with that printer, status, stop, resume, spool, macro and maintenance commands read and act on that printer, and its prompts are answered only in its own channel. A channel no printer claims gets silence — there is no fallback to the default printer for unmapped channels. Delivery methods stay global in v1; they are not per-printer.

**Shared channels (#536).** Two or more entries may claim the *same* channel on purpose: give them the same `chat_id` (or let several fall back to the global chat) and that channel becomes shared. A shared channel is served, but it decides no printer. Dropping a model link asks **which printer** first — busy printers are labelled with their state — and nothing is sliced or sent until you answer. Commands about a specific job (print next plate, print again) use the printer the job belongs to; "current print" commands (stop, resume) act on the printer that is actually printing or paused, ask when more than one qualifies, and refuse — naming the printers — when none does. Spool slot binds and screen syncs ask; the spool list shows each printer's loaded slots under its name; announcements name the printer they are about. A shared channel never silently uses the default printer. Printers that keep their own channel are unaffected, and the two shapes can mix: one shared channel for two printers, a private channel for a third.

**Listeners (#336).** Every printer whose connection needs the Moonraker listener gets its own listener instance — one child process per printer under the Docker supervisor, one runner per printer in the native service — started with that printer's per-printer view: its Moonraker URL, its state folder, its spool and its camera. A listener that dies is restarted on its own backoff and never takes the bot or a sibling listener down; the aggregated health covers every listener, and both the bot status reply and the Web UI status page report each one.

**Setting a roster up (#337).** The CLI setup wizard asks "Add another printer?" after the first printer and writes the roster for you, and the in-chat guided setup offers the same at its review step (reply `add`). Both refuse to guess at a config they cannot rewrite faithfully: the wizard only rewrites an existing `config/local.yaml` it can parse and re-emit unchanged-in-meaning, and leaves an un-round-trippable file byte-for-byte untouched with instructions instead. The Web UI does not edit the roster in this version — its field catalog addresses global fields only — but the status page lists the roster read-only (id, model, connection, channel), and `/api/status` carries the same rows under `printers`.

| YAML key | Environment variable | Docker value | What it does |
|---|---|---|---|
| `printers.id` | file only | — | Required. The printer's stable identity: a slug of `a-z`, `0-9`, `-` and `_` starting with a letter or digit, unique in the list. Renaming an `id` makes it a different printer as far as its state is concerned |
| `printers.model` | file only | — | Required. A registry model key, spelled exactly as `printer.model` |
| `printers.name` | `PRINT_HANDY__PRINTERS__<ID>__NAME` | — | Display name for this printer; defaults to the name the model's own defaults give `printer.name` |
| `printers.chat_id` | `PRINT_HANDY__PRINTERS__<ID>__CHAT_ID` | — | This printer's channel on the platform the bot runs (Discord channel ID, Telegram chat ID, or Matrix room ID); empty falls back to that platform's global chat. Two or more entries claiming the same channel make it a shared channel that asks which printer (see **Shared channels (#536)** above) |
| `printers.moonraker_url` | `PRINT_HANDY__PRINTERS__<ID>__MOONRAKER_URL` | — | This entry's `services.moonraker_url`; must be empty for offline, Bambu LAN, stock Flashforge, PrusaLink and SDCP models |
| `printers.moonraker_ws_url` | `PRINT_HANDY__PRINTERS__<ID>__MOONRAKER_WS_URL` | — | This entry's `services.moonraker_ws_url`; derived from `moonraker_url` when empty |
| `printers.octoprint_url` | `PRINT_HANDY__PRINTERS__<ID>__OCTOPRINT_URL` | — | This entry's `services.octoprint_url`, for OctoPrint models |
| `printers.camera_snapshot_url` | `PRINT_HANDY__PRINTERS__<ID>__CAMERA_SNAPSHOT_URL` | — | This entry's `services.camera_snapshot_url`; derived from `moonraker_url` when empty |
| `printers.lan_host` | `PRINT_HANDY__PRINTERS__<ID>__LAN_HOST` | — | This entry's `bambu.lan_host` (Bambu Lab models only); set together with `lan_serial` |
| `printers.lan_serial` | `PRINT_HANDY__PRINTERS__<ID>__LAN_SERIAL` | — | This entry's `bambu.lan_serial` (Bambu Lab models only); set together with `lan_host` |
| `printers.lan_access_code_env` | `PRINT_HANDY__PRINTERS__<ID>__LAN_ACCESS_CODE_ENV` | — | This entry's `bambu.lan_access_code_env`: the environment variable holding the printer's LAN access code |
| `printers.start_confirm_s` | `PRINT_HANDY__PRINTERS__<ID>__START_CONFIRM_S` | — | This entry's `bambu.start_confirm_s` |
| `printers.link_host` | `PRINT_HANDY__PRINTERS__<ID>__LINK_HOST` | — | This entry's `prusa.link_host` (Prusa models only) |
| `printers.api_key_env` | `PRINT_HANDY__PRINTERS__<ID>__API_KEY_ENV` | — | This entry's `prusa.api_key_env`: the environment variable holding the PrusaLink API key |
| `printers.host` | `PRINT_HANDY__PRINTERS__<ID>__HOST` | — | This entry's `elegoo_sdcp.host` or `flashforge_stock.host`, chosen by the entry's model; no other model takes a `host` |
| `printers.nozzle_type` | `PRINT_HANDY__PRINTERS__<ID>__NOZZLE_TYPE` | — | This entry's `printer.nozzle_type` override, limited to the model's supported types |
| `printers.tool_count` | `PRINT_HANDY__PRINTERS__<ID>__TOOL_COUNT` | — | This entry's `printer.tool_count` override, for multi-tool models |
| `printers.bed_mm` | file only | — | This entry's `printer.bed_mm` override (two positive numbers): a custom build volume, e.g. for an Other/generic printer whose measured bed differs from the model default |
| `printers.max_print_height_mm` | file only | — | This entry's `printer.max_print_height_mm` override (a positive number), the companion to `printers.bed_mm` |

## Compose variables

These configure the Compose deployment, not Print Handy itself. Set them in `.env`.

| Variable | Default | What it does |
|---|---|---|
| `PRINT_HANDY_IMAGE` | none, required | Image the combined container runs. Set a tested immutable digest (`ghcr.io/catduckgnaf/print-handy@sha256:...`), not `:latest`, `:develop` or `:nightly`. The current release digest is in [Pinning the image](DOCKER.md#pinning-the-image). |
| `PRINT_HANDY_DATA` | `./data` | Host folder holding the `state`, `archive`, `records` and `cache` volumes (plus the legacy `hermes` folder while an install still uses it, #914) |
| `PRINT_HANDY_SPOOLMAN_DATA` | `./data/spoolman` | Host folder for the bundled Spoolman database, config, logs and backups |
| `PRINT_HANDY_CONFIG` | `/app/config/docker.yaml` | Settings file Print Handy reads (YAML, or legacy `.toml`) |
| `DISCORD_CLIENT_ID` | empty | The Discord application's ID for the live view (#311): a plain identifier (it appears in invite URLs), not a secret. The Activity server refuses to start without it when live viewing is enabled |
| `CLOUDFLARED_IMAGE` | none, required for the sidecar | Image the optional `cloudflared` sidecar runs (Compose profile `live-stream`): a locally tested tag or immutable digest of the Cloudflare Tunnel connector |
| `TZ` | `America/New_York` | Container timezone (IANA name): the image's local time for log timestamps, scheduled and timed features and the bundled services' timestamps. Set any IANA name, for example `Europe/London`; an unknown name is logged at startup and the default is used. Chat display times follow `discord.timezone` instead |
| `SPOOLMAN_CORS_ORIGIN` | empty | Exact browser origin(s) allowed to call the bundled Spoolman, comma-separated, with scheme and port (#798); see [Fluidd says Spoolman is blocking its origin](DOCKER.md#fluidd-says-spoolman-is-blocking-its-origin) |

## Runtime environment

| Variable | Default | What it does |
|---|---|---|
| `PRINT_HANDY_ORCA_CHANNEL` | `stable` | Selects the one bundled OrcaSlicer at startup; Print Handy ships a single build (v2.5.0-rc), `preview` and `alpha` start on it with a startup notice (the preview channel was removed, so the setting can be dropped), and any other value stops the container |
| `PRINT_HANDY_USER_CONFIG` | `$HOME/.config/print-handy/settings.yaml` | Your own settings file, layered over `PRINT_HANDY_CONFIG` and under environment variables. Optional (the default location may be absent), but a path set here must exist or startup stops; image updates never replace it. In Docker `HOME=/data`, so it is `/data/.config/print-handy/settings.yaml`, which the Unraid template maps to appdata `dot-config` |
| `PRINT_HANDY_BUNDLED_SPOOLMAN` | `true` | `true` runs the bundled Spoolman API; `false` uses `services.spoolman_url` only |
| `SPOOLMAN_HOST` | `127.0.0.1` | Bundled Spoolman bind address; widen it only together with an approved port mapping, see docs/DOCKER.md |
| `SPOOLMAN_UPDATE_CHECK` | `FALSE` | Bundled Spoolman's outbound update check (upstream's own default is true since Spoolman 0.27.1). The supervisor exports `FALSE` for the bundled instance: the bundled Spoolman is updated by Print Handy's pins, and Print Handy is local-first, so it never polls api.github.com. An external Spoolman you run yourself keeps its own setting |
| `PRINT_VISION_URL` | empty | Custom vision endpoint for camera checks; takes precedence over `services.vision_chat_url` and needs no key. See [Custom vision endpoint](#custom-vision-endpoint-print_vision_url) |
| `PRINT_FUZZY_URL` | empty | Opt-in semantic endpoint consulted only when a `/print-lookup` query matches nothing locally; stateless, no key sent. See [Semantic lookup endpoint](#semantic-lookup-endpoint-print_fuzzy_url) |

`WITH_ORCA=false` (default) omits both Orca channels; slicing fails closed without either binary.

## Build arguments

Image build inputs (`docker build --build-arg NAME=value`), not runtime settings. `PRINT_HANDY_ORCA_CHANNEL` is a runtime environment variable (default `stable`; `preview` and `alpha` start on the same single build with a startup notice, because the preview channel was removed) and requires an image built with `WITH_ORCA=true`.

| Argument | Default | What it does |
|---|---|---|
| `APP_CHANNEL` | `development` | Application provenance label, `stable` or `development`; it does not change the source checkout |
| `WITH_ORCA` | `false` | `true` installs the one pinned official prebuilt OrcaSlicer release (v2.5.0-rc, SHA256 verified in `docker/orca-prebuilt.sh`; the single bundled build - earlier two-channel builds pinned v2.5.0-alpha in #356 and v2.5.0-beta after it); OrcaSlicer is never built from source; `false` omits it and slicing fails closed |
| `WITH_GO2RTC` | `true` | `true` installs the pinned official go2rtc prebuilt (v1.9.15, SHA256 verified in `docker/go2rtc-prebuilt.sh`) and the VAAPI driver stack for in-container timelapse capture (#246); the published images set it; `false` omits both |
| `SPOOLMAN_REF` | `ff8efd517b76a4b2783f29c89e9b9fbcd8cd167b` | Pinned Spoolman v0.27.1 source commit; only its Python API backend is installed, never the web UI |
| `ORCA_BUILD_JOBS` | `auto` | Bambu Studio source build only: parallel compile jobs, `auto` (memory-aware, below the CPU count) or a whole number from 1 to 64 |
| `ORCA_BUILD_MEM_PER_JOB_MIB` | `2560` | Bambu Studio source build only: memory budgeted per job by `auto`, at least 256 |
| `ORCA_CCACHE_MAXSIZE` | `5G` | Bambu Studio source build only: size cap of the compiler cache inside the build cache |
| `WITH_BAMBU` | `false` | `true` builds Bambu Studio from pinned source into `/usr/bin/bambu-studio`; `false` omits it and Bambu Lab slicing fails closed |
| `BAMBU_REF` | `56a57d00a591360cf41bf21383915ee5bf357be5` | Pinned Bambu Studio 02.09.01.70 source commit |
| `BAMBU_BUILD_PROFILE` | `release-static-fhs` | Names the Bambu Studio build options; rename it to reset the build trees |

## Secrets

Secrets come from the environment only (Docker: `.env`, never committed or shared), never from YAML or the image.

| Variable | Used for |
|---|---|
| `DISCORD_BOT_TOKEN` | Standalone bot token. The variable name is `discord.token_env` |
| `TELEGRAM_BOT_TOKEN` | Telegram standalone bot token. The variable name is `telegram.token_env` |
| `MATRIX_ACCESS_TOKEN` | Matrix standalone bot access token. The variable name is `matrix.access_token_env` |
| `MQTT_PASSWORD` | MQTT notify (#622). Never a setting. Used when `notify.mqtt_username` is set. A diagnostics bundle does not include it |
| `NTFY_ACCESS_TOKEN` | ntfy notify (#622). Never a setting. Sent as a Bearer token when publishing to a protected ntfy topic; public topics need none. A diagnostics bundle does not include it |
| `WEBUI_ACCESS_TOKEN` | Optional Web UI (#286) access token, required when it binds beyond loopback. The variable name is `webui.access_token_env` |
| `DISCORD_CLIENT_SECRET` | Live camera in Discord (#311): the Discord application's client secret, used server-side for the Activity OAuth exchange. Never a setting |
| `LIVE_STREAM_SESSION_SECRET` | Live camera in Discord (#311): signs the Activity session markers. Never a setting |
| `TUNNEL_TOKEN` | Live camera in Discord (#311): the named Cloudflare Tunnel's token. Read only by the optional `cloudflared` sidecar; Print Handy itself never uses it |
| `LLM_API_KEY` | Optional key for `llm.chat_url`. The variable name is `llm.api_key_env` |
| `AGENT_API_KEY` | Optional key for `agent.chat_url`. The variable name is `agent.api_key_env` |
| `OCTOPRINT_API_KEY` | `octoprint.marlin` printers: OctoPrint application key |
| `BAMBU_LAN_PASSWORD` | Bambu Lab printers: the LAN access code for read-only status. The variable name is `bambu.lan_access_code_env` |
| `MOONRAKER_API_KEY` | Moonraker API key, only when Moonraker requires one |
| `CLIPROXY_API_KEY` | Key for `services.vision_chat_url` (the name is historical; any provider's key goes here). Not needed when that endpoint is on this machine or your LAN |
| `MAKERWORLD_TOKEN` | MakerWorld links: the account token, used when `~/.config/bambu/token` does not exist (a saved file wins) |
| `THINGIVERSE_TOKEN` | Thingiverse links: the App Token, used when `~/.config/thingiverse/token` does not exist (a saved file wins) |
| `CULTS3D_TOKEN` | Cults3D links: `nickname:key`, used when `~/.config/cults3d/token` does not exist (a saved file wins) |
| `CREALITYCLOUD_TOKEN` | Creality Cloud downloads: the login, written `model_user_id:model_token`, used when `~/.config/crealitycloud/token` does not exist (a saved file wins). See [Connecting a login-token site from chat](PROJECT-REFERENCE.md#connecting-a-login-token-site-from-chat) |
| `THANGS_TOKEN` | Thangs downloads: the login token, used when `~/.config/thangs/token` does not exist (a saved file wins) |
| `NEXPRINT_TOKEN` | Nexprint downloads: the login token, used when `~/.config/nexprint/token` does not exist (a saved file wins) |
| `MYMINIFACTORY_OAUTH_TOKEN` | MyMiniFactory downloads: the OAuth sign-in's access token, used when `~/.config/myminifactory/oauth_token` does not exist (a saved file wins) |
| `AD5X_SERIAL` | Stock Flashforge printers: the printer serial number for the stock LAN API. Never a setting; set it together with `flashforge_stock.host` |
| `AD5X_CHECK_CODE` | Stock Flashforge printers: the printer check code for the stock LAN API. Never a setting |

The MakerWorld account token is not an environment variable. It is the file `~/.config/bambu/token` (Docker: `/data/.config/bambu/token`), needed only for MakerWorld links; see [MakerWorld account token](PROJECT-REFERENCE.md#makerworld-account-token).

The Creality Cloud download token is likewise a file, not an environment variable: `~/.config/crealitycloud/token` (Docker: `/data/.config/crealitycloud/token`), needed only for Creality Cloud downloads; see [Creality Cloud login token](PROJECT-REFERENCE.md#creality-cloud-login-token). Thangs and Nexprint work the same way, with `~/.config/thangs/token` and `~/.config/nexprint/token` (Docker: under `/data/.config/`); see [Connecting a login-token site from chat](PROJECT-REFERENCE.md#connecting-a-login-token-site-from-chat).

Direct file links — a public link straight at a `.stl` or `.3mf` file on any host, including a Dropbox share link — need no token and no setting; the fetch is guarded (public addresses only, the shared 200 MiB intake size limit) as described in [Direct file links](PROJECT-REFERENCE.md#direct-file-links).

## MQTT topics (#622)

Off unless `notify.mqtt_enabled` is true and `notify.mqtt_host` is set. Each event is one MQTT 3.1.1 QoS 0 publish to `<notify.mqtt_topic>/<event>`. The default root is `print-handy`, so a pause is `print-handy/paused`. The payload is one line: the printer id, when this process listens for one roster printer, then the same summary chat posted. Events: `print_started`, `paused`, `runout`, `watchdog_offer`, `complete`, `failed`, `spool_added`. There is no Home Assistant integration to install. A dashboard subscribes to those topics. The password is `MQTT_PASSWORD` in the environment, never in YAML and never in chat. Each publish waits for the broker's CONNACK (3 second timeout). A refused or unanswered publish is logged and does not block the chat post or the print. Each publish uses its own client id (`print-handy-` plus random hex), so several printers' listeners do not disconnect each other. Notifications are not printer actions. The watchdog still only notifies.

## ntfy (#622)

The second #622 channel, alongside MQTT: off unless `notify.ntfy_enabled` is true. A topic is required: set `notify.ntfy_topic` or put the topic in `notify.ntfy_url`, and settings validation refuses an enabled config with neither. Each event is one HTTP POST to the topic URL — `notify.ntfy_url` with `notify.ntfy_topic` appended, or the URL itself when it already names the topic. The title names the event (`Print Handy: complete`); the body is the same one-line payload the MQTT channel sends. A protected topic's access token is `NTFY_ACCESS_TOKEN` in the environment, sent as a Bearer token, never in YAML and never in chat. A refused or failed publish is logged and does not block the chat post or the print, and one channel's failure never blocks the other: with both on, an event counts as delivered when either channel takes it.

## Legacy environment names

The names below predate `PRINT_HANDY__SECTION__KEY` and still work; new installs should use the long form. The full map is `LEGACY_ENV` in `print_handy/settings.py`, for example `MOONRAKER_URL` → `services.moonraker_url`, `SPOOLMAN_URL` → `services.spoolman_url`, `PRINT_DISCORD_CHAT_ID` → `discord.chat_id`.
