# ZMod printers

Flashforge printers running [ZMod](https://zmod.link) are Print Handy's home platform: Klipper and Moonraker on top of the stock firmware. ZMod supports five Flashforge models, and Print Handy has first-class profiles for all five. Find yours in the table.

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
(released 2026-10-27; the feature-by-feature audit is recorded in the project's issue tracker). The audit
found no change to the contracts Print Handy relies on for the .gcode files
it delivers. 1.7.5's headline features (3MF support, instant color
mapping without the `FORCE_MD5` preprocessor, fast KAMP data) apply to the
3MF path. Print Handy delivers that `.gcode.3mf` package by
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

## ZMod parameters evaluated: `ORCA_FF` and `orca_lanes`

Two parameters ZMod documented on 2026-11-03, after the 1.7.5 release
above, so neither is in any ZMod version Print Handy has audited, and
neither has a version floor Print Handy can read yet. Both need the
native screen off (`DISPLAY_OFF`). The evaluation below is the answer
to [](https://github.com/catduckgnaf/print-handy/issues/851):
reasons why and reasons why not for each, then a verdict, every claim
tied to the code or the wiki as its source. Print Handy never gates
printing on a ZMod version, and this evaluation does not change that.

### `orca_lanes`: color sync, verdict: accommodate (parser fix shipped), full adoption deferred

**What it does.** ZMod publishes the IFS slots to the Moonraker
database so OrcaSlicer's "Sync filament" button can pull filament
color and type from the printer, whether the printer was added in
OrcaSlicer as Octo/Klipper (Moonraker) or as FlashForge. It is on by
default; disable it with `[zmod_color] orca_lanes: False` in
`mod_data/user.cfg`. ZMod documents it on the AD5X page (the upstream
docs commit applied the same section to the Creator 5 Pro page too, but
the lane shape described is the 4-slot IFS one, so how it behaves on a
toolhead machine is an open question for the live check). If colors are
managed by HelixScreen (`helix = 1`), ZMod skips publishing regardless
of the setting, so its data is not overwritten.

Source: [ZMod wiki, AD5X "Filament sync with
OrcaSlicer"](https://wiki.zmod.link/AD5X/#filament-sync-with-orcaslicer);
upstream docs commits `4b07865`/`676c925`/`8c20949` (2026-11-03) in
`ghzserg/zmod`, which contain only documentation; the firmware code
ships in release payloads, not in that repo.

**Why accommodate it.**

- **A second author was about to land in a namespace Print Handy
  already reads, and the old parser misread it.** Print Handy's
  HelixScreen inbound spool sync watches the Moonraker
  `lane_data` namespace and converges slot assignment through
  `spool_sync.reconcile`. ZMod writes that namespace in the AFC /
  Happy Hare shape: slot 1 is key `lane0`, …, slot 4 is `lane3`, with
  records like `{"lane": "0", "material": "PETG", "color": "#161616",
  "name": "PETG"}`. The parser's convention is the screen's: outer key
  `laneN` is 1-based, inner `lane` is 0-based (pinned by
  `test_parse_lane_data_outer_key_is_1_based_inner_lane_is_0_based`).
  Read under the screen's keys, ZMod's slot 2 record (`lane1`) would
  be attributed to slot 1, its slot 1 record (`lane0`) would be
  dropped as slot 0, and every slot's data would land one slot off.
  A test with the exact wiki-documented payload failed against the old
  parser and passes with the fix.
- **The fix is small and loses nothing the watcher can act on.** A
  ZMod record names no spool, so the watcher, which never guesses a
  spool, could only have produced a wrong-slot "unresolved" prompt
  from it. `parse_lane_data` now skips records carrying no
  screen-side field (`spool_id`, `scan_time`/`scanned_at`/`timestamp`,
  or any `helix_*` key); every screen record in the suite, including
  the id-less screen lane, still parses exactly as before (4 new
  tests in `tests/hermes/test_lane_data_sync.py`). On a
  no-HelixScreen install the watcher becomes inert for ZMod-authored
  data instead of wrong.
- **HelixScreen installs are unchanged by construction.** ZMod skips
  publishing when `helix = 1`, so the maintainer's AD5X, and any
  HelixScreen install, sees no new author at all.

**Why not adopt it fully.**

- **Print Handy gains nothing functional from it.** The slicer's Sync
  button is a slicer-side win; it needs nothing from Print Handy to
  work, and Print Handy has no use for a color/type assignment it
  cannot tie to a Spoolman spool.
- **Assignment is not presence, and the watcher must not confuse
  them.** ZMod's entries carry assignment (color/type), not sensor
  presence; the wiki says filament presence from the sensors is not
  taken into account, and the external spool is not published. Print
  Handy's "physically loaded" calls still come from the other sources
  they come from today: `zmod_color` presence and the native
  `/detail` read in `print_handy/core/spool_lifecycle.py`, and the
  IFS sensors in the listener. Nothing in the watcher treats
  `lane_data` as presence, and this change keeps it that way.
- **A slot with type `?` is removed, and a removal must not read as
  an unload for ZMod-authored lanes.** With the parser fix, removing
  a ZMod key changes nothing parsed; it reads as no data, never as
  an unload, which honors the rule "absent data reads as
  unknown, never as empty" for that author.
- **The parser fix does not solve the deeper problem, and this PR
  does not claim it does.** ZMod's 0-based keys overlap Print
  Handy's and HelixScreen's 1-based keys at `lane1`–`lane3` with
  different slot meanings, so the writers overwrite and delete each
  other's records in the shared namespace (ZMod republishing slot 3
  overwrites Print Handy's slot 2 record; clearing slot 3's type
  deletes key `lane2`). A vanished spool-bearing record is what the
  watcher converges as an unload, so that collision can unload a
  spool that is still loaded. The watcher guards that
  ([](https://github.com/catduckgnaf/print-handy/issues/852)):
  once it has seen a key-colliding writer, a vanished spool-bearing
  record only converges to an unload when the printer's own slot
  presence (`zmod_color` screenless, native `/detail` otherwise) reads
  the slot empty. Presence loaded posts one "change not verified" note
  and changes nothing; unreadable presence changes nothing until a
  readable poll settles it. HelixScreen installs, where ZMod does not
  publish, behave as before.

### `ORCA_FF`: native-screen emulator, verdict: document-only

**What it does.** With `SAVE_ZMOD_DATA ORCA_FF=1` and a reboot, a
ZMod printer with the native screen off answers the FlashForge host
protocol: OrcaSlicer and Flash Studio connect with host type
`FlashForge`, hostname = printer IP, API key / password `zmod`,
serial `zmod`, and operators pick spools for colors right in the
slicer when sending a print instead of the Fluidd menu, while Flash
Studio gets discovery, file upload, status, and print control. AD5X /
Creator 5 / Creator 5 Pro only; default off; takes effect after a
reboot.

Source: [ZMod wiki, Global
`ORCA_FF`](https://wiki.zmod.link/Global/#orca_ff).

**Why it is worth documenting.**

- **It is a real operator win that needs nothing from Print Handy.**
  The slicer-side spool choice and Flash Studio control work entirely
  outside Print Handy; documenting it costs a section and buys
  operators the knowledge that the option exists and what it changes.
- **A print sent this way is not invisible to Print Handy.** The
  Moonraker listener already handles prints it did not start: an
  unclaimed start (printer screen, other tools) gets exactly one
  canonical start post, a capture session, light-on, the visual
  watchdog, runout classification, and the completion announcer
  (`print_handy/hermes/moonraker_event_listener.py`, `on_status`).
  An `ORCA_FF` print is, to the listener, a print started by another
  tool; it gets monitored and reported like a screen-started print.

**Why not incorporate it.**

- **It is a parallel send path that bypasses everything Print Handy
  exists to do.** A print sent from OrcaSlicer or Flash Studio never
  passes the confirm flow, the spool checks, the readiness guards, or
  Print Handy's print accounting. That is an operator choice, not a
  bug, but there is nothing for Print Handy to adopt here without
  becoming a second slicer-send path, and the confirm-before-printing
  posture is the product.
- **Pointing Print Handy's stock-Flashforge profile at an emulator is
  a misconfiguration with named early failures, not a supported
  route.** The stock profile is never auto-selected: it requires the
  operator to choose `flashforge_stock.ad5x` and to put
  `AD5X_SERIAL` / `AD5X_CHECK_CODE` in the environment
  (`print_handy/printers/flashforge_stock/adapter.py`, `client()`),
  and every refused request surfaces as a named `StockLanError`
  (`stock LAN /detail refused`, and so on). The emulator's
  documented credentials are `zmod`/`zmod`, so the common case fails
  at the credential/detail stage with a named error. A deliberately
  configured emulator could accept an upload whose start sequence is
  the slicer's stock-owned one rather than ZMod's; that deserves
  this warning, not a code guard for a configuration Print Handy
  never makes on its own.
- **Print Handy cannot detect it with any mechanism it has today.**
  Saved ZMod parameters live in `mod_data/variables.cfg` on the
  printer, and no code in the repo reads that file or runs
  `GET_ZMOD_DATA` (the console macro that reports saved parameters);
  the version reads Print Handy does have (Moonraker update status,
  `/machine/system_info`) say nothing about it. Building a detection
  path for a document-level concern is not worth it; the doctor
  says nothing about `ORCA_FF`, and this section is where an
  operator learns it exists.
- **Spool choices made in the slicer do not make the next Print
  Handy print stale beyond what already exists.** Those choices
  change the printer-side mapping (`zmod_color`), not Spoolman's
  `ph_slot` assignments; the next Print Handy intake reads loaded
  spools from Spoolman and the listener rebuilds the slot cache from
  `ph_slot` at session start (`reconcile_spools`) with a color
  backfill from `zmod_color` (`backfill_slot_colors`), and `/spool
  sync` reconciles on demand. That is exactly the exposure a
  Fluidd-menu change already has; no new stale-state hazard, and
  no new code to close it.

## How the models differ

All five models speak the same ZMod language, Moonraker transport and ZMod's
`START_PRINT ... MESH=` / `END_PRINT` start code from their Orca templates, and
each ships its own Orca machine/process/filament templates under
`print_handy/templates/orca/`, derived from OrcaSlicer's upstream Flashforge
profiles with the same adjustments the AD5X templates carry (ZMod start/end
G-code, retraction tuning, corner-origin printable area).

ZMod documents **no Klipper 13 support on the Creator models**, run them on the
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
  not approximated, slice it single-colour or use a multi-slot printer. The Pro's
  enclosure, camera and air filtration do not by themselves change slicing
  geometry; its geometry is the AD5M's, and its thermal facts are recorded
  separately in the printer registry thermal record (see PRINTER-PROFILES.md).
- **Creator 5 and Creator 5 Pro** are toolhead machines: four independent
  toolheads (T0–T3, Klipper `extruder`…`extruder3`), each with its own filament,
  changed by the sliced file's bare `T` lines; a tool changer, not a feeder.
  Print Handy models them as **tools**, not IFS slots (`[printer] tool_count`,
  `filament_slots` stays 1): slicer tool *i* always prints with toolhead *i*, readiness
  and status are per toolhead, and Spoolman spools bind per toolhead; the
  toolhead is the identity unit, never an IFS slot. A job that asks for a tool remapped to another head, or a fifth
  tool, is refused rather than approximated. Multi-colour slicing uses the
  four-extruder Orca templates, and the Discord colour questions are per
  toolhead: for a source with two to four colours, intake asks one question
  per colour, which toolhead prints it, showing each toolhead's loaded
  spool, and a toolhead already assigned a different colour is not offered
  again. The recorded picks are the identity mapping (slicer tool *i* prints
  with toolhead *i*), so nothing is remapped. A source with more colours
  than toolheads is refused with that explanation; printing everything in
  one colour on one toolhead stays available. Multi-colour jobs print as one
  merged plate; a source that cannot merge onto one bed is refused rather
  than split into per-toolhead plates. The `PRINT_HANDY`
  plugin macro stays AD5X-only, ZMod's endless-spool feature exists on the
  Creator too, but the macro's slot model is the IFS one. The Pro adds the
  enclosed frame, active chamber heating (65 °C) and air filtration over the base
  Creator 5; the print geometry and tool model are identical, and its template
  records the chamber and filtration support.

## Print file format: `.gcode` or `.gcode.3mf`

What reaches the printer is Print Handy's own sliced output either way; a
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
Handy runs, verification, colour mapping, mesh and readiness guards, still
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
  can still be remapped from the printer's own screen; that is ZMod's
  domain.
- **Nothing here is live-hardware verified yet.** The format behaviour above
  is pinned by offline tests and ZMod's published documentation; the
  on-printer checks (starting a packaged print on 1.7.5, Fluidd/Mainsail
  listing metadata, the native screen's display of a packaged print, and
  timelapse markers inside one) are listed on for the first
  operator who runs them.

## Printer clock after a reboot

A ZMod printer does not keep time across a power cycle. Observed on the
AD5X on 2026-11-02: about a minute after a reboot the clock read
31 Dec 2025, and Moonraker's HTTP `Date` header said the same. Two time
clients were running on the printer; the stock firmware's `ntpclient`
against `pool.ntp.org` and an `ntpd` against a rotating Russian pool
server (`ntpN.vniiftri.ru`), and the clock had not caught up a minute
in, with the firewall already allowing all outbound traffic. While the
clock is wrong, the printer's own TLS connections can fail (GitHub
certificates look "not yet valid", so Moonraker update checks fail) and
Moonraker history is stamped with the wrong dates.

What the ZMod documentation (audited against the ZMod repo and wiki,
2026-11-02) does and does not offer:

- `SET_TIMEZONE ZONE=<zone>` is the supported way to set the timezone
  (System macros; run it with no parameters to list zones). Its default
  zone is `Asia/Yekaterinburg`. The setup guide has the operator run it
  once during installation. The docs do not state a separate NTP-server
  setting, and none was found in the ZMod macros or docs: the NTP
  server is not a ZMod setting Print Handy can point at a different
  pool.
- `DATE_SET DT=YYYY.MM.DD-HH:MM:SS` sets the system date and time once.
  It does not make the clock persist: the printer has no battery-backed
  clock to persist to, so the next power cycle starts from Dec 2025
  again and waits on NTP.
- There is no documented ZMod boot hook Print Handy could use to set
  the clock at startup.

ZMod's own boot script (ghzserg/z_ad5x `.shell/root/start.sh`, read
2026-11-03) sets the clock to 2026-01-01 00:00 local time on every boot,
then tries a fixed NTP server list for about ten minutes and never
again. A printer with no internet, or whose outbound UDP 123 is not
answered in that window, keeps the boot date for its whole uptime.

So Print Handy is the printer's time source. The Moonraker listener
compares the printer's clock (the `Date` header Moonraker already sends)
with the Print Handy host's, and whenever it is five minutes or more
off and the printer is fully idle (standby, no file, SD card not active)
it runs `DATE_SET` with the host's time. This happens at connect, at
Klipper ready (so right after a printer reboot) and every 10 minutes
after that, so a printer that was busy at boot is set soon after it is
idle. It never touches a
printing or paused printer. `DATE_SET` reads its stamp in the printer's
own zone (ZMod's `SET_TIMEZONE`, default `Asia/Yekaterinburg`); when the
readback is off by a whole zone gap, Print Handy sets it once more
corrected by that gap and remembers the gap, so a printer left on the
default zone still gets the right absolute time. A failed set (no
`DATE_SET` macro, a refusal, a clock still off) waits 30 minutes before
the next try. `PRINT_HANDY_CLOCK_SYNC=0` turns setting off.

The printer needs no internet for any of this; it only needs the Print
Handy host's clock to be right (the host's own NTP, or the router's).
Launch reconciliation does not depend on the printer clock at all: a
held start settles from Moonraker job ids and the printer's current
file.

The watch-and-warn messages remain for a clock Print Handy could not
set. A clock five minutes or more off posts one "printer clock has not
synced yet" message, one reminder if it is still wrong after 30 minutes,
and one "printer clock synced" message when it catches up. The watch
never blocks a print and never fires for an unreadable clock: a printer
that does not answer is unknown, not wrong. Running
`SET_TIMEZONE ZONE=<your zone>` once in the printer console keeps the
printer's own local times (screen, logs) in your zone.

## Recording a live start

The replay tests' start transitions (heating, IFS load, a start that drops back
to standby) are code someone wrote, not recordings. To record the real
ones, turn on the start recorder for the checklist run:
`PRINT_HANDY_RECORD_STARTS=1` in the environment of the process that runs
`print-now` (Docker: a line in `.env`, or a container variable on Unraid, then
recreate the container). It is off by default; unset it after the run.

While it is on, every start writes a recording, and nothing about the start
changes. From immediately before the one `SET_ZCOLOR` POST (or
`/printer/print/start` on the generic route) a background reader `GET`s
`/printer/objects/query?print_stats&extruder&heater_bed&zmod_color&idle_timeout&virtual_sdcard&webhooks`
every 2 s, plus the gcode store, and appends each reply with a monotonic and a
wall timestamp. It also records the exact start command and its HTTP receipt,
gcode store lines logged after the pre-POST watermark, the delivery format
(`.gcode` or `.gcode.3mf`), the ZMod version, the lease phases and the outcome.
It only reads: it never posts, never runs G-code, and a recording failure never
reaches the start. `print-now` usually returns while the AD5X is still
preparing, so the recording is then continued by a small detached process until
the printer prints this file, abandons the start, reports error or cancelled, or
15 minutes pass.

Files: `<hermes_home>/state/start-recordings/<UTC stamp>-<file stem>.jsonl`
(Docker: `/data/state/state/start-recordings/`), and `<same>.summary.json`
with the outcome, the time of the first heater target and of `printing`, any
`!!` lines and the lease phases. The JSONL holds the printer's address and real
file names; it is a local record, not a fixture.

To verify, record one start that prints (`preparing`) and one IFS load that
aborts back to standby (`prep_aborted`), then export each:

```
python3 -m print_handy.core.start_recorder export <recording>.jsonl --fixture preparing --out tests/fixtures/printers/ad5x_zmod/
python3 -m print_handy.core.start_recorder export <recording>.jsonl --fixture prep_aborted --out tests/fixtures/printers/ad5x_zmod/
```

The export writes `objects_query.<fixture>.json`: every polled reply in the
recorded snapshot's own shape (`{"result": {"eventtime", "status"}}`) with its
seconds since the POST, under `frames`, and the date, ZMod version and outcome
under `recorded`. File names are replaced as the fixture README describes, and
the export refuses a recording whose outcome does not match the fixture name or
that still holds anything the scrub test forbids. It prints the README table row
to add. Only export real recordings: hand-written ones are not accepted.

## Bundled Spoolman from printer Moonraker

Print Handy's bundled Spoolman normally lives on loopback inside the
Print Handy container, which is enough for Print Handy itself. When
Moonraker runs on the printer hardware; the usual ZMod layout, for
example an AD5X at `http://192.168.x.x:7125`, Moonraker's own
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

The Spoolman API is unauthenticated, LAN only, never forward port 7912
from a router. Home Assistant REST sensors use the same base URL
(`http://<unraid-ip>:7912/api/v1/...`).

### Fluidd says Spoolman is blocking its origin

If Fluidd or Mainsail shows "Spoolman is blocking Fluidd's browser origin", the browser is loading the printer UI from an origin that the bundled Spoolman does not allow. Set `SPOOLMAN_CORS_ORIGIN` on the Print Handy container to the exact origin you open in the browser, including scheme and port. Origins match exactly, so a bare `<printer-ip>:80` never matches. For a printer UI at `http://<printer-ip>`:

```
SPOOLMAN_CORS_ORIGIN=http://<printer-ip>,http://<printer-ip>:80
```

Moonraker's `[spoolman]` connection is server-to-server and does not need this variable. An external Spoolman container needs the same variable on its own container. Do not use `*`. See [Docker](DOCKER.md#fluidd-says-spoolman-is-blocking-its-origin).

## Choosing a model

Set `[printer] model` to the key from the table (or `PRINT_HANDY__PRINTER__MODEL` in
Docker). The setup wizard and the Web UI picker list these models from the printer
registry, so no other configuration names them. Stock (non-ZMod) firmware is a
separate path; only the AD5X has one today (`flashforge_stock.ad5x`,
see [Stock Flashforge printers](FLASHFORGE-STOCK.md)).
