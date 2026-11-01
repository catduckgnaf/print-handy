# Printer profiles and post-slice delivery

Printer support is organised **vendor first, then printer**. A vendor profile holds what all of a vendor's printers share. Each printer profile holds only what is different about that printer, and inherits everything else from its vendor. The options offered after a slice (print, send the files to Discord, package for Bambu) come from these profiles. A Bambu printer offers the Bambu handoff; a Klipper printer never does.

## Layout

```
print_handy/printers/
  base.py                VendorProfile, PrinterModel, PrinterAdapter, start_code_problems
  flashforge/
    __init__.py          VENDOR  Flashforge: Moonraker, ZMod START_PRINT start code, OrcaSlicer, .gcode
    ad5x.py              MODEL   flashforge.ad5x: 220 mm bed, 4-slot IFS, 0.25 to 0.8 mm nozzles
    ad5m.py              MODEL   flashforge.ad5m: 220 mm bed, single extruder (no IFS), 0.25 to 0.8 mm nozzles
    ad5m_pro.py          MODEL   flashforge.ad5m_pro: the AD5M geometry; enclosure, camera and filtration
    creator5.py          MODEL   flashforge.creator5: 256 mm bed, 4 independent toolheads (tools, not IFS slots)
    creator5_pro.py      MODEL   flashforge.creator5_pro: the Creator 5 toolheads; enclosure, chamber heat and filtration
  flashforge_stock/
    __init__.py          VENDOR  Flashforge stock: LAN HTTP on port 8898, slicer-owned start code, OrcaSlicer, .gcode
    ad5x.py              MODEL   flashforge_stock.ad5x: the AD5X bed, one tool, four IFS bays, no Moonraker.
                                 IFS occupancy is read from /detail and is not verified on a live printer.
                                 (adapter.py, client.py, guard.py)
  klipper/
    __init__.py          VENDOR  Klipper: Moonraker, PRINT_START/PRINT_END macros, OrcaSlicer, .gcode
    generic.py           MODEL   klipper.generic
  sovol/
    __init__.py          VENDOR  Sovol: the Klipper profile under the Sovol name
    sv06.py              MODEL   sovol.sv06
    sv01.py, sv08.py, …  MODEL   the rest of the Klipper line; see SOVOL.md
  sovol_marlin/
    __init__.py          VENDOR  Sovol on stock Marlin, through OctoPrint
    sv06.py              MODEL   sovol_marlin.sv06
  octoprint/
    __init__.py          VENDOR  OctoPrint: REST to Marlin, slicer-owned start code, OrcaSlicer, .gcode
    marlin.py            MODEL   octoprint.marlin
    ender3.py            MODEL   octoprint.ender3: the Ender-3 preset (220 mm bed, 250 mm height)
    ender3pro.py         MODEL   octoprint.ender3pro: the Ender-3 Pro preset
    ender3v2.py          MODEL   octoprint.ender3v2: the Ender-3 V2 preset (0.4 mm nozzle only)
    ender3v3se.py        MODEL   octoprint.ender3v3se: the Ender-3 V3 SE preset (Marlin firmware)
  bambulab/
    __init__.py          VENDOR  Bambu Lab: opt-in read-only LAN MQTT status, Bambu start code, Orca or Bambu Studio,
                                 .gcode.3mf handoff
    a1.py                MODEL   bambulab.a1: 256 mm bed, 0.4 mm nozzle, single filament, slicing offline
                                 (adapter.py, guard.py, facts.py, profiles.py; see BAMBU-LAB.md)
    p1s.py               MODEL   bambulab.p1s: 256 mm bed, 250 mm Orca height, 0.4 mm nozzle, single filament, slicing offline
    a1mini.py, p1p.py, x1.py, x1c.py, x1e.py, p2s.py, h2s.py, a2l.py
                         MODEL   #127 Stage 1: the remaining single-nozzle models, same shape as the P1S
                                 (a2l.py joined in #356, from OrcaSlicer v2.5.0-alpha's profiles)
    h2d.py, h2dpro.py, x2d.py, h2c.py
                         MODEL   The dual-nozzle models (#127 Stage 2, #308; h2c.py joined in #356)
  prusa/
    __init__.py          VENDOR  Prusa: PrusaLink LAN, slicer-owned start code, OrcaSlicer, .gcode/.bgcode
    mk4s.py              MODEL   prusa.mk4s: 250 x 210 mm bed, 220 mm height, single tool
    coreone.py           MODEL   prusa.coreone: 250 x 220 mm bed, 270 mm height, enclosed
    xl.py                MODEL   prusa.xl: 360 mm cube, 5 toolheads (multi-tool model)
    miniplus.py          MODEL   prusa.miniplus: 180 mm cube, Orca MINI profiles
                                 (adapter.py, client.py, guard.py, facts.py, profiles.py; see PRUSA.md)
  elegoo/
    __init__.py          VENDOR  Elegoo: Moonraker, template-inline start code, OrcaSlicer, .gcode
    neptune4.py          MODEL   elegoo.neptune4: 230 x 230 mm bed, 265 mm height
    neptune4pro.py       MODEL   elegoo.neptune4pro: 230 x 230 mm bed, 265 mm height
    neptune4plus.py      MODEL   elegoo.neptune4plus: 325 x 325 mm bed, 385 mm height
    neptune4max.py       MODEL   elegoo.neptune4max: 426 x 426 mm bed, 480 mm height
                                 (adapter.py, guard.py, facts.py, profiles.py; see ELEGOO.md)
  elegoo_sdcp/
    __init__.py          VENDOR  Elegoo (SDCP): SDCP LAN, slicer-owned start code, OrcaSlicer, .gcode/.bgcode
    centauricarbon.py    MODEL   elegoo_sdcp.centauricarbon: 256 mm cube, enclosed CoreXY
                                 (adapter.py, client.py, guard.py, facts.py, profiles.py; see ELEGOO.md)
  qidi/
    __init__.py          VENDOR  Qidi: Moonraker, template start code (PRINT_START-led), OrcaSlicer, .gcode
    q1pro.py             MODEL   qidi.q1pro: 245 x 245 mm bed, 240 mm height, enclosed CoreXY
    xplus3.py            MODEL   qidi.xplus3: 280 x 280 mm bed, 270 mm height, enclosed CoreXY
    xplus4.py            MODEL   qidi.xplus4: 305 x 305 mm bed, 280 mm height, enclosed CoreXY
    xmax3.py             MODEL   qidi.xmax3: 325 x 325 mm bed, 315 mm height, enclosed CoreXY
    xsmart3.py           MODEL   qidi.xsmart3: 175 x 180 mm bed, 170 mm height, enclosed CoreXY
                                 (adapter.py, guard.py, facts.py, profiles.py; see QIDI.md)
  creality/
    __init__.py          VENDOR  Creality: Moonraker, template start code (START_PRINT-led or inline), OrcaSlicer, .gcode
    ender3v3.py          MODEL   creality.ender3v3: 220 x 220 mm bed, 250 mm height
    ender3v3ke.py        MODEL   creality.ender3v3ke: 220 x 220 mm bed, 245 mm height
    ender3v3plus.py      MODEL   creality.ender3v3plus: 300 x 300 mm bed, 330 mm height
    k1.py                MODEL   creality.k1: 220 x 220 mm bed, 250 mm height, enclosed CoreXY
    k1c.py               MODEL   creality.k1c: 220 x 220 mm bed, 250 mm height, enclosed CoreXY
    k1max.py             MODEL   creality.k1max: 300 x 300 mm bed, 300 mm height, enclosed CoreXY
                                 (adapter.py, guard.py, facts.py, profiles.py; see CREALITY.md)
  anycubic/
    __init__.py          VENDOR  Anycubic: Moonraker (community-firmware installs), template start code (fully inline), OrcaSlicer, .gcode
    kobra2.py            MODEL   anycubic.kobra2: 220 x 220 mm bed, 250 mm height
    kobra2neo.py         MODEL   anycubic.kobra2neo: 220 x 220 mm bed, 250 mm height
    kobra2pro.py         MODEL   anycubic.kobra2pro: 220 x 220 mm bed, 250 mm height
    kobra2plus.py        MODEL   anycubic.kobra2plus: 320 x 320 mm bed, 400 mm height
    kobra2max.py         MODEL   anycubic.kobra2max: 420 x 420 mm bed, 500 mm height
                                 (adapter.py, guard.py, facts.py, profiles.py; see ANYCUBIC.md)
  snapmaker/
    __init__.py          VENDOR  Snapmaker: Moonraker, Klipper-macro start, OrcaSlicer, .gcode
    u1.py                MODEL   snapmaker.u1: 270 mm cube, 4 toolheads (tool changer, not IFS)
                                 (adapter.py, guard.py, facts.py; generic templates; see SNAPMAKER.md)
templates/orca/<folder>/ machine.json, process.json, filament.json per printer; a model with
                         generated_templates (the A1) ships none: <state>/orca-profiles/<folder>
```

