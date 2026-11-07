# Test-suite audit and cleanup accounting (2026-11-07)

Requested by catduck after a CI screenshot showed a wall of skips: "some of
the things like openscad need to be installed, there are dead references in
more, we need a deep and thorough accounting of all the testing for dead
things", refined to: go through every single test line by line; the
tesseract, reference-model, fixture and hermes references are specifically
suspect.

## Method and evidence

- Every file under `tests/` was inventoried file by file (538 Python files
  after this cleanup; the per-file inventory is the appendix below, with an
  AST count of test functions/classes per file and every skip/xfail gate
  line in that file). Files with no problem carry a "reviewed, clean" line
  so the coverage of the audit itself is provable.
- Every skip, `skipif`, `pytest.skip`, `importorskip` and `xfail` in the
  tree was classified against three evidence sources: the product code
  (does the gated surface still exist?), the shipped image (the Dockerfile
  runtime stage), and the workflows in `.github/workflows/` (does any job
  satisfy the gate?).
- The screenshot baseline was identified exactly: CI run 42416040044 on PR
  #1022 (branch `feat/1002-phase2-roasts`, head `ecdfa3e`), job
  `offline-tests (3.12, shard 2)`: **4 failed, 4047 passed, 72 skipped,
  8244 deselected, 11 warnings, 707 subtests passed in 362.73s**.

## The 4 screenshot failures are not on main

The 4 failures in that shard were
`test_visual_check_alerts_on_air_uncertain_and_endpoint_error`,
`test_empty_idle_on_layer_one_rechecks_within_60s_and_alerts_on_second` and
`test_concurrent_suspicious_checks_pause_only_once`
(all `tests/hermes/test_visual_print_watch.py`) plus
`SettingsParityTests.test_every_documented_env_name_is_accepted_by_the_loader`
with `env='PRINT_HANDY__VISION__ROAST_MODEL'` (`tests/test_docker_release.py`).
The other shards of the same run failed on sibling tests from the same two
files. Evidence they are #1022-branch-specific, not main defects:

- On main, `print_handy/hermes/moonraker_event_listener.py` defines
  `_visual_alert(filename, image, reason, identity=None)` with no `verdict`
  keyword; the branch's product code calls it with `verdict=`, which is what
  the failing tests' doubles reject.
- On main, `vision.roast_model` exists (`print_handy/settings.py`,
  `print_handy/webui/catalog.py`, `print_handy/ai_routes.py`); the branch
  removes the AI roast feature, and its settings-parity failures are that
  removal landing incompletely.
- Running both files on fresh `origin/main` in this cleanup's worktree:
  60 passed, 840 subtests passed.

