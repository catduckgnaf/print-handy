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

Both slice variants run speculatively in the background when flagged. Because the operator makes the choice before confirming the bed is clear, the print starts immediately with the chosen slice and no second question interrupts the start. The post-slice dialog remains only as a fallback for un-prechecked cache misses and never asks twice for the same recipe.

## Phase 2 hardening (#922)

- A sloped wall that roots into the shelf below it is not a floating underside: a sloped face (downward normal within 55 degrees of vertical, not near-horizontal) whose sub-1.0 mm clearance strip is narrower than 2.0 mm and touches a zero-clearance anchor edge is recorded as a `sloped_wedge_root` region and does not make the model risky.
- Choosing "with supports" does **not** waive a low-clearance finding (a gap under 1.0 mm). Supports cannot fit into such a gap, so that choice is satisfied only by a verified geometry repair (extend, sink, reorient) or by choosing **Print anyway** explicitly, and it must additionally pass post-slice toolpath coverage verification for every flagged region before the print starts. Choosing "without supports" is the operator's explicit as-authored decision after seeing the findings and is recorded as such (`via = without_supports`).
- Geometry analysis also emits `supportable_overhang` regions (clearance at least 1.0 mm, area at least 500 mm2 or span at least 15 mm). They are not risky at pre-check; when the job is sliced with supports they are the required targets for the coverage check: 100% of the 3 mm cells under each region's bounding box must contain a `;TYPE:Support` / `;TYPE:Support interface` toolpath point.
- A single mesh can bake its support pillars into the part itself, leaving no named support object to detect. When one mesh's downward overhang area is at least five times its bed-contact area (and at least 500 mm2), the analysis names it as a single-mesh cantilever warning. If Orca warns about floating regions or a cantilever and the operator chooses to print without supports, the job records an explicit `UNSUPPORTED_CANTILEVER_OVERRIDE` bound to the warning text and the source hash.
- A single-object 3MF without material attributes (`pid`/`pindex`, as OpenSCAD exports) is treated exactly like a single-part STL across clearance analysis, single-color slot binding and post-slice verification; STL sources get the same clearance analysis as 3MF sources.
- Cancelling at the post-slice support prompt cancels the start, not the slice: once a slice is referenced by `JOB.json` and the intake receipt, its files stay on disk with status `SLICED_NOT_STARTED`.
- Uploading an already-sliced `.gcode.3mf` package is refused with that explanation — it is not reported as a corrupt 3MF structure.

## Every other warning (information only)

Other non-critical warnings never gate, block or add a button. Identical text is shown once, and the pre-print summary carries one compact line (count plus the first warning); the full list stays in the job's slice provenance. Warnings known to be harmless go in `print_handy/data/slice_warning_ignore.json` (`{"ignore": [{"pattern": "<regex>"}]}`); they are logged and not shown. The support finding cannot be ignored.
