# Firmware updates

Print Handy can check a printer's firmware state and, for the one family
where a safe programmatic path exists, apply one component update at a
time behind a confirmation. Everything else is reporting or guided manual
steps. This page is the per-family matrix: what `/print-firmware` does for
each supported printer family, and why. It matches the code in
`print_handy/firmware_update.py`; if the two ever disagree, the code's
tests are wrong or this page is stale; both are bugs.

Nothing here is live-hardware verified unless a row says so. The ZMod /
AD5X behavior rests on ZMod's and Moonraker's documentation and on
mocked Moonraker responses in `tests/test_firmware_update.py`.

## The commands

- `/print-firmware`: the report. On a Moonraker printer it reads
  Moonraker's Update Manager state (a user-invoked check refreshes first;
  Print Handy never refreshes in the background) and lists each
  component's current version and the available update, if any. On any
  other printer it answers with that family's guidance below.
- `/print-firmware update <component>`: apply exactly one component
  update, ZMod and vanilla Klipper printers only (see the eligibility
  rule). The component name is resolved against the live status just
  read, a typed name is never sent on trust, and the confirmation
  shows `component: current -> available`. There is nothing to confirm
  when no update is available.
- `/print-firmware guide zmod` / `/print-firmware guide stock`; the
  guided manual flows for the ZMod base and the stock Flashforge
  firmware under it (AD5X family). Print Handy performs none of those
  steps; the guide ends by asking you to run `/print-firmware` so the
  resulting versions are verified. `guide zmod` follows ZMod's
  "Updating the Mod": check for updates in Fluidd or Mainsail first.
  A routine update needs no USB drive and runs from the Software
  Update section (errors on the first check are normal; check again,
  then restore and update each module one at a time). A component
  with a real available version can also go through
  `/print-firmware update`; a component reported as `?` only through
  Software Update. Only when ZMod shows "Update Z-Mod via USB" does
  the guide send you to USB: the `ZFLASH` macro (it downloads the
  release to the USB drive, so the drive must be in, and in before
  power on without the native screen) or a manual install. It then
  checks the OS and ZMod version pairing (System, Distribution) and
  closes with `UPDATE_MCU FORCE=13` only if the report shows a
  Klipper 13 MCU out of step. It names no `ZFLASH2` / `ZFLASH3`
  macros: ZMod's sources at 1.7.6 name only `ZFLASH`.

There is no automatic applying and no background updater. Availability
may be reported (see the update alerts below); only a person confirms an
update.

When Moonraker reports `?` for a component's latest version (a live
AD5X on ZMod 1.7.5-2 does this for every git component), that row says
"latest version not reported" and the report says Print Handy cannot
tell whether an update exists. It never says "up to date" for such a
component, and `/print-firmware update` refuses it rather than guess.

