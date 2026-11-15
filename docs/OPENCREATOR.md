# OpenCreator printers

Flashforge Creator 5 and Creator 5 Pro printers running
[OpenCreator](https://opencreator.readthedocs.io/en/latest/), the custom
firmware from the FlashForge-C5-Modding-Group. OpenCreator replaces the
stock host software with Kalico (a Klipper fork) and upstream Moonraker,
with Fluidd or Mainsail as the web UI, an AFC standalone toolchanger for
the four toolheads, and Moonraker Timelapse. It is a second, mutually
incompatible custom firmware for hardware Print Handy already supports
on [ZMod](ZMOD.md): a printer runs one or the other, never both, and
each firmware has its own Print Handy model.

| Model | `[printer] model` | Build volume | Filament system | Status |
| --- | --- | --- | --- | --- |
| Flashforge Creator 5 | `flashforge_opencreator.creator5` | 256 x 256 x 256 mm | 4 independent toolheads | Supported (see the verification note below) |
| Flashforge Creator 5 Pro | `flashforge_opencreator.creator5_pro` | 256 x 256 x 256 mm | 4 independent toolheads | Supported (see the verification note below) |

The research behind these profiles is
[#1490](https://github.com/catduckgnaf/print-handy/issues/1490).

## Verification status: source-verified contracts, not hardware verified

Every OpenCreator contract in this page and in the profiles was checked
against the OpenCreator documentation site and the shipped config tree
in the upstream `opencreator-fs` repository on 2026-11-14. A second
research pass the same day read the firmware's own source (the
OpenCreator fork of Kalico, default branch at the commit the 0.0.7.4
release ships, and upstream Kalico) and source-verified the contracts
tranche 1 had to leave open: the dispatcher's synthesized print start,
the `creator5_toolchanger` status shape, and the AFC lane objects and
their map. Those contracts are graded source-verified below. No
OpenCreator printer has printed through Print Handy, and nobody on
this side has an OpenCreator install to query, so nothing here is
hardware verified yet. The one contract source could not settle is
the `lane_data` record shape the firmware publishes for OrcaSlicer
filament sync (the sources disagree three ways; see the research
issue). Treat these profiles the way the ZMod profiles were treated
before their first live checks: the slicing path is tested offline,
the printer path is not yet proven.

## Public alpha: upstream's own warning

OpenCreator calls itself public alpha and development software on its
home page, its firmware page and its install page. Its own words: do
not treat any full custom firmware from the project as complete, and
outside reviews or videos may be out of date or not working. The first
public release (0.0.1) was 2026-10-16 and releases have landed near
daily since. Several features are still marked Testing, Untested or
Planned upstream, including filament runout switchover (Untested) and
ACE Pro multiplexing (Planned). Print Handy support is therefore an
opt-in firmware variant you select on purpose with the model keys
above, never a silent extension of the ZMod Creator profiles.

## Editions

| Edition | What it is | Print Handy position |
| --- | --- | --- |
| OpenCreator CFW | Kalico, Moonraker and a web UI on the printer's own computer; the installer replaces the stock host software end to end and flashes all four MCU boards | The edition these profiles target |
| OpenCreator for SBC | The same stack on a Raspberry Pi or other SBC, with the printer's boards tunneled over one USB gadget cable; Moonraker's address is the SBC's address | Same profiles; point `services.moonraker_url` at the SBC |
| OpenCreator Legacy | The stock FlashForge stack plus comfort mods; upstream says it no longer gets meaningful updates or Discord support | Not an OpenCreator target here: a Legacy printer is a stock printer for Print Handy's purposes, and there is no stock Creator profile yet |

## Install boundaries

- One touchscreen installer from a USB drive. It accepts only a
  Creator 5 (product ID 0028) or a Creator 5 Pro (product ID 0029) on a
  supported kernel package, and stops without changing anything
  otherwise.
- The claimed FlashForge firmware range is 1.7.8 to 2.0.0 with no
  update or downgrade needed first. The real gate is the kernel
  package (2.0.1 to 2.0.8): a future FlashForge release with a new
  kernel package needs an installer update before OpenCreator can be
  installed on it.
- The installer backs everything up before changing anything, and
  offers a full rollback to stock. Keep the backup: this is alpha
  software, and rollback is the recovery path.
- Install takes about 15 to 30 minutes, mostly backup, and the printer
  needs internet during install.

### Default SSH credential warning

The installer adds a root SSH login named `opencreator` with a
published, shared default password that the OpenCreator documentation
prints and tells users to change with `passwd`. Until the owner changes
it, anyone on the network who has read the same public documentation
can log in as root. Change it as part of the install, before the
printer is used. Print Handy never needs the SSH login; it talks to
Moonraker only.

## How Print Handy talks to it

Standard upstream Moonraker on port 7125, with the shipped
authorization trusting every LAN client (the slicer guidance says to
remove serial numbers and API keys; do not expect per-client
authentication on the printer). Set `services.moonraker_url` to the
printer (CFW) or to the SBC (SBC edition). Print start is a normal
Moonraker `/printer/print/start`. Two upstream differences to know:

- The machine provider is disabled and replaced by an OpenCreator
  Moonraker component for host reboot, shutdown and service restarts,
  so host power features that expect Moonraker's own machine API may
  not answer the usual way.
- The job queue defaults to not loading on startup and not advancing
  automatically; a queued job is started from Fluidd or Mainsail.

## Slicer differences from the ZMod Creator profiles

Print Handy's OpenCreator templates are the Creator pair's templates
with these deltas, each from the OpenCreator slicer documentation:

| Setting | ZMod Creator profiles | OpenCreator profiles | Why |
| --- | --- | --- | --- |
| Machine start G-code | `START_PRINT ... MESH=auto` form | The documented minimal form (next section) | OpenCreator has no `START_PRINT`; its start macro is synthesized by the firmware from the file |
| Label objects | Off | On (`gcode_label_objects`) | The firmware's adaptive mesh (KAMP) and per-tool purge lines use object labels |
| Exclude objects | Off | On (`exclude_object`) | Object exclusion from the web UI and screen needs it |
| Arc fitting | Off | Off (unchanged) | Arcs are not supported under Klipper |
| Retraction and deretraction speed | 30 | 45 on all extruders | Documented optional nicety (faster, little stringing in upstream testing) |
| Z-hop height | 0.4 | 0.3 on all extruders | Documented optional nicety (less Z stress, slightly faster) |
| Use 3MF instead of G-code | On in the template | Off (`use_3mf`) | Delivery is plain G-code (next section) |
| Host type and printer agent | OctoPrint-shaped values inherited from upstream profiles | Moonraker | The documented connection form |
| Machine end G-code | `END_PRINT` | Empty | Print end is firmware-owned; see below |

### The documented start form

The machine start G-code Print Handy writes for these models is, in
full:

```gcode
SET_PRINT_STATS_INFO TOTAL_LAYER=[total_layer_count]
M140 S[bed_temperature_initial_layer_single]
M191 S[overall_chamber_temperature]
M106
M106 P2 S0 ; center fresh air input fan off
M106 S0 ; model fan off
M106 P3 S0 ; filter unit fan off
G90
M83
```

What is absent is the point. There is no print-start macro line and no
nozzle heating line: a patched virtual_sdcard in Kalico synthesizes
`C5_PRINT_START` from the file's first and last 64 KB (first tool
command, first bed and nozzle heating lines, and Orca's config block)
when the file has no explicit line. Writing an explicit `C5_PRINT_START`
into the start G-code would replace the synthesized one and lose the
firmware's per-tool material, used-tools and idle-temperature filling,
so Print Handy never writes one. The form above is the Pro's: the base
Creator 5 has no chamber heater, so its start form omits the `M191`
line entirely and Print Handy never writes a chamber command for it.
The file header G-code is empty, because the FlashForge profile's
header time estimate otherwise hides the real estimate in Fluidd and
Mainsail.

Print Handy's verification models the same synthesis (source-verified
against the fork): the bed from the first `M140` or `M190`, the chamber
from the first `M141` or `M191`, the tool from the first `T0` to `T3`
line or the first used slot in Orca's `filament used [mm]` list, and
the hotend from the first `M109`, else the first `M104`, else the
selected tool's `nozzle_temperature_initial_layer` in the config block.
The synthesized command names the used tools as `USED_TOOLS=`, passes
Orca's first `first_layer_height` as `FIRST_LAYER_HEIGHT=`, and fills
`MATERIAL0` through `MATERIAL3` from the last `filament_type` line in
the footer (falling back to the header). Material entries are
upper-cased only when they match the firmware's allowed character set;
quoted names are unquoted first, and invalid or missing slots become
`UNKNOWN`. Verification's synthesized-start model includes these values
alongside the temperature and tool fields. A file
with no discoverable hotend temperature fails verification with the
firmware's own failure mode named, because that is what the printer
would do with it: fail before any motion. An explicit `C5_PRINT_START`
line in the file opts out of synthesis, and verification then checks
that line's own values instead.

