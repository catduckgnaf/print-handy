# 3MF conversion evidence matrix (#1110)

This page is the documentation and evidence matrix for issue #1110 (foreign
3MF conversion: designer intent, provenance, filament mapping,
compatibility). It maps every acceptance criterion in #1110 to the
implementation and test evidence that establishes it, including coverage
that predates the #1110 completion program (#713, #723, the #714 corpus and
the existing support regression corpus), as #1110's validation plan
requires.

**Status: final verification, 2026-11-09.** This pass verifies the full
assembled stack (PR #1111, then Phase D PR #1127, Phase A PR #1128,
Phase B PR #1129, Phase C PR #1133). It supersedes the interim pass
recorded at the Phase B tip. Verdicts were established by reading the code
and tests at the stack tip and running the named evidence tests, not by
copying the phase pull requests' claims. Criteria 1, 2, 3, 5 and 6 are
met. Criterion 4 is **partial**, precisely bounded below: every reference
type is now provably updated or refused/preserved by name with no partial
output and no silent drop, but three reference rows are handled by named
refusal rather than by update in the generic remapper. Whether those
named refusals satisfy the criterion is the close decision; the close
itself happens only after the stack is merged.

## Verdict summary

| # | Criterion | Verdict at this stack tip |
|---|-----------|---------------------------|
| 1 | Setting intent | Met |
| 2 | Per-job provenance | Met |
| 3 | Per-plate filament usage | Met |
| 4 | Atomic complete remapping | **Partial — all references updated or refused/preserved by name; three named-refusal residues (see criterion 4)** |
| 5 | Versioned compatibility | Met |
| 6 | Safety gates preserved | Met |

## Criterion 1 — Setting intent

Requirement (#1110 item 1): distinguish deliberate designer settings from
inherited source defaults using `different_settings_to_system` and
source-preset comparison when available; if metadata or exact source
profiles are unavailable, intent is UNKNOWN, never guessed; preserve model
decisions (supports, walls, infill, seam) while destination machine
constraints prevail; first establish existing behavior and do not duplicate
conversion pipelines.

| Requirement element | Implementation evidence | Test evidence |
|---|---|---|
| `different_settings_to_system` names deliberate changes | `print_handy/core/author_settings.py::conversion_provenance` parses the changed-settings list (Orca project field; PrusaSlicer `; different_settings_to_system =` line) into the per-key `origin`/`reason` fields | `tests/pipeline/test_author_settings.py::test_conversion_provenance_uses_different_settings_to_system_without_guessing`, `::test_conversion_provenance_marks_unchanged_keys_as_inherited`, `::test_conversion_provenance_unlisted_key_reason_agrees_with_origin` |
| Exact source-preset comparison | `author_settings._source_preset_settings` reads only an exact embedded preset setting record (`Metadata/filament_sequence.json` print preset); `conversion_provenance` compares project values against it | `tests/pipeline/test_author_settings.py::test_conversion_provenance_compares_to_exact_embedded_source_preset` |
| Intent UNKNOWN, never guessed, when evidence is unavailable | `conversion_provenance`: with neither a changed-settings list nor an exact preset, `origin` is `unknown` and the reason states the comparison is unavailable; a present but empty changed-settings list establishes nothing; `intent_status` is `known` only when a changed-settings list exists | `tests/pipeline/test_author_settings.py::test_conversion_provenance_marks_intent_unknown_without_source_metadata`, `::test_conversion_provenance_empty_changed_list_is_unknown_intent` |
| Designer decisions preserved; destination machine constraints prevail | `author_settings.carry` carries only the author-intent allowlist over the destination template; lowered designer speeds only (`author_settings.lowered_speeds`); unacceptable enumerations fall back to the template (`author_settings.invalid_enum`, registry rule `orca-enum-fallback`); machine, temperature and G-code keys are never in the allowlist and are removed from adopted copies (registry rule `foreign-machine-key-removal`) | `tests/pipeline/test_author_settings.py::test_more_author_intent_is_kept_over_the_template`, `::test_only_speeds_below_the_profile_are_kept`, `::test_unknown_author_enum_falls_back_to_the_template`, `::test_bad_enum_never_reaches_the_slice_profile`; whole-route audit: `tests/pipeline/test_foreign_audit_matrix.py::test_audit_whole_model_route`, `::test_audit_derived_plate_route` |
| No duplicated conversion pipeline | One carry path: `print_handy/core/orca_adapter.py::slice_model` calls `author_settings.carry` once with `PROCESS_KEYS`, and `orca_adapter.adopt_job_settings` defaults to the same `PROCESS_KEYS`; since Phase D, `PROCESS_KEYS` itself is the registry rule `author-intent-allowlist`'s parameter table, so there is no second key table to drift | `tests/pipeline/test_compat_registry.py::test_converter_tables_are_the_registry_tables`; `tests/pipeline/test_foreign_audit_matrix.py::test_silent_drop_regression_author_keys_named_in_the_differs_list_reach_orca` |

Notes: exact preset comparison exists only where the source carries an
embedded preset record (an Orca project today); a PrusaSlicer project is
judged by its changed-settings line alone. Where neither evidence exists
the verdict is unknown by design — that is the criterion's required
behavior, not a shortfall. The earlier partial verdict on PR #1111 ("not
yet a complete conversion-pipeline audit") is answered by the pre-existing
foreign audit matrix above, which exercises whole-model and derived-plate
routes for PrusaSlicer and Bambu-derivative projects against every
Flashforge model, plus the Phase D parity tests that pin the single
pipeline's tables to the registry.

## Criterion 2 — Per-job provenance

Requirement (#1110 item 2): a structured JSON record integrated with job
artifacts, recording source preset value, project value, target value,
selected value, origin/reason, unresolved conflicts, and source/destination
slicer and profile versions; a concise consequential summary in the
existing Discord/job presentation; no standalone evidence-report workflow.

| Requirement element | Implementation evidence | Test evidence |
|---|---|---|
| Structured per-setting record | `author_settings.conversion_provenance` returns schema version 1 with per-key `source_preset_value`, `project_value`, `target_value`, `selected_value`, `origin`, `reason`, plus `unresolved_conflicts` and `conversion_notes` | `tests/pipeline/test_author_settings.py::test_conversion_provenance_records_selected_value_after_enum_fallback`, `::test_conversion_provenance_stores_only_differing_or_carried_keys` |
| Authoritative JSON job artifact | `author_settings.PROVENANCE_ARTIFACT` (`conversion_provenance.json`); `orca_adapter.slice_model` writes it beside the slice output it describes, embeds the same record in `JOB.json` slice provenance, and sets the record's `artifact` field only when the write succeeds (a failed write removes the claim) | `tests/pipeline/test_author_settings.py::test_slice_provenance_records_real_destination_versions_rules_and_artifact` |
| Real source/destination slicer versions | Source: `author_settings._read_raw_project` / `source_identity` read the source project's own version evidence. Destination: `print_handy/core/slice_guard.py::slicer_version` reads the version from the producing slicer's own identity line in its output (`generator_identity`), so a fallback slice records the fallback slicer's version | `tests/pipeline/test_author_settings.py::test_slice_provenance_records_prusa_rules_and_source_version_for_a_prusa_project`, `::test_conversion_provenance_records_destination_identity_and_governing_rules` |
| Real source/destination profile versions, honestly null | `conversion_provenance` takes `source_profile_version`/`source_profile_name` from the source's embedded preset record; a source that records none keeps the field null with `source_profile_version_status` of `recorded`, `not_recorded_by_source` or `not_applicable`. Destination: `author_settings.destination_profile_identity` (template's own `version`, else the template folder `SOURCE.json` vendor version, else the Orca `setting_id`, else the name) | `tests/pipeline/test_author_settings.py::test_conversion_provenance_records_source_profile_version_from_embedded_preset`, `::test_conversion_provenance_marks_source_profile_version_not_recorded`, `::test_destination_profile_identity_prefers_real_versions_over_setting_id`, `::test_slice_model_provenance_targets_the_destination_template` |
| Governing registry rules recorded | `conversion_provenance` records `governing_rules` and `registry_version`; `author_settings.governing_rule_ids` keeps only candidate rules the registry's own family/version applicability check accepts for the recorded pair | `tests/pipeline/test_author_settings.py::test_conversion_provenance_records_destination_identity_and_governing_rules` |
| Concise Discord presentation, no standalone workflow | `print_handy/hermes/discord_print_summary.py::format_slice_summary` reports only consequential items (unknown designer intent, the count of unresolved conflicts) plus the artifact's name; the record itself is never dumped | `tests/hermes/test_print_messages.py::test_slice_summary_reports_only_consequential_conversion_provenance`, `::test_slice_summary_references_provenance_artifact_and_stays_concise`, `::test_slice_summary_omits_nonconsequential_conversion_provenance` |

## Criterion 3 — Per-plate filament usage

Requirement (#1110 item 3): account for object/part assignments, triangle
painting, layer changes, height ranges, and wall/infill/support/interface/
tower assignments; ask only for colors required by the selected plate;
retain original IDs; model logical color IDs, loaded slots and physical
nozzles as distinct concepts; unknown usage preserves source data and
blocks readiness if mapping cannot be validated.

| Requirement element | Implementation evidence | Test evidence |
|---|---|---|
| Per-class per-plate derivation | `print_handy/core/source_colors.py::inventory` records `plate_usage` built by `source_colors._plate_usage` over the classes in `source_colors._USAGE_CLASSES` (`objects`, `painting`, `layer_changes`, `height_ranges`, `wall`, `infill`, `support`, `interface`, `tower`), each row carrying its status, source filaments and evidence | `tests/core/test_plate_usage.py::test_object_assignments_are_per_plate_with_original_ids`, `::test_triangle_painting_adds_painted_filaments_to_their_plate`, `::test_layer_changes_are_attributed_to_their_own_plate`, `::test_height_range_extruder_assignment_is_per_plate`, `::test_support_and_interface_designations_are_role_usage` |
| Painting detail | `source_colors._painting_usage` decodes `paint_color` states per part (unpainted and base-state triangles count the part's base extruder). Since Phase C, PrusaSlicer `mmu_segmentation` is decoded per state by `painted_geometry` with the same TriangleSelector decoding, so its states count exactly (Phase B's conservative count of every palette filament is superseded) | `tests/core/test_plate_usage.py::test_triangle_painting_adds_painted_filaments_to_their_plate`, `::test_unpainted_source_painting_class_is_known_empty`; `tests/pipeline/test_remap_exhaustive.py::test_mmu_segmentation_states_are_decoded_per_part` |
| Wall/infill/tower accounting per format | `source_colors._prusa_role_extruders` reads PrusaSlicer per-feature extruders (`perimeter_extruder`, `infill_extruder`, `solid_infill_extruder`) as the wall and infill assignments. The Bambu/Orca and PrusaSlicer formats name no distinct wall, infill or prime-tower filament, so those classes are recorded `not_carried_by_format` — a stated limitation, not a hidden filament: walls and infill print with the object filament already counted under `objects`, and the tower prints with the filaments in play | `tests/core/test_plate_usage.py::test_wall_infill_tower_are_not_carried_by_an_orca_project`, `::test_prusa_role_extruders_are_read_from_the_prusa_config` |
| Unknown stays unknown, never guessed or silently zero | `_plate_usage` marks a class `unknown` when the source should carry it but it cannot be determined (a part with no extruder assignment, a tool change naming no extruder, unparsable height ranges, undecodable paint); a disagreement between the analysis and the slicer-reported totals in `slice_info.config`, or a damaged slicer usage record, makes the plate's usage unknown with the disagreement recorded | `tests/core/test_plate_usage.py::test_layer_change_without_an_extruder_is_unknown_not_zero`, `::test_malformed_height_ranges_are_unknown_not_absent`, `::test_part_without_extruder_assignment_is_unknown`, `::test_slicer_disagreement_makes_the_plate_usage_unknown`, `::test_invalid_slicer_record_makes_the_plate_usage_unknown`, `::test_slicer_reported_totals_merge_when_they_agree` |
| Selected-plate-only prompts | `source_colors.prompts` and `source_colors.canonical_prompts` accept `selected_plate_ids` and scope through `source_colors._selected_scope`; the chat intake, the Discord color flow and the color-plan packet all generate questions from this same prompt layer (call sites in `print_handy/core/model_job.py`, `print_handy/core/discord_color_intake.py`, `print_handy/core/prepared_intake.py`). A filament used only through a class with no question of its own still gets a question naming the class | `tests/core/test_plate_usage.py::test_canonical_prompts_cover_only_the_selected_plate`, `::test_grouped_and_legacy_prompts_scope_the_same_way`, `::test_painted_prompts_scope_to_the_selected_plates_filaments`, `::test_layer_change_rows_scope_to_their_plate`, `::test_height_range_filament_gets_a_question`, `::test_scoped_decisions_validate_against_the_scoped_questions` |
| Readiness blocking | `source_colors.plate_readiness_problems` / `require_plate_readiness` block, with the reason named, when a selected plate has an unknown usage class, a slicer/analysis disagreement, or a used filament with no validated slot mapping; called from `model_job.intake_color_choices` and `model_job.slice_job` with the destination's slot capacity. Unselected plates never prompt or block; the spool projection counts only the selected plates' validated slicer-reported grams (`model_job._selected_plate_stats`, `model_job.filament_usage_by_selection`) | `tests/core/test_plate_usage.py::test_ready_when_every_selected_plate_filament_is_mapped`, `::test_unmapped_selected_filament_blocks_with_a_named_reason`, `::test_unselected_plates_never_block`, `::test_unknown_usage_blocks_only_the_selected_plate`, `::test_disagreement_blocks_the_selected_plate`, `::test_missing_decisions_block`, `::test_single_color_selection_maps_every_used_filament`, `::test_spool_projection_counts_only_the_selected_plate` |
| Original IDs; color, slot and nozzle kept distinct | Usage records carry logical source filament IDs end to end (a plate using only filament 3 is recorded as filament 3, never renumbered); decisions map source IDs to loaded slots; readiness validates slots against real loaded-slot capacity (`model_job.position_limit`). The AD5X's four IFS spools share one nozzle, and that shared nozzle adds no slots: a mapping naming a fifth slot is not validated | `tests/core/test_plate_usage.py::test_object_assignments_are_per_plate_with_original_ids`, `::test_shared_nozzle_destination_still_validates_real_slot_capacity` |

## Criterion 4 — Atomic complete remapping

Requirement (#1110 item 4): update every reference, including paint,
parts, feature/support assignments, height ranges, layer changes, purge
matrices and vectors; preserve geometry and paint regions; leave the
original immutable; fail without partial output; cleanup/merging is
conservative and allowed only when usage is proven; never silently drop
colors.

**Verdict: partial — precisely bounded.** Phase C (PR #1133) rebuilt this
surface around a generic package remapper
(`print_handy/core/package_remap.py::remap_package`) and a staged atomic
write shell (`package_remap.write_package_atomically`) now used by the
generic remapper and every exporter. Every one of the 28 audited
reference types (R01–R28) is now either provably updated, or refused or
preserved *by name* — nothing is carried stale or dropped silently
anywhere, the original is immutable, and every refusal fails without
partial output. The criterion's first demand, "update every reference,"
is nevertheless not literally met for three rows in the generic
remapper, which are handled by named refusal instead of update (rows
marked *refusal* below). The pre-Phase C baseline was 4 of 28 reference
types atomically remapped, 8 read but not remapped, 7 rewritten
non-atomically, 2 not read and 7 not applicable; the per-element
verdicts below are the post-Phase C state, each verified against the
code and tests at the Phase C tip.

| Requirement element | Verdict | Implementation and test evidence |
|---|---|---|
| Atomic shell: staged write, no partial output | Met | `package_remap.write_package_atomically` stages beside the target, checks zip integrity, re-validates the staged payload (for the generic remapper, a cross-reference consistency re-read: every filament reference in the staged package resolves under the new numbering), and only then replaces the target; it is used by `remap_package` and by `color_geometry.export_color_geometry`, `export_single_color_plate`, `export_volume_plate` and `export_production_plate`. Refusals preserve source and pre-existing destination bytes: `tests/pipeline/test_remap_exhaustive.py::test_generic_remap_refuses_an_unmapped_range_extruder_atomically`, `::test_generic_remap_refuses_the_unestablished_flush_vector`; pre-existing coverage `tests/pipeline/test_color_geometry.py::test_failed_remap_preserves_source_and_existing_destination_bytes`, `::test_archive_layer_remap_preserves_members_and_is_atomic` |
| Paint: `paint_color` states | Met (updated) | Permuted through the paint table in the generic remapper (`package_remap._remap_model_member` over `painted_geometry.rewrite_paint_bytes`) and the rebuilt export; geometry bytes otherwise untouched; support, seam and fuzzy-skin paint carry no filament identity and are preserved verbatim. `tests/pipeline/test_remap_exhaustive.py::test_generic_remap_permutates_every_reference_and_preserves_the_rest` |
| Paint: PrusaSlicer `mmu_segmentation` | Met (updated) | Decoded per state by `painted_geometry` (`MMU_RE`, the same TriangleSelector decoding as `paint_color`), translated to `paint_color` on remap with the Prusa attribute dropped; a state with no mapping refuses before any byte is written (registry rule `mmu-segmentation-paint-translation`). `tests/pipeline/test_remap_exhaustive.py::test_mmu_segmentation_states_are_decoded_per_part`, `::test_mmu_segmentation_export_translates_paint_to_the_chosen_tools`, `::test_mmu_segmentation_export_refuses_a_paint_state_with_no_choice` |
| Parts: object/part extruders | Met (updated) | `package_remap.remap_extruder_metadata` rewrites every extruder metadata value in `model_settings.config` and `Slic3r_PE_model.config` (value 0, "inherit the object's extruder," is structural and passes through); an unmapped value refuses, naming the member. Same swap test as above |
| Feature/support assignments | Met (updated, with named drops) | Support designations are remapped in-package (`package_remap.permute_project_settings`; Prusa pair in `remap_prusa_member`); a designation whose filament a remap removes falls back to 0, the object's own filament, with the fallback named in the remap report — never a value past the new palette (registry rule `support-designation-remap`). Per-part author settings outside the intent allowlist are still not carried by the rebuilt export, but the drop is now named per part and key in the export report's `dropped_part_settings` (`color_geometry._dropped_part_settings`): `tests/pipeline/test_remap_exhaustive.py::test_rebuilt_export_names_dropped_per_part_settings` |
| Height ranges | Met (updated; one path refuses by name) | The generic remapper translates range extruder values through the mapping and object ids through the caller's object map, refusing values or objects it does not know (`package_remap.remap_ranges`). The single-color copy rewrites values to its one filament; slice-time adoption clamps a value past the job's filament count to the job's last filament and records the clamp in its `changed` record (`orca_adapter.adopt_job_settings`, registry rule `height-range-extruder-remap`). The rebuilt geometry export, which renumbers object ids, refuses a source whose ranges name extruders, by name, rather than mistranslate them: `tests/pipeline/test_remap_exhaustive.py::test_rebuilt_export_refuses_height_range_extruders_by_name` |
| Layer changes | Met (updated) | Tool changes are remapped in the generic remapper and the rebuilt export; an unmapped tool change now refuses in-export instead of dropping silently (`color_geometry.export_color_geometry`). A kept entry's `color` attribute is carried verbatim: it is display metadata (tool selection comes from `extruder`), and Orca's 3MF loader requires it — stripping it made real Orca reject every height-color slice (return code -6) in Phase C's own CI, so the behavior was reversed on real-slicer evidence (`print_handy/core/layer_gcode.py::remap`). `tests/pipeline/test_remap_exhaustive.py::test_rebuilt_layer_change_keeps_the_color_attribute` |
| Purge matrix | Met (updated) | `permute_project_settings` permutes `flush_volumes_matrix` on both axes (a size mismatch refuses). Swap-tested: in a 1↔2 swap the from-1-to-2 and from-2-to-1 entries (100 and 200) trade places (`test_generic_remap_permutates_every_reference_and_preserves_the_rest`); registry rule `flush-matrix-permutation` |
| Purge vector | **Partial (named refusal)** | `flush_volumes_vector` is not permuted under a non-identity mapping: its layout semantics are not established from this repository (only "two entries per filament" in one synthetic fixture), so `permute_project_settings` refuses and names the key rather than guess a layout (registry rule `flush-vector-unestablished-refusal`). The identity mapping passes it through untouched, and a one-filament collapse keeps the used filament's own pair — the one slice of the layout the fixture establishes. `tests/pipeline/test_remap_exhaustive.py::test_generic_remap_refuses_the_unestablished_flush_vector`, `::test_declare_one_filament_keeps_the_used_filaments_entries` |
| Palette and per-filament arrays | Met (updated) | Generic permutation in destination order for the palette and every per-filament array (an array of unexpected length refuses). The one-filament collapse keeps the *used* source filament's entries — temperatures, density, flow, profile id — resolving the audit's open question (entry 1 was previously kept regardless): `color_geometry._declare_one_filament` with `keep_index`; `tests/pipeline/test_remap_exhaustive.py::test_declare_one_filament_keeps_the_used_filaments_entries`, `::test_single_color_plate_carries_no_stale_filament_reference` |
| Sliced usage records (`slice_info.config`) | Met (updated or named) | Filament ids are remapped in the generic remapper (`package_remap.remap_slice_info`, unmapped id refuses). The single-color copy drops the member and names the drop in `dropped_entries` (usage is re-derived by slicing) |
| Embedded plate G-code (`Metadata/plate_N.gcode`) | **Refusal (named)** | The generic remapper refuses by name: bare G-code is never rewritten in this converter and its tool commands would contradict the new numbering. The single-color copy drops the member and names the drop. Not updated on any path: `tests/pipeline/test_remap_exhaustive.py::test_generic_remap_refuses_unclassified_and_embedded_gcode_members` |
| Filament sequence (`Metadata/filament_sequence.json`) | **Refusal (named)** | The generic remapper refuses a non-identity remap by name: what real packages store there beyond the embedded preset record is not established, and that record is provenance evidence about the *source* (criterion 2 compares against it), so rewriting it would falsify provenance. Copy paths preserve it byte-for-byte and name it in `preserved_unremapped_entries`. Identity remaps pass it through |
| Member classification | Met | Before any byte is written, `package_remap.member_handling` classifies every member (remap, preserve, refuse-G-code, refuse-sequence); an unclassified member refuses by name rather than being carried or dropped silently (registry rule `remap-member-classification`). Classified opaque members (thumbnails, relationship parts, vendor records, `Auxiliaries/`) are preserved byte-for-byte, and the production copy no longer drops `Auxiliaries/`: `tests/pipeline/test_remap_exhaustive.py::test_production_plate_preserves_auxiliaries_members`. The rebuilt export names every source member it does not carry in `dropped_members`, and strips dangling per-triangle `pid`/`p1`/`p2`/`p3` material references rather than emitting them (the face colors stay disclosed through the inventory's `face_colors_ignored` note) |
| Object-id policy | Met | Package remaps never renumber object ids; ids change only through the caller's explicit `object_map`, which translates every object-keyed reference (height ranges, model settings, plate instances) with the same map. The rebuilt export is the one renumbering path, which is why it refuses extruder-carrying height ranges (above). `tests/pipeline/test_remap_exhaustive.py::test_generic_remap_identity_preserves_object_ids_and_members` |
| Conservative cleanup; never silently drop colors | Met | The generic remapper accepts only a bijection onto a contiguous destination numbering — a collision (a merge) or a gap refuses, so merging stays a collapse-exporter decision, never a permutation (`package_remap.validate_mapping`; `tests/pipeline/test_remap_exhaustive.py::test_generic_remap_rejects_a_noncontiguous_or_colliding_mapping`). Every drop on every path is named in its report (`dropped_entries`, `dropped_members`, `dropped_part_settings`, `preserved_unremapped_entries`, designation-fallback notes). Job-level decisions remain remapped atomically on a copy (`print_handy/core/ai_fix.py::remapped_decisions`; `tests/test_ai_fix_985.py::test_remapped_decisions_rejects_partial_or_unknown_mapping_atomically`) |

Residue, stated exactly: three reference rows are refused by name rather
than updated in the generic remapper — `flush_volumes_vector` under a
non-identity mapping, `Metadata/filament_sequence.json` under a
non-identity mapping, and embedded plate G-code. Each refusal is named,
fail-closed and without partial output, which satisfies the criterion's
failure mode ("fail without partial output") and its "never silently
drop" rule; none of the three can be updated honestly from evidence in
this repository today (unestablished layout or sequence semantics, and
G-code this converter never rewrites). The verdict is therefore
partial rather than met: the update demand is unmet for those three
rows, and accepting the named refusals in their place is the close
decision.

Documentation correction resolved: Phase C rewrote
`docs/3MF-REMAP-INVENTORY.md` to the post-Phase C behavior, and the stale
statement flagged in the interim pass (pre-existing destination
preservation described as "not yet verified," although
`test_failed_remap_preserves_source_and_existing_destination_bytes`
verifies it) no longer appears there; the inventory's regression
coverage section now records the staged-write and preservation state
above.

## Criterion 5 — Versioned compatibility

Requirement (#1110 item 5): explicit compatibility rules identify
source/destination slicer versions, reason, and regression evidence;
evaluate percent versus millimeter line widths, incompatible
enumerations/values, source hardware-only keys and commands; record
profile provenance; no unexplained broad substitutions.

| Requirement element | Implementation evidence | Test evidence |
|---|---|---|
| Versioned rule registry | `print_handy/core/compat_registry.py`: `REGISTRY_VERSION` 1, 32 rules (`compat_registry.RULES` — the 25 Phase D rules plus Phase C's 7 remap rules; the registry version stays 1), each a `Rule` with a unique id, decision kind, source family and version applicability spec, destination family and spec, reason, exact parameters, and named regression evidence (tests and/or a documentation section) | `tests/pipeline/test_compat_registry.py::test_registry_has_a_version_and_unique_ids`, `::test_every_rule_carries_the_required_fields`, `::test_version_applicability_specs_parse`, `::test_every_evidence_reference_resolves` |
| Version applicability is honest about unknowns | `compat_registry.parse_version_spec` / `applies` / `rule_applies`: an unknown version (None) matches only an unrestricted (`any`) spec, so a version-scoped rule never silently governs an unversioned source or destination | `tests/pipeline/test_compat_registry.py::test_version_applicability_specs_parse` |
| The converter consults the registry (no parallel tables) | `author_settings` builds `PRUSA_KEYS` and `ORCA_ENUMS` from rule parameters and resolves translation, threshold, ironing, style and sequence rules by id at the call sites; `orca_adapter` builds `PROCESS_KEYS` from the `author-intent-allowlist` rule and resolves the remaining rules (sentinel normalization, template override, tower clamp, percent line widths, range clamp) by id | `tests/pipeline/test_compat_registry.py::test_converter_tables_are_the_registry_tables`, `::test_percent_line_width_parity_with_registry`, `::test_enum_fallback_parity_with_registry`, `::test_hardware_only_key_protection_parity_with_registry` |
| Percent versus millimeter line widths | Rule `orca-percent-line-width-to-bambu-mm`: Orca percentage `*_line_width` values are converted with the destination machine's nozzle diameter for a Bambu Studio destination; the conversion fails closed when the diameter cannot be read | `tests/pipeline/test_orca_adapter.py::test_bambu_fallback_converts_percent_line_widths` |
| Incompatible enumerations/values | Rule `orca-enum-fallback`: an author enumeration Orca does not accept is replaced by the destination template's value (or dropped to Orca's default) with a conversion note; the accepted sets are named per key in the rule | `tests/pipeline/test_author_settings.py::test_unknown_author_enum_falls_back_to_the_template`, `::test_valid_author_enums_pass_unchanged`, `::test_bad_enum_never_reaches_the_slice_profile` |
| Source hardware-only keys and commands | Rule `foreign-machine-key-removal` names the key families exactly (machine, motion, temperature, bed, G-code families) and removes them from adopted copies so Orca falls back to its default; the target template supplies machine setup | `tests/pipeline/test_foreign_project_adoption.py::test_derived_plate_reaches_orca_with_the_job_profile_not_the_foreign_one`, `::test_plugin_keys_are_stripped_from_an_imported_project`; `tests/pipeline/test_port_contracts.py::test_shipped_protected_baseline_matches_the_orca_machine_template` |
| Profile provenance recorded | The registry declares the version-pair fields (`compat_registry.provenance_version_fields`); Phase A fills them in conversion provenance — see criterion 2 | See criterion 2 evidence |
| No unexplained broad substitutions | Every rule's parameters name exactly the keys, values, patterns or profile it governs; the one profile substitution (`arachne-flex-profile`) names its exact profile and trigger. Lookup of an unknown rule id refuses (`compat_registry.get_rule` raises) rather than falling back | `tests/pipeline/test_compat_registry.py::test_every_rule_carries_the_required_fields`, `::test_registry_lookup_refuses_an_unknown_id` |
| Out-of-registry items, with reasons | `docs/PRINTER-PROFILES.md` ("Versioned compatibility registry") records the decisions deliberately not in the registry, with reasons: the fitted nozzle's tuned profile and the ZMod purge profile (destination hardware facts owned by the printer profiles), source-profile compatibility warnings (display only; they convert nothing), and temperatures (they follow the loaded spool and filament family, never a slicer-pair rule) | Documentation verified against `print_handy/core/compat_registry.py` and the profile code at this stack tip |

The full rule table (id, kind, source, destination) is maintained in
`docs/PRINTER-PROFILES.md` under "Versioned compatibility registry"; the
integrity test above fails if a cited test or documentation section stops
existing, and the parity tests fail if the converter's live tables drift
from the registry.

## Criterion 6 — Safety

Requirement (#1110 item 6): actual loaded filament and printer calibration
outrank designer temperature and flow-ratio values by default; designer
values are evidence, not authority; validate purge tower/priming against
the destination architecture; unsupported features or unresolved mappings
block automated print readiness; warning plus "fix in slicer" is
insufficient; preserve all existing support, launch, authorization,
temperature and capacity gates.

| Requirement element | Implementation evidence | Test evidence |
|---|---|---|
| Loaded filament outranks designer values | Temperatures and filament flow values are not in the author-intent allowlist (`PROCESS_KEYS` carries no temperature key and no filament flow-ratio key), so a designer's values cannot ride over the job; `orca_adapter.slice_model` sets each slice's temperatures from the job's loaded spool on every slice | Allowlist pinned by `tests/pipeline/test_compat_registry.py::test_converter_tables_are_the_registry_tables`; conversion precedence audited by `tests/pipeline/test_foreign_audit_matrix.py` |
| Calibration outranks designer values | Machine, motion and calibration-bearing keys are removed from adopted copies (rule `foreign-machine-key-removal`) and supplied by the destination template; protected-settings baselines refuse a slice whose start or end G-code differs from the approved baseline | `tests/pipeline/test_port_contracts.py::test_shipped_protected_baseline_matches_the_orca_machine_template`; `tests/printers/test_vendor_profiles.py` (foreign machine macros) |
| Purge tower/priming validated against the destination | Rule `wipe-tower-bed-clamp` clamps the prime tower inside the destination bed using the job's effective tower width; the job's purge settings are generated for the destination architecture (`orca_adapter.ifs_purge_settings`) | `tests/pipeline/test_author_settings.py::test_tower_clamp_uses_the_effective_width_not_the_template_width`, `::test_every_plates_tower_stays_inside_the_bed`, `::test_slice_model_clamps_with_the_job_tower_width` |
| Unresolved mappings block readiness | Phase B's `require_plate_readiness` blocking (criterion 3) plus PR #1111's multi-plate guard: `source_colors.validate_plate_usage_if_present` / `require_plate_usage_known` refuse slicing when a claimed plate's usage is unknown | `tests/core/test_plate_usage.py` readiness tests (criterion 3); `tests/pipeline/test_color_geometry.py` usage-guard coverage |
| No existing gate weakened anywhere in the stack | Diff review of every phase: Phase D moved the converter's existing tables into the registry verbatim (parity tests pin equality, no behavior change); Phase A's only change to `print_handy/core/slice_guard.py` adds the read-only `slicer_version` helper and changes no guard verdict; Phase B's changes to `model_job`, `discord_color_intake` and `prepared_intake` add readiness calls and prompt scoping and remove no check (existing `validate_decisions` calls gained the selected-plate scope and slot capacity, nothing else); PR #1111 added refusals and preserved intake guard precedence | Support regression corpus, run at this stack tip and passing: `tests/pipeline/test_intake_support_precheck.py`, `tests/pipeline/test_intake_support_unverified_968.py`, `tests/test_supports_decision.py`, `tests/test_modeled_supports.py`; gate and guard suites are covered by CI on the exact head |

## Pre-program coverage (#1110 validation plan)

#1110 names existing work that must be documented, not re-implemented:

- #711 and #718 were addressed by merged #713; #716 and #717 by merged
  #723, which also added core color corpus coverage for #714.
- The foreign conversion audit matrix
  (`tests/pipeline/test_foreign_audit_matrix.py`) and adoption tests
  (`tests/pipeline/test_foreign_project_adoption.py`) predate this program
  and establish the kept/replaced/dropped contract summarized in
  `docs/PRINTER-PROFILES.md`; the ForgeBridge edge cases are covered by
  `tests/pipeline/test_forgebridge_edge_cases.py`. Print Handy follows
  ForgeBridge's published rules; no PrintNomad code, fixtures or
  dependencies are used anywhere in this stack (design research only, per
  #1110's attribution terms).
- `model_job.filament_usage_by_selection` (slicer-reported per-plate
  grams/meters by selection) predates Phase B; Phase B's analysis explains
  *why* a plate uses a filament and scopes the spool projection to the
  selected plates, rather than replacing the totals.
- The support incident regression corpus (intake support precheck,
  unverified-support handling, support decisions and modeled supports)
  is listed under criterion 6 and was run, unchanged, at this stack tip.

## Local verification runs for this pass

Run at the stack tip with the repository's test interpreter; counts are
quoted from the finished logs only:

- `tests/pipeline/test_author_settings.py` — 39 passed
- `tests/pipeline/test_compat_registry.py` — 9 passed
- `tests/core/test_plate_usage.py` — 29 passed
- `tests/hermes/test_print_messages.py` — 61 passed
- `tests/pipeline/test_foreign_audit_matrix.py`,
  `tests/pipeline/test_foreign_project_adoption.py`,
  `tests/pipeline/test_forgebridge_edge_cases.py`,
  `tests/pipeline/test_color_geometry.py`,
  `tests/pipeline/test_intake_support_precheck.py`,
  `tests/pipeline/test_intake_support_unverified_968.py`,
  `tests/test_supports_decision.py`, `tests/test_modeled_supports.py` —
  124 passed in one batch
- `tests/test_docs_index_contract.py` — 1 passed (65 subtests, including
  this page's new index entry)
- Final pass, at the Phase C tip: `tests/pipeline/test_remap_exhaustive.py`
  — 16 passed; one batch of `tests/pipeline/test_color_geometry.py`,
  `tests/pipeline/test_compat_registry.py`, `tests/core/test_plate_usage.py`,
  `tests/pipeline/test_foreign_project_adoption.py`,
  `tests/pipeline/test_forgebridge_edge_cases.py` and
  `tests/pipeline/test_author_settings.py` — 139 passed

## What remains

- The stack merges (the merges are catduck's). On merge, #1110's close
  decision rests on this final verification: criteria 1, 2, 3, 5 and 6
  met, and criterion 4 partial with the three named-refusal residues
  recorded above — accepting those refusals (each named, fail-closed and
  without partial output) in place of updates is the decision to make.
- If any residue is later resolved (a `flush_volumes_vector` layout or a
  `filament_sequence.json` sequence semantics established from real
  packages), the corresponding refusal can become an update and this
  matrix's criterion 4 row should be re-verified.
