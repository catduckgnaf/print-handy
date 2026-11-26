# Qidi printers (Klipper line: Q1 Pro, X-Plus 3, X-Plus 4, X-Max 3, X-Smart 3)

[Documentation home](README.md) · [Printer profiles](PRINTER-PROFILES.md)

Qidi's current Klipper printers work through **Moonraker**, which their stock firmware already runs: no cloud account, nothing to install on the printer. The templates and guard pass offline tests, but no Qidi has printed through Print Handy yet. Keep a hand on the stop button for the first one.

| Model | Key | Bed (mm) | Height | Notes |
|---|---|---|---|---|
| Q1 Pro | `qidi.q1pro` | 245 × 245 | 240 | Enclosed CoreXY; actively heated chamber (set by the template start code) |
| X-Plus 3 | `qidi.xplus3` | 280 × 280 | 270 | Enclosed CoreXY |
| X-Plus 4 | `qidi.xplus4` | 305 × 305 | 280 | Enclosed CoreXY; actively heated chamber (set by the template start code) |
| X-Max 3 | `qidi.xmax3` | 325 × 325 | 315 | Enclosed CoreXY |
| X-Smart 3 | `qidi.xsmart3` | 175 × 180 | 170 | Enclosed CoreXY; marketed as 180 × 180 (see below) |

Select one with `printer.model: qidi.q1pro` (etc.).

Bed sizes are the pinned Orca profiles' printable areas; the values slicing enforces. Qidi's marketed beds differ in one case: the X-Smart 3 is sold as 180 × 180 mm, but its Orca profile's printable area is 175 × 180 mm; both figures are recorded in the model's facts, and the Orca value is the one used. The Q1 Pro's profile also records a bed exclusion polygon (the purge zone along the back edge), which is carried into the model facts point for point; the X-Plus 4's profile records one too.

## Set up the profiles