Upstream's own reference for print-start configuration is the
OpenCreator Send plugin (see the scoped-out list below for why Print
Handy does not adopt it): it mirrors this same synthesis, and when an
operator changes a per-print option it writes the explicit
`C5_PRINT_START` line at the top of the file with the option switches
(`FLOW_CALIBRATION`, `PURGE`, `PURGE_LINE`, `PURGE_LINE_ALL_TOOLS`,
`BED_LEVELING`, `HOST_PRESSURE_ADVANCE`, and the build plate check)
set on it, which is exactly the opt-out above. Where the plugin's
mirror and the fork's dispatcher differ, the fork's current default
branch is the authority this page and Print Handy's model follow.

Machine end G-code is empty. Print end is firmware-owned
(`C5_PRINT_STOP`), there is no `END_PRINT` on this firmware, and the
one line the slicer documentation says to remove from the stock
profile's end G-code (`G1 X150 Y150 E-1.2 F12000`) is not present.

## Delivery: plain G-code, and what happens to 3MF uploads

These models deliver plain `.gcode`, always. The ZMod delivery setting
(`[printer] zmod_delivery_format`) belongs to the ZMod vendor only and
does not exist on this vendor, so the ZMod version floor can never
package an OpenCreator job as `.gcode.3mf`.

That matters because of what the firmware does with 3MF uploads: a
Moonraker component intercepts them and repacks the upload as a UFP
package carrying only the lowest-numbered plate's G-code and its
thumbnail. A ZMod-shaped `.gcode.3mf` package would survive as a print,
but its `slice_info.config` color and metadata work would be silently
discarded. Plain `.gcode` loses nothing on this firmware.

