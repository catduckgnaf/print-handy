# Guided setup and debugging in Discord

[Documentation home](README.md) · [Discord bot setup](DISCORD-BOT-SETUP.md) · [Standalone bot](STANDALONE.md)

Print Handy has three guided Discord flows, available to allowed users in the print channel:

| Command | What it does |
| --- | --- |
| `/print-handy setup` | Start or resume guided setup, one small question at a time |
| `/print-handy add-printer` | Add another printer to this install by walkthrough, proven live |
| `/print-handy debugging` | Run read-only diagnostics and report redacted, actionable findings |
| `/print-handy status` | Show the saved setup checklist |
| `/print-handy cancel` | Pause setup; the saved checklist is kept |
| `/print-handy help` | List these commands |

Typing the same text works, like the other commands. Neither flow prints, moves, heats, or
authorizes a printer, and neither flow can start one.

## Setup

Setup asks six small steps, validating each answer before moving on:

1. **Printer model**: a model key from the printer registry (see
   [printer profiles](PRINTER-PROFILES.md)).
2. **Print channel**: the numeric Discord channel ID.
3. **Allowed users**: numeric Discord user IDs, comma separated.
4. **Printer URL** (optional); the address for the model chosen in step 1. Klipper
   (Moonraker) and OctoPrint take a URL, and that host can be another computer.
   Bambu Lab, PrusaLink, stock Flashforge, and Elegoo SDCP take a hostname or IP,
   not a full URL. A model with no address accepts only `skip`.
5. **Spoolman URL** (optional), validated as a URL. Spoolman is printer-independent, so
   this step is offered for every model, including models with no Moonraker connection.
   The Spoolman included in the Docker stack is at `http://spoolman:8000`; an
   existing external Spoolman keeps its own URL, exactly as in the CLI setup wizard.
6. **Bot token stored securely**: this step never collects the token. It explains how to
   store it: as the environment variable named by `[discord] token_env` (default
   `DISCORD_BOT_TOKEN`) in the profile/data folder `.env` file with mode 600, as in the
   [Discord bot setup guide](DISCORD-BOT-SETUP.md). Reply `stored` when it is saved, or
   `skip` to do it later. Pasting a token into chat is rejected and never saved.

### Progress, cancellation, timeout, and recovery

- After each answer the bot replies with the next question and a checklist showing what is
  done, what is current, and what remains.
- The checklist is saved in the state folder (`guided-setup.json`, file mode 600) after
  every step. `/print-handy cancel` pauses setup without losing it; a bot restart does not
  lose it either. `/print-handy setup` resumes at the first unfinished step, and
  `/print-handy setup restart` discards the saved answers and starts over.
- A session that has been idle for 24 hours is timed out: a plain reply after that is not
  treated as an answer. Run `/print-handy setup` to resume the saved checklist.
- An invalid answer (a non-numeric ID, a bad URL, an unknown model key) is rejected with the
  reason and the same question is asked again; the step does not advance.

### The consent step

Reaching the end writes nothing. The review step summarizes the answers (IDs are not
repeated back) and offers four replies:

- `apply`: the explicit consent to write. Only then are the collected settings merged
  into `config/local.yaml` through the same merge the CLI setup wizard uses: values already
  in the file always win, and no secret is written (the flow collected none).
- `add`: set up another printer. The flow asks for the first printer's id (the base
  flow never needed one) and then, per added printer, its id, model, own channel and
  Printer URL (a URL or a hostname, matching that printer). Applying then writes a `[[printers]]` roster instead of the single-printer
  sections, and refuses, leaving the file untouched, when the existing file already
  describes a printer or cannot be rewritten faithfully. Each added printer gets its own
  channel binding and state folder; Spoolman stays one shared inventory.
- `manual`: nothing is written; the bot returns precise manual steps naming the file,
  sections, and keys to set, where the token belongs, and the offline checks to run.
- `/print-handy cancel`: leave setup unfinished and resume later.

Setup never installs software, restarts a service, or changes network exposure. Those stay
manual, deliberate actions from the install guides.

## Adding a printer

`/print-handy add-printer` onboards another printer onto an install that is already set up, 
including one still on the legacy single-printer settings shape. It runs on the same
checklist machinery: one question at a time, the checklist saved after every step,
`/print-handy cancel` pauses and `/print-handy add-printer` resumes, and nothing is written
until `apply` at the review step.

1. **Printer model**: a model key from the registry, as in setup.
2. **Display name**: the name the which-printer picker and announcements show when several
   printers share the channel. It must differ from your other printers' names; the
   roster id is derived from it.
3. **Connection fields for that model's kind**: a Moonraker URL; a Bambu Lab LAN address,
   serial and the LAN-mode choice; a PrusaLink host; an Elegoo SDCP host (with the note that
   SDCP has no authentication at all); an OctoPrint URL; or a stock FlashForge host. Leaving
   the address blank (or choosing no LAN connection) adds the printer as an offline entry.
