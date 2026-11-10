# Creality printers (Klipper / Creality OS line: Ender-3 V3 series, K1 series)

[Documentation home](README.md) · [Printer profiles](PRINTER-PROFILES.md)

Creality's Klipper-based printers, the ones running **Creality OS**, work through **Moonraker**, the API that firmware exposes. Slicing, profile generation and the guard pass offline tests, but no Creality machine has run a real print through Print Handy, so your K1 or Ender-3 V3 is the first. Keep a hand on the stop button.

| Model | Key | Bed (mm) | Height | Notes |
|---|---|---|---|---|
| Ender-3 V3 | `creality.ender3v3` | 220 × 220 | 250 | Open-frame bed-slinger |
| Ender-3 V3 KE | `creality.ender3v3ke` | 220 × 220 | 245 | Open-frame bed-slinger; fully inline start code (see below) |
| Ender-3 V3 Plus | `creality.ender3v3plus` | 300 × 300 | 330 | Open-frame bed-slinger |
| K1 | `creality.k1` | 220 × 220 | 250 | Enclosed CoreXY |
| K1C | `creality.k1c` | 220 × 220 | 250 | Enclosed CoreXY |
| K1 Max | `creality.k1max` | 300 × 300 | 300 | Enclosed CoreXY |

Select one with `printer.model: creality.k1` (etc.).

Bed sizes are the pinned Orca profiles' printable areas, the values slicing enforces; here they agree with Creality's marketed sizes. The vendor writes `bed_exclude_area` two ways, a `["0x0"]` list on the Ender-3 V3 line and a bare `"0x0"` string on the K1 line, and both mean no exclusion; the profile checker treats them the same.

**Marlin Enders are not this vendor.** The older Ender-3 / Ender-3 V2 / Ender-3 S1 / Ender-5 and CR-10 lines run Marlin firmware with no Moonraker endpoint; they are not covered by anything on this page and keep the existing route: OctoPrint in front of the printer, the per-model presets (`octoprint.ender3`, `octoprint.ender3pro`, `octoprint.ender3v2`) or the generic `octoprint.marlin`, see [OctoPrint + Marlin](OCTOPRINT-MARLIN.md). The Ender-3 V3 SE is likewise not covered by this vendor: it does not expose the Klipper/Moonraker endpoint this vendor uses, and is preset as `octoprint.ender3v3se` on the OctoPrint route.

## Set up the profiles

Print Handy ships no Creality slicer profiles (OrcaSlicer's Creality profiles are AGPL-3.0). Generate them from your own OrcaSlicer v2.5.0 installation (the one shipped slicer; the 2.5 line since #356):

```bash
python3 scripts/creality-templates.py --printer k1 --resources <OrcaSlicer resources folder>
```

The resources folder holds `profiles/Creality.json` and `profiles/Creality/`, or, OrcaSlicer 2.5 and later, the packed preset cache `profiles/Creality.opc` on its own; the command reads either. It resolves the profiles' `inherits`/`include` chains and refuses unless they hash to a checked identity recorded in `print_handy/printers/creality/facts.py` (OrcaSlicer 2.4.2, Creality vendor profiles `02.03.02.75`; OrcaSlicer v2.5.0-alpha, `02.03.02.87`, OrcaSlicer v2.5.0-beta, `02.03.02.87`, and OrcaSlicer v2.5.0-rc, `02.03.02.88`, each from its tree or its packed cache, each packaging has its own identity), then writes them into `<state>/orca-profiles/creality_<model>/`, never into the checkout. One 2.5 change to know: upstream renamed the whole `Creality Generic …` filament family to `Generic …`, and the checked default filaments follow the new names, so generate Creality templates from a 2.5 install (2.5.0-alpha, 2.5.0-beta or 2.5.0-rc), a folder generated from 2.4.2 keeps verifying, but its profile names no longer resolve in a 2.4.2 tree. Missing, edited or unrecognised profiles fail closed, exactly as for Bambu Lab, Prusa and Elegoo.

Three upstream quirks are recorded in facts.py so they are not "fixed" later: the Ender-3 V3 and V3 Plus machine profiles name a `default_print_profile` that does not exist in the shipped process list (the list names are the real ones and are what Print Handy records), the K1C machine profile's default adds a "(0.4 nozzle)" suffix its process profile does not carry, and the checked filament names genuinely differ per model in 2.5 (`@Ender-3 V3-all` vs `@Creality Ender-3V3-all` vs `@Ender-3 V3 Plus-all`).

## Moonraker

These printers run a Klipper firmware (Creality OS) behind a Moonraker endpoint, so everything Moonraker-shaped works the way it does for any Klipper printer: set `services.moonraker_url` to the printer's Moonraker endpoint on the standard port 7125 (for example `http://192.168.1.60:7125`) and Print Handy uploads, starts and watches prints through the usual path, with `printer` delivery on by default. Because the connection is Moonraker, prints are covered by the Moonraker listener's start/pause/completion posts like any other Moonraker printer. What the dedicated `creality` vendor adds over `klipper.generic` is the correct bed size and height per model and the pinned templates: the generic profile's 220 × 220 × 250 mm defaults match the K1 line only by accident and are wrong for the V3 KE, the V3 Plus and the K1 Max.

The start G-code is the template's own sequence, verbatim: most of these profiles open with the printer's `START_PRINT` macro, while the Ender-3 V3 KE's profile runs a fully inline sequence with no macro call at all; the adapter ships whatever the generated machine profile carries, and the guard does not demand a macro. The guard refuses other ecosystems' start code (a `PRINT_START`-led file, for example), a file sliced for a different model, temperatures above the model's published limits (Ender-3 V3 line: 300 °C nozzle, 110 °C bed; K1 line: 300 °C nozzle, 100 °C bed) and nozzle sizes with no template here.

## What is not supported

- Every Marlin Creality printer (older Ender-3/CR lines), see the note above; they use the OctoPrint route: a per-model preset where one exists, else `octoprint.marlin`.
- The Ender-3 V3 SE (no Moonraker endpoint on its stock firmware; preset as `octoprint.ender3v3se`).
- The K2 series and other CFS multi-material models: the CFS filament system is not modelled here; single-slot templates would misrepresent them.
- Other Klipper-based Creality models whose Orca identities are not pinned yet (for example the Ender-5 Max and CR-10 SE): follow-ups on this vendor pack, added the same way once checked.
- Multi-nozzle variants: templates are pinned to the stock 0.4 mm nozzle profiles, as with the other generated-template vendors.
- Any claim of a successful print on real hardware.