## Firmware mismatch warning

The two Creator firmwares publish different Moonraker objects, so
Print Handy can name the likely cause when a Creator model points at
the other firmware:

- An OpenCreator model pointed at a ZMod printer: the printer reports
  `zmod_color`, which OpenCreator never publishes, and readiness fails
  with a warning naming ZMod and the ZMod model keys.
- A ZMod Creator model pointed at an OpenCreator printer: a status
  sample carrying OpenCreator evidence (the `creator5_toolchanger`
  object, AFC objects, or the `opencreator` update component) and no
  `zmod_color` produces the mirror warning naming OpenCreator and the
  OpenCreator model keys.

The check reads object names only, never unverified inner fields, and
absence of evidence is treated as absence: a plain missing-object
failure keeps its usual wording.

## Persistent feature switches (the Misc switches)

OpenCreator keeps 13 virtual Misc switches that persist across
restarts, settable from Fluidd, Mainsail or `SET_PIN`, and listed by
`C5_MISC`. Per-print `C5_PRINT_START` parameters win over the switches
for that print only. Defaults, from the shipped config:

| Switch | Default | What it controls |
| --- | --- | --- |
| `flow_calibration` | On | Flow (pressure advance) calibration at print start; the upstream toggle page calls it known to be extremely inaccurate and recommends it off on first setup |
| `purge` | On | Purge each prepared tool into the bucket at print start |
| `purge_line` | On | The purge line in front of the print, separate from the bucket purge |
| `purge_line_all_tools` | Off | Every used tool draws its own purge line |
| `bed_leveling` | On | A fresh adaptive mesh before each print; off loads the saved default mesh |
| `bed_soak` | Off | Wait at bed temperature before a fresh mesh (2 minutes when on; skipped below 45 C) |
| `clog_detection` | Off | Pause when a tool's filament motion sensor stops while filament is loaded |
| `lower_bed_on_end` | Off | Lower the bed to the bottom after the print |
| `chamber_wait` | Off | On: `M191` blocks until the chamber reaches target. Off: it sets the target and printing continues |
| `door_notify` | Off | Pop-up and beep when a door opens during a print, or a print starts with one open |
| `quiet_mode` | Off | Silences the buzzer |
| `chamber_heat_fan_full` | Off (Pro only) | Chamber heater fan at 90 percent while heating instead of slowing near target |
| `host_pressure_advance` | Off | The experimental host pressure advance test at print start |

