# AD5X on ZMod: recorded printer responses

Real responses from a Flashforge AD5X running ZMod (screenless, `display_off=1`), recorded on
2026-10-31 and replayed by `tests/replay/` through a fake Moonraker. They exist so the start and
material path is tested against what the printer really says, not against hand-written mocks.

## How they were recorded

Read-only `GET` requests only, made with `curl` from the Unraid host to the printer's Moonraker, plus
read-only `GET`s of the Spoolman API from inside the Print Handy container. Nothing was posted, no
G-code was run and no print was started, stopped or changed. The printer was printing when recorded.

## Files

| File | Source request | State |
| --- | --- | --- |
| `objects_query.printing.json` | `/printer/objects/query` for webhooks, print_stats, extruder, heater_bed, virtual_sdcard, zmod_color, filament_switch_sensor head_switch_sensor, save_variables, idle_timeout, toolhead | recorded (printing) |
| `objects_query.idle.json` | same | synthesised from the printing record, see below |
| `history_list.json` | `/server/history/list?limit=5` | recorded, scrubbed |
| `files_list.json` | `/server/files/list?root=gcodes` | recorded, trimmed from 123 entries to 6, names replaced |
| `printer_info.json` | `/printer/info` | recorded |
| `server_info.json` | `/server/info` | recorded, one host scrubbed |
| `configfile_settings.json` | `/printer/objects/query?configfile=settings` | recorded, trimmed to the `extruder` nozzle and pressure advance keys and `probe.z_offset` |
| `bed_mesh_profiles.json` | `/printer/objects/query?bed_mesh=profiles` | recorded, profile names only (probe points dropped) |
| `config_files_list.json` | `/server/files/list?root=config` | recorded, trimmed to `printer.cfg` |
| `spool_id.json` | `/server/spoolman/spool_id` | recorded |
| `spoolman_spool_17.json` | Spoolman `/api/v1/spool/17` (Silk PLA, IFS slot 2) | recorded, scrubbed |
| `spoolman_spool_12.json` | Spoolman `/api/v1/spool/12` (plain PLA, IFS slot 4) | recorded, scrubbed |
| `silk_pla_slot2_sliced.gcode.txt` | header and config block of an archived AD5X Silk PLA slice | header and config verbatim, body synthesised, see below |

The `.txt` suffix keeps the G-code fixture from being picked up as a printable file.

## Synthesised fields

`objects_query.idle.json` is the printing record with only the fields that change between printing
and idle edited. Everything else, including the saved variables, `zmod_color` slots and the
`toolhead` block, is as recorded.

- `print_stats`: `state` -> `standby`, `filename` -> `""`, `total_duration`, `print_duration` and
  `filament_used` -> `0.0`, `info` -> `{total_layer: null, current_layer: null}`
- `extruder`: `target` and `power` -> `0.0` (`temperature` stays at the recorded, still hot, value)
- `heater_bed`: `target` and `power` -> `0.0` (`temperature` as recorded)
- `virtual_sdcard`: `is_active` -> `false`, `file_path` -> `null`, `progress`, `file_position` and
  `file_size` -> `0`
- `idle_timeout`: `state` -> `Ready`, `printing_time` -> `0.0`

The fake server changes these at run time to play a start (heaters on, then `print_stats` printing
the uploaded file, then finished with no file); those transitions are code in `tests/replay/conftest.py`,
not recordings.

`silk_pla_slot2_sliced.gcode.txt`: the header block (the `hermes_tool_slots` and `zmod_color_data`
lines, `"Silk PLA"` quoted as Orca writes it) and the whole `CONFIG_BLOCK` are copied from a real
slice, minus the first-line MD5 (the printer is screenless, and the body is not the sliced body).
The executable body is synthesised: the startup heater lines, a six-layer stub with the `;TYPE` and
`;WIDTH` lines the safety checks need, and `END_PRINT`. The recorded slice carried a nozzle target of
210 C, the stale value of bug #692; the tests set the target in the file to 220 or 210 as the case
requires (`helpers.sliced_gcode`).

The Print Handy hardware record (`helpers.hardware_record`) is not a recording. It is written from
the recorded slots, saved variables and probe offset, with the printer-wide `temps_c` left at 210/60.

## What was scrubbed

- Every model and print file name: replaced by `Replay-Part-N_plate_1_<sha256 of a made-up string>.gcode`
  and `Replay-File-N.*` (the same name everywhere it appears: print_stats, virtual_sdcard, history, file list).
- History: job `uuid` and any thumbnail paths dropped.
- `server_info.json`: the CORS warning's LAN wildcard replaced by `printer.test`.
- Spoolman: comments, lot and article numbers, vendor, and registered/first-used/last-used timestamps dropped.
- Nothing was recorded that holds an API key, token, Wi-Fi name or password, or a serial number, and
  `tests/replay/test_fixtures_scrubbed.py` fails if one of those, a private IP address, a UUID or a
  MAC address appears in a fixture. The printer's address never appears: the tests serve everything
  from `127.0.0.1`.

To re-record, repeat the read-only requests in the table, then apply the same scrub. Do not run
anything that changes printer state.

The start transitions (#843: `objects_query.preparing.json`, `objects_query.prep_aborted.json`) are
recorded by Print Handy itself during a real start with `PRINT_HANDY_RECORD_STARTS=1` and exported
with `python3 -m print_handy.core.start_recorder export`, which applies the file-name scrub above. See
[Recording a live start](../../../../docs/ZMOD.md#recording-a-live-start-842-843). Never hand-write one.