They are therefore **not** fixed in this PR (this PR is based on main and
does not stack on #1022); they belong to the #1022 lane.

## Deselection accounting (the 8244)

The 8244 deselected in every offline shard are **not dead tests**. Two
mechanisms explain all of them:

1. Sharding: `tests/conftest.py` (`pytest_collection_modifyitems`) deals
   collected items round-robin across `PRINT_HANDY_TEST_SHARD=index/3`;
   each of the 3 shards deselects the other two thirds (~12,366 collected,
   ~4,122 kept per shard).
2. The edit corpus: `pytest_ignore_collect` in the same conftest keeps
   `tests/corpus/edit` out of the offline shards unless
   `PRINT_HANDY_EDIT_CORPUS=1`; dedicated `edit-corpus-offline` and
   `edit-corpus-real` jobs in `ci.yml` run it.

Verdict: keep. Nothing is dead by deselection; every deselected item runs
in another shard or a dedicated job on the same PR.

## Findings and actions

| # | Category | Location | Verdict | Action taken | Evidence |
|---|---|---|---|---|---|
| 1 | Missing binary (shipped dependency) | `tests/core/test_design.py:29`, `tests/pipeline/test_render_preview.py:12`, `tests/test_design_dispatch.py:33` (openscad/xvfb-run) | install-and-run | `ci.yml` offline legs now install `openscad xvfb xauth` (step modeled on the existing ffmpeg/jq steps) | Dockerfile runtime stage installs openscad and xvfb; `print_handy/core/design.py`, `cad_split.py` and `render_preview.py` shell out to openscad; `docs/DESIGN.md` and `docs/STANDALONE.md` document it as a requirement |
| 2 | Missing binary (shipped dependency) | `tests/hermes/test_issue_514_photo_intake.py:589` (tesseract) | install-and-run | `ci.yml` offline legs now install `tesseract-ocr` in the same step | Dockerfile runtime stage installs tesseract-ocr; `print_handy/hermes/spool_label.py:376-417` is the live local OCR route and only offers itself when the binary exists |
| 3 | Extension not built in job | `tests/pipeline/test_native_bounds.py:73,82,126,236` (38 skips in the screenshot shard) | install-and-run | `ci.yml` offline legs build the bounds wheel exactly as the `native-bounds` job does (pinned rustup 1.99.1, maturin 1.16.0, skeleton staging, wheel install, `.so` mirrored into the source package) and set `PRINT_HANDY_BOUNDS_REQUIRE_NATIVE=1` so a missing extension hard-fails instead of skipping | The dedicated `native-bounds` job (3.12 and 3.14) already ran this file with the extension; the offline legs ran the same file without building it |
| 4 | Dead fixture reference (host-only folder) | `tests/pipeline/test_color_geometry.py` (5 tests), `tests/pipeline/test_tower_conflicts.py:66` (1 test) — `PRINT_HANDY_COUPONS` / sol_recovery coupon 3MF | repair with shipped fixture | Tests now use the shipped corpus case `tests/corpus/cases/assembly-3c-tight/model.3mf` (3 parts, 3 source extruders, inventory-verified); the env gate is deleted. The `painted` refusal case now builds the file's own real painted fixture (`_painted_two_plate_source`) and drops the `filament:2` decision so a painted state is genuinely uncovered | No workflow sets `PRINT_HANDY_COUPONS`; the folder exists only on one operator host; the corpus case satisfies every assertion the coupon tests make (verified by running them) |
| 5 | Dead fixture variant (host-only recording) | `tests/pipeline/test_multicolor_contract.py` `reviewed-orca` parametrization of `test_public_prepare_verify_launch` (8 cases) | remove-dead (variant only) | Parametrization removed; the synthetic variant (same flow) stays and runs everywhere | The variant only re-ran the identical flow against recorded Orca G-code pinned by SHA-256 from the same sol_recovery folder; real Orca G-code is covered by the Slicer test jobs |
| 6 | Dead reference model (archive path) | `tests/pipeline/test_orca_e2e_postit.py` (whole file, 1 test) | remove-dead | File deleted | It globs the operator's print archive for the post-it model, but `tests/conftest.py` replaces the archive path with an empty temp dir, so the test skipped on every machine including the operator's; it also needed a real Orca flatpak and printer hardware state. Per-part slicing of multi-part 3MFs is covered by the corpus and slicer jobs |
| 7 | Dead reference model (archive path) | `tests/pipeline/test_part_families_split.py` `test_postit_merges_on_one_plate_with_tower` | remove-dead | Test removed (with the now-unused archive/settings lines); a comment in the file records why | Same archive-glob mechanism as #6: always skipped under the test harness. Family grouping stays covered by the synthetic-manifest tests in the same file |
| 8 | Dead fixture + hardcoded personal path | `tests/pipeline/test_support_clearance.py:12,124` — V14 fixture at a hardcoded `/home/<operator>/.hermes/profiles/<profile>/...` path (full literal deliberately not re-quoted here) | repair (rewrite) | `test_component_based_3mf_parses_transforms_no_false_positives` builds a synthetic component-based 3MF in `tmp_path`: two components, one authored 0.4 mm above the plate and pulled onto it by its component transform (a dropped transform would flag its 900 mm2 underside). The hardcoded path is gone — it was the only personal home path in the whole test tree | Path search across `tests/`: this was the sole `/home/<name>/` literal that is not an intentional fake in a sanitizer test. The phase-2 file in the same area already establishes the synthetic-fixture pattern for these Pixel Patina conditions |
| 9 | Dead external import | `tests/hermes/conftest.py` `hermes_cli_plugins` fixture + 2 tests in `tests/hermes/test_print_feedback_guard.py:327,341` | rewrite against live surface | The two tests now call the plugin's registered hooks directly (`cap_discord_clarifications`, `guard_terminal_loop`) and assert the same outcomes through their documented dict returns; the fixture is deleted | `hermes_cli` is the external Hermes Agent package: this repo references it only as a host module name (`print_handy/bot/health.py` `HERMES_MODULES`), nothing in the repo or any workflow provides it, so both tests skipped everywhere. The plugin itself is live: `plugins/print-feedback/__init__.py` `register()` binds both functions as `pre_tool_call` hooks, and the hook-registration test in the same file pins that |
| 10 | Stale skip guard (surface now exists) | `tests/pipeline/test_discord_color_hook.py:18` | repair | The `hasattr` skip guard became an assertion | `color_selection_dispatch` exists at `plugins/print-feedback/__init__.py:1469`; the guard predated it landing and could only mask a future regression |
| 11 | Stale skip guard (parameter now exists) | `tests/core/test_spool_canon.py:103` | repair | The `TypeError`-to-skip wrapper is removed | `create_spool(spec, *, client=...)` exists at `print_handy/hermes/spool_sync.py:1809` |
| 12 | Env gate no workflow sets | `tests/corpus/test_complex_corpus.py:239` `COMPLEX_CORPUS_ROOT` | repair (align) | The test now also accepts `PRINT_HANDY_PRIVATE_CORPUS`, the root the model-corpus workflow and `run_complex_corpus.py` already use | `model-corpus.yml` sets only `PRINT_HANDY_PRIVATE_CORPUS`; `run_complex_corpus.py` already falls back to it. Named follow-up: `model-corpus.yml` runs the corpus runner scripts, not pytest, so this manifest check still only executes on a machine that fetched the corpus — wiring it into that workflow is left as a follow-up rather than expanding this PR |
| 13 | Legitimate opt-in gates | `tests/slicer/test_*_real.py` (`PRINT_HANDY_REAL_SLICER` / `PRINT_HANDY_REAL_BAMBU`), `tests/services/test_spoolman_real.py` (`PRINT_HANDY_SPOOLMAN_TEST_URL`), ffmpeg/ffprobe gates, `container` marker, POSIX/Windows gates, `tests/pipeline/conftest.py` real-slicer marker | keep-gated | No change | Each is satisfied by a named job: Slicer test (stable) and (bambu) set the real-slicer vars; the `spoolman` job starts a throwaway Spoolman and sets the URL; the offline legs install ffmpeg (and now openscad/tesseract) and set `PRINT_HANDY_IN_CONTAINER=1`; platform gates are correct by construction |
| 14 | Vestigial import guards | `pytest.importorskip("trimesh")` (3 sites), discord.py ImportError skip in `tests/corpus/test_user_stories.py:87`, zxing-cpp gates in `test_issue_514_photo_intake.py` | keep (harmless, never fire in CI) | No change | trimesh is in the `[test]` extra, discord.py and zxing-cpp are runtime dependencies in `pyproject.toml`, so CI installs all of them; the guards only help partial local environments |
| 15 | Strict xfail register | `tests/strict_xfail_register.json`, `tests/corpus/test_corpus.py:63` | keep | No change | One registered strict xfail (corpus `known_failure` flow), owner named, expiry 2027-02-04, enforced by `scripts/check_strict_xfails.py` in the offline legs |
| 16 | Script-style suites outside pytest | `tests/docker/` (image acceptance), `tests/corpus/time_corpus.py` / `timing_gate.py` / runner scripts | keep | No change | Run by `docker-image-validation.yml`, `develop.yml`, `docker-publish.yml` (acceptance/lifecycle) and the `discord-timing` / `slicer-timing-real` jobs in `ci.yml` |

## OpenSCAD: explicit section (catduck: "we absolutely need lots of OpenSCAD testing")

OpenSCAD is a shipped product dependency (Dockerfile runtime stage), not
an optional extra, so its tests are install-and-run, never removal
candidates. Product paths that shell out to the binary, and their coverage
before this PR:

| Product path | Real-binary tests before | After this PR |
|---|---|---|
| Design engine render + repair loop (`print_handy/core/design.py`, runner seam at `design.py:662`) | `tests/core/test_design.py`: 8 tests gated `needs_scad` — all skipped in CI | Gate now executes in every offline leg (openscad + xvfb installed) |
| Preview rendering (`print_handy/core/render_preview.py`, `_openscad_cmd` at line 19) | `tests/pipeline/test_render_preview.py`: whole module gated — all skipped in CI | Executes in every offline leg |
| Design dispatch end to end (`tests/test_design_dispatch.py`) | 12 tests gated `needs_scad` — all skipped in CI (6 of them are the screenshot's dispatch skip lines; the rest sit in other shards) | Executes in every offline leg |
| CAD split execution (`print_handy/core/cad_split.py` `_run_openscad`, line 477; called from `prepare_cad_split` with `execute_openscad=True`) | **No real-binary test existed**: every execution test faked `_run_openscad` or ran with `execute_openscad=False` | Added `tests/core/test_cad_split.py::test_run_openscad_real_binary_exports_and_refuses_bad_source`: real binary exports a 10x20x30 cube to a binary STL the repo's own reader parses with exact dimensions, and a syntax-broken source raises RuntimeError with no output |
| Sandbox pre-scan, source extraction, session store (`design.py` non-render paths) | Covered everywhere with a scripted fake model (`tests/core/test_design.py`, `tests/core/test_design_841.py` with an injected runner) | Unchanged — these paths do not need the binary, and the fakes pin the seams the real-binary tests then exercise |
| `bot check` design row (`print_handy/bot/__main__.py:301`) | Covered by `tests/bot/test_design_check_row.py` with `which` mocked | Unchanged — the row is a PATH probe, so a mock is the correct surface |

Coverage gap found and closed: the cad_split execution seam (row 4).
Remaining known limit: `prepare_cad_split`'s full multi-file split still
has no end-to-end real-binary run (its stages are pinned by the fake-run
tests plus the new seam test); that is a candidate for a follow-up, not a
skip — nothing in CI skips on its account.

## Before / after (offline legs)

Baseline is the screenshot job (run 42416040044, `offline-tests (3.12,
shard 2)`, on the #1022 branch): 4 failed, 4047 passed, 72 skipped, 8244
deselected. After numbers are quoted in the PR body from the finished CI
logs on this PR's exact head; the skip lines for openscad, xvfb-run,
tesseract and the native bounds extension, and the coupon, post-it and V14
skip lines, no longer appear because those tests execute (or were removed
as dead, per the table above).

## Appendix: per-file inventory

Every Python file under `tests/`, counted and gate-checked after this
cleanup. "reviewed, clean" means: the file was read, its imports resolve
to live modules, its fixture references exist in the repo, and it carries
no skip/xfail gate. Gate notes quote the gate line so a reader can find
the classification for that gate in the findings table or in the
keep-gated row.

Files inventoried: 538. Test functions/classes counted by AST: 7402.

| File | Tests | Status |
|---|---|---|
| `tests/bot/__init__.py` | 0 | reviewed, clean |
| `tests/bot/conftest.py` | 0 | reviewed, clean |
| `tests/bot/test_access.py` | 15 | reviewed, clean |
| `tests/bot/test_agent_lane.py` | 15 | reviewed, clean |
| `tests/bot/test_approval_buttons.py` | 15 | reviewed, clean |
| `tests/bot/test_approval_invariants.py` | 8 | reviewed; gates: L18:xfail; PR #987 (the #978 remediation) then landed the guard in; L158:# do is select a fix/waiver application — see the strict xfail below. |
| `tests/bot/test_auto_light_check.py` | 9 | reviewed, clean |
| `tests/bot/test_clarify_check_row.py` | 1 | reviewed, clean |
| `tests/bot/test_debug_mode_bot.py` | 4 | reviewed, clean |
| `tests/bot/test_default_channel_ownership.py` | 5 | reviewed, clean |
| `tests/bot/test_design_check_row.py` | 3 | reviewed, clean |
| `tests/bot/test_discord_no_llm.py` | 9 | reviewed, clean |
| `tests/bot/test_guided.py` | 21 | reviewed, clean |
| `tests/bot/test_guided_multi.py` | 6 | reviewed, clean |
| `tests/bot/test_guided_onboarding.py` | 28 | reviewed, clean |
| `tests/bot/test_health_and_transport.py` | 21 | reviewed, clean |
| `tests/bot/test_llm.py` | 18 | reviewed, clean |
| `tests/bot/test_local_detector_check.py` | 5 | reviewed, clean |
| `tests/bot/test_login_connect_flow.py` | 3 | reviewed, clean |
| `tests/bot/test_matrix.py` | 30 | reviewed, clean |
| `tests/bot/test_outage_notices.py` | 4 | reviewed, clean |
| `tests/bot/test_platform.py` | 10 | reviewed, clean |
| `tests/bot/test_print_number.py` | 6 | reviewed, clean |
| `tests/bot/test_progress_editing.py` | 4 | reviewed, clean |
| `tests/bot/test_prompts.py` | 8 | reviewed, clean |
| `tests/bot/test_rest.py` | 6 | reviewed, clean |
| `tests/bot/test_routing.py` | 19 | reviewed, clean |
| `tests/bot/test_runtime.py` | 35 | reviewed, clean |
| `tests/bot/test_secret_message_delete.py` | 7 | reviewed, clean |
| `tests/bot/test_shared_channel.py` | 25 | reviewed, clean |
| `tests/bot/test_start_bookkeeping_838.py` | 3 | reviewed, clean |
| `tests/bot/test_startup_retry.py` | 20 | reviewed, clean |
| `tests/bot/test_telegram.py` | 22 | reviewed, clean |
| `tests/bot/test_typed_file_selection.py` | 2 | reviewed, clean |
| `tests/bot/test_typed_reply_scope.py` | 9 | reviewed, clean |
| `tests/conftest.py` | 0 | reviewed; gates: L161:skip_container = pytest.mark.skip( |
| `tests/core/__init__.py` | 0 | reviewed, clean |
| `tests/core/test_assembly_check.py` | 11 | reviewed, clean |
| `tests/core/test_cad_split.py` | 11 | reviewed; gates: L173:@pytest.mark.skipif(not shutil.which('openscad'), reason='openscad not installed') |
| `tests/core/test_color_hardening.py` | 34 | reviewed, clean |
| `tests/core/test_compact_packing.py` | 2 | reviewed, clean |
| `tests/core/test_compact_stl.py` | 1 | reviewed, clean |
| `tests/core/test_compact_topology.py` | 2 | reviewed, clean |
| `tests/core/test_control_lock.py` | 13 | reviewed, clean |
| `tests/core/test_design.py` | 34 | reviewed; gates: L29:needs_scad = pytest.mark.skipif(not (shutil.which("openscad") and shutil.which("xvfb-run")), |
| `tests/core/test_design_841.py` | 20 | reviewed, clean |
| `tests/core/test_edit_plan.py` | 36 | reviewed, clean |
| `tests/core/test_exclude_object.py` | 3 | reviewed, clean |
| `tests/core/test_failure_report.py` | 23 | reviewed, clean |
| `tests/core/test_filament_color.py` | 30 | reviewed, clean |
| `tests/core/test_flex_print_slicing.py` | 9 | reviewed, clean |
| `tests/core/test_fuzzy.py` | 8 | reviewed, clean |
| `tests/core/test_fuzzy_hardening.py` | 22 | reviewed, clean |
| `tests/core/test_fuzzy_typos.py` | 19 | reviewed, clean |
| `tests/core/test_gcode_3mf.py` | 16 | reviewed, clean |
| `tests/core/test_geometry_layout.py` | 9 | reviewed, clean |
| `tests/core/test_imports.py` | 1 | reviewed, clean |
| `tests/core/test_interrupted_intake.py` | 5 | reviewed, clean |
| `tests/core/test_large_model_notice.py` | 3 | reviewed, clean |
| `tests/core/test_layer_gcode.py` | 10 | reviewed, clean |
| `tests/core/test_mesh_fast.py` | 15 | reviewed, clean |
| `tests/core/test_mesh_fast_adversarial.py` | 9 | reviewed, clean |
| `tests/core/test_no_printable_geometry.py` | 1 | reviewed, clean |
| `tests/core/test_non_finite_geometry.py` | 2 | reviewed, clean |
| `tests/core/test_orca_config.py` | 4 | reviewed, clean |
| `tests/core/test_part_translator.py` | 13 | reviewed, clean |
| `tests/core/test_prepared_intake_units.py` | 21 | reviewed; gates: L311:pytest.importorskip('trimesh') |
| `tests/core/test_printer_clock.py` | 19 | reviewed, clean |
| `tests/core/test_printer_geometry.py` | 4 | reviewed, clean |
| `tests/core/test_printer_state_temperatures.py` | 6 | reviewed, clean |
| `tests/core/test_rack_orientation.py` | 6 | reviewed, clean |
| `tests/core/test_resources.py` | 26 | reviewed; gates: L157:@pytest.mark.skipif(os.name != 'posix', reason='owned workers use process groups') |
| `tests/core/test_roast_engine.py` | 16 | reviewed, clean |
| `tests/core/test_settings_provenance.py` | 13 | reviewed, clean |
| `tests/core/test_sizing.py` | 33 | reviewed, clean |
| `tests/core/test_slice_failure.py` | 16 | reviewed, clean |
| `tests/core/test_slice_guard.py` | 37 | reviewed, clean |
| `tests/core/test_source_colors.py` | 5 | reviewed, clean |
| `tests/core/test_source_profile_warnings.py` | 13 | reviewed, clean |
| `tests/core/test_spacing_presets.py` | 5 | reviewed, clean |
| `tests/core/test_spool_canon.py` | 16 | reviewed, clean |
| `tests/core/test_spoolman_client.py` | 8 | reviewed, clean |
| `tests/core/test_start_bookkeeping_838.py` | 19 | reviewed, clean |
| `tests/corpus/__init__.py` | 0 | reviewed, clean |
| `tests/corpus/conftest.py` | 0 | reviewed, clean |
| `tests/corpus/design/mac_mini_rack_issue_998/generate_rack_mount.py` | 0 | reviewed, clean |
| `tests/corpus/design/mac_mini_rack_issue_998/make_dimension_diagram.py` | 0 | reviewed, clean |
| `tests/corpus/design/mac_mini_rack_issue_998/make_previews.py` | 0 | reviewed, clean |
| `tests/corpus/design/mac_mini_rack_issue_998/validate_fixtures.py` | 0 | reviewed, clean |
| `tests/corpus/edit/__init__.py` | 0 | reviewed, clean |
| `tests/corpus/edit/conftest.py` | 0 | reviewed, clean |
| `tests/corpus/edit/runner.py` | 0 | reviewed, clean |
| `tests/corpus/edit/test_corpus_edits.py` | 6 | reviewed, clean |
| `tests/corpus/edit/test_edit_phrasings.py` | 8 | reviewed, clean |
| `tests/corpus/edit/test_edit_v12.py` | 4 | reviewed, clean |
| `tests/corpus/edit/test_geometry_screening.py` | 4 | reviewed, clean |
| `tests/corpus/fake_discord.py` | 0 | reviewed, clean |
| `tests/corpus/fetch_complex_corpus.py` | 0 | reviewed, clean |
| `tests/corpus/fit.py` | 0 | reviewed, clean |
| `tests/corpus/geometry/detect_underfaces_reference.py` | 0 | reviewed, clean |
| `tests/corpus/geometry/make_geometry_fixtures.py` | 0 | reviewed, clean |
| `tests/corpus/harness.py` | 0 | reviewed, clean |
| `tests/corpus/make_models.py` | 0 | reviewed, clean |
| `tests/corpus/matrix.py` | 0 | reviewed, clean |
| `tests/corpus/printer_overrides.py` | 0 | reviewed, clean |
| `tests/corpus/run_complex_corpus.py` | 0 | reviewed, clean |
| `tests/corpus/run_external_corpus.py` | 0 | reviewed, clean |
| `tests/corpus/run_external_edit_corpus.py` | 0 | reviewed, clean |
| `tests/corpus/spool_fixture.py` | 0 | reviewed, clean |
| `tests/corpus/test_complex_corpus.py` | 16 | reviewed; gates: L239:@pytest.mark.skipif(not (os.environ.get("COMPLEX_CORPUS_ROOT") |
| `tests/corpus/test_corpus.py` | 17 | reviewed; gates: L54:is open; its flow tests then xfail (strict, so the marker must be removed; L56:be listed in tests/strict_xfail_register.json; scripts/check_strict_xfails.py; L63:marks=pytest.mark.xfail(strict=True, reason=str(reason)))); L412:@pytest.mark.skipif(REAL, reason="transport does not depend on slicer; mock tier covers it") |
| `tests/corpus/test_external_answers.py` | 4 | reviewed, clean |
| `tests/corpus/test_failure_messages.py` | 2 | reviewed; gates: L27:@pytest.mark.skipif(REAL, reason="the guard reads mock-tier chat posts; the real slicer runs elsewhere") |
| `tests/corpus/test_journey_matrix.py` | 4 | reviewed, clean |
| `tests/corpus/test_mac_mini_rack_fixture_998.py` | 5 | reviewed, clean |
| `tests/corpus/test_runner_contracts.py` | 11 | reviewed, clean |
| `tests/corpus/test_spool_fixture.py` | 3 | reviewed, clean |
| `tests/corpus/test_timeout_marker.py` | 1 | reviewed, clean |
| `tests/corpus/test_timing_gate.py` | 21 | reviewed, clean |
| `tests/corpus/test_user_stories.py` | 5 | reviewed; gates: L87:pytest.skip("discord.py is not installed for the system Python") |
| `tests/corpus/time_corpus.py` | 0 | reviewed, clean |
| `tests/corpus/timing_gate.py` | 0 | reviewed, clean |
| `tests/corpus/timing_variance.py` | 0 | reviewed, clean |
| `tests/docker/fake_moonraker.py` | 0 | reviewed, clean |
| `tests/docker/image_probe.py` | 0 | reviewed, clean |
| `tests/docker/lifecycle_backup.py` | 0 | reviewed, clean |
| `tests/fixtures/support_coverage_969/generate_fixtures.py` | 0 | reviewed, clean |
| `tests/gcode_factory.py` | 0 | reviewed, clean |
| `tests/hermes/__init__.py` | 0 | reviewed, clean |
| `tests/hermes/conftest.py` | 0 | reviewed, clean |
| `tests/hermes/fixtures/build_tiny_detector.py` | 0 | reviewed, clean |
| `tests/hermes/test_abandoned_start.py` | 17 | reviewed, clean |
| `tests/hermes/test_bambu_watch.py` | 47 | reviewed; gates: L766:@_pytest.mark.skipif(not (_FFMPEG and _FFPROBE), reason="needs ffmpeg and ffprobe on PATH") |
| `tests/hermes/test_bed_separate.py` | 23 | reviewed, clean |
| `tests/hermes/test_boundary_integration.py` | 21 | reviewed, clean |
| `tests/hermes/test_clarify_plugin.py` | 8 | reviewed, clean |
| `tests/hermes/test_clarify_retire.py` | 9 | reviewed, clean |
| `tests/hermes/test_clarify_ui.py` | 14 | reviewed, clean |
| `tests/hermes/test_clarify_wiring.py` | 10 | reviewed, clean |
| `tests/hermes/test_coldpull_acceptance.py` | 13 | reviewed, clean |
| `tests/hermes/test_completion_delivery.py` | 12 | reviewed, clean |
| `tests/hermes/test_component_ack_order.py` | 8 | reviewed; gates: L6:fails this file. The spool-question same/cancel gap was a strict xfail until the handlers acknowledged; L7:first (P2-164); those markers are removed, and a new strict xfail belongs in; L8:tests/strict_xfail_register.json with an owner and an expiry.; L151:"""Was a strict xfail for the #594 phase 2 gap (the handler awaited the |
| `tests/hermes/test_control_lock_interprocess.py` | 2 | reviewed, clean |
| `tests/hermes/test_elegoo_watch.py` | 19 | reviewed; gates: L364:@pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="needs ffmpeg and ffprobe on PATH") |
| `tests/hermes/test_estimate_breakdown.py` | 13 | reviewed, clean |
| `tests/hermes/test_exclude_chat.py` | 10 | reviewed, clean |
| `tests/hermes/test_family_auto_light.py` | 22 | reviewed, clean |
| `tests/hermes/test_family_control.py` | 19 | reviewed, clean |
| `tests/hermes/test_family_event_listener.py` | 16 | reviewed, clean |
| `tests/hermes/test_family_watch_pauses.py` | 6 | reviewed, clean |
| `tests/hermes/test_hosting_profile.py` | 15 | reviewed, clean |
| `tests/hermes/test_issue_512_intake.py` | 24 | reviewed, clean |
| `tests/hermes/test_issue_514_photo_intake.py` | 47 | reviewed; gates: L589:@pytest.mark.skipif(not spool_label.tesseract_available(), reason="no tesseract binary"); L593:pytest.skip("no TrueType font to render a label with"); L638:@pytest.mark.skipif(not spool_codes.codes_available(), reason="zxing-cpp not installed"); L726:@pytest.mark.skipif(not spool_codes.codes_available(), reason="zxing-cpp not installed") |
| `tests/hermes/test_issue_792_picker_hardening.py` | 14 | reviewed, clean |
| `tests/hermes/test_klipper_report.py` | 11 | reviewed, clean |
| `tests/hermes/test_klipper_report_filters.py` | 6 | reviewed, clean |
| `tests/hermes/test_lane_b.py` | 6 | reviewed, clean |
| `tests/hermes/test_lane_data_sync.py` | 46 | reviewed, clean |
| `tests/hermes/test_legacy_slots_key.py` | 4 | reviewed, clean |
| `tests/hermes/test_light_control.py` | 24 | reviewed, clean |
| `tests/hermes/test_listener_auto_light.py` | 3 | reviewed, clean |
| `tests/hermes/test_listener_mesh.py` | 16 | reviewed, clean |
| `tests/hermes/test_loaded_spools.py` | 15 | reviewed, clean |
| `tests/hermes/test_loaded_spools_fuzzy.py` | 8 | reviewed, clean |
| `tests/hermes/test_local_vision.py` | 61 | reviewed, clean |
| `tests/hermes/test_maintenance.py` | 8 | reviewed, clean |
| `tests/hermes/test_material_matrix.py` | 6 | reviewed, clean |
| `tests/hermes/test_material_temps.py` | 10 | reviewed, clean |
| `tests/hermes/test_material_validation.py` | 5 | reviewed, clean |
| `tests/hermes/test_monitor_worker.py` | 8 | reviewed, clean |
| `tests/hermes/test_native_error_alert.py` | 4 | reviewed, clean |
| `tests/hermes/test_next_plate_offer.py` | 8 | reviewed, clean |
| `tests/hermes/test_nozzle_change_workflow.py` | 6 | reviewed, clean |
| `tests/hermes/test_octo_watch.py` | 19 | reviewed, clean |
| `tests/hermes/test_panic.py` | 13 | reviewed, clean |
| `tests/hermes/test_paused_job_gone.py` | 6 | reviewed, clean |
| `tests/hermes/test_plan_timelapse.py` | 15 | reviewed, clean |
| `tests/hermes/test_platform_media_sizing.py` | 28 | reviewed, clean |
| `tests/hermes/test_print_alert_buttons.py` | 60 | reviewed, clean |
| `tests/hermes/test_print_control.py` | 50 | reviewed, clean |
| `tests/hermes/test_print_feedback_guard.py` | 20 | reviewed, clean |
| `tests/hermes/test_print_markers.py` | 18 | reviewed, clean |
| `tests/hermes/test_print_messages.py` | 50 | reviewed, clean |
| `tests/hermes/test_print_next.py` | 45 | reviewed, clean |
| `tests/hermes/test_print_number_followups.py` | 6 | reviewed, clean |
| `tests/hermes/test_print_record_timestamps.py` | 4 | reviewed, clean |
| `tests/hermes/test_print_records.py` | 21 | reviewed, clean |
| `tests/hermes/test_print_stop.py` | 18 | reviewed, clean |
| `tests/hermes/test_profile_platform_intake.py` | 1 | reviewed, clean |
| `tests/hermes/test_prusa_watch.py` | 20 | reviewed; gates: L331:@pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="needs ffmpeg and ffprobe on PATH") |
| `tests/hermes/test_purchase_intake.py` | 31 | reviewed, clean |
| `tests/hermes/test_reboot_standby.py` | 1 | reviewed, clean |
| `tests/hermes/test_roast_chat.py` | 6 | reviewed, clean |
| `tests/hermes/test_runout_pause_442.py` | 44 | reviewed, clean |
| `tests/hermes/test_runout_same_filament.py` | 6 | reviewed, clean |
| `tests/hermes/test_safe4_pause_alerts.py` | 2 | reviewed, clean |
| `tests/hermes/test_screenless_ifs_presence.py` | 25 | reviewed, clean |
| `tests/hermes/test_slot_load.py` | 11 | reviewed, clean |
| `tests/hermes/test_spool_guided.py` | 34 | reviewed, clean |
| `tests/hermes/test_spool_ifs_identity_regressions.py` | 6 | reviewed, clean |
| `tests/hermes/test_spool_question_cancel.py` | 15 | reviewed, clean |
| `tests/hermes/test_spool_question_stale_buttons.py` | 11 | reviewed, clean |
| `tests/hermes/test_spool_reconcile.py` | 22 | reviewed, clean |
| `tests/hermes/test_spool_swap_override.py` | 3 | reviewed, clean |
| `tests/hermes/test_spool_sync.py` | 34 | reviewed, clean |
| `tests/hermes/test_spoolman_watcher.py` | 20 | reviewed, clean |
| `tests/hermes/test_start_bookkeeping_838.py` | 13 | reviewed, clean |
| `tests/hermes/test_start_claim_paths.py` | 5 | reviewed, clean |
| `tests/hermes/test_state_file_isolation.py` | 5 | reviewed, clean |
| `tests/hermes/test_stock_watch.py` | 23 | reviewed; gates: L319:@pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="needs ffmpeg and ffprobe on PATH") |
| `tests/hermes/test_stop_button_view.py` | 4 | reviewed, clean |
| `tests/hermes/test_stop_capture.py` | 9 | reviewed, clean |
| `tests/hermes/test_stray_chat_dispatch.py` | 13 | reviewed, clean |
| `tests/hermes/test_summary_contract.py` | 5 | reviewed, clean |
| `tests/hermes/test_timelapse_capture.py` | 55 | reviewed, clean |
| `tests/hermes/test_timelapse_delivery_contract.py` | 5 | reviewed; gates: L163:@pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="needs ffmpeg and ffprobe on PATH") |
| `tests/hermes/test_timelapse_encode.py` | 46 | reviewed; gates: L526:@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),; L567:@pytest.mark.skipif(not _real_tools(), reason="ffmpeg/ffprobe not available"); L589:@pytest.mark.skipif(not _real_tools(), reason="ffmpeg/ffprobe not available"); L614:@pytest.mark.skipif(not _real_tools(), reason="ffmpeg/ffprobe not available") |
| `tests/hermes/test_timelapse_gpu_fallback.py` | 19 | reviewed, clean |
| `tests/hermes/test_timelapse_playback.py` | 26 | reviewed, clean |
| `tests/hermes/test_timelapse_rest_transport.py` | 4 | reviewed, clean |
| `tests/hermes/test_timelapse_states.py` | 8 | reviewed, clean |
| `tests/hermes/test_vertical_short.py` | 23 | reviewed, clean |
| `tests/hermes/test_vision_keyless_local.py` | 5 | reviewed, clean |
| `tests/hermes/test_vision_url_contract.py` | 10 | reviewed, clean |
| `tests/hermes/test_visual_print_watch.py` | 52 | reviewed, clean |
| `tests/perf/__init__.py` | 0 | reviewed, clean |
| `tests/perf/test_perf_gate.py` | 7 | reviewed, clean |
| `tests/pipeline/__init__.py` | 0 | reviewed, clean |
| `tests/pipeline/conftest.py` | 0 | reviewed; gates: L166:skip = pytest.mark.skip(reason=f"needs the real Orca slicer: set {REAL_SLICER}=1") |
| `tests/pipeline/helpers.py` | 0 | reviewed; gates: L12:pytest.skip(f"runtime module {name} is not installed", allow_module_level=True) |
| `tests/pipeline/native_bounds_corpus.py` | 0 | reviewed, clean |
| `tests/pipeline/test_952_followups.py` | 6 | reviewed, clean |
| `tests/pipeline/test_acceleration_profile.py` | 3 | reviewed, clean |
| `tests/pipeline/test_ai_fix_intake_978.py` | 14 | reviewed, clean |
| `tests/pipeline/test_artifact_binding.py` | 2 | reviewed, clean |
| `tests/pipeline/test_assembly_split_refusal.py` | 4 | reviewed, clean |
| `tests/pipeline/test_author_settings.py` | 16 | reviewed, clean |
| `tests/pipeline/test_background_slice.py` | 8 | reviewed, clean |
| `tests/pipeline/test_channel_routing.py` | 3 | reviewed, clean |
| `tests/pipeline/test_chat_text_cleanup.py` | 11 | reviewed, clean |
| `tests/pipeline/test_clear_job.py` | 8 | reviewed, clean |
| `tests/pipeline/test_color_geometry.py` | 31 | reviewed, clean |
| `tests/pipeline/test_color_intake.py` | 14 | reviewed, clean |
| `tests/pipeline/test_core_material_colors.py` | 5 | reviewed, clean |
| `tests/pipeline/test_crealitycloud_sites.py` | 29 | reviewed, clean |
| `tests/pipeline/test_cults3d_sites.py` | 18 | reviewed, clean |
| `tests/pipeline/test_data2_fixes.py` | 10 | reviewed, clean |
| `tests/pipeline/test_delivery.py` | 23 | reviewed, clean |
| `tests/pipeline/test_delivery_bambu_a1.py` | 2 | reviewed, clean |
| `tests/pipeline/test_derived_plate_filament_arrays.py` | 1 | reviewed, clean |
| `tests/pipeline/test_direct_link_pinning.py` | 3 | reviewed, clean |
| `tests/pipeline/test_direct_links.py` | 23 | reviewed, clean |
| `tests/pipeline/test_discord_color_hook.py` | 2 | reviewed, clean |
| `tests/pipeline/test_discord_color_intake.py` | 8 | reviewed, clean |
| `tests/pipeline/test_edit_pipeline.py` | 6 | reviewed, clean |
| `tests/pipeline/test_first_layer_workflow.py` | 6 | reviewed, clean |
| `tests/pipeline/test_fit_auto_rearrange.py` | 25 | reviewed, clean |
| `tests/pipeline/test_fit_options_buttons.py` | 13 | reviewed, clean |
| `tests/pipeline/test_flash_studio.py` | 9 | reviewed, clean |
| `tests/pipeline/test_foreign_audit_matrix.py` | 3 | reviewed, clean |
| `tests/pipeline/test_foreign_project_adoption.py` | 4 | reviewed, clean |
| `tests/pipeline/test_forgebridge_edge_cases.py` | 13 | reviewed, clean |
| `tests/pipeline/test_gcode_perf_925.py` | 27 | reviewed, clean |
| `tests/pipeline/test_generic_runtime.py` | 19 | reviewed, clean |
| `tests/pipeline/test_ifs_purge_mode.py` | 13 | reviewed, clean |
| `tests/pipeline/test_intake_590.py` | 5 | reviewed, clean |
| `tests/pipeline/test_intake_repair.py` | 6 | reviewed, clean |
| `tests/pipeline/test_intake_support_precheck.py` | 17 | reviewed, clean |
| `tests/pipeline/test_intake_support_unverified_968.py` | 8 | reviewed, clean |
| `tests/pipeline/test_launch_clock_reset.py` | 7 | reviewed, clean |
| `tests/pipeline/test_launch_state.py` | 51 | reviewed, clean |
| `tests/pipeline/test_layer_color_intake.py` | 8 | reviewed, clean |
| `tests/pipeline/test_login_token_sites.py` | 44 | reviewed, clean |
| `tests/pipeline/test_makerworld_profiles.py` | 5 | reviewed, clean |
| `tests/pipeline/test_mapped_preparing.py` | 1 | reviewed, clean |
| `tests/pipeline/test_model_intake_audit_placement.py` | 1 | reviewed, clean |
| `tests/pipeline/test_model_job.py` | 145 | reviewed, clean |
| `tests/pipeline/test_model_parts.py` | 10 | reviewed, clean |
| `tests/pipeline/test_multi_tool_intake.py` | 24 | reviewed, clean |
| `tests/pipeline/test_multicolor_contract.py` | 15 | reviewed, clean |
| `tests/pipeline/test_myminifactory_sites.py` | 20 | reviewed, clean |
| `tests/pipeline/test_native_bounds.py` | 22 | reviewed; gates: L73:pytest.skip("native bounds extension not built"); L75:pytest.skip("non-ASCII corpus cases are Python-path by design (ASCII gate)"); L82:pytest.skip("native bounds extension not built"); L126:pytest.skip("native bounds extension not built"); L236:pytest.skip("native bounds extension not built") |
| `tests/pipeline/test_native_contract.py` | 17 | reviewed, clean |
| `tests/pipeline/test_native_idle.py` | 18 | reviewed, clean |
| `tests/pipeline/test_native_startup.py` | 5 | reviewed, clean |
| `tests/pipeline/test_offset_authority.py` | 12 | reviewed, clean |
| `tests/pipeline/test_orca_adapter.py` | 22 | reviewed, clean |
| `tests/pipeline/test_orca_sanitize.py` | 8 | reviewed; gates: L151:@pytest.mark.skipif(REAL is None or not shutil.which('flatpak') |
| `tests/pipeline/test_part_families_split.py` | 9 | reviewed, clean |
| `tests/pipeline/test_paste_login_token.py` | 30 | reviewed; gates: L157:@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX permission bits'); L164:@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX permission bits') |
| `tests/pipeline/test_plate_merge.py` | 39 | reviewed, clean |
| `tests/pipeline/test_plate_thumbnail.py` | 5 | reviewed, clean |
| `tests/pipeline/test_port_contracts.py` | 3 | reviewed, clean |
| `tests/pipeline/test_prep_lifecycle.py` | 22 | reviewed, clean |
| `tests/pipeline/test_prepared_intake.py` | 128 | reviewed, clean |
| `tests/pipeline/test_presend_outcome.py` | 5 | reviewed, clean |
| `tests/pipeline/test_pressure_advance_record.py` | 4 | reviewed, clean |
| `tests/pipeline/test_print_now_intake_receipt.py` | 4 | reviewed, clean |
| `tests/pipeline/test_printer_state_offline_check.py` | 3 | reviewed, clean |
| `tests/pipeline/test_prusa_object_settings.py` | 10 | reviewed, clean |
| `tests/pipeline/test_purge_plan.py` | 33 | reviewed, clean |
| `tests/pipeline/test_recorded_temperatures.py` | 7 | reviewed, clean |
| `tests/pipeline/test_render_preview.py` | 2 | reviewed; gates: L12:pytestmark = pytest.mark.skipif(not (shutil.which('openscad') and shutil.which('xvfb-run')), |
| `tests/pipeline/test_safe4_printer_routing.py` | 5 | reviewed, clean |
| `tests/pipeline/test_screen_mode_migration.py` | 4 | reviewed, clean |
| `tests/pipeline/test_secondary_printer_recipe.py` | 5 | reviewed, clean |
| `tests/pipeline/test_shell_cli.py` | 3 | reviewed; gates: L9:needs_tools = pytest.mark.skipif(not (shutil.which("bash") and shutil.which("jq")), reason="bash and jq are re |
| `tests/pipeline/test_single_extruder_intake.py` | 9 | reviewed, clean |
| `tests/pipeline/test_slice_recipe.py` | 16 | reviewed, clean |
| `tests/pipeline/test_slice_warnings.py` | 20 | reviewed, clean |
| `tests/pipeline/test_slicer_choice.py` | 6 | reviewed, clean |
| `tests/pipeline/test_slot_remap.py` | 6 | reviewed, clean |
| `tests/pipeline/test_source_colors.py` | 10 | reviewed, clean |
| `tests/pipeline/test_source_colors_platenames.py` | 3 | reviewed, clean |
| `tests/pipeline/test_split_fit.py` | 83 | reviewed, clean |
| `tests/pipeline/test_spool_lifecycle.py` | 26 | reviewed, clean |
| `tests/pipeline/test_spool_swap_override.py` | 6 | reviewed, clean |
| `tests/pipeline/test_spoolman_best_effort.py` | 17 | reviewed, clean |
| `tests/pipeline/test_spoolman_warning.py` | 2 | reviewed, clean |
| `tests/pipeline/test_spoolman_warning_only.py` | 14 | reviewed, clean |
| `tests/pipeline/test_spoolman_wrapper_contract.py` | 6 | reviewed, clean |
| `tests/pipeline/test_start_path.py` | 44 | reviewed, clean |
| `tests/pipeline/test_start_progress.py` | 7 | reviewed, clean |
| `tests/pipeline/test_start_readback.py` | 15 | reviewed, clean |
| `tests/pipeline/test_stock_slot_slice.py` | 3 | reviewed, clean |
| `tests/pipeline/test_support_clearance.py` | 5 | reviewed, clean |
| `tests/pipeline/test_support_clearance_phase2.py` | 27 | reviewed, clean |
| `tests/pipeline/test_support_coverage_963.py` | 9 | reviewed, clean |
| `tests/pipeline/test_support_coverage_969.py` | 10 | reviewed, clean |
| `tests/pipeline/test_support_coverage_golden_pack.py` | 11 | reviewed, clean |
| `tests/pipeline/test_support_filament.py` | 9 | reviewed, clean |
| `tests/pipeline/test_thingiverse_sites.py` | 13 | reviewed, clean |
| `tests/pipeline/test_threemf_canonical.py` | 12 | reviewed, clean |
| `tests/pipeline/test_tower_conflicts.py` | 8 | reviewed, clean |
| `tests/pipeline/test_worker_lifecycle.py` | 12 | reviewed, clean |
| `tests/pipeline/test_yolo.py` | 18 | reviewed, clean |
| `tests/printers/__init__.py` | 0 | reviewed, clean |
| `tests/printers/a1_fixture.py` | 0 | reviewed, clean |
| `tests/printers/a1_flow.py` | 0 | reviewed, clean |
| `tests/printers/anycubic_fixture.py` | 0 | reviewed, clean |
| `tests/printers/bambu_family_fixture.py` | 0 | reviewed, clean |
| `tests/printers/creality_fixture.py` | 0 | reviewed, clean |
| `tests/printers/elegoo_fixture.py` | 0 | reviewed, clean |
| `tests/printers/p1s_fixture.py` | 0 | reviewed, clean |
| `tests/printers/prusa_fixture.py` | 0 | reviewed, clean |
| `tests/printers/qidi_fixture.py` | 0 | reviewed, clean |
| `tests/printers/test_adapters.py` | 40 | reviewed, clean |
| `tests/printers/test_anycubic.py` | 5 | reviewed, clean |
| `tests/printers/test_bambu_ams.py` | 18 | reviewed, clean |
| `tests/printers/test_bambu_ams_mapping.py` | 27 | reviewed, clean |
| `tests/printers/test_bambu_camera.py` | 26 | reviewed, clean |
| `tests/printers/test_bambu_hms.py` | 21 | reviewed, clean |
| `tests/printers/test_bambu_hms_generator.py` | 3 | reviewed, clean |
| `tests/printers/test_bambu_lan.py` | 20 | reviewed, clean |
| `tests/printers/test_bambu_listener.py` | 22 | reviewed, clean |
| `tests/printers/test_bambu_material_vocabulary.py` | 8 | reviewed, clean |
| `tests/printers/test_bambu_print.py` | 17 | reviewed, clean |
| `tests/printers/test_bambu_upload.py` | 12 | reviewed, clean |
| `tests/printers/test_bambulab_a1.py` | 60 | reviewed, clean |
| `tests/printers/test_bambulab_family.py` | 11 | reviewed, clean |
| `tests/printers/test_bambulab_h3c.py` | 6 | reviewed, clean |
| `tests/printers/test_bambulab_operator_presets.py` | 22 | reviewed, clean |
| `tests/printers/test_bambulab_p1s.py` | 18 | reviewed, clean |
| `tests/printers/test_color_system.py` | 7 | reviewed, clean |
| `tests/printers/test_creality.py` | 7 | reviewed, clean |
| `tests/printers/test_elegoo.py` | 21 | reviewed, clean |
| `tests/printers/test_elegoo_sdcp_camera.py` | 2 | reviewed, clean |
| `tests/printers/test_flashforge_stock.py` | 24 | reviewed, clean |
| `tests/printers/test_flashforge_stock_camera.py` | 9 | reviewed, clean |
| `tests/printers/test_golden_inputs.py` | 1 | reviewed, clean |
| `tests/printers/test_machine_gcode_templates.py` | 6 | reviewed, clean |
| `tests/printers/test_material_profiles.py` | 38 | reviewed, clean |
| `tests/printers/test_model_baselines.py` | 8 | reviewed, clean |
| `tests/printers/test_octoprint.py` | 7 | reviewed, clean |
| `tests/printers/test_octoprint_anycubic_presets.py` | 4 | reviewed, clean |
| `tests/printers/test_octoprint_client.py` | 13 | reviewed, clean |
| `tests/printers/test_octoprint_ender_presets.py` | 4 | reviewed, clean |
| `tests/printers/test_preset_cache.py` | 5 | reviewed, clean |
| `tests/printers/test_prusa.py` | 17 | reviewed, clean |
| `tests/printers/test_prusa_camera.py` | 14 | reviewed, clean |
| `tests/printers/test_qidi.py` | 5 | reviewed, clean |
| `tests/printers/test_slice_temperatures.py` | 17 | reviewed; gates: L111:pytest.skip(f"{model} has no checked profile for {material}"); L122:pytest.skip(f"{model} has no checked profile for {material}") |
| `tests/printers/test_snapmaker.py` | 9 | reviewed, clean |
| `tests/printers/test_stock_detail_auth.py` | 5 | reviewed, clean |
| `tests/printers/test_vendor_profiles.py` | 18 | reviewed, clean |
| `tests/replay/__init__.py` | 0 | reviewed, clean |
| `tests/replay/conftest.py` | 0 | reviewed, clean |
| `tests/replay/helpers.py` | 0 | reviewed, clean |
| `tests/replay/test_fixtures_scrubbed.py` | 2 | reviewed, clean |
| `tests/replay/test_start_recorder.py` | 11 | reviewed, clean |
| `tests/replay/test_start_replay.py` | 16 | reviewed, clean |
| `tests/replay/test_verify_replay.py` | 4 | reviewed, clean |
| `tests/services/__init__.py` | 0 | reviewed, clean |
| `tests/services/test_spoolman_real.py` | 5 | reviewed; gates: L13:pytestmark = pytest.mark.skipif(not URL, reason="set PRINT_HANDY_SPOOLMAN_TEST_URL to a throwaway Spoolman") |
| `tests/slicer/__init__.py` | 0 | reviewed, clean |
| `tests/slicer/per_z_analysis.py` | 0 | reviewed, clean |
| `tests/slicer/test_bambu_real_a1.py` | 4 | reviewed; gates: L37:pytestmark = pytest.mark.skipif(not (REAL and REAL_BAMBU), |
| `tests/slicer/test_creator_heads_real.py` | 2 | reviewed; gates: L25:pytestmark = pytest.mark.skipif(not REAL, reason="set PRINT_HANDY_REAL_SLICER=1 to slice with the real Orca CL |
| `tests/slicer/test_derived_plate_real.py` | 2 | reviewed; gates: L26:pytestmark = pytest.mark.skipif(not REAL, reason="set PRINT_HANDY_REAL_SLICER=1 to slice with the real Orca CL |
| `tests/slicer/test_flashforge_baselines_real.py` | 1 | reviewed; gates: L24:pytestmark = pytest.mark.skipif(not REAL, reason="set PRINT_HANDY_REAL_SLICER=1 to slice with the real Orca CL |
| `tests/slicer/test_layer_color_real.py` | 3 | reviewed; gates: L29:pytestmark = pytest.mark.skipif(not REAL, reason="set PRINT_HANDY_REAL_SLICER=1 to slice with the real Orca CL |
| `tests/slicer/test_orca_edits_real.py` | 3 | reviewed; gates: L25:pytestmark = pytest.mark.skipif(not REAL, reason="set PRINT_HANDY_REAL_SLICER=1 to slice with the real Orca CL |
| `tests/slicer/test_orca_real.py` | 7 | reviewed; gates: L138:@pytest.mark.skipif(not REAL, reason="set PRINT_HANDY_REAL_SLICER=1 to slice with the real Orca CLI") |
| `tests/slicer/test_orca_real_bambu_a1.py` | 6 | reviewed; gates: L32:pytestmark = pytest.mark.skipif(not REAL, reason="set PRINT_HANDY_REAL_SLICER=1 to slice with the real Orca CL |
| `tests/slicer/test_per_z_analysis.py` | 4 | reviewed, clean |
| `tests/slicer/test_per_z_real.py` | 3 | reviewed; gates: L54:@pytest.mark.skipif(not REAL, reason="set PRINT_HANDY_REAL_SLICER=1 to slice with the real Orca CLI"); L76:@pytest.mark.skipif(not REAL, reason="set PRINT_HANDY_REAL_SLICER=1 to slice with the real Orca CLI"); L94:@pytest.mark.skipif(not REAL, reason="set PRINT_HANDY_REAL_SLICER=1 to slice with the real Orca CLI") |
| `tests/slicer/test_prusa_foreign_real.py` | 2 | reviewed; gates: L26:pytestmark = pytest.mark.skipif(not REAL, reason="set PRINT_HANDY_REAL_SLICER=1 to slice with the real Orca CL |
| `tests/slicer/test_spacing_minimums_real.py` | 2 | reviewed; gates: L22:pytestmark = pytest.mark.skipif(not REAL, reason="set PRINT_HANDY_REAL_SLICER=1 to slice with the real Orca CL |
| `tests/test_ai_fix_978.py` | 13 | reviewed, clean |
| `tests/test_ai_fix_985.py` | 11 | reviewed, clean |
| `tests/test_ai_fix_chat_978.py` | 6 | reviewed, clean |
| `tests/test_ai_fix_followups_978.py` | 24 | reviewed; gates: L28:trimesh = pytest.importorskip("trimesh") |
| `tests/test_ai_fix_review_978.py` | 15 | reviewed; gates: L196:trimesh = pytest.importorskip("trimesh") |
| `tests/test_ai_routes.py` | 24 | reviewed, clean |
| `tests/test_bot_gate.py` | 4 | reviewed, clean |
| `tests/test_bump_upstream.py` | 107 | reviewed, clean |
| `tests/test_calibration.py` | 65 | reviewed, clean |
| `tests/test_chat_remedies.py` | 2 | reviewed, clean |
| `tests/test_ci_bambu_workflow.py` | 8 | reviewed, clean |
| `tests/test_ci_standalone_workflow.py` | 12 | reviewed, clean |
| `tests/test_cloudflare_examples.py` | 3 | reviewed; gates: L44:@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed") |
| `tests/test_color_plate_failures_716.py` | 1 | reviewed, clean |
| `tests/test_combined_supervisor.py` | 16 | reviewed, clean |
| `tests/test_connection_probe.py` | 19 | reviewed, clean |
| `tests/test_container_marker.py` | 4 | reviewed, clean |
| `tests/test_contribution_policy.py` | 26 | reviewed, clean |
| `tests/test_debug_mode.py` | 14 | reviewed, clean |
| `tests/test_debug_webui.py` | 4 | reviewed, clean |
| `tests/test_design_dispatch.py` | 46 | reviewed; gates: L33:needs_scad = pytest.mark.skipif(not (shutil.which("openscad") and shutil.which("xvfb-run")), |
| `tests/test_develop_workflow.py` | 11 | reviewed, clean |
| `tests/test_diagnostics.py` | 9 | reviewed, clean |
| `tests/test_diagnostics_590.py` | 21 | reviewed, clean |
| `tests/test_diagnostics_bundle.py` | 10 | reviewed, clean |
| `tests/test_diagnostics_stage2.py` | 1 | reviewed, clean |
| `tests/test_diagnostics_stage4.py` | 4 | reviewed, clean |
| `tests/test_diagnostics_stage5.py` | 6 | reviewed, clean |
| `tests/test_diagnostics_stage6.py` | 4 | reviewed, clean |
| `tests/test_dispatch_precedence.py` | 5 | reviewed; gates: L30:an archived job, the reprint route wins (was a strict xfail until #988; L76:# today the link route decides wrong — see the strict xfail below. |
| `tests/test_docker_release.py` | 57 | reviewed, clean |
| `tests/test_docker_release_contract.py` | 28 | reviewed, clean |
| `tests/test_docs_index_contract.py` | 1 | reviewed, clean |
| `tests/test_doctor.py` | 33 | reviewed, clean |
| `tests/test_edit_dispatch.py` | 36 | reviewed, clean |
| `tests/test_edit_phrasing.py` | 11 | reviewed, clean |
| `tests/test_entrypoint_layout_matrix.py` | 7 | reviewed; gates: L102:@pytest.mark.skipif(os.environ.get("PRINT_HANDY_LAYOUT_TEST_MOUNT") != "1", |
| `tests/test_error_code_docs.py` | 19 | reviewed, clean |
| `tests/test_event_starvation.py` | 6 | reviewed, clean |
| `tests/test_firmware_update.py` | 74 | reviewed, clean |
| `tests/test_friendly_errors.py` | 12 | reviewed, clean |
| `tests/test_fuzzy_semantic.py` | 21 | reviewed, clean |
| `tests/test_gcode_factory.py` | 5 | reviewed, clean |
| `tests/test_go2rtc_container.py` | 51 | reviewed, clean |
| `tests/test_home_update.py` | 8 | reviewed, clean |
| `tests/test_image_inventory_workflow.py` | 4 | reviewed, clean |
| `tests/test_install_hermes.py` | 19 | reviewed, clean |
| `tests/test_intake_resources.py` | 10 | reviewed, clean |
| `tests/test_issue_policy.py` | 5 | reviewed, clean |
| `tests/test_knowledge.py` | 46 | reviewed, clean |
| `tests/test_light_settings.py` | 6 | reviewed, clean |
| `tests/test_listener_gate.py` | 20 | reviewed, clean |
| `tests/test_listener_health.py` | 9 | reviewed, clean |
| `tests/test_listener_identity.py` | 1 | reviewed, clean |
| `tests/test_listener_registry.py` | 25 | reviewed, clean |
| `tests/test_live_activity.py` | 26 | reviewed, clean |
| `tests/test_live_dispatch.py` | 7 | reviewed, clean |
| `tests/test_live_gate.py` | 20 | reviewed, clean |
| `tests/test_live_stream.py` | 57 | reviewed, clean |
| `tests/test_logs.py` | 29 | reviewed, clean |
| `tests/test_macros.py` | 60 | reviewed, clean |
| `tests/test_material_hardening.py` | 21 | reviewed, clean |
| `tests/test_matrix_confirm_live_key.py` | 5 | reviewed, clean |
| `tests/test_migrate_names.py` | 9 | reviewed, clean |
| `tests/test_modeled_supports.py` | 4 | reviewed, clean |
| `tests/test_multi_printer_walkthrough.py` | 3 | reviewed, clean |
| `tests/test_native_release_acceptance.py` | 6 | reviewed, clean |
| `tests/test_nightly_workflow.py` | 9 | reviewed, clean |
| `tests/test_notify_mqtt.py` | 13 | reviewed, clean |
| `tests/test_nozzle.py` | 24 | reviewed, clean |
| `tests/test_nozzle_change.py` | 26 | reviewed, clean |
| `tests/test_orca_pins.py` | 11 | reviewed, clean |
| `tests/test_outage_alert.py` | 5 | reviewed, clean |
| `tests/test_per_printer_state.py` | 31 | reviewed, clean |
| `tests/test_post_release_surfaces_workflow.py` | 11 | reviewed, clean |
| `tests/test_pre_release_checklist.py` | 30 | reviewed, clean |
| `tests/test_print_feedback_guard.py` | 6 | reviewed, clean |
| `tests/test_print_handy_cfg.py` | 9 | reviewed, clean |
| `tests/test_print_stats_wrapped.py` | 40 | reviewed, clean |
| `tests/test_printer_clock_and_timelapse_fallback.py` | 21 | reviewed, clean |
| `tests/test_printer_identity.py` | 12 | reviewed, clean |
| `tests/test_printer_state_provision.py` | 6 | reviewed, clean |
| `tests/test_printers_roster.py` | 35 | reviewed, clean |
| `tests/test_profile_platform.py` | 18 | reviewed, clean |
| `tests/test_projects_issue1008.py` | 18 | reviewed, clean |
| `tests/test_public_text_policy.py` | 5 | reviewed, clean |
| `tests/test_recent_model_link.py` | 5 | reviewed, clean |
| `tests/test_release_check.py` | 8 | reviewed, clean |
| `tests/test_release_surfaces_gate.py` | 10 | reviewed, clean |
| `tests/test_renamed_key_readers.py` | 4 | reviewed, clean |
| `tests/test_repo_attributes.py` | 1 | reviewed, clean |
| `tests/test_reprint_phrase_precedence.py` | 20 | reviewed, clean |
| `tests/test_restart_gateway_script.py` | 1 | reviewed, clean |
| `tests/test_retired_memory_config.py` | 1 | reviewed, clean |
| `tests/test_roast_corpus.py` | 10 | reviewed, clean |
| `tests/test_roster_write.py` | 18 | reviewed, clean |
| `tests/test_sanitize_logs.py` | 10 | reviewed, clean |
| `tests/test_settings.py` | 97 | reviewed, clean |
| `tests/test_setup_wizard.py` | 13 | reviewed, clean |
| `tests/test_state_file_failure_paths.py` | 13 | reviewed; gates: L13:pinned as strict xfails when the bug was filed from this pass per #391's |
| `tests/test_state_files.py` | 15 | reviewed, clean |
| `tests/test_state_volume.py` | 24 | reviewed; gates: L246:pytest.skip("unshare(1) is not available"); L248:pytest.skip("user and mount namespaces are not available") |
| `tests/test_strict_xfail_register.py` | 8 | reviewed; gates: L1:"""The strict xfail register matches the markers, and rejects a stale one."""; L12:"check_strict_xfails", ROOT / "scripts" / "check_strict_xfails.py"); L30:(root / "tests" / "strict_xfail_register.json").write_text(; L38:@pytest.mark.xfail(strict=True, reason="open defect"); L43:self.assertIn("strict xfail has no register entry: tests/test_sample.py::test_open", errors); L44:self.assertIn("strict xfail has no register entry: corpus:sample-case", errors); L51:@pytest.mark.xfail(strict=True, reason="open defect"); L66:self.assertIn("register entry has no strict xfail: tests/test_sample.py::test_gone", errors); L68:def test_a_non_strict_xfail_needs_no_entry(self):; L71:@pytest.mark.xfail(reason="flaky, not strict"); L74:@pytest.mark.xfail; L82:def test_a_param_mark_is_a_strict_xfail(self):; L86:pytest.param(1, marks=pytest.mark.xfail(strict=True, reason="open")),; L87:pytest.param(2, marks=pytest.mark.xfail(reason="not strict")),; L95:self.assertEqual(["strict xfail has no register entry: tests/test_sample.py::test_cases"], errors); L97:def test_a_class_mark_is_a_strict_xfail(self):; L100:@pytest.mark.xfail(strict=True, reason="entire class fails"); L108:self.assertEqual(["strict xfail has no register entry: tests/test_sample.py::TestSample"], errors); L115:@pytest.mark.xfail(strict=True, reason="entire class fails") |
| `tests/test_thermal_capabilities.py` | 13 | reviewed, clean |
| `tests/test_thermal_probe.py` | 22 | reviewed, clean |
| `tests/test_timeout_policy.py` | 2 | reviewed, clean |
| `tests/test_upstream_version_parity.py` | 1 | reviewed, clean |
| `tests/test_upstream_watch.py` | 26 | reviewed, clean |
| `tests/test_us_spelling.py` | 14 | reviewed, clean |
| `tests/test_variables_contract.py` | 4 | reviewed, clean |
| `tests/test_webui_camera.py` | 8 | reviewed, clean |
| `tests/test_webui_config.py` | 29 | reviewed, clean |
| `tests/test_webui_config_page.py` | 16 | reviewed, clean |
| `tests/test_webui_controls.py` | 55 | reviewed, clean |
| `tests/test_webui_designs.py` | 16 | reviewed, clean |
| `tests/test_webui_gate.py` | 16 | reviewed, clean |
| `tests/test_webui_header_throttle.py` | 7 | reviewed, clean |
| `tests/test_webui_layers.py` | 7 | reviewed, clean |
| `tests/test_webui_login.py` | 21 | reviewed, clean |
| `tests/test_webui_redesign.py` | 8 | reviewed, clean |
| `tests/test_webui_roster.py` | 2 | reviewed, clean |
| `tests/test_webui_setup.py` | 27 | reviewed, clean |
| `tests/test_webui_spools.py` | 52 | reviewed, clean |
| `tests/test_webui_status.py` | 46 | reviewed, clean |
| `tests/test_webui_walkthrough.py` | 51 | reviewed, clean |
| `tests/test_wrapped_records_from_writers.py` | 4 | reviewed; gates: L13:phase 2 (PR #986), and were strict xfails until it landed:; L117:@pytest.mark.skipif(not (ffmpeg and ffprobe), |
| `tests/test_wrapped_sharing.py` | 16 | reviewed, clean |
| `tests/test_wrapped_v2.py` | 35 | reviewed, clean |
| `tests/test_wrapped_video_real_ffmpeg.py` | 2 | reviewed, clean |
| `tests/test_wrapped_video_tts.py` | 21 | reviewed, clean |
