<p align="center">
  <img src="assets/branding/logo-lockup-dark.png" alt="Print Handy logo" width="480">
</p>

# Print Handy

[![CI](https://github.com/catduckgnaf/print-handy/actions/workflows/ci.yml/badge.svg)](https://github.com/catduckgnaf/print-handy/actions/workflows/ci.yml)
[![Discord](https://img.shields.io/badge/Discord-Join%20the%20server-5865F2?logo=discord&logoColor=white)](https://discord.gg/mT2Ht3nqZu)

**Bambu Handy for every printer.** Paste a model link into Discord, Telegram or Matrix, approve the slice, and go do something else. Print Handy slices it for your printer, matches your spools, watches the camera for spaghetti, and roasts your model when it finishes.

- **Runs on your machine.** One Docker container on your network. No account, no Print Handy cloud.
- **No AI required.** Slicing, spools, printing and the roast are plain local code. AI features are optional add-ons.
- **Nothing prints until you tap approve.** Every job waits for a human.

Current release: **2.3.3**

[Get started](#get-started) · [Documentation](https://catduckgnaf.github.io/print-handy-docs/) · [Discord server](https://discord.gg/mT2Ht3nqZu) · [Releases](https://github.com/catduckgnaf/print-handy/releases) · [Contribute](CONTRIBUTING.md)

## See it in action

**Drop a link, pick your colors.**

<img src="assets/screenshots/discord-01-link-and-colors.png" alt="Print Handy in Discord: paste a model link, the bot checks the model, then asks which color each part prints in" width="720">

**Choose how many copies, then confirm. Nothing prints until you do.**

<img src="assets/screenshots/discord-02-copies-and-confirm.png" alt="Print Handy in Discord: choose the number of copies, then confirm the estimate with Slice and print now, Slice only, Stop with colors saved or Cancel print request" width="720">

**It prints, and it roasts you a little.**

<img src="assets/screenshots/discord-03-print-start.png" alt="Print Handy in Discord: the print started post, ending with a one-line roast of the model" width="720">

<details>
<summary><b>More: fit, spools, the camera watchdog and the finish</b></summary>

1. **If it doesn't fit, you choose how.** Models are measured at full size and never silently shrunk: split across plates, turn it, shrink it on purpose, or cancel. ![The fit question and the units question](assets/screenshots/discord-08-fit-and-geometry.png)
2. **It finishes, with a timelapse.** ![Print complete post with timelapse](assets/screenshots/discord-04-print-complete.png)
3. **The camera watchdog spots trouble.** ![Camera alert with Pause, It's fine and Stop asking for this print](assets/screenshots/discord-05-camera-watchdog.png)
4. **Pause, runout and the spool question.** ![Paused post and runout spool question](assets/screenshots/discord-06-pause-and-runout.png)
5. **Change a spool, guided.** ![Changing a spool, guided](assets/screenshots/discord-09-spool-change.png)
6. **A new spool in Spoolman gets offered for a slot.** ![New spool prompt and load flow](assets/screenshots/discord-07-new-spool.png)

These are mockups built from the bot's real message text and buttons, with placeholder names. Sources for every line are in [assets/screenshots](assets/screenshots/README.md).

</details>

## Get started

You need a 64-bit Linux machine (Debian, Ubuntu, or a Raspberry Pi 4 or 5 on 64-bit Raspberry Pi OS) with Docker and the Compose plugin. The image is published for `linux/amd64` and `linux/arm64`.

```bash
mkdir print-handy && cd print-handy
curl -fLO https://raw.githubusercontent.com/catduckgnaf/print-handy/2.3.3/docker-compose.yml
curl -fL https://raw.githubusercontent.com/catduckgnaf/print-handy/2.3.3/.env.example -o .env
```

Edit `.env` and fill in three things:

- `PRINT_HANDY_IMAGE`: the image to run, as a pinned digest (`ghcr.io/catduckgnaf/print-handy@sha256:...`) from [Pinning the image](docs/DOCKER.md#pinning-the-image). There is no default, and that is deliberate: a tag can move or be republished, so a default would let the image change under you.
- `DISCORD_BOT_TOKEN` and `PRINT_HANDY__DISCORD__CHAT_ID`: the bot and the channel it serves. [Discord bot setup](docs/DISCORD-BOT-SETUP.md) takes about 15 minutes. For [Telegram](docs/TELEGRAM-BOT-SETUP.md) or [Matrix](docs/MATRIX-BOT-SETUP.md), set `PRINT_HANDY__BOT__PLATFORM` and that platform's token instead.
- `PRINT_HANDY__SERVICES__MOONRAKER_URL`: your printer's address, for example `http://192.0.2.50:7125` on Klipper. Other printer families use their own variables, named in the [printer guides](docs/PRINTER-SUPPORT.md).

Then start it:

```bash
docker compose up -d
docker compose ps        # wait for "healthy"
```

1. **Claim the Web UI.** Open `http://<this-machine>:8088` and finish the walkthrough's Login step: set a password, or record that you mean to leave it open. Until then, anyone who can reach port 8088 can use it, printing and arming controls included.
2. **Say hello.** Send `/print-help` in your print channel. When the bot answers with its command list, paste a model link.
3. **Check the install.** `python3 -m print_handy.doctor` checks the settings, the slicer and every configured service, and names whatever is wrong.

The first start creates `./data` for you. Settings, upgrades and the full variable list are in the [Docker guide](docs/DOCKER.md).

### Other ways to install

- **Unraid:** add `https://github.com/catduckgnaf/catduck-unraid-templates` under **Apps > Settings > Use DockerHub / Custom repositories**, then search Apps for `print-handy`. See the [Docker guide](docs/DOCKER.md).
- **Home Assistant OS:** add `https://github.com/catduckgnaf/print-handy-haos-addon` under **Settings > Apps > App store > Repositories** and install **Print Handy**. See the [Home Assistant OS guide](docs/HOME-ASSISTANT-OS.md).
- **No Docker:** a [native install](docs/INSTALL.md) or the [standalone bot](docs/STANDALONE.md).

## Is my printer supported?

Print Handy knows 84 printer models, grouped by how they connect. Any Klipper printer with Moonraker can use the generic Klipper profile, and any Marlin printer behind OctoPrint can use the generic OctoPrint profile.

| Connects by | What you get | Printers |
|---|---|---|
| **Moonraker (Klipper)** | The full flow: slicing, gated print start, progress, alerts, timelapses | Flashforge AD5X, AD5M and Creator 5 lines on [ZMod](docs/ZMOD.md), [Elegoo](docs/ELEGOO.md) Neptune 4, [Qidi](docs/QIDI.md), [Creality](docs/CREALITY.md) K1 and Ender-3 V3, [Anycubic](docs/ANYCUBIC.md) Kobra 2, [Sovol](docs/SOVOL.md), [Snapmaker](docs/SNAPMAKER.md) U1, and any other Klipper printer |
| **Bambu Lab LAN** | Slicing and packaging, with opt-in LAN status, upload and gated start | A1, P1, P2S, X1, H2 and X2D lines. See [Bambu Lab printers](docs/BAMBU-LAB.md) |
| **PrusaLink** | Slicing, with opt-in LAN status, upload and gated start | MK4S, CORE One, XL, MINI+. See [Prusa printers](docs/PRUSA.md) |
| **Flashforge stock LAN** | The printer's own LAN API, no ZMod needed | AD5X. See [Stock Flashforge printers](docs/FLASHFORGE-STOCK.md) |
| **Elegoo SDCP** | SDCP LAN, opt-in | Centauri Carbon. See [Elegoo printers](docs/ELEGOO.md) |
| **OctoPrint (Marlin)** | A generic Marlin profile plus built-in presets | Creality Ender-3, Anycubic and Sovol Marlin presets, and any other Marlin printer. See [OctoPrint + Marlin](docs/OCTOPRINT-MARLIN.md) |

The model-by-model list, how to set up an unlisted printer, and how to file a [printer request](https://github.com/catduckgnaf/print-handy/issues/new?template=printer_report.yml) are in the [printer support guide](docs/PRINTER-SUPPORT.md). Thousands of automated tests cover every printer family, and the AD5X on ZMod is the author's daily printer ([tested baseline](docs/DEVICE-AD5X.md#tested-baseline)). Tests are still not your printer on your bench, so watch the first print.

## What it does

**Out of the box, with no AI:**

- **Prepare models** from MakerWorld, Printables, Thingiverse, MyMiniFactory, Creality Cloud, Thangs, Nexprint, any direct STL or 3MF link, or an upload, with guided colors, filament, copies and fit.
- **Slice without surprises** with one bundled build (v2.5.0) of OrcaSlicer, full-size measurement first, and an exact recipe saved for every reprint.
- **Track filament** with the bundled Spoolman: `/spool` lists, changes and creates spools, and runout asks which spool is next.
- **Follow every print** with start, pause, error and completion posts, a timelapse, and the roast.
- **Care for the printer** with guided firmware updates and calibration (`/print-firmware`, `/print-calibration`), each behind a confirmation.
- **Use the local Web UI** for status, settings, spools, a sliced-layer view, and a yearly print recap.
- **Talk to it by name.** "@Print Handy spool list" works like `/spool list`, and everyday typos still land on the right command.

**With an optional model endpoint** (a local server, a hosted API, or [Cloudflare's free tier](docs/CLOUDFLARE.md)):

- **Camera watchdog.** A vision check during the print flags spaghetti, a detached part or a stalled print, and posts the photo with **Pause** and **It's fine** buttons. A built-in local detector works with no AI at all. Tune it with `PRINT_HANDY__VISUAL_WATCH__SENSITIVITY` (1 to 5). See [Camera watch](docs/DOCKER.md#camera-watch-visual_watch).
- **Design a part in chat** with `/print-design`: describe it, check the preview, refine it, print it. See [AI design](docs/DESIGN.md).
- **Chat in plain language** on Discord (opt-in). The model can only pick from Print Handy's own checked commands.

Every AI feature fails soft: if the endpoint is down, printing carries on. The full command list is in the [project reference](docs/PROJECT-REFERENCE.md).

**Home Assistant:** the [Print Handy integration](https://github.com/catduckgnaf/ha-print-handy) puts every printer on your roster in Home Assistant, with live status and controls.

## Safety

Print Handy is careful, but it is not a guarantee. Check supports, bed clearance and the G-code before a first print, keep the confirmation guards on, and don't treat a camera check as permission to print unattended. Only print models you are allowed to download, and review logs before attaching them to a public issue.

## Find the right guide

| I want to… | Go here |
|---|---|
| Install and configure | [Documentation site](https://catduckgnaf.github.io/print-handy-docs/) · [Docs source](docs/README.md) |
| Run the Docker container | [Docker guide](docs/DOCKER.md) |
| Check or add a printer | [Printer support](docs/PRINTER-SUPPORT.md) · [Printer profiles](docs/PRINTER-PROFILES.md) |
| Run two or more printers | [More than one printer](docs/MULTI-INSTANCE.md) |
| Use the Web UI | [Web UI guide](docs/WEBUI.md) |
| Look up commands and settings | [Project reference](docs/PROJECT-REFERENCE.md) · [Settings](docs/SETTINGS.md) |
| Get help | [Discord server](https://discord.gg/mT2Ht3nqZu) (#support) |
| Report a bug or contribute | [Issues](https://github.com/catduckgnaf/print-handy/issues) · [Contribution guide](CONTRIBUTING.md) |
| See what shipped | [Releases](https://github.com/catduckgnaf/print-handy/releases) · [Release-note guide](docs/RELEASE-NOTES-GUIDE.md) |

The Discord server is the community home: ask in **#support**, talk printers in **#printers**, and watch **#updates** for releases. Bugs and feature requests belong in [Issues](https://github.com/catduckgnaf/print-handy/issues).

*Not affiliated with Bambu Lab. We just liked their app enough to want it on every other printer.*

## License

Print Handy is licensed under the [GNU Affero General Public License v3.0 only](LICENSE) (AGPL-3.0-only). Version 2.2.7 and earlier were released under the MIT License; [NOTICE](NOTICE) keeps those terms for the code carried forward. The third-party components the image and the wheel ship, and their licenses, are listed in [docs/COMPONENTS.md](docs/COMPONENTS.md).
