# Printer support: levels, new printers and hardware checks

[Documentation home](README.md) · [Printer profiles](PRINTER-PROFILES.md) · [Contributing](../CONTRIBUTING.md)

This page answers three questions: what "supported" means for a printer, what to do if yours is not listed, and how to confirm that a listed printer really works. The [README](../README.md#is-my-printer-supported) groups printers by how they connect; the model-by-model detail is in the vendor guides linked from there.

## What "supported" means

The code has no single "support level" field. A printer model either exists or it does not, and the per-printer guides say in prose how far it has been checked. In plain terms there are three levels:

| Level | What it is | Where it lives |
|---|---|---|
| **Generic adapter** | A placeholder model for any printer that speaks the same protocol. It has one nominal bed (`klipper.generic`: 220 x 220 mm, 250 mm high, 0.4 mm nozzle) and no facts about your machine. You override the numbers. | `print_handy/printers/klipper/generic.py`, `print_handy/printers/octoprint/marlin.py` |
| **Built-in model (offline-tested)** | A model module with its own bed, height, nozzle limits and slicer templates, usually with a vendor G-code guard and sourced facts, all pinned by automated tests that use fake printers and fake transports. | `print_handy/printers/<vendor>/<model>.py`, listed in `print_handy/data/printers-registry/<vendor>.toml` |
| **Verified on hardware** | A person ran the checklist below on the real printer and reported the result. | Stated only in prose in the docs. No field in the code records it. |

Where this comes from:

- A model is a `PrinterModel` with a `vendor`, an `adapter`, a template folder, nozzle limits and default settings (`print_handy/printers/base.py`). Any module in a vendor package that defines `MODEL` is registered automatically (`print_handy/printers/__init__.py`).
- `print_handy/data/printers-registry/<vendor>.toml` (one file per vendor) is the data index of every registered model. `tests/test_knowledge.py::test_printer_registry_matches_code_profiles` fails if an entry drifts from its model.
- The word `verified` in code is narrower than it sounds. Each sourced value in a vendor's `facts.py` is a `Fact(value, source, verified)` (for example class `Fact` in `print_handy/printers/qidi/facts.py`). `verified=True` means the value was read straight from its source, such as OrcaSlicer's printable area. It does not mean a printer was involved. The temperature ceilings, for instance, are recorded as unverified manufacturer figures.
- `verified_slicers` on an adapter (`PrinterAdapter` in `base.py`) lists the slicers whose output that printer's G-code guard has been checked against. That is also about files, not hardware.
- The docs state the hardware position in prose, per family and per model, in the vendor guides (see [Qidi](QIDI.md), [Snapmaker](SNAPMAKER.md), [ZMod](ZMOD.md), [Bambu Lab](BAMBU-LAB.md)).

## How extensively is this tested?

Extensively at the code level, and the depth is deliberate. The automated suite carries more than 5,800 tests, including dedicated per-family suites with recorded fixtures and golden slicer inputs for the ZMod Flashforge line (the AD5X above all), stock Flashforge, Klipper in general and the Klipper vendor lines (Sovol, Elegoo, Creality, Qidi, Anycubic, Snapmaker), Marlin behind OctoPrint, Bambu Lab LAN, PrusaLink and Elegoo SDCP. A shared adapter contract runs against every registered model, and registry parity tests fail if a model's documented facts drift from its code profile, so coverage grows with each printer added instead of being re-earned per model.

Live evidence is narrower and is recorded where it exists: the AD5X on ZMod is the author's own daily printer, with a written tested baseline ([AD5X best practices](DEVICE-AD5X.md#tested-baseline)), and a Bambu Lab A1 run on 26 Oct 2026 was sliced, uploaded over FTPS, started, and reported printing by the printer ([A1 walkthrough](BAMBU-A1-DOCKER.md), section 8). A hardware report is what moves a printer from the second level above to the third, and the printer's guide is then updated by hand.

Extensive testing is still not a hardware test of your printer. Keep the confirmation and artifact guards on while you check anything.

## Your printer is not listed

Pick the route that matches what the printer runs.

**Klipper with Moonraker.** Select `klipper.generic`. In your settings file (`config/local.yaml`, or the matching `PRINT_HANDY__...` environment variables from [SETTINGS.md](SETTINGS.md)) set at least:

- `services.moonraker_url`, for example `http://192.168.1.60:7125`.
- `printer.bed_mm`, `printer.max_print_height_mm`, `printer.nozzle_diameter_mm` and `printer.nozzle_type` to your real machine. The generic defaults match no particular printer and will mis-slice a different bed.
- `printer.start_macro` and `printer.end_macro` if your start and end macros are not `PRINT_START` and `PRINT_END`.

The generic model refuses multi-color and multi-plate sources. It slices with the generic Klipper templates (`templates/orca/klipper_generic`), so check the slicer profiles as well as the numbers. [Configuration](CONFIGURATION-YAML.md) covers the settings file.

**Klipper accessory: Bondtech INDX.** A Klipper printer over Moonraker (the generic model or a Klipper vendor line, such as a Sovol SV08) fitted with a Bondtech INDX tool changer is supported as an accessory, not as a separate model: record it in the top-level accessories list and the printer gains a shared-tool shape (passive tools, one shared extruder and heater, per-color tool questions, and the INDX `CHANGE_TOOL` slicing form). Bondtech officially recognizes the Sovol SV08 and SV08 Max and the Voron Trident R2 integrations, plus custom CoreXY machines as a class; a Voron 2.4 integration exists but is unverified, and no RatRig V-Core integration is published. The Prusa first-party INDX build for the CORE One line is not this accessory. See [Accessories](ACCESSORIES.md) for detection, the record format and the named INDX refusal states. Support is established by offline tests; no live INDX printer has been run yet.

**Marlin behind OctoPrint.** Select `octoprint.marlin`, set `services.octoprint_url`, supply the OctoPrint API key as a secret, and override the placeholder dimensions the same way. Four Enders already have presets with real dimensions. See [OctoPrint + Marlin](OCTOPRINT-MARLIN.md).

**Anything else** (a vendor LAN protocol, a cloud-only printer) needs a new adapter, which is a larger contribution. Open a request first so the approach is agreed before code is written.

Before the first print on any generic setup, run `python3 -m print_handy.doctor` (it contacts the services you configured), slice a small test part with the bed clear, and read the start G-code in the output. If it homes, heats and levels the way your own slicer's start code does, you are in good shape. If anything looks wrong, stop and file a request.

Then open a request with the [printer form](https://github.com/catduckgnaf/print-handy/issues/new?template=printer_report.yml) and choose "Request support for a printer that is not listed". Include the model, firmware, connection type, the bed and height you configured, and what worked or failed.

## Adding a built-in printer

The smallest change is one new model in an existing vendor package, such as another Sovol or Qidi model. The worked example here is the Qidi package, `print_handy/printers/qidi/`. [Printer profiles](PRINTER-PROFILES.md#adding-a-printer) has the minimal Sovol version.

What a vendor package like Qidi contains:

| File | What it does |
|---|---|
| `__init__.py` | `VENDOR = VendorProfile(...)`: connection (`moonraker`), start-code family, allowed slicers, file formats, and shared setting defaults such as the start and end macro names. |
| `q1pro.py` (one per model) | `MODEL = PrinterModel(key="qidi.q1pro", ...)`: label, adapter class, template folder name, supported nozzle sizes and types, and the `printer` defaults (name, `bed_mm`, `max_print_height_mm`, nozzle, `filament_slots`). The key must be `<vendor package>.<module name>`. |
| `adapter.py` | The vendor adapter. For Qidi it subclasses the Klipper adapter and uses the template's own start sequence. |
| `facts.py` | Sourced facts per model: bed, height, exclusion zones, nozzle sizes, temperature ceilings and the checked slicer profile identities. Each is a `Fact` that records its source. |
| `guard.py` | Offline checks on sliced G-code: another ecosystem's start code, a file sliced for a different model, temperatures over the model's limits, nozzle sizes with no template. |
| `profiles.py` | Generates and re-verifies the slicer templates from the operator's own OrcaSlicer, and refuses anything that does not hash to a recorded identity. |

The rest of the checklist for a new model:

1. **Registry entry.** Add a `[[printer]]` block to `print_handy/data/printers-registry/<vendor>.toml` for the model's vendor (schema in `print_handy/data/printers-registry/README.md`) in the same change. Key, label, firmware, connection, bed, height and slot count must match the code.
2. **Slicer templates.** Either ship your own generic templates in `print_handy/templates/orca/<folder>/` (`machine.json`, `process.json`, `filament.json`), or, for vendors whose slicer profiles cannot be redistributed, follow the Qidi pattern: a `scripts/<vendor>-templates.py` generator that reads the user's OrcaSlicer install and checks it against a recorded identity (see `scripts/qidi-templates.py`; Bambu Lab, Creality, Elegoo and Prusa have siblings). Do not copy slicer vendor profile text into the repository.
3. **Source every number.** Take bed size and height from the slicer profile's printable area and height, not from marketing pages, and record the source in `facts.py`. Mark a value unverified if it came from a spec sheet.
4. **Tests.** Use the vendor's existing test file as the template (`tests/printers/test_qidi.py` with `tests/printers/qidi_fixture.py`). They run offline against invented slicer resources. Golden Orca input files in `tests/printers/golden/` pin the slicer inputs, and `tests/printers/test_vendor_profiles.py` pins model defaults.
5. **Docs.** Add the model to the vendor's guide (for example [QIDI.md](QIDI.md)), to the README's printer groups when it changes one, and to `docs/README.md` if you add a page. Say what has and has not been tested. Do not claim hardware success without a report.

Run the offline suite as described in [CONTRIBUTING.md](../CONTRIBUTING.md) (`python -m pytest -q tests`), and at minimum `tests/printers`, `tests/test_knowledge.py` and `tests/test_docs_index_contract.py`. Say in the pull request which checks you ran and which you did not.

Documentation-only contributions are welcome too: corrections to a printer guide, a better bed or height source, or a worked setup for a generic printer. Use the documentation issue form for those.

## Confirm that a listed printer works

Do this only on a printer you own and are watching. Start with a small, flat test part, clear the bed, and keep a hand near the printer's stop button. Print Handy's own gates stay on throughout. Each item below is something you can observe. Mark each one passed, failed, or not possible on your printer.

**Setup**

1. **Model and numbers.** `printer.model` is the key for your printer. Bed size, height and nozzle in the settings match the machine. Compare against your slicer's own profile. Pass if the bed size and height agree within a millimeter and the configured nominal nozzle diameter matches the installed nozzle exactly; a different nominal diameter is a different nozzle for slicing, not a rounding difference.
2. **Doctor.** `python3 -m print_handy.doctor` (or `/print-handy debugging` in chat) reports the printer reachable and no blocking errors. The doctor contacts your configured services.
3. **Slicer output.** Slice the test part and open the G-code. The first lines are your printer's real start sequence (for the Klipper vendors, a call to the printer's own start macro; for Bambu Lab, the vendor start block). Temperatures match the filament. Every move is inside the bed. The guard did not refuse the file. Pass if the output matches what the vendor's own slicer profile would produce.

**Printing**

4. **Upload.** The file arrives on the printer or its host with the right name and size, and is not started. Pass if it is in the printer's file list and nothing is printing.
5. **Gated start.** Start requires the bed-clear confirmation in chat, and one confirmation starts one print. Pass if no print starts until you confirm, and the print that starts is the file you uploaded.
6. **Real start behavior.** The printer homes, heats, runs its leveling step as the start sequence says, and begins the first layer in the right place. Pass if it matches what the same file does from the printer's own interface.
7. **Status and progress.** The bot posts the start, and status or progress reflects the printer (state, percent, temperatures, and layer or time where the printer reports them). Pass if the numbers agree with the printer's screen.
8. **Pause, resume, cancel.** Pause from chat holds the print, resume continues it, and cancel stops it and leaves the printer safe (heaters off, or as the printer's end routine leaves them). These controls are documented for Moonraker printers (`/print-stop`, `/print-resume`, and the Resume and Cancel buttons on pause alerts; see the [detailed project reference](PROJECT-REFERENCE.md)). For a family where chat does not offer them, mark the item not possible and say which controls it did show.
9. **Filament and runout, where it applies.** On a multi-slot printer (AMS, IFS, toolheads) the slot or tray mapping matches what you loaded, and a job whose mapping cannot be resolved is refused. On a printer with a runout sensor, pulling the filament pauses the print, the bot announces a runout rather than a generic pause, and the spool question offers Cancel print. For single-extruder printers without a sensor, write "not applicable".
10. **Camera and timelapse, where it applies.** If the printer has a camera source Print Handy supports, a still appears in the status or completion post, and a timelapse is attached when timelapse capture is enabled. Some families state that no camera source is verified (the Centauri Carbon, and the AD5X on stock firmware). For those, mark not possible and note what the printer or firmware advertises.
11. **Completion.** When the print ends, one completion post appears with the right outcome (success, failed or cancelled) and the job is recorded. A cancelled print must not post as a success.
12. **Restart behavior (optional).** Restart the bot mid-print. For Bambu Lab, OctoPrint, Prusa, Elegoo SDCP and stock Flashforge, the launched-print monitor runs inside the process that started the print and is not restarted with it (see the vendor guides). Note what you saw.

Items 8 and 9 interrupt a print, so only do them on a test part. Do not mark an item passed unless you watched it happen.

## What to submit

Use the [printer form](https://github.com/catduckgnaf/print-handy/issues/new?template=printer_report.yml). It asks for:

- The printer model and its Print Handy key (`printer.model`).
- The firmware or mod and its version.
- The connection type (Moonraker, OctoPrint, a vendor LAN protocol, offline).
- The Print Handy version and how it is installed. The diagnostics bundle records the version too.
- The checklist above, with each item marked passed, failed or not possible, and a sentence on anything that surprised you.
- The diagnostics bundle. Run `/print-handy bundle` in chat, or `python3 -m print_handy.diagnostics_bundle` on the host, and attach the generated `diagnostics-bundle.md`. It holds versions, a redacted settings summary, the printer roster, doctor output and a bounded log tail. Secret values are left out and the sanitizer strips internal addresses, but read the file before attaching it and remove anything you would not want public. It is written to a local file and goes nowhere unless you attach it. See [Diagnostics](DIAGNOSTICS.md) and [Guided setup and debugging](GUIDED-SETUP-DEBUGGING.md).

Never put access codes, API keys, tokens or private model files in a report. A report does not authorize anyone to operate your printer.

If anything moved unexpectedly, heated when it should not have, or ignored a confirmation, say so in the first line.
