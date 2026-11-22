# Filament families and sub-types: design (#1788 phase B)

Status: decided, not built. This record fixes the model before any code
changes, as #1788 requires. Code that implements it should cite the section
it implements, and change this record if a decision changes.

## Why

On 2026-11-21 the first PETG spool on the AD5X exposed three gaps:

- Print Handy had no word for "high-speed PETG". `printer_state.material_family`
  refuses speed grades ("HS, HF, High Speed stay refused: no checked
  profile exists for them").
- The slice took the PLA template's flow ratio (1.05), because
  `DEFAULT_MATERIAL_TUNING` has a row only for PLA.
- The color question named the spool "High Speed Matte Black PETG", in
  whatever order the free-text Spoolman name happened to use.

The research comment on #1788 surveys the standards (OpenPrintTag, OpenTag3D,
OpenSpool, Bambu RFID, the vendor CFS formats, SpoolmanDB, Orca, HelixScreen
v2, zmod_nfc). This record turns it into decisions.

## Decisions

### 1. A spool's type is a family plus a set of flags

- **Family:** the base polymer, with a filler kept in the family: `PLA`,
  `PETG`, `PETG-CF`, `PA-CF`, `PCTG`, and so on. This is
  `printer_state.material_family` as it is today, so verification,
  material warnings (#1336) and the slicer's `filament_type` keep working
  unchanged. The family list is Orca's `MaterialType` list, the superset
  that slicers and `lane_data` match on.
- **Flags:** a set of property words modelled on OpenPrintTag `tags`, the
  only enumerated vocabulary with a speed grade and implication rules.
  Print Handy adopts this subset and nothing else until a source needs
  more:

| Flag | Meaning | Short code in labels |
| :--- | :--- | :--- |
| `high_speed` | Modified for faster printing (higher melt flow). OpenPrintTag's word; HS and HF both map here, since no standard separates them | `HS` |
| `matte` | Matte finish | finish word "Matte" |
| `silk` | Silk finish | finish word "Silk" |
| `translucent` | Translucent | finish word "Translucent" |
| `plus` | Toughened "PLA+" or "Pro" grade. No standard has this; Print Handy names it so PLA+ stops being a separate family | `+` |
| `glow_in_the_dark`, `contains_wood`, `imitates_marble` | Effect fills | not shown in the short label |

Fillers (`contains_carbon_fiber`, `contains_glass_fiber`) stay in the
family (`PETG-CF`) and are not flags. Shore hardness (`95A`) is a property
of the TPU spool, not a flag.

**Unknown words are dropped, never guessed.** A label word Print Handy
cannot map leaves the flag set without it, and is logged once per spool.

### 2. Reading flags from each source

One table maps each source's vocabulary to flags, kept beside
`filament_families.toml` (data, not code):

| Source | Field | Maps |
| :--- | :--- | :--- |
| Spoolman filament | `material`, `name`, and the extra fields `finish` / `pattern` / `fill` / `translucent` / `glow` when SpoolmanDB filled them | `material` gives the family; `finish: matte` sets `matte`; name words (`High Speed`, `HS`, `HF`, `Matte`, `Silk`, `+`, `Pro`) set flags |
| HelixScreen v2 lane | `material`, `filament_type`, `filament_id` | `material` gives the family; `filament_type` words ("PLA Silk") set flags; `filament_id` resolves through Orca's profiles (section 3) |
| OpenTag3D / Snapmaker modifiers (through zmod_nfc) | `material_mod` | `HF` sets `high_speed`, `Silk` sets `silk`, `Matte` sets `matte`, `Pro` sets `plus`; `Basic` sets nothing |
| Bambu detailed type / product ID | "PETG HF", `GFG02`, `GFG96` | `HF` sets `high_speed`; the ID resolves through Bambu Studio's profiles |

HelixScreen's legacy `helix_material` is read only when the v2 fields are
empty, and never wins over them.

**Reuse before building:** zmod_nfc already normalizes every NFC format to
`material + modifiers[]`. When an NFC reader is in use, Print Handy reads
its output through the table above instead of decoding tags itself.

### 3. Slicing values come from the vendor's own profile first

For a family plus flags on a given printer and nozzle, the slice takes its
values (flow ratio, max volumetric speed, temperatures, bed, fans) from the
first match in this order:

1. **The printer vendor's profile for that sub-type**, matched by Orca
   `filament_id` (Orca's ID-first rule). PETG with `high_speed` on a
   Flashforge AD5X 0.4 resolves to `HS PETG @FF AD5X` (`OFDxxTpW`): flow
   0.98, 12 mm³/s, 235 °C (240 first layer), bed 75 °C.
2. **The vendor's generic profile for the family**: `Generic PETG @Flashforge`
   (`OFYPdQJh`): flow 1.0, 12 mm³/s, 255 °C, bed 70 °C.
3. **The library generic**: `filament_families.toml`, as today.
4. **Spool and operator overrides on top**, as today: `material_tuning`,
   then the Spoolman spool's temperatures.

The slice header's `filament_settings_id` names the profile used (for
example `HS PETG @FF AD5X`), not "Generic PLA", so a print record says which
values it was cut with.

**No family inherits another family's tuning.** A PETG job on the PLA
template takes the template's PLA flow ratio today; under this model the
family's own row always replaces it, and a family with no row at any level
takes 1.0.

`material_tuning` stays keyed by family. A per-flag override
(`material_tuning."PETG high_speed"`) is the next step only if a real
spool needs it; the vendor profile covers today's case.

### 4. One label everywhere: `<Color> <Finish>, <Family> <Sub-type>`

Built from the structured fields, never from the free-text name:

- **Color** is the swatch name (`named_color`), else Spoolman's color name.
- **Finish** is the finish flag word (Matte, Silk, Translucent), or nothing.
- **Family** is the family.
- **Sub-type** is the short codes (`HS`, `+`), or nothing.

| Spool | Today | Under this model |
| :--- | :--- | :--- |
| #22 | High Speed Matte Black PETG | Black Matte, PETG HS |
| #16 | Matte White PLA | White Matte, PLA |
| #19 | Bronze PLA+ | Bronze, PLA+ |
| #12 | Dark Green PLA | Dark Green, PLA |

The same label is used in color questions, `/print-spools`, job posts,
slot-verify questions, the Web UI and the lane name pushed to HelixScreen.
`source_colors._spool_label`, `print_summary.spool_picker_label` and
`lane_data_sync._spool_label` become one function.

### 5. Verification compares family and flags

`slot_verify` and the lane watcher compare the bound spool's family plus
flags with the lane's (section 2). A family difference is a doubt, as
today. A flag difference that changes the slice (`high_speed` present on
one side only) is also a doubt, because it picks a different profile. A
finish-only difference (`matte`) is not: it never changes the slice.

### 6. One committed database of every Orca vendor profile (#1816)

Andrew, 2026-11-22: vendor values come from one database of every OrcaSlicer vendor printer and filament
profile, not a hand-picked table, and an Orca update goes through it.

- **Source:** OrcaSlicer's `resources/profiles` at the release `docker/orca-prebuilt.sh` pins (v2.5.1:
  72 vendor folders, 1,194 printer presets, 7,637 filament presets). Generated by
  `scripts/build_orca_profile_db.py`; never edited by hand. Regenerating at the same pin is
  byte-identical, and a test fails when the database's Orca commit is not the pinned one.
