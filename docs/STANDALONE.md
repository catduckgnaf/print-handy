# The Print Handy bot

This is the bot with no Docker around it: links, files, color buttons, slicing and printer alerts, all from one Python process. No AI needed. Also runs on Telegram or Matrix. One token, one bot; two copies means two answers to everything.

An OpenAI-compatible LLM is an optional extra for replying to free text. To run on Telegram instead of Discord, set `bot.platform` to `telegram` and follow
[Telegram bot setup](TELEGRAM-BOT-SETUP.md). The Telegram bot is a pure bot with no LLM at all, unmatched
text gets the short “didn't understand” reply even when `[llm]` is configured for another platform.

Or in a Matrix room: set `bot.platform` to `matrix` and follow
[Matrix bot setup](MATRIX-BOT-SETUP.md). The Matrix bot is a pure bot with no LLM too; prompts are a
numbered list answered by typing the number (Matrix has no buttons), and the room must be unencrypted.

The bot's handlers live in one module: `print_handy.bot.router` loads `plugins/print-feedback` through its
own `register()` hook and runs its handlers in order. Every safety gate applies: nothing heats, moves or
starts without a button press from an allowed user, and the G-code guards and fit checks run on every print.

Use one bot token for one bot. Two live connections on one token both answer every message.

## Discord setup

