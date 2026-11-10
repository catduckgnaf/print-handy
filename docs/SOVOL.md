# Sovol printers

[Documentation home](README.md) · [Printer profiles](PRINTER-PROFILES.md) · [OctoPrint + Marlin](OCTOPRINT-MARLIN.md)

Sovol is split the same way Flashforge is: one brand for the Klipper path, one for the stock Marlin path. Pick the key that matches the firmware the printer is running today. A machine that can do either, such as the SV06, is listed twice.

| Path | Vendor key | Connection | Printer URL |
|---|---|---|---|
| Sovol (Klipper) | `sovol.<model>` | Moonraker | `services.moonraker_url` |
| Sovol (Marlin/OctoPrint) | `sovol_marlin.<model>` | OctoPrint | `services.octoprint_url` |

The Printer URL is the address Print Handy talks to. It does not have to be on the printer. A Klipper host or an OctoPi on another computer is the right value.

## Models

Build volumes are the printable area and height from the OrcaSlicer Sovol machine profile for that model (the 0.4 mm nozzle file), except where OrcaSlicer has no profile. The SV03 uses the Marlin firmware printer table (360 x 360 x 410 mm). The SV04 uses the single-mode size from its firmware specifications (300 x 300 x 400 mm); copy and mirror modes are smaller and are not the catalog bed. The M1D uses Sovol's single-toolhead size (300 x 300 x 350 mm); seven-tool printing is 240 x 300 x 350 mm and is not the catalog bed. None of these entries have been checked on a live printer. The slicer templates are Print Handy's generic Klipper or Marlin set with that volume substituted. They are not a tuned copy of Sovol's own profiles, and upstream profile text is not in this repository.

Stock Marlin, also listed under Klipper for a converted machine:

| Model | Bed (mm) | Height (mm) |
|---|---|---|
| SV01, SV01 Pro, SV02 | 280 x 240 | 300 |
| SV03 | 360 x 360 | 410 |
| SV04 | 300 x 300 | 400 |
| SV05 | 220 x 220 | 300 |
| SV06 | 220 x 220 | 250 |
| SV06 Plus | 300 x 300 | 340 |

Both SV06 keys (`sovol.sv06` and `sovol_marlin.sv06`) use the machine's real 250 mm height, matching the OrcaSlicer profile and the shipped templates.

Factory Klipper (Klipper key only):

| Model | Bed (mm) | Height (mm) | Motion |
|---|---|---|---|
| SV06 ACE | 220 x 220 | 250 | CoreXZ |
| SV06 Plus ACE | 300 x 300 | 350 | CoreXZ |
| SV07 | 220 x 220 | 250 | Cartesian |
| SV07 Plus | 300 x 300 | 350 | Cartesian |
| SV08 | 350 x 350 | 345 | CoreXY |
| SV08 Max | 500 x 500 | 500 | CoreXY |
| Zero | 152.4 cube | 152.4 | CoreXY |
| M1D | 300 x 300 | 350 | CoreXY |

The SV04 is an IDEX machine, and the M1D is a multi-tool machine. These catalog entries are the single-mode or single-toolhead volume only. They do not slice copy mode, mirror mode, or a tool change.

Start G-code on the Klipper keys is the same `PRINT_START` / `PRINT_END` family as `sovol.sv06`. If the installed firmware uses a different macro, set `printer.start_macro` and `printer.end_macro` before the first print. Two stock firmwares name their own start macro differently: the SV08 line's stock config and the Zero's stock firmware define `START_PRINT` (the Zero's pair is `START_PRINT` / `END_PRINT`, called bare), and the macro registry describes those stock names for those models too, see MACROS.md for the per-model coverage and the Zero's macro-pack caveat. The Marlin keys use OctoPrint and do not send Moonraker macros; their start G-code heats, then homes (`G28`) before it prints.

A config written before this split with a bare `model: sv06` keeps meaning `sovol.sv06`. Use `sovol_marlin.sv06` (or `vendor: sovol_marlin`) for the stock Marlin SV06.