Print Handy ships no Qidi slicer profiles (OrcaSlicer's Qidi profiles are AGPL-3.0). Generate them from your own OrcaSlicer v2.5.2 installation (the one shipped slicer; the 2.5 line since #356; its Qidi profiles are unchanged from v2.5.1, so a 2.5.2 install verifies against the recorded 2.5.1 identities):

```bash
python3 scripts/qidi-templates.py --printer q1pro --resources <OrcaSlicer resources folder>
```

The resources folder holds `profiles/Qidi.json` and `profiles/Qidi/`, or, OrcaSlicer 2.5 and later, the packed preset cache `profiles/Qidi.opc` on its own; the command reads either. It resolves the profiles' `inherits`/`include` chains and refuses unless they hash to a checked identity recorded in `print_handy/printers/qidi/facts.py` (OrcaSlicer 2.4.2, Qidi vendor profiles `02.04.00.06`; OrcaSlicer v2.5.0-alpha, `02.04.00.17`, OrcaSlicer v2.5.0-beta, `02.04.00.19`, and OrcaSlicer v2.5.0-rc, `02.04.00.20`, and OrcaSlicer v2.5.1, `02.04.00.21`, each from its tree or its packed cache, each packaging has its own identity), then writes them into `<state>/orca-profiles/qidi_<model>/`, never into the checkout. One 2.5 change to know: upstream renamed the whole `Qidi Generic …` filament family to `Generic … @Qidi …`, and the checked default filaments follow the new names, so generate Qidi templates from a 2.5 install (2.5.0-alpha, 2.5.0-beta, 2.5.0-rc, 2.5.1 or 2.5.2), a folder generated from 2.4.2 keeps verifying, but its profile names no longer resolve in a 2.4.2 tree. Missing, edited or unrecognised profiles fail closed, exactly as for Bambu Lab, Prusa and Elegoo.

## Moonraker

QIDI's 2026-11 releases change the supported filament-management contract even though the printer still runs a Klipper/Moonraker stack. The older Q1 Pro 4.4.24 and X-Plus 4 1.7.1 documentation did not prohibit Fluidd filament management; the Q1 Pro 6.1.1 and X-Plus 4 2.1.1 release notes explicitly say it is unsupported, direct BOX filament settings to QIDI Studio, and warn never to edit BOX filament through Fluidd. Both releases replace the Klipper configuration and require recalibration; keep those steps separate from Print Handy's software profile generation. The X-Plus 4 notes additionally announce support for new BOX firmware. The sources do not specify a supported Moonraker API for managing BOX records, so Print Handy does not infer one.

Accordingly, for Q1 Pro and X-Plus 4 all versions currently fail closed for printer-side slot metadata writes from Print Handy: no `CHANGE_ZCOLOR` screen G-code and no Moonraker `lane_data` database POST. This conservative model-level behavior also protects older firmware until reliable firmware-version detection and a supported per-version write contract are verified. Spoolman/local bookkeeping remains local and does not configure the printer or BOX. Existing print upload/start/status behavior remains unchanged; no release evidence reviewed here establishes a regression in those paths. QIDI Studio is the documented BOX settings interface.

| Model and release | Upstream source | Contract decision | Print Handy paths and tests | Hardware status |
| --- | --- | --- | --- | --- |
| Q1 Pro 6.1.1 | [Release `Q1_PRO_V6.1.1`](https://github.com/QIDITECH/QIDI_Q1_Pro/releases/tag/Q1_PRO_V6.1.1), commit `61836007215a8b4f71418ac62059bc81c1f40b93`; [version README](https://raw.githubusercontent.com/QIDITECH/QIDI_Q1_Pro/Q1_PRO_V6.1.1/README.md) | Fluidd filament management unsupported; BOX settings through QIDI Studio; config replaced, recalibration required | `Q1ProAdapter` refuses screen G-code and `lane_data` writes; offline version contract fixture | No QIDI hardware verification; BOX presence/state and actual Moonraker permission behavior unknown |
| X-Plus 4 2.1.1 | [Release `Plus4_v2.1.1`](https://github.com/QIDITECH/QIDI_PLUS4/releases/tag/Plus4_v2.1.1), commit `ec595fc903540564be757bcafa745cd5c4a52cd0`; [version README](https://raw.githubusercontent.com/QIDITECH/QIDI_PLUS4/Plus4_v2.1.1/README.md) | Same restrictions; also announces new BOX firmware support | `XPlus4Adapter` refuses screen G-code and `lane_data` writes; offline version contract fixture | No QIDI hardware verification; BOX presence/state and actual Moonraker permission behavior unknown |

The firmware-specific fixture is `tests/printers/qidi_firmware_contracts.json`. It records distinct release tags, commit IDs, release dates, configuration/recalibration requirements, and the BOX/Fluidd policy. This is documentation evidence, not captured firmware behavior. QIDI's upstream Klipper/Moonraker repository tree does not expose enough version-specific printer runtime source to prove BOX API behavior; the vendor's explicit restriction is the governing evidence. No live printer, firmware package installation, or physical workflow was tested. Do not use generic Klipper firmware update controls or recommend downgrades based on this audit.

Qidi's start G-code opens with the printer's own `PRINT_START` macro, the Q1 Pro passes `BED=`/`HOTEND=`/`CHAMBER=`, and continues with an inline sequence from the template; the adapter ships that sequence verbatim. The guard refuses other ecosystems' start code (a `START_PRINT`-led file, for example), a file sliced for a different model, temperatures above the model's published limits (Q1 Pro / X-Plus 3 / X-Max 3: 350 °C nozzle, 120 °C bed; X-Plus 4: 370 °C nozzle, 120 °C bed; X-Smart 3: 300 °C nozzle, 110 °C bed) and nozzle sizes with no template here. The Q1 Pro's heated chamber is driven by the template's start code (the slicer's chamber temperature), not by a separate Print Handy control.

## What is not supported

- The rest of the newer Qidi generation (X-Max 4, Plus 5, Q2 / Q2C / Q2 Pro, QD Plus): profiles exist in Orca, but their identities are not pinned here yet; they are follow-ups on this vendor pack, added the same way once checked.
- The older X-Max / X-Plus / X-CF Pro generation and the X-Pro / X-One line.
- Multi-nozzle variants: templates are pinned to the stock 0.4 mm nozzle profiles, as with the other generated-template vendors.
- Any claim of a successful print on real hardware.
