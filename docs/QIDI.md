# Qidi printers (Klipper line: Q1 Pro, X-Plus 3, X-Plus 4, X-Max 3, X-Smart 3)

[Documentation home](README.md) · [Printer profiles](PRINTER-PROFILES.md)

Print Handy supports Qidi's current Klipper-based printers through **Moonraker**, the API their stock firmware already runs — no cloud account, nothing to install on the printer. Nothing here has been tested on a Qidi printer yet: slicing, profile generation and the guard are covered by offline tests, and live support is explicitly unclaimed until a hardware test exists.

| Model | Key | Bed (mm) | Height | Notes |
|---|---|---|---|---|
| Q1 Pro | `qidi.q1pro` | 245 × 245 | 240 | Enclosed CoreXY; actively heated chamber (set by the template start code) |
| X-Plus 3 | `qidi.xplus3` | 280 × 280 | 270 | Enclosed CoreXY |
| X-Plus 4 | `qidi.xplus4` | 305 × 305 | 280 | Enclosed CoreXY; actively heated chamber (set by the template start code) |
| X-Max 3 | `qidi.xmax3` | 325 × 325 | 315 | Enclosed CoreXY |
| X-Smart 3 | `qidi.xsmart3` | 175 × 180 | 170 | Enclosed CoreXY; marketed as 180 × 180 (see below) |

Select one with `printer.model: qidi.q1pro` (etc.).

Bed sizes are the pinned Orca profiles' printable areas — the values slicing enforces. Qidi's marketed beds differ in one case: the X-Smart 3 is sold as 180 × 180 mm, but its Orca profile's printable area is 175 × 180 mm; both figures are recorded in the model's facts, and the Orca value is the one used. The Q1 Pro's profile also records a bed exclusion polygon (the purge zone along the back edge), which is carried into the model facts point for point; the X-Plus 4's profile records one too.

## Set up the profiles

Print Handy ships no Qidi slicer profiles (OrcaSlicer's Qidi profiles are AGPL-3.0). Generate them from your own OrcaSlicer v2.5.0-rc installation (the one shipped slicer; the 2.5 line since #356):

```bash
python3 scripts/qidi-templates.py --printer q1pro --resources <OrcaSlicer resources folder>
```

The resources folder holds `profiles/Qidi.json` and `profiles/Qidi/`, or — OrcaSlicer 2.5 and later — the packed preset cache `profiles/Qidi.opc` on its own; the command reads either. It resolves the profiles' `inherits`/`include` chains and refuses unless they hash to a checked identity recorded in `print_handy/printers/qidi/facts.py` (OrcaSlicer 2.4.2, Qidi vendor profiles `02.04.00.06`; OrcaSlicer v2.5.0-alpha, `02.04.00.17`, OrcaSlicer v2.5.0-beta, `02.04.00.19`, and OrcaSlicer v2.5.0-rc, `02.04.00.20`, each from its tree or its packed cache — each packaging has its own identity), then writes them into `<state>/orca-profiles/qidi_<model>/`, never into the checkout. One 2.5 change to know: upstream renamed the whole `Qidi Generic …` filament family to `Generic … @Qidi …`, and the checked default filaments follow the new names, so generate Qidi templates from a 2.5 install (2.5.0-alpha, 2.5.0-beta or 2.5.0-rc) — a folder generated from 2.4.2 keeps verifying, but its profile names no longer resolve in a 2.4.2 tree. Missing, edited or unrecognised profiles fail closed, exactly as for Bambu Lab, Prusa and Elegoo.

## Moonraker

The Qidi line runs stock Klipper behind Moonraker, so everything Moonraker-shaped works the way it does for any Klipper printer: set `services.moonraker_url` to the printer's Moonraker endpoint on the standard port 7125 (for example `http://192.168.1.60:7125`) and Print Handy uploads, starts and watches prints through the usual path, with `printer` delivery on by default. Because the connection is Moonraker, prints are covered by the Moonraker listener's start/pause/completion posts like any other Moonraker printer. What the dedicated `qidi` vendor adds over `klipper.generic` is the correct bed size and height per model and the pinned templates: the generic profile's 220 × 220 × 250 mm defaults match no Qidi model and would silently mis-slice.

Qidi's start G-code opens with the printer's own `PRINT_START` macro — the Q1 Pro passes `BED=`/`HOTEND=`/`CHAMBER=` — and continues with an inline sequence from the template; the adapter ships that sequence verbatim. The guard refuses other ecosystems' start code (a `START_PRINT`-led file, for example), a file sliced for a different model, temperatures above the model's published limits (Q1 Pro / X-Plus 3 / X-Max 3: 350 °C nozzle, 120 °C bed; X-Plus 4: 370 °C nozzle, 120 °C bed; X-Smart 3: 300 °C nozzle, 110 °C bed) and nozzle sizes with no template here. The Q1 Pro's heated chamber is driven by the template's start code (the slicer's chamber temperature), not by a separate Print Handy control.

## What is not supported

- The rest of the newer Qidi generation (X-Max 4, Plus 5, Q2 / Q2C / Q2 Pro, QD Plus): profiles exist in Orca, but their identities are not pinned here yet; they are follow-ups on this vendor pack, added the same way once checked.
- The older X-Max / X-Plus / X-CF Pro generation and the X-Pro / X-One line.
- Multi-nozzle variants: templates are pinned to the stock 0.4 mm nozzle profiles, as with the other generated-template vendors.
- Any claim of a successful print on real hardware.
