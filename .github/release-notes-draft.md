# Print Handy 2.3.5: one Spoolman, honest numbers, quieter Discord

One Docker image for amd64 + arm64: ghcr.io/catduckgnaf/print-handy:2.3.5, also tagged latest.

2.3.4 grew the Web UI. 2.3.5 makes the numbers trustworthy: filament is counted once, even with a Spoolman that lives somewhere else; the spool line before slicing uses the real sliced grams; and loaded spools sync both ways. Discord goes back to plain messages, a model made of several parts prints all of them, and the two newest firmware families got a full audit, so they refuse what they cannot check instead of guessing.

## Testers wanted: Creator 5 and Adventurer 5M owners

The OpenCreator (Creator 5, Creator 5 Pro) and Forge-X (Adventurer 5M, 5M Pro) support is built from each firmware's own source and docs and covered by offline tests, but **none of it has run on a real printer yet**. Anything Print Handy cannot verify fails closed and says why. If you own one of these printers, we need you:

- **Creator 5 or 5 Pro on OpenCreator:** the read-only captures and watched prints listed in #1557. Start with the captures; none of them starts a print.
- **Adventurer 5M or 5M Pro on Forge-X:** a screen-mode read (Feather, Guppy and Headless), one watched print, and the calibration macros. Tell us on Discord or open an issue with what you saw.

Trim serial numbers, addresses and keys from anything you share.

## What you can do

