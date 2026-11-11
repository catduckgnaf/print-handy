# Printer calibration

Print Handy can run a printer's calibrations from chat, but the pattern
is the product, and it is the same for every driven calibration:

1. **Before values first.** Print Handy reads the current values from
   the printer and shows them before anything runs.
2. **Confirm, with the warnings.** The confirmation names exactly what
   will be sent and states the plain warnings for that calibration
   (heat, motion, noise, staying nearby).
3. **Run, idle only.** A calibration runs only while the printer is
   idle. `print_stats` printing, paused, unrecognised or unreadable
   refuses before anything is sent, and the state is checked again
   immediately before the run command goes out.
4. **New values AND differences.** When the run finishes (Print Handy
   polls the printer for the result, never one long blocking call), it
   presents the new values and the numeric differences: old → new with
   signed deltas (mesh range, PID terms, shaper frequency/type, screw
   errors). Moonraker holds the run command's HTTP call open until the
   gcode finishes, so a run that outlasts the client timeout is treated
   as sent and its result is still collected by polling; a connection
   that fails before the command goes out is a failure. Console results
   are matched by timestamp, so a full Moonraker console history (it
   keeps the newest 1000 lines) does not hide them.
5. **Accept / Discard.** Nothing is saved before this step. Accept sends
   exactly the documented save, once (and warns first when the save
   restarts Klipper). SAVE_CONFIG restarts Klipper mid-request, so
   Moonraker usually answers 503 or drops the connection; that is the
   expected restart, never resent. Accept reports success once Klipper
   is ready again, and says the save is unconfirmed if it does not come
   back. Discard sends the documented revert, or nothing
   when nothing was applied, and never a save. Both re-check that the
   printer is still idle first. No answer before the prompt times out is
   not a Discard: nothing is sent, and the reply says the new values
   stay in memory until Klipper restarts.
6. **A record either way.** Every finished run (accepted, discarded or
   failed) and every report or guide writes a calibration record;
   `/print-calibration records` lists the recent ones.

The primitives and save semantics come from the Stage 0 evidence on (ZMod wiki, Klipper documentation and source, Moonraker),
recorded as a comment on the issue before the code was written. This
page matches `print_handy/calibration.py`; if the two ever disagree,
one of them is a bug. The chat commands on this page are not live-hardware
verified yet; Stage 2 of #565 is a live session on a real AD5X. (A full
mesh run on an AD5X was verified on 2026-10-28 through the `mesh-run`
script path, not through `/print-calibration`; see
[DEVICE-AD5X.md](DEVICE-AD5X.md#verified-on-hardware).)

## The commands

- `/print-calibration`: the menu: the calibrations this printer
  actually offers, from live discovery (below), and how each is served.
- `/print-calibration mesh`: full bed mesh.
- `/print-calibration screws`: bed screws.
- `/print-calibration pid extruder [temp]` / `pid bed [temp]`, heater
  PID. Without a temperature, the flow asks (extruder: 240 °C default,
  200/255 offered; bed: 60 °C default, 80/110 offered, calibrate at the
  temperature you print at).
- `/print-calibration shaper`: input shaper.
- `/print-calibration belts`: belt check (ZMod; report-only).
- `/print-calibration zoffset`: the Z-offset report and the
  mode-correct adjustment guide.
- `/print-calibration records`: recent calibration records for the
  printer.

## Who is eligible, and what is offered

Eligibility mirrors firmware updates: the printer must connect
over Moonraker and its firmware family must be `zmod` or vanilla
`klipper`. Vendor Klipper forks (Elegoo, Creality, QIDI, Anycubic) are
refused with a plain explanation; their calibration macros and save
behavior are the vendor's own. Non-Moonraker printers have no
calibration path.

Eligibility is only the gate. A calibration is then offered **only when
live discovery shows its primitive exists on that printer**; the macro
in Moonraker's objects list, or the section in the configfile settings.
Nothing is assumed from the model name: no `AUTO_FULL_BED_LEVEL`, no
mesh menu entry.

## Per-calibration notes

### Full bed mesh

- **ZMod, screenless:** ZMod's `AUTO_FULL_BED_LEVEL` (load-cell aware,
  cleans the nozzle first; ZMod's docs say the built-in web mesh tools
  are not suitable for these printers). It is run as
  `AUTO_FULL_BED_LEVEL PROFILE=print_handy SAVE=0 EXTRUDER_TEMP=220
  BED_TEMP=60` (the temperatures of the screenless mesh contract,
  `config/mesh-interface.json`), so it measures into the scratch
  profile `print_handy` in memory only. The before value is the saved
  `auto` profile; the one ZMod's `START_PRINT MESH=auto` loads. Accept
  loads the measurement, saves it as `auto`, removes the scratch
  profile and sends `SAVE_CONFIG`; Discard removes the scratch profile
  and reloads the previously active profile (or clears the mesh). It
  never replaces the native screen's mesh. The run also re-zeros the Z offset's start point, as
  ZMod's leveling always does.
