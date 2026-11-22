# When supports cannot help

A support needs room under the surface it holds: at least the first layer
plus the support's top and bottom Z distance. On the AD5X profile that is
0.24 mm plus 2 x 0.18 mm, so an underside less than about 0.6 mm above the
plate (or above another part) cannot hold even one support layer. A raft
does not change this; it lifts the support and the part together.

For a gap that small:

- Extend the underside down to the plate, so it prints as solid.
- Or sink the part by the gap, accepting the cut volume at the base.
- Or reorient so the surface no longer faces down.

Before choosing, check the underside with `underside_profile`: a wall
flaring out at 45 degrees or more prints without support, and a low gap
found there is not a problem.
