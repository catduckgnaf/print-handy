# 3MF remap inventory: current export preservation boundary

This is an implementation inventory, not evidence that every reference is remapped. The existing `export_color_geometry` and `export_single_color_plate` functions build reduced output packages rather than rewriting an arbitrary source archive.

## Observed package members

Existing synthetic coverage in `tests/pipeline/test_color_geometry.py` constructs packages with:

- `[Content_Types].xml`, `_rels/.rels`
- `3D/3dmodel.model` and external object model members under `3D/Objects/*.model`
- `3D/_rels/3dmodel.model.rels`
- `Metadata/model_settings.config`
- `Metadata/project_settings.config`

Other readers in `source_colors.py` recognize `Metadata/Slic3r_PE.config`, `Metadata/Slic3r_PE_model.config`, `Metadata/slice_info.config`, layer G-code metadata, and paint attributes (`paint_color`, `mmu_segmentation`). The 3MF inventory also reads model component/build references and source object/part settings.

## Current exporter behavior and unsupported preservation classes

`export_color_geometry` traverses source component/build references to derive geometry and emits a new package containing only `[Content_Types].xml`, `_rels/.rels`, rebuilt `3D/3dmodel.model`, rebuilt `Metadata/model_settings.config`, and optionally remapped layer G-code. It does not preserve arbitrary source package members or relationships. Its mesh-leaf handling does not establish complete semantic rewriting of every paint representation, part/feature/support assignment, height-range reference, purge matrix/vector, or slicer-specific opaque metadata. Those classes remain unsupported for a generic all-reference remap until individually inventoried, transformed, and regression-tested.

- `export_single_color_plate` is also a derived-geometry/package exporter; it is not an in-place or generic ZIP reference remapper. The production-package color patch path in `color_geometry.py` copies ZIP payload members and byte-patches one project filament color; `test_production_plate_recolor_preserves_unrelated_zip_members` verifies a successful nonidentity slot mapping preserves unrelated opaque and preview member payloads byte-for-byte. This is a one-field production color selection path, not a general reference remap.

## Preservation contract to establish before conversion changes

- On validation/remap failure, source bytes and any pre-existing destination bytes must remain identical.
- A successful supported transformation must preserve unrelated ZIP members byte-for-byte.
- Source input remains immutable.
- Unsupported or ambiguous reference classes must fail closed rather than be silently dropped.

## Regression coverage status

Existing `tests/pipeline/test_color_geometry.py` verifies source immutability and that validation failures do not create a new output. It does not yet verify preservation of a pre-existing destination or unrelated members on a successful generic remap. These are the next focused regression cases. No licensed PrintNomad code or fixtures are used.
