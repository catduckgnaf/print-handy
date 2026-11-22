# Orientation trade-offs

Reorienting can remove overhangs, but it moves the problem as often as it
solves it: what faced sideways now faces down, the base area changes, and
the print time follows the new height and support volume.

Measure the candidates before recommending one: run `overhang_scan` and
`clearance_report` on the rotated copy and compare print time and grams from
a slice. On the 2026-11-21 couch organizer the best orientation saved only
2.5 h of 18.5 h, while fixing the geometry in its original orientation gave
a print without supports of 10 h 41 m.

Prefer the orientation the designer chose unless the measurements show a
clear gain; it usually reflects how the part is used and where it must be
strong and smooth.
