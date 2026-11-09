# 3MF remap inventory: exhaustive remapping after Phase C

This inventory records, per package member and reference type, what the
remap paths do after Phase C of the #1110 completion program (criterion 4:
atomic complete remapping). It supersedes the pre-Phase-C "current
preservation boundary" note, which described exporters that built reduced
packages and carried the remaining members stale or dropped them silently.
The full audit behind this state is the Phase C reference inventory
(R01–R28); this page is the shipped behavior.

## Atomic remap shell and member classification

Every package write the remap paths make is staged: the new package is
written to a staging file beside the target, validated (zip integrity, plus
a cross-reference consistency re-read for the generic remapper), and only
then replaces the target. A validation failure leaves the source bytes and
any pre-existing destination bytes identical, and the source is always read
read-only. On success, members a path does not rewrite are preserved
byte-for-byte (copy paths) — the rebuilt geometry export is the exception
below, and it names every source member it does not carry in its report's
`dropped_members`.

## Member classification and the object-id policy

Before any byte is written, the generic package remapper
(`print_handy/core/package_remap.py`, registry rule
`remap-member-classification`) classifies every source member:

- **Remapped**: `Metadata/project_settings.config`,
  `Metadata/model_settings.config`, `Metadata/Slic3r_PE.config`,
  `Metadata/Slic3r_PE_model.config`, `Metadata/layer_config_ranges.xml`,
  `Metadata/custom_gcode_per_layer.xml`, `Metadata/slice_info.config`, and
  the `.model` members (paint attributes only; geometry bytes otherwise
  untouched).
- **Preserved byte-for-byte**: `[Content_Types].xml`, relationship parts,
  thumbnails and preview images, `Auxiliaries/` members, and opaque vendor
  records — members with no filament reference this converter knows.
- **Refused by name**: embedded plate G-code (`Metadata/plate_N.gcode`;
  bare G-code is never rewritten and its tool commands would contradict the
  new numbering), `Metadata/filament_sequence.json` under a non-identity
  mapping (its sequence semantics are not established, and its embedded
  preset record is provenance evidence about the source), and any member
  with no classification at all.

Object-id policy: **package remaps never renumber object ids.** The generic
remapper and the copy exporters (single-color, production) preserve ids
exactly; ids change only through the generic remapper's explicit caller
object map, which translates every object-keyed reference (height ranges,
model settings, plate instances) with the same map. The rebuilt geometry
export (`export_color_geometry`) is the one path that renumbers objects —
it assigns fresh sequential ids — so it carries no object-keyed member it
cannot translate: it refuses a source whose height ranges name extruders
(see below) and names every other uncarried member in `dropped_members`.

## Palette, per-filament arrays and purge references

A remap permutes the project palette (`filament_colour`, `filament_type`)
and every per-filament array by the same mapping, in destination order
(registry rule `per-filament-array-permutation`). A one-filament collapse
keeps the *used* source filament's entries — its temperatures, density,
flow and profile id — not entry 1's; the slot recolor targets that same
index before the collapse, so the surviving color is the slot's verified
color. An array whose length matches neither the palette nor a known shape
refuses the remap.

The purge matrix `flush_volumes_matrix` is the palette-count-squared
row-major matrix of purge volumes between ordered filament pairs (the shape
`orca_adapter.ifs_purge_settings` generates), and is permuted on both axes;
the diagonal stays zero and a size mismatch refuses (registry rule
`flush-matrix-permutation`).

