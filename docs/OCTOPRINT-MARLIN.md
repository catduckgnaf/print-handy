# OctoPrint and Marlin support

Print Handy supports Marlin printers through OctoPrint. Select the `octoprint.marlin` printer model in the existing printer configuration and set `services.octoprint_url` to the OctoPrint server URL. Configure the OctoPrint API key using the existing secret/configuration mechanism; do not place credentials in shared configuration or logs.

The adapter reads OctoPrint printer and job status, checks that the printer is operational and not busy before submission, uploads G-code, and starts it through OctoPrint. Marlin heat commands are sent before file start. The G-code must contain slicer-generated bed and nozzle heating commands and slicer-owned shutdown commands (`M104 S0` and `M140 S0`). Print Handy does not append end G-code.

Marlin support is via OctoPrint, not a direct serial or standalone Marlin transport. Klipper/ZMod and Flashforge continue to use their existing adapters. Operations not supported by OctoPrint, including Moonraker object queries and Klipper mesh preflight, are not assumed available. No physical printer test is part of this integration; automated tests mock the OctoPrint API.

## G-code guard

Every OctoPrint model is checked by the family guard in `print_handy/printers/octoprint/guard.py` before a file is accepted, the same checks the Prusa guard runs for the same Marlin dialect. The guard refuses Klipper or ZMod macro start code Marlin cannot execute (`START_PRINT`, `PRINT_START` and similar), a file sliced for a different printer model, a `T1` or above tool change on these single-tool machines, temperatures above a limit the model declares, and toolpath motion outside the model's bed or printable height (the shared toolpath bounds validator). The checks are driven by the facts each model already declares: bed and height from the model settings, the expected printer model from the model's pinned Orca template, the nozzle sizes from the model record. No OctoPrint model declares temperature limits today, so the temperature check skips for them rather than inventing a limit; a model whose settings declare `max_hotend_c` or `max_bed_c` is checked against exactly those values. Hand-written start code that Marlin can execute (homing, bed probing, heater waits, purge moves) is not refused: the foreign-marker list covers only commands Marlin cannot execute.

Generic Klipper (`klipper.generic`) deliberately has no equivalent family guard (the decision recorded on issue #1469). `PRINT_START` is generic Klipper's own start macro, so the foreign-marker check cannot apply to it, and the generic profile declares no single printer model identity or temperature limits for the mismatch and temperature checks to compare against; its dimensions are operator-overridden placeholders. Its `gcode_problems` therefore stays the Bondtech INDX check it has always had. If generic Klipper ever declares sourced per-model facts, a minimal guard (model mismatch, declared temperature limits, bounds) should be revisited then.

## Ender presets

`octoprint.marlin` is the generic profile, with placeholder dimensions to override in local config. Four Creality Ender models ship as presets of the same support, same adapter, same OctoPrint route, but the model's real dimensions and templates instead of placeholders:

| Key | Model | Bed (mm) | Height (mm) |
|---|---|---:|---:|
| `octoprint.ender3` | Creality Ender-3 | 220 x 220 | 250 |
| `octoprint.ender3pro` | Creality Ender-3 Pro | 220 x 220 | 250 |
| `octoprint.ender3v2` | Creality Ender-3 V2 | 220 x 220 | 250 |
| `octoprint.ender3v3se` | Creality Ender-3 V3 SE | 220 x 220 | 250 |

Dimensions are the pinned OrcaSlicer profiles' printable area/height (OrcaSlicer's 2.5 line, Creality vendor profiles `02.03.02.87`); the templates are Print Handy's own Marlin set with those facts substituted, Orca profile content is AGPL-3.0 and is not republished. Upstream ships only a 0.4 mm nozzle machine profile for the Ender-3 V2, so that preset accepts only 0.4 mm. The Ender-3 V3 SE belongs here despite its name: its stock firmware is Marlin-based (the pinned profile declares `gcode_flavor: marlin2`) and it has no Moonraker endpoint, the Klipper Enders (Ender-3 V3, K1 family) are in [Creality printers](CREALITY.md). Select a preset with `PRINT_HANDY__PRINTER__VENDOR=octoprint` and `PRINT_HANDY__PRINTER__MODEL=ender3` (etc.).

## Anycubic presets

Eight Anycubic Marlin models ship as presets of the same support, with dimensions from the same pinned tree (OrcaSlicer's 2.5 line, Anycubic vendor profiles `02.04.00.09`):

| Key | Model | Bed (mm) | Height (mm) |
|---|---|---:|---:|
| `octoprint.i3megas` | Anycubic i3 Mega S | 210 x 210 | 205 |
| `octoprint.vyper` | Anycubic Vyper | 250 x 255 | 265 |
| `octoprint.chiron` | Anycubic Chiron | 400 x 400 | 450 |
| `octoprint.maxpro2` | Anycubic 4Max Pro 2 | 270 x 210 | 190 |
| `octoprint.kobra` | Anycubic Kobra | 220 x 220 | 250 |
| `octoprint.kobraneo` | Anycubic Kobra Neo | 220 x 220 | 250 |
| `octoprint.kobraplus` | Anycubic Kobra Plus | 300 x 300 | 350 |
| `octoprint.kobramax` | Anycubic Kobra Max | 400 x 400 | 450 |

Templates are Print Handy's own Marlin set with those facts substituted, as for the Enders. Upstream ships only a 0.4 mm nozzle machine profile for every one of these models, so each preset accepts only 0.4 mm. The Kobra Neo's pinned profile declares `gcode_flavor: marlin2`; the rest declare `marlin`. These machines predate the Kobra 2 family's Klipper-derived firmware and have no Moonraker endpoint; the Kobra 2 family itself is in [Anycubic printers](ANYCUBIC.md), with the firmware caveat stated there. Two Anycubic Marlin models are deliberately not preset: the 4Max Pro (upstream names no default filament profile for it, so its slicing baseline cannot be pinned like the others) and the Predator (a delta machine with a circular printable area; the registry models rectangular build volumes only).

## Completion posts and timelapse

There is no OctoPrint listener, so a print Print Handy launches is followed by a launched-print monitor (the same pattern as the Bambu and stock Flashforge families). When the print ends, the monitor makes the completion post. If OctoPrint's own timelapse feature is enabled and it rendered a movie for the job, the monitor downloads that movie, sizes it to the chat platform's upload limit, and attaches it to the completion post. A rendered movie that cannot be downloaded or sized is explained in the completion post itself; if OctoPrint lists no movie for the job, the completion posts as plain text and the reason is in the log. The monitor runs inside the process that launched the print and is not restarted if that process restarts mid-print; prints started outside Print Handy are not monitored.