1. In the [Discord developer portal](https://discord.com/developers/applications), create an application and a bot.
   Copy the bot token.
2. Under **Bot > Privileged Gateway Intents**, turn on **Message Content** (needed for links, typed replies and
   "print it").
3. Invite the bot with the `bot` and `applications.commands` scopes and these permissions in the print channel:
   View Channel, Send Messages, Send Messages in Threads, Attach Files, Embed Links, Read Message History.
4. Copy the channel ID and the Discord user IDs of the people who may print (Developer Mode, then Copy ID).

## Settings

| Key | Default | What it does |
|---|---|---|
| `bot.mode` | `standalone` | The only mode: the bot runs itself (`python3 -m print_handy.bot`). A config stating the removed `hermes` mode is refused at load with a message naming the removal |
| `bot.platform` | `discord` | The chat platform the standalone bot talks to: `discord`, `telegram` or `matrix`. A config without this key keeps `discord` |
| `telegram.token_env` | `TELEGRAM_BOT_TOKEN` | Name of the environment variable that holds the Telegram bot token |
| `telegram.chat_id` | empty | The print chat. Group and channel IDs are negative. The bot ignores every other chat (forum topics inside it work like threads) |
| `telegram.allowed_user_ids` | empty | Telegram user IDs allowed to use the bot, separated by commas or spaces |
| `telegram.approval_user_id` | empty | Always allowed as well |
| `telegram.upload_max_bytes` | `50000000` | Largest file the Telegram bot uploads (the Bot API cloud limit); timelapse video is shrunk to this value, not Discord's |
| `matrix.homeserver_url` | empty | The Matrix homeserver base URL (matrix.org or self-hosted) |
| `matrix.access_token_env` | `MATRIX_ACCESS_TOKEN` | Name of the environment variable that holds the Matrix access token |
| `matrix.room_id` | empty | The print room's ID (starts with `!`). The room must be unencrypted; the bot ignores every other room |
| `matrix.allowed_user_ids` | empty | Matrix user IDs allowed to use the bot, full `@user:server` form, separated by commas or spaces |
| `matrix.approval_user_id` | empty | Always allowed as well |
| `matrix.bot_user_id` | empty | The bot's own Matrix user ID. Empty: asked from the homeserver at startup |
| `matrix.upload_max_bytes` | `50000000` | Largest file the Matrix bot uploads; lowered to the homeserver's media limit when it reports a smaller one. Timelapse video is shrunk to that limit; if it still cannot fit, the completion post says so instead of attaching the video |
| `bot.health_port` | `0` | Serve `GET /healthz` on this port (200 healthy, 503 not). `0` turns the HTTP check off |
| `bot.health_stale_s` | `120` | The health check fails when the heartbeat is older than this |
| `bot.sync_commands` | `true` | Register the slash commands with Discord at startup |
| `logging.level` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. Use `DEBUG` while chasing a problem; environment: `PRINT_HANDY__LOGGING__LEVEL` |
| `logging.format` | `text` | `text` is one readable line per event. `json` is one JSON object per line for a log shipper |
| `logging.file` | empty | Also write a rotating log file (5 MiB, 3 backups) at this path. Empty logs to stderr only |
| `discord.bot_name` | `Print Handy` | Name used in bot replies and help; environment: `PRINT_HANDY__DISCORD__BOT_NAME` |
| `discord.chat_id` | empty | The print channel. The bot ignores every other channel (threads inside it work) |
| `discord.allowed_user_ids` | empty | Optional whitelist of Discord user IDs, separated by commas or spaces. Empty lets everyone in the print channel use the bot |
| `discord.denied_user_ids` | empty | Optional deny list of Discord user IDs; a denied user is refused even when whitelisted or the approval user |
| `discord.approval_user_id` | empty | Always allowed as well; setting one does not restrict anyone else |
| `discord.token_env` | `DISCORD_BOT_TOKEN` | Name of the environment variable that holds the bot token |
| `discord.chat_llm_enabled` | `true` | Discord's chat-LLM switch. `false` runs Discord as a pure bot, unmatched text gets the short “didn't understand” reply, while `[llm]` stays configured for `/print-design`. Roasts and camera checks keep their own vision switches |
| `llm.chat_url` | empty | Optional OpenAI-compatible chat completions URL. Empty: unmatched text gets the short “didn't understand” reply |
| `llm.model` | empty | Model name, required when `chat_url` is set |
| `llm.api_key_env` | `LLM_API_KEY` | Name of the environment variable that holds the LLM API key, if it needs one |
| `llm.timeout_s` | `20` | LLM request timeout |
| `agent.chat_url` | empty | Stray-chat agent lane: an agent endpoint, any OpenAI-compatible chat completions URL, e.g. a Hermes agent behind its API, answers Discord free text ahead of the `[llm]` call. Empty: no agent lane |
| `agent.model` | empty | Agent model name; optional, empty omits `model` from the request |
| `agent.api_key_env` | `AGENT_API_KEY` | Name of the environment variable that holds the agent endpoint's API key, if it needs one |
| `agent.timeout_s` | `120` | Agent request timeout (one attempt, no retries) |
| `vision.roast_enabled` | `true` | The one-line roast: one per print on the completion post, the failure and cancel posts and a confirmed-spaghetti camera alert, plus on request in chat ("roast my last print"), on every platform. From the local roast corpus, no LLM or endpoint. `false` turns roasts off; camera checks are unaffected |
| `vision.roast_model` | `luna-latest` | Legacy, unused since #1002 (roasts are local); kept so older configs load |

Every key can come from the environment as `PRINT_HANDY__SECTION__KEY`, for example
`PRINT_HANDY__BOT__MODE=standalone` or `PRINT_HANDY__DISCORD__ALLOWED_USER_IDS="111 222"`.

For example, set `PRINT_HANDY__DISCORD__BOT_NAME="Workshop Printer"` in the process environment, or
`bot_name: "Workshop Printer"` inside the YAML `discord:` mapping. Docker Compose reads
the same variable from `.env`. Restart the bot after changing it. This changes its self-description,
not the Discord account username or server nickname; set those in Discord. The default stays `Print Handy`.

The token is only ever read from the environment variable `discord.token_env` names, or from the `.env` file in
`paths.hermes_home` (the data folder; the name is historical, it does not need Hermes). With no allow list,
everyone in the print channel can use the bot, the channel is the access boundary, and `check` reports
`access: channel (everyone)`; set `discord.allowed_user_ids` to restrict the bot to listed users, and
`discord.denied_user_ids` to exclude specific users in any mode. A refused message is logged at INFO with
the user ID and reason, and answered in channel once per user per hour, never silently dropped.

```yaml
# config/local.yaml
bot:
  mode: standalone
  health_port: 8080
discord:
  chat_id: "123456789012345678"
  allowed_user_ids: "111111111111111111, 222222222222222222"
paths:
  hermes_home: ~/.local/share/print-handy   # state, logs and the .env with DISCORD_BOT_TOKEN
services:
  moonraker_url: http://192.168.1.10:7125
  spoolman_url: http://192.168.1.10:7912
```

## Run

| Command | What it does |
|---|---|
| `python3 -m print_handy.bot` (or `... run`) | Start the bot. Refuses to start, and lists why, unless every check below passes |
| `python3 -m print_handy.bot check` | Offline setup check: mode, token present, channel, allowed users, discord.py, handlers loaded, no host module imported, state folder writable, LLM, roast. Exit 1 on any FAIL |
| `python3 -m print_handy.bot health` | Exit 0 only while a running bot reports a fresh heartbeat, is logged in, its process is alive and no host module is loaded in it |
| `python3 -m print_handy.hermes.moonraker_event_listener` | The printer listener. It posts over the configured platform's REST API with that platform's bot token (Discord REST, the Telegram Bot API when `bot.platform` is `telegram`, or the Matrix Client-Server API when it is `matrix`; its Discord button posts degrade to plain text on Telegram and Matrix) |
| `python3 -m print_handy.doctor` | Also reports the bot mode and the Discord login |

The bot writes its heartbeat to `<paths.hermes_home>/state/bot_health.json` every 15 seconds. `health` and
`GET /healthz` read the same record:

```json
{"mode": "standalone", "connected": true, "user": "Print Handy#1234", "handlers": 15,
 "hermes_modules": [], "heartbeat_at": 1792727951.3, "pid": 7, "chat_id": "123...", "allowed_users": 2,
 "llm": false, "open_prompts": 0, "started_at": 1792727900.1}
```

`hermes_modules` lists any loaded module from a host application (`tools`, `gateway`, `hermes_state`, `hermes_cli`). It must
be empty; the check fails otherwise. That is the proof the bot runs on its own.

### systemd

`scripts/install-standalone.sh` writes `print-handy-bot.service` and `print-handy-listener.service` to
`~/.config/systemd/user`, creates the data folder and a `.env` (mode 600) for the token, and enables nothing.
Follow the lines it prints.

### Container

The bot and the listener are two long-running processes from one image:

```
python3 -m print_handy.bot run                                  # Discord bot
python3 -m print_handy.hermes.moonraker_event_listener          # printer listener
HEALTHCHECK: python3 -m print_handy.bot health                  # or GET :$PRINT_HANDY__BOT__HEALTH_PORT/healthz
```

Required environment: `PRINT_HANDY__BOT__MODE=standalone`, `DISCORD_BOT_TOKEN`, `PRINT_HANDY__DISCORD__CHAT_ID`,
`PRINT_HANDY__DISCORD__ALLOWED_USER_IDS`, `PRINT_HANDY__SERVICES__MOONRAKER_URL`. Both processes must share
`paths.hermes_home` (state) and `paths.archive`; the bot also writes dropped files under
`<paths.cache>/discord-uploads`.

## What you get

* Post a MakerWorld, Printables, Thingiverse, MyMiniFactory, Creality Cloud, Thangs or Nexprint link, or drop one `.3mf` or `.stl`: the intake runs with the same
  buttons. "print it" after a link prints that link (the intake names the model and asks first); "print the last one again" and "print again" reprint the last job that actually printed (when that job's source model is gone, the bot names it and points to `/print-again` rather than picking an older model). "print 57" (or "print #57", "reprint 57", "print 57 again") reprints
  print #57 behind the same bed-clear button as `/print-again 57`; an unknown number gets a "no such print"
  reply pointing at `/print-again list`, not the help text. A bare number on its own is not a print request.
* `/spool` walks you through spools with buttons: it asks whether you are adding a **new** spool or
  **changing** the spool in a slot (listing and screen sync stay in the same menu). Adding offers the
  filament types already in the Spoolman database first, picking a known type supplies its colour and
  colour code from the database, so you are never asked for a hex code and never re-type a known colour;
  only a genuinely new type asks for brand / material / colour name and grams. Before anything is
  created, the confirmation names the spool number the new spool will be, and the result reports the
  number actually created. Changing a slot walks slot → replacement (a shelf spool, or add-new first) →
  what happens to the old spool: **back on the shelf**, **archive it**, or **mark it as depleted**
  (archived with 0 g left). Nothing is created, bound, archived or zeroed before that flow's
  confirmation, and cancelling or letting a question expire changes nothing. The full typed forms
  (`/spool new <brand, material, color, grams>`, `/spool change <slot> <spool#>`, `/spool list`,
  `/spool sync`) still work in one shot. Plain requests work too, with no LLM configured: "add new
  filament", "add a new spool", "we need to add new filament" and "change / swap / replace the
  filament" start the same guided flow; ordinary discussion about filament does not.
* `/print-spools` answers what is loaded, where, and how much is left: a loaded-only,
  read-only list; each loaded location with its spool number, brand, material, color, grams
  left and percent left where the spool's starting weight is recorded. `/spool loaded`, a bare
  `/print-colors` (no JOB.json; the archived-job form is unchanged), and plain asks ("what
  colors are loaded", "current spools", "how much filament is left") answer the same way, with
  no LLM configured. A clear typo of one of those asks ("currant spools", "what colours are <!-- us-spelling: allow colours -->
  loded") gets a "Did you mean?" prompt first; the view is shown only on a yes, never on a
  fuzzy match alone. The full inventory, shelf included, stays on `/spool list`. If Spoolman
  does not answer, locations and spools are still reported from the record, with amounts
  named unavailable.
* Estimates account for copies. Copies cloned onto one plate carry their totals in the sliced G-code
  itself, and the slice summary, estimate breakdown and start post show those totals as-is. Sequential
  copies (`/print-again … x3`, and multi-copy parts in a multipart plan) are labelled per copy: the
  confirmation and start posts add the all-copies printing time and filament, the completion prompt and
  the continuation summary show the updated remaining time and filament as copies finish, and no post
  quotes one copy's finish as the whole job's, sequential finishes depend on the bed-clear pauses
  between copies.
* Slash commands: `/print-next`, `/print-stop`, `/print-resume`, `/print-again`, `/print-reboot`,
  `/print-coldpull`, `/print-bed-down` (prepare to clean: separate bed and nozzle, asks first, never homes or heats), `/nozzle`, `/print-maintenance`, `/print-colors`, `/spool`, `/print-spools`, `/print-lookup`, `/print-troubleshoot`,
  `/print-macros` (Moonraker printers only; lists the macros the printer defines, and `run <name>` runs one behind a confirmation),
  `/print-firmware` (firmware version report on Moonraker printers; `update <component>` applies one component update behind a confirmation on ZMod and vanilla Klipper printers only; `guide zmod` / `guide stock` walk through the manual AD5X updates; other families get their manual update guidance),
  `/print-calibration` (printer calibrations with before/after values and an accept step before anything saves, on ZMod and vanilla Klipper printers; Z offset is report + guide; `records` lists past runs),
  `/print-handy` (guided setup and read-only debugging), `/print-source` (which model sites are connected), `/print-help`. Typing the same text works too.
* When a multi-plate job still has a plate queued, chat can offer the next plate once the camera's empty-bed check says the bed is clear. On Discord that offer uses the existing Start next plate button, and confirm runs the same start as `/print-next`. Cancel leaves the queue alone and ends the bed checks for that plate. The check runs only while the printer is idle, and each finished plate gets its own offer. Telegram and Matrix name `/print-next`, which still asks before starting. A bed that is not clear is posted once and does not offer. No camera, a dark frame, or no verdict posts nothing, and it does not invent an empty bed. Nothing starts without the confirm.
* `/print-design <what to make>` (or the short `/design`) designs a new part with AI + OpenSCAD (see
  [AI design](DESIGN.md)); with a model file dropped in the same message, or a model link in it, it edits
  that model instead, and phrases like "design me …" or "design this" start design without the command.
  Design is on automatically whenever an OpenAI-compatible endpoint is configured; the `[design]`
  settings, or `[llm]`'s, unless `design.enabled` is set false; it also needs the `openscad` binary on
  the machine (the container image includes it; a native install must add it, e.g. `apt install openscad`).
  Design runs on Discord, Telegram and Matrix; on Telegram and Matrix it is the one model-backed
  feature, served by the configured `[design]` endpoint only, ordinary chat there still gets no LLM
  reply and falls back to the short “didn't understand” reply, exactly as before.
* Listener posts (started, complete, failed, timelapse) and alert buttons (Resume, Cancel, spool questions, IFS
  insertion, plate picker) work while the bot runs.
* A pending spool question (filament runout or a mid-print swap) can also end the print: tap its **Cancel print**
  button or reply `cancel the print`. That cancels through the same path as `/print-stop`, closes the question
  and stops its reminders; filament usage stays paused for every spool, exactly as a cancelled print leaves it.
  If the cancel does not end the print, the question stays open and answerable.
* A pause caused by a filament runout is announced as a runout, not a generic pause: the listener combines the
  pause message, the printer's own filament sensor (switch or motion sensor, discovered from Moonraker's object
  list; a disabled, absent or unreadable sensor counts as no evidence, never as a runout) and, on printers with
  IFS slot presence, the slot readings. The alert names the slot and spool that ran out, is followed by the spool
  question for registering the replacement spool, and carries the usual **Resume print** / **Cancel print**
  buttons, Resume acts only when you press it and re-checks the paused job first. If a slot empties while the
  print is already paused and the pause had not been classified as a runout, the alert is re-posted as a runout
  alert (with the spool question) instead of only being logged.
* A spool created directly in Spoolman (its web UI, or another tool) gets a prompt too: on the same poll
  cadence as the IFS presence check, the listener diffs Spoolman's spool IDs against the ones it already
  knows (kept in the state folder; the first poll learns the current inventory silently, so existing spools
  never prompt). Each genuinely new, non-archived spool is named in Discord, number, brand, material and
  colour, exactly once, with **Load into a slot** (the guided change flow's slot → old spool's fate →
  confirm steps, with the new spool already chosen), **Leave on the shelf** and **Dismiss**. A spool that
  arrives already archived is learned silently, a deleted spool is forgotten silently, and when Spoolman is
  unreachable or not configured the poll simply skips; it never blocks the listener and never prompts.
* A typed reply answers your own open question ("2" for two copies, "split", "cancel"). It
  never answers another allowed user's question, even in the same channel or thread. Buttons are shared: any
  allowed user may press an open question's button, and the message then shows who chose.
* Anything else is tried with a model lane first; the `[agent]` lane when an agent endpoint is
  configured, else the LLM when `[llm]` is set, and only falls back to the short
  “didn't understand” reply when
  the model cannot solve it. The agent lane is different in kind from the LLM call below: the bot
  sends your text once, with no bot system prompt, and posts the endpoint's reply as-is; the
  endpoint (a Hermes agent behind its API, say) answers in its own voice, and its reply can never
  name a command into execution. The bot keeps no conversation history; it sends the live session
  key as an `X-Print-Handy-Session` header the endpoint may key its own conversation on. If a
  configured agent endpoint does not answer, the reply says so (“The chat agent didn't answer…”)
  instead of pretending the message was not understood. The order is: explicit commands, deterministic phrases (like "print 57" above),
  links/files and typed answers to an open question run first and are never rerouted through the model; any
  other text goes to the model, which may answer directly or name one command from a fixed list (read-only or
  button-gated: `/print-stop`, `/print-next`, `/spool list`, `/print-spools`, ...). A named command runs through the same
  handler as a typed one and asks for its button the same way. Only if the model declines, names no usable
  command, errors or times out does that reply appear, once, and never together with a model reply for
  the same message. The model can never run `/spool change`, `/spool new`, `/spool sync`, `/print-colors` or
  `/print-again stop`, and it never sees links, files or the printer. With no `[llm]` configured, unmatched
  text gets that short reply as before, and Telegram and Matrix never use the LLM at all (see above).

## Running Discord with no chat LLM

Discord can run as a pure bot too, the way Telegram and Matrix always do. Set
`discord.chat_llm_enabled` to `false` (or `PRINT_HANDY__DISCORD__CHAT_LLM_ENABLED=false`) and restart:
unmatched text gets the short “didn't understand” reply, and no chat message is ever sent to a model, while everything
deterministic (commands, links, files, typed answers, buttons, `print 57`) works exactly as before.
The switch gates every stray-chat model call: the `[llm]` call and the `[agent]` lane alike.
`bot check` says so on its llm and agent rows, naming the switch, and the bot's status reports `"llm": false`.

You do not have to remove `[llm]` to do this, and you should not if you use `/print-design`: design
inherits the `[llm]` endpoint when `[design]` is unset, and that inheritance keeps working with chat
off, design has its own switch (`design.enabled`). Two other features use a model through the
separate vision endpoint or need none, and keep their own gates: the **roast** (local, no LLM;
`vision.roast_enabled`, on by default) and the **camera watchdog** (`visual_watch.enabled`, on
`services.vision_chat_url`, or, with no endpoint at all, on the built-in local failure detector,
`vision.local_detector_enabled`, #455). Turning the chat LLM off changes none of them; turn each off at its own
switch if you want no model calls at all.

## How the bot is put together

| Piece | Where it lives |
|---|---|
| Dispatch handlers | `plugins/print-feedback` (the handler module); `print_handy.bot.router.Router` calls its `register()` and runs the handlers in order |
| Authorization, delivery adapter, session key | `print_handy.bot.runtime`: allowed user IDs plus the print channel; replies go to the same channel or thread; one session per person per channel |
| Adapter `send` / `send_clarify` | The platform interface in `print_handy.bot.platform`: `send`, `send_clarify`, `answer_typed`, `close_prompt`, `is_allowed_id`, plus per-platform limits (`message_limit`, `upload_max_bytes`, `supports_buttons`, `max_buttons`). Discord's implementation is `print_handy.bot.adapter.StandaloneAdapter` over discord.py. `MEDIA:/path` becomes an attachment; long text is split at 2000 characters |
| Question registry (register, wait, resolve, cancelled) | `print_handy.core.prompts`. The bot draws its own questions: they are posted from the bot's own registry, clicks and typed answers are resolved by the bot, and an answered question edits in place ("you chose X", buttons removed) |
| Platform handler registration and the adapter's `_is_allowed_user` | The router hands the handler module's `alert_button_handlers` our discord.py client; the adapter implements `_is_allowed_user` |
| Listener posts | `print_handy.bot.rest.send`: Discord REST with the bot token, `MEDIA:` files as attachments; no subprocess poster |
| "print it" link recall | `print_handy.bot.recent_links`: the last model link per channel in `<state>/recent_model_links.json` |
| /print-design model | The `[design]`/`[llm]` HTTP transport: the configured endpoint answers |
| Data folder, install script, systemd units | `paths.hermes_home` is just the data folder (the name is historical); `scripts/install-standalone.sh` and `systemd/*.service.template` |

There is no agent behind the bot: a message no handler claims simply ends (or gets the
bounded stray-chat reply, above) instead of falling through to an assistant.

## Moving from a Hermes-hosted install

The Hermes install method and `bot.mode: hermes` are removed; a config stating that mode
is refused at load with a message naming the removal. Moving an existing install over:

1. Stop the gateway for this bot (or remove the Discord token from that profile). One
   token, one connection: two live connections on one token answer twice.
2. Keep `paths.hermes_home` pointing at the old profile folder to keep its state (print
   numbers, plans, alerts), or copy its `state/` folder to the new data folder.
3. Set `bot.mode: standalone` (or remove the mode line) and `discord.chat_id`
   (`discord.allowed_user_ids` is optional), and put `DISCORD_BOT_TOKEN` in the
   environment or the data folder's `.env`.
4. Run `python3 -m print_handy.bot check`, then `scripts/install-standalone.sh` and enable both units.
5. Optional, keep agent answers for stray chat: point `[agent] chat_url` (and
   `api_key_env`) at an agent API endpoint (a Hermes agent behind its API works). Without
   it, stray chat uses the `[llm]` endpoint if one is configured, else the short bounded
   reply.
6. If the profile pinned a design model (`auxiliary.print_design`), set the same model
   under `[design]` (or `[llm]`): design resolves only its HTTP endpoint now.

## Adding a chat platform

For contributors: the bot core and the plugin handlers talk to a chat service only through
`print_handy.bot.platform`, `BotCore` is built with one platform adapter and never imports a platform
library. A new platform is a new module under `print_handy/bot/` holding a `StandaloneAdapter`-shaped
implementation of `send` / `send_clarify` / `answer_typed` / `close_prompt` / `is_allowed_id` (prompt
bookkeeping and choice resolution are shared in `BasePlatformAdapter`), a client that turns service
events into `runtime.make_source` / `runtime.make_event` values, its name in `platform.KNOWN_PLATFORMS`
and its module in `platform.platform_module`, a settings section validated in `settings.py`, and a fake
in `tests/bot/` that drives a full intake with no network (see `tests/bot/test_platform.py`). Per-platform
limits live on the adapter, not in callers. No handler or core change should be needed.

## Not covered yet

* Free-form agent work has no equivalent: filament photo inventory, incident write-ups, mesh routines from chat
  and free-form troubleshooting. With an LLM configured you get a short reply, not an agent.
* Button questions asked by a running intake live in memory. After a bot restart an old question's buttons answer
  "That question already closed"; send the link again. Listener alert buttons do survive restarts.