Print Handy does not change these switches. They are recorded here
because they change what a print start does on the printer, in ways a
Print Handy operator will otherwise meet unexplained.

## Creator 5 vs Creator 5 Pro on OpenCreator

Same toolheads, same 256 mm cube. The base printer has no chamber
heater, no chamber fans and no aux fan; all of that is Pro-only, and
the slicer start form's P2/P3 fan lines are no-ops or graceful
messages on a base printer (its start form carries no chamber line at
all; see the start form section).

### Temperature ceilings (manufacturer ratings)

FlashForge's own specifications table for the Creator 5 Pro
(flashforge.com, read 2026-11-14) rates the heaters at:

| Heater | Ceiling | Models |
| --- | --- | --- |
| Extruder | 320 C | Both |
| Bed | 120 C | Both |
| Chamber | 65 C | Pro only |

Those ratings are the ceilings Print Handy records and enforces for
these models. One upstream warning travels with them: OpenCreator's
shipped Pro config sets the chamber heater `max_temp` to 80 C, 15 C
above the manufacturer rating, and the firmware clamps a job's chamber
target to that config value at print start. Print Handy's 65 C ceiling
is therefore the only thing holding a chamber target to the rated
maximum on this firmware; do not read the config's 80 C as headroom.
It is held in two places: the controls, macros and preheat refuse a
chamber target above it, and verification refuses a Pro file whose
start would run the chamber above it (the file's first `M141` or
`M191`, or the chamber value the firmware would take from the file's
settings block). The base Creator 5 has no chamber heater, so no
chamber ceiling applies to its files.

### Readiness, the active tool and the AFC lane map

Readiness and status ride on the `creator5_toolchanger` object
(source-verified against the fork): its status is exactly
`active_tool` (an integer 0 to 3 from the physical dock and grab pins,
or None), `sensor_error` (a string or None), `busy`,
`build_plate_detected`, `dock_positions` and `tool_measurements`. A
non-null `sensor_error` is a readiness problem naming the sensor
disagreement. `active_tool` None with no sensor error means every tool
is docked, the normal idle state, and is not a problem: print start
picks up the tool it needs. A `busy` toolchanger refuses the start
until it finishes. `build_plate_detected` is not read for readiness:
it is the result of the last plate check, so a stale value proves
nothing, and print start runs its own check after homing. Print
Handy's status reports the active tool from this object.

AFC is part of readiness as well: the `AFC` object must be reporting
with no error state, and no toolhead's `AFC_extruder` object may
report a mount sensor error.

The printer's AFC lane map is part of readiness too. Inside
`C5_PRINT_START`, the firmware remaps the file's tool numbering
through that map. Each toolhead is configured as an `AFC_extruder
extruder0` to `extruder3` section mapped to a `T` command, and the
firmware reports the live map on the matching lane objects, `AFC_lane
extruder0` to `extruder3` (the `AFC_extruder` status itself carries no
map, so Print Handy reads the lane objects). The shipped map is the
identity, extruderN to `T<N>`. Print Handy's Creator contract is
identity-only, so a non-identity map refuses the start with a message
naming the actual mapping, rather than letting the printer remap a
job invisibly. Print Handy never sends `SET_MAP` itself: on this
firmware that command swaps two lanes' maps instead of assigning one,
and changing the map is the operator's decision at the printer.

### Hotend control: MPC, not PID

All four hotends on OpenCreator run Kalico's MPC (model predictive
control) with per-material parameters the firmware applies at print
start from the file's material parameters. There are no PID gains to
tune on the hotends, so Print Handy offers no PID calibration for
them, even though the config sections also carry PID terms; the
firmware's own tuning path is its `C5_MPC_CALIBRATE` command, with
`C5_PID_CALIBRATE` also present upstream. PID-shaped assumptions
(heat-up prediction from PID terms, PID tuning offers) do not apply
to this firmware's hotends. The bed stays on conventional control.

