# Issue #998 reference CAD corpus: 2014 Mac mini, 19-inch 2U rack mount

Reference run: https://github.com/catduckgnaf/print-handy/issues/998

This directory preserves the real CadQuery generator, original preview/drawing scripts,
and the original assembly/printing instructions from the ChatGPT session, plus **generated
binary artifacts** (STL, STEP, PNG) checked into this branch by a dedicated GitHub
Actions fixture-generation workflow.

## Reproduction

Use Python 3.11, CadQuery 2.8.0, VTK 9.6.2, Matplotlib 3.10.8,
Trimesh 4.11.1. From this directory:

```sh
python generate_rack_mount.py
xvfb-run -a python make_previews.py
python make_dimension_diagram.py
python validate_fixtures.py
```

The CI workflow `.github/workflows/issue-998-cad-fixture.yml` generates the
binaries on its dedicated fixture branch and commits them in Git format, together
with `SHA256SUMS.txt`, `WARNING_NOT_VALIDATED.txt`, and a reproducible ZIP.

The original ChatGPT ZIP was
`2014_Mac_Mini_19inch_2U_PLA_Rack_Mount_v1.zip`, SHA-256
`72da79a42d35f15e28d61091807903802f4694ee72a5ae6562e00924dbaea437`.
**Generated CI binaries are not claimed to be byte-identical to that original ZIP.**
Different OpenCascade/VTK/ZIP versions can change bytes even if dimensions
are the same. Preserve the original hash for provenance; compare geometry,
part inventory and measured constraints.

## IMPORTANT: Reference includes known issues

This is an intentionally **unvalidated reference prototype**, not a validated
mount design. Use it to test whether Print Handy can recognize and correct
issues instead of saying everything is printable.

1. Rack ears have Z height **88.5 mm**, below nominal 2U pitch (88.9 mm)
   but above common faceplate clearance (~88.11 mm). Could interfere with
   rack neighbors. Flag, revise, re-render and revalidate.
2. The original assembly script translates the center tray **+10 mm Z**.
   Its bolt holes are nominally z0..10 while arms are z0..10, so the parts
   are not correctly seated in the assembly export. The STL parts themselves
   remain at their print-frame z0. This discrepancy MUST be discovered by
   assembly interface/collision validation; do not silently advertise a
   mechanically validated assembled STEP.
3. The center tray footprint is 214 mm square on a nominal 220 mm bed,
   just 3 mm margin per edge before toolhead exclusions or adhesion geometry.
4. Structural PLA creep, screw-bearing stresses, and physical load are
   untested. Never infer certified strength from manifold meshes.

## Expected Print Handy feature behavior

The automated acceptance run should take the original natural-language user
prompt, research device/rack dimensions with confidence/provenance, propose
visual alternatives, choose a sensible default, generate multiple parametric
parts and an assembly, validate joint registration in common coordinates,
catch the known assembly Z mismatch and rack panel clearance, show true
measured views, provide *progress updates while working*, then package
manufacturing files, editable sources, BOM and instructions. Only explicit
approval may pass artifacts into ordinary slicing/printing gates.

Reference source scripts are CC0-1.0. No hardware load test was performed.