- **ZMod, native screen:** the screen owns the mesh; it loads its mesh
  for every print, so Print Handy **reports** the stored mesh's range
  and guides a rebuild from the stock screen instead of measuring over
  the owner.
- **Generic Klipper:** `BED_MESH_CALIBRATE PROFILE=print_handy`, same
  accept/discard shape; Accept saves it as `default`, the profile
  Klipper loads at startup.

Right after the new mesh reads back, Print Handy sends `TURN_OFF_HEATERS`
and checks that both heater targets read 0, so the printer does not sit
hot while the Accept/Discard question waits. The result says whether that
was confirmed; if it was not, it tells you to turn the heaters off yourself.
The printer is re-checked first: if it is no longer idle (a print started
from the screen or another client meanwhile), the heaters are left alone and
the result says so.

The result presentation is the mesh range (lowest → highest probed
point) before and after, with the signed range difference.

### Bed screws

- **ZMod:** **guide only.** `BED_LEVEL_SCREWS_TUNE` measures with the
  load cells and then waits for its own confirmation at the printer,
  which a chat prompt cannot answer. The guide carries ZMod's own
  warnings: clean the nozzle first, the printer does **not** reset the
  heater temperatures afterwards (zero them by hand), and **the bed
  mesh and Z offset are invalid after a screw tune**.
- **Generic Klipper with `[screws_tilt_adjust]`:** driven.
  `SCREWS_TILT_CALCULATE` is one non-interactive command whose console
  output names each screw's probed height and the exact turn
  (`adjust CW 01:15`; a clock face). The flow is measure → you turn the
  screws → measure again, up to three rounds, showing each screw's
  error and its change between rounds. Nothing is ever saved; the
  adjustment is physical.
- **Generic Klipper with only `[bed_screws]`:** guide only, 
  `BED_SCREWS_ADJUST` is an interactive `ACCEPT`/`ADJUSTED` dialogue at
  the nozzle.

### Z offset, report + guide in every mode

This is the Stage 0 verdict, and it is deliberate. On ZMod, the native
screen's offset and the screenless offset are **two separate values,
each living its own life** (ZMod wiki). There is no pre-print Z-offset
value the bot can responsibly set in either mode:

- **Native screen:** the screen owns its offset (`zProbeOffset`). It is
  adjusted **only during printing**, on the screen, and the screen saves
  it and loads it at every print start. Adjusting through the web
  interface while the screen is in use is not advisable (the screen
  overwrites it), `Z_OFFSET_APPLY_ENDSTOP` must not be used, and
  `[probe] z_offset` must not be edited. Print Handy reports the
  screen's value (and the screenless store's, as a separate value with
  the difference) and gives the screen steps. Creator 5 / 5 Pro are the
  exception, their two stores are synchronised, but the adjustment is
  still an in-print, at-the-printer action.
