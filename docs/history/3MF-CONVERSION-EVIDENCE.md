# 3MF conversion evidence matrix (#1110)

This page maps the six acceptance criteria of issue #1110 (foreign 3MF
conversion: designer intent, provenance, filament mapping, compatibility)
to what the code does today and to the tests that show it. It was rewritten
from the code on `main` at `fe1cb5d3` (2026-11-10), after review found
that an earlier version described behavior the code does not have, and
updated from `main` at `64407bcf` for the usage reading of #1203, the
filament fallbacks of #1213 and the #1155 remainder (feature filament keys,
unknown per-filament arrays, the filament sequence member). Work that is
still open is tracked in #1155's deferred list.

## How to read the evidence

- A row cites a test only when that test exists and runs a production entry
  point: `inventory`, `model_job.slice_job`, `orca_adapter.slice_model`,
  `color_geometry.export_color_geometry` or `export_single_color_plate`,
  or the Discord summary formatter. Tests that call only an internal helper
  are not cited. Where a row has no such test, the row says so.
- "Production path" means a caller outside `tests/` reaches the code. A
  function that only tests call is named as such.
- Filament usage problems are warnings the operator can accept, not blocks
  (the owner ruling merged in #1190). Since #1213 a used filament with no
  slot chosen, a slot past the printer's slots and color choices never made
  are warnings too, each naming the fallback slot it prints from after
  acceptance. Problems that are not filament data still refuse, for example
  decisions bound to a different source.
- A row with no production-path test says "No production-path test" in its
  test column, and names the narrower test that exists, if any.

## Summary

| # | Criterion | Status on `main` |
|---|-----------|------------------|
| 1 | Setting intent | Implemented and tested on the slice path |
| 2 | Per-job provenance | Implemented and tested on the slice path |
| 3 | Per-plate filament usage | **Partial**: the analysis reads every class, Orca per-feature filaments included, but nothing in production selects a plate, so whole-source jobs warn over every plate |
| 4 | Atomic complete remapping | **Not met**: the exporters are atomic and remap feature filament keys with the parts' mapping, but the generic remapper has no production caller and the deferred items below stay open |
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
| Exact preset comparison | `author_settings.read_project` reads the embedded print preset from `Metadata/filament_sequence.json`; when no changed-settings list exists, `conversion_provenance` compares project values against it | `tests/pipeline/test_author_settings.py::test_a_foreign_slice_compares_to_the_exact_embedded_preset_without_a_changed_list` |
| UNKNOWN when evidence is missing | An Orca project with neither a list nor a preset records `unknown`. A PrusaSlicer project always records `unknown`: it embeds no preset and never writes `different_settings_to_system`, and a settings line of that name is not read | `tests/pipeline/test_author_settings.py::test_slice_records_intent_unknown_for_a_prusa_project_and_a_project_without_metadata` |
| A plain STL has no designer intent to guess | `conversion_provenance` records `not_applicable`, and each selected value gets a `selected_reason` (`operator`, `job`, `compatibility_fallback` and so on) | `tests/pipeline/test_author_settings.py::test_a_plain_stl_slice_records_no_designer_intent_and_attributes_selected_values` |
| Designer decisions kept, destination constraints win | `author_settings.carry` copies only the author-intent allowlist over the destination template, keeps only speeds lower than the profile, and falls back to the template for an enumeration Orca rejects. Machine, temperature and G-code keys are not in the allowlist | `tests/pipeline/test_author_settings.py::test_more_author_intent_is_kept_over_the_template`, `::test_lowered_speeds_reach_the_slice_profile`, `::test_bad_enum_never_reaches_the_slice_profile`, `::test_prusa_settings_reach_the_slice_profile`; whole-route audit: `tests/pipeline/test_foreign_audit_matrix.py::test_audit_whole_model_route`, `::test_audit_derived_plate_route`, `::test_silent_drop_regression_author_keys_named_in_the_differs_list_reach_orca` |
| One conversion pipeline | `author_settings.PRUSA_KEYS` and `ORCA_ENUMS` are built from the compatibility registry's rule parameters, and `orca_adapter.PROCESS_KEYS` from the `author-intent-allowlist` rule, so there is one key table | No production-path test; the table contents are pinned by the registry-structure test `tests/pipeline/test_compat_registry.py::test_registry_tables_hold_the_expected_values` |

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
| Per-class, per-plate analysis | `source_colors.inventory` stores `plate_usage`, built by `_plate_usage` over `_USAGE_CLASSES` (`objects`, `painting`, `layer_changes`, `height_ranges`, `modifiers`, `wall`, `infill`, `support`, `interface`, `tower`). Each class is `known`, `unknown` or `not_carried_by_format`, with its evidence. Original source filament IDs are kept | `tests/core/test_plate_usage.py::PlateUsageAnalysisTests::test_object_assignments_are_per_plate_with_original_ids`, `::test_triangle_painting_adds_painted_filaments_to_their_plate`, `::test_layer_changes_are_attributed_to_their_own_plate`, `::test_height_range_extruder_assignment_is_per_plate`, `::test_support_and_interface_designations_are_role_usage` |
| Painting | `_painting_usage` decodes `paint_color` and PrusaSlicer `mmu_segmentation` states per part | `tests/core/test_plate_usage.py::PlateUsageAnalysisTests::test_triangle_painting_adds_painted_filaments_to_their_plate`, `::test_unpainted_source_painting_class_is_known_empty`; `tests/pipeline/test_remap_exhaustive.py::test_mmu_segmentation_states_are_decoded_per_part` |
| Wall, infill and tower | PrusaSlicer: `_prusa_role_extruders` reads `perimeter_extruder`, `infill_extruder` and `solid_infill_extruder`, and asks a new question only when an object sets a role extruder (#1202). OrcaSlicer (#1203): `_orca_feature_usage` reads `wall_filament`, `sparse_infill_filament` and `solid_infill_filament` at project, object and part level, and `_orca_tower_usage` reads `wipe_tower_filament`, which counts only on a plate that prints a tower. A value outside the palette, or one that is not a filament number, makes the class `unknown` and is never counted as a used filament. A project that names none of these keys (Bambu Studio writes none) keeps them `not_carried_by_format` | `tests/core/test_plate_usage.py::PlateUsageAnalysisTests::test_prusa_role_extruders_are_read_from_the_prusa_config`; `tests/core/test_plate_usage.py::UsageUndercountTests::test_orca_object_wall_filament_override_is_counted`, `::test_orca_infill_override_to_an_unasked_filament_is_unknown_not_a_question`, `::test_orca_feature_filament_outside_the_palette_is_unknown`, `::test_orca_wipe_tower_filament_counts_only_where_a_tower_prints`, `::test_orca_default_feature_filaments_add_no_filament_or_question` (all through `inventory`) |
| Unknown stays unknown | A class is `unknown` when the source should carry it but it cannot be read (a part with no extruder, a tool change with no extruder, unparsable height ranges, an out-of-palette support designation). A mismatch with the slicer-reported totals in `slice_info.config`, or a damaged record there, makes the plate unknown | `tests/core/test_plate_usage.py::PlateUsageAnalysisTests::test_layer_change_without_an_extruder_is_unknown_not_zero`, `::test_malformed_height_ranges_are_unknown_not_absent`, `::test_part_without_extruder_assignment_is_unknown`, `::test_slicer_disagreement_makes_the_plate_usage_unknown`, `::test_invalid_slicer_record_makes_the_plate_usage_unknown` |
| Questions only for the selected plate | `source_colors.prompts`, `canonical_prompts` and `grouped_prompts` scope by `selected_plate_ids`, and the Discord, chat and packet intake pass `job['selected_plates']`. **No production code writes `selected_plates`**; only tests do. In production the value is always empty, so every plate is asked about | No production-path test can cover this; the scoping tests call the prompt functions with a hand-built plate list |
| Readiness | `model_job._enforce_filament_readiness` (the selected-plate gate) runs only when `job['selected_plates']` is set, which production never does, so that branch is unreachable outside tests. Without a selection, a whole-source multi-plate `slice_job` presents every plate's filament warnings (unknown usage, damaged slicer usage records) through `_enforce_filament_warnings` (#1202), and slices only after the operator accepts them | `tests/pipeline/test_filament_warn_accept.py::WholeSourceMultiPlateTests::test_unknown_usage_on_any_plate_warns_then_slices_after_acceptance`, `::SliceJobWarnAcceptTests::test_damaged_usage_claim_waits_for_acceptance_then_slices`. Selected-plate path, with the test supplying `selected_plates`: `::SliceJobWarnAcceptTests::test_selected_plate_unknown_usage_waits_for_acceptance_then_slices` |
| Colors, slots and nozzles kept distinct | Usage records carry logical source filament IDs; decisions map IDs to loaded slots; a slot past `model_job.position_limit` warns and prints from a named fallback slot after acceptance (#1213), and a shared nozzle adds no slots | `tests/pipeline/test_filament_fallbacks_1189.py::SliceJobFallbackTests::test_slot_past_what_the_printer_has_loaded`, `::test_ad5x_slot_past_4_falls_back_to_a_loaded_slot_and_passes_tool_verification` (through `slice_job`) |

The usage under-counts the review found (interface-only support filaments,
height ranges keyed by resource id instead of the Bambu object index,
extruder 0 in a range, objects with instances on two plates, unmapped
`slice_info` filaments) were fixed in #1203, with tests in
`tests/core/test_plate_usage.py::UsageUndercountTests`. The two left after
that are fixed too, with tests in `tests/core/test_usage_reads_1155.py`
(all through `inventory`):

- A Bambu or OrcaSlicer `modifier_part` that names its own extruder is the
  `modifiers` class of its object's plates. A filament some color question
  already maps is counted; one that none maps, or one outside the palette,
  makes the class `unknown` (a warning to accept) and is never a used
  filament or a new question.
- PrusaSlicer's `Metadata/Prusa_Slicer_custom_gcode_per_print_z.xml` (height
  tool and color changes) and `Metadata/Prusa_Slicer_layer_config_ranges.xml`
  (range extruders) are read. The exports do not carry either, so an entry
  that changes the filament makes `layer_changes` or `height_ranges`
  `unknown` with that reason, never known and empty. Pauses, custom G-code,
  range extruder 0 and a range naming its object's own extruder change no
  filament. An unparsable member is `unknown`.
- A PrusaSlicer modifier volume (`volume_type` `ParameterModifier`, or the
  older `modifier` flag, in `Metadata/Slic3r_PE_model.config`) that names an
  extruder other than 0 and its object's own makes the `modifiers` class
  `unknown` with the volume named. PrusaSlicer stores the volume as a
  triangle range of its object's one mesh, which inventory does not split,
  so the region is not carried with its own filament; it is never a used
  filament or a new question. Tests: `tests/core/test_prusa_modifier_usage_1155.py`
  (through `inventory`).

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
  what a production export does. The feature-key helpers
  `package_remap.remap_feature_filaments` and `remap_filament_metadata` are
  the exception: the single-color copy calls them.
- The exporters rebuild or collapse a package with their own code and do not
  use the generic remapper.

| Requirement element | What the code does | Production-path tests |
|---|---|---|
| Atomic write | `write_package_atomically` stages the package in a uniquely named temporary file in the target's folder (`tempfile.mkstemp`), validates it, flushes and `fsync`s it, then `os.replace`s it onto the target. Any failure removes the temporary file and leaves an existing target unchanged. With `source=` it refuses a target that resolves to the source; `remap_package` passes it, the exporters do not yet | `tests/pipeline/test_remap_exhaustive.py::test_export_single_color_plate_failure_after_staging_leaves_no_partial_output` (the replace fails after staging and nothing is left behind) |
| Original immutable | Exporters read the source and write the target only | The test above asserts the source bytes are unchanged |
| Rebuilt export refuses what it cannot translate | `export_color_geometry` refuses a source whose height ranges name extruders, an unmapped layer-change tool and an unmapped paint state, each by name. It names every member it does not carry in `dropped_members` and every dropped per-part setting in `dropped_part_settings` (a feature override it carries as a tool, below, is not listed) | `tests/pipeline/test_remap_exhaustive.py::test_rebuilt_export_refuses_height_range_extruders_by_name`, `::test_mmu_segmentation_export_refuses_a_paint_state_with_no_choice`, `::test_rebuilt_export_names_dropped_per_part_settings` |
| Feature filament keys | Both exports remap them with the parts' own mapping (#1155). Single-color copy: `_declare_one_filament` sets print-wide `wall_filament`, `sparse_infill_filament`, `solid_infill_filament` and `wipe_tower_filament` that name a filament the plate prints to 1, and the object and part wall, infill and `support_filament` metadata the same way; a plate `filament_maps` list (one entry per palette filament) keeps the kept filament's entry. Rebuilt export: an object or part override is written on the part as the tool the decisions give that filament; the print-wide keys are not carried (every object names its own tool) and are named in the report with the tool each maps to. A value the mapping does not cover, past the palette or not a number is unknown usage: never a used filament and never a refusal. It is written as 0 (no specific filament) where 0 means that, removed where it does not, and named in the report's `feature_filaments` | `tests/pipeline/test_feature_filament_remap_1155.py::test_single_color_plate_remaps_every_feature_key_with_the_parts_mapping`, `::test_single_color_plate_maps_a_feature_filament_picked_for_the_same_slot`, `::test_rebuilt_export_writes_feature_overrides_as_the_tools_the_parts_use`; slice round trip on the AD5X: `::test_non_identity_feature_remap_slices_to_gcode_that_passes_ad5x_tool_verification` |
| Unknown per-filament arrays | `_PER_FILAMENT_KEYS` stays an allowlist. A palette-length project array it does not know is kept as authored and named as an unknown passthrough (`package_remap.unknown_palette_arrays`): in the single-color copy's `feature_filaments` and in the generic remapper's notes. Permuting every palette-length list was rejected because some only happen to have that length (a four-point bed outline on a four-filament project) | `tests/pipeline/test_feature_filament_remap_1155.py::test_single_color_plate_names_an_unknown_palette_length_array_and_keeps_it` |
| Paint | The rebuilt export translates PrusaSlicer `mmu_segmentation` to the chosen tools and remaps `paint_color` states for painted one-color plates. Support, seam and fuzzy-skin paint carries no filament and is not touched | `tests/pipeline/test_remap_exhaustive.py::test_mmu_segmentation_export_translates_paint_to_the_chosen_tools`, `tests/pipeline/test_color_geometry.py::test_painted_one_color_view_exports_each_part_plate` |
| Layer changes | Remapped in the rebuilt export and the single-color copy; the `color` attribute is kept because Orca's loader requires it | `tests/pipeline/test_remap_exhaustive.py::test_rebuilt_layer_change_keeps_the_color_attribute`, `::test_single_color_plate_carries_no_stale_filament_reference` |
| Single-color copy | `_declare_one_filament` keeps the used filament's entries in every per-filament array, remaps support designations, sets height-range and layer-change extruders to 1, and drops `slice_info.config` and embedded G-code with the drop named in the report. For a per-filament array whose length is neither 1 nor the palette size it keeps the entry at the declared filament's own index when there is one, else the first entry, and logs which; it never refuses | `tests/pipeline/test_remap_exhaustive.py::test_single_color_plate_carries_no_stale_filament_reference` |
| Opaque members | The production-plate copy keeps `Auxiliaries/` members byte for byte | No production-path test: `tests/pipeline/test_remap_exhaustive.py::test_production_plate_preserves_auxiliaries_members` runs `export_production_plate`, which has no production caller |
| Purge matrix and vector | The single-color copy keeps the used filament's diagonal matrix entry and its own pair of the flush vector. The generic remapper permutes `flush_volumes_matrix`; it refuses a non-uniform `flush_volumes_vector` under a non-identity mapping because its layout is not established, and passes a uniform one (the slicer default) through, since every reordering of it is the same list (#1155) | Single-color copy: `tests/pipeline/test_feature_filament_remap_1155.py::test_single_color_plate_remaps_every_feature_key_with_the_parts_mapping`. No production-path test for the generic remapper's permutation, which has no production caller; library tests `tests/pipeline/test_remap_package_bambu_1155.py::test_a_realistic_bambu_project_remaps_completely`, `::test_a_non_uniform_flush_vector_still_refuses_by_name` |
| Generic remapper on PrusaSlicer packages | `package_remap.remap_prusa_member` refuses a mapping that does not cover the whole extruder palette, then permutes every per-filament list, remaps the role extruders (0 kept; an absent one that defaults to 1 is written as filament 1's new number), permutes the wiping-volumes matrix on both axes and the load/unload vector by pair, and keeps per-extruder printer lists with their physical extruder (named when they differ). The model config's role extruders and the PrusaSlicer height members are remapped (#1155) | No production-path test (no production caller). Library tests: `tests/pipeline/test_remap_package_prusa_1155.py::test_a_prusa_package_remaps_palette_settings_roles_and_matrix`, `::test_a_mapping_that_does_not_cover_the_prusa_palette_refuses`, `::test_the_corpus_prusa_mmu_project_remaps_completely` |
| Generic remapper on real Bambu and Orca projects | `package_remap.member_handling` classifies the standard members of a Bambu Studio / OrcaSlicer save (#1155): `plate_N.json` and `plate_N.gcode.md5` (the previous slice's records) are kept under the identity mapping and dropped by name otherwise; `cut_information.xml`, `layer_heights_profile.txt` and `brim_ear_points.txt` (object-keyed, no filament reference) are preserved and refuse by name under an object map. An object on implicit filament 1 (no extruder, or 0) has it written explicitly before a mapping that moves filament 1; a build object with no settings entry refuses that mapping by name | No production-path test (no production caller). Library tests: `tests/pipeline/test_remap_package_bambu_1155.py::test_a_realistic_bambu_project_remaps_completely`, `::test_a_sliced_project_refuses_for_its_embedded_gcode_by_name`, `::test_a_build_object_with_no_settings_entry_refuses_a_remap_that_moves_filament_1` |
| Embedded G-code and `filament_sequence.json` | Under a non-identity mapping the single-color copy drops embedded G-code (named in `dropped_entries`) and keeps the sequence file byte for byte (named in `preserved_unremapped_entries`), because its preset record is provenance evidence about the source; the rebuilt export carries neither and names them in `dropped_members`. The generic remapper refuses both under a non-identity mapping | `tests/pipeline/test_remap_exhaustive.py::test_single_color_plate_carries_no_stale_filament_reference`, `tests/pipeline/test_feature_filament_remap_1155.py::test_single_color_plate_keeps_the_filament_sequence_byte_for_byte_under_a_remap`, `::test_rebuilt_export_drops_the_filament_sequence_member_by_name_under_a_remap`. No production-path test for the generic refusals (no production caller); the library test is `::test_generic_remap_refuses_the_filament_sequence_only_under_a_non_identity_mapping` |
| One read per call | A slice, an export and an inventory each run in a `painted_geometry.decode_scope`: each `.model` member is decompressed once and each painted mesh decoded once, however many consumers ask (the multi-material scan in `slice_model` included). Verdicts do not change | `tests/pipeline/test_feature_filament_remap_1155.py::test_one_slice_reads_each_model_member_once_and_decodes_paint_once`, `::test_decode_scope_never_changes_a_verdict` |
| One inventory per color flow | The Discord color flow (`discord_color_intake.collect`) runs in a `source_colors.inventory_scope`: the question build and each `intake_color_choices` pass (two in a warning round) share one inventory of the source, keyed by its content hash. Each caller gets its own copy; only the manifest is kept, never member bytes or decoded paint, and nothing outlives the flow. A changed source hashes differently and is inventoried again | `tests/pipeline/test_discord_color_intake.py::test_discord_flow_inventories_the_source_once`, `::test_discord_flow_reinventories_a_source_changed_during_selection`; `tests/pipeline/test_filament_warn_accept.py::DiscordWarningRoundTests::test_the_warning_round_inventories_the_source_once`; timing: `tests/core/test_usage_reads_1155.py::test_a_second_inventory_in_one_flow_skips_the_paint_pass` |
| Modifier extruders | The rebuilt export writes a `modifier_part`'s own extruder as the tool the decisions give that filament. A filament no tool of the job loads is not written (the body's tool prints the region) and is named in `feature_filaments` as unknown usage | `tests/core/test_usage_reads_1155.py::test_rebuilt_export_writes_the_modifiers_own_filament_as_its_tool`, `::test_rebuilt_export_names_a_modifier_filament_the_job_does_not_load` |

Open gaps, all deferred past 2.2.9 in #1155:

- **No production caller.** `remap_package` is still experimental: every
  production export rebuilds or collapses the package with its own code, so
  the generic remapper's completeness on real projects (above) is library
  evidence only.
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
| Versioned registry | `print_handy/core/compat_registry.py`: `REGISTRY_VERSION` 1 and 32 rules in `RULES`, each with a unique id, kind, source and destination family and version spec, reason, parameters and named evidence | No production-path test; registry-structure tests: `tests/pipeline/test_compat_registry.py::test_registry_has_a_version_and_unique_ids`, `::test_every_rule_carries_the_required_fields`, `::test_every_evidence_reference_resolves`, `::test_registry_parameters_are_frozen`, `::test_the_shipped_orca_is_inside_every_rules_declared_versions` |
| Honest about unknown versions | `compat_registry.applies` matches an unknown version only against an unrestricted spec. `author_settings.governing_rules` records every applied rule and lists mismatches separately. A foreign project outside a rule's declared versions is refused after slicing; a plain STL never is | `tests/pipeline/test_author_settings.py::test_a_foreign_project_converted_outside_a_rules_declared_versions_is_refused`, `::test_a_plain_stl_is_never_refused_for_rule_versions_and_rules_are_recorded` |
| Percent versus millimeter line widths | Rule `orca-percent-line-width-to-bambu-mm` converts with the destination nozzle diameter for a Bambu Studio destination and fails closed when the diameter is unreadable | `tests/pipeline/test_orca_adapter.py::test_bambu_fallback_converts_percent_line_widths` |
| Incompatible enumerations | Rule `orca-enum-fallback` replaces an enumeration Orca rejects with the template's value | `tests/pipeline/test_author_settings.py::test_bad_enum_never_reaches_the_slice_profile` |
| Hardware-only keys and commands | Rule `foreign-machine-key-removal` removes machine, motion, temperature, bed and G-code keys from adopted copies; the target template supplies the machine setup | `tests/pipeline/test_foreign_project_adoption.py::test_derived_plate_reaches_orca_with_the_job_profile_not_the_foreign_one`, `::test_plugin_keys_are_stripped_from_an_imported_project` |
| Profile provenance | See criterion 2 | See criterion 2 |
| No unexplained substitution | Each rule names its keys, values or profile. `compat_registry.get_rule` raises for an unknown id | No production-path test; the registry lookup test is `tests/pipeline/test_compat_registry.py::test_registry_lookup_refuses_an_unknown_id` |

[Printer profiles](../PRINTER-PROFILES.md) holds the rule table and the
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
| Calibration outranks designer values | Rule `foreign-machine-key-removal`, plus the protected-settings baseline that refuses a slice whose start or end G-code differs from the approved baseline | Key removal on the slice path: `tests/pipeline/test_foreign_project_adoption.py::test_derived_plate_reaches_orca_with_the_job_profile_not_the_foreign_one`. No production-path test for the baseline refusal; the shipped baseline is pinned by the contract test `tests/pipeline/test_port_contracts.py::test_shipped_protected_baseline_matches_the_orca_machine_template` |
| Purge tower validated against the destination | Rule `wipe-tower-bed-clamp` clamps the tower inside the bed using the job's tower width; `orca_adapter.ifs_purge_settings` builds the purge settings for the destination | `tests/pipeline/test_author_settings.py::test_slice_model_clamps_with_the_job_tower_width` |
| Unresolved mappings | A used filament with no slot chosen, a slot past the printer's slots and color choices never made warn, name the fallback slot, and print from it after acceptance (#1213). Decisions bound to another source still refuse with a named reason. Unknown or damaged usage warns and waits for acceptance | `tests/pipeline/test_filament_fallbacks_1189.py::SliceJobFallbackTests::test_used_filament_with_no_slot_chosen`, `::test_slot_past_what_the_printer_has_loaded`, `::test_color_choices_never_made`; `tests/pipeline/test_filament_warn_accept.py::SliceJobWarnAcceptTests::test_damaged_usage_claim_waits_for_acceptance_then_slices`. No production-path test for the other-source refusal: `tests/pipeline/test_filament_warn_accept.py::GateTests::test_decisions_bound_to_another_source_still_refuse` calls `_enforce_filament_readiness` directly |
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
  [Printer profiles](../PRINTER-PROFILES.md). Print Handy follows ForgeBridge's
  published rules; no PrintNomad code, fixtures or dependencies are used.
- `model_job.filament_usage_by_selection` (slicer-reported per-plate grams
  by selection) predates the usage analysis.

## What remains

The open gaps under criteria 3 and 4 are #1155's deferred list (the
"deferred post-2.2.9" items in its body). #1110 is closed; #1155 is the home
for its remaining requirements. If a gap closes, update its row here in the
same pull request and cite a test that runs the production entry point.
