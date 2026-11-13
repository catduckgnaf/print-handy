# Telegram bot setup

Print Handy can run its standalone bot on Telegram instead of Discord. The same handlers, prompts and safety gates serve both platforms;
only the transport changes. The Telegram bot is a **pure bot: it has no
LLM**. Unmatched text gets the short “didn't understand” reply, and no `[llm]` setting is
ever consulted for Telegram; that is deliberate, and it is how the
no-LLM standalone mode from the standalone design is being proven first.

One-way posts are the exception to "no model is involved": the
completion posts carry the **roast**; a
one-line joke at the model's expense, from the local roast corpus, on
Telegram just as on Discord. See [The roast](#the-roast) below. It never
answers messages and changes nothing about the pure-bot rule above.

The bot talks to the Telegram Bot API by long polling, so it needs no
inbound port, no public URL and no webhook. It uses only the Python
standard library; there is no Telegram package to install.

## 1. Create the bot with BotFather

1. In Telegram, open [@BotFather](https://t.me/BotFather) and send `/newbot`.
2. Pick a display name and a username ending in `bot`.
3. BotFather replies with the bot token (it looks like
   `123456789:AA...`). Keep it secret: anyone with the token controls the
   bot. If it ever leaks, revoke it with `/revoke` in BotFather.
4. Optionally send `/setcommands` to BotFather and paste the commands from
   `print_handy/bot/commands.py` (names use underscores, e.g.
   `print_next`). The bot also registers its command menu itself at
   startup, best-effort, and typed commands work either way.

## 2. Create the print chat and find the IDs

1. Create a Telegram group for printing and add the bot to it. A private
   one-to-one chat with the bot works too.
2. **Chat ID**: send a message in the group, then read it back from
   `https://api.telegram.org/bot<token>/getUpdates`; the `chat.id` of the
   message is the chat ID. Group and supergroup IDs are **negative**
   (for example `-1001234567890`); a one-to-one chat ID is your own
   positive user ID. Tools like @userinfobot can also show it.
3. **Your user ID**: message [@userinfobot](https://t.me/userinfobot), or
   read `from.id` in the same `getUpdates` output. User IDs are positive
   numbers. Collect the ID of every person who may print, plus the
   approver's.

Forum groups (topics enabled) work: the bot scopes each topic like a
Discord thread, so a prompt opened in one topic is answered in that
topic.

With several printers (a `printers:` list, see
[SETTINGS.md](SETTINGS.md)), give each printer its own group and put that
group's chat ID in the printer's `chat_id` entry: each chat then drives
only its own printer, end to end. The `[telegram] chat_id` above remains
the fallback channel for the one entry that sets no `chat_id` (the first,
default printer). A chat no printer claims gets silence. Two entries may
also claim the *same* group on purpose: that group becomes a shared
channel where the bot asks which printer before anything
printer-bound happens, see "Shared channels" in SETTINGS.md.

## 3. Configure Print Handy

```yaml
# config/local.yaml
bot:
  mode: standalone
  platform: telegram
telegram:
  chat_id: "-1001234567890"
  allowed_user_ids: "424242, 515151"
  approval_user_id: "424242"
services:
  moonraker_url: http://192.168.1.10:7125
```

Put the token in the environment variable `[telegram] token_env` names
(`TELEGRAM_BOT_TOKEN` by default), in the process environment or in the
`.env` file in `paths.state_home`, mode 600. Never put the token in a
config file. Every setting can also come from the environment, for
example `PRINT_HANDY__TELEGRAM__CHAT_ID=-1001234567890`.

Nobody allowed means nobody can use the bot: with no allowed users every
message is ignored and `check` fails.

## 4. Check and run

```bash
export TELEGRAM_BOT_TOKEN=...   # or put it in the data folder's .env
python3 -m print_handy.bot check   # offline: mode, token present, chat, allowed users, handlers, no Hermes
python3 -m print_handy.bot         # start polling
```

Printer event posts (started, complete, failed, timelapse) come from the
listener, unchanged: in standalone mode with `platform: telegram` it
posts through the Telegram Bot API instead of Discord REST.

## The roast

When a print finishes, the completion post ends with a one-line **roast** of
the model, exactly one per print, one short sentence from the bot persona.
Prints that do not finish get one too, from the bank that matches how they
ended: the failure post draws from the failed bank, the cancel post from the
cancelled bank, and a camera alert that confirms spaghetti carries a line
from the spaghetti bank.
You can also ask for one in plain words: "roast my last print", "roast print
84", "roast the dragon" (typos are fine; the print is found the same way
`print 84` and `/print-again <name>` find it).

Roasts need **no LLM and no endpoint**. Each line comes from the
local roast corpus shipped with Print Handy (`print_handy/data/roasts/`) and
is filled from facts about that print, title, duration, material, color,
grams, printer, finish hour, how many times it has been printed. A line that
needs a fact Print Handy does not have is never used, a failed or cancelled
print gets a gentle line about that and never a cheerful one, and the same
line does not come back until the rest have had a turn.

```yaml
# config/local.yaml
vision:
  roast_enabled: true   # this is the default; shown for clarity
```

In Docker the same switch is `PRINT_HANDY__VISION__ROAST_ENABLED`, also
defaulting to `true`.

- **Turn it off** with `vision.roast_enabled: false` (or
  `PRINT_HANDY__VISION__ROAST_ENABLED=false`). Camera checks are unaffected.
- `python3 -m print_handy.bot check` reports the roast on its `roast` line:
  on with the size of the local corpus, or off by the switch.
- `vision.roast_model` is no longer used; older configs that set it load
  unchanged.
- The roast is a post decoration, not the chat LLM: Telegram still
  never consults `[llm]`, and unmatched text still gets the command
  list.

## AI design on Telegram

`/print-design` (or `/design`) and the design phrases ("design me …",
"design this" with a model file attached as a document, or a model link)
work on Telegram too, see [AI design with OpenSCAD](DESIGN.md). Design is
the one model-backed feature here, and it runs only through an explicitly
configured `[design]` endpoint (or the `[llm]` values it inherits):

```yaml
# config/local.yaml
design:
  chat_url: http://192.168.1.10:8317/v1/chat/completions
  model: sol-latest        # a stronger model for design turns
```

With an endpoint configured, design is on automatically (set
`design.enabled: false` to force it off). With none configured, the
commands answer "design is not set up". This changes nothing about the
pure-bot rule: design turns call only that design endpoint, ordinary chat
still never consults `[llm]`, and unmatched text still gets the command
list. The Slice it / Change it / Cancel choices are inline buttons, like
every other Telegram prompt.

## Pairing with moonraker-telegram-bot (ZMod companion)

If your printer runs ZMod (an AD5X, say), you may already run
[moonraker-telegram-bot](https://github.com/nlef/moonraker-telegram-bot)
by nlef. It is the bot ZMod itself uses, on a separate host, in place
of moonraker-timelapse, and the two bots are complements: they run
side by side against the same Moonraker, each with its own token.

| Job | Which bot |
| --- | --- |
| Prepare a print: link or file in, colour/fit questions, slicing, and a confirmation before anything prints | Print Handy |
| Operate the running printer: status, pause, resume, cancel, emergency stop, macro buttons, power devices | moonraker-telegram-bot |
| Push notifications with camera photos while a print runs | Either, pick one notifier (below) |
| A rendered timelapse video in the chat | moonraker-telegram-bot (frames by time interval or Z-height, rendered at 15 fps by default) |

Boundaries, stated plainly:

- moonraker-telegram-bot is a **separate upstream project**. Print
  Handy does not bundle it, does not require it, and does not support
  it, report its problems to its own tracker. It works with
  **Moonraker printers only**.
- Its control commands **act on the printer as soon as they are
  tapped**. Only the commands in its `[telegram_ui]
  require_confirmation` list ask first (by default that list covers
  cancel, emergency stop, shutdown, restarts and power; pause, resume
  and status are not in it). That is a different safety model from
  Print Handy's, where a prepared job prints only after an explicit
  confirmation. Decide on purpose which bot may control your printer.
- Print Handy's Telegram bot stays a prep bot: it has no live printer
  control commands, and pairing changes nothing about that.

### Two bots, two tokens, never share one

Create **two** bots with BotFather and give each program its own
token. A Telegram bot token supports exactly one `getUpdates` poller;
if both programs poll the same token they steal each other's updates
and both misbehave. Print Handy's token goes in `TELEGRAM_BOT_TOKEN`
(or the `[telegram] token_env` you configured), the companion's in the
`bot_token` of its own `telegram.conf`. Both bots can sit in the same
Telegram chat.

### Pick one notifier

Both bots can post print-started, progress and print-finished messages,
so an unconfigured pair posts everything twice. Choose one notifier and
silence the other:

- **The companion notifies; Print Handy stays quiet.** Do not run Print
  Handy's listener: in a standalone install, start
  `print-handy-bot.service` but not `print-handy-listener.service`.
  Print Handy has no per-message notification switch; the `[listener]`
  settings only set timing intervals, and every event post (started,
  complete, failed, timelapse) plus the camera-watch alerts and spool
  questions comes from that one process, so those go quiet too. In
  Docker the listener is part of the supervised container, so silence
  the companion instead (next bullet).
- **Print Handy notifies; the companion stays quiet.** Remove the whole
  `[progress_notification]` section from the companion's
  `telegram.conf` (or never add it). Its notifier is enabled by that
  section existing at all; without it, the bot sends no print-started,
  progress or print-finished posts. Its on-demand commands (`/status`
  and the buttons) and its timelapse still work, and it still reports
  printer errors.

Companion-side sources, checked 2026-10-26: the upstream
[sample-config wiki page](https://github.com/nlef/moonraker-telegram-bot/wiki/Sample-config)
(`[progress_notification]`, `[timelapse]`, `[telegram_ui]
require_confirmation`) and the project's `bot/configuration.py` /
`bot/notifications.py`, where the notifier's enabled flag is exactly
"the `[progress_notification]` section is present".

## How Telegram differs from Discord here

- **Buttons**: prompts are inline keyboards. Answering edits the message
  to remove the keyboard and record the choice.
- **Alert buttons**: on Moonraker (Klipper and ZMod) printers, pause and
  stop alerts carry their Resume / Cancel buttons on Telegram too, as an inline keyboard with the same actions
  as Discord; a press re-reads the printer first, exactly like a Discord
  click. The listener's other button posts (plate picker, insertion and
  spool questions) are still sent as plain text on Telegram. The text
  names the typed command or reply that does the same job. Pause alerts
  from Bambu Lab, PrusaLink, OctoPrint, SDCP and stock Flashforge
  printers stay plain text on Telegram and name the typed command.
- **Messages are plain text.** No Markdown parse mode is set, so file
  names, model names and G-code fragments are shown exactly as written
  (Discord's `**bold**` markers appear literally).
- **Limits**: 4096 characters per message (long replies are split),
  files up to 50 MB through the Bot API. Model files (.3mf/.stl) arrive
  as documents; previews and timelapses are sent back as photos/videos
  when Telegram has a native type for them, everything else as a
  document. Timelapse video is sized to `telegram.upload_max_bytes`
  (50 MB by default), not to Discord's smaller cap, so Telegram keeps
  the quality Discord's limit would have thrown away.
- **Commands** arrive as text: `/print-next plan-1`. The `@YourBot`
  suffix that group chats add is stripped when it names this bot; a
  command addressed to a different bot (`/spool@OtherBot`) is ignored.
  Mentioning the bot works in place of the slash: `@YourBot spool list`
  runs `/spool list`, and so does the configured `bot_name` typed at
  the start ("Print Handy, spool list"). Even with BotFather's privacy
  mode on, Telegram delivers commands and `@YourBot` mentions in a
  group, so mentioning the bot works either way.
- **The roast**: completion posts carry the same one-line roast as Discord, from the
  local corpus with no LLM (`vision.roast_enabled`, on by default; see [The roast](#the-roast)).
