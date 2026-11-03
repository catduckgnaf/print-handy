# Detailed project reference

[Documentation home](README.md) · [Project overview](../README.md)

This page preserves the detailed project reference. Start with the setup guides for installation; features and defaults vary by printer and configuration.

[![CI](https://github.com/catduckgnaf/print-handy/actions/workflows/ci.yml/badge.svg)](https://github.com/catduckgnaf/print-handy/actions/workflows/ci.yml)

[Contributing](../CONTRIBUTING.md) · [User documentation](../docs/README.md)

Print Handy is an automated 3D printing pipeline driven from a Discord channel, for Sovol and other Klipper printers, and Flashforge printers running ZMod. Post a MakerWorld, Printables or Thingiverse link, a direct link to a 3MF or STL file, or a 3MF or STL file, and the bot downloads the model, merges multi plate parts, matches colors to loaded spools in Spoolman, and slices headless with OrcaSlicer. Once someone confirms the bed is clear, it sends the job to the printer through Moonraker, watches it through the camera, and posts the result.

## About

Print Handy is almost entirely Python. Downloading, plate merging, color matching, slicing, job start, printer monitoring, spool sync, timelapses, and print records are all plain scripts with fixed rules, so they are fast and make no LLM calls.

The Discord side runs one of two ways. As a **standalone bot** (`python3 -m print_handy.bot`), Print Handy talks to Discord itself with a bot token: links, files, buttons, slash commands, slicing, print start and printer alerts need no Hermes and no LLM; an OpenAI-compatible model can optionally word replies to free text. See [Standalone bot](../docs/STANDALONE.md). Or the conversation runs on [Hermes Agent](https://github.com/NousResearch/hermes-agent) using Luna at medium reasoning, with the same handlers loaded as a Hermes plugin. Two optional extras also call a model: the camera watchdog asks a vision model to classify a print frame, and the "print started" post adds a one line roast. Leave `services.vision_chat_url` empty and the roast never runs; the watchdog then runs only if `PRINT_VISION_URL` is set or the built-in local failure detector is enabled (`vision.local_detector_enabled`, #455 — see [SETTINGS.md](SETTINGS.md#local-failure-detector)). The roast is its own switch, `vision.roast_enabled`, **on by default whenever an LLM/vision endpoint is available**, and it applies on every platform — the Telegram and Matrix bots are pure bots for chat (no `[llm]` fallback), but their print-started posts carry the roast just like Discord's (completion posts never do). Set `vision.roast_enabled` to `false` to turn roasts off while keeping camera checks.

Supported printers:
* **Sovol** on stock Klipper, where Print Handy started, plus any other Klipper printer with Moonraker. Single extruder.
* **Flashforge (running ZMod)**, including IFS multi color slots and the ZMod plugin below.

Pick one with `[printer] model` (see Settings). The offline core in `print_handy/core/` runs with no printer.

## How it works

In the `#3d-printer` channel, someone posts a link. The bot replies with a job summary and a button:

> **Mrs catduck:** https://makerworld.com/en/models/931302-holder-for-post-it-notes
>
> **Print Handy:** Model: Post it Note Holder with Stencils. Filament: Matte Black PLA (~48g). Estimated print time: 1 hour 14 minutes. Target printer: Flashforge (ZMod).
>
> [Slice and print now (bed is clear)]
>
> **Print Handy:** Bed heating started. Job sent to Moonraker. Finish time estimated for 4:22 PM ET. Don't touch the build plate.

The print starts only when someone clicks the button. On completion, the bot posts stats and writes a JSON print record for the job.

A bed-clear approval starts one print. Starting the same G-code again needs a new confirmation (`print-now --reprint`). Only one start runs per printer at a time. If Print Handy cannot tell whether a start reached the printer (it stopped during the request, or the printer never confirmed it), it refuses later starts on that printer until the printer reports that exact file. An operator can also check the printer and run `python3 -m print_handy.core.model_job reconcile-launch --started` or `--not-started`; without a flag, the command only shows the pending start. Print Handy never resends that start on its own. This launch state is kept in `<hermes_home>/state/print_handy_launches.sqlite3`.

## Features

**Intake and slicing**
* MakerWorld, Printables and Thingiverse links, direct links to 3MF and STL files (see [Direct file links](#direct-file-links)), or uploaded 3MF and STL files. MakerWorld links need an [account token](#makerworld-account-token); Thingiverse links need an [App Token](#thingiverse-app-token); Printables links and uploads do not. MyMiniFactory links need an [API key](#myminifactory-api-key) and are inventory-only: the bot lists the model's files and then stops with the download gap named — it cannot download from MyMiniFactory yet. Cults3D links need an [API key](#cults3d-api-key) and are metadata-only by the API's own design: the bot confirms the listing through the API and then stops with the gap named — Cults' API never exposes a listing's files.
* Models published as several files print together. On Printables, Thingiverse, MyMiniFactory, Creality Cloud, Thangs, Nexprint and direct links, the files of a model are treated as parts of one model, not as alternatives: intake offers to print all of them as one job, one of each. Files that are versions of one part (names that differ only by a version word, such as `cap-sign` / `cap-nosign`, `with_logo` / `no_logo`, `v1` / `v2`, `easy` / `hard`, `supported` / `presupported`, or a size such as `M3` / `M4` or `100mm` / `150mm`) get one short question per part ("Which cap?"); parts that share the same choice of sizes are asked once. `left` / `right`, `top` / `bottom` and numbered parts are never treated as versions. **Choose files** picks any subset by typed numbers (`1 3`, `2-4`, `all`) or a single file by button. When a model has exactly one 3MF beside its STL files, the 3MF is offered first because it usually holds the whole model already laid out. Each chosen file is downloaded once (a file already downloaded for an earlier job is reused) and the files are combined into one geometry-only 3MF with one object per file, packed onto the bed when they fit together and otherwise set in a row for the fit step to rearrange or split; per-part colors, rearrangement and splitting then work as for any multi-object 3MF. A combined 3MF part keeps its geometry only, not its painted colors. A name that suggests a count (`latch_x2`) is mentioned, but one of each file is printed. `JOB.json` records every file in `selected_sources` (each with its own `downloaded_file`), while `selected_source` and `downloaded_file` name the combined project. MakerWorld print profiles are unchanged: they are real alternatives, and intake still asks for one.
* Print time and grams from the model page in the first reply.
* Multi plate models merged onto one bed when they fit; plates that would run longer than `layout.max_merged_hours` stay separate. Merging first tries the established rectangle packing; when that fails and at least one part is genuinely non-rectangular, the merge planner retries with the parts' actual convex hulls (translation only, at their authored orientation, with the same spacing measured hull-to-hull instead of rectangle-to-rectangle), so multi-plate sources that split only because of rectangular over-estimation — two diagonal or triangular parts, for example — can still merge onto one plate. Under that hull fallback rectangle-only sources keep exactly their previous merge verdicts and placements, and a hull placement that cannot be found within a bounded search falls back to the previous split outcome. When rectangle and hull packing both fail, the merge planner makes one more attempt with per-object yaw: a part that fits the usable bed only when turned in the bed plane is packed by its hull rotated to the same recorded yaw the full-size split evaluation chooses for such a part (recorded as `yaw_by_object` in the merge plan), while parts that fit as authored keep their authored orientation, so a source that split only because one part needed a turn can now merge. Slicing realizes each recorded turn through the exporters' per-object fit channels, composed with the plan's recorded shifts (a turned part's shift is measured from its rotated bounds), and proves the merged output landed at each turned part's rotated bounds plus its shift before accepting it; a merge plan that records a turn for an unknown object, a non-finite or out-of-range angle, or a turn without the shifts that realize it is refused rather than sliced as authored. A part that fits the bed at no checked yaw still blocks the merge, eligibility matches the hull fallback (single-build-item parts only), and every merge verdict that succeeds without a turn is unchanged.
* Oversized models split across plates, tracked as one plan.
* Full-size one-plate rearrangement comes first and is applied without a question. When the author's layout does not fit the bed, intake first asks `model_job.full_size_one_plate` whether every part fits one plate at 100% when moved or turned 90° in the bed plane — before any Orca option search. The check uses the planner the slice path uses, so an arrangement it reports is one slicing honors: per-part color jobs go through the merge planner (`plate_merge.plan` with the job's `full_size_rearrange_plan`), which keeps `layout.merge_spacing_mm`, `layout.merge_edge_margin_mm` and, for more than one slot, room for the prime tower, and realizes turns through the same `yaw_by_object` + shifts channel as other merges; single-color single-plate jobs go through `geometry_layout.arrange_and_export` at its own margins. When it finds one, intake records the plan, says in one line that the parts were rearranged (or rotated and rearranged) onto one plate at 100% because the author's layout does not fit, and skips the rotation, shrink and split searches. It does not apply when the operator already fixed a scale or orientation for the job, or for an inch-confirmed STL. Otherwise the fit question is asked as before, worded to distinguish "as the author laid it out" from "rearranged"; a per-part job is offered a one-plate rearrangement only when the merge planner proved it. A rearranged per-part job that still splits by slot at slice time packs each slot plate at 100% instead of keeping the author's overflowing layout. Every gap and margin these checks use (`layout.arrange_*`, `layout.merge_*`, `prime_tower.part_clearance_mm`, `prime_tower.edge_mm`, `prime_tower.merge_slack_mm`) is the configured value resolved from the `layout.spacing` preset (default `minimum`, measured with the real Orca CLI), and slicing uses the same values, so an offer is one the slice path can produce; a job's brim width is added to the arrange edge margin. While the fit checks run, one status message names what is being tried and how long the check has taken; it is edited in place on adapters that support message edits (Discord, Matrix, Telegram) and otherwise re-posted only on a phase change or at most once a minute, and never with identical text.
* Full-size fit is evaluated per part before any shrink is offered: each authored object is checked at 100% against the usable bed and the printer's height limit, including dimension-preserving in-plane yaw. The footprint judged at each yaw is the part's actual mesh, not its bounding rectangle: the envelope is computed from the rotated mesh vertices (the true in-plane mesh extrema), so a non-rectangular part is credited with the size it really occupies when turned; for a rectangular part the result is identical to the rectangle check. When the selected printer model declares a printable-area exclusion (for example the Bambu Lab P1S front-left exclusion), a part counts as fitting only when a placement of its footprint exists inside the usable area that avoids the exclusion, and that placement is reported; in a full-size split plan such a part gets its own plate with the exact placement recorded (`placement_by_object`), intake names the placed part and its position, and slicing lands that plate at the recorded placement — a plan that records a placement for a shared plate, an unknown object or an out-of-range position is refused rather than sliced centered into the exclusion. A part that fits no plate on its own is named as blocking, with the measured limit it failed; separate plates and smaller inter-object spacing are never presented as a cure for such a part, and supports are discussed only as overhang help, never as a size fix. A part that fits only when turned in the bed plane has the exact turn recorded in the plan (`yaw_by_object`); intake names the turned part and its angle. It is offered its own plate, unless a shared placement proved over the hulls rotated to their recorded turns lets it share a plate instead — the shared placement is tried first, because it fits more parts per plate, and the solo turned plate remains the fallback. Slicing applies a solo recorded turn to that plate alone; on a shared plate it applies each member's turn per object, composed with the plate's recorded shifts, and re-reads the derived plate to prove every turned member landed at its rotated bounds plus its shift. A plan that records a turn for a shared plate without the shifts that realize it is refused rather than sliced as authored. Shared split plates are packed by bounding rectangles first; only when that cannot bin a set of parts together does the planner retry the set by their actual convex hulls (translation only, at authored orientation), recording the proved per-object translations as `shifts_by_plate` in the split plan. Intake names such parts as rearranged on their shared plate, and slicing applies exactly the recorded shifts — a plan whose shifts name an unknown object, do not name exactly their plate's objects, are malformed or non-finite, belong to a solo plate, or mix with a placement for the same plate is refused rather than sliced as authored, and the derived plate is re-read to prove it landed at the recorded shifts before it is sliced. A part that fits no plate as authored or at any bed-plane yaw is evaluated last for a face change: a 90° turn about X or Y that lands it flat on another authored face, composed with a bed turn of the rotated footprint — a cardinal turn (0° or 90°) when one fits, else the best diagonal turn from the same bounded 1° yaw sweep used for in-plane turns. When such a candidate is proved — flat landing per build item, rotated height within the limit, footprint inside the usable area including the once-only brim budget — a full-size split plan records the full rotation, its new up-axis, and the supports/brim measurement taken with the same surface heuristics as the single-plate rotation offers in `reorientation_by_object`; intake names the reoriented part, its face change and whether it needs supports (choosing the split is the approval for those supports). The part is offered its own plate, unless a shared placement proved over the members' footprints in their recorded orientations (authored, turned or reoriented hulls) lets it share a plate instead — the shared placement is tried first, after the authored packing and the shared-turn attempts, because it fits more parts per plate, and the solo reoriented plate remains the fallback. On a bed with a declared exclusion a candidate is proved only when a placement of the reoriented footprint also avoids the exclusion, and that placement is recorded for the part like any placed part's. Slicing applies exactly the recorded rotation: to the whole plate for a solo part (with a recorded placement for that same part composed into the same fit), or per object composed with the plate's recorded shifts on a shared plate, and re-reads the derived plate to prove every reoriented member landed at its rotated extents on the bed. The recorded supports decision reaches the slicer for that plate: on a solo plate it is the part's own decision; on a shared plate a member that needs supports turns them on for the plate, members with records govern when every member has one, and a part with no record of its own keeps the default. A plan that records a reorientation for an unknown object, mixes it with a turn for the same object, records it for a shared plate without the shifts that realize it, or carries a matrix that is not exactly the recorded face change composed with its bed turn is refused rather than sliced as authored. The recorded brim recommendation is advisory only and never changes the job's brim policy. Reorientation remains conservative: no offer when the supports measurement cannot be made, and arbitrary tilts, upside-down flips and poses that do not land flat are never offered. Among proved candidates for a part, cardinal-turn candidates rank ahead of diagonal-turn ones — a diagonal turn only ever adds a fit, it never replaces a verdict a cardinal plan already achieved — and within each class the choice is ranked by fit quality — most bed margin, then no-supports over supports, then larger measured bed contact — so the recorded plan is always the best proved fit, never merely the first candidate found. On a bed with a declared exclusion, a part that would otherwise get a solo recorded placement may instead share such a hull-packed plate when the proved shared placement also avoids the exclusion; the shared placement then supersedes the solo one (a plate never carries both), and without a proved shared placement the solo placement behaviour is unchanged. Two clearances are kept distinct and each is applied once: `layout.merge_edge_margin_mm` is bed-edge clearance (usable area = bed − 2 × margin per axis) and `layout.merge_spacing_mm` is the gap between objects sharing a plate, which changes neither a part's footprint nor the printable area. When the job carries a brim policy, the fit offer also accounts for the brim: the brim/feature budget (`orca_adapter.required_feature_edge` — the explicit brim width plus the resolved travel sweep) shrinks the usable area exactly once in the per-part fit check and in full-size split planning, placements keep the brimmed footprint clear of any bed exclusion, and a part that fits only without its brim is named as blocking with the brim budget as the reason instead of being offered a split plan it cannot use; with no brim policy, fit verdicts are unchanged. The other generated features (skirt, supports, prime tower and purge structures) are slice-dependent and remain accounted for by the slicer capacity checks and the G-code out-of-bounds guard, so a geometry fit plan is an offer to slice, not a print-ready assurance.
* Copy counts offered only where the G-code will actually contain the copies.
* Each slice records a versioned recipe with its G-code: source file, printer model, orientation and scale, copies, supports and brim, split assignments, filament and nozzle, Orca profile files and slicer command. A slice made for one recipe is never reused for another.
* Headless OrcaSlicer with versioned machine, process, and filament templates; Bambu Studio as a fallback slicer with its network cut.
* Readable part names: generic CAD names like "Cylinder" become a shape, size, plate, and color description.

**Filament and spools**
* Colors matched to loaded IFS slots from Spoolman. PLA+ is accepted in a PLA slot.
* Filament estimates split across parts that share a spool, never multiplied.
* `/spool` lists, changes, creates, and syncs spools without moving the printer. Bare or incomplete
  forms open a guided button flow (#475): known Spoolman filament types are offered first (their
  colour/hex come from the database; hex is never asked), the very first question already previews
  the spool number the new spool will be (#512 — write it on the spool before loading), and changing
  a slot asks the old spool's fate (shelf / archive / depleted / delete, with Archive as depleted
  recommended when the old spool has 0 g left). The brand question is skipped unless
  `spool.prompt_brand` is on. Anchored plain phrases ("add new filament", "swap the filament", …)
  start the same flow with no LLM involved; the complete typed forms stay one-shot.
* `/print-spools` (#744) is the loaded-only, read-only view: per loaded location, spool number,
  brand, material, color, grams left and percent left where the spool's starting weight is
  recorded (percent named unknown, never estimated). Reached by `/print-spools`, `/spool loaded`,
  a bare `/print-colors` (the `<JOB.json>` form is unchanged), and anchored plain asks ("what
  colors are loaded", "current spools", "how much filament is left") with no LLM involved, on
  every platform. The fuzzy extension scores near misses of those asks against rival spool
  intents with the shared scorer: only a clear, uncontested match earns a did-you-mean prompt,
  and the view shows only on a yes. The scorer's canonical fold also equates the color
  spellings (colour/color, grey/gray), so a "Silk Grey" spool matches a "gray" ask everywhere <!-- us-spelling: allow colour -->
  fuzzy matching runs. A slot with
  filament recorded but no spool bound is named as unrecorded rather than dropped, and a
  Spoolman outage costs only the amounts (named unavailable), per the #387 fail-soft posture.
* The Discord insertion prompt (a spool changed on the printer's own screen) and the paused
  spool question answer "New spool" with the same conversational walkthrough (#512) instead of
  the old five-field text modal: material quick-select (PLA, Silk PLA, PLA+, PETG, TPU, ABS,
  Other), colour/finish picker (known colours for the material first), starting-weight buttons
  (1000 / 500 / 250 g or custom), the replaced spool's fate when the slot is occupied, and one
  confirmation — nothing is created before it. When any label-reading route is available (#514),
  the first step also offers **Use a photo of the label**: the operator sends one photo of the
  spool's box or sticker as the next message, and the label is read by the first route that can —
  its own barcode/QR code (zxing-cpp, local), the shared vision call (when an endpoint is
  configured), or local OCR (tesseract, matched against the known filament vocabulary) —
  extracting brand, material/sub-type, colour/blend, net weight and any printed label
  temperatures. The values pre-fill the walkthrough at the same confirmation, which names the
  route that read them (Edit details walks the normal steps; an unusable photo refuses with the
  reason and falls back to them). Label temperatures are recorded on the Spoolman filament, so
  the usual precedence holds for every material, and the photo itself is never stored.
* Every spool records a color code, and nobody types hex. The code is derived from the color
  name and finish against a named-color table ("matte white" → `FFFFFF`; a blend such as
  "rose red blue green" records its first color as the swatch plus every color as Spoolman
  `multi_color_hexes`); a picked swatch or label read wins over the name. Only when the name
  cannot be read ("Galaxy") does the walkthrough ask once, with swatch buttons of likely colors.
  Picking a spool for a slot (insertion prompt, walkthrough, `/spool`) confirms that slot's color.
  The color code only sets the preview color, the 3MF color labels and the screen swatch —
  never a toolpath — so slicing never dies on a missing one: it uses the slot record's hex, then
  Spoolman's `color_hex`, then the printer's reported slot hex (`zmod_color`), then the name, and
  only then neutral gray `#9E9E9E`, which chat says out loud before slicing. Existing slots with a
  blank hex are backfilled without user action — on read, and by the listener at startup and on
  each reconnect from Spoolman and the printer — and a valid hex is never overwritten (only a code
  Print Handy guessed itself may be upgraded from Spoolman or the printer). The backfill takes the
  printer's own reported hex ahead of Spoolman's, because mapped launch refuses a slot whose hex
  differs from the printer's (`live RGB mapping mismatch`); it sends nothing to the printer, and a
  slot filled from another source is logged as needing `/spool sync` (the screen sync, which
  defers while printing). Binding through `/spool` sends the derived code to the screen; the IFS
  insertion prompt stays inventory-only. The ZMod screen has no type for every material, so the
  screen shows the nearest one it has — ASA, ASA-CF and ABS-CF show ABS; PA, PC, PCTG, nylon and
  unknown names show `?` — and the bind and `/spool sync` replies say so (for example "screen shows
  ABS for ASA"). A mapped launch accepts exactly that stand-in label for that material.
* A slice refused for a deterministic reason says why in plain words (for example "Slot 1 (Matte
  white PLA) was never confirmed as loaded … Is Matte white PLA loaded in slot 1?") with
  `Reference: PH_SLICING_CONFIGURATION`, and offers the one safe fix as a button ("Yes, slot 1 has
  it: slice") that records the confirmation and slices again. A refusal with no safe fix (no spool
  recorded, a material that differs from the chosen spool) explains itself and offers no blind retry.
* `/print-colors <JOB.json>` opens the color selection flow for an archived job. Preparation only: it slices nothing, uploads nothing and starts nothing, and the path must be an archived `JOB.json`.
* A swapped spool can finish a job sliced for the old one, by explicit choice.
* `PRINT_HANDY` Klipper macro confirms ZMod can fall back to a matching backup slot.

**Printing**
* Nothing starts without a bed clear button press.
* Multi plate plans: `/print-next` offers the exact next plate, and finishing a plate advances the plan.
* `/print-again` reprints an archived job exactly, with no re-slice. A job sliced before slice recipes were recorded is refused until it is re-sliced once. A misspelled name never reprints anything: near matches come back as a numbered "Did you mean one of these?" list to pick from (#468), and a print number is always exact.
* `/print-stop` and `/print-resume` each send at most one Moonraker command behind a confirm button.
* `/print-reboot` uses the safe ZMod reboot flow.
* Printer models: `flashforge.ad5x`, `flashforge.ad5m`, `flashforge.ad5m_pro`, `flashforge.creator5`, `flashforge.creator5_pro`, `sovol.sv06`, `klipper.generic`, `octoprint.marlin`, `bambulab.a1`, `bambulab.p1s` (offline only, see [Bambu Lab printers](BAMBU-LAB.md)). Each job records the model it was sliced for and refuses another.

**Monitoring**
* Event driven Moonraker listener: start, pause, error, and completion posts with stats.
* Camera watchdog checks layer 2, layer 5, and periodic frames for empty bed, spaghetti, a detached part, or a stalled print. Stall sensitivity is adjustable from 1 to 5.
* Stop and pause alerts carry Resume and Cancel buttons.
* Klipper report posted when a print is cancelled.
* Timelapse per print, or one combined video for a multi plate plan, sized to fit Discord.
* Print event markers (#317): the listener records timestamped markers — start, camera-watchdog alert, pause, resume, cancel, error, and completion with its outcome (`success` / `failed` / `cancelled`) — to `<state_dir>/print_markers/<job>.json`, one file per print keyed by G-code filename, written atomically like the plan timelapse stores. Each marker carries the wall-clock time and, when known, the print-elapsed offset; `print_handy/hermes/print_markers.py` also provides the duration-proportional mapping from an elapsed offset into a rendered timelapse's time, which the future blooper-reel builder will use. Recording is fail-safe — a storage error is logged and skipped, never breaking the listener or a post. Developers adding a new listener domain (#299) should feed its normalised events into `print_markers.record()` so every domain's prints get the same record.
* Optional vertical short (#317): with `timelapse.vertical_short` on, the completion post also carries a 9:16 (720x1280) version of the timelapse from `print_handy/hermes/vertical_short.py` — blurred-background pad framing by default or a centre crop, an optional burned-in overlay (print title, printer, filament, elapsed time, outcome), and a one-second hold on the final frame. It is an extra attachment, never a replacement: it is clamped to the configured platform's upload limit, and any failure to build it is logged and skipped so the completion post always goes out with the timelapse itself.

Live camera streaming (#311) starts with a delivery-agnostic core in `print_handy/live_stream.py`: it resolves one normalised camera source from Moonraker's webcams API — the same Moonraker path whether the household runs Fluidd, Mainsail or neither, since both are frontends over Moonraker's webcam registry — with a stream URL derived from the existing snapshot URL as fallback, from OctoPrint's webcam settings, or as a snapshot-only source, and it plans a codec-aware ffmpeg encode (H.264: VAAPI, NVENC, QSV, then CPU `libx264`; VP8: VAAPI, then CPU `libvpx`; NVENC never serves VP8) from an injected hardware probe. Stage 3 adds the Discord surface: the `[live_stream]` settings, the `/print-live` command and its viewing-session state machine (same module), and the Activity core in `print_handy/live_activity.py` — because Discord's bot API cannot send video, the live view is an Embedded App (Activity) in the voice channel whose page plays the go2rtc MSE-over-WebSocket feed behind a server-side OAuth allowlist check. Stage 4 adds the deployment wiring: the supervised Activity server (`python3 -m print_handy.live_activity`, gated by `print_handy.live_gate` and mirrored in the container healthcheck), the Activity server's own authenticated proxy of the feed to go2rtc on loopback (one public origin, one Portal URL mapping), go2rtc starting for live viewing as well as timelapse capture under the same one-upstream rule, the auto-start-on-print trigger and empty-timeout enforcement over the shared session record, the Web UI catalog's Live camera group, and the Compose/Unraid surfaces (live settings, the two live secrets, the Discord application ID, and the profile-gated `cloudflared` sidecar for the named tunnel). The operator guide is [Discord bot setup §7](DISCORD-BOT-SETUP.md#7-live-camera-activity-optional-311). The Stage 0 live Discord spike has not run, so the feature is offline-verified only — nothing about it is claimed live-verified.

**Records and upkeep**
* Every finished job written as a JSON print record and to the job archive.
* Nightly catch up against Moonraker job history so nothing is missed.
* `/print-maintenance` guided checklist and reminders by print hours and days.
* `/print-coldpull` runs an operator confirmed cold pull at configured temperatures.
* `/nozzle change` (#513) walks a physical nozzle change: pick the diameter (0.2/0.4/0.6/0.8 mm) and material (brass, hardened steel, stainless steel, tungsten carbide, diamond), confirm heating in person, and — on Moonraker/Klipper printers only — it heats to 250 °C for the hot loosen, waits for the *Old Nozzle Removed & New Nozzle Threaded* and hot-torque confirmations, turns the heater off and waits for a live readback below 50 °C. Other connections get manual-heat instructions and the same step confirmations; nothing is sent to them. Completing the walk records the new nozzle in `printer_hardware.json` with the previous calibration marked historical only, then offers Z-offset (`--sync-z`), bed mesh leveling and a pressure advance / flow check. Cancelling after heating turns the heater off and records nothing.
* `/print-source` lists every model site and whether it is ready: connected (and from the token file or a variable), not connected, login expired, listing only, or not supported. It checks the saved credentials only, never contacts a site, and never shows a value. See [Connecting a login-token site from chat](#connecting-a-login-token-site-from-chat).
* `/print-lookup` searches the root [knowledge, firmware and printer registries](TROUBLESHOOTING.md) for commands, macros, errors, symptoms and printer facts. Read-only; it sends nothing. It tolerates typos ("adheison", "nozzel clog") locally, with no model and no network. An operator can add an opt-in semantic layer for questions phrased by meaning ("bed won't stick"): set `PRINT_FUZZY_URL` to an endpoint such as the [Cloudflare Worker](CLOUDFLARE.md); it is asked only when nothing matches locally, and only the query and public registry entries are sent ([contract](SETTINGS.md#semantic-lookup-endpoint-print_fuzzy_url)).
* Fuzzy matching everywhere suggests and never acts (#468). A mistyped command such as `/spol list` gets a "Did you mean `/spool list`?" with Yes and No buttons (60 seconds), and Yes runs the corrected command through its usual confirmations. Plain chat and ties never trigger a suggestion. All of this works with no LLM configured.
* `/print-macros` lists the macros the configured Moonraker printer actually defines, read live from `/printer/objects/list` and classified against the registry (see [printer macros](MACROS.md)). The listing is read-only. `/print-macros run <name> [KEY=VALUE ...]` runs one live-discovered macro behind a confirmation that shows the exact script line, refusing internal (`_`-prefixed) names, unknown names, and any run while a print is running or paused. Non-Moonraker printers get a plain explanation.
* `/print-firmware` reports the configured printer's firmware update state (see [firmware updates](FIRMWARE-UPDATE.md)). On Moonraker printers the report is read live from `/machine/update/status`; on ZMod printers it adds the OS↔`zmod` pairing check and Klipper 13 MCU currency when the data exposes them. `/print-firmware update <component>` applies one live-listed component behind a confirmation showing the exact version change — ZMod and vanilla Klipper profiles only, vendor Klipper forks and non-Moonraker printers refused by profile — refusing while a print is running or paused, and verifies the result by re-reading the status. `/print-firmware guide zmod` and `/print-firmware guide stock` walk through the manual AD5X base and stock firmware updates; every other family gets its manual update guidance.
* `/print-calibration` runs the configured printer's calibrations with the before/after accept pattern (see [printer calibration](CALIBRATION.md)): before values are shown first, the run is confirmed with its warnings and happens only while the printer is idle, the new values and the signed differences are presented, and nothing is saved until the operator accepts — ZMod and vanilla Klipper profiles only, and only for calibrations whose primitive live discovery finds on the printer. The menu covers the full bed mesh, bed screws, extruder/bed PID (raw `PID_CALIBRATE`, never ZMod's auto-saving PID_TUNE macros), input shaper and the report-only belt check; the Z offset is report + guide in every mode (the native screen owns its store), and `/print-calibration records` lists the per-printer calibration records.
* `/print-live [start | stop | status]` runs the live camera viewing session (Discord only; see [live camera Activity](DISCORD-BOT-SETUP.md#7-live-camera-activity-optional-311)). Start posts the Activity pointer for the one configured voice channel; the bot sends no video itself and records nothing. Telegram and Matrix have no live command.
* `/print-troubleshoot` walks a guided physical troubleshooting flow (first layer, clog, heat creep, bed level, ringing) one question at a time. Advice only; it never commands the printer.
* `/print-help` answers with what Print Handy can do and the commands it registers. Read-only.

## Settings

`config/home.yaml` holds the shared defaults and picks the printer. Printer-specific defaults (bed, height, nozzle, slots, margins, prime tower and macros) come from the selected model and can be overridden in YAML. Put machine-specific overrides in the gitignored `config/local.yaml`; YAML is preferred for new configuration. Existing `config/home.toml` and `config/local.toml` remain supported during migration. When both formats are present, YAML wins; TOML is used when the corresponding YAML file is absent. `PRINT_HANDY_CONFIG=/path/to/settings.yaml` selects one YAML file, and `.toml` paths remain supported. Environment variables (`PRINT_HANDY__SECTION__KEY` and the existing legacy names) override file settings.

Settings merge in this order: printer-model defaults, home configuration, the selected local or explicit configuration, then environment. A typo or invalid value stops startup and identifies the setting. Python 3.11 or newer is required. Changes require restarting the relevant process; settings are loaded once at process startup. See [YAML configuration and TOML migration](../docs/CONFIGURATION-YAML.md) for schema, precedence, safety limits and migration guidance.

```yaml
# config/local.yaml example
services:
  moonraker_url: "http://192.168.x.x:7125"
  spoolman_url: "http://192.168.x.x:7912"
visual_watch:
  enabled: false
  sensitivity: 2
```

### Printer models

One package per brand under `print_handy/printers/`, one file per printer inside it: `flashforge/ad5x.py`, `sovol/sv06.py`, `klipper/generic.py`. A model file holds the printer's defaults, its adapter and the name of its Orca template folder (`print_handy/templates/orca/<folder>`). Its user-adjustable keys are `printer.name`, `printer.firmware`, `printer.start_macro`, `printer.end_macro`, `printer.bed_mm`, `printer.max_print_height_mm`, `printer.nozzle_diameter_mm`, `printer.filament_slots`, `layout.spacing`, `layout.single_copy_edge_mm`, `layout.multi_copy_edge_mm`, `layout.arrange_gap_mm`, `layout.arrange_edge_mm`, `layout.merge_edge_margin_mm`, `layout.merge_spacing_mm`, `prime_tower.size_mm`, `prime_tower.part_clearance_mm`, `prime_tower.edge_mm`, `prime_tower.merge_slack_mm`, `cold_pull` profile names and values, and `orca.templates`. `config/home.yaml` picks one with `printer.model: flashforge.ad5x`; settings merge in this order, later wins: model defaults, home configuration, selected local or explicit configuration, environment.

Adding a printer is one new file in its brand package plus its templates; nothing in `core/` changes. The AD5X and ZMod code lives in `print_handy/printers/flashforge/zmod/`, and `base.py` lists what core still shares with it.

Upgrading from `[printer] family`: replace it in `config/local.toml` with `model = "flashforge.ad5x"` (or `sovol.sv06`, `klipper.generic`). A leftover `family` stops the import and names the key.

### Camera stall sensitivity

`visual_watch.enabled` independently enables camera monitoring; it defaults to `true`, preserving the historical behavior for existing TOML users. `visual_watch.sensitivity` sets how readily the camera watchdog posts a "stalled layers" alert, from 1 (quietest) to 5 (most alerts). Default sensitivity is 3.

| Level | Frame counts as unchanged if diff below | Stall alert after | Periodic camera check | Vision "ok" cancels a stall (Bambu monitor) |
|---|---|---|---|---|
| 1 | 1.5 | 8 layers | 30 min | yes |
| 2 | 2.0 | 5 layers | 20 min | yes |
| 3 | 3.0 | 3 layers | 15 min | no |
| 4 | 4.0 | 3 layers | 10 min | no |
| 5 | 5.0 | 2 layers | 7.5 min | no |

Empty bed, spaghetti, detached part, uncertain, and vision failure alerts fire at every level. On the Moonraker (Klipper) watchdog a vision "ok" or "safe" verdict cancels a stall at every level, and an "uncertain" verdict that says the camera feed is dark posts the once-per-print dark-camera notice instead of an alert.

### MakerWorld account token

MakerWorld links need a MakerWorld (Bambu Lab) account token. It is a file, not a setting or environment variable: Print Handy reads `~/.config/bambu/token` in the home directory of the account that runs it (in Docker, `/data/.config/bambu/token`; see [Docker](DOCKER.md#makerworld-account-token)) and sends it as an `Authorization: Bearer` header on the MakerWorld model lookup and on the file download. Printables links and uploaded files need no token.

To get the token, sign in at makerworld.com in a browser and copy the value of the site cookie named `token` from the browser's developer tools. Store it as the only content of the file, readable only by that account, and never commit or share it:

```bash
mkdir -p ~/.config/bambu && chmod 700 ~/.config/bambu
(umask 077 && cat > ~/.config/bambu/token)   # paste the cookie value, press Enter, then Ctrl-D
chmod 600 ~/.config/bambu/token
```

Without the file, MakerWorld intake stops before the model summary with a message naming the file; an empty file stops it too. Print Handy does not check the token when it reads it. Tokens expire: when MakerWorld downloads start failing, sign in again and replace the file. Observed by the reporter on 25 Oct 2026 ([#262](https://github.com/catduckgnaf/print-handy/issues/262)): with an invalid token, the MakerWorld model lookup still answered and the summary was posted, and the download was then refused with HTTP 403 ("Please log in to download models."). A wrong or expired token can therefore show up only at download time, after the model summary.

### Thingiverse App Token

Thingiverse links use Thingiverse's official REST API (`api.thingiverse.com`) and need a free Thingiverse App Token. It is a file, not a setting or environment variable: Print Handy reads `~/.config/thingiverse/token` in the home directory of the account that runs it (in Docker, `/data/.config/thingiverse/token`; see [Docker](DOCKER.md#makerworld-account-token) — the same bind-mounted `dot-config` directory holds both token files) and sends it as an `Authorization: Bearer` header when it looks up a thing, lists its files and downloads the chosen file. The token is read on each request and is never logged.

To get the token, sign in at thingiverse.com and create an app/token at <https://www.thingiverse.com/developers> under your Thingiverse account. Store the App Token as the only content of the file, readable only by that account, and never commit or share it:

```bash
mkdir -p ~/.config/thingiverse && chmod 700 ~/.config/thingiverse
(umask 077 && cat > ~/.config/thingiverse/token)   # paste the App Token, press Enter, then Ctrl-D
chmod 600 ~/.config/thingiverse/token
```

Without the file, Thingiverse intake stops before the model summary with a message naming the file and where to create the token; an empty file stops it too. Only `.stl` and `.3mf` files from a thing are selectable sources — a "download all" zip and other file types are not — and, as with the other sites, the chosen geometry is resliced locally; baked site G-code is never used. If Thingiverse answers with a 401/403 or a Cloudflare robot check (challenge), intake reports that plainly and stops; Print Handy never tries to solve or bypass a robot check. Open thingiverse.com in a browser and pass any check there, or check/replace the token, then send the link again.

Live status: verified end to end on 2026-10-29 with an operator App Token (thing 763622, the model lookup, its file list and a real STL download). Requests with the App Token passed Thingiverse's Cloudflare check, which answers anonymous requests with a challenge.

### MyMiniFactory API key

MyMiniFactory links use MyMiniFactory's official API v2 (`www.myminifactory.com/api/v2`) and need an API key from the operator's MyMiniFactory account. It is a file, not a setting or environment variable: Print Handy reads `~/.config/myminifactory/token` in the home directory of the account that runs it (in Docker, `/data/.config/myminifactory/token`; see [Docker](DOCKER.md#makerworld-account-token) — the same bind-mounted `dot-config` directory holds all the token files) and sends it as the `key` query parameter when it looks up an object. The key is read on each request and is never logged.

```bash
mkdir -p ~/.config/myminifactory && chmod 700 ~/.config/myminifactory
(umask 077 && cat > ~/.config/myminifactory/token)   # paste the API key, press Enter, then Ctrl-D
chmod 600 ~/.config/myminifactory/token
```

Without the key file, intake can also look a model up through the download sign-in below.

### MyMiniFactory sign-in

Downloading from MyMiniFactory uses its documented OAuth sign-in: [MyMiniFactory's API guidelines](https://www.myminifactory.com/pages/api-guidelines) allow in-app downloads only when the person downloading is signed in to their MyMiniFactory account, and an API key alone never gets a download link. The sign-in is saved as `~/.config/myminifactory/oauth_token` (Docker: `/data/.config/myminifactory/oauth_token`), or the `MYMINIFACTORY_OAUTH_TOKEN` variable when that file does not exist. When a download needs it, the bot's connect message walks through these steps:

1. **Register an app, once.** Sign in at <https://www.myminifactory.com>, open Account Settings, then **Developers**, and fill in the form: **Name** `Print Handy`; **Client key** any unique slug, for example `print_handy` (this is your client key); **Url** and **Redirect uri** any page you control, for example `https://github.com/catduckgnaf/print-handy`; **Email** as shown. Tick the robot check and Save. (Developers also shows an **API key** for the app; that key is what the [MyMiniFactory API key](#myminifactory-api-key) file above holds.)
2. **Sign in.** Open `https://auth.myminifactory.com/web/authorize?client_id=<client key>&redirect_uri=<redirect uri, URL-encoded>&response_type=token&state=print-handy` in the same browser and approve.
3. **Copy the address.** The browser lands on your redirect page with `#access_token=...&expires_in=604800...` in the address bar. The part after `#` never reaches that page's server.
4. **Save it** as the only content of the file. The whole address is fine; Print Handy picks the `access_token` out of it:

```bash
mkdir -p ~/.config/myminifactory && chmod 700 ~/.config/myminifactory
(umask 077 && cat > ~/.config/myminifactory/oauth_token)   # paste the address, press Enter, then Ctrl-D
```

The sign-in lasts 7 days (`expires_in=604800`). When it runs out, the bot offers the connect flow again and only steps 2 to 4 are needed. You can paste the whole address into the chat instead of saving the file; see [Connecting a login-token site from chat](#connecting-a-login-token-site-from-chat) for what that risks.

How a download works: the API's file lookup returns the file's download link for a signed-in user; that link answers with a redirect to MyMiniFactory's signed file storage (`dl4.myminifactory.com`), and Print Handy follows it **without** carrying the sign-in token to the storage host. MyMiniFactory's website sits behind Cloudflare; if a download ever answers with a Cloudflare robot check, intake stops and says so, with the browser workaround. Print Handy never tries to get past a robot check.

Live status: verified end to end on 2026-10-29 with an operator sign-in (model 111400, `flying-benchy.stl`): the lookup, the file list, the download link and the download itself, a valid STL byte-for-byte the size the listing reported.

### Cults3D API key

Cults3D links use Cults' documented GraphQL API (`cults3d.com/graphql`) and need an API key from the operator's own Cults account. It is a file, not a setting or environment variable: Print Handy reads `~/.config/cults3d/token` in the home directory of the account that runs it (in Docker, `/data/.config/cults3d/token`; see [Docker](DOCKER.md#makerworld-account-token) — the same bind-mounted `dot-config` directory holds all the token files). The file's only content is `nickname:key` — your Cults nickname (exactly as it appears on your profile, capitals included), a colon, then the API key — and Print Handy sends it as an HTTP Basic `Authorization` header when it looks up a creation. The key is read on each request and is never logged.

```bash
mkdir -p ~/.config/cults3d && chmod 700 ~/.config/cults3d
(umask 077 && cat > ~/.config/cults3d/token)   # type nickname:key, press Enter, then Ctrl-D
chmod 600 ~/.config/cults3d/token
```

Create the key at <https://cults3d.com/en/api/keys> while signed in to your Cults account.

**Cults3D support is metadata-only, by the API's own design.** [Cults' API page](https://cults3d.com/en/api) states the API gives access to a listing's details (photos, titles, descriptions, tags) but "will not give you access to the 3D files… The files remain hosted on Cults", downloaded only through the signed-in website. So with the key, pasting a Cults3D model link confirms the listing through the API — and then intake stops with that gap named: no operator credential can change it, because the API exposes no files to anyone. The workaround is to open the model page in a browser, download the file there, and upload or drop the file in the chat instead. Without the token file, Cults3D intake stops before any request with a message naming the file and where to create the key; a malformed file (no `nickname:key` pair) stops it too. If Cults answers with a 401, intake says the nickname or key is likely wrong; a 403 is reported as the site challenging automated access — Print Handy never tries to solve or bypass a site's checks.

Live status: verified on 2026-10-29 with an operator key. A Cults model link is looked up by its slug (`creation(slug:)`), and Print Handy reports the listing's title and license; an unknown link is refused as not found. The first build of this lookup, which had only been tested offline, never matched a real creation; that was found and fixed by this live check. Activation is exactly the token file above, no other setup.

### Creality Cloud login token

Creality Cloud links use the JSON API the Creality Cloud web app itself uses (`www.crealitycloud.com/api/cxy/...`). Looking up a model and listing its source files needs no login. Downloading a file goes through the API's `memberDownload` call, which needs the account login the web client keeps in two cookies: `model_user_id` (your numeric user id) and `model_token`. The API refuses the token without the user id ("Invalid Token"), so both are saved, together, as `model_user_id:model_token`. It is a file, not a setting or environment variable: Print Handy reads `~/.config/crealitycloud/token` in the home directory of the account that runs it (in Docker, `/data/.config/crealitycloud/token`; see [Docker](DOCKER.md#makerworld-account-token) — the same bind-mounted `dot-config` directory holds all the token files) and sends it as the `__CXY_TOKEN_` header on the download request. The token is read on each request and is never logged. Print Handy never stores a Creality Cloud password.

To get it, sign in at <https://www.crealitycloud.com> in a browser, open the browser's developer tools (Application, then Cookies, then `https://www.crealitycloud.com`), and copy the values of the `model_user_id` and `model_token` cookies. Store them as the only content of the file, as `model_user_id:model_token` (for example `1234567890:4f2a…`), readable only by that account, and never commit or share it:

```bash
mkdir -p ~/.config/crealitycloud && chmod 700 ~/.config/crealitycloud
(umask 077 && cat > ~/.config/crealitycloud/token)   # type model_user_id:model_token, press Enter, then Ctrl-D
chmod 600 ~/.config/crealitycloud/token
```

Without the file, a Creality Cloud link still produces the model summary and file list; the download then stops with a message naming the file. An empty file stops it too. Login tokens expire: when the site answers a download with "Invalid Token", sign in again and replace the file — intake names that as a token problem, not a generic failure. Only `.stl` and `.3mf` source files from a model's file list are selectable sources; the site's print profiles (printer-specific Creality Print 3MF projects) and pre-sliced G-code are never used — the chosen geometry is resliced locally, as with the other sites. A model whose file list has no source files (only print profiles) stops with that reason named.

Live status: verified end to end on 2026-10-29 with an operator login (acceptance model `bag-clip-30mm`): the model lookup and file list answered without a login, the download refused a missing or token-only login, and with `model_user_id:model_token` the chosen STL downloaded intact, byte-for-byte the size the listing reported.

### Connecting a login-token site from chat

Every model site that needs a credential connects the same way from chat: MakerWorld, Thingiverse and Cults3D with a token or key you create on the site, Creality Cloud, Thangs and Nexprint with the login token your signed-in browser holds, and MyMiniFactory with its OAuth sign-in ([MyMiniFactory sign-in](#myminifactory-sign-in)). Creality Cloud, Thangs and Nexprint download with the operator's own login token: a value the signed-in browser keeps in a cookie, saved on the Print Handy machine as `~/.config/<site>/token` (Docker: `/data/.config/<site>/token`, from the mounted `dot-config` folder). Print Handy cannot sign in for you. Each site's login page sits behind a robot check, and Print Handy never stores a password or solves a captcha, so the token always comes from a browser you signed in with yourself.

You do not have to set these up in advance. When a download needs a token that is missing, empty, expired or refused, the bot does not just fail. It posts **one message** that says which, with everything needed in it: why the token is needed, the numbered steps to get it from your own account or signed-in browser (the cookie to copy, or for MyMiniFactory the sign-in address), the two ways to hand it over, a plain note on what pasting risks, and a link to that site's section of this document. Under it are two buttons, **Saved, try again** and **Cancel**. You then do one of these:

1. **Paste the token as your next message.** Print Handy checks that it has the right shape for that site (for Creality Cloud: `model_user_id:model_token`, or a cookie string such as `model_user_id=123; model_token=abc` in either order; for Cults3D: `nickname:key`; for MyMiniFactory: the whole redirect address or just the token), strips quotes, whitespace, a leading `Bearer` and the surrounding cookie text, and writes it to the site's token file (`~/.config/<site>/token`; Docker: `/data/.config/<site>/token`) through a temporary file, so the file is never half written. The folder is created with mode 0700 and the file 0600. Then it retries the same download. A message that does not look like a token for that site (ordinary chat, a link or file path, the wrong cookie, an expired JWT) is not saved; Thangs tokens must be JWT-shaped (three dot-separated parts), and a MyMiniFactory address is accepted only for the `access_token=` value inside it, and the reply says why without repeating it; the question stays open. Pasting a login token is the one deliberate exception to "secrets are never collected in chat" (see [guided setup](GUIDED-SETUP-DEBUGGING.md)). It is opt-in, the reply never repeats the value, the bot deletes your message when the chat service allows it, and the file is written owner-only (0600).
2. **Save the file yourself** on the Print Handy machine (the walkthrough names the path, and the site's section below has the command), then press **Saved, try again** or type `saved`. The bot retries the same download, and the intake continues from there with no need to post the link again.

If the site still refuses the token, the question comes back, up to three times. Typing `cancel` (or pressing **Cancel**) stops the intake with the token file and its path named.

**What pasting risks.** The token is a login for your account on that site, so treat a pasted one like a password. Print Handy stores it on the machine it runs on, in the file above, readable only by the account that runs it. The chat message that carries it is visible to anyone who can read that channel until it is deleted. Print Handy deletes the message as soon as the token is saved (Discord, Telegram and Matrix; it needs the bot's permission to delete messages in that channel) and tells you whether that worked, but the chat service may keep a copy, and the bot can only delete what it is allowed to. If you cannot delete it either, treat the token as exposed: sign out of the site to invalidate it and connect again. If that is a concern in your channel, save the file instead. The token value is never written to the log, to a job receipt, or into any error or reply text.

The offer comes wherever the credential is first needed: at the model lookup for MakerWorld, Thingiverse and Cults3D (their APIs need it to look a model up at all), and at the download for the rest. `/print-source` shows at any time which sites are connected.

**Saving it as a variable instead.** Each site also reads an environment variable when its token file does not exist: `MAKERWORLD_TOKEN`, `THINGIVERSE_TOKEN`, `CULTS3D_TOKEN` (as `nickname:key`), `CREALITYCLOUD_TOKEN` (as `model_user_id:model_token`), `THANGS_TOKEN`, `NEXPRINT_TOKEN`, `MYMINIFACTORY_OAUTH_TOKEN`. Put it in your `.env` (Docker Compose passes the whole `.env` through; the Unraid template has masked fields for each under advanced settings) and restart Print Handy, since variables are read at start. A saved token file always wins over the variable, so the chat flow's freshly saved token takes effect even when an older variable is still set. When a variable's token expires, the bot's connect flow walks you through saving a file, which then takes over.

**Cancel** stops the intake with the token file and its path named, and nothing is sliced, uploaded or started. Login tokens expire; when one does, the next download offers the connect flow again. A token that is a JWT with an `exp` claim is checked locally first, so an expired one is named as expired before any request is sent.

### Thangs login token

Thangs links use the JSON API the Thangs web app itself uses (`thangs.com/api/...`). Links look like `thangs.com/designer/<user>/3d-model/<title>-<id>` or the short `thangs.com/m/<id>`. Looking up the model and listing its parts needs no login. The download asks the API for the model's signed download URL with the token sent as an `Authorization: Bearer` header. The token is the value of the `accessToken` cookie on `https://thangs.com` (the same value is in Local Storage under `accessToken`), saved as `~/.config/thangs/token`:

```bash
mkdir -p ~/.config/thangs && chmod 700 ~/.config/thangs
(umask 077 && cat > ~/.config/thangs/token)   # paste the accessToken cookie value, press Enter, then Ctrl-D
```

Thangs downloads a whole model, never a single part. When the answer is a zip, Print Handy keeps only the part you chose. Membership models and store items are marked in the model lookup. Downloading one needs a matching Thangs membership or purchase on the signed-in account; Print Handy never buys or unlocks a model, and a refusal says so. Free Thangs accounts have a daily download limit, which is named plainly when reached. Thangs login tokens last 7 days (observed 2026-10-29: the token's own `exp` claim is 7 days after it was issued), and Print Handy does not perform the site's refresh step, so expect the bot to offer the connect flow about once a week. The expiry is checked locally, so an expired token is named as expired before any request is sent.

### Nexprint login token

Nexprint (Elegoo) links use the gateway API the Nexprint web app itself uses (`www.nexprint.com/gateway/...`). Links look like `www.nexprint.com/en/models/<id>`. The model lookup and file list need no login. Each file is downloaded through a presigned-URL request with the token sent as an `Authorization: Bearer` header. The token is the value of the `auth_token` cookie on `https://www.nexprint.com` (or `access_token` inside Local Storage `ACCOUNT_INFO`), saved as `~/.config/nexprint/token`:

```bash
mkdir -p ~/.config/nexprint && chmod 700 ~/.config/nexprint
(umask 077 && cat > ~/.config/nexprint/token)   # paste the auth_token cookie value, press Enter, then Ctrl-D
```

Both the designer's own STL/3MF files and the model's print-setting 3MF projects are offered; a setting project is labeled with its setting name. Nexprint's download limits are named when reached. Only the `.com` site is supported, not `nexprint.cn`.

Live status: verified end to end on 2026-10-29 with operator tokens. For both sites the model lookup and file list answered without a login, the download endpoint refused a request with no token or a made-up one, and a real download succeeded: Nexprint returned the chosen STL directly (byte-for-byte the size the listing reported), and Thangs returned a zip of the whole model that Print Handy unpacked to the chosen part. Both files were valid STLs.

### MakerOnline (inventory only)

MakerOnline (Anycubic) links (`makeronline.com/en/model/<title>/<id>.html`) are recognized, and Print Handy looks up the model and lists its STL/3MF files through the JSON API the MakerOnline web app uses. No login is needed for that. **Downloads are not supported, and no token changes that.** MakerOnline's download service wants a device-fingerprint token from a browser security SDK (NetEase Yidun) on every download. Tested on 2026-10-29 with a valid operator login token: the token was accepted, and every download on two different models was refused with "The system is busy". Producing that fingerprint from a script would mean getting around a site's bot check, which Print Handy never does. So intake lists the files and stops with the gap named, and the workaround is to download the file from the model page in a browser and upload or drop it in the chat instead. There is no MakerOnline token file and no connect offer.

### Direct file links

A public link that points straight at a `.stl` or `.3mf` file works on any host, with no token or setup — the shape to use when a file is too big for a Discord upload. Dropbox share links work as pasted: a `www.dropbox.com` `/s/` or `/scl/` link is asked for its download form (the share form serves a preview page, not the file), and `dl.dropboxusercontent.com` links are used as-is. Other sharing services whose download needs a sign-in or an interstitial page (Google Drive, for example) are not supported; the refusal names that the link returned a web page instead of a file.

Because this is a fetch of an arbitrary URL from chat, it is guarded in ways the site downloads do not need to be:

* `http`/`https` only, and links carrying a user name or password are refused.
* The destination must be a public address. The host is resolved and checked before connecting, and every redirect target is checked again the same way before it is followed (at most 5 redirects) — a link, or a redirect, to localhost, a private address or a link-local address is refused and nothing is downloaded.
* The file name comes from the response's `Content-Disposition` header when it offers one, else from the final URL; the download streams under the same intake size limit as site downloads (200 MiB, see [intake resource limits](INTAKE-LIMITS.md)), counted as bytes arrive, and the partial file is removed when the limit trips.
* The saved bytes are checked before the normal source audit parses them: a response that is a web page (including HTML saved under a `.stl` name) and a non-archive file under a `.3mf` name are refused with that cause named, and the file is removed. From there the file follows the exact preparation path an upload follows.

Failures are named in chat with the cause and the remedy: the host could not be reached or answered with an HTTP error, the link returned a web page instead of a file, the target was not a public address, the file was not a `.stl`/`.3mf`, or the download exceeded the intake size limit.

### All settings

| Section | Key | Default | What it does |
|---|---|---|---|
| `printer` | `name`, `firmware` | from the model | Shown in job summaries |
| | `nozzle_type` | from the model or unset | Optional nozzle material; use a type supported by the selected printer model |
| | `model` | `flashforge.ad5x` | Printer model: `flashforge.ad5x`, `flashforge.ad5m`, `flashforge.ad5m_pro`, `flashforge.creator5`, `flashforge.creator5_pro`, `sovol.sv06`, `klipper.generic`, `octoprint.marlin`, `bambulab.a1`, `bambulab.p1s`. Sets every default below |
| | `start_macro`, `end_macro` | `PRINT_START`, `PRINT_END` | Start and end macros for stock Klipper models |
| | `bed_mm` | `[220, 220]` | Usable print area |
| | `max_print_height_mm` | `220` | Tallest allowed print |
| | `nozzle_diameter_mm` | `0.4` | Slicing refuses any other nozzle |
| | `filament_slots` | `4` | Filament slots; 1 for a single extruder |
| | `tool_count` | `1` | Independent toolheads on a tool-changer model (Creator 5 / 5 Pro: 4, Prusa XL: 5); slicer tool *i* is toolhead *i*, never an IFS slot |
| `layout` | `single_copy_edge_mm`, `multi_copy_edge_mm` | `1`, `3` | Distance from the bed edge |
| | `spacing` | `minimum` | Part gap preset (`minimum`, `low`, `medium` Standard, `high`); fills the gap keys below unless one is set explicitly. See [SETTINGS.md](SETTINGS.md#part-spacing-presets) |
| | `arrange_gap_mm`, `arrange_edge_mm` | `2`, `1` (preset) | Gaps when parts are rearranged onto one plate |
| | `merge_edge_margin_mm`, `merge_spacing_mm` | `1`, `2` (preset) | Gaps when merging plates |
| | `max_merged_hours` | `12` | Longer merged plates stay separate |
| `prime_tower` | `size_mm`, `part_clearance_mm`, `edge_mm`, `merge_slack_mm` | `45`, `3.5`, `4`, `1` (clearances from the preset) | Prime tower size and clearances |
| `orca` | `templates` | the model's folder under `print_handy/templates/orca` | Orca machine, process, and filament JSON |
| | `command` | Flathub OrcaSlicer | How to start the Orca CLI |
| `bambu` | `command` | Flathub Bambu Studio, no network | Fallback slicer |
| `paths` | `hermes_home` | `~/.hermes/profiles/3dprinting` | Hermes profile the bot runs in |
| | `archive`, `records_vault`, `cache` | `~/3d-prints`, `~/Documents/3D Printing Records`, `~/.hermes/cache` | Job archive, print records, cache |
| | `hermes_bin`, `hermes_agent`, `hermes_restart_helper`, `ffmpeg` | | Tool locations |
| `services` | `moonraker_url`, `moonraker_ws_url`, `camera_snapshot_url` | empty | Printer and camera; websocket is derived when empty |
| | `spoolman_url`, `openviking_url` | empty | Spoolman and optional memory store |
| | `vision_chat_url` | empty | OpenAI compatible endpoint for vision checks and roasts; empty disables both |
| | `stock_api_port` | `8898` | Flashforge stock API for IFS presence |
| `discord` | `chat_id`, `approval_user_id` | empty | Channel and approver IDs |
| | `upload_max_bytes` | `9500000` | Largest upload before video is shrunk |
| | `post_print_quiet_s` | `7200` | Retired (#524): the bot no longer resets chat sessions; kept so existing configs load |
| | `daily_reset_hour`, `timezone` | `4`, `America/New_York` | `daily_reset_hour` retired with it (#524); `timezone` is the display zone |
| | `bot_name` | `Print Handy` | Name the bot uses in the roast persona and the rendered profile templates (`SOUL.md`, locale strings). Set it to anything, for example your old name |
| | `question_timeout_s` | `1800` | How long a Discord question waits for a button press |
| | `safety_confirm_timeout_s` | `600` | How long a safety confirmation (stop, resume, reboot, cold-pull go-ahead) waits |
| | `upload_wait_s` | `3600` | How long an upload-wait freeze stays active |
| | `cold_pull_outcome_s` | `1800` | How long a cold-pull outcome prompt stays open |
| | `token_env` | `DISCORD_BOT_TOKEN` | Standalone bot: environment variable holding the bot token |
| | `allowed_user_ids` | empty | Standalone bot: optional whitelist of Discord user IDs, comma or space separated; empty lets everyone in the print channel use the bot |
| | `denied_user_ids` | empty | Standalone bot: optional deny list of Discord user IDs; a denied user is refused even when whitelisted or the approver |
| `bot` | `mode` | `hermes` | `standalone` runs `python3 -m print_handy.bot` with no Hermes; see [Standalone bot](../docs/STANDALONE.md) |
| | `health_port`, `health_stale_s` | `0`, `120` | Standalone bot: `GET /healthz` port (0 off) and heartbeat age limit |
| | `sync_commands` | `true` | Standalone bot: register slash commands at startup |
| `llm` | `chat_url`, `model`, `api_key_env`, `timeout_s` | empty, empty, `LLM_API_KEY`, `20` | Optional OpenAI-compatible model for standalone free-text replies; never starts anything itself |
| `design` | `enabled`, `chat_url`, `model`, `api_key_env`, `timeout_s`, `repair_attempts` | unset (auto), empty, empty, empty, `180`, `3` | `/print-design` (alias `/design`) AI design (#388); `enabled` unset = auto-on when an endpoint is configured, `false` forces off; empty fields inherit `llm`; the endpoint wins in Hermes mode too, and the profile's `auxiliary.print_design` block is the fallback when no endpoint is configured |
| `listener` | `tick_s` | `600` | Listener housekeeping interval |
| | `ifs_presence_poll_s` | `15` | IFS spool presence poll |
| | `spool_nag_s` | `600` | Reminder interval for an unanswered spool question |
| | `start_claim_ttl_s` | `1800` | How long a start button stays claimed |
| `vision` | `model`, `roast_model` | `gemini-latest`, `luna-latest` | Models for camera checks and roasts |
| | `roast_enabled` | `true` | The print-post roast switch: on by default whenever an LLM/vision endpoint is available, on every platform including Telegram; `false` turns roasts off, camera checks unaffected |
| `visual_watch` | `enabled`, `sensitivity` | `true`, `3` | Camera monitoring switch; sensitivity from 1 to 5 |
| `timelapse` | `archive_host`, `archive_dir`, `ssh_key` | empty | Printer-rendered timelapse archive; empty host disables the printer-rendered delivery path |
| | `render_wait_s`, `render_poll_s` | `900`, `10` | Wait for the printer to render the timelapse |
| | `max_bytes`, `local_retention_days`, `plan_clip_retention_days` | 200 MiB, `1`, `14` | Size cap and retention |
| | `vertical_short`, `vertical_short_mode`, `vertical_short_overlay` | `false`, `pad`, `true` | Optional 9:16 vertical short posted alongside each timelapse (#317); mode is the blurred-pad or centre-crop framing, overlay burns in the title/printer/filament/time text |
| `logging` | `level`, `format`, `file` | `INFO`, `text`, empty | Log verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`), `text` or `json` lines, and an optional rotating log file |
| `maintenance` | `quick_hours`, `full_hours`, `full_days` | `25`, `75`, `30` | When maintenance reminders come due |
| `nozzle_temp_offsets` | supported nozzle type | steel types `5.0`, others `0.0` | Temperature compensation from −10 to +15 °C for a known nozzle type; the per-type default applies generically to every filament baseline, and a value here overrides that type's default |
| `material_temps` | PLA, PETG, ABS, ASA, TPU, PA, PC (`nozzle`, `bed`; PCTG falls back to 250/80 °C unless set) | see `config/home.yaml` | Configurable fallback temperatures in °C; explicit Spoolman filament settings take precedence. Named variants adjust the fallback (#512): Silk PLA +10 °C and 0.7× outer-wall speed, PLA+ / Matte PLA / *-CF / *-GF +5 °C (PA-CF +10 °C), Matte PLA 0.9× outer-wall speed. Variants stack ("PLA+ Silk", "Silk PLA-CF"): the larger nozzle delta and the smaller outer-wall factor apply. A Spoolman temperature of 0 means not set. PET resolves to PETG; PA6 / PA12 / PAHT to PA; "TPU 95A" to TPU; PLA-S / PA-S (Bambu support grades) and HS / HF speed grades stay refused |
| `spool` | `prompt_brand` | `false` | Ask for brand/vendor in the new-spool walkthrough (#512); off by default, brand is skipped |
| `cold_pull` | profile name | `pla_trial = [220, 90]`, `petg = [245, 100]` | Hot and cold temperatures in °C; profile names are configurable |

## Install and update

See [docs/INSTALL.md](../docs/INSTALL.md) to install into a Hermes profile, or [docs/STANDALONE.md](../docs/STANDALONE.md) and `scripts/install-standalone.sh` to run the standalone bot with no Hermes.

Update only from a commit that is green in CI. On the machine running Print Handy:
```bash
scripts/home-update.sh          # fast-forward to origin/main
scripts/home-update.sh <tag>    # or a release tag
```
The script refuses to run if tracked files were edited by hand. It runs the offline tests and the doctor after the update, and resets to the previous commit if either fails. `config/local.yaml` and legacy `config/local.toml` are never touched. A listener restart command can go in `config/after-update.sh` (gitignored), which runs after a good update.

Check the setup any time with `python3 -m print_handy.doctor`. It also checks Moonraker and Spoolman when they are set in `[services]`, and warns about Print Handy state files stranded outside the profile `state` folder. The update and install scripts remove those strays when the profile holds the real copy (or the stray is empty); files in other profiles are only reported; `python3 -m print_handy.state_files [--dry-run] [--remove]` does the same by hand.

## Run with Docker

Prerequisites: Docker Engine with the Docker Compose plugin, and Git. From the repository root, copy `.env.example` to `.env` and set `PRINT_HANDY__SERVICES__MOONRAKER_URL` to your printer's Moonraker URL. Set other scalar settings with `PRINT_HANDY__SECTION__KEY` variables in `.env`; `config/docker.yaml` supplies Docker defaults. Lists such as `orca.command` belong in a config file, not environment variables. To replace the full config, mount YAML and set `PRINT_HANDY_CONFIG` to its container path. Existing TOML mounts remain compatible by mounting at `/app/config/docker.toml` and setting `PRINT_HANDY_CONFIG=/app/config/docker.toml` in `.env`.

Compose stores persistent data in `./data/hermes`, `./data/archive`, `./data/records`, and `./data/cache`; the included Spoolman uses `./data/spoolman`. Create these directories before starting. Spoolman is included in the Compose stack and published on no host port; Print Handy has no published host port and connects to Moonraker and Spoolman over the Compose network.

```bash
cp .env.example .env
mkdir -p data/hermes data/archive data/records data/cache data/spoolman
docker compose up -d
docker compose logs -f print-handy
```

To update a checkout, pull the desired source revision and rebuild/recreate the services with `docker compose up -d --build`. Stop the stack with `docker compose down`; the bind-mounted data directories remain. See [Docker details](../docs/DOCKER.md) for the full configuration notes.

## Running tests

From the repository root, CI runs this on every push and pull request:
```bash
python3 -m pip install -r requirements-test.txt
python3 -m pytest tests
```
A second CI job installs the Flathub OrcaSlicer, slices small test models with the templates, and runs the slice guards on the real G-code. Run it locally with `PRINT_HANDY_REAL_SLICER=1 python3 -m pytest -s tests/slicer`.

### Discord flow corpus

`tests/corpus/cases/` holds generated, redistributable 3MF projects and STL meshes run end to end through the Discord intake with fake button clicks. Nothing contacts a printer. Each case runs twice: through the Hermes plugin hook, and through the standalone bot on a fake discord.py client (`tests/corpus/fake_discord.py`), where real buttons resolve the core prompt registry. Every PR runs the corpus with fake Orca; a separate real-slicer CI job runs the same cases with Flathub Orca.

| Coverage | Cases | Behavior asserted |
| --- | --- | --- |
| Native STL intake and color selection | `stl-single-part` | Source kind and 30 × 30 × 20 mm bounds, one G-code, support off |
| Fit and operator-selected shrink | `must-shrink`, `stl-must-shrink` | 3MF and STL trigger fit prompt, scale range, one G-code |
| STL overhang and tall narrow geometry | `stl-needs-supports`, `stl-thin-footprint` | Source bounds, default support-off behavior, successful guarded slice |
| Named parts and single-plate multicolor | `parts-one-plate` | Exact part names/colors, merge mode, three tool slots, one G-code |
| Multiplate merge and split planning | `plates-multicolor`, `split-plan` | Source plate count, merged colors/tool slots, split mode and expected G-code counts |
| Supports-on geometry and baseline 3MF | `needs-supports`, `single-part` | Support-enabled profile plus support G-code, ordinary single-part intake |

All ten current models are generated locally by `tests/corpus/make_models.py`, have CC0 provenance in their manifests, and are under 5 MB. A future hand-authored regression fixture is also allowed when its redistribution license is documented and the public size limit is met; generator checks pin the built-in fixtures without requiring every public case to be generated. The corpus contains no private or license-restricted source files. Current scenario gaps include painted multicolor meshes, partial-plate selection, non-English part-name translation, rotation-only fit, archived-job reprints, and a near-5 MB timeout case. Generated STL covers shrink-to-fit but not split planning; split planning remains covered by 3MF.

To add a model that broke Discord: make `tests/corpus/cases/<name>/` with one supported model file (`.3mf` or `.stl`; `model.3mf` wins if a folder has more than one) and a `case.toml` listing the buttons pressed and what you expect. Only include models you may redistribute: your own work, CC0, CC BY, CC BY-SA, MIT, Apache (unversioned, 1.0, 1.1, or 2.0), or public domain. The license field must be an exact recognized label. Its only accepted suffix is `: generated by <path>, no third party geometry`, for generated fixtures. Other qualifiers, restrictions, and ambiguous labels are rejected. Record the exact provenance in `license = "..."`. Public models must be 5 MB or smaller. An unanswered question fails the test and prints its buttons, so copy them in. `tests/corpus/make_models.py` writes the built-in models from a `[model]` table in each generated case's `case.toml`, so adding a generated case is one `case.toml` edit plus `python -m tests.corpus.make_models` from the repository root; see any existing `case.toml` for the format. A `[model]` table names the shapes, colors and layout (`assembly`, `loose` or `plates`), and `fit = "roomy"`, `"tight"` or `"none"` sizes the model from the real merge planner (`print_handy/core/plate_merge`) instead of fixed millimeters. A `[printer]` table (`nozzle`, `tower`, `bed`, `slots`) runs that one case on a varied copy of the shared `printer_hardware.json`. Entries in `tests/corpus/matrix.toml` expand to whole named cases (colors x fit, with derived expectations and `known_failure` links). `test_checked_in_models_match_the_generator` checks every spec against its checked in file.

```bash
python3 -m pytest -q tests/corpus                                # fake Orca
PRINT_HANDY_REAL_SLICER=1 python3 -m pytest -q -s tests/corpus   # real Orca, slices under .slicer-work/
```

#### Local private corpus

Personal or non-redistributable models must stay outside the checkout. Create one folder per case under a private directory, with the model and a `case.toml` copied from a public example. The private corpus is selected only when explicitly configured:

```bash
export PRINT_HANDY_PRIVATE_CORPUS="$HOME/.local/share/print-handy/private-cases"
python3 -m pytest -q tests/corpus
unset PRINT_HANDY_PRIVATE_CORPUS
```

Private cases use the same offline flow and skip only the public redistribution-license check. The configured directory must exist outside the repository and contain at least one case. Private-corpus opt-in fails when `CI=true`; never set it in CI. The private model is not added to the checkout, public diff, or CI artifacts. Real-slicer output is temporary local scratch under the ignored `.slicer-work/` directory; do not commit or upload it.

## ZMod plugin and spool matching

This section is AD5X-only: the macro and the spool matching below describe the IFS
printer. The AD5M and AD5M Pro run the same ZMod firmware family and Print Handy
supports them as single-extruder models, but they have no IFS and no spool matching;
the Creator 5 and Creator 5 Pro are toolhead machines whose spools bind per
toolhead rather than per IFS slot, which this macro's slot model does not cover;
see [ZMod printers](ZMOD.md) for all five ZMod models and their differences.

ZMod switches to a backup spool mid-print only when another slot holds the exact same color and material. The `print-handy.cfg` macro lets you check that before a job starts:
```gcode
PRINT_HANDY SLOT=1
```
If slot 1 and slot 3 are both black PETG, it confirms:
```
PRINT_HANDY: black / PETG also on slot 3. ZMod can switch. This macro does not.
```
`PRINT_HANDY COLOR=black MATERIAL=PETG` checks all four slots without assuming an active toolhead.

To install, run this on the printer console:
```gcode
ENABLE_PLUGIN name=print-handy
```
Then add your slot assignments to `mod_data/user.cfg` in a `[gcode_macro _PRINT_HANDY_VARS]` block and run `FIRMWARE_RESTART`. Moonraker update manager entry, in `mod_data/user.moonraker.conf`:
```ini
[update_manager print-handy]
type: git_repo
path: /root/printer_data/config/mod_data/plugins/print-handy
origin: https://github.com/catduckgnaf/print-handy.git
primary_branch: main
managed_services: klipper
channel: dev
is_system_service: False
```

On Telegram, a ZMod household can also run moonraker-telegram-bot — the
bot ZMod itself uses — alongside Print Handy's Telegram bot for live
printer control, notifications and timelapse: see
[Telegram bot setup](TELEGRAM-BOT-SETUP.md#pairing-with-moonraker-telegram-bot-zmod-companion)
for the division of labour, the two-token rule and how to keep only one
bot posting progress notifications.

## Repository structure

* `config/home.yaml`: default settings. Legacy TOML remains supported during migration. See Settings.
* `print_handy/settings.py`: loads and validates settings.
* `print_handy/doctor.py`: checks that a machine can run Print Handy.
* `print_handy/core/`: offline 3MF and STL pipeline. No printer connection.
* `print_handy/hermes/`: Moonraker listener, Discord posts, spool sync, print control, and records.
* `print_handy/printers/`: one package per brand, one file per printer model; see `base.py` for what each one owns.
* `print_handy/templates/orca/<model folder>/`: Orca machine, process, and filament JSON per printer.
* `plugins/print-feedback/`: the Discord command handlers and interaction guards, loaded by the Hermes gateway or by the standalone bot.
* `print_handy/bot/`: standalone bot. `platform.py` is the chat-platform interface (#164: `send`, `send_clarify`, `answer_typed`, `close_prompt`, `is_allowed_id`, per-platform limits on the adapter); `adapter.py` is its Discord implementation over discord.py, `telegram.py`/`telegram_rest.py` its Telegram implementation over the standard-library Bot API (long polling, inline keyboards) and `matrix.py`/`matrix_rest.py` its Matrix implementation over the standard-library Client-Server API (/sync long polling with a persisted next_batch token, media repo uploads/downloads, numbered-list prompts — Matrix has no buttons — and auto-join of the print room on invite), selected by `[bot] platform` (default `discord`). The Telegram and Matrix adapters are pure bots (`uses_llm = False`): BotCore never calls the LLM fallback for them. Plus the router, health check and optional LLM; `systemd/` holds its units.
* `hermes/`: profile templates and skills installed by `scripts/install-hermes.sh`.
* `scripts/`: `install-hermes.sh`, `install-standalone.sh`, `home-update.sh`, and `migrate-names.sh` (one-shot, see `docs/RENAME-MIGRATION.md`).
* `print-handy.cfg`, `install.sh`, `uninstall.sh`, `update.sh`: ZMod plugin macro and lifecycle scripts. The cfg file name must match the plugin name (`print-handy`) or ZMod refuses to enable the plugin (#289).
* `tests/`: offline suite, real slicer checks, the Discord flow corpus (`tests/corpus/`), and the Klipper macro tests.
* `docs/`: install guide and AD5X integration specs.

## Links

* ZMod AD5X docs: [AD5X.md at 0017033](https://github.com/ghzserg/zmod/blob/001703371c90e228defce41bb4230b7fafd37fea/docs/en/AD5X.md)
* ZMod firmware project: [ghzserg/zmod](https://github.com/ghzserg/zmod)
* ZMod plugin guide: [wiki.zmod.link/Plugin](https://wiki.zmod.link/Plugin/)
* OrcaSlicer CLI documentation: [CLI mode](https://github.com/OrcaSlicer/OrcaSlicer/wiki/cli_mode)
* Releases: [GitHub releases](https://github.com/catduckgnaf/print-handy/releases)

## Spool / IFS source of truth

Spoolman is authoritative for spool identity, filament material/color, and which spool sits in which slot: the slot assignment lives in a Spoolman extra field, `ph_slot` (`<printer_id>:<slot>`, for example `ad5x:2`), which Print Handy creates on startup if it is missing (#798). `location` (`IFS Slot N` versus `Shelf`) stays human-readable and is still written, but it is display-only and is never read as truth. `printer_hardware.json`'s `filament_slots` is a cache, not a record of truth: `spool_sync.reconcile(printer_id)` rebuilds it from Spoolman's `ph_slot` assignments — running at listener startup and on each reconnect, after every bind, and from `/spool sync` — migrating the existing cache into Spoolman on the first run (when Spoolman carries no `ph_slot` values at all), clearing stale claims (a displaced, archived or deleted spool never keeps claiming a slot), converging the printer screen through the usual deferred-while-printing push, and naming conflicts (two spools claiming one slot, a slot claimed by an archived spool, a spool Spoolman no longer has) instead of picking silently. An unreachable Spoolman is a named reconcile failure and the cache is left untouched. Spoolman availability is never a gate, though (#235): when Spoolman is unreachable, errors, or disagrees with the record, the hardware check, G-code verification and slicing warn and continue on the recorded spool state (material, density and temperatures in `printer_hardware.json`) instead of refusing work. Since #387 that is structural rather than per-callsite discipline: every Spoolman HTTP call (lookup, spool assignment, usage reporting, active-spool set, inventory, health probe) goes through the one client, `print_handy/core/spoolman_client.py` — short timeout, every transport or HTTP error caught and logged as a WARNING — and a contract test fails CI if a Spoolman API path is paired with a direct HTTP transport anywhere else. The print-start active-spool sync is best-effort too: when Spoolman does not answer, or the Moonraker spool-pointer write fails, the launch proceeds on the slot's recorded data, the user sees the single line "Spoolman unavailable; spool tracking skipped for this job.", and the usage that could not be reported is recorded as pending (`spoolman_usage_pending` in the job record at start, and in the completion record when Spoolman is down at completion) for later reconciliation; it is never retried in a way that blocks the job. A live Spoolman answer that the spool is archived or a different material than the slot record still refuses a launch: that is Spoolman data contradicting the operator's record, not an outage. The Spoolman address itself comes from `services.spoolman_url` in settings (including the `PRINT_HANDY__SERVICES__SPOOLMAN_URL` environment variable) whenever that is set; the URL cached in `printer_hardware.json` is only a fallback, so a stale cached address cannot redirect lookups to a decommissioned host. A single-extruder printer (one slot) with no `filament_slots` record offers its configured `spool` as slot 1 in the Discord color questions (#263); when that spool has no Spoolman binding (`spoolman_id: null`) the selection carries none, whether or not a Spoolman URL is set. Multi-slot printers and the four-bay stock Flashforge still need their recorded slot map and a Spoolman ID per slot. Physical feeder slots are 1-based (1–4); G-code tools are zero-based (T0–T3), and mappings explicitly translate between them. On a multi-tool printer (Creator 5 / 5 Pro) the same recorded map is per toolhead instead — Spoolman locations are `Toolhead N` — and the Discord color questions follow suit (#346): one question per source color asking which toolhead prints it, toolhead labels instead of slot labels, a toolhead already assigned a different color is not offered again, and more distinct colors than toolheads is refused (one color per toolhead; printing everything in one color on one toolhead stays available). The picks slice through the model's four-extruder templates as the identity tool mapping, and summaries name `Toolhead N`, never a slot. G-code/job snapshots are provenance, not permission to override newer Spoolman facts. Color codes are read by one parser (`filament_color.parse_hex`: `#RGB`, `RRGGBB`, `RRGGBBAA`, optional `#` or `0x`, a comma list gives its first valid entry) and sent to screens as six-digit RGB; never infer missing color from another spool or slot. Every binder (`/spool`, the IFS insertion prompt, `spool_lifecycle`, the startup backfill) fills a slot's color in the same order and records `hex_source`: the typed or recorded slot hex, Spoolman `color_hex`, each valid `multi_color_hexes` entry, the printer-reported hex, the color name (modifier words such as "dark" and "light" shade the base color), then a neutral gray that is reported out loud. Two colors are compared as colors (CIE76 delta-E, `filament_color.same_color`), never as strings: a mapped launch proceeds on the same color in another spelling (FFFFFF vs FAFAFA, 0C6283FF vs 0C6283), prints a WARNING and proceeds when the printer slot's color and the record's are close, missing or unreadable, or when the record's hex was only guessed from the spool name, and refuses (before anything is uploaded) only when both are real hexes that are clearly different. Correct missing/invalid Spoolman color before binding. Screen updates are immediate when idle or persisted pending while printing; deferred/failed updates require explicit sync after printing. Moonraker active spool is usage accounting only: single-slot launch repoints it; multi-slot launch leaves it unchanged, so usage can be approximate. Selected physical slot determines identity/readiness, not the global accounting pointer.
