# Geometry audit acceptance map (#1152)

Baseline: main `320ffa2f`. This map distinguishes exact synthetic regression coverage from general geometric proof. No physical printer validation is claimed.

| Area | Production path and regression evidence |
| --- | --- |
| 1. Transformed size | `orca_adapter.source_triangles`, `geometry_layout.load_parts`; `tests/core/test_geometry_soundness.py` transformed envelope tests |
| 2. Scale and undo | `edit_plan`; geometry soundness percent composition, inch conversion, rotated-height tests |
| 3. Rotation envelopes | Geometry soundness vertex-envelope tests; stepped lay-flat regression counts only the outermost supporting plane, not all parallel terraces |
| 4. Placement | Geometry soundness target-bed centering, origin and Z landing tests; existing cutter regression from #1137 |
| 5. Winding and volume | `tests/core/test_mirrored_transforms.py`: build, component, composed reflections, volume export, color export; mirrored painted flattening now explicitly refuses rather than silently exporting inward normals or permuting paint trees |
| 6. Units and STL parsing | Geometry soundness 3MF unit and binary `solid`-header tests |
| 7. Numeric range | Geometry soundness distant-coordinate export test |
| 8. Support classification | Geometry soundness nonuniform scale/normal tests; #1137 wide sawtooth and square/round chamfer regressions |
| 9. Displayed versus placed size | Geometry soundness placed-dimension/rotated-height tests; #1137 Reorient regression |
| Fit exclusions, height and brim | `tests/core/test_fit_probe_parity.py` drives the real capacity function with fake Orca: native polygon retention, overlap refusal, nonoverlap, strict single/multi height, explicit brim kinds, lookup failure as unknown |

## Audited boundaries and known limits

* Fit probes are not final toolpath certification. Orca's arranged project retains the active printer's native exclusion polygon. The independent arranged-envelope check conservatively refuses overlap, including feature reserve. It can refuse a concave part or separated copies whose combined bounding rectangle crosses an exclusion even when no triangle does. The final slicer/toolpath safety gates remain mandatory. Multi-plate fit uses rectangular envelopes and two cardinal yaws only; it is not a continuous placement solver.
* `export_color_geometry` cannot safely flatten a reflected painted mesh: paint subdivision trees depend on vertex ordering. It refuses that case before writing a result. Two reflections compose to an orientation-preserving transform and remain supported. No unverified paint-tree permutation is attempted. Other per-color source-reader paths have not received a complete reflection audit in this follow-up; their normal-sensitive consumers must not be described as independently certified by these tests.
* Split-piece volume sums are **not comprehensively audited** by this follow-up. Existing split/topology tests do not prove conservation for every connector, clearance or Boolean operation. Material removed by kerfs, bores and sockets or added by joinery makes a blanket sum-equals-original assertion invalid. No new conservation guarantee is claimed.
* Assembly collision coverage is in `tests/core/test_assembly_declarations.py`: flush contact, separated parts, crossing slabs, identical placements, aligned overlaps, partial placement, and the 0.01 mm contact-band unknown result. The implementation is a floating-point witness search, not a general exact solid-intersection proof. Degenerate/nonmanifold input, very thin coplanar overlaps and subresolution overlap remain limits, not certified clearance. No collision gate has been relaxed by this work.
* Lay-flat now ranks actual supporting-plane area for each sampled face-normal direction. Normals are grouped at two decimal places and contact uses 0.01 mm tolerance. This is not a continuous stability solver or a guarantee of adhesion; thin feet, curved surfaces and geometry below the tolerance remain limitations. Support checks still apply to the resulting orientation.
* The wedge-root defect has a synthetic regression. No independently verified real-model reproducer was identified in this lane; #1151's requested real-model corpus follow-up therefore remains open rather than being invented or silently waived.

## Verification status

Regression suites are run only in GitHub Actions. The existence of a test above is not a claim that the current head passed. Consult the exact-head check results on PR #1175 and the separate reslice acknowledgment PR #1174. Corpus expectations have not been re-recorded or silently changed.