4. **Secrets are never collected in chat.** (The one deliberate exception is a model-site login
   token the operator chooses to paste; see
   [Connecting a login-token site from chat](PROJECT-REFERENCE.md#connecting-a-login-token-site-from-chat).)
   Where the connection needs one, the step names
   the environment variable to store it in; a per-printer name for Bambu Lab
   (`BAMBU_LAN_PASSWORD_<ID>`) and PrusaLink (`PRUSA_LINK_API_KEY_<ID>`), so each printer
   keeps its own secret, and you reply `stored` when it is saved in the `.env`, exactly
   like the token step in setup. OctoPrint and stock FlashForge use one shared variable
   name today (`OCTOPRINT_API_KEY`, `AD5X_SERIAL`/`AD5X_CHECK_CODE`); when a second printer
   of those kinds is added, the step says plainly that both printers must share it.
5. **Live connection test**: before anything is saved, the flow tests the real connection
   with the lightest proof the printer's kind offers (a Moonraker info read, a Bambu state
   snapshot, the SDCP status read that also discovers the printer's board id, and so on).
   A Moonraker test also reads nozzle diameter, bed size, and maximum Z from the printer
   config. A failed geometry read is named on the review and does not fail the connection.
   When those numbers disagree with the model you picked, the review warns and names the
   generic Klipper profile as an alternative. It does not change the model. Apply still
   uses the model you picked.
   A failure says why and offers `retry`, `back` to the step that failed, or `offline` to
   save the printer unverified instead.
   A passed test also reads the printer's thermal facts, read-only (chamber sensor and
   reading, fans, door switch; see
   [PRINTER-PROFILES.md](PRINTER-PROFILES.md#per-printer-facts-from-the-connected-printer)).
   When the printer reports something its model record does not already say, one more
   step lists each proposal with its evidence. Reply `all`, `none`, or the numbers to keep.
   Confirmed facts are saved for this printer only, at `apply`. OctoPrint printers cannot
   be probed, and a busy or failed probe proposes nothing.
6. **Review and `apply`**: the review names the printer, its id, its connection and whether
   the live test passed. Applying appends the entry to the `printers:` roster in the
   settings file (a legacy single-printer file is converted into the roster first; the
   printer you already run becomes entry 1 with its connection values unchanged; a
   timestamped backup of the file is always kept, and the rewrite does not preserve
   comments) and creates the new printer's first hardware record. No `chat_id` is written:
   the new printer serves the same shared channel under its name.

Applying never restarts anything. Restart Print Handy afterwards to bring the new printer
live; until then it has no listener and health checks report it missing, which the review
and completion messages call out as expected rather than a failure.

## Debugging

`/print-handy debugging` runs these read-only checks and reports each as ok, warning,
failed, or skipped, with a concrete next step for anything that needs one:

- **Configuration**: the settings load and validate; the failing key is named when not.
- **Bot mode and login**: the configured mode, whether the token
  environment variable is set, plus a read-only Discord login check (`GET /users/@me`).
- **Permissions**: the print channel ID and allowed user IDs are present and numeric, and
  the report lists the channel permissions the bot needs (View Channels, Send Messages,
  Send Messages in Threads, Embed Links, Attach Files, Read Message History). Channel
  overrides themselves are only visible to the logged-in bot; checking them is a listed
  manual next step when this check fails.
- **Service connectivity and health**: for each configured service (Moonraker, Spoolman,
  OctoPrint), one read-only GET against its health/version path. Unconfigured services are
  skipped, not failures.
- **Executables and versions**: the Python version, whether the configured slicer command
  exists and runs (`--version`), and whether ffmpeg is present when configured.
- **State and logs**: the state folder exists and is writable, whether a log file is
  configured and its folder exists, and the standalone bot heartbeat when one is present.

Debugging is read-only by default and in fact: its only network calls are the GET probes
above, it never calls a printer operation or a write endpoint, and it changes no settings.
Remediation that would edit configuration, install software, restart a service, or change
network exposure is never performed by the command; the report gives the manual next step
instead, and any such change needs your explicit go-ahead outside this flow.

### Redaction

Every line of the report is redacted before it is sent: token and password values are
removed, secret-shaped values and Discord-token-shaped strings are removed, private
filesystem paths are replaced with a placeholder, and long numeric identifiers (channel
and user IDs) are masked. Exact state/log paths therefore do not appear in Discord; run
`python3 -m print_handy.bot check` locally when you need the exact path.

## How this relates to the CLI checks

- The optional [Web UI](WEBUI.md) is a third setup surface over the same YAML settings:
  its `/config` page renders the catalogued settings with their source layer, previews a
  change before writing, and applies the same settings validation. It does not replace
  this guided flow, and neither surface collects secret values.
- `python3 -m print_handy.setup_wizard` is the full terminal installer wizard (printer
  registry menus, service installs with their own confirmations). The Discord setup flow
  covers initial configuration only and reuses the wizard's validation and config merge.
- `python3 -m print_handy.bot check` validates the standalone bot offline.
- `python3 -m print_handy.doctor` remains the machine-level check; unlike `/print-handy
  debugging` it is a terminal command whose output is not redacted for Discord, and it can
  contact configured services, run it only where those contacts are authorized.
