# Print Handy documentation

[Project overview](../README.md) · [Contributing](../CONTRIBUTING.md)

**Local-first, by design.** Print Handy was made as a local-only solution: it runs on your own machine, reaches your printers at their local addresses, and keeps models, spool records and print history on your network. There is no Print Handy account and no Print Handy cloud. The chat app you pick still carries what you send through it (a model file, a photo, a timelapse) over that provider's servers, like any other message there; Print Handy adds no cloud of its own, and the doctor, the Web UI and a self-hosted Matrix room stay local. You do not need AI — every core flow works with no model endpoint configured — and you do not need Discord: the same bot runs on Telegram or Matrix instead, and the local Web UI covers status, settings and spools on the machine itself. The guides below are written for that shape; anything that reaches outside the house (a model site, an optional cloud model endpoint) says so where it happens.

## 1. Install one mode

- [Standalone bot](STANDALONE.md): Discord without Hermes or a required LLM; token permissions, configuration, setup checks and systemd services. Needs a [Discord bot](DISCORD-BOT-SETUP.md).
- [Telegram bot setup](TELEGRAM-BOT-SETUP.md): run the standalone bot on Telegram instead of Discord (#164); a pure bot with no LLM, long polling, inline-keyboard prompts. Also covers pairing with ZMod's moonraker-telegram-bot as a companion for live control, notifications and timelapse (#300).
- [Matrix bot setup](MATRIX-BOT-SETUP.md): run the standalone bot in a Matrix room instead (#164); a pure bot with no LLM, /sync polling, numbered-list prompts, unencrypted room required.
- [Hermes installation](INSTALL.md): profile installation, private overlays and listener setup.
- [Docker](DOCKER.md): container configuration, persistent storage and service lifecycle. Follow the guide for the checked-out revision; do not assume a draft packaging PR is available.
- [Component provenance and vulnerability inventory](COMPONENTS.md): every pinned third-party component in the image and Compose file, where its pin lives, the weekly image SBOM and vulnerability report, and the review cadence and exception policy.
- [More than one printer](MULTI-INSTANCE.md): one instance per printer, kept as the alternative for hard process isolation, with one shared Spoolman. The default shape is one instance serving a whole roster — see [SETTINGS.md — Printers list](SETTINGS.md#printers-list) and [MULTI-PRINTER-DESIGN.md](MULTI-PRINTER-DESIGN.md).
- [From the image to a first A1 print](BAMBU-A1-DOCKER.md): the Docker image on a Bambu Lab A1, step by step (#274): Compose overrides, A1 templates from the bundled OrcaSlicer, the hand-written hardware record, MakerWorld token, Discord, the LAN print opt-in and the fixes still pending. Verified on an A1 on 26 Oct 2026.

Do not run both Discord modes with the same token. Installing, enabling services and contacting a printer are separate actions from offline model analysis.

## 2. Configure and use

- [YAML configuration and TOML migration](CONFIGURATION-YAML.md): schema, precedence and safety limits.
- [Guided setup and debugging in Discord](GUIDED-SETUP-DEBUGGING.md): the `/print-handy setup` checklist (resumable, consent-gated, never collects the token) and the read-only, redacted `/print-handy debugging` checks.
- [Web UI](WEBUI.md): the optional, off-by-default local page — first-run walkthrough, status and doctor checks, the picker that writes the same YAML settings file, spool management, the read-only designs catalog, and a read-only sliced-layer view. Printer controls are a separate opt-in and stay off by default.
- [Live camera in Discord](DISCORD-BOT-SETUP.md#7-live-camera-activity-optional-311): the optional Discord Activity live view (#311) — Developer Portal and named-tunnel setup, allowlist, privacy notes, and the honest verification status (the live spike has not run).
- [Settings reference](SETTINGS.md): every setting with its YAML key, environment variable and Docker value, plus Docker build arguments and secrets.
- [Detailed project reference](PROJECT-REFERENCE.md): workflow examples, commands, printer models, spool identity rules, monitoring and updates.
- [Printer macros](MACROS.md): `/print-macros` live macro discovery on Moonraker printers, and the per-vendor macro matrix.
- [Firmware updates](FIRMWARE-UPDATE.md): `/print-firmware` version reporting, guarded component updates on ZMod and vanilla Klipper, the guided ZMod base / stock firmware flows, and the per-family update matrix for every other printer.
- [Printer calibration](CALIBRATION.md): `/print-calibration` — the before/after accept pattern (before values, confirm, run idle-only, new values and differences, accept before anything saves), per-calibration notes for mesh, screws, PID, input shaper, belts and the Z-offset report/guide verdict, and calibration records.
- [Slicer warnings and the support question](SLICER-WARNINGS.md): how Print Handy reads Orca's warnings after every slice, the one warning that asks (supports needed while supports are off: re-slice with supports, print without, or cancel), the compact information line for the rest, and the ignore list.
- [Printer profiles and offline delivery](PRINTER-PROFILES.md): vendor/model inheritance, supported delivery methods, verification limitations, and what is kept from a 3MF made for another printer or slicer (#868).
- [Printer support](PRINTER-SUPPORT.md): what "supported" means for each printer, how to try one that is not listed, what a built-in printer consists of, and the hardware checklist for confirming a listed printer works.
- [Optional Flash Studio slicer](FLASH-STUDIO.md): opt-in, user-installed Flash Studio executable for Flashforge printers; an empty path keeps the normal OrcaSlicer choice.
- [Home Assistant panic and printer controls](home-assistant-panic.md): direct, no-LLM AD5X/ZMod printer controls from a Home Assistant dashboard, including the emergency-stop panic restart; no extra daemon.
- [Best practices on specific devices](DEVICE-BEST-PRACTICES.md): per-printer recommended settings, the order to change them in, and what has been verified on hardware. Starts with [AD5X best practices](DEVICE-AD5X.md): screenless with HelixScreen, ZMod version, Z offset, bed mesh and KAMP, Nopoop purge, QuickSwap order and risks, 3MF delivery.
- [AD5X integration contract](AD5X-CONTRACT.md): printer-specific behavior and limitations.
- [ZMod printers](ZMOD.md): all five ZMod models (AD5X, AD5M, AD5M Pro supported; Creator 5 / 5 Pro staged), and how their filament systems differ.
- [Stock Flashforge printers](FLASHFORGE-STOCK.md): every Flashforge model listed with the route that applies to it — the Adventurer 5X on factory firmware has the built-in stock profile (the LAN HTTP API, IFS prints, the launched-print monitor, and timelapse from the camera source the printer advertises; the AD5X has no built-in camera, so read the verification status before expecting a video), while the AD5M, AD5M Pro, Creator 5 and Creator 5 Pro are ZMod-only today.
- [OctoPrint/Marlin notes](OCTOPRINT-MARLIN.md): read the integration-specific limits before assuming Moonraker feature parity.
- [Sovol printers](SOVOL.md): Klipper and Marlin/OctoPrint keys for the SV series, the SV08 family, and the Zero, with the build volume each key uses.
- [Bambu Lab printers](BAMBU-LAB.md): profiles for every single-nozzle Bambu Lab printer (A1, A1 mini, P1P, P1S, P2S, X1, X1 Carbon, X1E, H2S), generating slicer profiles from your own OrcaSlicer, the G-code guard, opt-in read-only LAN status, unverified values, and the licensing basis awaiting approval.
- [Bambu Lab live connectivity research](BAMBU-CONNECTIVITY.md): LAN MQTT/FTPS evidence from maintained integrations, the 2025 authorization change, the staged live-support plan for the A1/P1S, and the unknowns that need a live test.
- [Prusa printers](PRUSA.md): the MK4S/CORE One/XL/MINI+ profiles, generating slicer profiles from your own OrcaSlicer, the PrusaLink LAN client (status, verified upload, gated start), and what is not supported.
- [Elegoo printers](ELEGOO.md): the Neptune 4 family on Moonraker and the Centauri Carbon on SDCP, generating slicer profiles from your own OrcaSlicer, the SDCP LAN client (status, verified upload, gated start), and what is not supported.
- [Qidi printers](QIDI.md): the Klipper line (Q1 Pro, X-Plus 3, X-Plus 4, X-Max 3, X-Smart 3) on Moonraker, generating slicer profiles from your own OrcaSlicer, and what is not supported.
- [Creality printers](CREALITY.md): the Klipper/Creality OS line (Ender-3 V3 series, K1 series) on Moonraker, generating slicer profiles from your own OrcaSlicer, why Marlin Enders are not this vendor, and what is not supported.
- [Anycubic printers](ANYCUBIC.md): the Klipper-based Kobra 2 family on Moonraker-exposed firmware (the stock-firmware caveat stated up front), generating slicer profiles from your own OrcaSlicer, why the Kobra 3 / S1 generation and the Marlin models are not this vendor, and what is not supported.
- [Snapmaker printers](SNAPMAKER.md): the U1 tool changer on Moonraker, four toolheads, generic templates, and what is not supported.
- [Read-only mesh topology audit](MESH-AUDIT.md): topology metrics, CLI usage, and manifold/watertight checks.
- [100%-scale geometry layout and export](GEOMETRY-LAYOUT.md): multi-object single-plate packing, status ladder, and geometry-only export boundaries.
- [Opt-in reviewed CAD split plans and dowel connectors](CAD-SPLIT-PLANS.md): reviewed split presets, chamfered dowel generation, OpenSCAD CSG script synthesis, and post-boolean volume conservation checks.
- [Running the AI features on Cloudflare's free tier](CLOUDFLARE.md): chat and design through Workers AI's OpenAI-compatible endpoint, the camera watchdog and lookup by meaning through the two reference Workers in `examples/cloudflare`, free-tier limits with rough usage per feature, and what leaves the house. Mock-tested only; not yet run against a real Cloudflare account.
- [AI design with OpenSCAD](DESIGN.md): `/print-design` describes a part in chat, renders and measures it, and hands the approved STL to normal intake (#388).

## 3. Troubleshoot by symptom

| Symptom | First check |
| --- | --- |
| Model intake is refused for size or complexity | Simplify or split the model offline; see [intake resource limits](INTAKE-LIMITS.md). |
| Bot replies twice | Confirm only one mode uses the token; see [standalone setup](STANDALONE.md). |
| Startup rejects a setting | Read the named key and [configuration precedence](CONFIGURATION-YAML.md); YAML takes precedence over corresponding TOML files. |
| Slicing fails or model does not fit | Check slicer command, selected printer/profile and model bounds in the [reference](PROJECT-REFERENCE.md). Do not bypass G-code or clearance guards. |
| Spool or color disagrees with the printer | Read the reference's spool/IFS source-of-truth rules before rebinding slots. |
| Installation or update fails | Use the guide for your selected mode; collect the actual error without tokens, private model files or personal service addresses. |
| Physical print problem (first layer, clog, heat creep, mesh, ringing) | `/print-troubleshoot` in Discord, or the [troubleshooting guide](TROUBLESHOOTING.md); `/print-lookup <query>` searches the knowledge, firmware and printer registries. Both are read-only and send nothing to the printer. |

`python3 -m print_handy.doctor` checks setup and can contact configured Moonraker and Spoolman services. Run it only where those contacts are authorized. A clean check does not prove physical readiness.

## 4. Develop and review

- [Listener domains](LISTENERS.md): the per-domain listener registry, the normalised status/event contract, and liveness (#299).
- [Diagnostics](DIAGNOSTICS.md): the #390 error taxonomy, greppable markers, the operator-message bar, and the staged audit inventory.
- [Multi-printer design](MULTI-PRINTER-DESIGN.md): the #257 Stage 2 design for one instance serving many printers — audit of the one-printer assumptions, config/state/routing decisions, and the staged implementation plan. Design only; not implemented.
- [Contribution workflow](../CONTRIBUTING.md) and [pull request template](../.github/PULL_REQUEST_TEMPLATE.md).
- [Release-note guidance](RELEASE-NOTES.md) and [internal release history](INTERNAL_RELEASE_NOTES.md).
- [Branding kit](BRANDING.md): the pitch, approved copy by length, voice rules, the catduck mascot, colors and assets. Copy from it whenever something describes Print Handy.
- [Rename migration](RENAME-MIGRATION.md) for older installations.
- [CAD evaluation](KICAD-EVALUATION.md) and [reuse map](REUSE-MAP.md) for technical background, not promises of shipped capability.

These versioned repository pages are the documentation home. Update them with behavior changes so readers can use the documentation for their checked-out revision.

## 5. Internal and historical records

These pages are kept as records of past work, not instructions to follow. Do not use them as setup or configuration guidance; the guides above are authoritative.

- [Docker #117 acceptance record](DOCKER-117-ACCEPTANCE.md): historical acceptance record for the #117 Docker packaging work.
- [Docker #117 progress record](DOCKER-117-PROGRESS.md): historical progress log for the #117 Docker packaging work.
- [Issue #126 timelapse record](ISSUE-126-TIMELAPSE.md): historical record of the #126 timelapse delivery contract.
- [Hardcoded values inventory](hardcoded-inventory.md): internal inventory of hardcoded values, kept for maintainers.

## Documentation ownership and wiki policy

This `docs/` directory is the dedicated, versioned documentation location for
issue #120, not a placeholder for a future website. Installation, configuration,
workflow reference and troubleshooting live here so changes can be reviewed
with code. GitHub renders these pages directly; use the docs for your selected
revision rather than assuming main describes an installed release.

Every feature PR must identify its documentation and wiki impact using the
[contribution checklist](../CONTRIBUTING.md#automated-pr-review-support).
Update the relevant guide and this index when adding a new page. A separate
GitHub Wiki, if maintained, should provide links or summaries pointing here;
it is not a second source of configuration or safety instructions. No wiki
publication or synchronization is automated by the PR checks.
