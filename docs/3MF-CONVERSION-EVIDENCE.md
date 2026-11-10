# 3MF conversion evidence matrix (#1110)

This page maps the six acceptance criteria of issue #1110 (foreign 3MF
conversion: designer intent, provenance, filament mapping, compatibility)
to what the code does today and to the tests that show it. It was rewritten
from the code on `main` at `fe1cb5d3` (2026-11-10), after review found
that an earlier version described behavior the code does not have. Work that
is still open is tracked in #1155.

## How to read the evidence

- A row cites a test only when that test exists and runs a production entry
  point: `inventory`, `model_job.slice_job`, `orca_adapter.slice_model`,
  `color_geometry.export_color_geometry` or `export_single_color_plate`,
  or the Discord summary formatter. Tests that call only an internal helper
  are not cited. Where a row has no such test, the row says so.
- "Production path" means a caller outside `tests/` reaches the code. A
  function that only tests call is named as such.
- Filament usage problems are warnings the operator can accept, not blocks
  (the owner ruling merged in #1190). Problems that are not filament data
  still refuse: no plate selected, decisions bound to a different source, a
  used filament with no slot mapping, and a mapping past the printer's slot
  capacity.

## Summary

| # | Criterion | Status on `main` |
|---|-----------|------------------|
| 1 | Setting intent | Implemented and tested on the slice path |
| 2 | Per-job provenance | Implemented and tested on the slice path |
| 3 | Per-plate filament usage | **Partial**: analysis is real, but nothing in production selects a plate, and Orca per-feature filaments are not read |
| 4 | Atomic complete remapping | **Not met**: the exporters are atomic, but the generic remapper has no production caller and several references are not remapped |
| 5 | Versioned compatibility | Implemented and tested on the slice path |
| 6 | Safety gates preserved | Implemented; filament conditions warn by owner ruling |

## Criterion 1: Setting intent

Requirement (#1110 item 1): tell deliberate designer settings from
inherited defaults using `different_settings_to_system` and an exact source
preset when available; when neither exists, intent is UNKNOWN, never
guessed; keep model decisions while the destination machine constraints
prevail.

| Requirement element | Implementation | Production-path tests |
|---|---|---|
| Deliberate changes come from `different_settings_to_system` | `author_settings.conversion_provenance` reads the print preset's entry (the first item of the list) of an Orca or Bambu project and labels each key `designer` or `inherited` | `tests/pipeline/test_author_settings.py::test_a_foreign_slice_attributes_designer_and_operator_values` |
| Exact preset comparison | `author_settings.read_project` reads the embedded print preset from `Metadata/filament_sequence.json`; when no changed-settings list exists, `conversion_provenance` compares project values against it | No production-path test; the comparison is covered only by unit tests in `tests/pipeline/test_author_settings.py` |
| UNKNOWN when evidence is missing | An Orca project with neither a list nor a preset records `unknown`. A PrusaSlicer project always records `unknown`: it embeds no preset and never writes `different_settings_to_system`, and a settings line of that name is not read | `tests/pipeline/test_author_settings.py::test_slice_records_intent_unknown_for_a_prusa_project_and_a_project_without_metadata` |
| A plain STL has no designer intent to guess | `conversion_provenance` records `not_applicable`, and each selected value gets a `selected_reason` (`operator`, `job`, `compatibility_fallback` and so on) | `tests/pipeline/test_author_settings.py::test_a_plain_stl_slice_records_no_designer_intent_and_attributes_selected_values` |
| Designer decisions kept, destination constraints win | `author_settings.carry` copies only the author-intent allowlist over the destination template, keeps only speeds lower than the profile, and falls back to the template for an enumeration Orca rejects. Machine, temperature and G-code keys are not in the allowlist | `tests/pipeline/test_author_settings.py::test_more_author_intent_is_kept_over_the_template`, `::test_lowered_speeds_reach_the_slice_profile`, `::test_bad_enum_never_reaches_the_slice_profile`, `::test_prusa_settings_reach_the_slice_profile`; whole-route audit: `tests/pipeline/test_foreign_audit_matrix.py::test_audit_whole_model_route`, `::test_audit_derived_plate_route`, `::test_silent_drop_regression_author_keys_named_in_the_differs_list_reach_orca` |
| One conversion pipeline | `author_settings.PRUSA_KEYS` and `ORCA_ENUMS` are built from the compatibility registry's rule parameters, and `orca_adapter.PROCESS_KEYS` from the `author-intent-allowlist` rule, so there is one key table | Table contents: `tests/pipeline/test_compat_registry.py::test_registry_tables_hold_the_expected_values` (a registry-structure test) |

## Criterion 2: Per-job provenance

Requirement (#1110 item 2): a structured JSON record with the artifacts of
the job, covering source preset, project, target and selected values,
origin, unresolved conflicts and source and destination versions, with a
concise summary in the existing Discord presentation and no standalone
report workflow.

| Requirement element | Implementation | Production-path tests |
|---|---|---|
| Structured per-setting record | `conversion_provenance` returns schema version 1 with `source_preset_value`, `project_value`, `target_value`, `selected_value`, `selected_reason`, `origin`, `reason`, plus `unresolved_conflicts` and `conversion_notes`. Only carried keys and keys whose selected value differs from the template are stored | `tests/pipeline/test_author_settings.py::test_a_foreign_slice_attributes_designer_and_operator_values` |
| JSON artifact beside the slice output | `author_settings.PROVENANCE_ARTIFACT` (`conversion_provenance.json`); `slice_model` writes it and embeds the same record in the job's slice provenance | `tests/pipeline/test_author_settings.py::test_slice_provenance_records_real_destination_versions_rules_and_artifact` |
| Destination slicer version | Read from the producing slicer's own identity line in its G-code (`slice_guard.slicer_version`), so a fallback slice records the fallback's version | Same test as the previous row |
| Source slicer version | `read_project` takes the Application metadata of an Orca project and the `; generated by` line of a PrusaSlicer project; the field stays null when the source states none | `tests/pipeline/test_author_settings.py::test_slice_provenance_records_prusa_rules_and_source_version_for_a_prusa_project` |
| Profile versions, null when unknown | Source: the embedded preset's version, else null with `source_profile_version_status` set to `not_recorded_by_source` or `not_applicable`. Destination: `destination_profile_identity` records the template's own `version` or the vendor version from `SOURCE.json`, else null with `not_recorded_by_template`; the Orca `setting_id` is stored separately as `destination_profile_id` | `tests/pipeline/test_author_settings.py::test_slice_provenance_records_real_destination_versions_rules_and_artifact`, `::test_slice_model_provenance_targets_the_destination_template` |
| Governing rules recorded | `governing_rules` lists every registry rule whose decision point the conversion reached; a rule whose declared family or versions do not cover the recorded pair is listed again in `rules_outside_declared_versions` | `tests/pipeline/test_author_settings.py::test_slice_provenance_records_rules_outside_their_versions_without_dropping_them` |
| Concise Discord presentation | `discord_print_summary.format_slice_summary` reports only consequential items (unknown intent, the count of unresolved conflicts) and the artifact name; it never dumps the record, a plain STL adds no conversion line, and a missing or broken record does not break the summary | `tests/hermes/test_print_messages.py::test_slice_summary_reports_only_consequential_conversion_provenance`, `::test_slice_summary_references_provenance_artifact_and_stays_concise`, `::test_slice_summary_omits_nonconsequential_conversion_provenance`, `::test_slice_summary_survives_a_missing_or_failed_plate_provenance_record` |

## Criterion 3: Per-plate filament usage

Requirement (#1110 item 3): account for object and part assignments,
triangle painting, layer changes, height ranges, and wall, infill, support,
interface and tower assignments; ask only for colors the selected plate
needs; retain original IDs; keep logical colors, loaded slots and nozzles
distinct; unknown usage preserves source data and blocks readiness if the
mapping cannot be validated.

**Status: partial.**

| Requirement element | What the code does | Production-path tests |
|---|---|---|
| Per-class, per-plate analysis | `source_colors.inventory` stores `plate_usage`, built by `_plate_usage` over `_USAGE_CLASSES` (`objects`, `painting`, `layer_changes`, `height_ranges`, `wall`, `infill`, `support`, `interface`, `tower`). Each class is `known`, `unknown` or `not_carried_by_format`, with its evidence. Original source filament IDs are kept | `tests/core/test_plate_usage.py::PlateUsageAnalysisTests::test_object_assignments_are_per_plate_with_original_ids`, `::test_triangle_painting_adds_painted_filaments_to_their_plate`, `::test_layer_changes_are_attributed_to_their_own_plate`, `::test_height_range_extruder_assignment_is_per_plate`, `::test_support_and_interface_designations_are_role_usage` |
| Painting | `_painting_usage` decodes `paint_color` and PrusaSlicer `mmu_segmentation` states per part | `tests/core/test_plate_usage.py::PlateUsageAnalysisTests::test_triangle_painting_adds_painted_filaments_to_their_plate`, `::test_unpainted_source_painting_class_is_known_empty`; `tests/pipeline/test_remap_exhaustive.py::test_mmu_segmentation_states_are_decoded_per_part` |
| Wall, infill and tower | PrusaSlicer: `_prusa_role_extruders` reads `perimeter_extruder`, `infill_extruder` and `solid_infill_extruder`. **Orca and Bambu projects: not read.** Their per-feature filament settings (wall, sparse infill, solid infill and wipe tower filament) are real, and no code in `print_handy/` reads them, so those classes are recorded as `not_carried_by_format` even though the format does carry them. The tower class is `not_carried_by_format` for every format | PrusaSlicer reading: `tests/core/test_plate_usage.py::PlateUsageAnalysisTests::test_prusa_role_extruders_are_read_from_the_prusa_config`. None for the Orca gap, which is open work |
| Unknown stays unknown | A class is `unknown` when the source should carry it but it cannot be read (a part with no extruder, a tool change with no extruder, unparsable height ranges, an out-of-palette support designation). A mismatch with the slicer-reported totals in `slice_info.config`, or a damaged record there, makes the plate unknown | `tests/core/test_plate_usage.py::PlateUsageAnalysisTests::test_layer_change_without_an_extruder_is_unknown_not_zero`, `::test_malformed_height_ranges_are_unknown_not_absent`, `::test_part_without_extruder_assignment_is_unknown`, `::test_slicer_disagreement_makes_the_plate_usage_unknown`, `::test_invalid_slicer_record_makes_the_plate_usage_unknown` |
| Questions only for the selected plate | `source_colors.prompts`, `canonical_prompts` and `grouped_prompts` scope by `selected_plate_ids`, and the Discord, chat and packet intake pass `job['selected_plates']`. **No production code writes `selected_plates`**; only tests do. In production the value is always empty, so every plate is asked about | No production-path test can cover this; the scoping tests call the prompt functions with a hand-built plate list |
| Readiness | `model_job._enforce_filament_readiness` runs only when `job['selected_plates']` is set, which production never does. The branch is therefore unreachable outside tests. Without a selection, `slice_job` still calls `validate_plate_usage_if_present` for every plate: a damaged slicer usage record becomes a warning the operator must accept before slicing. Absent source usage stays UNKNOWN and the destination slicer's output is authoritative, with no warning | Reachable path: `tests/pipeline/test_filament_warn_accept.py::SliceJobWarnAcceptTests::test_damaged_usage_claim_waits_for_acceptance_then_slices`. Selected-plate path, with the test supplying `selected_plates`: `::test_selected_plate_unknown_usage_waits_for_acceptance_then_slices` |
| Colors, slots and nozzles kept distinct | Usage records carry logical source filament IDs; decisions map IDs to loaded slots; `plate_readiness_problems` checks slots against `model_job.position_limit`, and a shared nozzle adds no slots | Slot capacity is covered only by unit tests of `plate_readiness_problems` |

Known gaps in the usage analysis are listed in #1155 (interface-only
support filaments, height ranges keyed by resource id instead of the Bambu
object index, extruder 0 in a range, objects with instances on two plates,
unmapped `slice_info` filaments).

## Criterion 4: Atomic complete remapping

Requirement (#1110 item 4): update every reference (paint, parts, feature
and support assignments, height ranges, layer changes, purge matrices and
vectors); preserve geometry and paint regions; leave the original immutable;
fail without partial output; clean up or merge only when usage is proven;
never silently drop colors.

**Status: not met.** Three things are true at once:

- The live export paths are `export_color_geometry` and
  `export_single_color_plate` (called from `model_job`), and
  `export_volume_plate` (called from `orca_adapter.export_geometry`). All
  write through `package_remap.write_package_atomically`. Nothing outside
  `color_geometry.py` calls `export_production_plate`.
- **`package_remap.remap_package` is experimental and has no production
  caller.** Only tests call it. The same is true of
  `package_remap.permute_project_settings`, `remap_prusa_member` and
  `painted_geometry.rewrite_paint_bytes`, which only `remap_package`
  reaches. Tests on those functions show what the remapper would do, not
  what a production export does.
- The exporters rebuild or collapse a package with their own code and do not
  use the generic remapper.

| Requirement element | What the code does | Production-path tests |
|---|---|---|
| Atomic write | `write_package_atomically` stages the package in a uniquely named temporary file in the target's folder (`tempfile.mkstemp`), validates it, flushes and `fsync`s it, then `os.replace`s it onto the target. Any failure removes the temporary file and leaves an existing target unchanged. With `source=` it refuses a target that resolves to the source; `remap_package` passes it, the exporters do not yet | `tests/pipeline/test_remap_exhaustive.py::test_export_single_color_plate_failure_after_staging_leaves_no_partial_output` (the replace fails after staging and nothing is left behind) |
| Original immutable | Exporters read the source and write the target only | The test above asserts the source bytes are unchanged |
| Rebuilt export refuses what it cannot translate | `export_color_geometry` refuses a source whose height ranges name extruders, an unmapped layer-change tool and an unmapped paint state, each by name. It names every member it does not carry in `dropped_members` and every dropped per-part setting in `dropped_part_settings` | `tests/pipeline/test_remap_exhaustive.py::test_rebuilt_export_refuses_height_range_extruders_by_name`, `::test_mmu_segmentation_export_refuses_a_paint_state_with_no_choice`, `::test_rebuilt_export_names_dropped_per_part_settings` |
| Paint | The rebuilt export translates PrusaSlicer `mmu_segmentation` to the chosen tools and remaps `paint_color` states for painted one-color plates. Support, seam and fuzzy-skin paint carries no filament and is not touched | `tests/pipeline/test_remap_exhaustive.py::test_mmu_segmentation_export_translates_paint_to_the_chosen_tools`, `tests/pipeline/test_color_geometry.py::test_painted_one_color_view_exports_each_part_plate` |
| Layer changes | Remapped in the rebuilt export and the single-color copy; the `color` attribute is kept because Orca's loader requires it | `tests/pipeline/test_remap_exhaustive.py::test_rebuilt_layer_change_keeps_the_color_attribute`, `::test_single_color_plate_carries_no_stale_filament_reference` |
| Single-color copy | `_declare_one_filament` keeps the used filament's entries in every per-filament array, remaps support designations, sets height-range and layer-change extruders to 1, and drops `slice_info.config` and embedded G-code with the drop named in the report. It now refuses a per-filament array whose length is neither 1 nor the palette size instead of keeping only its first entry | `tests/pipeline/test_remap_exhaustive.py::test_single_color_plate_carries_no_stale_filament_reference` |
| Opaque members | The production-plate copy keeps `Auxiliaries/` members byte for byte | `tests/pipeline/test_remap_exhaustive.py::test_production_plate_preserves_auxiliaries_members` (through `export_production_plate`, which has no production caller) |
| Purge matrix and vector | The generic remapper permutes `flush_volumes_matrix` and refuses `flush_volumes_vector` under a non-identity mapping because its layout is not established. The single-color copy keeps the used filament's pair | No production-path test |
| Embedded G-code and `filament_sequence.json` | The generic remapper refuses both under a non-identity mapping. The single-color copy drops the G-code (named) and keeps the sequence file byte for byte | No production-path test for the refusals |

Open gaps, all in #1155 and none fixed here:

- **PrusaSlicer package palette.** `remap_prusa_member` permutes a list only
  when its length equals the mapping length, and leaves temperature,
  extruder-role settings and the wiping-volumes matrix in source order. Not
  reachable in production because `remap_package` has no caller.
- **Feature filament keys.** Wall, sparse-infill, solid-infill and wipe-tower
  filament settings, per-object wall and support filament metadata and plate
  filament maps are not remapped by any path.
- **Unknown per-filament arrays** whose length equals the palette pass
  through the generic remapper unpermuted, because only keys in
  `color_geometry._PER_FILAMENT_KEYS` are permuted.
- **Implicit extruder 1.** A non-identity mapping does not make an unpainted
  face's implicit filament explicit first.
- **Real Bambu and Orca projects.** `package_remap.member_handling` has no
  class for `Metadata/plate_N.json`, `Metadata/cut_information.xml`,
  `Metadata/plate_N.gcode.md5` and similar standard members, so
  `remap_package` refuses them as unclassified.
- **Rebuilt export behavior change.** `export_color_geometry` refuses
  extruder-bearing height ranges instead of translating them.

[The remap inventory](3MF-REMAP-INVENTORY.md) lists the same members and
their handling by path.

## Criterion 5: Versioned compatibility

Requirement (#1110 item 5): explicit compatibility rules with source and
destination slicer versions, reasons and regression evidence; percent versus
millimeter line widths; incompatible enumerations; source hardware-only keys
and commands; profile provenance; no unexplained broad substitutions.

| Requirement element | Implementation | Production-path tests |
|---|---|---|
| Versioned registry | `print_handy/core/compat_registry.py`: `REGISTRY_VERSION` 1 and 32 rules in `RULES`, each with a unique id, kind, source and destination family and version spec, reason, parameters and named evidence | Registry structure: `tests/pipeline/test_compat_registry.py::test_registry_has_a_version_and_unique_ids`, `::test_every_rule_carries_the_required_fields`, `::test_every_evidence_reference_resolves`, `::test_registry_parameters_are_frozen`, `::test_the_shipped_orca_is_inside_every_rules_declared_versions` |
| Honest about unknown versions | `compat_registry.applies` matches an unknown version only against an unrestricted spec. `author_settings.governing_rules` records every applied rule and lists mismatches separately. A foreign project outside a rule's declared versions is refused after slicing; a plain STL never is | `tests/pipeline/test_author_settings.py::test_a_foreign_project_converted_outside_a_rules_declared_versions_is_refused`, `::test_a_plain_stl_is_never_refused_for_rule_versions_and_rules_are_recorded` |
| Percent versus millimeter line widths | Rule `orca-percent-line-width-to-bambu-mm` converts with the destination nozzle diameter for a Bambu Studio destination and fails closed when the diameter is unreadable | `tests/pipeline/test_orca_adapter.py::test_bambu_fallback_converts_percent_line_widths` |
| Incompatible enumerations | Rule `orca-enum-fallback` replaces an enumeration Orca rejects with the template's value | `tests/pipeline/test_author_settings.py::test_bad_enum_never_reaches_the_slice_profile` |
| Hardware-only keys and commands | Rule `foreign-machine-key-removal` removes machine, motion, temperature, bed and G-code keys from adopted copies; the target template supplies the machine setup | `tests/pipeline/test_foreign_project_adoption.py::test_derived_plate_reaches_orca_with_the_job_profile_not_the_foreign_one`, `::test_plugin_keys_are_stripped_from_an_imported_project` |
| Profile provenance | See criterion 2 | See criterion 2 |
| No unexplained substitution | Each rule names its keys, values or profile. `compat_registry.get_rule` raises for an unknown id | `tests/pipeline/test_compat_registry.py::test_registry_lookup_refuses_an_unknown_id` |

[Printer profiles](PRINTER-PROFILES.md) holds the rule table and the
decisions deliberately left out of the registry.

## Criterion 6: Safety

Requirement (#1110 item 6): loaded filament and printer calibration outrank
designer temperature and flow values; validate the purge tower against the
destination; unsupported features or unresolved mappings keep automated
print readiness from proceeding; preserve the existing support, launch,
authorization, temperature and capacity gates.

The wording "block" in the requirement is superseded for filament usage
data by the owner ruling in #1190: usage the source could not establish
warns, and the operator can accept the warning. The record of the
acceptance is bound to the exact warning texts, the source hash and the
plate selection, and it authorizes one slicing run.

| Requirement element | Implementation | Production-path tests |
|---|---|---|
| Loaded filament outranks designer values | The author-intent allowlist carries no temperature or flow-ratio key, and `orca_adapter` takes temperatures from the job's loaded filament profile and nozzle | `tests/pipeline/test_foreign_audit_matrix.py::test_audit_whole_model_route`, `::test_audit_derived_plate_route` |
| Calibration outranks designer values | Rule `foreign-machine-key-removal`, plus the protected-settings baseline that refuses a slice whose start or end G-code differs from the approved baseline | `tests/pipeline/test_port_contracts.py::test_shipped_protected_baseline_matches_the_orca_machine_template` |
| Purge tower validated against the destination | Rule `wipe-tower-bed-clamp` clamps the tower inside the bed using the job's tower width; `orca_adapter.ifs_purge_settings` builds the purge settings for the destination | `tests/pipeline/test_author_settings.py::test_slice_model_clamps_with_the_job_tower_width` |
| Unresolved mappings | When the readiness gate runs, a used filament with no slot mapping, a mapping past slot capacity, and decisions bound to another source refuse with a named reason. Unknown or damaged usage warns and waits for acceptance | `tests/pipeline/test_filament_warn_accept.py::SliceJobWarnAcceptTests::test_damaged_usage_claim_waits_for_acceptance_then_slices`. The refusal for an unmapped filament is covered only by a call to `_enforce_filament_readiness` (`GateTests::test_hard_problem_refuses_even_when_warnings_were_accepted`), not by a production-path test, and in production the selected-plate gate does not run (criterion 3) |
| Existing gates kept | The support, launch, authorization, temperature and capacity gates are outside this work's code; the support regression suites below run unchanged | `tests/pipeline/test_intake_support_precheck.py`, `tests/pipeline/test_intake_support_unverified_968.py`, `tests/test_supports_decision.py`, `tests/test_modeled_supports.py` |

## Coverage that predates #1110

#1110 names existing work that is documented here, not rebuilt:

- #711 and #718 were addressed by merged #713; #716 and #717 by merged #723,
  which also added core color corpus coverage for #714.
- The foreign conversion audit matrix
  (`tests/pipeline/test_foreign_audit_matrix.py`), the adoption tests
  (`tests/pipeline/test_foreign_project_adoption.py`) and the ForgeBridge
  edge cases (`tests/pipeline/test_forgebridge_edge_cases.py`) establish the
  kept, replaced and dropped contract summarized in
  [Printer profiles](PRINTER-PROFILES.md). Print Handy follows ForgeBridge's
  published rules; no PrintNomad code, fixtures or dependencies are used.
- `model_job.filament_usage_by_selection` (slicer-reported per-plate grams
  by selection) predates the usage analysis.

## What remains

Everything listed under criteria 3 and 4 above, plus the per-plate usage
under-counts and the Train 1 follow-ups, is tracked in #1155. #1110 stays
open until those land. If a gap closes, update its row here in the same pull
request and cite a test that runs the production entry point.
