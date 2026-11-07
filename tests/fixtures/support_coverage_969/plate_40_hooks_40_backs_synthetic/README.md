# SYNTHETIC fixture - 40 hooks + 40 backs (issue #969)

This is NOT the real archived intake `16fd2142...`. That archive lives on
catduck's Unraid/Uranium host and was unreachable from the build VM when
this fixture was made. The plate reproduces the scenario documented in
issue #969: 40 copies of one object (Hook) and 40 copies of another
(Back) on one plate, every region failing coverage - hooks at 33 and
44 percent (inside the recorded 25-50 percent band) and backs at
77.8 percent (inside the recorded 75-80 percent band). Object markers
repeat per layer in OrcaSlicer style: 163 marker blocks must resolve
to exactly 80 instances, none of them unverified. Regenerate with
../generate_fixtures.py; do not hand-edit.
