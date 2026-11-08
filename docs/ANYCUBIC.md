# Anycubic printers (Kobra 2 family on Moonraker-exposed firmware; Marlin models via OctoPrint)

[Documentation home](README.md) · [Printer profiles](PRINTER-PROFILES.md)

Print Handy supports Anycubic's Klipper-based Kobra 2 family through **Moonraker** — with a firmware caveat stated plainly below — and Anycubic's older Marlin machines as presets on the OctoPrint route. Nothing here has been tested on an Anycubic printer yet: slicing, profile generation and the guard are covered by offline tests, and live support is explicitly unclaimed until a hardware test exists.

**Read this first — the stock firmware caveat.** The Kobra 2 family ships with Anycubic's closed, Klipper-derived firmware, and it exposes **no stock Moonraker endpoint**. The `anycubic` vendor therefore covers installs where Moonraker is exposed — community firmware such as **Rinkhals** (mainline Klipper + Moonraker) running on the printer. On a fully stock Kobra 2 there is nothing for this vendor to connect to, and no preset here pretends otherwise.

| Model | Key | Bed (mm) | Height | Notes |
|---|---|---|---|---|
| Kobra 2 | `anycubic.kobra2` | 220 × 220 | 250 | Open-frame bed-slinger |
| Kobra 2 Neo | `anycubic.kobra2neo` | 220 × 220 | 250 | Open-frame bed-slinger |
| Kobra 2 Pro | `anycubic.kobra2pro` | 220 × 220 | 250 | Open-frame bed-slinger |
| Kobra 2 Plus | `anycubic.kobra2plus` | 320 × 320 | 400 | Open-frame bed-slinger |
| Kobra 2 Max | `anycubic.kobra2max` | 420 × 420 | 500 | Open-frame bed-slinger |

Select one with `printer.model: anycubic.kobra2` (etc.).

Bed sizes are the pinned Orca profiles' printable areas — the values slicing enforces; here they agree with Anycubic's marketed sizes. The vendor writes `bed_exclude_area` three ways across the family — a missing key on the Kobra 2, a `["0x0"]` list on the Kobra 2 Neo and an empty list on the rest — and all three mean no exclusion; the profile checker treats them the same.

**Marlin Anycubics are not this vendor.** The i3 Mega S, Vyper, Chiron, 4Max Pro 2 and the first Kobra line (Kobra, Kobra Neo, Kobra Plus, Kobra Max) run Marlin firmware with no Moonraker endpoint; they are presets of the OctoPrint support (`octoprint.i3megas`, `octoprint.vyper`, `octoprint.chiron`, `octoprint.maxpro2`, `octoprint.kobra`, `octoprint.kobraneo`, `octoprint.kobraplus`, `octoprint.kobramax`) — see [OctoPrint + Marlin](OCTOPRINT-MARLIN.md).

## Set up the profiles

Print Handy ships no Anycubic slicer profiles (OrcaSlicer's Anycubic profiles are AGPL-3.0). Generate them from your own OrcaSlicer v2.5.0 installation (the shipped slicer; the 2.5 line since #356; its Anycubic profiles are unchanged from v2.5.0-beta, so the recorded beta identities cover it):

```bash
python3 scripts/anycubic-templates.py --printer kobra2 --resources <OrcaSlicer resources folder>
```

The resources folder holds `profiles/Anycubic.json` and `profiles/Anycubic/`, or — OrcaSlicer 2.5 and later — the packed preset cache `profiles/Anycubic.opc` on its own; the command reads either. It resolves the profiles' `inherits`/`include` chains and refuses unless they hash to a checked identity recorded in `print_handy/printers/anycubic/facts.py` (OrcaSlicer 2.4.2, Anycubic vendor profiles `02.04.00.02`; OrcaSlicer v2.5.0-alpha, `02.04.00.09`, and OrcaSlicer v2.5.0-beta, `02.04.00.10`, each from its tree or its packed cache — each packaging has its own identity), then writes them into `<state>/orca-profiles/anycubic_<model>/`, never into the checkout. One 2.5 change to know: upstream renamed the base Kobra 2's checked filament from `Anycubic Generic PLA` to `Generic PLA @Anycubic`, and the recorded name follows the new form, so generate the base Kobra 2's templates from a 2.5 install (2.5.0-alpha or 2.5.0-beta) — a folder generated from 2.4.2 keeps verifying, but its profile names no longer resolve in a 2.4.2 tree. (The other four models' profile names are unchanged between the two versions and generate from either.) Missing, edited or unrecognised profiles fail closed, exactly as for Bambu Lab, Prusa and Elegoo.

Three upstream quirks are recorded in facts.py so they are not "fixed" later: the Kobra 2's machine profile declares `gcode_flavor: marlin` and the Kobra 2 Neo's declares `marlin2`, even though both are Klipper-line machines — the route follows the hardware and the dialect stays upstream's; the base Kobra 2's profile leaves `nozzle_type` undeclared ("undefine"), so its stock brass nozzle is recorded from Anycubic's published specification, unverified; and upstream ships only 0.4 mm nozzle machine profiles for the whole family, so every model here accepts only a 0.4 mm nozzle.

## Moonraker

On an install where the printer exposes Moonraker (the community-firmware case above), everything Moonraker-shaped works the way it does for any Klipper printer: set `services.moonraker_url` to the printer's Moonraker endpoint on the standard port 7125 (for example `http://192.168.1.60:7125`) and Print Handy uploads, starts and watches prints through the usual path, with `printer` delivery on by default. Because the connection is Moonraker, prints are covered by the Moonraker listener's start/pause/completion posts like any other Moonraker printer. What the dedicated `anycubic` vendor adds over `klipper.generic` is the correct bed size and height per model and the pinned templates: the generic profile's 220 × 220 × 250 mm defaults match the three 220 mm models only by accident and are wrong for the Plus and the Max.

The start G-code is the template's own sequence, verbatim: every profile in this family runs a fully inline sequence with no start macro — the adapter ships whatever the generated machine profile carries, and the guard does not demand a macro. The guard refuses other ecosystems' start code (a `PRINT_START`-led or `START_PRINT`-led file, for example), Anycubic's own proprietary `G9111` start command (the closed Kobra OS dialect — see below), a file sliced for a different model, temperatures above the model's published limits (220 mm models: 260 °C nozzle, 110 °C bed; Plus and Max: 260 °C nozzle, 90 °C bed) and nozzle sizes with no template here.

## What is not supported

- A fully stock Kobra 2 family printer: Anycubic's closed firmware exposes no Moonraker endpoint (see the caveat at the top). This vendor is for Moonraker-exposed installs only.
- The Kobra 3 / Kobra 3 Max / Kobra 3 V2 / Kobra S1 / Kobra S1 Max / Kobra X generation: their Orca start sequences open with Anycubic's proprietary `G9111` start command for the closed Kobra OS firmware, which no Moonraker host runs — no honest preset can be made from those profiles today, and the guard refuses `G9111`-led files outright.
- The 4Max Pro: upstream names no default filament profile for it in any packaging, so its slicing baseline cannot be pinned like the others.
- The Predator: a delta machine whose printable area is a ~370 mm circle; the registry models rectangular build volumes only.
- Every Marlin Anycubic on this vendor — see the note above; they use the OctoPrint route: a per-model preset where one exists, else `octoprint.marlin`.
- Multi-nozzle variants: templates are pinned to the stock 0.4 mm nozzle profiles, as with the other generated-template vendors.
- Any claim of a successful print on real hardware.