The `[printer] model` key is `<vendor>.<printer>`, which is the package name and the module name. Existing keys did not change.

### What lives where

| Vendor profile (`VENDOR = VendorProfile(...)`) | Printer profile (`MODEL = PrinterModel(...)`) |
|---|---|
| `connection`: `moonraker`, `octoprint`, `offline`, `bambulab_lan` or `flashforge_stock` | `bed_mm`, `max_print_height_mm` (printable area) |
| `start_code`: `zmod`, `klipper_macro`, `marlin`, `bambu` or `flashforge_stock` | printable-area exclusions and motion envelope (in its Orca `machine.json` and adapter) |
| `slicers`: allowed `[printer] slicer` values, first is default | `supported_nozzle_sizes`, `supported_nozzle_types`, `nozzle_diameter_mm` |
| `fallback_slicer`: one isolated attempt after an Orca failure | `color_system` (see Color architectures), `filament_slots` / AMS / AMS Lite, multi-color support |
| `file_formats`: what the printer takes, primary first | `adapter` class, Orca `templates` folder, protected-settings `baseline` |
| `delivery` and `default_delivery`: post-slice options | `label`, `printer.name` |
| `settings`: shared `firmware`, macro names, layout margins, prime tower, cold pull | any vendor setting it needs to override |

