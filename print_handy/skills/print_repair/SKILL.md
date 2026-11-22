# Print repair

Use this skill when a model file will not print cleanly as it is: the slicer
warns about floating regions or missing support, Print Handy's clearance
check flags an underside, or the owner asks why a part needs supports and
whether it can print without them.

## Procedure

1. Measure before deciding. Run `watertight` and `bodies` first: a mesh with
   open edges or stray bodies is fixed as a mesh before anything else.
2. Locate the problem with the geometry tools, not with guesses:
   `island_scan` for floating regions (the slicer's warning names no place;
   this does), `overhang_scan` for cantilevers, lips and bridges, and
   `clearance_report` for undersides too close to the plate for supports.
3. Check every low-clearance finding with `underside_profile` before acting
   on it. A sloped flare of 45 degrees or more holds itself up; the finding
   is then a false positive and needs no fix.
4. Tell defects from design (see the references). Sub-millimeter sag under a
   flat face is a defect to trim. A shelf is design: it needs a gusset, a
   fill or supports, and the owner chooses.
5. Propose the smallest change that removes the problem, and say what it
   costs: volume added or removed, and where.
6. A repair is reported as fixed only when Print Handy's verification
   passes: re-sliced with the job's exact profile, one watertight body, no
   new slicer warnings and no risky clearance findings.

## Rules

- Never claim a fix that was not verified. Report the measurement and the
  failure instead.
- Never edit the source file; every repair writes a copy.
- Use only the tools offered. Do not invent coordinates or geometry: every
  number you state comes from a tool result.
- Coordinates are millimeters in the model file's own frame. Z values from
  the scans are measured from the model's lowest point (the bed).
- Keep answers short: what is wrong, where, what you propose, what it costs.
