# Worked example: the 2026-11-21 couch organizer

The upload was a couch arm organizer about 214 x 135 x 133 mm (88,172
triangles). The slicer wanted supports and warned about floating regions
without saying where.

Diagnosis, with this skill's tools at 0.2 mm layers:

1. `island_scan` found four floating regions on one layer at Z 92.2 to
   92.4, from 154 to 347 mm2 each, with nothing under them.
2. `overhang_scan` found a cantilever about 26 mm deep starting at Z 27
   (about 1,180 mm2; `underside_profile` reads it as a flat underside) and
   the deck ceiling at Z 93, a large flat span anchored on both sides that
   bridges.
3. `clearance_report` flagged a 0.562 mm low clearance near the base.
   `underside_profile` showed a 53 degree wall flare there: a false
   positive that needed nothing.
4. The deck's underside sagged up to 0.72 mm below its own plane (about
   720 mm3): a defect, trimmed back to the plane.

Fixes: the sag trimmed, a 45 degree gusset under the cantilever, and the
floating regions given material to rest on. The verified result printed
without supports in 10 h 41 m and 302 g with no slicer warnings, and
`island_scan` on it finds no islands and `overhang_scan` no cantilever.