Moonraker's GitHub checks are unauthenticated, 60 requests an hour across
every repo on the printer. When that budget is spent, latest versions read
`?` until it resets, and the report says so with the reset time. Empty
placeholder entries (ZMod's Moonraker lists `moonraker` and `klipper` as
empty beside its real `moon` and `klippy`) are not components and are not
listed. A component Moonraker marks invalid (`is_valid: false`, for
example a modified repo) is reported with Moonraker's reason and is never
offered or requested, because Moonraker refuses to update it. On ZMod the
usual cause is an edited `moonraker.conf`: an installer such as
HelixScreen's appends its own section to ZMod's file, which ZMod says
must not be edited. Moonraker write-protects every file inside a repo its
update manager manages, so this cannot be fixed from Fluidd's editor or from
Print Handy; it takes a shell on the printer (SSH). Copy the added section
into `mod_data/user.moonraker.conf`, run `git checkout -- moonraker.conf` in
the Z-Mod repo (the `path` of Moonraker's `[update_manager Z-Mod]`,
`/usr/data/zmod/zmod` on the AD5X), and restart Moonraker
([#1295](https://github.com/catduckgnaf/print-handy/issues/1295)).

## ZMod update alerts in chat

On by default (`[firmware] update_alerts`, see [Settings](SETTINGS.md)).
For each ZMod printer, Print Handy checks once a day
(`[firmware] update_check_hours`) and posts one alert in the printer's chat
when a new ZMod release is available, on whichever platform the bot runs
(Discord, Telegram or Matrix):

> **ZMod update available for Garage AD5X:** 1.7.5-2 -> 1.7.6-17.
> Press **Update** to install it. Print Handy asks once more, checks the
> printer is idle and verifies the result.

- **Update** runs exactly `/print-firmware update zmod`: the component is
  read live, the same confirmation shows `zmod: current -> available`, the
  printer must be idle when you confirm and again just before the one
  upgrade request, and the result is verified by re-reading. Nothing is
  applied without both presses. **Skip this version** silences that release.
  Matrix has no buttons, so its alert names `/print-firmware update zmod`
  instead. After a bot restart an old alert's button no longer answers;
  the typed command still works.
- One alert per ZMod release, remembered in the printer's state folder
  (`firmware_update_alerts.json`), so a restart does not repeat it. Builds
  inside a release (the `-N` in `1.7.6-17`) do not alert on their own; the
  button applies whatever Moonraker offers when you press it, and the
  confirmation names it exactly. A release first announced without a
  button is announced once more if it later becomes installable from chat.
- Never while printing: a printing, paused or unreadable printer is asked
  again 30 minutes later. Checks are jittered, read-only (GET requests,
  never a Moonraker refresh), and fail soft: a failed check is logged once
  and sends no alert.

Where the versions come from: Moonraker first. ZMod's Moonraker lists a
`Z-Mod` component (git repo `ghzserg/z_ad5x`, on the branch of your ZMod
base, for example `1.7`) with its installed and remote version; that is the
update Fluidd's Software Update installs and the one the button applies.
ZMod's GitHub releases (`ghzserg/zmod`, latest final release, read without
a token, 10 second timeout, at most once an hour for the whole install) fill
two gaps: Moonraker showing `?` as the latest version (usually the printer's
own unauthenticated GitHub limit of 60 requests an hour), and a release on a
new ZMod base, which Moonraker's branch-pinned remote never shows.

When the alert has no button, it says why in one line and how instead:

| Case | How it is detected | The alert says |
|---|---|---|
| Moonraker marks the Z-Mod install not valid | `is_valid: false` (or `corrupt`) on the component, for example a checkout another installer changed; Moonraker refuses to update it | `/print-firmware` shows what changed and how to fix it (above) |
| The printer does not report the update | Moonraker's remote is `?`, the GitHub release is newer | Update from Fluidd or Mainsail: Settings, Software Update |
| The release needs USB | Its first two numbers differ from the ZMod base you installed from USB | Insert a USB drive and run `ZFLASH` (`/print-firmware guide zmod`) |
| The base and component do not pair | The OS and `zmod` versions differ in their first two numbers | The base updates from USB first: `ZFLASH` |

The USB rule is ZMod's own: its `zversion.sh` prints "Update Z-Mod from
USB" when the first two numbers of the network-updated version
(`/usr/data/zmod/zmod/version.txt`) differ from the USB-installed one
(`/root/printer_data/version.txt`). Print Handy cannot read those files, but
it reads the same two facts through Moonraker: ZMod's `start.sh` writes the
OS name as `zmod <USB version> -> <current version>`, which
`/machine/system_info` reports, and the release names the target.

Only ZMod printers get alerts in this release: the AD5X, AD5M, AD5M Pro,
Creator 5 and Creator 5 Pro under ZMod (`flashforge.*`), and only for the
ZMod component. Generic Klipper printers, other Moonraker components
(Klipper, Moonraker, Fluidd/Mainsail, plugins) and every other family are
#1294: vendor Klipper forks (Elegoo Neptune 4, Creality, QIDI, Anycubic,
Snapmaker) are never apply-eligible, and Bambu Lab, PrusaLink, OctoPrint,
SDCP and stock Flashforge have no update-availability state Print Handy can
read (the matrix below).

## What "update ZMod from Print Handy" covers and does not cover

