# Slicer warnings and the support question

[Documentation home](README.md)

After every successful Orca slice, Print Handy reads the slicer's `result.json` (`warnings[]` and `sliced_plates[].warning_message`) and sorts each warning.

## Support finding and intake pre-check

Print Handy performs speculative background slicing and geometry clearance analysis from intake. When OrcaSlicer detects floating regions or cantilevers, or when geometry analysis detects insufficient support clearance (under 1.0 mm gap across 500 mm2 or more, or along a narrow span) or ineffective modeled supports, the summary before bed-clear presents the choices directly:

- **Slice and print with supports (recommended)**: uses the printer profile's auto tree supports.
- **Slice and print without supports**: prints the un-supported slice as authored.
- **Extend underside to plate**: for low-gap undersides that face the plate, extends downward vertices to the build plate so the gap is eliminated while preserving materials and colors. The prompt states the measured volume this adds; build-item and component transforms are honored when the vertices are extended.
- **Flatten (sink) to plate**: sinks the model through the plate by the gap height. The prompt states the measured volume that ends up below the plate and is clipped.
- **Reorient (lay flat)**: records a rotation that puts the largest face on the plate, with no supports value attached, and clears the recorded support decision, so the post-rotation slice evaluates the support question freshly.
- **Print anyway (accept the risk)**: the explicit waiver for a low-clearance finding.

When the only evidence is a low-clearance underside (no slicer floating-region finding), supports cannot reach the gap, so the with-supports option is labeled "not recommended" and the without-supports option is listed first instead.

Both slice variants run speculatively in the background when flagged. Because the operator makes the choice before confirming the bed is clear, the print starts immediately with the chosen slice and no second question interrupts the start. The post-slice dialog remains only as a fallback for un-prechecked cache misses and never asks twice for the same recipe.

## Phase 2 hardening

- A sloped wall that roots into the shelf below it is not a floating underside: a sloped face (downward normal within 55 degrees of vertical, not near-horizontal) whose sub-1.0 mm clearance strip is narrower than 2.0 mm and touches a zero-clearance anchor edge is recorded as a `sloped_wedge_root` region and does not make the model risky. The strip's width is measured across the slope, so a bottom chamfer that follows a part's whole outline counts too.
- A gap smaller than the first layer height of the printer's profile (0.24 mm for a 0.4 mm nozzle) is read as contact, not as a low-clearance gap: the first layer is squished into it, so the underside prints as resting on what is below it. Such a gap never makes the model risky, never marks the supports option "not recommended", never asks for Print anyway after slicing, and never suggests extend or sink. A slope that rises from such a gap is treated like a wedge rooting into contact. Gaps from the first layer height up to 1.0 mm are unchanged.
- Choosing "with supports" does **not** waive a low-clearance finding (a gap under 1.0 mm). Supports cannot fit into such a gap, so that choice is satisfied only by a verified geometry repair (extend, sink, reorient) or by choosing **Print anyway** explicitly, and it must additionally pass post-slice toolpath coverage verification for every flagged region before the print starts. Choosing "without supports" is the operator's explicit as-authored decision after seeing the findings and is recorded as such (`via = without_supports`).
- Geometry analysis also emits `supportable_overhang` regions (clearance at least 1.0 mm, area at least 500 mm2 or span at least 15 mm). They are not risky at pre-check; when the job is sliced with supports they are the required targets for the coverage check: 100% of the 3 mm cells under each region's bounding box must be crossed by a `;TYPE:Support` / `;TYPE:Support interface` toolpath at a height that can hold that underside up. When slicing centered, scaled or rearranged the model, the regions are first mapped into the sliced coordinate system from the model's overall bounds in both systems.
- A single mesh can bake its support pillars into the part itself, leaving no named support object to detect. When one mesh's downward overhang area is at least five times its bed-contact area (and at least 500 mm2), the analysis names it as a single-mesh cantilever warning. If Orca warns about floating regions or a cantilever and the operator chooses to print without supports, the job records an explicit `UNSUPPORTED_CANTILEVER_OVERRIDE` bound to the warning text and the source hash.
- A single-object 3MF without material attributes (`pid`/`pindex`, as OpenSCAD exports) is treated exactly like a single-part STL across clearance analysis, single-color slot binding and post-slice verification; STL sources get the same clearance analysis as 3MF sources.
- Cancelling at the post-slice support prompt cancels the start, not the slice: once a slice is referenced by `JOB.json` and the intake receipt, its files stay on disk with status `SLICED_NOT_STARTED`.
- Uploading an already-sliced `.gcode.3mf` package is refused with that explanation; it is not reported as a corrupt 3MF structure.

## Filament warnings (print anyway)

No filament condition blocks a print. A filament finding stops the slice once with the warnings listed, and **Print anyway (accept the filament warnings)** records an acceptance bound to those exact warning texts, the source file and the selected plates. The acceptance covers one slicing run: a reslice asks again. Findings include unknown or disagreeing per-plate filament usage, damaged usage records in the source, and three cases that print from a named fallback slot:

- **A used filament with no slot chosen** prints from the slot already chosen for that same source filament, otherwise the slot chosen for the first part on that plate, otherwise the fallback slot below.
- **A slot number past the printer's slots** (for example slot 6 on a four-slot AD5X) prints from the fallback slot.
- **Color choices never made** print every part in one color from the fallback slot, the same as choosing one color for everything.

