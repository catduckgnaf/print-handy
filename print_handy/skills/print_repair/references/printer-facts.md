# Printer facts that bite

Flashforge AD5X with ZMod:

- Usable print area 220 x 220 mm, 220 mm tall. A repair that adds a base or
  a gusset must keep the part inside it.
- Filament comes from the IFS, four slots. A single-color G-code prints on
  tool T0, and ZMod maps T0 to slot 1 unless the start map says otherwise
  (#1776). A geometry repair never changes which slot a job uses.
- First layer 0.24 mm on the default profile. Pass the job's own layer and
  first layer heights to the scans.
- Supports keep a top and bottom Z distance of about 0.18 mm each, which
  sets the smallest gap a support fits in (see support limits).

Every repaired file goes back through the normal intake and the owner's
confirmation before anything prints.
