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
  resulting versions are verified.

There is no automatic applying and no background updater. Availability
may be reported; only a person confirms an update.

When Moonraker reports `?` for a component's latest version (a live
AD5X on ZMod 1.7.5-2 does this for every git component), that row says
"latest version not reported" and the report says Print Handy cannot
tell whether an update exists. It never says "up to date" for such a
component, and `/print-firmware update` refuses it rather than guess.

## What "update ZMod from Print Handy" covers and does not cover

For a ZMod printer, in one place (verified end to end in
[#1270](https://github.com/catduckgnaf/print-handy/issues/1270)
against ZMod's own update instructions and the 1.7.6 release):

- **Covered, in Print Handy:** reporting the state the printer
  exposes (every Update Manager component, the OS ↔ `zmod` pairing,
  Klipper 13 MCU currency), and applying one Moonraker component
  update at a time behind a confirmation, guarded as below.
- **Not covered: a ZMod release step.** Going from one ZMod release
  to the next (1.7.5 to 1.7.6, say) is a **base** update, not a
  component update. ZMod's own update instructions install the
  release from USB first (the `ZFLASH` macro or a manual USB
  install) and use the Software Update section in Fluidd or
  Mainsail for component updates afterwards. Print Handy guides the
  base step (`/print-firmware guide zmod`) and performs none of it.
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
  `UPDATE_MCU` at the printer, in that order.

Not covered by any check: the ZMod base itself and the stock firmware
underneath it. The report on a ZMod printer says so and names the two
guides.

## Per-family matrix

| Family (registry keys) | What Print Handy can report | Apply? | How the update is actually done |
|---|---|---|---|
| **ZMod, AD5X, AD5M, AD5M Pro, Creator 5, Creator 5 Pro** (`flashforge.*`, Moonraker) | Every Update Manager component (`zmod`, `klipper`, `moonraker`, Fluidd/Mainsail, plugins incl. Print Handy's own), plus the OS↔`zmod` pairing check and Klipper 13 MCU currency | **Yes, components only**, guarded as above | Components: `/print-firmware update <component>`. ZMod base: USB or the `ZFLASH` macro (downloads the release to USB, verifies MD5, installs on reboot), `guide zmod`. Stock firmware underneath: touchscreen / USB, `guide stock` |
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
