<p align="center">
  <img src="assets/branding/logo-lockup-dark.png" alt="Print Handy logo" width="480">
</p>

# Print Handy

[![CI](https://github.com/catduckgnaf/print-handy/actions/workflows/ci.yml/badge.svg)](https://github.com/catduckgnaf/print-handy/actions/workflows/ci.yml)

**Prepare and manage 3D prints from Discord, Telegram or Matrix. Just Drop a link or a file, answer a few questions and you are done!**

Current release: **2.2.3**

[Get started](#get-started-with-docker) · [See it in action](#see-it-in-action) · [Documentation](https://catduckgnaf.github.io/print-handy-docs/) · [Contribute](CONTRIBUTING.md) · [Releases](https://github.com/catduckgnaf/print-handy/releases)

## How it works

1. **Share a model.** Drop a MakerWorld, Printables, Thingiverse, MyMiniFactory, Creality Cloud, Thangs or Nexprint link in the chat, a direct link to an STL or 3MF file (a Dropbox share link works too), or upload an STL or 3MF file. MakerOnline links are checked for their file inventory only; downloads are not supported. Cults3D links are recognized the same way, but Cults' API exposes listing details only, never the files, so intake names that gap instead.
2. **Make a few choices.** A model that comes as several files (a box, its lid, a latch) prints as one job; you only pick between versions of a part, such as a lid with or without a logo. Print Handy walks you through colors and filament, how many copies, and — if the model needs it — how it should fit on the bed. Then it slices the model for your printer.
3. **Check and confirm.** You see the result before anything happens. Only when you approve it does Print Handy send the job to a configured printer.
4. **Follow along.** Progress, alerts and timelapses come back to the same chat.

That's the whole idea: the convenience of printing from chat, with a human — you — making the final call every time.

## See it in action

**Drop a link, pick your colors.**

<img src="assets/screenshots/discord-01-link-and-colors.png" alt="Print Handy in Discord: paste a model link, the bot checks the model, then asks which color each part prints in" width="720">

**Choose how many copies, then confirm — nothing prints until you do.**

<img src="assets/screenshots/discord-02-copies-and-confirm.png" alt="Print Handy in Discord: choose the number of copies, then confirm the estimate with Slice and print now, Slice only, Stop with colors saved or Cancel print request" width="720">

**The print starts — and yes, it roasts you a little.** When a model endpoint is configured, the print-started post can end with a one-line roast of your model.

<img src="assets/screenshots/discord-03-print-start.png" alt="Print Handy in Discord: the print started post, ending with a one-line roast of the model" width="720">

<details>
<summary><b>More of the conversation: fit, spools, the watchdog and the finish</b></summary>

1. **If it doesn't fit, choose how it fits.** The model is measured, never silently shrunk: split it across plates at full size, turn it, shrink it — or cancel. A small STL whose units are unclear gets a millimeters-or-inches question first. ![The fit question and the units question](assets/screenshots/discord-08-fit-and-geometry.png)
2. **It finishes, with a timelapse.** ![Print complete post with timelapse](assets/screenshots/discord-04-print-complete.png)
3. **The camera watchdog spots trouble.** ![Camera alert with Pause, It's fine and Stop asking for this print](assets/screenshots/discord-05-camera-watchdog.png)
4. **Pause, runout and the spool question.** ![Paused post and runout spool question](assets/screenshots/discord-06-pause-and-runout.png)
5. **Change a spool, guided.** Pick the slot, pick the new spool, decide what happens to the old one, confirm. ![Changing a spool, guided](assets/screenshots/discord-09-spool-change.png)
6. **A spool added in Spoolman gets offered for a slot.** ![New spool prompt and load flow](assets/screenshots/discord-07-new-spool.png)

These are mockups built from the bot's real message text and buttons, with placeholder names; the roast line is an example of the style, since real roasts are generated per print. Sources for every line are in [assets/screenshots](assets/screenshots/README.md).

</details>

## Get started with Docker

Docker is the easiest way to run Print Handy: one container does everything, and you start it with one command.

### What's in the box

Everything below is bundled in the one published image — there is nothing else to install:

| Part | What it does |
|---|---|
| Chat bot | Your interface: links, uploads, guided slicing, spool commands and print confirmation |
| Printer listener | One per printer, matched to how that printer connects. It follows the print and reports progress, alerts and timelapses to the chat |
| Spoolman | The filament and spool record keeper, bundled as an API only — Print Handy is its interface, so there is no separate Spoolman web UI to run. Already have your own Spoolman? Point Print Handy at it instead (see below) |
| OrcaSlicer | The slicer, in stable and preview channels |
| OpenSCAD | CAD splitting, and the engine behind designing parts in chat |
| ffmpeg and go2rtc | Timelapse capture and assembly, and the camera fan-out the watchdog and live view share |

If a required part stops, the container stops so Docker can restart it, and the health check covers the bot, the listeners and Spoolman.

**You need** a 64-bit Debian or Ubuntu machine, or a Raspberry Pi 4 or 5 with 64-bit Raspberry Pi OS. The image (`ghcr.io/catduckgnaf/print-handy`) is published for `linux/amd64` and `linux/arm64`.

### Run it

1. Download this repository (or just its `docker-compose.yml` and `.env.example`), and copy `.env.example` to `.env`.
2. In `.env`, fill in the three things Print Handy cannot guess: your chat platform's bot token and channel (Discord is the default; set `PRINT_HANDY__BOT__PLATFORM` for Telegram or Matrix), and your printer — its model key from [the printer groups below](#is-my-printer-supported) (the [printer support guide](docs/PRINTER-SUPPORT.md) lists the exact keys) and, for a Moonraker printer, its address, for example `PRINT_HANDY__SERVICES__MOONRAKER_URL=http://192.168.1.60:7125`. You can leave the allowed-users list empty — then everyone in the print channel can use the bot.
3. Use the `docker-compose.yml` from this repository — one service that runs the whole box, with every optional feature commented in. This is what it looks like (trimmed; the file itself has the full comments):

```yaml
services:
  combined:
    image: ${PRINT_HANDY_IMAGE:?Set PRINT_HANDY_IMAGE to a tested image digest or local tag}
    restart: unless-stopped
    env_file: [.env]
    environment:
      PRINT_HANDY__BOT__MODE: standalone
      PRINT_HANDY__SERVICES__SPOOLMAN_URL: ${PRINT_HANDY__SERVICES__SPOOLMAN_URL:-http://127.0.0.1:8000}  # the bundled Spoolman
      # …optional features (camera watch, Web UI, live view) are set here or in .env…
    volumes:
      - ${PRINT_HANDY_DATA:-./data}/hermes:/data/hermes
      - ${PRINT_HANDY_DATA:-./data}/archive:/data/archive
      - ${PRINT_HANDY_SPOOLMAN_DATA:-./data/spoolman}:/data/spoolman
    user: "1000:1000"
    healthcheck:
      test: [CMD, python3, /usr/local/bin/print-handy-healthcheck]
```

In `.env`, tell it which image to run and where its data lives:

```ini
PRINT_HANDY_IMAGE=ghcr.io/catduckgnaf/print-handy:2.2.3
PRINT_HANDY_DATA=./data
```

Pinning by digest instead of a tag is recommended for production; the digest for each release is in its release notes, and the [Docker guide](docs/DOCKER.md) shows the exact format. The data folder must be writable by UID 1000; Spoolman data lives in its `spoolman` subfolder unless you set `PRINT_HANDY_SPOOLMAN_DATA`.

4. Start it:

```bash
docker compose up -d
```

5. **Say hello in your chat channel and run `/print-handy setup`.** Guided setup walks a fresh install through a saved, resumable checklist — printer, channel, users, services — and writes nothing without an explicit `apply`, so you don't have to get every setting right in `.env` first try.

That's it. The [Docker guide](docs/DOCKER.md) covers health checks, upgrades, rollback and the full platform matrix.

- **Already run your own Spoolman?** Point `PRINT_HANDY__SERVICES__SPOOLMAN_URL` at it and set `PRINT_HANDY_BUNDLED_SPOOLMAN=false`. Nothing is migrated either way.
- **On Unraid?** Start from `docker/unraid/print-handy.xml`.

### Prefer not to use Docker?

There are two manual routes, both needing Python 3.11 or newer:

- The [standalone bot](docs/STANDALONE.md) runs on Discord — or on [Telegram](docs/TELEGRAM-BOT-SETUP.md) or [Matrix](docs/MATRIX-BOT-SETUP.md) instead — without Hermes.
- The [Hermes installation](docs/INSTALL.md) is for existing Hermes users on Discord.

One thing to know either way: slicing needs a compatible OrcaSlicer and the right printer, process and filament profiles. The Docker image bundles its slicers; a Python install alone does not provide one. The [configuration guide](docs/CONFIGURATION-YAML.md) covers the settings file when you need it — guided setup (above) is the friendlier place to start.

## Is my printer supported?

Print Handy groups printers by **how they connect**, because that is what decides what it can do with yours. There are 83 registered printer models today; this is the short version, and the full model-by-model list lives in the printer guides.

| Your printer connects by… | What you get | Which printers |
|---|---|---|
| **Moonraker (Klipper)** | The full flow: guided slicing, gated print start, progress, alerts and timelapses | Flashforge AD5X, AD5M and Creator 5 family running ZMod; Elegoo Neptune 4 family; Qidi Q1 Pro, X-Plus and X-Max lines; Creality K1 and Ender-3 V3 Klipper line; Anycubic Kobra 2 line on Moonraker-exposed firmware (see the Anycubic doc's stock-firmware caveat); Sovol SV01 through SV08, including the ACE, Plus, Zero, and M1D, on Klipper; Snapmaker U1 tool changer (published-spec support, not verified on hardware). **Any other Klipper printer** can use the generic Klipper profile. See [ZMod](docs/ZMOD.md) · [Elegoo](docs/ELEGOO.md) · [Qidi](docs/QIDI.md) · [Creality](docs/CREALITY.md) · [Anycubic](docs/ANYCUBIC.md) · [Sovol](docs/SOVOL.md) · [Snapmaker](docs/SNAPMAKER.md) |
| **Bambu Lab LAN** | Slicing, G-code checks and Bambu packaging, with opt-in LAN status, upload and gated print start | A1, A1 mini, P1P, P1S, P2S, X1, X1 Carbon, X1E, H2S, A2L and the dual-nozzle H2D, H2D Pro, H2C and X2D. See [Bambu Lab printers](docs/BAMBU-LAB.md) |
| **PrusaLink** | Slicing plus opt-in LAN status, upload and gated print start | MK4S, CORE One, XL and MINI+. See [Prusa printers](docs/PRUSA.md) |
| **Flashforge stock LAN** | Built-in profile over the printer's own LAN API — no Moonraker, no ZMod | AD5X on stock firmware. See [Stock Flashforge printers](docs/FLASHFORGE-STOCK.md) |
| **Elegoo SDCP** | SDCP LAN, opt-in | Centauri Carbon. See [Elegoo printers](docs/ELEGOO.md) |
| **OctoPrint (Marlin)** | A generic OctoPrint/Marlin profile, plus built-in presets with real dimensions | Ender-3, Ender-3 Pro, Ender-3 V2 and Ender-3 V3 SE presets, plus Anycubic i3 Mega S, Vyper, Chiron, 4Max Pro 2 and first-generation Kobra presets; Sovol SV01, SV01 Pro, SV02, SV03, SV04, SV05, SV06, and SV06 Plus on stock Marlin. Any other Marlin printer behind OctoPrint uses the generic profile. See [OctoPrint + Marlin](docs/OCTOPRINT-MARLIN.md) · [Sovol](docs/SOVOL.md) |

**Not listed?** A Klipper printer with Moonraker can use `klipper.generic`; a Marlin printer behind OctoPrint can use `octoprint.marlin`. Set your real bed size, height, nozzle and start/end macros first, because the placeholder numbers will not match your machine. Then open a [printer request](https://github.com/catduckgnaf/print-handy/issues/new?template=printer_report.yml).

**How tested is this?** Honestly: apart from one Bambu Lab A1 run, every profile above is covered by offline tests only — a generic profile is not a tuned or physically verified profile for your printer. If you ran the hardware checklist in the [printer support guide](docs/PRINTER-SUPPORT.md) on your printer, file a verification report through the same form and attach a redacted `/print-handy bundle`. Do the physical checks with the bed clear and a hand near the stop button.

## Which chat platform?

The same bot, with the same prompts and safety gates, runs on **Discord, Telegram or Matrix** — only the transport changes. Setup: [Discord](docs/DISCORD-BOT-SETUP.md) · [Telegram](docs/TELEGRAM-BOT-SETUP.md) · [Matrix](docs/MATRIX-BOT-SETUP.md).

- On Discord you can alternatively run Print Handy as a Hermes plugin instead of the standalone bot. Use only one mode with a given bot token.
- The Telegram and Matrix bots are pure bots with no LLM chat: send a message that isn't a command and you get the command list back. (Their roasts, camera checks and design still work — those use the vision and design endpoints, not chat.)

## What you can do

### With no LLM at all

Everything in this list is plain, deterministic code. No model endpoint is configured, and none is needed:

- **Prepare models.** MakerWorld, Printables, Thingiverse, MyMiniFactory, Creality Cloud, Thangs and Nexprint links, direct links to STL/3MF files on any public host, and STL/3MF uploads go through guided choices for colors and filament. MakerOnline links are inventory-only (the bot lists the files and names the download gap). Cults3D links stop with the named gap too — Cults' API never exposes a listing's files.
- **Slicing that respects geometry.** Every model is measured at full size first. Print Handy never silently shrinks a part: if the author's layout doesn't fit but every part fits one plate at 100% once moved or turned, it is rearranged and you are told so in one line; otherwise you choose — split it across plates at 100%, turn it on the bed or onto another face, or shrink it on purpose. A small STL gets the millimeters-or-inches question shown above, because STL files record no units. Copies are offered only where the G-code will actually contain them, multi-plate plans are tracked as one job, and each slice records its full recipe so a reprint is exact.
- **Manage filament.** `/spool` lists, changes, creates and syncs spools without moving the printer — the guided change is in the walkthrough above, and typing "add new filament" or "swap the filament" starts the same flow. A spool created in Spoolman gets offered for a slot. If a print pauses on filament runout, the spool question the bot posts has a **Cancel print** button, so you can always stop the print without answering the question first. And if Spoolman is down, slicing and printing carry on without it — spool tracking is skipped for that job, never a blocker.
- **Run approved jobs.** Confirm bed clearance, work through multi-plate plans with `/print-next`, and reprint archived jobs with `/print-again` — all behind the same guarded controls.
- **Start a screenless ZMod print.** On an AD5X with the screen off, including a HelixScreen setup, Print Handy starts the print from chat. That path is offline-tested; the first live print is still owed. See [AD5X best practices](docs/DEVICE-AD5X.md).
- **Care for the printer.** `/print-firmware` reports each component's current and available firmware version and applies one update at a time behind a confirmation, with guided manual updates for ZMod and stock firmware. `/print-calibration` runs bed mesh, PID, input shaper, screw and belt checks with the same careful pattern: current values first, a confirmation with the heat and motion warnings, the new values and the differences, and nothing saved until you accept. See [firmware updates](docs/FIRMWARE-UPDATE.md) and [printer calibration](docs/CALIBRATION.md).
- **Follow a print.** Start, pause, error and completion posts come back to your chat, with a timelapse per print (and an optional vertical 9:16 short). Every job is recorded.
- **Watch the camera live (Discord).** An optional live view inside a Discord voice channel, for checking on a print without leaving the chat. Nothing is recorded.
- **Get help in chat.** `/print-handy setup` walks a fresh install through setup; `/print-handy add-printer` adds another printer to a running install through the same guided pattern, with a live connection probe before anything is written; `/print-handy debugging` runs read-only, redacted checks; `/print-handy bundle` assembles a diagnostics bundle (secret values are never included) as a local file that only leaves the machine if you choose to attach it. See [guided setup and debugging](docs/GUIDED-SETUP-DEBUGGING.md) and [diagnostics](docs/DIAGNOSTICS.md).
- **Forgiving about typos.** `/print-lookup` finds "nozzel clog", a mistyped command gets a "Did you mean `/spool list`?" button, and `/print-again` with a half-remembered name offers a pick-list. No model needed, and a suggestion never runs anything on its own. An optional [semantic lookup](docs/SETTINGS.md#semantic-lookup-endpoint-print_fuzzy_url) adds meaning-level matches ("bed won't stick"), runnable on [Cloudflare's free tier](docs/CLOUDFLARE.md).
- **Use the optional Web UI.** An off-by-default local page for the same install: status and doctor checks, the settings picker, the read-only designs catalog, and a read-only sliced-layer view (colored by extrusion role, one layer at a time). It does not start, pause, or cancel a job. See the [Web UI guide](docs/WEBUI.md).

### With a model endpoint (the LLM features)

Point Print Handy at any OpenAI-compatible chat endpoint — a local model server, a hosted API, or Cloudflare Workers AI's free tier all fit the same settings ([Cloudflare guide](docs/CLOUDFLARE.md)) — and these switch on. Each is optional, and each fails soft: if the endpoint doesn't answer, the print flow carries on without it.

- **Chat in plain language (Discord).** Ask for things the way you'd say them, and Print Handy turns the reply into one of its own checked commands — the model can only ever pick from the same command list, never invent an action. Turn it off with `discord.chat_llm_enabled` and Discord becomes a pure bot too, while the endpoint stays available for design.
- **Camera watchdog.** While a print runs, Print Handy keeps an eye on the printer camera — at layer 2, layer 5 and then periodically — and a vision model classifies each frame. If it sees spaghetti, a detached part, an empty bed, a stalled print, or a frame it can't classify, it posts the photo with **Pause** and **It's fine** buttons. Tap Pause, and the paused post offers Resume and Cancel. The watchdog itself never touches the printer — it only alerts; every printer action is your tap. Tune how readily it alerts with `visual_watch.sensitivity` from 1 (quietest) to 5 (most alerts, default 3) — env `PRINT_HANDY__VISUAL_WATCH__SENSITIVITY` — or turn it off with `visual_watch.enabled`. It needs a camera snapshot and a vision endpoint (`services.vision_chat_url`) — or, with no endpoint at all, the built-in local failure detector (`vision.local_detector_enabled`), which judges frames on this machine and needs no LLM and no cloud service. With neither configured, camera checks stay off rather than guessing. See the [Docker guide](docs/DOCKER.md#camera-watch-visual_watch).
- **Design a part in chat.** `/print-design` (or `/design`) turns a plain-language description into an OpenSCAD part, shows you a preview with the real dimensions, and lets you ask for changes (up to 10 turns) before it joins the normal prepare-and-confirm flow. Works on Discord, Telegram and Matrix whenever a model endpoint is configured. See [AI design](docs/DESIGN.md).
- **The roast.** When printing begins, the print-started post can end with a one-line roast of your model (exactly one per print; completion posts never carry one). Roasts are on by default whenever an LLM/vision endpoint is available, on every platform, and `vision.roast_enabled` (env `PRINT_HANDY__VISION__ROAST_ENABLED`) turns them off without affecting camera checks.

Printer-specific capabilities and the full command list are in the [project reference](docs/PROJECT-REFERENCE.md).

## Please read: safety

Print Handy is careful, but it isn't a guarantee:

- A model that fits in a bounding box is not proof of a printable orientation. Check support needs, bed and brim clearance, profile compatibility and the generated G-code before physical use.
- Offline tests are not a live hardware test. Keep the confirmation and artifact guards enabled.
- A camera alert or classification is not a guarantee of safe unattended printing.
- Only use models you are allowed to download and process — and never attach private models, credentials or personal printer logs to a public issue without reviewing them first.

## Find the right guide

| I want to… | Go here |
| --- | --- |
| Install and configure Print Handy | [Documentation site](https://catduckgnaf.github.io/print-handy-docs/) · [Documentation home (source)](docs/README.md) |
| Run the Docker container | [Docker guide](docs/DOCKER.md) |
| Check whether my printer is supported, or add one | [Printer support](docs/PRINTER-SUPPORT.md) · [Printer profiles](docs/PRINTER-PROFILES.md) |
| Run Print Handy for two or more printers | [More than one printer](docs/MULTI-INSTANCE.md) |
| Set up the bot on Telegram or Matrix | [Telegram bot setup](docs/TELEGRAM-BOT-SETUP.md) · [Matrix bot setup](docs/MATRIX-BOT-SETUP.md) |
| Look up commands, settings and integrations | [Detailed project reference](docs/PROJECT-REFERENCE.md) |
| Report a problem or contribute | [Contribution guide](CONTRIBUTING.md) · [Issues](https://github.com/catduckgnaf/print-handy/issues) |
| See what shipped in each release | [GitHub releases](https://github.com/catduckgnaf/print-handy/releases) |
| Write or review release notes | [Release-note guide](docs/RELEASE-NOTES.md) · [Internal history](docs/INTERNAL_RELEASE_NOTES.md) |

Draft pull requests describe pending work, not installed or released capabilities.
