# Slicer warnings and the support question

[Documentation home](README.md)

After every successful Orca slice, Print Handy reads the slicer's `result.json` (`warnings[]` and `sliced_plates[].warning_message`) and sorts each warning.

## Support finding (asks a question)

A floating cantilever, floating regions, or "enable support generation" warning means the model needs supports. If the slice ran with supports off and no one has decided supports for this job, Print Handy pauses before bed-clear, upload and start and asks, naming the objects the slicer flagged:

- **Re-slice with supports**: slices again with the printer profile's auto tree supports.
- **Print without supports**: continues with the slice as it is.
- **Cancel**: nothing is uploaded or started.

The choice is saved on the job (`support_decision` in `JOB.json`), so retries and reprints do not ask again. An author's `enable_support = 0` inside the 3MF is not a decision. An explicit operator "no supports" is never overridden. This is a question, never a block.

## Every other warning (information only)

Other non-critical warnings never gate, block or add a button. Identical text is shown once, and the pre-print summary carries one compact line (count plus the first warning); the full list stays in the job's slice provenance. Warnings known to be harmless go in `print_handy/data/slice_warning_ignore.json` (`{"ignore": [{"pattern": "<regex>"}]}`); they are logged and not shown. The support finding cannot be ignored.
