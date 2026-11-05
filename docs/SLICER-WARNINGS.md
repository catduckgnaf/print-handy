# Slicer warnings and the support question

[Documentation home](README.md)

After every successful Orca slice, Print Handy reads the slicer's `result.json` (`warnings[]` and `sliced_plates[].warning_message`) and sorts each warning.

## Support finding and intake pre-check

Print Handy performs speculative background slicing and geometry clearance analysis from intake. When OrcaSlicer detects floating regions or cantilevers, or when geometry analysis detects insufficient support clearance (under 1.0 mm gap across 500 mm2 or more, or along a narrow span) or ineffective modeled supports, the summary before bed-clear presents the choices directly:

- **Slice and print with supports (recommended)**: uses the printer profile's auto tree supports.
- **Slice and print without supports**: prints the un-supported slice as authored.
- **Extend underside to plate**: for low-gap undersides, extends downward vertices to the build plate so the gap is eliminated while preserving materials and colors.
- **Flatten (sink) to plate**: sinks the model through the plate by the gap height.

Both slice variants run speculatively in the background when flagged. Because the operator makes the choice before confirming the bed is clear, the print starts immediately with the chosen slice and no second question interrupts the start. The post-slice dialog remains only as a fallback for un-prechecked cache misses and never asks twice for the same recipe.

## Every other warning (information only)

Other non-critical warnings never gate, block or add a button. Identical text is shown once, and the pre-print summary carries one compact line (count plus the first warning); the full list stays in the job's slice provenance. Warnings known to be harmless go in `print_handy/data/slice_warning_ignore.json` (`{"ignore": [{"pattern": "<regex>"}]}`); they are logged and not shown. The support finding cannot be ignored.