Settings merge in this order, later wins key by key:

1. vendor `settings`
2. printer `settings`
3. `config/home.yaml`
4. `config/local.yaml` (or `PRINT_HANDY_CONFIG`)
5. environment variables

The loaded settings also carry `printer.vendor`, `printer.slicer` and `delivery.methods`.

`tests/printers/golden/model_defaults.json` and `model_settings.json` were captured before the vendor layer existed. `tests/printers/test_vendor_profiles.py` checks that every existing printer still produces exactly those settings. The only differences are the three keys above.

## Color architectures

Every model names its color architecture in `MODEL.color_system` (and in the printer
registry), one of three classes (issue #656):

| Class | Meaning | Models |
|---|---|---|
| `single` | One extruder, one filament position. | Every model not listed below. |
| `multi_slot` | One extruder fed by several filament slots: `[printer] filament_slots` is greater than 1. For the AD5X pair the feeder is an IFS whose slots are bound to spools in the hardware record, and slot mapping, spool-slot validation and native slot sync hang off this class. For the Bambu AMS models the feeder is the AMS: the class and the slot count are the same vocabulary, but the binding is the printer's own tray report (matched to a job's filament slots at print start), so the record-bound IFS slicing machinery does not engage for them. | `flashforge.ad5x` (ZMod), `flashforge_stock.ad5x`; the single-nozzle Bambu AMS models (`bambulab.a1`, `a1mini`, `a2l`, `p1p`, `p1s`, `p2s`, `x1`, `x1c`, `x1e`, `h2s`) |
| `multi_tool` | Several independent toolheads, each with its own extruder and filament: `[printer] tool_count` is greater than 1 and `filament_slots` stays 1, so no IFS behavior switches on; slicer tool *i* always prints with toolhead *i*. | `flashforge.creator5`, `flashforge.creator5_pro`, `prusa.xl` |

