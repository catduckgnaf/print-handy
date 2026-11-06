# Print Handy documentation

[Project overview](../README.md) · [Contributing](../CONTRIBUTING.md)

Find the task you came to do. Print Handy runs locally, with no Print Handy cloud. Chat providers carry messages and files you send; model downloads and optional hosted AI use the network too. Core print flows do not require an AI model endpoint.

## Start here

- [Docker](DOCKER.md): recommended install. Follow the quick start, configure a printer and chat platform, then check setup.
- [Standalone bot](STANDALONE.md): run without Docker. Choose [Discord](DISCORD-BOT-SETUP.md), [Telegram](TELEGRAM-BOT-SETUP.md) or [Matrix](MATRIX-BOT-SETUP.md).
- [Hermes installation](INSTALL.md): add Print Handy to an existing Hermes installation.
- [Web UI](WEBUI.md): optional local status, setup and settings interface. Set access before exposing it on a network.
- [Bambu A1 Docker walkthrough](BAMBU-A1-DOCKER.md): hardware-specific steps and verification limits.

## Everyday printing

- [Project reference](PROJECT-REFERENCE.md): commands and everyday workflows.
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
- [Branding guide](BRANDING.md) · [Release notes](RELEASE-NOTES.md) · [Internal release history](INTERNAL_RELEASE_NOTES.md)
- [Rename migration](RENAME-MIGRATION.md)
- Historical or internal records, not setup instructions: [Docker #117 acceptance](DOCKER-117-ACCEPTANCE.md), [Docker #117 progress](DOCKER-117-PROGRESS.md), [timelapse issue #126](ISSUE-126-TIMELAPSE.md), [hardcoded values inventory](hardcoded-inventory.md), [CAD evaluation](KICAD-EVALUATION.md), [reuse map](REUSE-MAP.md), [multi-printer design](MULTI-PRINTER-DESIGN.md).

These versioned `docs/` pages are the source of truth. Document features with the code and identify wiki impact in the [contribution checklist](../CONTRIBUTING.md#automated-pr-review-support). Use documentation for the checked-out revision; draft work is not shipped capability.
