# Print Handy documentation

[Project overview](../README.md) · [Contributing](../CONTRIBUTING.md)

Find the task you came to do. Print Handy runs locally, with no Print Handy cloud. Chat providers carry messages and files you send; model downloads and optional hosted AI use the network too. Core print flows do not require an AI model endpoint.

## Start here

- [Docker](DOCKER.md): recommended install. Follow the quick start, configure a printer and chat platform, then check setup.
- [Standalone bot](STANDALONE.md): run without Docker. Choose [Discord](DISCORD-BOT-SETUP.md), [Telegram](TELEGRAM-BOT-SETUP.md) or [Matrix](MATRIX-BOT-SETUP.md).
- [Native installation](INSTALL.md): install Print Handy without Docker (Python + systemd).
- [Web UI](WEBUI.md): local status, setup and settings interface, on by default. It serves openly on your network until you set a login in its walkthrough.
- [Live demo](LIVE-DEMO.md): static, simulated Discord-style chat demo in the browser. No install, no account, nothing prints.
- [Bambu A1 Docker walkthrough](BAMBU-A1-DOCKER.md): hardware-specific steps and verification limits.

## Everyday printing

- [Project reference](PROJECT-REFERENCE.md): commands and everyday workflows.
- [Multi-part projects](PROJECTS.md): upload several STL/3MF files together, trace project/plate JSON IDs and print the next plate.
- [Settings](SETTINGS.md) · [YAML configuration](CONFIGURATION-YAML.md): settings, precedence and environment overrides.
- [Slicer warnings](SLICER-WARNINGS.md): OrcaSlicer warnings after slicing.
- [Firmware updates](FIRMWARE-UPDATE.md) · [Calibration](CALIBRATION.md) · [Printer macros](MACROS.md).
- [Design a part](DESIGN.md): optional AI-assisted OpenSCAD design. Review the result before the usual approval flow.
- [Edit a prepared model](EDITING.md): confirmed edit operations and limits. OrcaSlicer warning behavior is covered in [Slicer warnings](SLICER-WARNINGS.md).
- [Printer profiles](PRINTER-PROFILES.md): supported delivery methods and verification limits.
- [More than one printer](DOCKER.md#more-than-one-printer): one instance serves a whole printer roster — declare every printer in one config file; the full roster reference is the [Printers list](SETTINGS.md#printers-list). Running [separate instances](MULTI-INSTANCE.md) stays available when you want hard process isolation.

## Printer guides

Start with [Printer support](PRINTER-SUPPORT.md) for what support means, connection paths and the hardware checklist. A registered model or passing tests do not prove your physical printer has been verified.

- [ZMod](ZMOD.md) · [Stock Flashforge](FLASHFORGE-STOCK.md) · [AD5X best practices](DEVICE-AD5X.md)
- [Bambu Lab](BAMBU-LAB.md) · [Bambu connectivity details](BAMBU-CONNECTIVITY.md) · [Prusa](PRUSA.md) · [Elegoo](ELEGOO.md)
- [Creality](CREALITY.md) · [Qidi](QIDI.md) · [Sovol](SOVOL.md)
- [Anycubic](ANYCUBIC.md) · [Snapmaker](SNAPMAKER.md) · [OctoPrint and Marlin](OCTOPRINT-MARLIN.md)
- [Device best practices](DEVICE-BEST-PRACTICES.md) · [AD5X integration contract](AD5X-CONTRACT.md)

## Troubleshooting

- Stuck and want a person? Ask in the [Print Handy Discord server](https://discord.gg/mT2Ht3nqZu), in the #support channel — or file an [issue](https://github.com/catduckgnaf/print-handy/issues) when it is a bug that needs tracking.
- [Home Assistant panic recovery](home-assistant-panic.md) · [Troubleshooting by symptom](TROUBLESHOOTING.md) · [Guided setup and debugging](GUIDED-SETUP-DEBUGGING.md) · [Diagnostics](DIAGNOSTICS.md)
- [Intake limits](INTAKE-LIMITS.md) · [Mesh audit](MESH-AUDIT.md)
- The `doctor` checks configuration and configured services. It may contact Moonraker or Spoolman; run it only where authorized. A clean check does not prove physical readiness.

## Advanced setup

- [Docker details](DOCKER.md): image pinning, storage, optional camera features, timelapses, upgrades and rollback.
- [Web UI details](WEBUI.md): access, settings behavior, optional controls and limitations.
- [Multi-instance setup](MULTI-INSTANCE.md) · [Listeners](LISTENERS.md) · [Component provenance](COMPONENTS.md)
- [Cloudflare examples](CLOUDFLARE.md): optional hosted endpoints and network use. Examples are not proof of a live deployment.
- [Flash Studio](FLASH-STUDIO.md): optional user-installed slicer.
- [Geometry layout](GEOMETRY-LAYOUT.md) · [CAD split plans](CAD-SPLIT-PLANS.md)

## Contributors and history

- [Contribution workflow](../CONTRIBUTING.md) · [Pull request template](../.github/PULL_REQUEST_TEMPLATE.md)
- [Branding guide](BRANDING.md) · [Release notes](RELEASE-NOTES.md) · [Release checklist](RELEASE-CHECKLIST.md) · [Release notes template](RELEASE-NOTES-TEMPLATE.md) · [Internal release history](INTERNAL_RELEASE_NOTES.md)
- [Operator live-proof protocol](PHASE2-LIVE-PROOFS.md): evidence capture and reproducible OpenSCAD benchmarks for hardware-bound follow-ups.
- [Rename migration](RENAME-MIGRATION.md)
- Historical or internal records, not setup instructions: [Docker acceptance](DOCKER-117-ACCEPTANCE.md), [Docker progress](DOCKER-117-PROGRESS.md), [timelapse record](ISSUE-126-TIMELAPSE.md), [hardcoded values inventory](hardcoded-inventory.md), [CAD evaluation](KICAD-EVALUATION.md), [reuse map](REUSE-MAP.md), [multi-printer design](MULTI-PRINTER-DESIGN.md), [3MF remap inventory](3MF-REMAP-INVENTORY.md), [recovered testing lane](history/RECOVERY-TESTING.md).

These versioned `docs/` pages are the source of truth. Document features with the code and identify wiki impact in the [contribution checklist](../CONTRIBUTING.md#automated-pr-review-support). Use documentation for the checked-out revision; draft work is not shipped capability.