- **Screenless:** the offset is the saved `gcode_offsets.z`, loaded at
  print start when `SAVE_ZMOD_DATA LOAD_ZOFFSET=1` has been armed once.
  Adjustments happen during a print from the web interface and ZMod
  saves them itself; no `Z_OFFSET_APPLY_*` commands are needed or
  wanted. Print Handy reports the saved value and whether
  `LOAD_ZOFFSET=1` is armed, and explains `LOAD_ZOFFSET_NATIVE` for the
  one-time copy from the screen's store: with its default `START=0` it
  copies the native-screen offset into screenless mode and saves it
  (`START=1`, used at boot, only reports). There is no reverse copy.
- **Generic Klipper:** `PROBE_CALIBRATE` is an interactive `TESTZ`
  paper-test dialogue at the bed, guide only. Print Handy reports the
  configured `probe.z_offset` and walks through the steps.

### Extruder / bed PID; the accept gate is the point

ZMod's own `PID_TUNE_EXTRUDER` / `PID_TUNE_BED` macros **automatically
SAVE_CONFIG** when they finish, which would destroy the accept step, 
Print Handy never sends them. The flows use raw Klipper:

- Run: `PID_CALIBRATE HEATER=extruder TARGET=<t>` (or `heater_bed`). It
  applies the tuned values in the heater's memory, stores them for
  SAVE_CONFIG, and reports them on the console
  (`PID parameters: pid_Kp=… pid_Ki=… pid_Kd=…`), which is where Print
  Handy reads the result.
- Before values come from the configfile settings (`pid_Kp/pid_Ki/
  pid_Kd` for the heater).
- **Accept** = one `SAVE_CONFIG` (restarts Klipper, warned at the
  accept step).
- **Discard** = one `FIRMWARE_RESTART`, which drops the in-memory values
  and restores the saved ones. The restart is named in the result text
  before Discard can be chosen. Never a save.
- ZMod also recommends `[heater_bed] max_power: 0.6` on the AD5X so the
  bed heats fast enough to tune correctly; the guide text notes it and
  the bot never edits the config for you.

### Input shaper

- **ZMod:** `ZSHAPER`; **generic Klipper:** `SHAPER_CALIBRATE` (only
  offered where a resonance tester / accelerometer is configured).
- Both only *recommend* values on the console; nothing is applied or
  saved by the calibration itself. **Accept** = one `SAVE_CONFIG`
  (restarts Klipper); **Discard** sends nothing. One honesty note the
  flow states: the recommendation stays pending inside Klipper until
  the next restart, so an unrelated SAVE_CONFIG before a restart would
  include it.
- Graphs and CSVs land in the printer's Configuration → `mod_data`.
  SAVE_CONFIG does not update `max_accel`; if the report suggests a
  lower acceleration, that is a separate manual edit. Klipper advises
  against running shaper calibration often; the resonant vibration
  wears parts, and the confirmation says so.

### Belts (ZMod, report-only)

`BELTS_SHAPER_CALIBRATION` analyses CoreXY belt frequencies (half-axis
test, spectrogram in the web interface under Configuration →
`mod_data`). It applies nothing and saves nothing, so there is no
accept step: the flow confirms (it is loud, and the printer shakes),
runs, and reports what the printer's console answered.

## Records

One JSON file per printer, `calibration_records.json` under
`<state dir>/printers/<printer id>/`, newest last, capped at 100. Each
record holds the printer, the calibration name, the timestamp, the
before values, the after values, the numeric differences, the outcome
(`accepted`, `discarded`, `failed`, or `reported` for reports, guides
and the belt check) and a short detail (for example the PID target).
`/print-calibration records` lists the ten most recent, newest first.

## What is not covered

- Vendor-fork and non-Moonraker printers (above).
- Any calibration whose primitive live discovery does not find.
- Setting a Z offset from chat, in any mode (see the verdict above).
- Editing `printer.cfg` values directly (for example `max_power` or
  `max_accel`): noted in guides where relevant, never written by the
  bot.
