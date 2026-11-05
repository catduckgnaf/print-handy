# Existing-suite fake-test audit (excluding tests/corpus)

| File:test | Problem | Fix applied |
|---|---|---|
| `tests/core/test_imports.py::test_every_core_module_imports` | Caught only `ModuleNotFoundError` for known third-party imports and called `skipTest`, causing the entire import-surface check to stop early and report a skip rather than prove the remaining core modules import. | Removed unittest skip behavior; collect known missing dependency failures and assert at the end, while unexpected import errors still fail immediately. |
| `tests/hermes/test_material_validation.py::test_legacy_known_material_explicit_targets_remain_allowed` | Called a validator without checking its result or exception, so regression to raising / failure result would still pass. | Assert the validator's documented `None` return (it is a validation-only routine). |
| `tests/pipeline/test_native_contract.py::test_native_start_accepts_baseline_without_offset` | Called a validator without checking its result. | Assert successful validation returns `None`. |
| `tests/pipeline/test_native_contract.py::test_native_guard_accepts_ready_present_matching_slot` | Called the guard without checking its result. | Assert successful validation returns `None`. |
| `tests/test_nozzle.py::test_check_nozzle_material_safety_abrasive_on_hardened_passes` | Four calls could silently accept an unexpected return value. | Assert each supported nozzle/material combination returns the documented `None`. |
| `tests/test_nozzle.py::test_check_nozzle_material_safety_non_abrasive_on_brass_passes` | Three calls could silently accept an unexpected return value. | Assert each safe combination returns the documented `None`. |
| `tests/hermes/test_listener_mesh.py::test_printing_and_paused_do_not_query_history_or_post` (duplicate definition) | Exact duplicate test/class definition shadowed the first test and duplicated no additional behavior. | Removed the duplicate definition; the remaining test still checks both states and interaction outcomes. |
| `tests/pipeline/test_model_job.py::GcodeBoundsTests.test_bounds_match_reference_on_real_gcode_samples` | Had an offline/empty-archive `skipTest`, so its independent reference comparison was skipped on a clean environment. | Replaced archive-dependent samples with deterministic local G-code fixtures; compare both parser outcomes against an independent reference. Narrowed its defensive catch to `RuntimeError`. |
| `tests/pipeline/test_model_job.py::ModelJobDetailsTests.test_job_details_shows_stored_full_clean_when_history_fails` | Skipped if an optional runtime module was unavailable. | Removed the conditional skip and load the module directly so the test fails if its required runtime surface is unavailable. |

Audit counts: 9 test definitions fixed, including 1 duplicate removed. Static audit covered 114 test modules under core, pipeline, bot, Hermes, printers, slicer, services, and top-level tests; `tests/corpus/` was excluded. No `assert True` or test with no assertion survived the audit. No permanently skipped-test marker was found; the remaining catch-alls are intentional resilient production-behavior assertions and were reviewed.

## New behavior coverage

Added `tests/core/test_prepared_intake_units.py` and `tests/pipeline/test_model_intake_audit_placement.py` for geometry, topology, model audit, archive/XML/STL edge cases, worker failure/timeout/concurrency, and STL-file-to-audit-to-placement behavior. Full corpus recovery results and known hangs/failures are documented in `RECOVERY-TESTING.md`.

Known observed bug, since fixed: `orca_adapter.placement_candidates` used to accept NaN and positive or negative infinity and return a report. `test_placement_nonfinite_coordinates_do_not_produce_successful_candidate_report` now asserts those coordinates raise, with no xfail marker.