The purge vector `flush_volumes_vector` is **not** permuted: its layout
semantics are not established from this repository (only "two entries per
filament" in one synthetic fixture), and guessing a memory layout is not an
option. A remap that would have to permute it refuses and names the key
(registry rule `flush-vector-unestablished-refusal`). The identity mapping
needs no permutation and passes it through; a one-filament collapse keeps
the used filament's own pair, the one slice of the layout the fixture
establishes.

Support designations inside the package (`support_filament`,
`support_interface_filament`, and the Prusa `support_material_extruder`
pair) are rewritten through the mapping like any other reference (registry
rule `support-designation-remap`). A designation whose filament the remap
removes falls back to 0 — the object's own filament — with the fallback
named in the remap report; a designation never survives pointing past the
new palette. Slice-time adoption keeps its own end state (the job's values
win), as before.

## Prusa mmu_segmentation paint translation

PrusaSlicer's `slic3rpe:mmu_segmentation` triangle attribute carries the
same TriangleSelector encoding and state numbering as Bambu/Orca
`paint_color` (the corpus `prusa-mmu-project` fixture's `"8"` is state 2 in
both). It is decoded per state by `painted_geometry` — per-part usage
(Phase B) counts its states exactly, no longer conservatively as every
palette filament. The destination slicer (Orca) reads `paint_color`, so a
remap decodes the Prusa states, remaps them through the same paint table,
writes them as `paint_color`, and drops the Prusa attribute (registry rule
`mmu-segmentation-paint-translation`). A painted state with no slot choice
refuses, exactly as an uncovered `paint_color` state does. Support, seam
and fuzzy-skin paint carry no filament identity and are preserved verbatim
by the remap paths.

Per-triangle core material properties (`pid`/`p1`/`p2`/`p3`) name material
resources the rebuilt package does not carry, so the rebuilt export strips
them from its triangles instead of emitting a dangling reference; the face
colors themselves remain disclosed through the inventory's
`face_colors_ignored` note, as before.

## Height ranges and object ids

A height range's `<option opt_key="extruder">` is a filament reference
keyed by object id (registry rule `height-range-extruder-remap`). The
generic remapper translates the value through the mapping — and the object
id through the caller's object map, when one is given — and refuses a
value or object it does not know. The single-color copy, which preserves
object ids, rewrites the values to its one filament. At slice-time
adoption, where the plate was already renumbered by its export, a value
past the job's filament count clamps to the job's last filament (the
`_prusa_model_settings` precedent); the option and the range's other intent
(layer heights and other options, restricted as before) are preserved —
the value is never carried verbatim past the palette. The rebuilt geometry
export renumbers object ids, so it refuses a source whose height ranges
name extruders rather than drop the member silently.

## Per-layer tool changes, usage and the remaining members

Per-layer tool changes (`Metadata/custom_gcode_per_layer.xml`): the
standalone archive remap already refused unmapped tools; the in-export path
now refuses the same way instead of dropping the change, and a kept entry's
`color` attribute — the *source* filament's color — is stripped rather than
carried as if it described the mapped tool. Pauses and custom entries are
reported from the inventory, as before. The single-color copy remaps the
member to its one filament (or drops it, named, when it carries only
pauses/custom entries).

Sliced usage (`Metadata/slice_info.config`): the generic remapper remaps
its filament ids and refuses an unmapped one. The single-color copy drops
the member and names the drop in its report (`dropped_entries`) — usage is
re-derived by slicing the derived plate, and the source's records remain
the readiness evidence (Phase B reads the source, not the copy). Embedded
plate G-code drops the same named way in the single-color copy and refuses
in the generic remapper. The Prusa members
(`Slic3r_PE.config`/`Slic3r_PE_model.config`) are translated to the
one-filament numbering in the single-color copy — palette lists collapse
to the kept filament's entry, support extruders follow it, per-object
extruders become 1 — because the derived plate stays the author's package
for a Prusa source and the slice-time adoption translates and removes
those members from its own copy in any case. `Metadata/filament_sequence.json` is
preserved byte-for-byte by the single-color copy and named in
`preserved_unremapped_entries`: its only reader reads the embedded print
preset as provenance evidence about the source, and no sequence semantics
beyond that record are established (the Phase C reconciliation of audit
row R26 with Phase A).

Per-part settings outside the author-intent allowlist are still not carried
by the rebuilt export — that restriction is the registry's
`author-intent-allowlist` — but the drop is no longer silent: the export
report's `dropped_part_settings` names every dropped key per part, cutters
included.

The production copy (`export_production_plate`) preserves the whole member
set, `Auxiliaries/` included; it still byte-patches exactly one filament
color and nothing else.

## Regression coverage

`tests/pipeline/test_remap_exhaustive.py` pins the Phase C behavior:
mmu decoding and translation, the generic remap's permutation of every
reference class, the flush-vector refusal, atomicity (source and
pre-existing destination preserved on failure, unrelated members
byte-for-byte on success), the collapse keeping the used filament's
entries, the single-color copy carrying no stale reference, the rebuilt
export's named refusals and named drops, and the production member set.
Neighboring suites: `tests/pipeline/test_color_geometry.py`,
`tests/pipeline/test_derived_plate_filament_arrays.py`,
`tests/pipeline/test_foreign_project_adoption.py` (its list-shape and
layer-range test was updated deliberately: the ranges extruder is now
remapped, not carried verbatim), `tests/core/test_layer_gcode.py`,
`tests/core/test_plate_usage.py`, and the corpus `prusa-mmu-project` case.
No licensed third-party code or fixtures are used.
