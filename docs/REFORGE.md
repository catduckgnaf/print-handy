# Reforge printers

Flashforge Creator 5 and Creator 5 Pro printers running
[Reforge](https://github.com/Klipper4FlashForge/firmware), the custom
firmware from Klipper4FlashForge. Reforge replaces the Creator 5's
application layer with a Klipper fork, Moonraker, Mainsail and Fluidd,
keeps the stock MCU firmware, and drives the four toolheads with its own
`ff_toolchange` Klipper module, with AFC on top for tool mapping, backup
heads and Spoolman. It is the third custom firmware for hardware Print
Handy already supports on [ZMod](ZMOD.md) and [OpenCreator](OPENCREATOR.md).
A printer runs exactly one of them, and each has its own Print Handy model.

| Model | `[printer] model` | Build volume | Filament system | Status |
| --- | --- | --- | --- | --- |
| Flashforge Creator 5 | `flashforge_reforge.creator5` | 256 x 256 x 256 mm | 4 independent toolheads | Supported from published config (see below) |
| Flashforge Creator 5 Pro | `flashforge_reforge.creator5_pro` | 256 x 256 x 256 mm | 4 independent toolheads | Supported from published config (see below) |

## Verification status: built from published config, not hardware verified

Reforge support in Print Handy is built from Reforge's published config
and documentation, and it has not been confirmed on real hardware. Every
contract on this page was read from the Reforge repository at master
commit `eda86c1` (2026-11-04) on 2026-11-17: the Klipper extras
`ff_print.py` and `ff_toolchange.py`, the shipped config files
`printer.base.cfg`, `ff-print-macros.cfg`, `ff-afc.cfg`, `ff-chamber.cfg`
and `chamber/Creator5Pro.cfg`, the project's Moonraker config, and its
docs (`how-a-print-runs.md`, `printing.md`, `toolchange.md`,
`upgrading.md`). AFC's object shapes were read from the
AFC-Klipper-Add-On commit Reforge pins (`8ba0f7bf`). The HelixScreen
project's Creator 5 custom firmware research and its Reforge tracking
issue ([prestonbrown/helixscreen#2027](https://github.com/prestonbrown/helixscreen/issues/2027))
were the map. No Reforge printer has printed through Print Handy.

Reforge calls itself beta in its own changelog, and its AFC layer is
marked "installs and loads in the replica; not yet run on a printer" by
the project itself. Treat these profiles the same way.

## Which Reforge builds

Print Handy's Reforge profiles need a Reforge build with **AFC**: the
`afc-migration` work merged on Reforge master on 2026-11-04. The tagged
releases up to `v20260827f-melitopol` (2026-09-24) carry no AFC objects,
and Print Handy reads the tool map and each head's filament switch from
AFC. On a build without AFC, readiness refuses the start and says AFC is
not reporting. It does not fall back to guessing.

## How Print Handy talks to it

Standard Moonraker on port 7125, with Reforge's shipped config trusting
LAN clients and no API key. Set `services.moonraker_url` to the printer.
Print start is a normal Moonraker `/printer/print/start`. Reforge sets
Moonraker's machine provider to `none`, so host reboot and service
controls that expect Moonraker's machine API do not answer.

## How a print starts on Reforge

Reforge's `[ff_print]` module wraps `SDCARD_PRINT_FILE`. Before the file
runs, it reads four values out of the file itself (the first 256 KiB of
it) and calls `FF_BEFORE_PRINT_START` with them:

| Value | Read from | If missing |
| --- | --- | --- |
| Bed | the first `M140` or `M190 S<t>` line, whole degrees, above 0 only | the bed is not heated by the preparation |
| Nozzle | the first `M104` or `M109 S<t>` line, whole degrees, above 0 only | `START_PRINT` uses its default of 220 C |
| First tool | the first bare `T0` to `T3` line | no tool is checked, cleaned or picked up first |
| First layer | the first `;HEIGHT:` line | the print Z offset loses its first-layer term |

`FF_BEFORE_PRINT_START` runs `_FF_PREFLIGHT` (calibration and tool
presence) and then, while its `prepare` variable is 1, `START_PRINT`:
home, heat the bed, purge and wipe the first tool, wait for the bed,
re-home Z, load the mesh, pick up the first tool and set the print
offset. When the job leaves the printing state, `FF_AFTER_PRINT_END`
runs `END_PRINT`, which turns the heaters off, drops the bed, docks the
tool and resets AFC's tool map (`reset_tool_map`, on by default).

### The start form Print Handy writes

```gcode
SET_PRINT_STATS_INFO TOTAL_LAYER=[total_layer_count]
;HEIGHT:[initial_layer_print_height]
M140 S[bed_temperature_initial_layer_single]
M104 S[nozzle_temperature_initial_layer]
M191 S[overall_chamber_temperature]
M106 P2 S0 ; chamber loop and cool fans off
M106 S0 ; part cooling fan off
M106 P3 S0 ; chamber fan off
G90
M83
T[initial_extruder]
```

Every value `ff_print` reads is on its own plain line, so the printer
prepares with the job's own temperatures, tool and first-layer height.
The `;HEIGHT:` line is the same form Reforge's own feature-test G-code
uses. The final `T` line is AFC's tool select; the preparation has
already picked that head up, so it changes nothing. There is no
`START_PRINT` and no `G28`: Reforge runs `START_PRINT` itself, and its
`G28` docks a mounted tool before homing Z, so a `G28` in the file would
put back the tool the preparation just picked up. The base Creator 5 has
no chamber heater, so its form has no `M191` line. Machine end G-code is
empty: print end is Reforge's.

Verification models the same reading. A file is refused, before it is
sent, when the nozzle, bed, first tool or first-layer line is missing,
when the nozzle or bed `ff_print` would read differs from the job (the
firmware reads whole degrees, so a 215.5 C job is refused rather than
printed at 215), when the header carries its own `START_PRINT` or a
`G28`, or when a Pro's chamber target is above its ceiling.

### Slicer differences from the ZMod Creator profiles

| Setting | ZMod Creator profiles | Reforge profiles |
| --- | --- | --- |
| Machine start G-code | `START_PRINT ... MESH=auto` form | The form above |
| Machine end G-code | `END_PRINT` | Empty |
| Label objects and exclude objects | Off | On (Reforge ships `[exclude_object]`) |
| Use 3MF instead of G-code | On in the template | Off: delivery is plain `.gcode` |
| Host type and printer agent | OctoPrint-shaped values from upstream profiles | Moonraker |

## Readiness

Before a start, Print Handy reads the printer and refuses, naming the
cause, when:

- **The tool changer state is not ok.** `ff_toolchange` must report
  `state_ok`; otherwise its `state_reason` (the dock and grab sensors
  disagree) is shown. A tool change in progress (`toolchanger` status
  `changing`) also refuses.
- **A used head is missing or uncalibrated.** Every head the job uses,
  through the printer's AFC map, must be in its dock or mounted, and in
  `calibrated_tools`, and the calibration station must have a Z. These
  are the checks Reforge's own `_FF_PREFLIGHT` makes; Print Handy makes
  them first so the refusal comes before the upload.
- **A used head has no filament.** Each used head's
  `AFC_extruder e<n>` `tool_start_status` must read true. Unknown is a
  refusal.
- **The automatic preparation is off.** `ff_print` must report, and
  `FF_BEFORE_PRINT_START` must report `prepare` 1 and `defer_mesh` 0. With
  `prepare` off nothing homes before the file starts; with `defer_mesh` on
  the mesh, first tool and print offset are left to an `ADAPTIVE_MESH`
  call Print Handy's files do not make.
- **AFC is not ready.** The `AFC` object must report with no error
  state, the tool map must give every tool exactly one lane and one head,
  and an ACE unit is refused by name, as on OpenCreator.
- **The Pro's chamber heater is not reporting** (`heater_generic chamber_heater`).
- **The printer runs another Creator firmware.** A `zmod_color` or
  `creator5_toolchanger` object with no Reforge object names ZMod or
  OpenCreator and their model keys.

### The tool map

Reforge's AFC owns `T0` to `T3`: lane `e<n>` feeds head `n`, and
`SET_MAP` can move a slicer tool to another head. Print Handy follows
the printer's map, exactly as on OpenCreator: a remapped printer is
accepted, each tool's checks apply to the head it will really print on,
and Print Handy never sends `SET_MAP`. Reforge ships AFC's multiple tool
mapping switched on, so that switch alone is not a refusal (it is on
OpenCreator); a map is refused only when two tools would land on one
head, or a tool has no lane or more than one.

## Firmware mismatch and detection

The three Creator firmwares publish different objects. Reforge's are
`ff_toolchange` and `ff_print`, its own Klipper extras. Stock FlashForge
Klipper has neither (the stock touchscreen does the tool changes), and
its own `ff_eddy` sections are not evidence. Reforge also runs AFC, so
an AFC object alone no longer counts as OpenCreator evidence when a
Reforge object is present.

- Guided setup reads `/printer/objects/list` and offers the Reforge
  model, keeping base versus Pro, only when Reforge's objects are the one
  firmware's evidence present. Nothing changes without confirmation.
- A ZMod or OpenCreator Creator model pointed at a Reforge printer gets
  a readiness warning naming Reforge and its model keys, and the reverse
  holds for a Reforge model.
- `print-handy doctor` shows the result on its `creator firmware` row.

## Safety guards, the same as OpenCreator

- **Macros:** `/print-macros` lists Reforge's read-only
  `TOOLCHANGE_STATUS` and `TOOL_OFFSET_STATUS`, and `/print-macros run`
  is refused for every macro on these models.
- **Calibration:** Print Handy does not drive calibrations on Reforge.
  Tool offsets, the station, PID and the mesh are Reforge procedures run
  at the printer (Reforge's `calibration.md`).
- **Spools:** spool writes to Moonraker's `lane_data` are refused by
  name. AFC makes a head's Spoolman spool active when that head is picked
  up, so with Moonraker's Spoolman link on the printer counts filament
  itself.
- **Bed-clear, idle and confirmation checks** are the shared ones every
  Moonraker printer gets; nothing about Reforge skips them.

## Firmware updates

Print Handy never installs a Reforge update. A Reforge update is a
release package for the exact model on a FAT32 USB stick, installed at
the printer like the first flash, and Reforge's Moonraker config carries
no Update Manager entries, so there is nothing for Moonraker to apply.
Your `printer.cfg` and `moonraker-custom.conf` survive an update; the
`ff-*.cfg` files are replaced (Reforge's `upgrading.md`).

## Creator 5 vs Creator 5 Pro on Reforge

Same toolheads, same 256 mm cube. The Pro has the chamber heater
(`[heater_generic chamber_heater]`); the base model has only a chamber
temperature sensor, and Reforge's `M141`/`M191` macros ignore a chamber
target on it.

| Heater | Ceiling Print Handy enforces | Models |
| --- | --- | --- |
| Extruder | 320 C | Both |
| Bed | 120 C | Both |
| Chamber | 65 C | Pro only |

These are FlashForge's ratings. Reforge's Pro config sets the chamber
heater `max_temp` to 80 C, 15 C above the rating, so Print Handy's 65 C
ceiling is what holds a chamber target to the rated maximum.

## What is not supported yet

- Reforge builds without AFC (the tagged releases up to 2026-09-24).
- A custom `[ff_print] before_macro`: Print Handy checks the shipped
  `FF_BEFORE_PRINT_START` and cannot tell when another macro replaces it.
- Driving the sequence from the slicer (`prepare` 0 with an explicit
  `START_PRINT TOOLS=...` line), Reforge's `ADAPTIVE_MESH` path, and its
  per-tool `TOOLS=` clean temperatures. Print Handy's start form gates
  and cleans the first tool through the automatic path; Reforge's own
  `_FF_PREFLIGHT` checks only that tool, and Print Handy's readiness
  checks every used head before the start.
- Release notices for Reforge in the firmware feed.
- Calibration, load and unload, tool changes and any other command that
  moves or heats the printer.

## Choosing between the Creator firmwares

Same printer, three firmwares, three model keys. ZMod is
`flashforge.creator5` or `flashforge.creator5_pro` ([ZMod printers](ZMOD.md)),
OpenCreator is `flashforge_opencreator.creator5` or
`flashforge_opencreator.creator5_pro` ([OpenCreator printers](OPENCREATOR.md)),
and Reforge is the pair at the top of this page.
