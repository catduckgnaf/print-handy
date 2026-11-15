<p align="center">
  <img src="assets/branding/logo-lockup-dark.png" alt="Print Handy logo" width="480">
</p>

# Print Handy

[![CI](https://github.com/catduckgnaf/print-handy/actions/workflows/ci.yml/badge.svg)](https://github.com/catduckgnaf/print-handy/actions/workflows/ci.yml)
[![Discord](https://img.shields.io/badge/Discord-Join%20the%20server-5865F2?logo=discord&logoColor=white)](https://discord.gg/mT2Ht3nqZu)

**Bambu Handy for every printer. Including Bambu printers. We're not petty.**

Find a model, drop its link or STL/3MF into Discord, Telegram, Matrix or the Web UI, and let Print Handy deal with the fiddly bits: colors, copies, bed fit, slicing, spools and print updates. It runs on your own hardware. There's no Print Handy cloud, no mandatory AI subscription, and considerably fewer reasons to move your laptop next to a printer.

It also has a personality problem. Your model might get roasted, your eleventh copy deserves a Spinal Tap reference, and Pasta Cam would very much like your printer to stop making spaghetti.

Current release: **2.3.4**

[Install](#get-started) · [See it work](#from-link-to-print) · [Printer support](#will-it-work-with-my-printer) · [Full docs](https://catduckgnaf.github.io/print-handy-docs/) · [Discord](https://discord.gg/mT2Ht3nqZu) · [Releases](https://github.com/catduckgnaf/print-handy/releases)

## The fun stuff (and the genuinely useful stuff)

- **Drop a link or a file.** MakerWorld, Printables, Thingiverse, MyMiniFactory, Creality Cloud, Thangs, Nexprint, direct STL/3MF links and file uploads feed the print workflow. Some sites or downloads have limits or need credentials; [model sources](docs/PROJECT-REFERENCE.md) explains the details.
- **Tell the slicer what you want.** Pick colors, filaments and copies. Rotate, resize, split across plates and adjust supports from chat; Print Handy checks bed fit before making you waste filament on a sculpture of disappointment. OrcaSlicer and OpenSCAD are built in, so there's nothing else to install. [Model editing](docs/EDITING.md) has the supported operations and their limits.
- **Spoolman without the spreadsheet cosplay.** Track your filament, match spools to printer slots and get prompted when a spool runs out. A bundled Spoolman instance is included in the container; you can also connect your own. [Spools and commands](docs/PROJECT-REFERENCE.md).
- **Pasta Cam (Camera watchdog).** Camera checks can flag spaghetti, detached parts and other suspicious print behavior, with alerts and controls. Coverage depends on printer family, camera setup and enabled monitoring. It's a watchdog, not a psychic. Tune it with `PRINT_HANDY__VISUAL_WATCH__SENSITIVITY` (1 to 5). [Camera watch and compatibility](docs/DOCKER.md#camera-watch-visual_watch).
- **Print updates with attitude.** Progress, pauses, errors, finish posts, optional timelapses, model roasts and non-AI status quips. Some printer connections monitor only jobs Print Handy started. [Status voice](docs/STATUS-VOICE.md) · [Timelapses](docs/DOCKER.md#timelapse-capture-go2rtc).
- **A real Web UI.** Set up printers, work through a model link or upload, use chat in your browser, browse slices and print history, and view the **Pasta Wrapped** recap. Some recap/video features depend on the records and options available. [Web UI guide](docs/WEBUI.md).
- **Firmware, without pretending every printer is the same.** Check firmware versions and available components, including ZMod and HelixScreen updates; eligible ZMod and vanilla Klipper printers can apply a reported component update with confirmation. Other firmware paths get guidance or read-only reporting. [Exactly what updates work](docs/FIRMWARE-UPDATE.md).
- **Optional AI, not mandatory AI.** Describe a part to the design tool, iterate on it or use supported natural-language editing. Normal printing, filament tracking, roasts and status messages don't need an LLM. AI features require an endpoint you configure and have platform-specific limitations. [Design guide](docs/DESIGN.md) · [Chat platforms](docs/STANDALONE.md).

- **Home Assistant.** The [Print Handy integration](https://github.com/catduckgnaf/ha-print-handy) puts every printer in Home Assistant with live status and controls.

One container runs locally; the chat provider you choose still carries messages and attachments through its own service. The local Web UI doesn't need a Discord account. Don't mistake *local-first* for *every message stays in your basement*.

## From link to print

**1. Paste a model link. Choose the colors.** The printer doesn't care whether your taste is good.

<img src="assets/screenshots/discord-01-link-and-colors.png" alt="Discord model-link intake and color selection" width="720">

**2. Pick the number of copies and review the slice choices.** One little dragon, eleven little dragons. We aren't judging. Much.

<img src="assets/screenshots/discord-02-copies-and-confirm.png" alt="Discord copy-count selection and slice or print choices" width="720">

**3. Watch it start, or watch it get roasted.** Sometimes both.

<img src="assets/screenshots/discord-03-print-start.png" alt="Discord print-start status with a one-line roast" width="720">

<details>
<summary><b>More screenshots: fit checks, spools, Pasta Cam and the finish line</b></summary>

1. **Bed fit, geometry and the occasional uncomfortable measurement.** ![Fit and geometry prompts](assets/screenshots/discord-08-fit-and-geometry.png)
2. **Finished print and timelapse.** ![Print completed](assets/screenshots/discord-04-print-complete.png)
3. **Pasta Cam asks whether that's a print or an entrée.** ![Camera alert](assets/screenshots/discord-05-camera-watchdog.png)
4. **Pause, runout and spool choices.** ![Pause and runout](assets/screenshots/discord-06-pause-and-runout.png)
5. **Changing a spool.** ![Spool change](assets/screenshots/discord-09-spool-change.png)
6. **A new Spoolman spool joins the party.** ![New spool](assets/screenshots/discord-07-new-spool.png)

These screenshots are **mockups based on real bot text and controls**, not proof of a hardware print. Their sources are documented in [assets/screenshots](assets/screenshots/README.md).

</details>

## Get started

Three ways in: Docker (right here), [Unraid](#unraid) or the [Home Assistant add-on](#home-assistant-add-on).

**Docker** runs on 64-bit Linux (amd64 or arm64) with Docker and the Compose plugin. This is the short version; [the Docker guide](docs/DOCKER.md) covers version pinning, networks, storage, upgrades and every optional service.

```bash
mkdir print-handy && cd print-handy
curl -fLO https://raw.githubusercontent.com/catduckgnaf/print-handy/2.3.4/docker-compose.yml
curl -fL https://raw.githubusercontent.com/catduckgnaf/print-handy/2.3.4/.env.example -o .env
```

Edit `.env` and fill in three things:

- `PRINT_HANDY_IMAGE`: the image to run, as a pinned digest (`ghcr.io/catduckgnaf/print-handy@sha256:...`) from [Pinning the image](docs/DOCKER.md#pinning-the-image). There is no default, and that is deliberate: a tag can move or be republished, so a default would let the image change under you.
- Your chat: `DISCORD_BOT_TOKEN` and `PRINT_HANDY__DISCORD__CHAT_ID` from [Discord setup](docs/DISCORD-BOT-SETUP.md) (about 15 minutes). Prefer [Telegram](docs/TELEGRAM-BOT-SETUP.md) or [Matrix](docs/MATRIX-BOT-SETUP.md)? Set `PRINT_HANDY__BOT__PLATFORM` and that platform's token instead. The Web UI works either way.
- Your printer: `PRINT_HANDY__SERVICES__MOONRAKER_URL` for a Klipper printer. Other families don't magically have Moonraker; their variables are in [Printer support](docs/PRINTER-SUPPORT.md).

```bash
docker compose up -d
docker compose ps        # wait for "healthy"
```

1. **Claim the Web UI.** Open `http://<your-server>:8088` and **set its login during the first-run walkthrough**, or record that you mean to leave it open. Until then, anyone who can reach that port can use its printer controls.
2. **Say hello.** Send `/print-help` in your configured chat, or use the Web UI.
3. **Check the install.** `docker compose exec combined python3 -m print_handy.doctor` checks the settings, the slicer and every configured service, and names whatever is wrong.

### Unraid

In **Apps > Settings**, add `https://github.com/catduckgnaf/catduck-unraid-templates` under **Use DockerHub / Custom repositories**, then search Apps for **Print Handy**. The template and the Docker image are the same Print Handy, not two competing projects. See the [Unraid template repository](https://github.com/catduckgnaf/catduck-unraid-templates) and [Docker guide](docs/DOCKER.md) for networking and persistence.

### Home Assistant add-on

In **Settings > Apps > App store > Repositories**, add `https://github.com/catduckgnaf/print-handy-haos-addon`. Install **Print Handy**, start it and open the Web UI. Set its login, then connect your printer and optional chat platform. The add-on runs the published container through Home Assistant, with its own configuration and backup handling. [Full add-on guide](docs/HOME-ASSISTANT-OS.md).

Want printer entities in Home Assistant too? That's the separate [Print Handy integration](https://github.com/catduckgnaf/ha-print-handy), installed through HACS.

## Will it work with my printer?

The question that matters is **which firmware and connection your printer actually runs**, not merely the brand on the front. A built-in profile means Print Handy has definitions and offline tests; it **doesn't** mean someone printed on that exact hardware. [Printer support](docs/PRINTER-SUPPORT.md) explains verification levels and how to request a new printer.

### FlashForge: choose the firmware first

| Firmware / route | Printers with Print Handy profiles | What connects | Guide |
|---|---|---|---|
| **Stock FlashForge** | AD5X | Factory LAN HTTP API; no ZMod required | [Stock FlashForge](docs/FLASHFORGE-STOCK.md) |
| **ZMod** | AD5X, AD5M, AD5M Pro, Creator 5, Creator 5 Pro | Moonraker through ZMod | [ZMod](docs/ZMOD.md) |
| **Forge-X** (1.4.2 beta) | AD5M, AD5M Pro | Moonraker on Forge-X's patched stock Klipper; not ZMod, never both | [Forge-X](docs/FORGEX.md) |
| **OpenCreator** | Creator 5, Creator 5 Pro | Kalico / Moonraker; separate firmware and profiles from ZMod | [OpenCreator](docs/OPENCREATOR.md) |
| **Reforge** | **No dedicated profile yet** | Independent Creator 5 / 5 Pro custom firmware; compatibility not verified here | [Reforge upstream](https://github.com/Klipper4FlashForge/firmware) · [Request printer support](https://github.com/catduckgnaf/print-handy/issues/new?template=printer_report.yml) |

**What has actually been checked?** The ZMod AD5X has limited on-hardware checks, but its [ZMod guide](docs/ZMOD.md) does not claim a verified Print Handy-started print. The stock AD5X LAN upload/start path is implemented but [not yet hardware-confirmed](docs/FLASHFORGE-STOCK.md). OpenCreator's Creator profiles are [tested against documentation and offline contracts, not a real printer](docs/OPENCREATOR.md). Reforge has no integration to verify yet. That's the useful difference between *profile exists* and *go on, bet your printer on it*.

A Creator 5 running OpenCreator is not running ZMod, and a stock AD5X is not secretly a Moonraker printer. Choose the matching [model key](docs/PRINTER-PROFILES.md#find-your-printer). The [ZMod AD5X hardware baseline](docs/DEVICE-AD5X.md#tested-baseline) covers specific checks; it does not imply every FlashForge combination has been printed successfully.

### Everybody else

| Connection family | Built-in families | Guide |
|---|---|---|
| **Klipper / Moonraker** | Creality, Qidi, Sovol, Elegoo Neptune 4, Anycubic Kobra 2 line, Snapmaker U1, generic Klipper | [Printer profiles](docs/PRINTER-PROFILES.md) |
| **Bambu Lab LAN** | A1, P1, X1, P2, H2 and related supported profiles | [Bambu Lab](docs/BAMBU-LAB.md) |
| **PrusaLink** | MK4S, CORE One, XL, MINI+ | [Prusa](docs/PRUSA.md) |
| **Elegoo SDCP** | Centauri Carbon | [Elegoo](docs/ELEGOO.md) |
| **OctoPrint / Marlin** | Ender, Anycubic and Sovol presets, plus a generic OctoPrint profile | [OctoPrint / Marlin](docs/OCTOPRINT-MARLIN.md) |

Don't see your printer? The [printer support guide](docs/PRINTER-SUPPORT.md#your-printer-is-not-listed) explains generic profiles and the [printer request form](https://github.com/catduckgnaf/print-handy/issues/new?template=printer_report.yml). No need to buy a new printer just to please the documentation.

## Docs, help and the people who made this possible

For the complete setup and command references, use the [documentation home](docs/README.md) or [documentation site](https://catduckgnaf.github.io/print-handy-docs/). For humans with problems, there's the [Discord server](https://discord.gg/mT2Ht3nqZu) and [GitHub issues](https://github.com/catduckgnaf/print-handy/issues). Code contributions go through [CONTRIBUTING.md](CONTRIBUTING.md).

### Thanks, seriously

The FlashForge community has done a remarkable amount of work convincing these printers to speak something other than corporate. Print Handy builds on those efforts; it isn't their firmware and doesn't claim to maintain it.

- **[ZMod](https://zmod.link)** for opening up the Adventurer and Creator printers through Klipper and Moonraker.
- **[OpenCreator](https://github.com/FlashForge-C5-Modding-Group/OpenCreator)** and the [FlashForge Creator 5 Modding Group](https://github.com/FlashForge-C5-Modding-Group) for their alternative Creator 5 firmware and documentation.
- **[Reforge / Klipper4FlashForge](https://github.com/Klipper4FlashForge/firmware)** for another independent Creator 5 custom firmware path. It's acknowledged here, **not** advertised as a tested Print Handy integration.
- **[OrcaSlicer](https://github.com/OrcaSlicer/OrcaSlicer)**, **[Spoolman](https://github.com/Donkie/Spoolman)**, **[Klipper](https://github.com/Klipper3d/klipper)**, **[Moonraker](https://github.com/Arksine/moonraker)**, **[OpenSCAD](https://github.com/openscad/openscad)** and **[go2rtc](https://github.com/AlexxIT/go2rtc)** for the tools that make slicing, spools, printer control, design and cameras possible.

Thanks also to everyone filing hardware reports, testing strange combinations, fixing bugs and contributing code. If your printer works because somebody spent a weekend deciphering a protocol, they deserve a credit and probably a nap. Third-party licenses and component details are in [Component provenance](docs/COMPONENTS.md).

*Not affiliated with Bambu Lab or the firmware projects above. We just think the good printer experience should be less exclusive.*

## License

Print Handy's **current code is licensed under [GNU Affero General Public License v3.0 only (AGPL-3.0-only)](LICENSE)**. See [NOTICE](NOTICE) and [third-party component licenses](docs/COMPONENTS.md) for applicable notices.