- **Committed, not generated at install:** `print_handy/data/orca_profiles/` (an `index.json` of printer
  presets plus one JSON-lines file per vendor), so every Orca bump shows its profile changes as a reviewable
  diff. The profiles are AGPL-3.0 like Print Handy; NOTICE credits OrcaSlicer. This reverses, for this data
  only, the "Print Handy ships no OrcaSlicer profiles" rule in the vendor guides (maintainer decision,
  2026-11-22); the per-printer template pipelines are unchanged.
- **Orca bumps:** the bump regenerates the database and puts `orca_profile_db.diff_markdown` (presets added
  and removed, changed flow, temperature, volumetric speed, bed, fan and pressure-advance values) in the PR.
- **Resolution (section 3, levels 1 and 2):** `orca_profile_db.resolve(printer preset, filament type)`. The
  printer preset is the printer model plus the nozzle diameter, so the nozzle picks the vendor's per-nozzle
  profile (the AD5X has 0.25, 0.4, 0.6 and 0.8 sets). A profile's slice-relevant flags (`high_speed`, `silk`,
  `plus`) must all be on the spool; finish flags only break ties. Third-party brands in a vendor's folder are
  not used. No vendor profile means level 3, the library generic, as today.
- **Profile names:** the G-code's `filament_settings_id` names the vendor profile it was sliced with. The
  frozen `Hermes slot <n> <material>` names are retired (a breaking change; old G-code and history need not
  keep working; the other legacy names are #1817).
- **Nozzle type** (`nozzle.py` temperature offset and flow multiplier) is its own layer after the vendor
  profile and before spool and operator overrides, and every final value records the layer that set it.

## Build order

1. The flag table and parser (section 2), with the four labels in section
   4 as tests. No behavior change yet.
2. The label function (section 4), adopted everywhere a spool is named.
3. Vendor profile resolution (section 3), with the PETG flow-ratio
   inheritance fixed and `filament_settings_id` named. This changes slices,
   so it carries slicer corpus checks.
4. Flag-aware verification (section 5).

Each step is its own PR, referencing #1788.

## Not decided here

- High-flow against high-speed: no standard separates them, so both are
  `high_speed` until a vendor publishes different profiles for each.
- Per-flag `material_tuning` keys (section 3).
- Writing flags back to Spoolman's extra fields; Print Handy reads them only.