### Use a Spoolman that lives somewhere else
- Point Print Handy at a Spoolman on another host, or at the one your printer's Moonraker already uses. A new Spoolman accounting setting (per printer, or one global default) decides who counts filament use, Print Handy or the printer, so a print is counted once, even on firmware that charges at every tool change, such as OpenCreator's AFC. (#1550)
- Choosing printer-side counting when the printer has no Spoolman link falls back to Print Handy with a warning, instead of counting nothing. (#1553)
- The "this print could be counted twice" warning now asks both Spoolman addresses whether they are the same server. The same Spoolman reached two ways (one address inside the container, one through the host) no longer warns. When Print Handy cannot tell, it still warns and says it could not confirm. (#1560)

### Sync spools both ways
- `/spool sync` and the Web UI Sync button now sync both directions. A spool Print Handy shows as loaded but Spoolman does not is written to Spoolman as loaded. Archived or deleted spools are named, never written back. When the two disagree about a slot, Spoolman wins and the reply says so. (#1554)
- Chat can put a spool back on the shelf, give one spool its own name, and archive or unarchive it: `/spool shelve`, `/spool rename`, `/spool archive`, `/spool unarchive`. (#1540)

### Trust the spool line before you slice
- The "needs about X g" spool line now uses the sliced filament amount, the same figure the support options show, instead of a rough model estimate that could read about half the real use. With both support choices offered it shows the larger, and an estimate made before slicing is labeled as one. (#1562)

### Print a multi-part model without the extra tap
- A model whose files are all different parts (say a shell, a plunger and a spring) prints every part in one job and says which, with no "Print all 3 / Choose files" question. Versions of one part, and a 3MF next to its STLs, are still asked about. To get the question back, turn on **Ask which parts to print** in the Web UI settings (`discord.ask_which_parts`). (#1561)

### Read Discord without the box
- Discord print jobs are plain messages again: each question, progress line and the start is its own message, as on Telegram and Matrix. No more boxed card, and no card stuck on "Working" while the print runs. (#1559)
- New `/print-pause` pauses the running print right away, like the Pause button. "Pause the print", bed-leveling and camera phrases now suggest commands that exist. (#1535)

### See your nozzle and bed on the dashboard
- The dashboard shows each printer's nozzle and bed with the defaults a print uses, and a temperature graph with each heater's target. Swap nozzle opens the guided nozzle change in chat. (#1534)
- The Status page puts the job, camera and spools first, gives each heater its own temperature graph, makes the emergency stop a big red button, and folds system details away at the bottom. (#1541)
- The Web UI Timing page is gone. The same report is still in `print-handy doctor` and the diagnostics bundle. (#1558)

### Connect Home Assistant and API clients
- Upload a file straight to the Web UI inbox with the access token header (`POST /api/upload`), read the fitted nozzle and bed facts in printer status (size and material, Z offset, calibration due, per-material defaults), and record a nozzle change you have physically fitted (`POST /api/printers/<id>/nozzle`). (#1549)

### Creator 5 and Creator 5 Pro on OpenCreator
- Print Handy follows the printer's AFC tool map, so a map changed at the printer no longer blocks starts. A printer with an ACE unit is told plainly that ACE is not supported yet. (#1538)
- A start is refused, with the cause named, when a tool the job uses has no filament, when the printer is holding an interrupted print for recovery, or when a Pro's chamber heater is not reporting. A start the printer itself refused is named in the cancel report. (#1556)
- Multi-tool prints are charged to the spool on the toolhead that actually printed each tool, and resume reads the printing toolhead's own filament sensor. (#1556)
- The material warning checks the spool on the toolhead that will really print each tool, says so when the map could not be read, and now also warns at slicing. All of these are warnings you can accept. (#1555)
- Runout choices mirror the printer's, power-loss recovery is reported, and backup-lane warnings show. Recovery actions stay off until they are verified on hardware; use the printer's own controls. (#1547)
- Setup suggests the right Creator firmware profile and finds Moonraker snapshot cameras. Calibration menus explain the firmware's own path and refuse unverified runs. `print-handy doctor` names the Creator firmware the printer runs. (#1551, #1556)
- `/print-firmware update` is refused on OpenCreator, with how to update from Fluidd or Mainsail, and the firmware report shows how many commits behind you are, since OpenCreator has no version number. (#1539)
- Guidance from Cartrigger (OpenCreator) on #1490.

### Adventurer 5M and 5M Pro on Forge-X
- A print is refused when Print Handy cannot read the printer's screen mode, and the message says what is missing. Headless and Guppy screens reported as numbers are recognized. The mesh prompt no longer shows a ZMod-only warning. (#1555)
- Forge-X beta builds are ordered by their number in update notices. `[firmware] forgex_apply = true` installs a Forge-X update from chat within your version line; it is off by default. (#1532)

### Find your printer by brand
- The docs start from your printer: brand, then model, then the firmware it runs. FlashForge's four firmware routes sit under one brand, and the printer list reads properly on a phone. (#1529, #1544)

## Fixes
- Slicing no longer fails with "could not identify the cause" when the printer has no active spool on record, for example right after you unassign or rebind a spool, and the log no longer fills with warnings about a `.env` file nobody needed. This shipped as a same-day 2.3.4 hotfix. (#1533)
- Color-mapped jobs slice after an active spool is unassigned, using the job's selected slots; empty spool records no longer crash; and when the first slice has already failed, intake reports that failure instead of asking you to approve a slice that cannot succeed. (#1545)
- Toolhead printers no longer get a false material warning on every file, and a real mismatch is named for its tool. A multi-slot print started with no active spool set is recorded in history and its filament is charged. (#1543)
- Model link questions expire after the configured wait instead of staying open; Web UI workflow plate downloads no longer return 404; a slicing crash from a missing value is reported as a Print Handy fault; normal timestamps no longer fill the log with tracebacks. (#1542)
- Printer settings never save half of a rejected submission, overlapping saves keep unrelated changes, Status card connection errors hide URL credentials, and restart waits warn once instead of repeating tracebacks. (#1546)
- The Status page emergency stop works when the Web UI has a login; it was refused every time. (#1535)
- ZMod AD5M and Creator live G-code verification no longer stops with an internal error. (#1556)

## Upgrade and compatibility
- **Discord job posts are plain messages again.** The 2.3.4 job card is gone, and so is the `[discord] job_card_thread` setting. If you set it, startup names it as an ignored setting; it never stops startup. (#1559)
- **Multi-part models print every part by default.** If you relied on "Choose files" for a model made of parts, turn on `discord.ask_which_parts`. (#1561)
- **Spoolman accounting is new.** Left at `auto`, Print Handy keeps counting unless the printer's own firmware charges spools through a linked Moonraker. (#1550, #1553)
- **The Web UI Timing page is gone.** Use `print-handy doctor` or the diagnostics bundle. (#1558)
- **Forge-X needs a readable screen mode.** A print is refused until Print Handy can read it. (#1555)
- Settings added: `spool.accounting` and the per-printer `spoolman_accounting`, default `auto`, plus a per-printer `spoolman_url` (#1550); `firmware.forgex_apply`, default `false` (#1532); `discord.ask_which_parts`, default `false` (#1561). Setting removed: `discord.job_card_thread` (#1559).

## Technical details and contributors
- The firmware audit for the two newest families (#1555) and the code-only half of the OpenCreator work (#1556), each fix pinned by a regression test that failed on the previous main.
- The Web UI, chat and Home Assistant filament actions are held to each other by a parity test, so a spool action that exists in one place exists in all of them (test only). (#1552)
- Slicing-speed baselines verified unchanged for this release: from main CI run 43806603862 on bcf821cb, the discord-timing median ratio is 1.067 over 28 cases of 0.3 s and up with no rise over 20%, and the real-slicer leg shows a median ratio of 0.944 with no rises.

## Verification and known limitations
- Offline suites and CI green on every merged head, and on main before the release commit (CI run 43806603862).
- Not verified on hardware: OpenCreator on a real Creator 5 or 5 Pro, and Forge-X on a real Adventurer 5M or 5M Pro (see Testers wanted above and #1557); a real outside Spoolman with a linked Moonraker; two-way spool sync against a live printer. All are covered by offline tests.
- Real-install timing was not measured for this release.
