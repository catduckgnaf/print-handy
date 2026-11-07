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

## Thermal capabilities

Every registered model carries a structured thermal record in its
`[printer.thermal]` table in `print_handy/data/printers-registry/<vendor>.toml` (issue #720),
loaded as `PrinterEntry.thermal` by `print_handy/knowledge/registry.py` and
surfaced by `print_handy/knowledge/thermal.py`:

- **Enclosure:** `enclosed`, `open`, or `unknown`, plus whether door or lid
  state is knowable at all (`yes` / `no` / `unknown`).
- **Chamber heater:** whether one is present (`present` / `absent` /
  `unknown`) and whether active control is `supported`, `not_supported`, or
  `unknown`. Control is recorded as `supported` only when
  `chamber_heater_evidence` names a verified firmware command or macro.
  For example, the Creator 5 Pro's control was recorded `unknown` in #720
  because the Creator models' stock firmware support is undocumented; the
  #740 evidence pass found the ZMod config for this record carries a
  `[heater_generic chamber_heater]` with PID control, so control is now
  `supported` for the ZMod record — still on named evidence, never
  inferred from "enclosed". The AD5M Pro is enclosed and passive (no chamber heater).
  The AD5X has no chamber heater, but its enclosure is a separate kit, so
  its enclosure is recorded `unknown` and guided setup asks about it. An open-frame model such as the AD5M or the Ender-3
  presets has no chamber heater.
- **Chamber sensor:** whether one is present, and whether a connected
  printer can report a fresh reading at slice time. The #740 evidence
  pass filled these from named sources (firmware configs, vendor API
  surfaces, upstream OrcaSlicer system profiles, vendor documentation);
  both stay `unknown` for any model no source settles.
- **Fan mapping:** part-cooling, auxiliary, exhaust, and filtration fans,
  each `present` / `absent` / `unknown` with a mapping that is either a
  verified index or macro name or `unknown`. Orca's documented convention
  (M106 P0 part, P2 auxiliary, P3 exhaust) is not assumed to be a model's
  actual wiring — a mapping is recorded only when a source names the
  index, macro, or fan object for that model (issue #740). Print Handy's
  own Orca templates are never evidence: they carry boilerplate accessory
  values even on open-frame models.
- **Provenance:** each fact group (enclosure, chamber, fans) names its
  provenance: `vendor documentation`, `verified profile`,
  `operator-confirmed`, or `unknown`. A fact is only recorded as known
  when a named source supports it; since #740 those sources are the
  per-family evidence tables on that issue (firmware/vendor configs and
  API surfaces first, upstream OrcaSlicer system profiles second, vendor
  manuals and specs for presence claims only). When in doubt, the value
  is `unknown`, and a test pins that any known fan, sensor, or door fact
  carries a non-`unknown` provenance.

Two surfaces use the record, and neither changes any behavior:

- **Guided setup** (`print_handy/setup_wizard.py`) lists, for the chosen
  model, questions for only the thermal facts that are unknown for that
  model — the questions come from `thermal_setup_questions`. Known facts
  are not asked about again.
- **Slice summary** (`format_slice_summary`) appends the thermal
  assumption lines from `thermal_assumptions_lines` when a job names a
  registered model: enclosure, chamber capability, and fan facts, with
  unknown shown as unknown, so the operator sees what was assumed before
  confirming.

### Per-printer facts from the connected printer (#741)

Some facts differ per install: an optional fan kit, a chamber reading the
connected surface may or may not carry, a fan mapping the vendor never
published. `/print-handy add-printer` settles those from the printer itself.
After the live connection test passes, it runs a read-only thermal probe
(`print_handy/core/thermal_probe.py`):

- **Moonraker / Klipper:** lists the printer's objects and queries the
  temperature, fan, heater and button objects. A chamber-named
  `temperature_sensor`, `temperature_fan` or `heater_generic` (or an
  exhaust-named `temperature_fan`, the Sovol Zero shape) proposes a chamber
  sensor with a fresh reading. `fan_generic` / `temperature_fan` objects
  named for a role (exhaust, filter, auxiliary, part) propose that fan as
  present, mapped to the object name. A door- or lid-named `gcode_button`
  or `filament_switch_sensor` proposes door state knowable. The object list
  is complete, so a role with no object is proposed `absent` / `no`, except
  that no fan role is proposed absent while a fan object with an unclear
  name exists (the review lists those).
- **Bambu Lab LAN:** one full status report. `print.chamber_temper` or
  `device.ctc.info.temp` settles the chamber reading; `cooling_fan_speed`,
  `big_fan1_speed` and `big_fan2_speed` settle part, auxiliary and exhaust
  fans (`M106 P1` / `P2` / `P3`). Door state is not probed.
- **Elegoo SDCP:** one status frame. `TempOfBox` and the `ModelFan`,
  `AuxiliaryFan` and `BoxFan` channels propose sensor and fan presence.
- **PrusaLink:** a schema check of `/api/v1/status` on the printer's
  firmware. Today it has no chamber or door field, which confirms `no`; a
  firmware that adds one is picked up by name.
- **OctoPrint and other connections:** cannot be probed; the thermal
  questions stay the route.

The probe sends no fan, heater or G-code command and skips a printer that is
printing. A failed probe proposes nothing. Only findings that differ from the
model record are shown, each with its evidence and, where the model record
already has a value, that value too. The operator replies `all`, `none`, or
the numbers to keep. Confirmed facts are stored in that printer's hardware
record (`thermal_install`, provenance `operator-confirmed`, source "probed
from the connected printer on <date>"), only at `apply`. They replace the
model value for that printer in the remaining thermal questions and in its
slice summary, where the line names them. The registry is never written,
and a declined or failed probe leaves the hardware record unchanged.

No setting is added and no default changes: no fan, temperature, or
chamber command is sent or enabled by recording or showing these facts.
A note that says a model's geometry is the same as another model's means
geometry only; the thermal facts are recorded separately, as above.
Surface verdict: no new variable — Defaults tree, environment loader,
Web UI catalog, and Unraid templates are unchanged, and `docs/SETTINGS.md`
needs no row because there is no new setting.

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

The Discord flow is unchanged up to the slice: preview, colors, copies, then "Slice and print now (bed is clear)", "Slice only", "Scale the model" and "Cancel print request". "Scale the model" asks bigger or smaller, then 1%, 5% or 10%, and repeats the confirmation at the new size; a size that does not fit the bed is refused, and the bot offers the biggest size that does fit instead. The slice summary still shows per-plate artifacts, estimates and validation. A vendor without `printer` delivery (Bambu Lab) does not get the "Slice and print now" button.

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

## Projects from another printer or slicer (#868)

A 3MF made for another printer (a Bambu Lab project from MakerWorld, an Orca project for a Creality, a PrusaSlicer project) carries that printer's whole profile. Print Handy never slices with it. It slices from the target printer's own Orca template and copies back only the settings that describe the model rather than the machine. The rules follow the ones [ForgeBridge](https://forgebridge.app/) publishes for converting projects to Flashforge printers; the key tables and the code are Print Handy's own.

**Kept from the project (author intent).** Wall count and wall generator, sparse infill density, pattern and angle (`infill_direction`), top and bottom shell layers and thickness, top and bottom surface patterns, seam position, wall order (`wall_sequence`), ironing (type, pattern, flow, spacing), elephant-foot compensation, gap fill, brim width and gap, and the support switch, type, style, threshold angle and placement options. Layer height is read but always replaced by the fitted nozzle's tuned default, and the brim type is always the template's.

**Speeds.** Speeds come from the template. An author speed is kept only when the designer set it lower than the template's own value for that key (a slow bridge, a gentle outer wall); a faster one is ignored. Percentages and 0 ("use the default") are never treated as speeds. Accelerations, jerk, temperatures and fans always come from the template, the filament family and the loaded spool.

**Values OrcaSlicer cannot read.** An author value for an option list (`sparse_infill_pattern`, `support_style`, `support_type`, `seam_position`, `top_surface_pattern`, `bottom_surface_pattern`, `ironing_type`, `ironing_pattern`, `wall_sequence`, `wall_generator`, `gap_fill_target`) that OrcaSlicer does not accept, such as another slicer's spelling or a newer slicer's option, is replaced by the template's value. Organic tree supports slice as Tree Hybrid when the project uses variable layer height or the support top Z distance is 0.

**Painted supports.** A project with painted support enforcers and supports turned off slices with supports on, unless the operator decided supports for the job (a "no supports" answer wins). Painting only blockers changes nothing. When the model is sliced from rebuilt geometry rather than the project itself, the painting does not reach the slicer, so the supports are generated automatically.

**Per-object and per-part settings.** Settings the author put on one object or volume (in `model_settings.config`, or PrusaSlicer's `Slic3r_PE_model.config`) are kept on every route, under the same rule as project-level settings: only the author-intent keys above, with values Orca accepts. Object-level settings apply to every part of the object, and a part's own value wins over its object's. That includes modifier volumes: a volume that only changes settings (a denser infill zone, a different seam) keeps those settings when Print Handy rebuilds the geometry, instead of keeping only its shape. A source that carries per-part settings is rebuilt as a 3MF rather than flattened to STL, so the settings have somewhere to live. Settings outside the author-intent list (a per-part temperature, a machine value) are not carried; the job's profiles own those.

**A dedicated support filament.** When the author designated a filament for the supports (`support_filament`, and `support_interface_filament` for the interface layers; PrusaSlicer's `support_material_extruder`), Print Handy keeps it, the way ForgeBridge 1.1.4 does. The designated filament prints no part of its own, so intake asks about it like any other source color ("Choose the loaded color/material for the support filament …"), the spool picked for it is loaded as one of the job's filaments, and the slice points the support settings at it. Picking the same spool as a part's color is fine; the supports then print in that spool. What this means per printer:

- **AD5X (ZMod or stock), four IFS slots:** the support spool takes one of the four slots, alongside the part colors.
- **Creator 5 / Creator 5 Pro, four toolheads:** the support spool is one of the toolheads; a toolhead no part uses can be picked for supports alone.
- **AD5M / AD5M Pro (one extruder):** there is no second filament to load, so a multicolor project is already split or paused by the normal single-extruder flow; on a one-filament job the supports print in the job's filament.
- **A derived one-filament plate** always prints its supports in the plate's own filament, whatever the source project designated.

If the operator turns supports off for the job, no support spool is loaded. A designation of 0 (the slicer's "the object's own filament") is not a designation and changes nothing.

**PrusaSlicer projects.** The common settings in `Metadata/Slic3r_PE.config` are translated to their Orca names and then follow the same rules: `perimeters`, `fill_density`, `fill_pattern`, `fill_angle`, `top_solid_layers`, `bottom_solid_layers`, `top_solid_min_thickness`, `bottom_solid_min_thickness`, `top_fill_pattern`, `bottom_fill_pattern`, `support_material`, `support_material_auto` and `support_material_style`, `support_material_threshold` (0 means automatic and is skipped), `support_material_buildplate_only`, `seam_position` (`rear` becomes `back`), `external_perimeters_first`, `ironing` and its type, flow and spacing, `elefant_foot_compensation`, `brim_width`, `brim_separation` and the print speeds. Unmapped keys are ignored. `perimeter_generator` is not mapped: PrusaSlicer defaults to Arachne, and Arachne walls select the print-in-place flex profile. Per-object settings in `Metadata/Slic3r_PE_model.config` are translated the same way (object settings apply to all of the object's volumes; a volume's own settings and its extruder assignment are carried too), and `support_material_extruder` names the support filament as above.

**Split and merged plates.** A plate Print Handy derives from the source (a split, a merge, one color per plate) is a copy of the source package, so the source's `project_settings.config` travels with it. Before Orca reads that copy, every setting the job's own machine, process and filament profile sets is rewritten to the job's value. A machine, speed, acceleration, temperature or G-code setting the job leaves unset is removed, so Orca uses its default. The bed type becomes the job's (OrcaSlicer's default Cool Plate on Flashforge printers, never a Supertack or textured plate from the source). `inherits` and `different_settings_to_system` are cleared. Per-object and per-part overrides in `model_settings.config` and height-range overrides keep only the author-intent settings above, and a plate keeps no machine or bed setting. A PrusaSlicer plate carries its settings in `Slic3r_PE.config` and `Slic3r_PE_model.config` instead: on the copy Orca reads, the first is translated and adopted exactly like a `project_settings.config` (and seeded with the job's machine half), the second is converted to a `model_settings.config` with every volume on the plate's one filament, and both Prusa files are removed. The source file itself is never changed. The prime tower is kept inside the bed on every plate using the tower width the job actually builds (the configured size, raised for a larger nozzle), not the template's.

**Protected settings.** Every Flashforge printer (AD5X, AD5M, AD5M Pro, Creator 5, Creator 5 Pro and the stock AD5X) ships an approved protected-settings baseline, so a slice whose start or end G-code differs from the approved one is refused instead of only warned about. The AD5X's came from a real slice. The other five were approved with the same `approve-baseline` command from each model's slice header rebuilt from its templates, the method that reproduces the AD5X's real approval exactly, and the real-slicer CI test checks each against Orca's own output.

**Height-based color changes (#870).** A project can change color by layer height (`Metadata/custom_gcode_per_layer.xml`, ChromaCanvas-style prints and signs). Those tool changes are carried: each extruder they switch to is asked about like any other source color, and the changes are written into the sliced geometry with the extruders remapped to the slots or toolheads the operator picked, so the print changes tool at the recorded heights. On a single-extruder printer (AD5M, AD5M Pro, stock AD5X) the operator chooses instead: pause at each change height and swap the filament by hand, or print in one color — the question names the colors and heights that would be lost, and the choice is recorded on the job, never left silent. Pauses and custom G-code in the same file are reported in the inventory but not carried. On a derived single-color plate the file still travels byte for byte, and the post-slice guard refuses a plate whose G-code changes to a tool the plate does not have.

**The rules in one table.** What happens to each kind of setting in a foreign project, on every Flashforge model (AD5X stock and ZMod, AD5M, AD5M Pro, Creator 5, Creator 5 Pro):

| Setting in the source project | Fate |
|---|---|
| Author quality settings (walls, infill, shells, seam, wall order, surface patterns, ironing, elephant foot, gap fill, brim width/gap, support switch/type/style/placement) | **Kept** — project level, per-object and per-part, with values Orca accepts; an unacceptable value falls back to the template's |
| Designer-lowered speeds | **Kept** — only where strictly below the template's value for that key |
| PrusaSlicer setting names | **Renamed** — translated to the Orca names above (`perimeters` → `wall_loops`, `fill_density` → `sparse_infill_density`, …); a name with no translation is dropped |
| Printer model, machine start/end G-code, bed type, printable area, motion limits | **Replaced** — the target model's own template values |
| Speeds the designer raised, accelerations, jerk | **Replaced** — the template's values |
| Layer height | **Replaced** — the fitted nozzle's tuned default (the owner's decision: a carried layer height is not kept) |
| Temperatures | **Replaced, never clamped** — the loaded spool's values (Spoolman wins); a job whose spool temperature is out of range is refused, not adjusted |
| A dedicated support / interface filament | **Kept** — asked about at intake, loaded, and pointed at (see above); on a one-filament plate or a single-extruder job it prints in the job's filament |
| Height-based color changes | **Kept** — asked about and remapped (#870); single-extruder printers get the recorded pause-or-one-color choice |
| `inherits`, `different_settings_to_system`, preset bookkeeping | **Dropped** — cleared, so nothing is re-applied as a "user change" over the job's profile |
| Foreign keys the job's profile does not set and Print Handy does not keep (machine, motion, temperature and G-code names) | **Dropped** on any copy Orca reads directly; on rebuilt routes they never enter the export |
| Prime tower | **Rebuilt** — the job's tower at the job's effective width, kept inside the bed; only the author's on/off intent informs it |
| The prime-tower / purge behavior of the source printer (AMS, chute, vendor macros) | **Dropped** — the target printer's own purge rules apply |

**The audit behind the table (#867).** A PrusaSlicer project and a Bambu-derivative (Creality/Orca) project were run through every Flashforge model, whole-model and as a derived plate, and each combination's kept / replaced / dropped sets were checked against what Orca actually reads (`tests/pipeline/test_foreign_audit_matrix.py`); the PrusaSlicer fixture also slices with the real Orca in CI (`tests/slicer/test_prusa_foreign_real.py`), supports printing in the designated support spool included. Every cell came out the same, which is the point of the table above:

| Source | AD5X (ZMod and stock) | AD5M / AD5M Pro | Creator 5 / Creator 5 Pro |
|---|---|---|---|
| PrusaSlicer project | kept: translated walls/infill, per-object settings · replaced: machine, speeds, temps · dropped: untranslated keys, Prusa files on plate copies · leaked: none | same | same |
| Bambu-derivative project | kept: walls/infill, per-object settings, support filament · replaced: machine, speeds, temps, bed · dropped: differs/inherit bookkeeping · leaked: none | same | same |

"Leaked: none" is a checked result, not a hope: each run asserts the source printer's name and start-G-code marker appear in no config and no member of the copy Orca reads, and a regression test keeps the author's own settings reachable even when the project's `different_settings_to_system` names them (the silent-drop failure ForgeBridge documented).

**If a project still does not convert well.** Print Handy converts locally and never sends a file anywhere. As a manual fallback, an operator can run the file through [ForgeBridge](https://forgebridge.app/) — Rodrigo Mass's Bambu/Prusa → Flashforge converter, whose published rules this conversion follows — and then drop the converted file into Print Handy like any other model. Print Handy will treat the converted project as a Flashforge project and apply the same rules above to whatever author intent it still carries.

## Source-profile compatibility warnings (#722)

A model's project settings can name the printer they were authored for (`printer_model_id` / `printer_settings_id` in `project_settings.config`). When that printer is demonstrably not the target printer (an exact name match after ignoring case, separators and a nozzle suffix, against the target label and the `printer_model` / `printer_settings_id` its own machine template declares; a variant such as "Kobra 2 Pro" is never treated as "Kobra 2"), Print Handy compares the authored motion and fan values against the target's verified facts — the axis speed and acceleration limits in its own machine template, and the 0–100% fan scale, checked for every filament in a multi-filament project — and shows a warning on the slice confirmation for each value outside them, naming the value, the source profile it came from, and the target fact it conflicts with. Values with no verified target range (wall count, brim width) are never warned about, and a source that names no printer produces no warnings.

The values a warning names are not used for slicing. Print Handy slices from the target printer's own template and carries over only the author-intent settings listed under [Projects from another printer or slicer](#projects-from-another-printer-or-slicer-868); speeds come from the template unless the designer set one lower, and accelerations and fan values always do. The warning says so ("Print Handy does not slice with this value; the <printer> profile's own setting is used."). Warnings change nothing themselves. The precedence is: template/vendor profile first, then the carried author-intent settings, the filament-family overlay, material-variant adjustments, and operator/spool values last (they win).

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
| H3C tool changer | Initial support only: sourced facts and the `32X` HMS serial prefix are recorded (`facts.py`), and H3C fault texts resolve per model once the HMS table regenerated from Bambu Studio 02.09.01.70 is in. Not a selectable model — the pinned OrcaSlicer carries no H3C profiles, so no checked identity exists yet. Unverified on hardware; see [BAMBU-LAB.md](BAMBU-LAB.md) |

Bambu Studio is not treated as a submission path. Picking it as the slicer changes only which program writes the G-code. Live upload or print control would need its own evaluation of firmware, LAN versus cloud authentication and credential handling before any of it is built.
