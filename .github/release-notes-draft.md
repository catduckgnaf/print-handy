# Print Handy 2.3.4: the Web UI grows up

One Docker image for amd64 + arm64: ghcr.io/catduckgnaf/print-handy:2.3.4, also tagged latest.

2.3.3 learned to answer when you call it by name. 2.3.4 grows up everywhere else: the Web UI becomes a real dashboard with a print workflow in the browser, Discord gets real slash commands and one tidy card per job, color questions shrink to one tap, print starts answer in seconds, and two more printer families get a proper G-code guard. Underneath, the whole codebase stopped swallowing its own errors, so when something does go wrong it says so in the log.

## What you can do

### Run a print from the browser
- The Web UI's Status page is now a dashboard: camera, printer state, temperatures over time, filament, and recent prints in one place. A new Workflow page runs a print from a link or an upload right in the browser, a Printer settings tab makes per-printer setup plain, the Designs page can start a design in the browser, and Pasta Wrapped got its polish. The Web UI also installs as an app (PWA foundation) and has a proper favicon. (#1464)
- A new Sliced files page lets you download finished slices from the Web UI. (#1464)

### Use real Discord slash commands
- Slash commands are real slash commands now: each one answers once, inside the interaction, with no echoed command line and no second post. `/spool`, `/print-calibration`, `/print-firmware`, `/print-macros` and `/nozzle` open with real subcommands and typed options, and job, plan, plate, spool, slot, macro and printer fields autocomplete from your own lists as you type. `/print-colors` picks a past job instead of asking for a file path on the server, and `/print-help` leads with the five things people do most. (#1484)
- New: `/print-status` tells you how the current print is doing. (#1484)
- Plain words keep up with the new forms: "list the spools", "input shaper", "calibrate z offset" and the rest of the calibration and spool subcommand forms each offer the right command, and a mistyped subcommand (`/spool chnage`) asks "Did you mean `/spool change`?" before doing anything. (#1498)

### Answer color questions in one tap
- When a file's part colors match the spools you have loaded, Print Handy proposes the mapping up front ("Plaque: Black PLA+, Letters and trim: Bronze PLA+") and one tap accepts it. Parts that share a color are asked about once, together; on Discord all the choices arrive in one message of dropdowns; and the same file with the same spools loaded offers "Same colors as last time" first. Nothing is ever applied without your tap. (#1487)

### Watch one card per job on Discord
- Each Discord job is now a single card that updates in place: model, thumbnail when the source has one, material, estimate, and a color for its state. Questions appear as buttons on the card and fold into it when answered, progress just updates the card, and summaries and warnings no longer repeat-post. (#1488)

### Start prints that answer in seconds
- After the upload, chat now answers "the printer accepted the start and is heating up" within seconds instead of sitting silent for up to 40 s, and the upload, start-sent and confirmed updates are one message edited in place. A start is still sent exactly once and never resent. (#1485)

### Print to an Adventurer 5M or 5M Pro on Forge-X firmware
- Adventurer 5M and 5M Pro owners running Forge-X firmware (requires the Forge-X 1.4.2 beta; stable 1.4.1 is not supported by these models) can pick their printer (Flashforge AD5M (Forge-X) / Flashforge AD5M Pro (Forge-X)): slicing uses the firmware's documented single-line start form, delivery is plain G-code, firmware updates are reported but never applied, and pointing an AD5M profile at the wrong custom firmware (ZMod vs Forge-X) now says so in the readiness refusal — that direction previously passed silently, on a pair of firmwares that share the START_PRINT macro name with different parameters. Documented and configuration verified; not yet hardware verified. See docs/FORGEX.md. (#1506)

### Print to a Creator 5 or Creator 5 Pro on OpenCreator firmware
- Creator 5 and Creator 5 Pro owners running OpenCreator firmware can pick their printer (Flashforge Creator 5 (OpenCreator) / Flashforge Creator 5 Pro (OpenCreator)): slicing uses the firmware's documented start form with labeled objects, delivery is plain G-code, and pointing a Creator profile at the wrong custom firmware (ZMod vs OpenCreator) now says so in the readiness refusal. Documented and configuration verified; not yet hardware verified. See docs/OPENCREATOR.md. (#1495)
- Thanks to Cartrigger (FlashForge C5 Modding Group, OpenCreator) for explaining the firmware's VDS, ACE and print-start behavior on #1490 and for offering support.

### See where the time goes
- A new Timing page in the Web UI, a doctor section and the diagnostics bundle show the waits you actually feel — link to first question, last answer to ready, slicing, tap to print started — as medians and p90s from each job's timing log. (#1489)
- Crashes now leave evidence: an unhandled error saves a redacted crash record, the diagnostics bundle gains a Crashes section and carries logs even on installs that never configured a log file, and every job carries a correlation ID so one job can be followed across slicing, launch and monitoring. (#1409, #1424, #1429)

### Find your printer by brand
- The README's printer section and the printer profiles page now start from your printer's brand and model, then its firmware, then the connection. FlashForge shows its four profiled firmware routes (stock, ZMod, Forge-X, OpenCreator) under one brand heading, Creality, Qidi, Snapmaker and the other brands keep their own entries instead of disappearing into a generic Klipper list, and the profiles page lists every registered model key. (#1526)

### Stream the camera into a Discord voice channel
- The Discord live camera view now transcodes an MJPEG or snapshot printer camera to H.264 when the live feed values are left at their defaults, so the Activity player receives a stream it can open; RTSP and WebRTC cameras are still relayed unchanged. The setup guide records the final design: the camera plays in the channel's Activity and the bot runs the session without joining the call. Not live-verified yet; the first live sitting is still to come. (#1453)

## Fixes
- Slices for a printer other than the default were marked verified without the offline safety checks ever running, and could be downloaded on that basis. Every slice is now verified at slice time with the job's own printer, and a slice the checker refuses fails at slice time with the reason. (#1478)
- OctoPrint and Marlin printers accepted G-code files sliced for another printer, carrying Klipper or ZMod start macros, or moving outside the bed, and failed at the printer instead. Those files are now refused up front with the reason named; hand-written Marlin start code still works. (#1481)
- On key-protected Moonraker installs, some surfaces sent no API key and failed quietly. Stop, pause, resume, the Web UI, launch checks and the purge safety gate now all send the `MOONRAKER_API_KEY` you configured through one shared transport; a missing or wrong key is named as such, and a towerless print whose purge mode cannot be checked because of an auth failure is refused instead of going ahead unchecked. (#1483)
- A PrusaLink or Elegoo SDCP host written into the settings file under `[prusa]` or `[elegoo_sdcp]` was silently ignored as an unknown section, so the printer used a different address than the one on file. Those values now load. (#1480)
- Swapping spools between two IFS slots from the Web UI or the Home Assistant integration, or unassigning and reassigning them, no longer gets undone a few seconds later by the HelixScreen sync, and no longer posts "HelixScreen change" messages. (#1427)
- The channel is quieter: the AI fix button no longer appears on jobs built from several files, a failed fix is not offered again on the same slice, a finished print posts its completion exactly once, a flip-flopping screen slot settles into a single change message, and a camera check that cannot tell from one frame is logged quietly instead of posted. (#1486)
- Two expected events — looking up a print by a title shaped like a plate alias, and the connection drop a REBOOT macro causes on purpose — no longer write WARNING tracebacks to the log. (#1465)
- A geometry fix preview falls back to the built-in renderer when OpenSCAD hangs, instead of failing. (#1425)
- The button in chat is renamed **Slice and Download**, and that is what it delivers: the sliced file itself, on Flashforge printers too, including files sliced from AI designs. (#1464)
- Printer listener and watch failures are logged as warnings for every printer family, and Bambu Lab LAN listener lines are redacted like every other log line. (#1422)
- Release housekeeping: the README is rewritten as a short landing page (#1412), and the Home Assistant OS app now offers each release within minutes of publishing (#1418).

## Upgrade and compatibility
- **Slash commands answer in place.** Discord commands no longer echo the command line as a separate post before answering; each command answers once inside its interaction. (#1484)
- **The Web UI Layers page is gone.** Its job moved into the dashboard and workflow pages. (#1464)
- **Discord job posts change shape.** One card per job, edited in place, replaces the trail of separate posts. A new `[discord] job_card_thread` setting (off by default) moves long detail into a thread on the card. (#1488)
- **The Moonraker listener's separate log file is retired.** Its lines are in the shared log now, where `[logging] levels` and debug capture see them. (#1422)
- **Settings-file `[prusa]` and `[elegoo_sdcp]` hosts now take effect.** If you wrote one and Print Handy used a different address, it will now use the address on file. (#1480)
- **Live camera feeds transcode at defaults.** An MJPEG or snapshot camera is transcoded to H.264 for the Discord live view unless you changed the live feed values; the raw camera stream that timelapse and the watchdog read is unchanged. (#1453)
- No setting was removed or renamed. One setting was added: `discord.job_card_thread`, off by default. (#1488)

## Technical details and contributors
- The silent-failure cleanup (#1417, #1455): every silent `except: pass` and `contextlib.suppress` in the package was replaced with logging, recovery, or a narrowed handler — launch (#1439), runtime (#1431, #1432, #1433), core (#1421, #1434, #1435, #1436, #1482), bot (#1437, #1438), Web UI (#1426), jobs and G-code (#1423), and the package-root modules (#1440) — and a package-wide guard test now fails if a new silent handler lands (#1457), including broad no-log handlers on launch decision paths (#1479).
- Logging markers for the supervisor, Web UI and gates (#1420); default Docker log writes stay fail-open so a logging failure cannot take the container down (#1419).
- Tests no longer inherit a credential from the machine running the suite (#1441), and every test gets its own prompt registry (#1442).
- Development-checkout bug reports can answer the diagnostics-bundle field with Not applicable (#1477).
- Slicing-speed baselines verified unchanged for this release: from main CI run 43675045757 on the release commit, the discord-timing median ratio is 0.945 over the 28 cases of 0.3 s and up with no gate failures, and the real-slicer leg (same EPYC 7763 runner class as its baseline) shows no rises.

## Verification and known limitations
- Offline suites and CI green on every merged head, and on main at the release commit (CI run 43675045757).
- Not verified on hardware: OpenCreator support on a real Creator 5 or Creator 5 Pro (the toolchanger and AFC status shapes are unverified, so the active tool reports as unknown), Forge-X support on a real Adventurer 5M or 5M Pro (the mod_params and update-status shapes are unverified), the Discord voice-channel Activity live, a real Discord slash-command sitting, and the new Web UI workflow against a live printer. All are covered by offline tests.
- Real-install timing was not measured for this release; no proof-run timing summary was recorded.
