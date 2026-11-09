# 3MF conversion evidence matrix (#1110)

This page is the documentation and evidence matrix for issue #1110 (foreign
3MF conversion: designer intent, provenance, filament mapping,
compatibility). It maps every acceptance criterion in #1110 to the
implementation and test evidence that establishes it, including coverage
that predates the #1110 completion program (#713, #723, the #714 corpus and
the existing support regression corpus), as #1110's validation plan
requires.

**Status: interim verification, 2026-11-09.** This pass verifies the
assembled stack at `feat/1110-b-per-plate-usage` (PR #1111, then Phase D
PR #1127, Phase A PR #1128, Phase B PR #1129). Criterion 4 is **pending
Phase C** (exhaustive atomic remapping, in build) and is recorded below
with its pre-Phase C baseline, not verified. This matrix is therefore not
the final verification: #1110 closes only on a clean final pass after
Phase C lands and the stack is merged. Verdicts here were established by
reading the code and tests at the stack tip and running the named evidence
tests, not by copying the phase pull requests' claims.

## Verdict summary

| # | Criterion | Verdict at this stack tip |
|---|-----------|---------------------------|
| 1 | Setting intent | Met |
| 2 | Per-job provenance | Met |
| 3 | Per-plate filament usage | Met |
| 4 | Atomic complete remapping | **Pending Phase C — not yet verified** |
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
| Painting detail | `source_colors._painting_usage` decodes `paint_color` states per part (unpainted and base-state triangles count the part's base extruder); PrusaSlicer `mmu_segmentation`, which is not decoded per state, counts every palette filament, exactly as the geometry exporters treat it | `tests/core/test_plate_usage.py::test_triangle_painting_adds_painted_filaments_to_their_plate`, `::test_unpainted_source_painting_class_is_known_empty` |
| Wall/infill/tower accounting per format | `source_colors._prusa_role_extruders` reads PrusaSlicer per-feature extruders (`perimeter_extruder`, `infill_extruder`, `solid_infill_extruder`) as the wall and infill assignments. The Bambu/Orca and PrusaSlicer formats name no distinct wall, infill or prime-tower filament, so those classes are recorded `not_carried_by_format` — a stated limitation, not a hidden filament: walls and infill print with the object filament already counted under `objects`, and the tower prints with the filaments in play | `tests/core/test_plate_usage.py::test_wall_infill_tower_are_not_carried_by_an_orca_project`, `::test_prusa_role_extruders_are_read_from_the_prusa_config` |
| Unknown stays unknown, never guessed or silently zero | `_plate_usage` marks a class `unknown` when the source should carry it but it cannot be determined (a part with no extruder assignment, a tool change naming no extruder, unparsable height ranges, undecodable paint); a disagreement between the analysis and the slicer-reported totals in `slice_info.config`, or a damaged slicer usage record, makes the plate's usage unknown with the disagreement recorded | `tests/core/test_plate_usage.py::test_layer_change_without_an_extruder_is_unknown_not_zero`, `::test_malformed_height_ranges_are_unknown_not_absent`, `::test_part_without_extruder_assignment_is_unknown`, `::test_slicer_disagreement_makes_the_plate_usage_unknown`, `::test_invalid_slicer_record_makes_the_plate_usage_unknown`, `::test_slicer_reported_totals_merge_when_they_agree` |
| Selected-plate-only prompts | `source_colors.prompts` and `source_colors.canonical_prompts` accept `selected_plate_ids` and scope through `source_colors._selected_scope`; the chat intake, the Discord color flow and the color-plan packet all generate questions from this same prompt layer (call sites in `print_handy/core/model_job.py`, `print_handy/core/discord_color_intake.py`, `print_handy/core/prepared_intake.py`). A filament used only through a class with no question of its own still gets a question naming the class | `tests/core/test_plate_usage.py::test_canonical_prompts_cover_only_the_selected_plate`, `::test_grouped_and_legacy_prompts_scope_the_same_way`, `::test_painted_prompts_scope_to_the_selected_plates_filaments`, `::test_layer_change_rows_scope_to_their_plate`, `::test_height_range_filament_gets_a_question`, `::test_scoped_decisions_validate_against_the_scoped_questions` |
| Readiness blocking | `source_colors.plate_readiness_problems` / `require_plate_readiness` block, with the reason named, when a selected plate has an unknown usage class, a slicer/analysis disagreement, or a used filament with no validated slot mapping; called from `model_job.intake_color_choices` and `model_job.slice_job` with the destination's slot capacity. Unselected plates never prompt or block; the spool projection counts only the selected plates' validated slicer-reported grams (`model_job._selected_plate_stats`, `model_job.filament_usage_by_selection`) | `tests/core/test_plate_usage.py::test_ready_when_every_selected_plate_filament_is_mapped`, `::test_unmapped_selected_filament_blocks_with_a_named_reason`, `::test_unselected_plates_never_block`, `::test_unknown_usage_blocks_only_the_selected_plate`, `::test_disagreement_blocks_the_selected_plate`, `::test_missing_decisions_block`, `::test_single_color_selection_maps_every_used_filament`, `::test_spool_projection_counts_only_the_selected_plate` |
| Original IDs; color, slot and nozzle kept distinct | Usage records carry logical source filament IDs end to end (a plate using only filament 3 is recorded as filament 3, never renumbered); decisions map source IDs to loaded slots; readiness validates slots against real loaded-slot capacity (`model_job.position_limit`). The AD5X's four IFS spools share one nozzle, and that shared nozzle adds no slots: a mapping naming a fifth slot is not validated | `tests/core/test_plate_usage.py::test_object_assignments_are_per_plate_with_original_ids`, `::test_shared_nozzle_destination_still_validates_real_slot_capacity` |

## Criterion 4 — Atomic complete remapping (pending Phase C)

Requirement (#1110 item 4): update every reference, including paint,
parts, feature/support assignments, height ranges, layer changes, purge
matrices and vectors; preserve geometry and paint regions; leave the
original immutable; fail without partial output; cleanup/merging is
conservative and allowed only when usage is proven; never silently drop
colors.

**Verdict: not yet verified — pending Phase C.** Phase C (exhaustive
remapping) is in build on top of Phase B and has not landed at this stack
tip, so no verdict is claimed here. The final Phase E pass will verify
this criterion against the landed Phase C implementation.

Baseline at this stack tip, from `docs/3MF-REMAP-INVENTORY.md` and the
Phase C reference audit (28 reference types inventoried, R01–R28):

| Audit verdict | Count | Meaning |
|---|---|---|
| (a) Remapped atomically | 4 of 28 | Object extruder (R08), part extruder (R09), `paint_color` triangle paint (R15), per-layer tool changes (R21) |
| (b) Read but not remapped | 8 of 28 | Used for inventory, questions, validation or display only |
| (c) Rewritten non-atomically | 7 of 28 | Rewritten, truncated, folded or regenerated outside the validate-then-write discipline |
| (d) Not read | 2 of 28 | Values never decoded (PrusaSlicer `mmu_segmentation` states; embedded sliced G-code) |
| (e) Not applicable | 7 of 28 | Carries no filament/color/extruder identity to remap |

What already holds at this baseline: the layer archive path
(`print_handy/core/layer_gcode.py::remap_archive`) stages, validates and
renames atomically; the exporters in `print_handy/core/color_geometry.py`
validate completely before writing, so a validation failure creates no
output and preserves a pre-existing destination, and the source is never
modified (`tests/pipeline/test_color_geometry.py::test_failed_remap_preserves_source_and_existing_destination_bytes`,
`tests/pipeline/test_color_geometry.py::test_archive_layer_remap_preserves_members_and_is_atomic`,
`tests/pipeline/test_color_geometry.py::test_production_plate_recolor_preserves_unrelated_zip_members`).
Job-level filament decisions are remapped atomically on a copy
(`print_handy/core/ai_fix.py::remapped_decisions`;
`tests/test_ai_fix_985.py::test_remapped_decisions_rejects_partial_or_unknown_mapping_atomically`).

One documentation correction is owed by the final pass, recorded here so
it is not lost: the "Regression coverage status" paragraph of
`docs/3MF-REMAP-INVENTORY.md` says pre-existing destination preservation
is not yet verified, but
`test_failed_remap_preserves_source_and_existing_destination_bytes` does
verify it for the rebuilt export's failure path. The inventory file itself
is Phase C's working surface, so this page records the discrepancy rather
than editing that file mid-phase.

## Criterion 5 — Versioned compatibility

Requirement (#1110 item 5): explicit compatibility rules identify
source/destination slicer versions, reason, and regression evidence;
evaluate percent versus millimeter line widths, incompatible
enumerations/values, source hardware-only keys and commands; record
profile provenance; no unexplained broad substitutions.

| Requirement element | Implementation evidence | Test evidence |
|---|---|---|
| Versioned rule registry | `print_handy/core/compat_registry.py`: `REGISTRY_VERSION` 1, 25 rules (`compat_registry.RULES`), each a `Rule` with a unique id, decision kind, source family and version applicability spec, destination family and spec, reason, exact parameters, and named regression evidence (tests and/or a documentation section) | `tests/pipeline/test_compat_registry.py::test_registry_has_a_version_and_unique_ids`, `::test_every_rule_carries_the_required_fields`, `::test_version_applicability_specs_parse`, `::test_every_evidence_reference_resolves` |
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

## What remains

- Phase C lands (exhaustive atomic remapping) and this matrix's
  criterion 4 row is verified in a final pass.
- The `docs/3MF-REMAP-INVENTORY.md` correction noted under criterion 4.
- The stack merges (the merges are catduck's); only a clean final pass
  after those merges closes #1110.
