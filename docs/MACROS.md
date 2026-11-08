# Printer macros

Macros are a Moonraker/Klipper feature: a macro is a named block in the
printer's own Klipper config, and Moonraker lists the ones a printer actually
defines. Print Handy does not hard-code a per-model macro list — it asks the
printer.

## `/print-macros` — what your printer defines (Stage 1, read-only)

Run `/print-macros` in Discord, Telegram or Matrix. On a printer whose
connection is Moonraker, Print Handy makes one read-only request —
`GET /printer/objects/list` — and lists every `gcode_macro <NAME>` the
printer reports, classified:

- **Known to Print Handy** — the name matches a macro entry in the
  descriptive registry (the per-firmware files under
  [`print_handy/data/firmware-commands/`](../print_handy/data/firmware-commands), the same registry
  `/print-lookup` searches), **and that entry is scoped to
  this printer model**. The listing shows the registry's description and
  safety note. Scoping matters: macro names repeat across firmwares with
  different behavior (a Sovol `START_PRINT` is not ZMod's `START_PRINT`), so
  an entry written for another printer never describes this printer's macro —
  the name falls back to "defined by your printer's config" instead. The run confirmation follows the same rule for the safety
  note it shows.
- **Defined by your printer's config** — the printer defines it but the
  registry does not describe it. Listed by name only; Print Handy invents no
  description for a macro it has not verified.
- **Internal helper macros** — names starting with `_`, the Klipper helper
  convention. Listed separately, never presented as operator macros, and
  never runnable from the bot (Stage 2 included).

The listing sends nothing to the printer beyond that one read request. If
Moonraker is unreachable or answers something unreadable, the reply is
"Could not read the printer's macros" and nothing is sent.

On a printer whose connection is not Moonraker there is no macro namespace
to read, and `/print-macros` says so plainly instead of failing — see the
matrix below.

## `/print-macros run <name> [KEY=VALUE ...]` — run one, guarded (Stage 2)

```
/print-macros run START_PRINT BED_TEMP=60 EXTRUDER_TEMP=210
```

Running a macro is deliberately narrow:

- **Discovered names only.** The set of macros is read live from the
  printer again at run time, and the typed name must match one of those
  names (case-insensitively, Klipper's own convention). A name the printer
  does not define is refused — Print Handy never sends a typed name on
  trust, and there is no way to type free G-code here. Internal
  (`_`-prefixed) macros are refused even though they are discovered.
- **Arguments are `KEY=VALUE` words only** — one word each, letters, digits
  and `. _ + -` in the value, no spaces, quotes or other symbols, no key
  twice. Anything else refuses the run before any prompt appears.
- **The confirmation shows the exact line.** Before anything is sent, the
  confirm prompt displays the single script line exactly as it will be
  posted, plus the registry safety note for a known macro — or, for a
  printer-defined macro, the explicit statement that it is defined by the
  printer's own config and is not described by Print Handy. Only the usual
  confirm button runs it; declining or letting the prompt time out sends
  nothing.
- **Never while printing.** The printer's `print_stats` state is checked
  before the prompt and again immediately before sending. Printing, paused,
  an unrecognised state, or an unreadable state at either check refuses the
  run and sends nothing.
- **One POST, honestly reported.** A confirmed run makes one
  `POST /printer/gcode/script` with that one line. Success means Moonraker
  accepted the script — the reply says so and does not claim the macro's own
  actions have finished. A failed send is reported as failed/uncertain,
  never as "ran".

The natural-language (LLM) helper may name only the bare, read-only
`/print-macros` listing. It can never name a `run` form — running a macro is
something a person types and confirms, the same treatment the other
state-changing commands get in the allow-list.

## The per-vendor macro matrix, as it actually is

The Web UI's printer picker offers exactly the registered models — the
picker's choices are derived from the printer registry, not hard-coded — so
this matrix covers every model a user can pick. "Registry coverage" counts
the `print_handy/data/firmware-commands/` macro entries scoped to the model: the entries
`/print-macros` can use to describe that model's discovered macros.

| Printer family (models) | Connection | Macro situation |
| --- | --- | --- |
| Flashforge AD5X (ZMod) | Moonraker | ZMod macros in the printer's Klipper config: `START_PRINT` / `END_PRINT` own the job start and end, `CHANGE_ZCOLOR` syncs IFS slots to the native screen, `AUTO_FULL_BED_LEVEL` and `BED_LEVEL_SCREWS_TUNE` are the levelling helpers, and the IFS feed path is internal (`_`-prefixed). The registry also describes ZMod's operator macros verified from its shipped config (Stage 3 of #291): `COLDPULL`, `LOAD_FILAMENT` / `UNLOAD_FILAMENT` / `LOAD_MATERIAL`, `CLEAR_NOZZLE`, `SET_EXTRUDER_SLOT`, the Spoolman pair `SET_ACTIVE_SPOOL` / `CLEAR_ACTIVE_SPOOL`, `SAVE_ZMOD_DATA` / `GET_ZMOD_DATA`, `NOZZLE_CONTROL`, the PID tunes, `ZSHAPER`, `DISPLAY_OFF` / `DISPLAY_ON`, and Print Handy's own `PRINT_HANDY` slot-match reporter. Registry coverage: 29 entries. `/print-macros` discovers the live set; `/print-lookup` describes the registry-known ones. |
| Flashforge AD5M / AD5M Pro (ZMod) | Moonraker | The same ZMod firmware family as the AD5X: the AD5M models ship the same `zmod_ff5x` `base.cfg` and `client.cfg`, so the registry scopes the shared ZMod operator set to them too — start/end, the filament and cold-pull set, the Spoolman pair, the data and display macros, the PID tunes, `ZSHAPER`, and the Mainsail/ZMod client pause set. The AD5X-only hardware entries stay AD5X-only: the IFS path (`CHANGE_ZCOLOR`, `_INSERT_PRUTOK_IFS`, `SET_EXTRUDER_SLOT`), the levelling helpers verified on the AD5X, and `PRINT_HANDY`. Registry coverage: 23 entries. |
| Flashforge Creator 5 / Creator 5 Pro (ZMod) | Moonraker | ZMod models whose Print Handy templates emit the same `START_PRINT ... MESH=auto` start, `END_PRINT` and `SET_PRINT_STATS_INFO` lines as the AD5X — the registry describes exactly those three for the Creator pair. The wider ZMod operator set is **not** scoped to them: it was verified from the ff5x config set the AD5X and AD5M ship, not from a Creator ZMod install (recorded NO-ADD below). Registry coverage: 3 entries. |
| Generic Klipper, Sovol SV06 | Moonraker | The printer's config is expected to provide `PRINT_START` / `PRINT_END`; Print Handy's profiles emit those names and the installed macro owns heating, homing and mesh loading. Anything else the config defines is discovered live by `/print-macros`. Where the config is Mainsail's client config, the registry also describes its `PAUSE` / `RESUME` / `CANCEL_PRINT` wrappers and the layer-pause set (`SET_PAUSE_NEXT_LAYER`, `SET_PAUSE_AT_LAYER`, `SET_PRINT_STATS_INFO`) — Klipper's own commands of the pause/resume names are built-ins under `[pause_resume]`, not macros, and never appear in a listing. Registry coverage: 8 entries each. |
| Sovol Klipper, other models (SV01 line, SV02–SV05, SV06 ACE / Plus / Plus ACE, SV07 / SV07 Plus, SV08 Max, M1D) | Moonraker | Print Handy's templates emit the same `PRINT_START` / `PRINT_END` pair as the SV06 for every Sovol Klipper model, and the registry now scopes that pair to all of them. The Mainsail client set is **not** scoped beyond the SV06 and generic Klipper: those entries describe Mainsail's client config specifically, and the other models' stock configs were not verified to ship it (recorded NO-ADD below). Registry coverage: 2 entries each. The SV08 Max also gets the stock `START_PRINT` entry (below), 3 in all. |
| Sovol SV08 | Moonraker | Two start names are real on this model. Print Handy's templates emit `PRINT_START`, and Sovol's stock firmware config (Macro.cfg) defines its own `START_PRINT` — which takes no temperature arguments and reads the heater targets instead — plus a stock `CLEAN_NOZZLE`. The registry describes all three (and `PRINT_END`), with the stock names as their own entries so neither is described as the other. The stock end macro's name was not verified from a citable source, so no entry claims it (recorded NO-ADD below). Registry coverage: 4 entries. |
| Sovol Zero | Moonraker | Registered as `sovol.zero` since 2.2.1. Stock firmware names its macros `START_PRINT` / `END_PRINT` (called bare; temperatures ride on the heater lines), and the registry now scopes entries for both to the Zero from the #612 audit inventory — see the Zero section below for the community macro pack that can replace them. Print Handy's template for the Zero emits `PRINT_START`, which is also described. Registry coverage: 4 entries. |
| Creality Ender-3 V3 / V3 KE / V3 Plus, K1 / K1C / K1 Max | Moonraker | Creality's Klipper firmware (Creality OS) defines `START_PRINT` and `END_PRINT` in its `gcode_macro.cfg`; Print Handy's profiles open with `START_PRINT` (the Ender-3 V3 KE profile may run fully inline instead — the macro is still the family's stock start). The registry describes both, scoped to all six models. Registry coverage: 2 entries each. |
| Qidi Q1 Pro, X-Max 3, X-Plus 3, X-Plus 4, X-Smart 3 | Moonraker | Qidi's stock Klipper start macro is `PRINT_START` — Qidi's own profiles open with it (the Q1 Pro passes `BED=` / `HOTEND=` / `CHAMBER=`) and continue inline. The registry describes it for all five models. There is no `PRINT_END` entry: Qidi's profiles store the end sequence inline, not as a macro (recorded NO-ADD below). Registry coverage: 1 entry each. |
| Snapmaker U1 | Moonraker | Print Handy's U1 templates emit the same `PRINT_START` / `PRINT_END` pair as the Sovol Klipper line, and the registry scopes that pair to the U1 on the same basis: the templates are generic files this project wrote, and the U1's own start sequence is not confirmed on hardware. No wider macro set is described for the U1: its stock Klipper-fork macro names beyond the pair its Print Handy templates emit were not verified from a citable source. Registry coverage: 2 entries. |
| Anycubic Kobra 2 / Neo / Pro / Plus / Max | Moonraker | A Moonraker family, so `/print-macros` works — but Print Handy's profiles for the family run a **fully inline** start sequence with no start macro at all, and the Anycubic guard treats a `START_PRINT`-led file as another ecosystem's start code. No registry entries are scoped to these models (recorded NO-ADD below): a community Klipper install on a Kobra 2 commonly defines `START_PRINT` / `END_PRINT`, but describing those names for this family would contradict what Print Handy itself ships and accepts for it, so discovered macros on a Kobra 2 list as defined by the printer's own config. Registry coverage: 0 entries, by verdict. |
| Elegoo Neptune 4 / Pro / Plus / Max | Moonraker | A Moonraker printer, so `/print-macros` works — but note the start sequence is **inline** in the sliced file, not a macro call. `PRINT_START` is a foreign marker on this family: Print Handy's Elegoo guard refuses a file that calls it (see `print_handy/printers/elegoo/guard.py`). A `PRINT_START` macro appearing in a Neptune's own config listing is the printer's business; Print Handy never emits one into a Neptune job. No registry entries are scoped to these models (recorded NO-ADD below). Registry coverage: 0 entries, by verdict. |
| Bambu Lab (all 13 models) | Bambu LAN | No Klipper macros at all. The printer runs plain G/M/T codes only; Klipper or ZMod macro lines are refused by the G-code guard before they can reach the hardware (see the note in `print_handy/printers/base.py`). No macro namespace, so `/print-macros` explains instead of listing. Bambu's error vocabulary is its HMS fault codes; those are decoded by the dedicated HMS lookup in the Bambu adapter from its generated table, not by this registry (recorded verdict below). |
| Prusa MK4S / CORE One / XL / MINI+ | PrusaLink | Buddy firmware, not Klipper. There is no Moonraker and no user macro namespace; start/end behavior lives in the slicer profile's G-code. `/print-macros` explains instead of listing. The command side is decided: Buddy is its own registry family (`prusa`), describing the layer that is Prusa's alone — the `M862.3` model check and `M862.6` feature check the stock start G-code opens with, the print-check refusal they produce, and the XL's `M863` tool remapping. The generic Marlin-compatible codes are not repeated there. Registry coverage: 3 entries per model, 4 on the XL. |
| Elegoo Centauri Carbon | SDCP | No Moonraker and no macro namespace exposed over SDCP. `/print-macros` explains instead of listing. SDCP's status codes are protocol values Print Handy's client translates; there is no published operator command or error vocabulary to describe (recorded verdict below). |
| Flashforge Adventurer 5X (stock) | Stock Flashforge | Stock firmware, plain G-code only — the same line-by-line guard posture as Bambu. The ZMod macro set exists only on a ZMod install (the AD5X row above). `/print-macros` explains instead of listing. The stock LAN API is the protocol Print Handy speaks to the printer, not an operator vocabulary, and stock publishes no error-code set for lookup (recorded verdict below). |
| Marlin via OctoPrint (all 13 OctoPrint models, and the 8 Sovol Marlin/OctoPrint models) | OctoPrint | Marlin has no Klipper-style macro namespace; start/end G-code lives in the slicer profile or OctoPrint settings. `/print-macros` explains instead of listing. The command/error side is decided per model: the standard Marlin set — homing, mesh probing and loading, probe offset, filament change, EEPROM store, and the thermal errors — is scoped to every one of the 21 presets, so `/print-lookup` describes it for a named Ender, Kobra or Sovol Marlin preset exactly as it does for the generic `octoprint.marlin`. Registry coverage: 8 entries scoped per model, plus the shared heater entry. |

### Recorded NO-ADD verdicts

A NO-ADD verdict is a decision, not a gap left hanging: the model's macros
stay discoverable live by `/print-macros`, listed as defined by the printer's
own config, with no invented description.

- **Elegoo Neptune 4 family** — no entries. The start sequence is inline by
  design and `PRINT_START` is a refused foreign marker; no vendor operator
  macro set for the family was verified from a citable source.
- **Anycubic Kobra 2 family** — no entries. Print Handy's profiles are fully
  inline and its guard refuses `START_PRINT`-led files as foreign, so the
  community Klipper `START_PRINT` convention is deliberately not described
  for this family.
- **Qidi end macro** — no `PRINT_END` entry. Qidi's profiles store the end
  sequence inline; only `PRINT_START` is a macro call.
- **Mainsail client set beyond generic Klipper, the SV06 and the ZMod
  models** — the `PAUSE` / `RESUME` / `CANCEL_PRINT` wrappers and the
  layer-pause set describe Mainsail's client config (and ZMod's client
  config) specifically. They are not scoped to the Creality, Qidi, Anycubic,
  Elegoo or other Sovol models, whose stock configs were not verified to
  ship that client config; scoping them by name alone is the #612 error.
- **Sovol SV08 stock end macro** — no entry under its stock name. The
  SV08's stock `START_PRINT` and `CLEAN_NOZZLE` are verified from its
  Macro.cfg; its stock end name was not verified from a citable source, so
  only the `PRINT_END` name Print Handy emits is described.
- **Creator 5 / Creator 5 Pro ZMod operator set** — only the three names
  their templates emit are described. The wider ZMod operator set was
  verified from the ff5x config set, not from a Creator ZMod install.
- **AD5M / AD5M Pro IFS and levelling entries** — `CHANGE_ZCOLOR`, the IFS
  feed path, `SET_EXTRUDER_SLOT`, `AUTO_FULL_BED_LEVEL`,
  `BED_LEVEL_SCREWS_TUNE` and `PRINT_HANDY` stay AD5X-only: the IFS is
  AD5X hardware, and the helpers were verified on the AD5X.
- **Non-Moonraker macros** (Bambu Lab, Prusa, Elegoo SDCP, stock
  Flashforge, Marlin/OctoPrint) — no macro entries, because there is no
  macro namespace to describe; `/print-macros` explains instead of listing.
  This verdict covers macros only; the command and error side of each family
  is decided separately in the next section.
- **Prusa `M870` chamber vents** — no entry. Prusa's Buddy command list
  documents `M870` for the CORE One+ and CORE One L; the registered model is
  the CORE One, whose vents are not the automatic ones that code drives, so
  the code is not described for it.
- **Bambu Lab HMS codes in the registry** — no entries. Bambu's error
  vocabulary is real and citable, but it already has a dedicated home: the
  Bambu adapter's HMS lookup decodes the codes from a table generated from
  Bambu Studio's own resources at a pinned commit (`print_handy/printers/bambulab/hms.py`),
  per-model texts included. Registry entries would duplicate that generated
  table in hand-written form and drift from it.
- **Elegoo SDCP commands and errors** — no entries. SDCP is a machine
  protocol: its command numbers and status codes are values Print Handy's
  client sends and translates, not an operator vocabulary, and Elegoo
  publishes no user-facing error-code set for the Centauri Carbon that a
  lookup could describe.
- **Stock Flashforge commands and errors** — no entries. Beyond plain
  G-code, the stock AD5X speaks Flashforge's LAN HTTP API, which is the
  integration protocol Print Handy's stock adapter implements, not a
  vocabulary a user types or reads; stock publishes no error-code set for
  lookup. The described macro set for this hardware is the ZMod one, and it
  exists only on a ZMod install.

## Commands and errors for the non-Moonraker families

The registry's entries cover commands and error codes as well as macros,
and its lookup serves every registered model — so #645's macro verdicts
left the other two kinds undecided for the non-Moonraker families. #647
decides them, family by family:

- **Marlin over OctoPrint — entries, scopes widened.** The Marlin entries
  were written as generic Marlin knowledge but scoped to the generic
  `octoprint.marlin` preset alone, so the 20 named presets (the Ender and
  Anycubic OctoPrint models, the Sovol Marlin line) resolved almost nothing
  Marlin-specific. Every preset runs this same firmware, so the standard
  set now names all 21 models, and one error entry was added on the same
  evidence: `MINTEMP / MAXTEMP triggered`, Marlin's own sensor-range error
  strings. The heater entry stays scoped to all printers, as before.
- **Prusa Buddy — entries, under a new `prusa` firmware family.** Buddy is
  Prusa's fork of Marlin 2, but the layer a user meets is Prusa's own and
  Prusa publishes it: the `M862.3` / `M862.6` print checks that open the
  stock start G-code, the refusal they produce, and the XL's `M863` tool
  remapping. Those entries sit in `print_handy/data/firmware-commands/prusa.toml` under
  firmware `prusa`, and the Prusa models' registry `firmware_family` is now
  `prusa`. A separate family — rather than a scope on the Marlin file — is
  what keeps Marlin's entries from describing Prusa's checks and Prusa's
  checks from leaking into Marlin lookups; the generic Marlin-compatible
  codes are deliberately not repeated in the Prusa file. `M870` is the one
  documented Buddy code deliberately left out (verdict above).
- **Bambu Lab, Elegoo SDCP, stock Flashforge — recorded verdicts, no
  entries**, for the reasons in the verdict list above: Bambu's HMS
  vocabulary is already decoded by its own generated lookup, and SDCP and
  the stock Flashforge API are integration protocols with no published
  operator command or error vocabulary to describe.

Every non-Moonraker model now ends in a decided state — scoped entries or
a named verdict — and the coverage tests pin that state, including that no
family's lookup borrows another family's entries.

## Sovol Zero, and the svzero-profiles macro pack

The Sovol Zero is registered as `sovol.zero` (since 2.2.1, alongside the
other Sovol Klipper models). What follows is verified from the community
profile project [rpcyan586/svzero-profiles](https://github.com/rpcyan586/svzero-profiles)
at `main` eb38beb (release v2.1.2; GPL-3.0 — facts only, nothing copied), so a
Zero's behavior is named instead of assumed to be plain stock Klipper:

- **Stock start/end naming differs.** Stock Sovol firmware (both 1.3.7 and
  1.4.x) ships `START_PRINT` / `END_PRINT`, and the profiles call `START_PRINT`
  **bare** — the job's temperatures ride on its `M140` / `M190` / `M104` /
  `M109` heater lines, not on macro arguments. Print Handy's G-code check
  accepts that shape for a Moonraker printer when the temperatures are proved
  (by `BED_TEMP=` / `EXTRUDER_TEMP=` arguments, or by matching first `M190` /
  `M109` lines), and its own sliced jobs still emit `PRINT_START BED=…
  HOTEND=…` as before.
- **The optional macro pack replaces core behavior.** One firmware-matched
  file (75 macros on 1.3.7, 76 on 1.4.x) replaces `START_PRINT`, `END_PRINT`
  and `CANCEL_PRINT`, overrides the built-in commands `M104`,
  `SET_HEATER_TEMPERATURE`, `M141` and `M191`, adds a no-op `T0` and its own
  `LOAD_FILAMENT` / `UNLOAD_FILAMENT`, plus chamber control, a nozzle-brush
  routine, a positioned purge (`PURGE_LINE`) and an input-shaper test. On a
  printer running it, those command names do what the pack says, not what
  stock Klipper does.
- **Jobs can be refused after Print Handy's checks.** The sliced start
  sequence calls gates that run before any heater is commanded: `_PREFLIGHT`
  (no filament at the runout sensor; with the optional Python helper, also a
  Spoolman material/remaining check) and `SVZERO_REQUIRE` (a job sliced for a
  different machine mode is refused; a pack-version mismatch only warns). If
  a confirmed print does not start and the printer stayed cold, check for
  these gates at the printer console — Print Handy cannot read the refusal
  reason from Moonraker state.
- **Modes change the machine.** A precision mode (separate slicer profiles,
  different stepper microsteps/current) is selected by a config include and a
  Klipper restart; with the pack's Moonraker helper installed, a job sliced
  for the other mode can make the printer swap modes, restart and start the
  file again. `SVZERO_XY_CURRENT` lowers X/Y current for one job and relies
  on the pack's `END_PRINT` / `CANCEL_PRINT` to restore it.
- **Timers keep working after end/cancel.** Chamber fan loops and a
  post-print cooldown ramp (bed target lowered gradually, fans weighted in)
  run on delayed timers, so fans and the bed target can keep changing after
  Print Handy reports a job finished or canceled. That is the pack working
  as designed, not a stuck print.
- **The two firmwares are not interchangeable.** The 1.4.x pack replaces the
  stock `[temperature_fan exhaust_fan]` section and probes by plate contact;
  the 1.3.7 pack merges into the existing fan definitions and probes with a
  pressure tare-tap, and the first-layer nozzle-growth offset
  (`SVZERO_NOZZLE_GROWTH`) only applies on 1.3.7. Do not carry a macro
  description from one version to the other.
- **Profiles without the pack still print.** The profiles' start G-code
  calls pack macros that stock firmware does not define; stock Klipper warns
  about each unknown command and continues, and a priming skirt covers the
  missing purge. That stepping-over is a Klipper behavior for unknown
  commands, not a promise about every line: a command that names something
  the config does not have (the pack's own example is `SET_GCODE_VARIABLE`
  against a missing target) can still fail a job.
- **Registry posture.** The Zero is registered now, and this audit's
  inventory was the evidence base for scoped entries, as planned: the
  **stock** firmware's `START_PRINT` and `END_PRINT` now have entries scoped
  to `sovol.zero`, describing the stock macros. The optional pack's own
  names still have **no entries**: the pack replaces the stock start, end
  and cancel behavior, so on a pack printer those names do what the pack
  says — a listing classifies by name and scope, and cannot tell a stock
  `START_PRINT` from the pack's replacement. Read a Zero's known-macro
  descriptions as stock-firmware descriptions, and check for the pack's
  gates (above) when a print behaves unexpectedly. Thanks to printer-scoped
  classification, a Zero's listing never borrows the ZMod descriptions its
  macro names happen to share.

## Where the knowledge lives

- The **printer** is the authority on which macros exist — that is what
  `/print-macros` reads, live.
- The **registry** (the per-firmware files under `print_handy/data/firmware-commands/`) is the descriptive layer: it
  says what a known macro does and what to be careful about. It is advisory
  reference data, also used by `/print-lookup`; the lookup, troubleshooting
  and macro-listing code never sends an entry to a printer. The only send
  path for a macro is the guarded `/print-macros run` flow above, and it
  sends the operator-confirmed line for a live-discovered name — the
  registry supplies the safety note in the prompt, never the command.
- Registry entries are added only from verified firmware evidence. An
  unverified name stays "defined by your printer's
  config" — present in the listing, without an invented description.

Nothing here claims live-hardware verification: discovery is tested offline
against recorded Moonraker response shapes, as with the Bambu, Prusa and
Elegoo LAN work.
