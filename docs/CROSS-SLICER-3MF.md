# Cross-slicer 3MF: sources, conversion, export and accounting

[Documentation home](README.md) · [Printer profiles](PRINTER-PROFILES.md) ·
[3MF conversion evidence](history/3MF-CONVERSION-EVIDENCE.md)

Print Handy imports 3MF projects made by other slicers, translates what
can honestly survive the move to your printer, and can hand the result
back as an editable project. This page says exactly what happens per
source, and what is not claimed. The machine-readable version of this
table is `print_handy/core/source_capabilities.json`; fixture honesty
(genuine producer output versus spec-synthetic construction) is recorded
in `tests/corpus/producer-fixtures/PROVENANCE.json`.

## What kind of file is this?

Every 3MF is classified by its content, never its name, into a producer
kind and one user-facing class:

| Class | Meaning |
|---|---|
| Editable project | Geometry plus a slicer project Print Handy can translate or remap. |
| Geometry only | A mesh with no process settings to translate; destination defaults apply. |
| Sliced only | Toolpaths with no editable geometry: reviewable, never re-sliced or exported as a project. |
| Unsupported / corrupt | Not readable as a usable 3MF; the refusal names why. |

## Source capability table

| Source | Settings translation | Editable export | Evidence |
|---|---|---|---|
| OrcaSlicer | Native project, author intent carried by the established allowlist | Yes | Real destination slice in CI; a genuine OrcaSlicer 2.5.1 CLI fixture is in the corpus (its Application metadata names the Bambu base; the OrcaSlicer metadata element is the producer proof, and readers honor it over the Application name) |
| Bambu Studio | Native lineage project; Bambu Studio is also a supported fallback executor | Yes | Real Bambu Studio A1 slice in CI, plus the genuine Cura, PrusaSlicer and OrcaSlicer projects sliced by the real Bambu Studio; `Slicer test (bambu)` is a required check; a genuine Bambu Studio 02.08.02.61 CLI fixture is in the corpus and slices through the production rebuild |
| PrusaSlicer | Slic3r config translated through the versioned compatibility registry | Yes | Real Orca slice; a genuine PrusaSlicer 2.8.1 CLI fixture is in the corpus (its CLI export carries geometry and a model config, no settings snapshot) |
| Cura | Validated subset only: layer heights, wall count, top/bottom shell, infill density/pattern, support enable/structure/placement, brim, seam. Each range- or enum-checked | Yes | A genuine UltiMaker Cura 5.8.1 fixture is in the corpus (saved through the real application's GUI; Cura has no headless project-save operation), plus the spec-synthetic layout fixture through the production reader plus a real Orca slice |
| Anycubic Slicer Next | Detection only, on the X-ACNext slice-info marker; no settings translation is claimed | As a Bambu-lineage project | Parser-level |
| Plain core 3MF | None (no settings carried) | Yes | Real destination slice |
| Sliced-only G-code.3MF | None | Refused, with the reason stated | Parser-level |

Destination machine settings always win: the destination printer's
machine profile, firmware commands, temperatures, toolhead and slot
identities and your Spoolman selections are authoritative. A source's
start/end G-code, machine identity, accelerations and extruder offsets
are never carried across, from any producer.

## Export editable 3MF

The intake menu (chat) and the Web UI job page offer **Export editable
3MF** beside Slice and Download. The export strips stale slice
artifacts (old plate G-code, slice records and checksums), preserves
the source SHA-256 in its provenance record, and writes atomically.
It never starts a print, sends a machine command or touches spool
inventory. A sliced-only file is refused with the reason, not dressed
up as a project.

## Conversion report

Each sliced job's settings report (Web UI JSON download) has a readable
twin at the job's conversion report page: preserved, translated,
replaced by destination, dropped and unknown decisions, rendered from
the same provenance record as the JSON, never a second version of the
truth.

## The wired-in surfaces

Three surfaces sit on the statistics, intake and feasibility library
code, all reached from the places jobs already live:

- **Per-plate statistics** (`core/plate_statistics.py`, glued by
  `core/slice_result.py`): the slice-result card (`/print-slice-result
  [job]` in chat, and the Slice result link on the Web UI's sliced-jobs
  page) shows the plate's actual thumbnail when the plate file carries
  one, the layer count, the estimated time and the estimated grams per
  logical filament and per mapped slot, from Orca's
  `--export-statistics` file when one exists, else from the G-code
  comments, recording which. Figures a source does not state stay
  unknown. Spool suggestions are exact material and color matches with
  their confidence named, never a closest-color guess. Slicing alone
  never deducts inventory.
- **External sliced jobs** (`core/external_intake.py`): the
  `[external_intake]` setting names the one allowlisted directory; the
  Web UI's External intake page shows draft cards for stable files in
  it. A card is a draft: nothing prints or charges from a scan. The
  page's Mark as printed confirmation records a physical print exactly
  once per file, printer and print id: a repeat, a print id already in
  the print accounting ledger, and an unconfirmed spool charge are
  refused rather than charged twice.
- **Snapmaker U1 feasibility** (`core/u1_feasibility.py`): the U1
  feasibility link on the sliced-jobs page (and from the slice-result
  card) renders the read-only report for a job's source: per plate,
  the filament count against the U1's four toolheads, support-only
  filaments and interleaved-layer evidence. It collapses no colors and
  plans no swaps. The U1 stays hardware-unverified: no U1 has printed
  through Print Handy, and the report is not a print plan.

## Install paths

Nothing here changes installation: Docker, Unraid and the Home
Assistant add-on remain the three supported paths, and no new mandatory
dependency was added.
