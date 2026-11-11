# Printer accessories

An accessory is hardware fitted to one printer that did not ship with its model: a tool changer, a filament system, a sensor or a heater that changes how Print Handy slices, starts, reports or assigns filament for that one unit. Accessories sit beside printer models the way models sit beside vendors. The host printer keeps its model, bed and profile; the accessory record lives on that printer's configuration and survives restart, and the adapter resolves the printer's effective shape from the model plus its accessories. A printer with no accessories behaves exactly as it did before accessories existed.

## What counts as an accessory

A thing is an accessory when all five of these hold:

1. **Per-unit variance.** Two printers of the same model differ because one has it fitted. Hardware every unit of a model ships with is a model trait, not an accessory: the Flashforge IFS is built into the AD5X and cannot be retrofitted to an AD5M, so IFS stays part of the AD5X model.
2. **It changes behavior, not just description.** Slicing, print start, status or readiness, or filament and color assignment must change. A purely passive add-on, such as an enclosure panel, is at most an override on the model's recorded facts.
3. **Removable or retrofittable** without changing the printer's identity. The host keeps its model, bed and profile when the accessory is fitted or removed.
4. **A verified detection signal, or a manual record.** Detection reads the printer's own published objects or config and allows an operator override. Nothing is detected from a guessed object name.
5. **One position-shaping accessory at a time.** A tool changer or filament system accessory determines the printer's effective color architecture and positions. Sensor and thermal accessories stack alongside it.

An unknown or misspelled accessory key is refused by name in settings validation. It is never silently dropped. See [Settings](SETTINGS.md) for the `[[accessories]]` list.

## Bondtech INDX

The first accessory is the Bondtech INDX tool changer, fitted to a Klipper printer reached over Moonraker. INDX is not a printer and not a filament switcher: one Smart Head stays on the gantry and picks up passive tools from a dock. Each tool carries its own filament and nozzle. The passive tools have no wires, no heaters and no electronics; the Smart Head heats the mounted tool by induction and reads its temperature without contact. A docked tool cannot be heated, preheated or held at a standby temperature.

With INDX recorded, the printer's effective color architecture is a shared-tool shape: a variable number of passive tools, one shared extruder, one shared heater, and exactly one active tool. Multi-color jobs ask one question per color, mapped to tools, and more distinct colors than the tool count is refused. Slicing uses the tool change G-code `CHANGE_TOOL TOOL={next_extruder} TEMP={new_filament_temp}`, so each incoming tool is picked up first and then heated straight to its own filament's temperature. Status names the active tool and the single extruder temperature as that tool's temperature; no per-tool temperatures are invented for docked tools.

Two INDX refusal states are surfaced by name in readiness and status:

- A half-seated tool: the persistent `seat_suspect` flag blocks tool changes across restarts until an operator clears it by hand with `MANUAL_TOOL_REMOVE`, `MANUAL_TOOL_SEAT` or `MANUAL_TOOLHEAD_RESET`.
- Uncalibrated docks: `docks_calibrated` is false on a fresh install by design, and tool changes are refused until the docks are calibrated.

Filament problems on INDX tools stay warnings the user can accept. They never hard-block a print.

### Detection and recording

Detection is verified against the upstream Klipper package. A Moonraker `configfile` settings payload proves an INDX install when it carries an `[indx]` section, an `[mcu indxmcu]` section, and an `[extruder]` with `sensor_type: indx`. The tool count comes from `[gcode_macro TOOL_POSITIONS]` `tool_count` only. There is no upstream cap on the tool count; dock space on the host is the limit. The live active tool is `save_variables.variables.active_tool`, where -1 means an empty head.

Per-tool nozzle size, nozzle type and filament are not detectable at all upstream, so they are recorded manually on the accessory record. A dock position can also hold a non-printing tool (a pen, cutter or camera): record it with `heats = false` and no nozzle or filament, and it still counts toward the tool count.

### INDX setup example

Record INDX in the settings file as a top-level accessories list (TOML form shown; the YAML form is a list under `accessories:` with the same keys):

```toml
[[accessories]]
key = "bondtech.indx"
tool_count = 4

[[accessories.tools]]
tool = 0
nozzle_size_mm = 0.4
nozzle_type = "cht"
filament = "PLA"

[[accessories.tools]]
tool = 1
nozzle_size_mm = 0.4
nozzle_type = "cht"
filament = "PETG"

[[accessories.tools]]
tool = 2
nozzle_size_mm = 0.6
nozzle_type = "cht"
filament = "PLA"

[[accessories.tools]]
tool = 3
heats = false
```

Nozzle types are `cht` or `standard`. Tool indexes are 0-based and must stay below `tool_count`.

### Hosts

Bondtech officially recognizes integrations for the Sovol SV08 and SV08 Max and the Voron Trident R2, plus custom CoreXY machines as a class. A Voron 2.4 integration exists but is unverified upstream. No RatRig V-Core integration is published; a V-Core with INDX uses the generic CoreXY path only. The Prusa first-party INDX build for the CORE One and CORE One L runs closed Prusa firmware, not the open Klipper package, and is not covered by this accessory. RepRapFirmware (Duet) INDX installs are also not covered: Print Handy has no Duet connection.

Support here is established by offline code and tests. No live INDX printer has been run by this project yet, and no live-hardware claim is made.
