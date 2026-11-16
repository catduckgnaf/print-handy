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
| OrcaSlicer | Native project, author intent carried by the established allowlist | Yes | Real destination slice in CI |
| Bambu Studio | Native lineage project; Bambu Studio is also a supported fallback executor | Yes | Real Bambu Studio A1 slice in CI (that check is not branch-protection required) |
| PrusaSlicer | Slic3r config translated through the versioned compatibility registry | Yes | Real Orca slice; a genuine PrusaSlicer 2.8.1 CLI fixture is in the corpus (its CLI export carries geometry and a model config, no settings snapshot) |
| Cura | Validated subset only: layer heights, wall count, top/bottom shell, infill density/pattern, support enable/structure/placement, brim, seam — each range- or enum-checked | Yes | Spec-synthetic fixture through the production reader plus a real Orca slice; no genuine Cura-produced fixture yet (Cura has no headless project-save operation; the gap is tracked in the capability manifest) |
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
the same provenance record as the JSON — never a second version of the
truth.

## Per-plate statistics

After a verified slice, the job's statistics artifact shows the plate
thumbnail when one exists, layer count and estimated time when known,
and **estimated** grams per logical filament and per mapped slot.
Estimates come from Orca's statistics output when the pinned binary
emits it, else from verified G-code comments; the artifact records
which. Figures a source does not state stay unknown. Slicing alone
never deducts inventory, and spool suggestions are exact material/color
matches — never a closest-color guess.

## External sliced jobs (opt-in)

An operator may configure one watched import directory. New stable
files there become draft job cards — nothing prints, sends or charges
on appearance. Only an explicit **mark as printed** confirmation from
the operator records accounting, exactly once per physical print; a
print already recorded (including one Print Handy launched) cannot be
confirmed again.

## Snapmaker U1

The U1 stays hardware-unverified: no U1 has printed through Print
Handy. For a project using more logical filaments than the U1's four
toolheads, Print Handy produces a read-only feasibility report (per
plate: filament count, support-only filaments, interleaved-layer
evidence). It collapses no colors and plans no swaps.

## Install paths

Nothing here changes installation: Docker, Unraid and the Home
Assistant add-on remain the three supported paths, and no new mandatory
dependency was added.
