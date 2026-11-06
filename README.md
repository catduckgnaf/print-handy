<p align="center">
  <img src="assets/branding/logo-lockup-dark.png" alt="Print Handy logo" width="480">
</p>

# Print Handy

[![CI](https://github.com/catduckgnaf/print-handy/actions/workflows/ci.yml/badge.svg)](https://github.com/catduckgnaf/print-handy/actions/workflows/ci.yml)

**Your slicer lives on a desk. You don’t.** Share a model link or file in Discord, Telegram, or Matrix. Choose filament, review the estimate, and approve the exact job before it reaches your printer.

Print Handy runs locally without a Print Handy cloud. Chat providers carry messages and files; model downloads and optional hosted AI use the network. AI is optional. Those plastic dragons do not download themselves.

[Get started](#get-started) · [See it in action](#see-it-in-action) · [Documentation](https://catduckgnaf.github.io/print-handy-docs/) · [Contribute](CONTRIBUTING.md) · [Releases](https://github.com/catduckgnaf/print-handy/releases)

*Not affiliated with Bambu Lab. We just liked their app enough to want it on every other printer.*

## How it works

1. **Share a model.** Drop a MakerWorld, Printables, Thingiverse, MyMiniFactory, Creality Cloud, Thangs or Nexprint link in the chat, a direct link to an STL or 3MF file (a Dropbox share link works too), or upload an STL or 3MF file. MakerOnline links are checked for their file inventory only; downloads are not supported. Cults3D links are recognized the same way, but Cults' API exposes listing details only, never the files, so intake names that gap instead.
2. **Make a few choices.** A model that comes as several files (a box, its lid, a latch) prints as one job; you only pick between versions of a part, such as a lid with or without a logo. Print Handy walks you through colors and filament, how many copies, and — if the model needs it — how it should fit on the bed. Then it slices the model for your printer.
3. **Check and confirm.** You see the result before anything happens. Only when you approve it does Print Handy send the job to a configured printer.
4. **Follow along.** Progress, alerts, the timelapse and a roast of your model choices come back to the same chat.

That's the whole idea: the convenience of printing from chat, with a human — you — making the final call every time.

![Print Handy in Discord: paste a model link, choose filament, review the estimate and approve the job](assets/screenshots/discord-01-link-and-colors.png)

## What it does

- **Prepare a print.** Share a supported model link or upload an STL/3MF; choose filament, copies and fit options.
- **Review before printing.** Inspect the estimate and approve the exact sliced job. Print Handy does not silently shrink a model.
- **Track filament.** Spool records and slot mapping are handled through the bundled Spoolman API or an existing Spoolman.
- **Follow the job.** Receive progress and completion messages in chat. Optional camera checks and timelapse features need compatible camera setup.
- **Use your connection path.** Supported integrations vary by printer family. Start with [Printer support](docs/PRINTER-SUPPORT.md); a supported profile is not proof of hardware verification.
- **Keep work local-first.** Print Handy runs on your machine without its own cloud. Chat providers carry sent messages/files; model downloads and optional hosted AI use the network.

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

## Get started

**Docker is the install path.** Choose the standard image **with AI extras** or the smaller **without AI** image in the [Docker guide](docs/DOCKER.md), then configure one of three chat sources: [Discord](docs/DISCORD-BOT-SETUP.md), [Telegram](docs/TELEGRAM-BOT-SETUP.md), or [Matrix](docs/MATRIX-BOT-SETUP.md). The optional [Web UI](docs/WEBUI.md) provides local status and settings without a chat platform. On Unraid, use the [template](docker/unraid/print-handy.xml).

Pin a verified image digest rather than `:latest` or `:develop`. The [docs home](docs/README.md) links setup, printer, and troubleshooting guides.

**You need** a 64-bit Debian or Ubuntu machine, or a Raspberry Pi 4 or 5 with 64-bit Raspberry Pi OS, for the Docker route. The image (`ghcr.io/catduckgnaf/print-handy`) is published for `linux/amd64` and `linux/arm64`.

### What's in the box

Everything below is bundled in the one published Docker image — there is nothing else to install:

| Part | What it does |
|---|---|
| Chat bot | Your interface: links, uploads, guided slicing, spool commands and print confirmation |
| Printer listener | One per printer, matched to how that printer connects. It follows the print and reports progress, alerts and timelapses to the chat |
| Spoolman | The filament and spool record keeper, bundled as an API only — Print Handy is its interface, so there is no separate Spoolman web UI to run. Already have your own Spoolman? The [Docker guide](docs/DOCKER.md) covers pointing Print Handy at it instead |
| OrcaSlicer | The slicer: one bundled build (v2.5.0-rc) |
| OpenSCAD | CAD splitting, and the engine behind designing parts in chat |
| ffmpeg and go2rtc | Timelapse capture and assembly, and the camera fan-out the watchdog and live view share |

If a required part stops, the container stops so Docker can restart it, and the health check covers the bot, the listeners and Spoolman.

### The one command in this README: the doctor

However you installed it, one check is worth knowing by heart:

```bash
python3 -m print_handy.doctor
```

The doctor checks that the machine, the settings, the slicer and the configured services (Moonraker, Spoolman, the bot) are all in shape, and names what is wrong when something is not. It contacts the services you configured, so run it where that is expected. A clean doctor is a healthy install, not proof a print will succeed; the [printer support guide](docs/PRINTER-SUPPORT.md) has the physical checklist for that.

## Is my printer supported?

Print Handy groups printers by **how they connect**, because that is what decides what it can do with yours. There are 84 registered printer models today; this is the short version, and the full model-by-model list lives in the printer guides.

| Your printer connects by… | What you get | Which printers |
|---|---|---|
| **Moonraker (Klipper)** | The full flow: guided slicing, gated print start, progress, alerts and timelapses | Flashforge AD5X, AD5M, AD5M Pro, Creator 5 and Creator 5 Pro running ZMod; Elegoo Neptune 4 family; Qidi Q1 Pro, X-Plus and X-Max lines; Creality K1 and Ender-3 V3 Klipper line; Anycubic Kobra 2 line on Moonraker-exposed firmware (see the Anycubic doc's stock-firmware caveat); Sovol SV01 through SV08, including the ACE, Plus, Zero, and M1D, on Klipper; Snapmaker U1 tool changer (published-spec support, not verified on hardware). **Any other Klipper printer** can use the generic Klipper profile. See [ZMod](docs/ZMOD.md) · [Elegoo](docs/ELEGOO.md) · [Qidi](docs/QIDI.md) · [Creality](docs/CREALITY.md) · [Anycubic](docs/ANYCUBIC.md) · [Sovol](docs/SOVOL.md) · [Snapmaker](docs/SNAPMAKER.md) |
| **Bambu Lab LAN** | Slicing, G-code checks and Bambu packaging, with opt-in LAN status, upload and gated print start | A1, A1 mini, P1P, P1S, P2S, X1, X1 Carbon, X1E, H2S, A2L and the dual-nozzle H2D, H2D Pro, H2C and X2D. See [Bambu Lab printers](docs/BAMBU-LAB.md) |
| **PrusaLink** | Slicing plus opt-in LAN status, upload and gated print start | MK4S, CORE One, XL and MINI+. See [Prusa printers](docs/PRUSA.md) |
| **Flashforge stock LAN** | Built-in profile over the printer's own LAN API — no Moonraker, no ZMod | Every Flashforge model Print Handy knows is listed here, with the route that actually applies to it: **AD5X** has the built-in stock-firmware profile. **AD5M, AD5M Pro, Creator 5 and Creator 5 Pro** have no stock-firmware profile yet; those printers are supported on ZMod (the Moonraker row above). See [Stock Flashforge printers](docs/FLASHFORGE-STOCK.md) |
| **Elegoo SDCP** | SDCP LAN, opt-in | Centauri Carbon. See [Elegoo printers](docs/ELEGOO.md) |
| **OctoPrint (Marlin)** | A generic OctoPrint/Marlin profile, plus built-in presets with real dimensions | Ender-3, Ender-3 Pro, Ender-3 V2 and Ender-3 V3 SE presets, plus Anycubic i3 Mega S, Vyper, Chiron, 4Max Pro 2 and first-generation Kobra presets; Sovol SV01, SV01 Pro, SV02, SV03, SV04, SV05, SV06, and SV06 Plus on stock Marlin. Any other Marlin printer behind OctoPrint uses the generic profile. See [OctoPrint + Marlin](docs/OCTOPRINT-MARLIN.md) · [Sovol](docs/SOVOL.md) |

**Not listed?** A Klipper printer with Moonraker can use `klipper.generic`; a Marlin printer behind OctoPrint can use `octoprint.marlin`. The [printer support guide](docs/PRINTER-SUPPORT.md) covers how to set one up with your real machine numbers (the placeholder numbers will not match your machine) and how to open a [printer request](https://github.com/catduckgnaf/print-handy/issues/new?template=printer_report.yml).

**How tested is this?** Extensively, and the coverage is widest exactly where people print most. The automated suite carries more than 5,800 tests, with dedicated per-family suites, recorded fixtures and golden slicer inputs for the ZMod Flashforge line (the AD5X above all), stock Flashforge, Klipper in general and the Klipper vendor lines (Sovol, Elegoo, Creality, Qidi, Anycubic, Snapmaker), Marlin behind OctoPrint, Bambu Lab LAN, PrusaLink and Elegoo SDCP — plus a shared adapter contract that runs against every registered model, so a new printer inherits hundreds of checks instead of starting from zero. The AD5X on ZMod is also the author's own daily printer, and its tested baseline is written down in the [AD5X best practices](docs/DEVICE-AD5X.md#tested-baseline); a full Bambu Lab A1 print over LAN is recorded too. Two honest limits remain: extensive testing of a family's code path is not the same as your exact model printing on your bench, and a generic profile is not a tuned profile for your printer. If you run the hardware checklist in the [printer support guide](docs/PRINTER-SUPPORT.md) on your printer, file a verification report through the same form; that is how a model graduates from tested to verified on hardware. Do the physical checks with the bed clear and a hand near the stop button.

## Which chat platform?

The same bot, with the same prompts and safety gates, runs on **Discord, Telegram or Matrix** — only the transport changes. Setup: [Discord](docs/DISCORD-BOT-SETUP.md) · [Telegram](docs/TELEGRAM-BOT-SETUP.md) · [Matrix](docs/MATRIX-BOT-SETUP.md).

- On Discord you can alternatively run Print Handy as a Hermes plugin instead of the standalone bot. Use only one mode with a given bot token.
- The Telegram and Matrix bots are pure bots with no LLM chat: send a message that isn't a command and you get the command list back. (Their roasts, camera checks and design still work — those use the vision and design endpoints, not chat.)
- Discord is a choice, not a requirement: the same local-first install serves Telegram or Matrix instead, and the doctor and the local Web UI need no chat platform at all.

## What you can do

### With no LLM at all

Everything in this list is plain, deterministic code, running locally. No model endpoint is configured, and none is needed. This is the default shape of Print Handy, not a reduced mode:

- **Prepare models.** MakerWorld, Printables, Thingiverse, MyMiniFactory, Creality Cloud, Thangs and Nexprint links, direct links to STL/3MF files on any public host, and STL/3MF uploads go through guided choices for colors and filament. MakerOnline links are inventory-only (the bot lists the files and names the download gap). Cults3D links stop with the named gap too — Cults' API never exposes a listing's files.
- **Slicing that respects geometry.** Every model is measured at full size first. Print Handy never silently shrinks a part: if the author's layout doesn't fit but every part fits one plate at 100% once moved or turned, it is rearranged and you are told so in one line; otherwise you choose — split it across plates at 100%, turn it on the bed or onto another face, or shrink it on purpose. A small STL gets the millimeters-or-inches question shown above, because STL files record no units. Copies are offered only where the G-code will actually contain them, multi-plate plans are tracked as one job, and each slice records its full recipe so a reprint is exact.
- **Manage filament.** `/spool` lists, changes, creates and syncs spools without moving the printer — the guided change is in the walkthrough above, and typing "add new filament" or "swap the filament" starts the same flow. A spool created in Spoolman gets offered for a slot. If a print pauses on filament runout, the spool question the bot posts has a **Cancel print** button, so you can always stop the print without answering the question first. And if Spoolman is down, slicing and printing carry on without it — spool tracking is skipped for that job, never a blocker.
- **Run approved jobs.** Confirm bed clearance, work through multi-plate plans with `/print-next`, and reprint archived jobs with `/print-again` — all behind the same guarded controls.
- **Start a screenless ZMod print.** On an AD5X with the screen off, including a HelixScreen setup, Print Handy starts the print from chat using the same start path as ZMod's print-file macro. Issues #842 and #843 were closed after the first live print was reported; see [AD5X best practices](docs/DEVICE-AD5X.md) for scope and verification limits.
- **Care for the printer.** `/print-firmware` reports each component's current and available firmware version and applies one update at a time behind a confirmation, with guided manual updates for ZMod and stock firmware. `/print-calibration` runs bed mesh, PID, input shaper, screw and belt checks with the same careful pattern: current values first, a confirmation with the heat and motion warnings, the new values and the differences, and nothing saved until you accept. See [firmware updates](docs/FIRMWARE-UPDATE.md) and [printer calibration](docs/CALIBRATION.md).
- **Follow a print.** Start, pause, error and completion posts come back to your chat, with a timelapse per print (and an optional vertical 9:16 short). Every job is recorded.
- **Watch the camera live (Discord).** An optional live view inside a Discord voice channel, for checking on a print without leaving the chat. Nothing is recorded.
- **Get help in chat.** `/print-handy setup` walks a fresh install through setup; `/print-handy add-printer` adds another printer to a running install through the same guided pattern, with a live connection probe before anything is written; `/print-handy debugging` runs read-only, redacted checks; `/print-handy bundle` assembles a diagnostics bundle (secret values are never included) as a local file that only leaves the machine if you choose to attach it. See [guided setup and debugging](docs/GUIDED-SETUP-DEBUGGING.md) and [diagnostics](docs/DIAGNOSTICS.md).
- **Forgiving about typos.** Chat understands everyday misspellings too: "seperate the bed and nozzle", "do some maintance", "shrnik it by 20%", "can you add supprots", "callibrate the bed". A correction never invents an action (you still get the usual confirmation), and a word that could be two real words ("brim" or "trim") becomes a pick list. Numbers, units, links, file names and mentions are left alone. `/print-lookup` finds "nozzel clog", a mistyped command gets a "Did you mean `/spool list`?" button, and `/print-again` with a half-remembered name offers a pick-list. No model needed, and a suggestion never runs anything on its own. An optional [semantic lookup](docs/SETTINGS.md#semantic-lookup-endpoint-print_fuzzy_url) adds meaning-level matches ("bed won't stick"), runnable on [Cloudflare's free tier](docs/CLOUDFLARE.md).
- **Use the optional Web UI.** An off-by-default local page for the same install: a first-run walkthrough, status and doctor checks, the settings picker, spool management, the read-only designs catalog, and a read-only sliced-layer view (colored by extrusion role, one layer at a time). Printer controls (pause, resume, cancel, target temperatures) live there too and stay off unless the operator turns them on. See the [Web UI guide](docs/WEBUI.md).

### With a model endpoint (the LLM features)

Point Print Handy at any OpenAI-compatible chat endpoint — a local model server, a hosted API, or Cloudflare Workers AI's free tier all fit the same settings ([Cloudflare guide](docs/CLOUDFLARE.md)) — and these switch on. Each is optional, and each fails soft: if the endpoint doesn't answer, the print flow carries on without it.

- **Chat in plain language (Discord).** Ask for things the way you'd say them, and Print Handy turns the reply into one of its own checked commands — the model can only ever pick from the same command list, never invent an action. Turn it off with `discord.chat_llm_enabled` and Discord becomes a pure bot too, while the endpoint stays available for design.
- **Camera watchdog.** Optional camera checks post alerts. `visual_watch.auto_pause_early` is a separate opt-in that can pause the printer after suspicious early-layer checks; it is off by default. See the [Docker guide](docs/DOCKER.md#camera-watch-visual_watch) for setup. Camera analysis is not a guarantee of safe unattended printing.
- **Design a part in chat.** `/print-design` (or `/design`) turns a plain-language description into an OpenSCAD part, shows you a preview with the real dimensions, and lets you ask for changes (up to 10 turns) before it joins the normal prepare-and-confirm flow. Works on Discord, Telegram and Matrix whenever a model endpoint is configured. See [AI design](docs/DESIGN.md).
- **The roast.** When printing begins, the print-started post can end with a one-line roast of your model (exactly one per print; completion posts never carry one). Roasts are on by default whenever an LLM/vision endpoint is available, on every platform, and `vision.roast_enabled` (env `PRINT_HANDY__VISION__ROAST_ENABLED`) turns them off without affecting camera checks.

Printer-specific capabilities and the full command list are in the [project reference](docs/PROJECT-REFERENCE.md).

## Please read: safety

Print Handy is careful, but it isn't a guarantee:

- A model that fits in a bounding box is not proof of a printable orientation. Check support needs, bed and brim clearance, profile compatibility and the generated G-code before physical use.
- Tests, however extensive, are not a live hardware test of your printer. Keep the confirmation and artifact guards enabled.
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
