# Forge-X printers

Flashforge Adventurer 5M and Adventurer 5M Pro printers running
[Forge-X](https://github.com/DrA1ex/ff5m), the unofficial firmware mod
from DrA1ex. Forge-X keeps the printer's stock-generation Klipper and
patches it in place, and adds Moonraker, Fluidd or Mainsail, and a
choice of screen modes (the stock screen, the lightweight Feather
screen, Guppy Screen, or headless). It is a second, mutually
incompatible custom firmware for hardware Print Handy already supports
on [ZMod](ZMOD.md): a printer runs one or the other, never both (do
not flash one mod over the other), and each firmware has its own Print
Handy model.

| Model | `[printer] model` | Build volume | Filament system | Status |
| --- | --- | --- | --- | --- |
| Flashforge Adventurer 5M | `flashforge_forgex.ad5m` | 220 x 220 x 220 mm | Single extruder | Supported (see the verification note below) |
| Flashforge Adventurer 5M Pro | `flashforge_forgex.ad5m_pro` | 220 x 220 x 220 mm | Single extruder | Supported (see the verification note below) |

The research behind these profiles is
[#1506](https://github.com/catduckgnaf/print-handy/issues/1506).

## Which Forge-X line these profiles target: the 1.4.2 beta only

These profiles support the **Forge-X 1.4.2 beta only**: the
`1.4.2` branch, or a 1.4.2 prerelease from beta-2 onward. **Stable
1.4.1 is not supported by these models.** That is a deliberate
choice, not an oversight: the beta is where Forge-X is moving (it
changes the default screen to Feather, adds the native timelapse and
accepts `.gcode.3mf` packages), it is the line the research in #1506
verified contract by contract, and supporting one line keeps every
contract in these profiles single and testable. Upstream's own
README calls 1.4.2 a major update that is already considered stable
enough for regular use, but it is still a beta, and beta software can
change the contracts below before it ships.

The promotion path is recorded here so nobody has to re-decide it
later: when upstream ships 1.4.2 as a stable release on its `main`
branch, support **moves** to that stable release and **stays** on
stable, the way Print Handy's OrcaSlicer support moved to one stable
channel. The beta targeting is dropped at that point, not kept as a
second channel, and no dual-line machinery is built in the meantime.

What the beta line means in practice:

- **Default screen: Feather.** The beta makes Feather the default
  screen and turns it into a full interactive screen. The other beta
  screen modes (the stock screen, Guppy, headless) exist, but screen
  mode changes mesh profile naming, Z-offset handling and
  filament-runout behavior, and per-screen-mode print start is
  follow-up work in #1506, not part of these profiles yet.
- **Delivery:** the beta accepts plain G-code and Orca's
  `.gcode.3mf` packages, but whether it accepts Print Handy's own
  ZMod-shaped package is not proven, so these profiles deliver plain
  G-code (see Delivery below).
- **Timelapse:** the beta adds a native Moonraker timelapse that
  handles frames itself, which is why the templates never write
  `TIMELAPSE_TAKE_FRAME` into a file.
- **Power-loss recovery** is on by default in the beta (on the
  non-stock screens).
- **Updates:** the `forge-x` update component on a beta install
  tracks the `1.4.2` branch. See the firmware-update posture below.

## Verification status: documented and config verified, not hardware verified

Every Forge-X contract in this page and in the profiles was checked
against the Forge-X documentation and the shipped config trees of
the `1.4.2` branch of the upstream
repository on 2026-11-15. No Forge-X printer has printed through
Print Handy, and nobody on this side has a Forge-X install to query.
Contracts that only a live printer can settle are named as open in
the research issue: the exact live shapes of the `mod_params`,
`timelapse_state` and power-loss objects, whether a USB-installed
printer's `forge-x` update component reports a readable version, and
the beta's `.gcode.3mf` acceptance of Print Handy's package. Treat
these profiles the way the ZMod profiles were treated before their
first live checks: the slicing path is tested offline, the printer
path is not yet proven.

## Install boundaries and warnings

- Forge-X installs the way stock firmware does: the release image for
  your exact model goes on a FAT32 USB drive, unopened, and the
  printer installs it at power-on. It is fully removable, and the
  printer can still boot the stock firmware.
- Verified stock firmware range: 2.6.5 to 5.1.x. You do not need to
  update or downgrade the stock firmware first within that range.
- **Warranty and damage warning, in upstream's own terms:** installing
  the mod risks voiding the warranty or damaging the printer, and a
  mistake can leave the printer unable to boot (recoverable, in rare
  cases only with additional hardware or soldering).
- **Full recalibration is mandatory.** After installing or
  uninstalling Forge-X, and after changing screen modes, check all
  printer parameters and perform a full recalibration (bed mesh and
  Z offset at minimum) before printing. Skipping it can damage the
  printer. Print Handy cannot do this recalibration for you.
- Do not stack mods: do not flash Forge-X over another mod, or another
  mod over Forge-X.

### Default SSH credential warning

Forge-X documents a root SSH login with a published, shared default
password (`root`) that its documentation prints. Until the owner
changes it, anyone on the network who has read the same public
documentation can log in as root. Change it as part of the install,
before the printer is used. Print Handy never needs the SSH login; it
talks to Moonraker only.

## How Print Handy talks to it

Standard Moonraker on port 7125, with no forced login in the shipped
config. Set `services.moonraker_url` to the printer. Print start is a
normal Moonraker `/printer/print/start`. Two upstream behaviors to
know:

- Moonraker can be deliberately stopped during a print from the
  printer side (`STOP_MOD`, a resource-saving feature on this
  128 MiB printer), so a Forge-X printer going quiet mid-print is not
  automatically a fault.
- Forge-X ships no `[spoolman]` section, so Spoolman is absent on the
  printer by default, unlike on ZMod. A user can enable it in their
  own Moonraker configuration; Print Handy does not assume it.

## Slicer differences from the ZMod AD5M profiles

Print Handy's Forge-X templates are the AD5M pair's templates with
these deltas, each from the Forge-X slicer documentation for the
1.4.2 beta:

| Setting | ZMod AD5M profiles | Forge-X profiles | Why |
| --- | --- | --- | --- |
| Machine start G-code | `START_PRINT ... MESH=auto` with adapter-added heating lines | The documented form (next section) | Both firmwares name the macro `START_PRINT`, but the parameters, their order and the fatal deprecated parameters differ |
| Label objects | Off | On (`gcode_label_objects`) | KAMP adaptive meshing needs labeled objects |
| Exclude objects | Off | On (`exclude_object`) | KAMP and the beta's exclude-object screen page need it |
| Layer-change G-code | Layer statistics plus `TIMELAPSE_TAKE_FRAME` | Layer statistics only | The beta's native timelapse handles frames itself; its slicer documentation says not to emit the frame call |
| G-code thumbnails | 140x110 | 512x512/PNG | The beta's Feather screen shows the slicer preview at that size |
| Use 3MF instead of G-code | On in the template | Off (`use_3mf`) | Delivery is plain G-code (next section) |
| Host type and printer agent | Not set in the template | Moonraker | The documented connection form |

### The documented start form

The machine start G-code Print Handy writes for these models is, in
full:

```gcode
SET_PRINT_STATS_INFO TOTAL_LAYER=[total_layer_count]
START_PRINT EXTRUDER_TEMP=[nozzle_temperature_initial_layer] BED_TEMP=[bed_temperature_initial_layer_single]
```

Machine end G-code is `END_PRINT`.

What is absent is the point:

- **No `MESH=`.** Forge-X's `MESH` parameter takes a bed-mesh profile
  name whose meaning depends on the screen mode (`MESH_DATA` on the
  stock screen, `auto` on the others, because the stock software owns
  that profile and the Z-offset handling there). ZMod's `MESH=auto`
  does not carry over, and the per-screen-mode start work is a
  follow-up tranche in #1506.
- **No `Z_OFFSET`, no `SKIP_ZOFFSET`.** Both are deprecated on
  Forge-X and fatal: passing either one cancels the print.
- **No `TIMELAPSE_TAKE_FRAME`** anywhere in the file (see the table
  above), and no `TIMELAPSE=` parameter in this tranche: the beta
  accepts one, and per-print timelapse choices are tranche 2 work.

`START_PRINT` must stay on a single line with both temperatures:
missing either one aborts the job. The stock screen's documented form
appends `M190` / `M104` heating lines for the stock screen's benefit;
these profiles use the form the other screens document, and
Forge-X's own start sequence heats the bed and nozzle itself.

Calibration note: Forge-X's own documentation says not to calibrate
with the raw Klipper `BED_MESH_CALIBRATE`, because it skips the
load-cell (weight sensor) reset. Use Forge-X's own calibration macros
and screens for bed mesh and Z offset on this firmware.

## Print start by screen mode

Forge-X documents two machine start forms, chosen by the printer's local screen (the ff5m 1.4.2 slicing guide):

| Screen (`display` setting) | Documented start G-code |
| --- | --- |
| Feather, Guppy or Headless | `START_PRINT EXTRUDER_TEMP=<nozzle> BED_TEMP=<bed>` on one line |
| Stock | the same line, then `M190 S<bed>` and `M104 S<nozzle>` |

Print Handy's Forge-X profiles slice the Feather, Guppy and Headless form. Print Handy reads the screen from Moonraker (`mod_params`, the `display` setting of Forge-X's `SET_MOD` system). If the printer reports the Stock screen, the start is refused with the reason: switch the display with `SET_MOD PARAM=display VALUE=FEATHER` (or `GUPPY` or `HEADLESS`). A missing or unrecognized value is not guessed in either direction.

The start line never carries `Z_OFFSET` or `SKIP_ZOFFSET`. Both are deprecated on Forge-X, and passing either cancels the print. Nor does it carry any ZMod start parameter. The two-way firmware mismatch warning (below) still applies.

Documented and config verified, not hardware verified (#1506). Calibration routing and timelapse are separate follow-ups.

## Delivery: plain G-code

These models deliver plain `.gcode`, always.
The ZMod delivery setting (`[printer] zmod_delivery_format`) belongs
to the ZMod vendor only and does not exist on this vendor, so the
ZMod version floor can never package a Forge-X job as `.gcode.3mf`.

That is the only safe default today: while the beta accepts Orca's
`.gcode.3mf` packages, its unpacking is undocumented and Print
Handy's ZMod-shaped package (built for ZMod's parser) is not proven
to be the same thing. Plain `.gcode` loses nothing on the beta.

One Forge-X delivery behavior to know: its MD5 gate is on by default.
If a delivered file carries an MD5 checksum line, Forge-X verifies it
at print start, and a file whose checksum no longer matches its
contents is cancelled and deleted by the printer. Print Handy does
not rewrite a delivered file under the same name, so a file it
uploaded stays consistent; if a file you delivered by hand vanishes
at print start, a stale checksum is the first suspect.

## Firmware mismatch warning

The two AD5M firmwares publish different Moonraker surfaces, so Print
Handy can name the likely cause when an AD5M model points at the
other firmware:

- A Forge-X model pointed at a ZMod printer: the printer reports
  ZMod firmware surfaces, which Forge-X never publishes, and
  readiness fails with a warning naming ZMod and the ZMod model keys.
- A ZMod AD5M model pointed at a Forge-X printer: a status sample
  carrying Forge-X evidence (the `mod_params` object, or the
  `forge-x` update component) produces the mirror warning naming
  Forge-X and the Forge-X model keys. This direction matters most:
  both firmwares use a `START_PRINT` macro, so without the warning a
  ZMod profile passed its readiness check on a Forge-X printer and
  could send a job whose start line the firmware interprets
  differently, including parameters Forge-X rejects fatally.

The check reads object and component names only, never unverified
inner fields, and absence of evidence is treated as absence: a plain
missing-object failure keeps its usual wording.

## Firmware updates: reporting only

Print Handy's posture for Forge-X updates is **reporting only**, and
it is recorded in the models' registry entries, not just here: their
`firmware_family` is `none`, with a Forge-X guidance entry in the
firmware-update machinery, so `/print-firmware` lists what the
printer's Moonraker update manager reports and Print Handy never
offers to apply it. That is deliberate (#1506, under #1280 section
I), for three reasons:

- The `forge-x` component (a git component, alongside `fluidd`,
  `mainsail` and `guppyscreen`; there is deliberately no `klipper` or
  `moonraker` component, because those update with the flashed image)
  tracks a branch tip (the `1.4.2` branch on a beta install), not
  release tags, so its reported versions are git describe strings,
  not release numbers.
- Whether a printer installed from the USB image reports a readable
  `forge-x` version at all is unverified; a version Moonraker reports
  as not reported is stated as not reported, never as up to date.
- Upstream's rule is that over-the-air updates are supported only
  within a version line; crossing lines (for example 1.4.x to a
  future 1.5.x) is a USB re-flash. Print Handy has not yet encoded
  that rule as a gate.

To update manually: within a version line, use Software Update in
Fluidd or Mainsail at the printer. Across lines, download the
Forge-X release image for your exact model and install it from a USB
drive at the printer. Guarded apply from Print Handy is tranche 3 of
#1506, gated on a live update-status capture from a real Forge-X
install.

## AD5M vs AD5M Pro on Forge-X

Same extruder, same 220 mm cube, and in the beta's shipped defaults
the two models' printer configurations are identical. The Pro adds
the enclosed frame, a camera and air filtration. Neither model has a
chamber heater or a chamber temperature sensor on Forge-X: the Pro
enclosure is passive, with circulation fans and a servo, so Print
Handy records no chamber ceiling for either model. The recorded
ceilings are the extruder maximum of 350 C and the bed maximum of
130 C, both sourced from the beta branch's shipped default config.

## Calibration

`/print-calibration` on a Forge-X printer uses Forge-X's own macros,
from the 1.4.2 beta's calibration guide and macro files. Each driven run
keeps Print Handy's usual pattern: current values first, a confirmation,
the run only while the printer is idle, then the new values and an
Accept or Discard before anything is saved.

| Calibration | How Print Handy serves it on Forge-X |
| --- | --- |
| Full bed mesh | `AUTO_FULL_BED_LEVEL PROFILE=print_handy EXTRUDER_TEMP=220 BED_TEMP=60`: cleans the nozzle, tares the load cell and probes into a scratch profile. Accept copies it to `auto` and runs `SAVE_CONFIG`; Discard reloads the previous mesh and saves nothing. Never a bare `BED_MESH_CALIBRATE`, which skips the cleaning and the tare. Run only on Feather, Guppy or Headless; on the Stock screen (its own `MESH_DATA` profile) or an unreadable screen mode, the mesh is reported instead. |
| Extruder and bed PID | Raw `PID_CALIBRATE` at the temperature you pick. Never `PID_TUNE_EXTRUDER` or `PID_TUNE_BED`. Accept is `SAVE_CONFIG`, Discard is `FIRMWARE_RESTART`. |
| Input shaper | `FORGE_SHAPER_CALIBRATE`. It applies its recommendation in memory, so Accept is `SAVE_CONFIG` and Discard is `FIRMWARE_RESTART`, which drops it. Graphs stay under mod_data in Fluidd or Mainsail. |
| Bed screws | A guide: `BED_LEVEL_SCREWS_TUNE` is turned by hand at the printer. |
| Z offset | A guide: Feather's Z Offset paper test, or `SET_GCODE_OFFSET` plus `SET_MOD PARAM=load_zoffset VALUE=1`. Print Handy reads whether `load_zoffset` is on and never puts `Z_OFFSET` or `SKIP_ZOFFSET` on a start line. |

A calibration only appears when the printer defines its macro.

## Timelapse

Forge-X's native timelapse is switched on at the printer
(`SET_MOD PARAM=timelapse VALUE=1`), and finished videos appear in
Mainsail's Timelapse view. `[printer] forgex_timelapse` controls the
prints Print Handy slices: `printer` (the default) sends nothing and the
printer's switch decides; `off` adds `TIMELAPSE=0` to the file's single
`START_PRINT` line, so those prints make no timelapse. Forge-X cannot be
told to make a timelapse for one print, so there is no `on`. A file with
no `START_PRINT` line, more than one, or one that already names
`TIMELAPSE` is refused rather than guessed at. Print Handy does not yet
collect Forge-X's own timelapse videos; its Discord timelapse is its
separate camera recording.

## What is not supported yet

Scoped out so far (#1506 names the follow-up tranches):

- The Stock screen's print start (its extra heating lines): Stock-screen
  prints are refused with the fix named, see above.
- Collecting Forge-X's own timelapse videos into chat.
- Guarded firmware apply, update alerts and a firmware watch entry:
  tranche 3, under #1280 section I.
- Forge-X on any other printer: upstream has not ported it to the
  AD5X or the Creator line, so no Print Handy key implies it.
- Driving the USB full-image update flow from Print Handy: that flow
  is a guided manual install at the printer, at most.

## Choosing between the AD5M firmwares

Same printer, two firmwares, two model keys. If the printer runs
ZMod, use `flashforge.ad5m` or `flashforge.ad5m_pro` and see
[ZMod printers](ZMOD.md). If it runs Forge-X, use the keys in the
table at the top of this page. The firmware mismatch warning above
exists for the day the wrong one is picked.
