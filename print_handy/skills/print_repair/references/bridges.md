# Bridges

A flat ceiling held at both ends can bridge: the slicer stretches strands
from one anchor to the other without support. `overhang_scan` reports such a
ceiling as one large overhang on a single layer; that alone does not make it
a problem.

A ceiling can bridge when:

- it is anchored on two opposite sides along the direction the strands run;
- the span between the anchors is moderate (tens of millimeters prints well
  on a tuned printer; longer spans sag more);
- some sag is acceptable, typically because the underside is hidden inside
  the part.

It needs support, a gusset or a different orientation when it is anchored
on one side only (a cantilever), when its first layer is floating (see
`island_scan`), or when its underside is a visible surface that must stay
flat.