The class is the authority: the adapter properties `multi_slot` / `multi_tool` and the
settings validation for `filament_slots` / `tool_count` read it, instead of re-inferring the
architecture from counts or adapter flags. The classes are mutually exclusive by
construction, and a model whose settings contradict its class (`filament_slots` above 1
outside `multi_slot`, `tool_count` above 1 outside `multi_tool`) is refused when the
profile loads. One recorded posture: the dual-nozzle Bambu models (H2D, H2D Pro,
X2D, H2C) are `single` deliberately (#671): two independent extruders, each fed by
its own AMS, are neither the one-extruder `multi_slot` shape nor the `multi_tool`
tool-changer contract, so neither class is claimed for them; the slicer assigns
their filaments to extruders in the file itself, and print starts resolve AMS trays
per filament slot. The Prusa XL's Stage 1 posture ended with #670: it is
`multi_tool`, driven through the same adapter contract as the Creator pair.

## Config

Existing configs keep working unchanged:

```yaml
printer:
  model: flashforge.ad5x       # the full vendor.printer key, as before
```

These forms are also accepted, and are stored as the full key:

```yaml
printer:
  vendor: sovol                # "bambu" is accepted for "bambulab"
  model: sv06                  # the printer part; on its own it works when only one vendor has that name
                               # (a bare sv06 still means sovol.sv06, as before sovol_marlin.sv06 existed)
  slicer: orca                 # orca everywhere; bambu_studio only where the vendor allows it (Bambu Lab)
delivery:
  methods: [printer, discord]  # a subset of the vendor's delivery options; default is the vendor default
```

Combinations the vendor does not offer stop startup and name the setting. Examples are a vendor that disagrees with a full model key, `slicer: bambu_studio` on a Klipper printer, and `bambu_handoff` on a Flashforge printer. Every existing vendor defaults to `methods: [printer]`, so its Discord flow is exactly what it was. Add `discord` to get a "Send the files here" button after slicing.

## Adding a printer

A new Sovol printer is one module plus its Orca templates. Everything not listed comes from the Sovol vendor profile:

```python
# print_handy/printers/sovol/sv08.py
"""Sovol SV08 on stock Klipper. Templates: templates/orca/sovol_sv08."""
from ..base import PrinterModel
from ..klipper.adapter import KlipperAdapter
from . import VENDOR

MODEL = PrinterModel(
    key="sovol.sv08",
    label="Sovol SV08 (Klipper)",
    adapter=KlipperAdapter,
    templates="sovol_sv08",
    vendor=VENDOR,
    supported_nozzle_sizes=(0.4,),
    settings={"printer": {"name": "Sovol SV08", "bed_mm": [350.0, 350.0], "max_print_height_mm": 345.0,
                          "nozzle_diameter_mm": 0.4, "filament_slots": 1}},
)
```

Then add `print_handy/templates/orca/sovol_sv08/{machine,process,filament}.json` and select it with `printer: {model: sovol.sv08}`. Discovery registers the new module automatically. A module that leaves out `vendor=` inherits its package's `VENDOR`. A module that names another vendor is refused.

A new vendor is a package with an `__init__.py` defining `VENDOR = VendorProfile(key="<package name>", ...)`. `VendorProfile` checks its own consistency. An offline vendor cannot offer `printer`, and only Bambu start code can offer `bambu_handoff`.

## After a slice

The Discord flow is unchanged up to the slice: preview, colors, copies, then "Slice and print now (bed is clear)", "Slice only", "Stop with colors saved" and "Cancel print request". The slice summary still shows per-plate artifacts, estimates and validation. A vendor without `printer` delivery (Bambu Lab) does not get the "Slice and print now" button.

After **Slice only**:

- **Only `printer` enabled** (the default for every existing printer): the same question as before, with the Start button and Cancel.
- **More options enabled**: one menu with the options the profile enables:
  - **Start next plate · bed is clear**: the same per-job bed-clear button, and still the only way to print. It runs every existing print-now gate.
  - **Send the files here** (`discord`): attaches each plate's file to the thread.
  - **Package for Bambu (no upload)** (`bambu_handoff`): builds a `.gcode.3mf` per plate, with the plate preview PNGs the printer and the Bambu apps show (#275).
  - **Done, don't print**

  After a delivery the menu returns without that option ("Anything else for this slice?"). Each method runs to completion at most once per slice.

Slicer choice, file delivery, printer handoff and print authorization are separate decisions. A delivery never creates, uses up or implies a bed-clear approval.

**Binding.** When the slice returns, the receipt records each G-code's SHA256. A delivery sends only files listed in this job's `JOB.json` whose bytes still match. A changed, missing or unknown file gets "Not delivered: ..." and nothing is sent.

**Discord size limits.** `[discord] upload_max_bytes` (default 9.5 MB, under Discord's 10 MB limit for servers without boosts) is the cap per file:

- A file under the cap is attached as is.
- A larger `.gcode` is zipped (G-code usually shrinks 4 to 6 times) and the zip is sent if it fits.
- If it still does not fit, or it is already compressed (`.3mf`), it is not sent. The reply gives its size and its path on the Print Handy host.

Every file is one message with one `MEDIA:` attachment, labelled "Plate n of m". A summary line follows: "Sent n of m file(s) here. Too big for Discord: ... Failed: ...".

**Said before the button, not after (#272).** The post-slice menu sizes each file before it offers "Send the files here". A plain `.gcode` over the cap is deflated once, in memory, to learn what its zip would weigh; a `.gcode.3mf` package counts as is. A file that cannot fit in any form is named in the same message with its size, the cap and its path on the Print Handy host (for a Bambu package, `<archive>/<job>/sliced/.../bambu/plate_1.gcode.3mf`), and when nothing fits the button is left out, so the operator never picks a download that fails. Files under the cap are attached exactly as before. The per-file check at send time stays as a safety net for a borderline file. The cap is [`discord.upload_max_bytes`](SETTINGS.md) (`PRINT_HANDY__DISCORD__UPLOAD_MAX_BYTES`); raise it only when your server's boost level allows larger uploads. Large files stay on the host; Print Handy has no download server, so fetch them from the archive path it names.

**Failures and retries.** A failed upload (the adapter reports `success=False`, or the send raises) is reported with the error, and the option stays in the menu. A retry sends only the files not yet delivered. Too-big files cannot be fixed by a retry, so they count as reported and the option closes. Every attempt is recorded under `deliveries` in the intake receipt. Expiry and cancellation behave as before.

## Source-profile compatibility warnings (#722)

A model's project settings can name the printer they were authored for (`printer_model_id` / `printer_settings_id` in `project_settings.config`). When that printer is demonstrably not the target printer (an exact name match after ignoring case, separators and a nozzle suffix, against the target label and the `printer_model` / `printer_settings_id` its own machine template declares; a variant such as "Kobra 2 Pro" is never treated as "Kobra 2"), Print Handy compares the authored motion and fan values against the target's verified facts — the axis speed and acceleration limits in its own machine template, and the 0–100% fan scale, checked for every filament in a multi-filament project — and shows a warning on the slice confirmation for each value outside them, naming the value, the source profile it came from, and the target fact it conflicts with. Values with no verified target range (wall count, brim width) are never warned about, and a source that names no printer produces no warnings.

Warnings never change a setting. The existing precedence is unchanged and unaffected by warnings: template/vendor profile first, then the filament-family overlay, material-variant adjustments, and operator/spool values last (they win). Any future automatic translation of foreign values would be a separate issue with its own test matrix.

## Bambu Lab

| Capability | Status |
|---|---|
| Vendor profile, settings, delivery options | Implemented. Printer profiles: A1, A1 mini, P1P, P1S, P2S, X1, X1 Carbon, X1E, H2S (every single-nozzle model); LAN status/upload/start are opt-in (see BAMBU-LAB.md) |
| Start-code guard | Implemented. After slicing, and again before packaging, every G-code line for a Bambu printer must be a plain G, M or T code. Klipper or ZMod macros (`PRINT_START`, `START_PRINT ... MESH=`, `CHANGE_ZCOLOR`, `LOAD_ZOFFSET`, `BED_MESH_*`) block the slice or the package |
| `.gcode.3mf` packaging | Implemented, offline and deterministic. Contents: `Metadata/plate_<n>.gcode`, its upper-case MD5 in `Metadata/plate_<n>.gcode.md5`, `Metadata/slice_info.config` with the plate index and nozzle, and an empty `3D/3dmodel.model`, plus the plate previews `Metadata/plate_<n>.png` (512 px) and `Metadata/plate_<n>_small.png` (128 px) from Orca's embedded thumbnail or an offline render of the sliced geometry (#275; `top_<n>.png` and `pick_<n>.png` are not written), laid out the way Bambu Studio exports a sliced plate. **Not yet verified on Bambu hardware or in Bambu Studio.** `printer_model_id` is not written |
| Discord delivery | Implemented. Bambu Lab's primary format is `.gcode.3mf`, so "Send the files here" sends the packages |
| Bambu Studio as the primary slicer | Implemented, opt-in with `printer.slicer: bambu_studio`, allowed only by the Bambu Lab profile. Runs the configured `[bambu] command` (Flathub Bambu Studio with `--unshare=network`) with the same CLI flags as the existing fallback: `--datadir`, `--load-settings`, `--load-filaments`, `--arrange`/`--orient`, `--slice 0`, `--outputdir`. Orca is not run and there is no fallback. Every Print Handy guard still applies to its output. The real-slicer CI job covers OrcaSlicer only, so this path is tested with a mocked CLI |
| LAN upload | Implemented opt-in (#187 stage c): FTPS upload with size readback, used by the gated LAN print path. Cloud upload is unsupported. Read-only LAN MQTT *status* is separate — see the telemetry row in [BAMBU-LAB](BAMBU-LAB.md) |
| Starting a print | Opt-in (#187 stage d): the Start button appears only when the operator adds `printer` to `[delivery] methods`, with LAN configured and the printer in Developer Mode; the start then runs every gate (readiness twice, bed-clear approval, verified upload, telemetry confirmation). Otherwise no Start button is offered — open the package in Bambu Studio or Bambu Handy, or copy it to the SD card |
| Pausing or stopping a print | Commands exist on the LAN client; the external Discord print-control module does not call them yet |
| Telemetry | Read-only and opt-in: with `[bambu] lan_host`/`lan_serial` configured, the health check reads one LAN MQTT status snapshot. No listener. **Not tested on a printer** |
| AMS / AMS Lite multi-color | Unsupported, per the printer profile (issue #127) |

Bambu Studio is not treated as a submission path. Picking it as the slicer changes only which program writes the G-code. Live upload or print control would need its own evaluation of firmware, LAN versus cloud authentication and credential handling before any of it is built.
