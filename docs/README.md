# Print Handy documentation

[Back to Print Handy](../README.md) · [Discord help](https://discord.gg/mT2Ht3nqZu) · [Report a bug or request a printer](https://github.com/catduckgnaf/print-handy/issues)

The README is the tour. This is where you come when the printer decides to have opinions. Pick what you're trying to do; you shouldn't have to read a novel to find the spool settings.

## Install Print Handy

There are **three main installation routes**. They all run Print Handy, not three different products.

| Your setup | Guide |
|---|---|
| A Linux machine with Docker | [Docker and Compose](DOCKER.md) |
| An Unraid server | [Unraid template](https://github.com/catduckgnaf/catduck-unraid-templates) · [Unraid-specific Docker settings](DOCKER.md#unraid-dockerman) |
| Home Assistant OS or Supervised | [Home Assistant add-on](HOME-ASSISTANT-OS.md) |

For any route, [choose your printer](PRINTER-PROFILES.md#find-your-printer), then connect the chat platform you actually want: [Discord](DISCORD-BOT-SETUP.md), [Telegram](TELEGRAM-BOT-SETUP.md) or [Matrix](MATRIX-BOT-SETUP.md). Or use the [local Web UI](WEBUI.md), including its browser chat and model workflow.

**Before exposing the Web UI:** follow the first-run login setup. Until access is configured, anyone who can reach the port can use the interface and its printer controls. Details are in the [Web UI guide](WEBUI.md).

## Start printing

- [Print a model and see the commands](PROJECT-REFERENCE.md): links, files, colors, copies and the usual questions.
- [Edit it](EDITING.md): rotate, resize, supports, geometry changes and what those commands actually do.
- [Multi-part jobs](PROJECTS.md): several files or plates, and how to print the next one.
- [Slicer warnings](SLICER-WARNINGS.md): what OrcaSlicer found and what Print Handy asks you to decide.
- [Spool and printer management](PROJECT-REFERENCE.md): filament inventory, slot matching, commands, print records.
- [More than one printer](DOCKER.md#more-than-one-printer): one runtime with a printer roster; [isolated instances](MULTI-INSTANCE.md) if that's what you need.
- [The jokes](STATUS-VOICE.md): status quips, copy-count surprises and how the roast system works. Yes, the docs actually explain the jokes.

## Cameras, design and upgrades

- [Pasta Cam / camera watchdog](DOCKER.md#camera-watch-visual_watch): which printer connections can be watched, camera requirements and alert settings.
- [Timelapses](DOCKER.md#timelapse-capture-go2rtc): frame capture, encoding and sharing.
- [Web UI, data and Pasta Wrapped](WEBUI.md): print history and optional recap tools.
- [AI design](DESIGN.md): describe or revise a part with OpenSCAD and a configured model endpoint. Ordinary printing does not need AI.
- [Firmware and component updates](FIRMWARE-UPDATE.md): which updates Print Handy can apply, which it can only report and which need a manual guide.
- [Calibration](CALIBRATION.md) · [Printer macros](MACROS.md) · [Accessories](ACCESSORIES.md).

## Printer support

**Start with the firmware, not the logo.** A profile passing automated tests is not the same as a verified real-machine print. [Support and verification levels](PRINTER-SUPPORT.md) spells out the difference, and [printer profiles](PRINTER-PROFILES.md) lists the exact model keys.

### FlashForge

- **Factory firmware:** [Stock FlashForge AD5X](FLASHFORGE-STOCK.md) (native LAN interface, not Moonraker).
- **ZMod:** [AD5X, AD5M, AD5M Pro, Creator 5, Creator 5 Pro](ZMOD.md) (Moonraker). [AD5X hardware notes](DEVICE-AD5X.md).
- **OpenCreator:** [Creator 5 and Creator 5 Pro](OPENCREATOR.md) (its own Moonraker profiles, distinct from ZMod).
- **Reforge:** [Upstream Creator 5 firmware](https://github.com/Klipper4FlashForge/firmware), acknowledged but **no separate Print Handy profile or verified integration yet**. [Request support](https://github.com/catduckgnaf/print-handy/issues/new?template=printer_report.yml).

These firmware projects are independent. Selecting a similarly named Creator 5 profile from the wrong firmware is not a clever compatibility trick.

### Other printer families

- [Bambu Lab](BAMBU-LAB.md) · [Prusa](PRUSA.md) · [Elegoo](ELEGOO.md).
- [Creality](CREALITY.md) · [Qidi](QIDI.md) · [Sovol](SOVOL.md) · [Anycubic](ANYCUBIC.md).
- [Snapmaker](SNAPMAKER.md) · [OctoPrint / Marlin](OCTOPRINT-MARLIN.md) · [Generic Klipper and unlisted printers](PRINTER-SUPPORT.md#your-printer-is-not-listed).
- [Printer hardware best practices](DEVICE-BEST-PRACTICES.md) · [Bambu LAN details](BAMBU-CONNECTIVITY.md).

## Something broke?

[Symptom-by-symptom troubleshooting](TROUBLESHOOTING.md) is the first stop. Then [diagnostics](DIAGNOSTICS.md), [guided setup and debugging](GUIDED-SETUP-DEBUGGING.md), [intake limits](INTAKE-LIMITS.md) or [mesh audit](MESH-AUDIT.md), depending on what went wrong. [Home Assistant panic recovery](home-assistant-panic.md) is there if you need it.

The `doctor` checks settings and services, not whether a printer on the other end of the network will physically behave. If you're stuck, ask in [Discord #support](https://discord.gg/mT2Ht3nqZu) or [open an issue](https://github.com/catduckgnaf/print-handy/issues). Include the printer model, firmware, sanitized logs and what actually happened.

## For tinkerers and contributors

- [All settings](SETTINGS.md) · [YAML configuration](CONFIGURATION-YAML.md) · [Listeners](LISTENERS.md) · [Component licenses and provenance](COMPONENTS.md).
- [Docker runtime details](DOCKER.md) · [Web UI internals](WEBUI.md) · [Cloudflare examples](CLOUDFLARE.md) · [Optional Flash Studio](FLASH-STUDIO.md).
- [Geometry and layout](GEOMETRY-LAYOUT.md) · [CAD split plans](CAD-SPLIT-PLANS.md) · [Projects](PROJECTS.md).
- **Advanced, non-primary setup:** [Native Python/systemd](INSTALL.md) and [standalone bot details](STANDALONE.md). These are two guides for the *same* advanced path, not fourth and fifth headline installers. Native installs must provide their own slicer and other dependencies.
- [Contributing](../CONTRIBUTING.md) · [Branding](BRANDING.md) · [Release notes guide](RELEASE-NOTES-GUIDE.md).

Old investigation reports, previous architecture decisions and superseded release material stay in [docs/history](history/). They are records, not instructions for a fresh install. The [Bambu A1 walkthrough](BAMBU-A1-DOCKER.md) and [simulated live demo](LIVE-DEMO.md) remain available as evidence and examples, not as separate installation choices.

These versioned docs are the source for the code in this repository. The [public documentation site](https://catduckgnaf.github.io/print-handy-docs/) syncs its copies separately, so a brand-new change here can appear there later. Keep docs and code in agreement; screenshots of a happy bot are not hardware verification.
