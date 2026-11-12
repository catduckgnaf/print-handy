# Print Handy 2.2.10: ZMod 1.7.6, update alerts with a button, and your printer's screen in the Web UI

One Docker image for amd64 + arm64: ghcr.io/catduckgnaf/print-handy:2.2.10, also tagged latest.

2.2.10 is the ZMod 1.7.6 release. ZMod 1.7.6 shipped on 2026-11-11 and Print Handy was audited against it the same day: every changelog row has a verdict, and the parts that touch Print Handy (the printer screen, the built-in AFC panel, Orca slot sync, slicer-side spool selection) are handled below. Then we pointed it at a real AD5X and let it update itself. If you upgrade an existing install, read "Upgrade and compatibility" first.

## What you can do

### Get told when ZMod updates, and update with one press
- Each ZMod printer gets one chat alert per new ZMod release, on Discord, Telegram, Matrix and the Web UI chat. It names the printer and the change (`1.7.5-2 -> 1.7.6-17`) and carries **Update** and **Skip this version**. Update runs the same confirmed path as `/print-firmware update`: it asks again, checks the printer is idle right before sending, and reports the version it verified afterwards. Matrix has no buttons, so its alert names the command. (#1297)
- When an update cannot be applied from chat, the alert says why in one line and what to do instead: Moonraker cannot read the latest version, the install is marked not valid, or the release is a new ZMod base that installs from USB or `ZFLASH`. That last rule is enforced on every route, not just written in the alert. (#1297) (#1314)
- Say it how you say it: "update helix", "update helix screen" and "update helixscreen" are one component, and so are `zmod`/`z-mod`, `klipper`/`klippy`, `moonraker`/`moon` and `guppy`/`guppyscreen`. On ZMod, asking for GuppyScreen tells you it ships inside ZMod. Every bot command now has a plain-words equivalent, and CI keeps it that way. (#1297) (#1298) (#1292)
- `/print-firmware` on ZMod lists only what Moonraker really reports, never offers a component Moonraker would refuse, and `/print-firmware guide zmod` leads with ZMod's own no-USB route. A Klipper 13 MCU that is out of step gets `UPDATE_MCU FORCE=13`, because a bare `UPDATE_MCU` flips the firmware and would flash the wrong build. (#1296) (#1278) (#1288)
- A weekly firmware watch compares ZMod, Klipper, Moonraker, OctoPrint, Marlin, Prusa Buddy (per printer line) and QIDI (per model) with the versions Print Handy was last audited against, and opens one issue per newer release. Notification only. (#1278) (#1289)

### See the printer's own screen
- ZMod printers publish their screen, and the Web UI now shows it: a read-only **Printer screen** page from the printer's card, behind the Web UI login, refreshing while it is open. Read the prompt on the printer from the couch. Nothing on the page can press the screen. (#1312)
- The screen is never mistaken for the bed camera. ZMod lists its screen as a webcam named `screen`, and a printer that listed it first would have had its live view, snapshots and camera watch pointed at the display instead of the build plate. Automatic camera selection now skips screen feeds, and a printer whose only listed feed is its screen says no camera is available. (#1309)

### Spools, slots and the slicer on ZMod 1.7.6
- Orca plates that use only some of a project's filaments (filaments 1 and 3, or filament 3 alone) can be printed with an explicit tool map instead of being refused with "tool mapping must be contiguous". Every used tool must still be mapped and material-checked, and a map entry for a tool the file never uses is refused by name. `print-now --slot` and `remap-slot` remap the tool the file actually uses. Files you finalized before this release launch exactly as they did. (#1313)
- Lane records from the printer are read in both shapes ZMod and HelixScreen write. A record that names a different slot than the one it is stored under, or two records that disagree about one slot, are named once and never rebind a slot. Binding a spool no longer writes an IFS-shaped record into a Creator printer's toolhead namespace. (#1310)

### Act on print alerts from Telegram and Matrix
- Pause and stop alerts from the print watch carry buttons on Telegram and reactions on Matrix, like Discord. Only allowed users can act, only the actions the alert offered work, and a reaction made while the bot was down is ignored when it comes back. (#1299) (#1311)

### Projects, intake and slicing
- Collect a multi-part project over several messages ("add to project", then **Done adding**), or drop a ZIP of sources, and it goes through the one project builder. A multi-part AI design hands all its parts to intake as one project. (#1220) (#1223)
- A per-part color job that only renumbers filaments now slices your original project, remapped, so the designer's per-filament settings, purge matrix, paint and height ranges survive. Verified with real OrcaSlicer. (#1283) (#1253) (#1261)
- PrusaSlicer modifier, negative and support-blocker volumes are split out and never printed as solid plastic. (#1265) (#1257)
- AI fix split cuts and caps through solid material instead of refusing, and the painted-plate fit measures each scale step without writing a 3MF, which is much faster on big painted models. (#1263) (#1264)

### Printer accessories
- Printers can record accessories, starting with the Bondtech INDX toolchanger: the Klipper printer it is fitted to is treated as a multi-tool printer, and no other printer is. (#1268)

### Pasta Wrapped
- Print Handy Wrapped is now **Pasta Wrapped**, everywhere you see it. (#1301) (#1302)

## Fixes
- A short print reported the wrong duration in its follow-up and a timelapse that claimed more frames than were captured. Both are fixed, a claimed start that printed is no longer reported as abandoned, and timelapse assembly defaults to the CPU encoder. (#1287)
- Timelapse VAAPI encoding hard-coded `/dev/dri/renderD128` and failed when the Intel GPU had another render node. It now opens the usable non-NVIDIA node. (#1267)
- A container that cannot write its data folder now stops once with a message naming the folder, the host folder, the user and the exact `chown` to run, instead of failing later or restart-looping. (#1281)
- Camera and vision failures in the print watch post one alert per failure episode and one notice when checks work again, on Moonraker and Bambu Lab. (#1252)
- The Web UI uses the same heater ceiling as every other surface, and uncertain Bambu Lab start leases settle from printer status. (#1255)
- Fit probes read the job printer's own bed exclusions, and the geometry audits for split pieces, mirrored support paint and wedge roots are finished. (#1247) (#1249)
- Web UI remainders: a request id on every 500, upload refusals that drain the request, and a password re-hash fix. (#1254)
- "skill" is no longer corrected to "still", and chat forgives a few more operator-mined typos. (#1292) (#1272)
- Faster chat dispatch and painted-model handling, with no verdict changes. (#1250)

## Upgrade and compatibility
- **ZMod update alerts are on by default.** A ZMod printer is checked about once a day (first check 2 to 10 minutes after start, never during a print) by reading Moonraker, plus at most one unauthenticated GitHub request an hour. Turn it off with `[firmware] update_alerts = false`; change the interval with `update_check_hours` (1 to 168). If the bot and the Web UI chat both run, each announces a release once.
- ZMod 1.7.6 is the new audited baseline. Print Handy keeps working on 1.7.5.
- After a ZMod update reboots the printer, Moonraker can report every component as unknown until its update manager refreshes; Fluidd or Mainsail's "check for updates" clears it.
- The Unraid templates follow `:latest` and never pin a version or digest. (#1266)
- Matrix alert reactions need an unencrypted room, as the rest of the Matrix bot does.

## Verified and not verified
Live, on an AD5X on 2026-11-11: Print Handy updated ZMod 1.7.5-2 to 1.7.6-17 and then HelixScreen v1.0.7 to v1.0.8 through the same path the Update button runs, verified each by re-reading the printer, and the printer came back ready with its spool records intact. On the same printer, read-only: the update alert detection, the printer screen view, camera selection against the real webcam list, and lane-record parsing of the real records.

Not verified live: Matrix reactions, sparse tool maps through a real print, the Web UI alert buttons, and the weekly firmware watch on its schedule. They are covered by offline tests.

## Under the hood
- CI: a pull request's superseded runs are cancelled but a main run never is, a burst of merges to main keeps only the newest queued run, and the read-only image probes get a writable data root. (#1248) (#1293) (#1308)
- Contribution rules enforced by Actions: a bug fix carries a regression test seen failing, and an issue closes only with its checklist finished. (#1230)
- Docs: US spelling across the user guides, the AD5X contract moved to history, the 2.2.9 index digest pinned and the CA template snapshot refreshed, and the README quickstart test keeps the Web UI's cookies. (#1259) (#1251) (#1256) (#1258)