## What is not supported yet

Scoped out of the first tranche (#1490 names the follow-up tranches):

- Per-print `C5_PRINT_START` choices (purge, purge line, bed leveling,
  flow calibration) as Print Handy job questions: a later tranche.
  (Tranche 2 delivered the synthesized-start modeling in verification,
  readiness and the active tool on the `creator5_toolchanger`
  contract, the AFC identity lane-map refusal, and the manufacturer
  ceilings recorded above.)
- Converging Print Handy's spool records with the firmware's own
  `lane_data` publishing. Tranche 3 delivered the reading half: the
  namespace can carry either of two publisher shapes (the generic AFC
  publisher's `T<n>` keys, or the `creator5_filament_sync` module's
  `c5_tool<n>` keys), and Print Handy now parses both, labels every
  record with the shape it came from, and reports a conflict instead
  of guessing when the two shapes disagree about a tool. Which
  publisher owns a given install's namespace is still unsettled (the
  upstream sources disagree three ways), so nothing converges from
  these records yet and Print Handy does not write that namespace for
  these models: a spool bind or unbind names that refusal as its
  lane_data outcome. A live capture of the namespace on a real
  install remains the gate for convergence.
- ACE Pro feeders and driving the VDS filament dryer, as Print Handy
  features. What the surfaces actually are (source-verified against
  the fork and the shipped config tree, 2026-11-15, following the
  upstream maintainer's pointers on the research issue): the VDS
  dryer's two chambers are standard Moonraker temperature sensors,
  `temperature_sensor dryer_a` and `temperature_sensor dryer_b`,
  registered by the `creator5_dryer` module itself (status:
  temperature, humidity, measured minimum and maximum) whenever the
  dryer config is included, which the installer does when a dryer
  answers during install. They read through the ordinary sensor path,
  no custom object needed; the dryer's own object and its
  `C5_DRYER_START` / `STOP` / `PAUSE` / `DEHUMIDIFY` / `STATUS`
  commands and material macros are the control surface Print Handy
  does not drive yet. The ACE Pro is not in any OpenCreator release:
  it lives only on the fork's `ace-pro` and `ace-upstream` branches.
  There its unit is an ordinary AFC unit whose lanes are ordinary AFC
  lanes (the standard spool macros register for them), and its dryer
  presents per unit as a heater object `ace_dryer_<unit>`, a humidity
  object `ace_humidity_<unit>` and a `temperature_fan <unit>`, with
  `AFC_ACE_*` commands; the `ace-upstream` branch additionally carries
  an optional `temperature_ace` sensor type. So when ACE ships, it
  rides the existing AFC and sensor paths rather than a new surface;
  Print Handy adds no ACE code until it is in a release.
- Driving OpenCreator updates or alerting on its versions: upstream
  has no version tracking yet, so there is no reliable version to read.
- The OpenCreator Send OrcaSlicer plugin: Print Handy's slice guard
  protects `slicing_pipeline_plugin` by design, so a downloaded project
  cannot inject it, and Print Handy does not adopt it. It remains
  upstream's reference implementation for print-start configuration
  (see the start form section).
- The Legacy edition (see Editions above).

## Credits

Thanks to [Cartrigger](https://github.com/Cartrigger) of the FlashForge C5 Modding Group, who develops OpenCreator. On #1490 he explained how VDS and ACE appear to Moonraker (dedicated temperature sensors with macros; ACE behaves like current AFC systems), pointed us to the [opencreator-send](https://github.com/FlashForge-C5-Modding-Group/opencreator-send) plugin as the print-start reference behind this page's start contract, and offered help with anything else Print Handy needs. Print Handy reads that plugin for behavior only and copies none of its code.

## Choosing between the Creator firmwares

Same printer, two firmwares, two model keys. If the printer runs ZMod,
use `flashforge.creator5` or `flashforge.creator5_pro` and see
[ZMod printers](ZMOD.md). If it runs OpenCreator, use the keys in the
table at the top of this page. The firmware mismatch warning above
exists for the day the wrong one is picked.