For a ZMod printer, in one place (verified end to end in
[#1270](https://github.com/catduckgnaf/print-handy/issues/1270)
against ZMod's own update instructions and the 1.7.6 release):

- **Covered, in Print Handy:** reporting the state the printer
  exposes (every Update Manager component, the OS ↔ `zmod` pairing,
  Klipper 13 MCU currency), and applying one Moonraker component
  update at a time behind a confirmation, guarded as below.
- **Not covered: a ZMod update itself.** ZMod updates from the
  Software Update section in Fluidd or Mainsail, with no USB drive,
  unless ZMod shows "Update Z-Mod via USB". That message marks a
  firmware-level update, which must go through USB (the `ZFLASH`
  macro or a manual USB install) even if you updated recently.
  Print Handy guides both routes (`/print-firmware guide zmod`) and
  performs none of either.
  The component apply path moves components **within** the installed
  base; it does not take a printer across a release step, and the
  pairing refusal exists because ZMod component updates fail when
  the base and the `zmod` component do not pair.
- **Not covered: the stock firmware underneath.** 1.7.6's "AD5X:
  support for firmware 3.1.8" is compatibility (ZMod 1.7.6 runs on
  stock 3.1.8), not a requirement: the pairing rule pairs the ZMod
  OS with the `zmod` component, not with the stock firmware.
  Updating the stock firmware stays a guided manual step
  (`/print-firmware guide stock`).
- **The `?` catch, in practice.** On the one live install shape
  documented here (an AD5X on ZMod 1.7.5-2), Moonraker reports `?`
  as the latest version for every **git** component (`zmod`,
  `klipper`, `moonraker`) because it cannot read those checkouts.
  For those components the apply path is unreachable in practice:
  Print Handy refuses them with the "latest version not reported"
  reason rather than guess. The path does work for a component that
  reports a real available version (the web-served Fluidd
  component is the known case on that install) and on any install
  whose Moonraker can read its checkouts. Whether a given install
  reports real versions is something only that install can show;
  the report states what it read either way.

## Safety rules (stage 2 apply)

- **Eligibility is a property of the printer profile**, from
  the printer registry (`print_handy/data/printers-registry/<vendor>.toml`): `firmware_family` is `zmod` or vanilla
  `klipper`, the connection is Moonraker, and the vendor is not a fork
  vendor. Fork vendors, Elegoo, Creality, QIDI, Anycubic, are refused
  by vendor, even though their printers speak Moonraker. A future model
  from a fork vendor is refused by the same rule until per-vendor
  evidence clears that vendor.
- **Idle only.** The printer's `print_stats` state is checked before
  the confirmation and again immediately before the upgrade POST.
  Printing, paused, an unrecognised state, or an unreadable state at
  either check refuses the update (fail closed). Moonraker itself also
  refuses update calls while printing; Print Handy refuses earlier and
  in plain language.
- **One component at a time**: ZMod's own guidance, and the only shape
  the confirmation offers.
- **Verified, never assumed.** After the POST, Print Handy re-reads the
  update status (with a short bounded wait for a printer that may be
  restarting) and reports exactly one of: updated to the new version;
  still showing the update as available (treat as failed); or the
  printer did not come back (unverified). A successful POST alone never
  counts as success. When an update does fail, ZMod's own recovery
  for a damaged mod is reinstalling it from USB (the printer's boot
  check says so and points at `ZFLASH`), which is the base guide's
  flow; Print Handy reports the failure and does not attempt a
  repair itself.
- **ZMod pairing blocks apply.** When the printer's OS version and its
  `zmod` component version disagree in their first two components
  (OS 1.8.x with ZMod 1.9.x, say), ZMod component updates fail, so
  Print Handy refuses and points at the base-update guide instead.

## The ZMod extras in the report

On ZMod printers the report adds two best-effort checks. Absent or
unreadable data is simply not reported, never presented as a mismatch.

- **OS ↔ `zmod` pairing**, from `GET /machine/system_info` (the OS
  distribution version) against the `zmod` component in the update
  status, per the pairing rule above.
- **Klipper 13 MCU currency**, comparing Klipper's `mcu` object
  (`mcu_version`, the firmware build running on the MCU) against the
  klipper host component's version. Only a Klipper 13 host (0.13.x) is
  judged, and only when both versions parse: differing release triples
  are reported with ZMod's `UPDATE_MCU` step named. This judgement is
  Print Handy's conservative reading of the documented fields, not a
  Klipper protocol guarantee. Print Handy never runs `UPDATE_MCU`
  itself. It is a printer-side step (ZMod's script also stops the
  native firmware service while it works), so after a `klipper`
  component update the sequence is: apply the component, re-run
  `/print-firmware`, and if the MCU reads out of step, run
  `UPDATE_MCU FORCE=13` at the printer, in that order. Name the
  version: ZMod's `UPDATE_MCU` without `FORCE` switches the MCU to
  the *other* firmware (on a Klipper 13 install, back to the native
  Klipper build), which is the step ZMod's wiki gives before removing
  the mod or updating the stock firmware, not after a Klipper 13
  update.

Not covered by any check: the ZMod base itself and the stock firmware
underneath it. The report on a ZMod printer says so and names the two
guides.

## Per-family matrix

| Family (registry keys) | What Print Handy can report | Apply? | How the update is actually done |
|---|---|---|---|
| **ZMod, AD5X, AD5M, AD5M Pro, Creator 5, Creator 5 Pro** (`flashforge.*`, Moonraker) | Every Update Manager component (`zmod`, `klipper`, `moonraker`, Fluidd/Mainsail, plugins incl. Print Handy's own), plus the OS↔`zmod` pairing check and Klipper 13 MCU currency | **Yes, components only**, guarded as above | Components: `/print-firmware update <component>`. ZMod itself: Software Update in Fluidd/Mainsail, no USB, unless ZMod shows "Update Z-Mod via USB"; then the `ZFLASH` macro (downloads the release to the USB drive, verifies MD5, installs on reboot) or a manual USB install, `guide zmod`. Stock firmware underneath: touchscreen / USB, `guide stock` |
| **Generic Klipper + Sovol SV06** (`klipper.generic`, `sovol.sv06`, Moonraker) | Every Update Manager component, as the operator configured it in `moonraker.conf` | **Yes**, same guarded path, minus the ZMod layers | `/print-firmware update <component>`; what it updates is the operator's own Update Manager configuration |
| **Elegoo Neptune 4 family** (`elegoo.neptune4*`, Moonraker, vendor fork) | The fork's component versions, information only | **No, hard excluded** | Elegoo's `ELEGOO_UPDATE_DIR` USB package via the touchscreen; touchscreen firmware separately via TF card. Elegoo warns that updating to official Klipper/Moonraker/Fluidd "will make the machine not work properly" |
| **Creality Klipper, K1 family, Ender-3 V3 family** (`creality.*`, vendor fork) | The fork's component versions, information only | **No, hard excluded** | Creality touchscreen OTA / Creality Cloud, or Creality's USB package |
| **QIDI, Q1 Pro, X-Plus 3/4, X-Max 3, X-Smart 3** (`qidi.*`, vendor fork) | The fork's component versions, information only | **No, hard excluded** | QIDI's official update file on a USB drive at the printer. Online-updating Klipper/Moonraker through the web UI is documented to break QIDI's board/display connections |
| **Anycubic Kobra 2 family** (`anycubic.kobra2*`) | Update state only where a Moonraker endpoint exists (community-firmware installs); stock KobraOS exposes none | **No, excluded** | Stock: Anycubic's USB `update/update.swu` package. Community installs: that firmware's own process |
| **Flashforge stock AD5X** (`flashforge_stock.ad5x`) | Nothing programmatic, the stock LAN API carries device info, not update state | No | Touchscreen Device Info → Firmware Update (OTA), IFS Update separately, or a firmware file on USB inserted while powered off, `guide stock` |
| **Bambu Lab, all models** (`bambulab.*`, LAN) | The installed firmware version from the LAN status; newer-version availability is a cloud/wiki fact | No | Cloud OTA (printer screen / Bambu Studio / Handy) with cloud mode on, or the manual offline SD-card upgrade. LAN-only mode, Print Handy's mode, receives no OTA |
| **Prusa, MK4S, CORE One, XL, MINI+** (`prusa.*`, PrusaLink) | The printer/firmware identity PrusaLink exposes; no update-availability state | No | Buddy firmware `.bbf` on a FAT32 USB drive, flashed at reboot from the printer's prompt |
| **Elegoo Centauri Carbon** (`elegoo_sdcp.centauricarbon`, SDCP) | No firmware-update state in the SDCP protocol | No | Touchscreen OTA when online, or the USB `update.bin` / `update/update.swu` package (also the rollback path) |
| **Marlin via OctoPrint** (`octoprint.*`) | The board's firmware name/version where the printer reports it (`M115` lineage) | No | Board flashing over serial by board-specific tooling, in OctoPrint, the Firmware Updater plugin. OctoPrint's own Software Update covers OctoPrint and its plugins, not the board |

## Out of scope, deliberately

- Updating Print Handy itself, its container, Spoolman or OrcaSlicer, 
  this feature is printer firmware only.
- Flashing Klipper MCU boards (no `make flash`, Katapult, DFU or serial
  flashing). The report surfaces an out-of-step MCU and names ZMod's own
  `UPDATE_MCU` step; that is all.
- Custom or community firmware installation (OpenNept4une, X1Plus,
  Klipper conversions), vendor-supported update paths only.