The fallback slot is the printer's active slot when the printer record names one that is loaded, otherwise its first loaded slot. With no loaded-slot information at all it is slot 1, and the warning says so. The warning always names the slot. The substitution is made before slicing, so the G-code only uses slots the printer has and goes through the same bounds and slot checks as any other print. The job keeps the color choices you made; the slots the print used are recorded with the sliced file.

## Material warnings at print start (print as sliced)

The same ruling holds when a print starts: no material condition hard-blocks it (#1336). When the start finds one of the conditions below and nothing else is wrong, nothing is uploaded and the chat asks once, on every transport (Discord, Telegram, Matrix and the Web UI chat), for the intake start, `/print-next`, `/print-again` (reprints and copies 2 and later) and the Discord plate and Retry buttons:

- **Reslice for the loaded material** (the first button and the safe answer): nothing starts; send the model or link again and it is sliced for what is loaded now.
- **Print as sliced**: the start runs again with your answer for this exact G-code only.
- **Cancel**.

The conditions, each named with its consequence in the question:

- **Sliced for one material, another loaded** (for example a PETG file with PLA loaded), on every printer family. Printing it as sliced heats to the G-code's own temperatures, the ones for the material it was sliced for, never the loaded spool's.
- **Abrasive filament on a brass or other soft nozzle** (carbon fiber, glass fiber, glow, wood, metal fill). The warning says plainly that this filament will wear out the nozzle.
- **A material the printer maker does not recommend** for the printer (Bambu Lab's list).
- **A Bambu AMS with no tray holding the required material**: the warning names the tray that would be used, and Print as sliced uses it for this start.
- **An AD5X slot screen label of another material family** than the job selected (a family match such as PLA for PLA+, or SILK for Silk PLA, is not a difference).

What an acceptance never does:

- It never waives a mechanical limit. The axis bounds and the heater maxima are checked on every accepted start, as on a yolo start; a file over the maximum still refuses.
- It never carries over. It is bound to the SHA-256 of the exact G-code it was given for and to the materials it names, and it lasts one start: another file, another mismatch, a later copy, a reprint or a Retry asks again.
- It never skips the nozzle check. An accepted start checks the loaded material against the fitted nozzle and shows the result; abrasive filament on a brass nozzle is its own warning, accepted on its own.

A refusal that names a material warning beside a real fault (a wrong nozzle size, for example) offers no Print as sliced: fix the fault first.

Two more material conditions join the slicing round described above (**Print anyway (accept the filament warnings)**), so they are answered on every surface that already answers filament warnings:

- **The job's saved slot selection no longer matches the slot**: "slot N now holds <current>; the job chose <saved>". Accepting slices the job for what the slot holds now (the job's selection moves to the slot's current spool).
- **Abrasive filament on a brass or other soft nozzle** at slicing time, with the same plain wording.

A remap to a slot holding another material (`model_job remap-slot` or `print-now --remap-slot`) warns the same way, and accepting it (`--accept-material-mismatch`) reslices the job for that slot: re-finalizing the old G-code would keep the sliced material's temperatures. A file with no single-plate job to reslice from is refused with that reason.

A slot spool swapped since slicing (same material family) is asked in chat as **Use the loaded spool**, which passes `--accept-spool-change` for that start.

A Spoolman spool that is archived or records another material than the slot record is a plain warning at start (usage may be charged to the wrong spool until Spoolman is corrected); the print uses the slot record.

From the command line, `model_job print-now` (and `--reprint`) takes `--accept-material-mismatch` to print as sliced through every material warning of that run. A chat answer passes the exact warnings with `--accept-material KEY` (the `[accept KEY]` the refusal printed) and `--accept-material-sha256` for the G-code it was given for.

### Not enough filament on a slot (print anyway)

Before anything is uploaded, a Klipper start (the AD5X and other Moonraker printers) compares the grams the G-code says each slot feeds with what that slot's spool has left: Spoolman's remaining weight, else the slot record's. Every slot the print uses is checked, not only the first, and tools that share a slot add up. When a spool has less than the print needs, the same question as above asks, on the same transports and flows, with two buttons:

- **Print anyway**: the start runs again with your answer for this exact G-code only. The spool may run out partway, which pauses the print until the slot is reloaded.
- **Cancel**: nothing starts. Load more filament (or correct the spool's weight in Spoolman) and start again.

Each short slot is named with its spool, the grams needed and the grams left, for example "Slot 2 (bronze PLA+ spool #19) needs about 6.8 g but Spoolman says the spool has 0 g left, short by about 6.8 g". A spool at 0 g still asks, and the warning says the 0 g came from Spoolman: a spool whose remaining weight was never set in Spoolman reads as 0 g, so check it there if the spool is not really empty. A spool whose weight is unknown (no Spoolman weight and no slot record) is not asked about. From the command line the shortage keys work like the material ones (`--accept-material KEY`, or `--accept-material-mismatch` for every warning of that run).

## Every other warning (information only)

Other non-critical warnings never gate, block or add a button. Identical text is shown once, and the pre-print summary carries one compact line (count plus the first warning); the full list stays in the job's slice provenance. Warnings known to be harmless go in `print_handy/data/slice_warning_ignore.json` (`{"ignore": [{"pattern": "<regex>"}]}`); they are logged and not shown. The support finding cannot be ignored.
