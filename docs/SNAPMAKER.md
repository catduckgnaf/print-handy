# Snapmaker printers (U1)

[Documentation home](README.md) · [Printer profiles](PRINTER-PROFILES.md)

The Snapmaker U1 works through **Moonraker**. It runs Klipper-based firmware and it is a tool changer: four toolheads, not a filament feeder. Support rests on the manufacturer's published specification, and no U1 has printed through Print Handy yet. The templates are generic files this project wrote; Snapmaker's own profiles and OrcaSlicer's profiles are not shipped.

| Model | Key | Bed (mm) | Height | Tools |
|---|---|---|---|---|
| U1 | `snapmaker.u1` | 270 × 270 | 270 | 4 independent toolheads |

Select it with `printer.model: snapmaker.u1`.

`filament_slots` stays 1. `tool_count` is 4. Slicer tool 0 prints with toolhead 1, tool 1 with toolhead 2, and so on. A file that remaps a tool onto a different head is refused.

## Moonraker

Set `services.moonraker_url` to the printer's Moonraker endpoint (for example `http://192.168.1.70:7125`). Upload, start and watch use the same Moonraker path as any other Klipper printer. The web UI lists `snapmaker.u1` with the other registered models.

Published limits, used by the guard and not confirmed on a machine: 300 °C nozzle, 100 °C bed, stock 0.4 mm stainless steel nozzle. The guard also refuses start markers that belong to other ecosystems (`START_PRINT`, ZMod color commands). It does not claim the rest of a file is a valid U1 start.

The adapter has an unwired helper (`spool_remain_g`) that reads `filament_remaining_g` or `spool_remain_g` from a toolhead object in a status sample when one carries it. Nothing in the status, intake or low-filament paths calls it yet, so Print Handy does not currently report or act on U1 spool remain. A missing field would not be treated as an empty spool. Those field names are guesses and the U1's real object names have not been confirmed.

## What is not supported

- Snapmaker 2.0, Artisan, J1, and any machine that is not the U1.
- Copying or generating Snapmaker Orca or Snapmaker Orca profiles. The shipped templates are generic.
- Applying firmware updates. The U1 is a vendor fork; Print Handy does not drive its updater.
- Any claim of a successful print on real hardware.
