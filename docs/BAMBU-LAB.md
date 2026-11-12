# Bambu Lab printers (all single-nozzle models; slicing, optional LAN status, upload and gated print start)

[Documentation home](README.md) · [Printer profiles](PRINTER-PROFILES.md)

Running the Docker image on an A1? Follow [From the image to a first A1 print](BAMBU-A1-DOCKER.md), the step-by-step walkthrough run on an A1 on 26 Oct 2026, including the workarounds that were needed then.

Select the A1 with `printer.model: bambulab.a1`. Print Handy slices for the A1, checks fit and G-code, and can package the result. It connects to the printer only when you configure LAN status (see [LAN status](#lan-status-read-only-opt-in)), and it uploads or starts a print only under the further opt-in described in [Printing over LAN](#printing-over-lan-opt-in). One full A1 print over the LAN has been recorded (see the walkthrough above); other A1 setups and the LAN path in general are covered by offline tests only.

Select the P1S with `printer.model: bambulab.p1s`. Like every Bambu Lab printer it slices with Bambu Studio by default (`printer.slicer: orca` opts out). It works the same way, and has not been tested on a P1S. Its differences: OrcaSlicer's P1S profile slices to 250 mm high (Bambu lists 256 mm), an 18 × 28 mm area at the front left of the bed is kept clear, and the vendor start and end moves use the front purge lane up to Y = 265. The AMS is not driven (its reported state is read for status); P1S jobs are single color. Generate its profiles with `--printer p1s`.

## All single-nozzle models

Every current single-nozzle Bambu Lab printer is a model on the same vendor, with the same LAN configuration (`[bambu] lan_*` keys are per-installation, not per-model), the same delivery options, and profiles generated the same way (`--printer <name>`). None has been tested on hardware.

| Model | Key | Bed (mm) | Orca height | Bed exclusion | Notes |
|---|---|---|---|---|---|
| A1 mini | `bambulab.a1mini` | 180 × 180 | 180 | none | AMS Lite, not driven |
| P1P | `bambulab.p1p` | 256 × 256 | 250 | 18 × 28 front-left | Open frame as shipped |
| X1 | `bambulab.x1` | 256 × 256 | 250 | 18 × 28 front-left | Stainless stock hotend |
| X1 Carbon | `bambulab.x1c` | 256 × 256 | 250 | 18 × 28 front-left | Hardened steel stock hotend |
| X1E | `bambulab.x1e` | 256 × 256 | 250 | 18 × 28 front-left | Ethernet; heated chamber not driven |
| P2S | `bambulab.p2s` | 256 × 256 | 256 | none | AMS 2 Pro, single path only |
| H2S | `bambulab.h2s` | 340 × 320 | 340 | none | 350 °C hotend; single nozzle |
| H2D | `bambulab.h2d` | 350 × 320 | 325 | none | Dual nozzle; 350 °C hotend |
| H2D Pro | `bambulab.h2dpro` | 350 × 320 | 325 | none | Dual nozzle; H2D Pro profiles |
| X2D | `bambulab.x2d` | 256 × 256 | 261 | none | Dual nozzle |
| H2C | `bambulab.h2c` | 330 × 320 | 325 | none | Dual nozzle; 350 °C hotend |
| A2L | `bambulab.a2l` | 330 × 320 | 325 | none | Large-format A-series; single nozzle, AMS Lite |

Each model's facts (profile names, the SHA-256 identities of its checked OrcaSlicer profiles, exclusions, motion envelope, materials) live in `facts.py` beside the A1's and P1S's, derived the same way: the identities were computed by running Print Handy's own profile flattening over each pinned OrcaSlicer release, a pipeline that reproduces the recorded A1 and P1S identities exactly. Every model records one identity per checked packaging: OrcaSlicer 2.4.2's JSON tree (BBL `02.01.00.19`), OrcaSlicer v2.5.0-alpha's tree and packed preset cache (BBL `02.08.00.15`), and OrcaSlicer v2.5.0-beta's tree and packed preset cache (BBL `02.08.00.16`), the 2.5 line; the shipped slicer is now v2.5.0, whose BBL profiles are unchanged from the beta, so a 2.5.0 install hashes to these same identities. A folder generated from any of them keeps verifying.

**Dual-nozzle models:** the H2D, H2D Pro and X2D are supported the same way, with the AMS mapping described under [Printing over LAN](#printing-over-lan-opt-in). Their profiles carry two `nozzle_diameter` entries, which the identity and guard checks now handle per extruder. The H2D's 350 mm width spans both tools (a single tool reaches 305 mm); the recorded bed is the full profile volume, so slice inside the per-tool limits your mapping uses. **The H2C and the A2L** joined when OrcaSlicer v2.5.0-alpha became the shipped slicer: both exist only in the 2.5 line's BBL index (`02.08.00.15` in the alpha, `02.08.00.16` in the beta), so they record only those releases' tree and packed-cache identities. The H2C is a dual-nozzle model handled like the H2D; the A2L is registered exactly as its machine profile declares; a single 0.4 mm nozzle on a 330 × 320 mm bed-slinger frame. Note the H2C's per-material profiles are upstream's dedicated `… @BBL H2C 0.4 nozzle` variants (the unsuffixed Generic H2C profiles cover the 0.6/0.8 mm nozzles only).

Print Handy ships no A1 slicer profiles. Before the first A1 slice, generate them from your own OrcaSlicer (see [Set up the A1 profiles](#set-up-the-a1-profiles)).

## Initial support: the Bambu Lab H3C (not yet a selectable model)

**Initial support, unverified on hardware.** The H3C is Bambu Lab's nine-tool tool changer (the Swaptek system: nine PrintTools, each with its own heater, cutter and filament path, parked on a rack and swapped by one toolhead extruder), launched 2026-11-03 with Bambu Studio 02.09.01.70, the first Studio release that carries it. Print Handy records what is known about it, but **you cannot select it yet**: there is no `bambulab.h3c` model key, and picking one fails like any unknown model.

What is recorded (in `print_handy/printers/bambulab/facts.py`, the `H3C` block), read from Bambu Studio at the pinned commit `56a57d00a591360cf41bf21383915ee5bf357be5`; its device definition `resources/printers/N10.json` and the H3C machine profiles:

| Fact | Value |
|---|---|
| Build volume | 330 × 320 × 325 mm, no bed exclusion |
| Tools | 9 PrintTools; 0.4 mm hardened steel fitted; 0.2 / 0.6 / 0.8 mm offered |
| Temperatures | Hotend to 350 °C, bed to 120 °C (device-definition ranges) |
| Filament system | AMS 3H, nine slots (published spec; not read from bambulab.com directly) |
| Serial prefix | `32X`; the first three characters of an H3C serial |

What already works: HMS decoding. The HMS table regenerated from Bambu Studio 02.09.01.70 carries a `32X` per-model section (597 HMS and 120 print-error overrides), and the decoder takes its per-model prefix from the configured serial, so H3C fault texts resolve per model once that table is in, with no further code change. The prefix and the resolution path are pinned by tests now, against a synthetic 32X table.

What blocks full support, precisely: Print Handy slices Bambu printers from OrcaSlicer profiles whose flattened SHA-256 identities it has checked (see the model table above). The pinned OrcaSlicer v2.5.0 carries **no H3C profiles at all**, its BBL index and machine folder were checked and contain none, so no identity can be computed, and a model registered without one would fail its profile checks rather than refuse cleanly. The H3C waits in the same posture the H2C held before the 2.5 line arrived: when a pinned OrcaSlicer release brings H3C profiles, registration lands the same way (identities over that tree and its packed cache, a model module, a registry entry, family tests). Bambu Studio's own H3C profiles exist, but Print Handy has never taken Bambu Studio profiles as an identity source, and this change does not start.

Also not covered by initial support: the AMS 3H and the tool rack are not driven or modeled beyond the facts above, Studio 2.9's mixed-nozzle-diameter slicing is not handled, and nothing about the H3C has been tested on hardware; there is no H3C on the bench.

## Set up the A1 profiles

The A1's OrcaSlicer machine, process and filament profiles are OrcaSlicer's and Bambu Lab's work, under the AGPL-3.0, so they are not in this repository. Print Handy builds its A1 templates from the OrcaSlicer installed on the same machine:

```bash
python3 scripts/bambu-templates.py --printer a1 --resources <OrcaSlicer resources folder>
```

- **The resources folder** is the one holding `profiles/BBL.json` and `profiles/BBL/`, or, OrcaSlicer 2.5 and later, the packed preset cache `profiles/BBL.opc` on its own. For the Flathub build it is under `$(flatpak info --show-location com.orcaslicer.OrcaSlicer)/files/share/`. For an AppImage it is `squashfs-root/resources`.
- **What it does.** It resolves the A1 profiles' `inherits` and `include` chains and refuses unless they hash to an OrcaSlicer version Print Handy has been checked against: OrcaSlicer 2.4.2 (BBL vendor profiles `02.01.00.19`), OrcaSlicer v2.5.0-alpha (BBL vendor profiles `02.08.00.15`) and OrcaSlicer v2.5.0-beta (BBL vendor profiles `02.08.00.16`), the 2.5 releases from their JSON tree or their packed preset cache. The shipped slicer is now v2.5.0, whose BBL profiles are unchanged from the beta, so 2.5.0 installs hash to these same recorded beta identities. It then writes the profiles and Print Handy's overrides into `<state_home>/state/orca-profiles/bambulab_a1/`, never into the checkout. Nothing is downloaded.
- **`--check`** writes nothing. It reports whether the installed OrcaSlicer matches and whether the generated folder is current.
- **Missing, edited or unrecognised profiles refuse everything.** Slicing, `verify-gcode`, packaging and `print-now` all fail closed with the command to run. That includes another OrcaSlicer version.
- **OrcaSlicer 2.5's packed profiles are read directly.** It packs its vendor profiles into one `BBL.opc` preset cache; Print Handy decodes the cache (`print_handy/printers/bambulab/preset_cache.py`) and flattens it like the tree. The two packagings of one release flatten differently, the cache holds only the options OrcaSlicer's own config definitions know, under their current names, so each packaging has its own recorded identity in `facts.py`. A cache that fails its format checks (magic, format version, size, CRC32, vendor) is refused, as is any other unchecked version.
- **Upgrading from an older version?** Run the command again. A folder generated earlier keeps slicing PLA, but PETG, TPU and PVA are refused until it is regenerated, with this command in the message.

## Filament profiles per material

The A1 and P1S each have one checked OrcaSlicer filament profile per material. A slice uses the one for the material in the hardware record (the loaded spool) and refuses a material that has none. It never slices one material with another's profile:

| Material | A1 profile | P1S profile |
|---|---|---|
| PLA (the default; also PLA+, Matte or Silk PLA and PLA-CF) | `Generic PLA @BBL A1` | `Bambu PLA Basic @BBL X1C` |
| PETG | `Generic PETG @BBL A1` | `Bambu PETG Basic @BBL X1C` |
| TPU | `Generic TPU @BBL A1` | `Bambu TPU 95A @BBL X1C` |
| PVA | `Generic PVA @BBL A1` | `Bambu PVA @BBL X1C` |

- **How a material is matched.** By its base polymer, the way Print Handy already groups materials for temperatures: PLA+, Matte PLA and PLA Pro use the PLA profile, and so do fibre-filled fills (PLA-CF), as before. The guard still requires a hardened steel nozzle for those. A label Print Handy does not group, such as `PETG-HF`, does not match PETG and is refused.
- **What the profile supplies.** Its cooling, flow, speeds, retraction and its filament start and end G-code. The hardware record still sets the material label, density, nozzle and bed temperatures and the colour, as before.
- **Generated, not shipped.** `bambu-templates.py` generates all four from your OrcaSlicer. It refuses, and writes nothing, unless each one hashes to the identity recorded in `facts.py`, has the expected `filament_type` and lists the printer in `compatible_printers`. The default stays `filament.json`; the others go to `filaments/<MATERIAL>.json`, with their flattened vendor copies in `upstream/filaments/`. Each is verified again whenever it is used.
- **Refused:** a material with no checked profile (for example ABS, ASA, PA or wood fill) is refused before the slicer runs. The message lists the checked materials. Bambu lists ABS, ASA, PC, PA and PET as not recommended on the A1, and the guard refuses them anyway.
- **Other models** (A1 mini, P1P, X1 family, P2S, H2S) still have only their PLA default. A PETG record on them is refused with the same message rather than sliced as PLA.
- **Not yet:** choosing another vendor profile for a material (for example `Bambu PETG HF @BBL A1`). Your own presets are supported, see [Your own filament presets](#your-own-filament-presets). An `[orca] templates` override may carry its own `filaments/<MATERIAL>.json`, but it is never the reference: the guard still holds the output to the generated profiles.

### Your own filament presets

A vendor profile is one size for everyone. If you tune filament in Bambu Studio or OrcaSlicer, your own temperatures, flow, retraction, you can register that preset with Print Handy, and slicing then uses it for its material instead of the vendor profile:

1. In Bambu Studio or OrcaSlicer, save your filament preset and export it (the preset JSON).
2. Register it against the same installed OrcaSlicer you generated the templates from:

   ```bash
   python3 scripts/bambu-templates.py --printer a1 --resources <OrcaSlicer resources folder> --import-filament <preset.json>
   ```

   `--material PETG` may be added to confirm the material; it is checked against the preset, never chosen by it.
3. To go back to the vendor profile: `python3 scripts/bambu-templates.py --printer a1 --remove-filament PETG`. Re-running the import replaces a registered preset. Regenerating the templates keeps it.

**How a preset earns trust.** A preset is partial: an `inherits` parent plus your overrides. It is flattened against your installed OrcaSlicer and refused, with nothing written, unless:

- its parent is one of the printer's checked filament profiles (for example `Generic PETG @BBL A1`); a preset over an unchecked parent is refused;
- it sets only filament settings its parent has; a machine or process setting (layer height, printer model, …) refuses the import;
- its `filament_type` stays its parent's material, and that material is not one Bambu lists as not recommended for the model;
- it does not change the filament start or end G-code; those stay the checked parent's, so the guard's vendor-block checks are unchanged;
- its values stay inside the model's recorded limits: nozzle and bed temperatures within `max_hotend_c` / `max_bed_c`, fan speeds 0–100 %, retraction and maximum volumetric speed positive.

**Where it lives, and how it is held to account.** The flattened preset, its template and a manifest entry (preset name, parent, SHA-256) are written under the generated folder's `operator/` subfolder (`<state>/orca-profiles/bambulab_a1/operator/`), on your machine only, never in the repository. While a preset is registered it replaces the vendor profile for its material everywhere the material selects a profile; a file that names the vendor profile in `filament_settings_id` is still checked against the vendor profile itself. Every use re-verifies the preset exactly like a generated template: the flattened copy must hash to the manifest, the template must be that copy plus Print Handy's overrides, and its filament G-code must still equal the checked parent's. An edited or stale preset refuses the slice with the re-import command. The Discord preview names it: "Black PETG, your preset 'PETG fast'" instead of the vendor profile name.

One preset per material, and it applies to the Bambu model it was imported for (`--printer` chooses which). The preset's settings (flow, cooling, speeds, retraction) come from the preset; the hardware record still sets the material label, density, nozzle and bed temperatures and the colour, exactly as with a vendor profile.

## How the A1 fits the vendor and printer profiles

The A1 is one printer module under the existing Bambu Lab vendor profile. See [Printer profiles](PRINTER-PROFILES.md) for the general layout.

| File | Role |
|---|---|
| `print_handy/printers/bambulab/__init__.py` | `VENDOR`: the only authority for connection (`bambulab_lan`), start code (`bambu`), slicers, file formats and delivery (`discord`, `bambu_handoff`; never `printer`) |
| `print_handy/printers/bambulab/a1.py` | `MODEL` for `bambulab.a1` with `vendor=VENDOR`: bed, height, nozzle, slots, adapter, templates, baseline file |
| `print_handy/printers/bambulab/adapter.py` | `BambuAdapter` (the old `BambuOfflineAdapter` name is kept as an alias): upload/start calls raise `LiveUnsupported`; when LAN status is configured it also reads telemetry through `client.py`. `connection` is read from `VENDOR`. `verified_slicers` allows Bambu Studio (default) and OrcaSlicer |
| `print_handy/printers/bambulab/ams.py` | Read-only decoding of the status snapshot's AMS block: units and trays (presence from `tray_exist_bits`), the active tray, the external spool and Filament Backup, normalized onto the status shape by `adapter.py`. Sends nothing |
| `print_handy/printers/bambulab/guard.py` | The A1 G-code checks, run after slicing, before packaging and at `print-now` |
| `print_handy/printers/bambulab/facts.py` | Supplemental metadata only: each hardware value the guard enforces, with its source and whether it was verified, plus the profile names, Print Handy's overrides and the SHA-256 identity of the checked OrcaSlicer profiles. It decides nothing about connection, slicer or delivery |
| `print_handy/printers/bambulab/profiles.py` | Generates the A1 templates from an installed OrcaSlicer and re-verifies them before every use |
| `<state_home>/state/orca-profiles/bambulab_a1/` | The generated templates, on the operator's machine only (`MODEL.generated_templates`) |

The P1S is `bambulab/p1s.py`: its facts in `facts.P1S` (profile names, checked identity, 250 mm Orca height, bed exclusion, motion envelope), a `BambuAdapter` subclass and a `MODEL` with `generated_templates=True`. Its templates go to `<state_home>/state/orca-profiles/bambulab_p1s/`. Every Stage 1 model follows exactly this shape: `bambulab/<model>.py`, a facts block in `facts.py`, and templates under `<state_home>/state/orca-profiles/bambulab_<model>/`.

## What works and what does not

| Capability | Status |
|---|---|
| Slicing with OrcaSlicer | Supported with the shipped OrcaSlicer v2.5.0's own profiles (unchanged from v2.5.0-beta), generated locally from its JSON tree or its packed preset cache; real-slicer CI runs the same pinned bundle, so template generation from the packed cache is exercised there. Folders generated from OrcaSlicer 2.4.2 keep verifying against its recorded identity. A MakerWorld or Bambu Studio 3MF's project settings carry the designer's structure over the template (layer height, walls, infill, brim, supports); that includes where supports go, `support_threshold_angle`, `support_style`, `support_critical_regions_only`, `support_remove_small_overhang`, not only the support switch and type. Support filament, speeds and Z gaps stay the printer template's |
| Fit checks against 256 × 256 × 256 mm | Supported; the same deterministic checks as other models. Geometry is centred on the printer's own bed (see below) |
| G-code validation | Supported: after slicing, before packaging, at `print-now` and `verify-gcode` |
| Delivery | `discord` and `bambu_handoff` by default; `printer` is offered but opt-in (add it to `[delivery] methods`). A `.gcode.3mf` package is built in Bambu Studio's export layout, with the plate previews the printer screen, Bambu Handy and Bambu Studio read: `Metadata/plate_1.png` (512 px) and `Metadata/plate_1_small.png` (128 px), taken from Orca's thumbnail block when the G-code has one (it does not for Bambu printers), else rendered offline from the geometry the slicer was given (`core/plate_thumbnail.py`, numpy and Pillow, no OpenSCAD). `top_1.png`, `pick_1.png` and the Studio project files are not written, and `slice_info.config` names no images. A preview that cannot be made is logged and the package is built without it, as before. Opening the package in Bambu Studio or Bambu Handy, seeing the preview on the printer, printing it from SD, or printing a plain `.gcode` on an A1 has **not** been tested. A package over the Discord upload cap is reported with its host path, never offered for download (see [Handoff packages and the Discord upload limit](#handoff-packages-and-the-discord-upload-limit)) |
| Print | Opt-in: with `printer` in `[delivery] methods`, LAN configured and the printer in Developer Mode, `print-now` uploads the package and starts it through every Print Handy gate, see [Printing over LAN](#printing-over-lan-opt-in). Without the opt-in, `print-now` verifies the file, then refuses; the bed-clear approval is not used up |
| Bambu Studio as slicer | Default for every Bambu Lab printer; `[printer] slicer: orca` opts out. Real Bambu Studio A1 slices run in the CI `Slicer test (bambu)` leg (Flathub Bambu Studio 2.8.2.61): Bambu Studio as the primary slicer and as the Orca fallback both complete the production flow, slice, verify, package. Bambu Studio identifies its output with its header block (`; HEADER_BLOCK_START`, then `; BambuStudio <version>`); it writes no `; generated by` line, and identity is read from the header block |
| Live telemetry | Read-only, opt-in: with `[bambu] lan_host` + `lan_serial` set and the access code supplied as a secret, the health check reads one MQTT status snapshot (state, progress, temperatures, and the AMS state the printer reports, loaded trays with material/color/remaining, the active tray, the external spool and Filament Backup), see [LAN status](#lan-status-read-only-opt-in). **Not tested on a printer.** The Bambu listener also holds a long-lived MQTT session and reports status/events through the listener contract, see [LAN status](#lan-status-read-only-opt-in) and [Listener domains](LISTENERS.md). The camera is covered separately, see [Camera](#camera) |
| Upload | FTPS to the printer's storage root with the remote size read back for verification; used by the LAN print path, never on its own |
| Print control | Start via the gated print path (below). Pause/resume/stop commands exist on the LAN client (`adapter.control`) for the operator tooling that drives them; the Discord print-control module is external to this repository and does not call them yet |
| Discord color question | The configured spool in `printer_hardware.json` (`spool.color` or `spool.name`, and `spool.material`) is offered as slot 1, so no `filament_slots` entry and no Spoolman spool are needed. With `spool.spoolman_id: null` the job records no Spoolman ID; a spool bound to Spoolman keeps its ID on the job, and Spoolman lookups only warn. The material, nozzle and temperature checks still apply |
| AMS Lite multi-color | Not supported at intake: per-part color jobs are refused, as are multi-part and multi-plate sources. A prepared multi-filament file reaches the LAN print gates through `print-now`, see the AMS mapping notes under [Printing over LAN](#printing-over-lan-opt-in) |
| Color architecture | The single-nozzle AMS models are classified `multi_slot` (see [Color architectures](PRINTER-PROFILES.md#color-architectures)): the AMS is their slot system, four slots per unit from each model's profile facts, expressed through the shared adapter vocabulary (`slot_count`, readiness, the printer's reported tray binding). This changes no payload and no behavior: Print Handy still does not slice per slot for a Bambu printer, a job's filament slots are the sliced file's own, matched to reported AMS trays at print start, so the record-bound IFS machinery (slot questions from the record map, per-slot slicing, print-now's record-slot gate) does not engage. The dual-nozzle models (H2D, H2D Pro, X2D, H2C) stay `single` deliberately: two independent extruders, each AMS-fed, are neither the one-extruder `multi_slot` shape nor the `multi_tool` tool-changer contract |
| P1S | Supported offline, like the A1: `bambulab.p1s`, with profiles generated by `--printer p1s` from the pinned OrcaSlicer (v2.5.0; its BBL profiles are unchanged from v2.5.0-beta, and a 2.4.2 folder keeps verifying). Note the P1S's default filament is the dedicated `Bambu PLA Basic @BBL P1S 0.4 nozzle` profile in the 2.5 line, the X1C profile no longer lists the P1S as compatible, so regenerate P1S templates from a 2.5 install (2.5.0-alpha, 2.5.0-beta or 2.5.0) rather than reusing a folder whose profile names no longer resolve. Not tested on a P1S |
| Materials | A1 and P1S: PLA, PETG, TPU and PVA, each with its own checked vendor filament profile (see [Filament profiles per material](#filament-profiles-per-material)). Other models: PLA only. Your own Bambu Studio / OrcaSlicer filament presets can be imported as the checked profile for their material (see [Your own filament presets](#your-own-filament-presets)). Only the offline tests and a profile generation from OrcaSlicer v2.5.0-beta (tree and packed cache; the v2.5.0-rc profiles and the shipped v2.5.0's are unchanged from it) have exercised PETG, TPU and PVA; no real-slicer or printer run yet |
| A1 mini, P1P, X1, X1 Carbon, X1E, P2S, H2S | Supported the same way: one model module and one facts block each, profiles generated by `--printer a1mini` / `p1p` / `x1` / `x1c` / `x1e` / `p2s` / `h2s`. Not tested on hardware |
| H2D, H2D Pro, X2D | Supported the same way: dual-nozzle models with per-slot AMS mapping at print start. Not tested on hardware |
| H2C | Supported the same way: `bambulab.h2c`, dual nozzle with per-slot AMS mapping, profiles generated by `--printer h2c` from OrcaSlicer v2.5.0 (tree or packed cache; its BBL profiles are unchanged from v2.5.0-beta, and the 2.5.0-alpha identities are retained). Not tested on hardware |
| A2L | Supported the same way: `bambulab.a2l`, single nozzle with AMS Lite (not driven), profiles generated by `--printer a2l` from OrcaSlicer v2.5.0 (tree or packed cache; its BBL profiles are unchanged from v2.5.0-beta, and the 2.5.0-alpha identities are retained). Not tested on hardware |

Supported firmware and authentication modes for live use: LAN Mode with the printer's LAN access code for status (the setup wizard collects both); printing additionally requires Developer Mode, see [Printing over LAN](#printing-over-lan-opt-in). `services.moonraker_url` and `services.octoprint_url` must stay empty for a Bambu Lab model; settings refuse to load otherwise. The Moonraker listener does not apply, Bambu Lab has its own listener domain; printer-rendered timelapse download and cold pull do not apply (Print Handy's own captured timelapse does, see [Camera](#camera)). The camera watchdog does apply, to prints Print Handy launched, driven by the launched-print monitor rather than the listener).

In the combined Docker container the supervisor honors the listener registry: for a Bambu Lab model it starts the Bambu listener (never the Moonraker listener), and the healthcheck requires that listener's heartbeat. With LAN not configured the Bambu listener idles without any network contact, so the container starts, stays up and reports healthy with just the bot (with a token) and the bundled Spoolman, exactly as before.

### Handoff packages and the Discord upload limit

A `.gcode.3mf` is the plate's G-code deflated, so a long print makes a big package: one real A1 part (329 g, 11 h) sliced to 83 MB of G-code and a 22.9 MB package, far over [`discord.upload_max_bytes`](SETTINGS.md) (default 9.5 MB, under Discord's 10 MB cap for an unboosted server). Since the package is already compressed, zipping cannot help. The post-slice menu therefore sizes the package before it offers "Send the files here": not yet built, it is estimated from the deflated G-code; once "Package for Bambu (no upload)" has run, its exact size is used. When it cannot fit, the menu and the "Bambu package ready" reply say so with the size, the cap and the package's path on the Print Handy host (`<archive>/<job>/sliced/.../bambu/plate_<n>.gcode.3mf`, next to the G-code), and the send button is left out. Copy the package from that path to the printer's SD card or to Bambu Studio yourself; Print Handy has no download server. A package under the cap is attached as before.

## LAN status (read-only, opt-in)

Off by default. To enable it, set both keys and supply the secret:

```yaml
bambu:
  lan_host: 192.168.1.50   # the printer's LAN address
  lan_serial: 00M00A000000000   # its serial number (Settings > WLAN; case is normalized)
  lan_access_code_env: BAMBU_LAN_PASSWORD   # the default; names an environment variable
```

Put the printer's LAN access code in the environment variable that `lan_access_code_env` names (the setup wizard stores it in the profile `.env` as `BAMBU_LAN_PASSWORD`). Never put the code itself in a config file: it is also the password for the printer's other LAN services. The log redactor covers `*_PASSWORD` variable names.

What happens then: the printer health check (`printer_state.check`, used by the doctor/status paths) opens one short MQTT-over-TLS connection to port 8883 as user `bblp`, asks for the version and a full status snapshot (`pushall`, A1/P1 printers otherwise report only changes), normalizes state/progress/temperatures into the same shape other printers report, and disconnects. It publishes nothing else. The peer certificate chain is verified against Bambu Lab's CA bundle shipped at `print_handy/printers/bambulab/bambu_ca.pem`; hostname checking is off because the certificate names the printer serial, not its IP. There is no option to disable certificate verification. Busy state, a print error, active HMS errors, and the firmware signature bit (Developer Mode off, writes would be blocked) are reported as readiness problems. Print errors and HMS errors are decoded into plain text (with the code) from Bambu's own HMS tables as shipped in Bambu Studio, see [HMS error translation](BAMBU-CONNECTIVITY.md#hms-error-translation-306); an unlisted code is reported with its raw code, and decoding never changes gating. A wrong access code is reported with the remedy (re-read the current code on the printer; it changes after a factory reset), not retried forever.

The same snapshot's AMS block is decoded read-only: each unit (AMS, AMS Lite, AMS 2 Pro or AMS HT, typed from the printer's own version report, never from the model name) with its trays, present (from `tray_exist_bits`, the report's presence authority), material, color, percent remaining and which tray is active, plus the external spool and the Filament Backup (auto-switch) state, which is unknown rather than off when the printer does not report it. The decoded state rides on the normalized status (`ams`), and when the printer's active tray disagrees with the recorded spool (material, or color when both sides are hex) the health check adds a warning naming both; the record is kept and nothing is resolved automatically. Reading is all it does: the AMS is never driven, no AMS command exists in this stage, and jobs stay single-color.

**The Bambu listener.** With LAN status configured, the Bambu listener (`print_handy/printers/bambulab/listener.py`, the `bambulab_lan` listener domain, see [Listener domains](LISTENERS.md)) holds one MQTT session open instead of polling snapshots: it requests `get_version` + `pushall` on every connect, merges the printer's delta pushes into a persistent state, and emits the normalised status/event contract (started/paused/resumed/cancelled, completion, error with decoded HMS, snapshot-available). Its connection discipline follows an audit of Bambuddy (protocol facts only): only `push_status` frames count as telemetry (command acknowledgements never update state), 60 s without a message means the session is stale, and a reconnect is a hard reset, a fresh MQTT client, so a publish queued on the dead session can never replay, after a 30 s cooldown. The listener is read-only: the only payloads it ever publishes are its own `get_version`/`pushall` requests. Without LAN configured it idles with a heartbeat and never touches the network, so an offline Bambu configuration behaves exactly as before. **Not tested on a printer.**

What it is not: the listener adds no AMS detail beyond what the status snapshot carries (the Moonraker listener does not run for Bambu). The camera is separate from status, see [Camera](#camera) below. Newer firmware's packed temperature words (`device.*.info.temp`) are not decoded; the classic temperature fields are used. All of this is implemented from a research document and the maintained integrations it cites, and **none of it has been run against a real Bambu printer yet**; treat the first live use as a test, and expect the unknowns listed in the research document (certificate chain on real firmware, single-MQTT-session contention with Bambu Studio/Handy, snapshot behavior) to apply.

## Camera

With LAN status configured, the camera shows where a Moonraker printer's camera shows:

- **Status.** `/print-stop` and the status paths fetch one snapshot with the status and post it alongside. With no LAN configured there is no image and no error; the status carries a plain "camera is not configured" note instead.
- **Completion.** When a print Print Handy launched finishes, the completion post carries the final snapshot, like the Moonraker completion post.
- **Watchdog.** While a print Print Handy launched runs, the launched-print monitor polls status and runs the same visual checks as the Moonraker watchdog, the same vision verdict path and the same `[visual_watch]` sensitivity profiles, and posts an alert with the snapshot and Pause / "It's fine" / "Stop asking for this print" buttons when a check looks wrong ("Stop asking for this print" mutes camera posts for that print only; the next print asks again). A completely dark frame is a camera problem, not a print verdict: it skips the vision check and, at most once per print, posts a plain dark-camera notice instead of an alert. The watchdog is alert-only unless you turn on `visual_watch.auto_pause_early`: then a risky print (one with a slicer support finding or a print-anyway override) is checked on every early layer, and two independent suspicious early-layer views send one pause through the same path as the alert's Pause button, which re-reads the printer and refuses a different file. It never cancels. Otherwise Pause is the operator's button press. A check that hangs is abandoned after 150 seconds with one notice, camera and vision failures alert once per episode, and a print whose firmware reports no layer is checked on a timer. Prints started outside Print Handy (Bambu Studio, the printer screen) are not watched, the camera watchdog is driven by the launched-print monitor, not by the Bambu listener.

The A1/P1 camera is a proprietary stills protocol: one JPEG frame over TCP port 6000, authenticated by a packet that embeds the LAN access code; the same secret as status, via the same `lan_access_code_env` indirection; there is no separate camera setting. Snapshots are bounded (8 MiB) and JPEG-validated before use, exactly like the Moonraker camera path, and camera errors never include the access code.

The X1 family's camera is an RTSPS stream (port 322, `bblp` / the LAN access code, path `/streaming/live/1`), and Print Handy does not embed an RTSP client: X1 stills come through the go2rtc fan-out instead. When go2rtc is running with this printer's stream as its configured source, the same status, completion and watchdog places show X1 stills, read from go2rtc's loopback still of that stream. To enable it: turn on `[timelapse] capture_enabled` or `[live_stream] enabled` (either one starts go2rtc) and set `[timelapse] capture_source_url` to the printer's RTSPS URL, `rtsps://bblp:<access code>@<printer address>:322/streaming/live/1`. When go2rtc is not running, or is serving a different camera, an X1 camera request says exactly that and what to enable, there is no silent missing image and no fallback RTSP client. P2S/H2 camera protocols are not in the verified connectivity survey, so those models report no verified camera protocol rather than guessing. Timelapse is covered separately below.

### Timelapse

With `[timelapse] capture_enabled` on, a print Print Handy launched also gets a timelapse, captured by the same launched-print monitor and delivered with the completion post alongside the final snapshot, the shared timelapse pipeline: frames under the `[timelapse]` capture settings, the one encoder choice with CPU fallback, and the video sized to the configured platform's upload limit. The frame source is the camera above, per family:

- **A1/P1:** frames are port-6000 stills taken at the `[timelapse] capture_interval_s` cadence. This camera is a low-frame-rate stills camera, so the honest expectation is a slideshow-style timelapse, not smooth video; a shorter interval cannot make the hardware faster, it only asks more often.
- **X1 family:** frames are go2rtc stills of the RTSPS stream; the same enablement as X1 stills above (go2rtc running with this printer's stream as its source). Without it there is no capture, and the monitor logs the reason once; the print and its completion post are unaffected.

Prints started outside Print Handy are never captured (nothing owns their lifecycle), and a capture or encode failure degrades to the completion post without the video, never to silence. Timelapse is covered by fake-transport tests only, like the rest of the Bambu LAN path, it has **not been run against a real Bambu printer yet**; treat the first live use as a test.

**None of this has been run against a real Bambu printer yet.** The 6000 protocol and the go2rtc stills path are implemented from the maintained clients cited in [BAMBU-CONNECTIVITY.md](BAMBU-CONNECTIVITY.md) and covered by fake-transport tests; treat the first live use as a test.

## Printing over LAN (opt-in)

A second, separate opt-in on top of LAN status. All three must hold before a Start button exists at all:

1. LAN status is configured, as above.
2. `printer` is added to `[delivery] methods` in your config, or from the environment as `PRINT_HANDY__DELIVERY__METHODS=discord,bambu_handoff,printer` (comma- or space-separated; in Docker this avoids replacing the image's `docker.yaml` with a mounted copy). It is offered by the vendor profile but is **never a default**, precisely so that nobody gets a print button by accident.
3. The printer is in **Developer Mode** (Settings, with LAN Mode). Current firmware refuses unsigned control commands otherwise; Bambu Handy and cloud access stop working in that mode. Print Handy detects the block from the status snapshot's signature bit and refuses before sending anything, naming the remedy.

With all three, the post-slice menu and `print-now` gain the same Start flow other printers have, through the same gates, in the same order (`core/model_job._print_bambu`, modelled on the OctoPrint path): live readiness is checked (idle, no print error, no HMS errors, signature bit clear), the G-code is verified offline, the package is built by the same code as the handoff delivery and must contain exactly `Metadata/plate_1.gcode` (multi-plate packages are refused, which plate a multi-plate `project_file` starts is a recorded unknown), then a submission lease is taken, readiness is checked **again** inside the lease, the single-use bed-clear approval for that exact G-code is consumed, the package is uploaded over FTPS with its remote size verified, and the `project_file` command is sent. The start is then confirmed from fresh status snapshots, this file reported as preparing or running (`gcode_state` `PREPARE` or `RUNNING`), never assumed from the command being accepted. The printer is polled every 10 s for `[bambu] start_confirm_s` seconds (default 90, settable 1–300, see [Settings](SETTINGS.md)): an A1 can take over a minute after `project_file` to unpack a large package before it reports anything. Every snapshot in that window is logged at INFO and appended to the job's `TIMING.jsonl` as a `bambu_start_confirm` line (state, `gcode_state`, file, `mc_print_stage`), so an unconfirmed start leaves evidence of what the printer said. If the window ends without the printer reporting the file, the job is marked submitted/awaiting-confirmation with a do-not-resubmit warning, like OctoPrint, and the launch lease's detail records the last state the printer reported; the operator then checks the printer and runs `python3 -m print_handy.core.model_job reconcile-launch --started` or `--not-started`.

Details worth knowing:

- The start command sends every calibration flag **off** (bed levelling, flow, vibration, first-layer inspection); the behavior the Home Assistant integration's start service uses. The printer runs the file as sliced; Print Handy does not ask for calibration passes that were not part of the slice.
- **AMS mapping.** Before the bed-clear approval is consumed, the print path resolves which tray prints each of the file's filament slots (`print_handy/printers/bambulab/ams_mapping.py`). A mapping stored on the job (`ams_mapping` in JOB.json, canonical tray ids: AMS tray `ams_id × 4 + slot`, AMS-HT unit ids 128–135, external spool 254/255, −1 unmapped) is trusted verbatim; otherwise the mapping is recomputed from the trays the printer itself reports in the in-lease status snapshot, matching materials slot by slot, on a dual-nozzle printer, a slot only takes a tray whose AMS unit reports the extruder that slot prints with. A multi-filament job that cannot be fully mapped is refused there, with the reason, and the approval is left intact; nothing is uploaded. The command then carries the mapping in both wire forms (`ams_mapping`, with the external spool converted to −1, and `ams_mapping2` as `{ams_id, slot_id}` pairs), and `use_ams` as a JSON boolean, true when any slot maps to a real AMS tray, false for an external-spool-only start. A single-filament job whose material matches nothing the printer reports keeps the earlier start (sliced for the recorded spool, which telemetry may not report); the print result records which source the mapping came from (`job`, `snapshot` or `legacy`). The command's hygiene follows that audit: sequence id `20000`, one fresh submission id shared by project/task/subtask, an empty md5, and no `nozzle_mapping` unless a caller supplies a resolved one. None of this is hardware-verified yet, it is pinned by offline fake-MQTT tests only.
- Pause/resume/stop commands exist on the client (`BambuAdapter.control`), but the Discord print-control module lives outside this repository and does not call them yet; pausing a Bambu print from Discord is not wired up.
- **Live evidence so far is one run.** On 26 Oct 2026 an A1 (Developer Mode, Docker image 2.1.3) accepted a 22.9 MB Print Handy package via `project_file` from the storage root and printed it, answering the research document's live unknown #1 for that printer, and it reported the file only after the old single confirmation snapshot, which is why confirmation now polls over a window. Everything else here is still offline-tested only; treat a first start on your own printer as a test print you watch.
- The start summary's estimate comes from the sliced G-code. Bambu Studio writes it as `; model printing time: …; total estimated time: …` rather than the OrcaSlicer/PrusaSlicer `estimated printing time (normal mode)` line; both forms are read now, so a Bambu Studio plate no longer shows "Estimated total: unknown".

The LAN path above (telemetry, then upload, then gated print start) is built from the protocol evidence in [Bambu Lab LAN protocol evidence](BAMBU-CONNECTIVITY.md): MQTT and FTPS, the LAN Mode plus Developer Mode requirement current firmware puts on control, and the unknowns that still need a live test before more is claimed.

## What the guard checks, and what it trusts

A sliced file is untrusted. `guard.gcode_problems` refuses it unless all of the following hold:

1. **Foreign code.** No Klipper or ZMod code anywhere: `START_PRINT`, `MESH=`, `PRINT_START`, `END_PRINT`, any Klipper `UPPER_SNAKE` command, or the ZMod `; MD5:` line. `base.start_code_problems` independently requires every line to be a G, M or T code.
2. **Identity.** `printer_model = Bambu Lab A1`, and the file carries a verified slicer identity: OrcaSlicer's `; generated by OrcaSlicer …` line, or Bambu Studio's header block (`; BambuStudio <version>` directly under `; HEADER_BLOCK_START`). Any other or missing identity is refused. A `; generated by BambuStudio` line is not a Bambu Studio identity; no released Bambu Studio writes one. Either producer's output then faces every check below unchanged.
3. **Split.** The file divides into prelude, toolpath and epilogue (`slice_guard.split_toolpath`):
   - Orca 2.5+ marks the boundaries with `MACHINE_START_GCODE_END` and `MACHINE_END_GCODE_START`.
   - Orca 2.4 has no such tags. There the toolpath runs from the first `; CHANGE_LAYER` (or `;LAYER_CHANGE`) to the last `Custom` role tag (`; FEATURE: Custom` or `;TYPE:Custom`).
   - These markers are comments, so they only split the file. They are never a reason to trust a block.
4. **Vendor blocks.** The prelude and epilogue are matched line by line against the **verified generated** vendor templates (`guard.vendor_block_problems`):
   - Each executable line must be the next template line. A placeholder matches exactly one token.
   - Only lines inside an Orca `{if}` block may be skipped. Every other template line must be present, so heater-off lines cannot go missing.
   - The only other lines allowed are Orca's own non-motion preamble codes: `M73`, `M201`–`M205`, `G90`, `G21`, `M82`/`M83`, `M106`/`M107`, `M981`.
   - So a vendor block cannot be edited, reordered, extended or truncated.
   - An `[orca] templates` override is never the reference. A template start or end that differs from the verified profile is refused before Orca runs.
   - Missing or unverified profiles refuse the file.
5. **Toolpath motion.** A modal interpreter (`slice_guard.toolpath_motion_problems`) runs through the whole file. In the toolpath:
   - **State it tracks.** G90/G91 positioning, M82/M83 extruder mode (G90/G91 make the extruder mode unknown until the next M82/M83), G92, and the XYZ position. The position is never carried into the toolpath from the vendor start.
   - **Where moves may go.** A relative move needs a known starting position. Every move stays inside the motion envelope, with Z from 0 to height + 1 mm.
   - **Extrusion.** Every possibly extruding move (an E move in an unknown mode counts) must be at a known position on the 256 mm bed at 0 < Z ≤ 256.
   - **Refused outright.** Homing, probing other than Bambu's in-print head-wrap probes (G39, G39.3), and XYZ coordinate resets. Arcs are refused too, with one verified exception: a non-extruding G2/G3 with an I/J centre offset, from a fully known position, whose whole circle stays inside the envelope; the form of Bambu Studio's helical end-of-print Z lift (`G17`, then `G3 … P1`), which OrcaSlicer does not emit. Extruding arcs and R-form arcs are refused.
   - **Outside the toolpath.** In the prelude and epilogue, XY moves are held to the envelope.
6. **Spoofed markers fail closed.** A spoofed, duplicated or missing marker either moves vendor lines into the toolpath, where they fail the motion checks, or moves toolpath lines into a template check, where they fail. It cannot hide code.
7. **Tools, limits and materials.**
   - No tool change in the toolpath (AMS Lite).
   - M104/M109 at most 300 °C; M140/M190 at most 100 °C.
   - Exactly one filament type. Materials Bambu lists as not recommended are refused; abrasive filament needs `nozzle.type: hardened_steel`.
   - Exactly one `filament_settings_id`, naming one of the printer's checked filament profiles. Its filament start and end G-code are the ones the vendor blocks are matched against (item 4), and its `filament_type` must be the material the file declares. A PETG file sliced with the PLA profile is refused even though its `filament_type` says PETG.
   - 0.4 mm nozzle, stainless or hardened steel.
8. **Plate type.** The file's settings block must name `curr_bed_type = Textured PEI Plate`.
9. **Hardware record.** When given, the nozzle, material and temperatures must match it.

The generated templates are trusted only while `profiles.problems` finds nothing:

- The flattened vendor profiles kept in `upstream/` still hash to an identity recorded in `facts.py`.
- Each template is exactly that profile plus Print Handy's overrides.
- The profile describes the A1 in `facts.py`: model name, 256 × 256 mm area, 256 mm height, no exclusion area, 0.4 mm nozzle.
- It contains no Klipper or ZMod code.

That makes them a faithful copy of the installed OrcaSlicer's A1 profile. It does not prove they are correct for an A1; that needs a printer.

The offline tests never see a real profile. They generate templates from an invented resource tree (`tests/printers/fixtures/orca_resources`), trusted for the test only. Its G-code is written for the tests, with placeholders, an `{if}` branch, off-bed moves and heater-off lines, and it is not Bambu or OrcaSlicer code.

## Plate type

The A1 vendor profile's default plate is the Textured PEI Plate, and its start G-code has a branch that runs only for that plate, a small nozzle offset. Orca's CLI does not take the plate from the vendor profile; it resolved Cool Plate. Print Handy's process override therefore sets `curr_bed_type: Textured PEI Plate` explicitly, and the guard refuses any other plate. Real-slicer CI reads that branch from the generated profile and checks that its lines ran. Other plates are not supported in this phase.

## Protected-settings baseline

The A1 has its own baseline file, `config/protected-settings-baseline.bambulab.a1.json`. It does not exist yet, so the baseline check only warns.

`config/protected-settings-baseline.bambulab.a1.proposed.json` is a proposal. It records what OrcaSlicer 2.4.2 writes for the A1's protected keys, and the real-slicer CI test checks that the supported slicer's output matches it. Nothing reads it as an approval. (The A1's vendor start/end G-code changed in OrcaSlicer v2.5.0-alpha; the proposal was regenerated from the new slicer's output after a line-by-line review of the changed blocks. The changes are Bambu's: a 220 °C flush temperature for PLA on non-0.2 nozzles in the start block, and in the end block a removed fixed retract, an explicit G90 and a 0.4 mm Z drop.)

Approving it is a separate, deliberate maintainer step:

1. Slice an A1 job with the supported slicer.
2. Review the protected values.
3. Run:

   ```bash
   python3 -m print_handy.core.approve_baseline approve-baseline <plate_1.gcode> \
       --baseline config/protected-settings-baseline.bambulab.a1.json
   ```

The command prints the differences and writes only that file. `--baseline` refuses the shared `config/protected-settings-baseline.json`, which stays the AD5X's.

## Geometry

Exporters that place a model for an operator-chosen scale or rotation used to centre it at a fixed 110 mm, the middle of a 220 mm bed. That puts large A1 parts off the bed. `orca_adapter._place_triangles`, `export_geometry` and `place_stl` now centre on the target printer's own bed (half its `bed_mm`):

- **220 mm beds:** unchanged.
- **A 240 mm part on the A1:** lands at 8 to 248 mm.
- **Existing printers with a local `bed_mm` override that is not 220 mm:** these now centre on their own bed rather than at 110 mm.

Known limitation: the multi-color and negative-volume exporters in `core/color_geometry.py` still centre at 110 mm. Those paths are unreachable for the single-filament A1.

## Values and sources

| Value | Source | Verified |
|---|---|---|
| Printable area 0 to 256 × 0 to 256 mm, height 256 mm | OrcaSlicer A1 profile; checked again in every generated profile | Yes. Bambu also publishes 256 × 256 × 256 mm |
| No bed exclusion area | OrcaSlicer A1 profile; checked again in every generated profile | Yes |
| 0.4 mm stainless steel stock nozzle, Textured PEI default plate | OrcaSlicer A1 profile | Yes |
| Profile identity: SHA-256 of the flattened A1 machine, process and filament profiles, one per checked packaging: OrcaSlicer 2.4.2 (BBL `02.01.00.19`), OrcaSlicer v2.5.0-alpha (BBL `02.08.00.15`) and OrcaSlicer v2.5.0-beta (BBL `02.08.00.16`), tree and packed cache | Computed from installed OrcaSlicer copies of the three releases | Yes. Hashes only |
| Hotend at most 300 °C, bed at most 100 °C | Bambu Lab A1 spec pages | **No**: read from search-result extracts; bambulab.com was unreachable from the build environment |
| 0.2, 0.6 and 0.8 mm hotends; hardened steel needed for CF/GF and glow filaments | Bambu Lab A1 spec pages | **No**, same reason. Only 0.4 mm is templated and accepted |
| PLA, PETG, TPU, PVA ideal; ABS, ASA, PC, PA, PET and fiber-filled versions not recommended | Bambu Lab A1 spec pages | **No**, same reason. Not-recommended materials are refused outright, a conservative choice |
| One filament profile per ideal material, with its SHA-256 (`orca_filaments`, and `filaments` in the identity) | Computed by running `profiles.flatten_filaments` and `profiles.sha256` over the pinned OrcaSlicer releases (v2.5.0-beta at `345efec64c7b95a05ce5cf96df0dde66a158835c`; v2.5.0-alpha and 2.4.2 retained), the same pipeline that reproduces the recorded identities | Yes. Hashes and names only; whether each profile prints well on an A1 or P1S is not tested |
| AMS Lite: 4 spools | Bambu Lab A1 Combo pages | **No**. Not used |
| `.gcode.3mf` as Bambu's job format; LAN/cloud connection types | Bambu Lab pages | **No** |
| Motion envelope X −48.2 to 267, Y −0.5 to 262.5 mm; 1 mm travel headroom above 256 | Derived from the vendor G-code moves | **No**: not a Bambu figure and not a hardware certification |

All values are in `print_handy/printers/bambulab/facts.py` with the same source labels.

## Provenance and licence

**Maintainer approval needed.** The A1 profiles are OrcaSlicer's, which took them from Bambu Lab's Bambu Studio, and both are AGPL-3.0. This repository does not contain them. Generating them from the operator's own OrcaSlicer at install time is the approach this change takes. Whether that basis is acceptable is for the maintainer, or legal review, to approve. This document is not that approval.

What this repository holds, and what it does not:

| Item | In the repository | Kind |
|---|---|---|
| OrcaSlicer/Bambu A1 machine, process and filament profiles, and their G-code | **No.** Generated per installation into `<state_home>/state/orca-profiles/bambulab_a1/` | Third-party, AGPL-3.0, read from the operator's OrcaSlicer |
| Profile names, the `ORCA_OVERRIDES` keys and values, settings-ID keys | Yes, `facts.py` | Original: identifiers and Print Handy's own settings |
| SHA-256 identity of the checked profiles and hardware numbers | Yes, `facts.py` | Facts: hashes and measurements, no profile text |
| The material to filament-profile names (`orca_filaments`) and their hashes | Yes, `facts.py` | Identifiers and hashes, no profile text |
| `config/protected-settings-baseline.bambulab.a1.proposed.json` | Yes | SHA-256 of the start and end G-code OrcaSlicer 2.4.2 writes, plus `post_process` (empty) and `printer_technology` (`FFF`); no G-code. Regenerated from OrcaSlicer v2.5.0-alpha output (originally recorded under 2.4.2) |
| `profiles.py`, `scripts/bambu-templates.py`, adapter, guard, tests | Yes | Original |
| `tests/printers/fixtures/orca_resources` | Yes | Original, invented for the tests (see its README); no OrcaSlicer or Bambu content |

Upstream references, cited and not republished: [OrcaSlicer](https://github.com/SoftFever/OrcaSlicer) tag `v2.5.0-beta`, commit `345efec64c7b95a05ce5cf96df0dde66a158835c`, `resources/profiles/BBL/` and `resources/profiles/BBL.opc`; tag `v2.5.0-alpha`, commit `3880f2c58ec392ed2bd5938ab31c9c7c496c0eb2` (retained identity); tag `v2.4.2`, commit `8500fcdccaa10b5099ac20d252af3a7c560046f1`, `resources/profiles/BBL/` (retained identity). The A1 profiles there are:

- `machine/Bambu Lab A1 0.4 nozzle.json` and its parents;
- `process/0.20mm Standard @BBL A1.json`;
- `filament/Generic PLA @BBL A1.json`, and `Generic PETG @BBL A1.json`, `Generic TPU @BBL A1.json` and `Generic PVA @BBL A1.json`, each with its parents. The P1S equivalents are the `@BBL X1C` profiles named in the table above.

An earlier revision of this pull request committed flattened copies of these files with a notice. They were removed in the same pull request, and they remain in its branch history.

## Evidence

| Kind | What exists | Not covered |
|---|---|---|
| Geometry | Offline tests: centring on 220 and 256 mm beds, fit bounds on the A1 volume, hardware-record limits | Physical bed calibration |
| Real slicer | CI Slicer test (stable) job on the exact head, the pinned OrcaSlicer v2.5.0 flatpak bundle (the one bundled build; its BBL profiles are unchanged from v2.5.0-beta). It generates the A1 profiles from that installation's packed preset cache and runs the production `model_job.slice_job` → verification → `bambu_handoff` → `print_now` path in a process configured as `bambulab.a1`. It checks that the guard passes on real output, the plate branch ran, the toolpath bounds, the baseline proposal, an oversized model and AD5X output. The Slicer test (bambu) job runs the A1 production path against the real Flathub Bambu Studio (pinned ostree commit, templates still generated from the Orca bundle): the CLI, its slicing path and both production flows, Bambu Studio as the primary slicer, and the Orca→Bambu fallback with Orca unable to run, pass on the exact head | A1 slicing with any other Orca version; Bambu Studio versions other than the pinned 2.8.2.61 |
| Packaging consumer | Offline: the `.gcode.3mf` holds exactly the validated G-code plus valid 512 and 128 px plate PNGs and is refused for wrong-model or foreign files; the Discord flow offers it and no print | Opening the package in Bambu Studio or Bambu Handy; the preview on the printer screen or in the apps; reading it from SD on an A1 |
| Hardware | None | Everything on a real A1: start sequence, envelope, plate offset, temperatures, materials, a finished print |
