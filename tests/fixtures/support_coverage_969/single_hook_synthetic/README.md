# SYNTHETIC fixture - single hook (issue #969)

This is NOT the real archived intake `724a5b63...`. That archive lives on
catduck's Unraid/Uranium host and was unreachable from the build VM when
this fixture was made. The geometry reproduces the scenario documented in
issue #963: a 12 x 15 mm bounding box (20 grading cells of 3 mm) whose
underside footprint occupies 10 cells; the support interface passes
through 16 of the 20 box cells (80 percent by the old bounding-box
method) and through all 10 footprint cells (10 of 10). The four box
cells with no interface are exactly the four #963 named as holding no
underside: (0,0), (2,4), (3,3), (3,4). The unflagged low_clearance
region from the same evidence is present and must be excluded from
grading, not graded. Object markers repeat per layer in OrcaSlicer
style, and the last layer is only the hook tip, so instance merging
is exercised. Regenerate with ../generate_fixtures.py; do not hand-edit.
