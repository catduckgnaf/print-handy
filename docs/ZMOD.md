# ZMod printers

Print Handy's home platform is Flashforge printers running [ZMod](https://zmod.link)
(Klipper + Moonraker on top of the stock firmware). ZMod supports five Flashforge
models; Print Handy has first-class profiles for all five
([#301](https://github.com/catduckgnaf/print-handy/issues/301)).

| Model | `[printer] model` | Build volume | Filament system | Status |
| --- | --- | --- | --- | --- |
| Flashforge AD5X | `flashforge.ad5x` | 220 × 220 × 220 mm | 4-slot IFS feeder | Supported |
| Flashforge AD5M | `flashforge.ad5m` | 220 × 220 × 220 mm | Single extruder, no IFS | Supported |
| Flashforge AD5M Pro | `flashforge.ad5m_pro` | 220 × 220 × 220 mm | Single extruder, no IFS | Supported |
| Flashforge Creator 5 | `flashforge.creator5` | 256 × 256 × 256 mm | 4 independent toolheads | Supported |
| Flashforge Creator 5 Pro | `flashforge.creator5_pro` | 256 × 256 × 256 mm | 4 independent toolheads | Supported |

Every profile in this table is offline-verified. The only live checks so far
are on one AD5X (ZMod 1.7.5-2, screenless): Z offset sync, a full mesh run
and IFS slot presence, listed in [DEVICE-AD5X.md](DEVICE-AD5X.md#verified-on-hardware).
No print started by Print Handy has been verified on ZMod hardware.

## Audited ZMod version

Print Handy's ZMod contracts were last audited against **ZMod 1.7.5**
(released 2026-10-27; the feature-by-feature audit is
[#562](https://github.com/catduckgnaf/print-handy/issues/562)). The audit
found no change to the contracts Print Handy relies on for the .gcode files
it delivers. 1.7.5's headline features (3MF support, instant color
mapping without the `FORCE_MD5` preprocessor, fast KAMP data) apply to the
3MF path. Since #564, Print Handy delivers that `.gcode.3mf` package by
default on ZMod 1.7.5 or newer (`[printer] zmod_delivery_format`, see
[DEVICE-AD5X.md](DEVICE-AD5X.md)); that path is not yet live-verified. No `START_PRINT`
signature, `SAVE_ZMOD_DATA` variable, timelapse, camera or `notify` change
is recorded upstream. The next ZMod release diffs against this 1.7.5
baseline.

`/print-firmware` reports the installed ZMod version for a ZMod printer,
read from the `zmod` component in Moonraker's update status (ZMod names it
`Z-Mod`). On the printer itself that component is a git checkout Moonraker
cannot read, so it reports `?`; Print Handy then reads the version from the
OS distribution in `/machine/system_info`, which ZMod names like
`zmod 1.7.5 -> 1.7.5-2` (base -> installed), and says so in the report. When
neither source names a version it states "ZMod version unknown". `?` is never
treated as a version or as an available update. The doctor
reports it too, on its own `zmod version` row, so the diagnostics bundle
carries it into bug reports. Print Handy never guesses a version and
never gates printing on it.

## How the models differ

All five models speak the same ZMod language — Moonraker transport and ZMod's
`START_PRINT ... MESH=` / `END_PRINT` start code from their Orca templates — and
each ships its own Orca machine/process/filament templates under
`print_handy/templates/orca/`, derived from OrcaSlicer's upstream Flashforge
profiles with the same adjustments the AD5X templates carry (ZMod start/end
G-code, retraction tuning, corner-origin printable area).

ZMod documents **no Klipper 13 support on the Creator models** — run them on the
stock-Klipper ZMod configuration, not the Klipper 13 one.

The filament system is where they part:

- **AD5X** is the full ZMod path: 4 IFS slots, slot questions during intake, Spoolman
  slot matching, the native-screen and stock `/detail` machinery, and the
  `PRINT_HANDY` ZMod plugin macro that reports spools ZMod can switch to mid-print
  (see the project reference's "ZMod plugin and spool matching" section).
- **AD5M and AD5M Pro** are single-extruder printers. Their adapter shares the ZMod
  start code and Moonraker transport with the AD5X and nothing else: no IFS slot
  questions, no spool-switch claims, no `PRINT_HANDY` macro (it is AD5X-only), no
  native-screen sync and no stock `/detail` polling. A multi-colour job is refused,
  not approximated — slice it single-colour or use a multi-slot printer. The Pro's
  enclosure, camera and air filtration do not by themselves change slicing
  geometry; its geometry is the AD5M's, and its thermal facts are recorded
  separately in the printer registry thermal record (see PRINTER-PROFILES.md).
- **Creator 5 and Creator 5 Pro** are toolhead machines: four independent
  toolheads (T0–T3, Klipper `extruder`…`extruder3`), each with its own filament,
  changed by the sliced file's bare `T` lines — a tool changer, not a feeder.
  Print Handy models them as **tools**, not IFS slots (`[printer] tool_count`,
  `filament_slots` stays 1): slicer tool *i* always prints with toolhead *i*, readiness
  and status are per toolhead, and Spoolman spools bind per toolhead — the
  toolhead is the identity unit, never an IFS slot. A job that asks for a tool remapped to another head, or a fifth
  tool, is refused rather than approximated. Multi-colour slicing uses the
  four-extruder Orca templates, and the Discord colour questions are per
  toolhead: for a source with two to four colours, intake asks one question
  per colour — which toolhead prints it — showing each toolhead's loaded
  spool, and a toolhead already assigned a different colour is not offered
  again. The recorded picks are the identity mapping (slicer tool *i* prints
  with toolhead *i*), so nothing is remapped. A source with more colours
  than toolheads is refused with that explanation; printing everything in
  one colour on one toolhead stays available. Multi-colour jobs print as one
  merged plate; a source that cannot merge onto one bed is refused rather
  than split into per-toolhead plates. The `PRINT_HANDY`
  plugin macro stays AD5X-only — ZMod's endless-spool feature exists on the
  Creator too, but the macro's slot model is the IFS one. The Pro adds the
  enclosed frame, active chamber heating (65 °C) and air filtration over the base
  Creator 5; the print geometry and tool model are identical, and its template
  records the chamber and filtration support.

## Print file format: `.gcode` or `.gcode.3mf` (#564)

What reaches the printer is Print Handy's own sliced output either way — a
downloaded print file is never sent as-is; everything is resliced locally first.
`[printer] zmod_delivery_format` chooses the container it travels in:

- **`auto` (the default)**: the sliced job is packaged as a `.gcode.3mf` when the
  printer reports **ZMod 1.7.5 or newer**, and sent as plain `.gcode` when the
  version is older or cannot be read.
- **`gcode`**: always plain `.gcode`, exactly as before this setting existed.
- **`3mf`**: always the package, even below the version floor (an operator
  override for printers whose ZMod was updated out of band).

The package wraps the finalized `.gcode` byte-for-byte (`Metadata/plate_1.gcode`
inside the zip, with the plate metadata and an MD5 sidecar). Its
`Metadata/slice_info.config` names each used filament with its slot id, type
and colour, plus the print time, weight and printer model, in the shape
OrcaSlicer's own sliced export uses: that file is where ZMod reads the colours
it maps to the IFS slots and the details Fluidd/Mainsail show. Every check Print
Handy runs — verification, colour mapping, mesh and readiness guards — still
runs on that `.gcode` before packaging, and the `.gcode` stays the artifact of
record for reprints, slot remaps and print records. The job and print records
note which format a print used.

Why 3MF at all: per ZMod's own documentation for 1.7.5+, a `.gcode.3mf` makes
colour scanning and mapping instant (the `.gcode` path's preprocessor step does
not run), `FORCE_MD5` "only applies when sending G-code" so no MD5 pass is
needed, and KAMP data is available instantly instead of after a scan. The floor
is 1.7.5 because that is the release whose changelog adds 3MF colour mapping;
below it, the same package would buy nothing and risk the older firmware
misreading it, so `auto` stays on `.gcode` there.

Two boundaries to know:

- **Slot remaps happen before packaging.** Print Handy's `/print-again`
  slot remap rewrites the `.gcode` (mapping metadata and checksum) and the
  result is then packaged like any other job. A file already on the printer
  can still be remapped from the printer's own screen — that is ZMod's
  domain. A color remap made on the printer after a `.gcode.3mf` starts is
  not written back to Spoolman. Spoolman keeps the mapping confirmed in
  chat, and the confirm card says so when this job will be sent as
  `.gcode.3mf`. A plain `.gcode` card does not get that line. `auto` with
  an unknown ZMod version stays on `.gcode`, so it does not either. With
  `auto`, the card reads the ZMod version the way delivery does, so ZMod
  1.7.5 or newer gets the line. A failed read counts as unknown.
  No recorded fixture shows Moonraker or Klipper reporting the slot map
  the job used after start. `zmod_color` is slot presence, and `SET_ZCOLOR`
  is the start command, not a read-back. This confirm sentence is the whole
  change. Deduction stays on the mapping confirmed in chat. A missing map
  is not invented and does not change the start.
- **Nothing here is live-hardware verified yet.** The format behaviour above
  is pinned by offline tests and ZMod's published documentation; the
  on-printer checks (starting a packaged print on 1.7.5, Fluidd/Mainsail
  listing metadata, the native screen's display of a packaged print, and
  timelapse markers inside one) are listed on issue #564 for the first
  operator who runs them.

## Bundled Spoolman from printer Moonraker (#443)

Print Handy's bundled Spoolman normally lives on loopback inside the
Print Handy container, which is enough for Print Handy itself. When
Moonraker runs on the printer hardware — the usual ZMod layout, for
example an AD5X at `http://192.168.x.x:7125` — Moonraker's own
`[spoolman]` component needs to reach that API over the LAN to sync
active spool IDs with the IFS slots. That access is opt-in:

1. In the Unraid template, keep the advanced **Spoolman port** mapping
   (host 7912 to container 8000) and set the advanced **Spoolman bind
   host** variable `SPOOLMAN_HOST=0.0.0.0`. With the default
   `127.0.0.1`, Spoolman stays loopback-only and unexposed. Under
   Compose instead, uncomment the `"7912:8000"` mapping and set
   `SPOOLMAN_HOST=0.0.0.0` in `.env` (see
   [Docker](DOCKER.md#included-spoolman)).
2. In the printer's Moonraker config, point `[spoolman]` at the
   Print Handy host:

   ```ini
   [spoolman]
   server: http://<unraid-ip>:7912
   ```

3. Check from any LAN machine: `GET http://<unraid-ip>:7912/api/v1/info`
   answers HTTP 200 when the bundled Spoolman is up.

The Spoolman API is unauthenticated — LAN only, never forward port 7912
from a router. Home Assistant REST sensors use the same base URL
(`http://<unraid-ip>:7912/api/v1/...`).

## Choosing a model

Set `[printer] model` to the key from the table (or `PRINT_HANDY__PRINTER__MODEL` in
Docker). The setup wizard and the Web UI picker list these models from the printer
registry, so no other configuration names them. Stock (non-ZMod) firmware is a
separate path — only the AD5X has one today (`flashforge_stock.ad5x`,
see [Stock Flashforge printers](FLASHFORGE-STOCK.md)).
