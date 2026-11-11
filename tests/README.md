# Test coverage map

Which suites pin which subsystem, so feature work can see the gaps it is
adding to (#391 scope 5). Maintained by the test-expansion pass; update
this file when a suite is added, moved, or starts/stops pinning an area.

## Subsystem → suites

| Subsystem | Pinned by |
|---|---|
| Discord bot core, commands, runtime, typed replies | `tests/bot/test_runtime.py`, `test_prompts.py`, `test_typed_reply_scope.py`, `tests/corpus/` |
| Discord no-LLM modes (#466): no `[llm]`, and `[llm]` configured with `discord.chat_llm_enabled=false`; bot-check/status pins, touchpoint inventory, full intake | `tests/bot/test_discord_no_llm.py` |
| Bot startup retry / transient classification (#364) | `tests/bot/test_startup_retry.py` |
| Platform adapters (Discord / Matrix / Telegram), access control | `tests/bot/test_platform.py`, `test_matrix.py`, `test_telegram.py`, `test_access.py`, `test_routing.py` |
| Guided setup | `tests/bot/test_guided.py`, `test_guided_multi.py`, `tests/test_setup_wizard.py` |
| Bot health / heartbeat / listener transport | `tests/bot/test_health_and_transport.py`, `tests/test_listener_health.py`, `tests/test_listener_gate.py` |
| (removed) Hermes profile platform switch (#384) | the reader, its doctor/bot-check rows and its tests were deleted with the Hermes install and runtime mode |
| Moonraker listener, announcements, completion delivery | `tests/hermes/test_lane_b.py`, `test_print_messages.py`, `test_completion_delivery.py`, `test_paused_job_gone.py`, `test_print_alert_buttons.py` |
| Timelapse: Moonraker delivery contract (#373) | `tests/hermes/test_timelapse_delivery_contract.py` |
| Timelapse: launched-print monitors (OctoPrint / stock / Bambu / Prusa / Elegoo SDCP) | `tests/hermes/test_octo_watch.py`, `test_stock_watch.py`, `test_bambu_watch.py`, `test_prusa_watch.py`, `tests/printers/test_prusa_camera.py`, `test_elegoo_watch.py`, `tests/printers/test_elegoo_sdcp_camera.py` |
| Timelapse: capture, encode, sizing, plan clips, vertical short | `tests/hermes/test_timelapse_capture.py`, `test_timelapse_encode.py`, `test_platform_media_sizing.py`, `test_plan_timelapse.py`, `test_vertical_short.py`, `test_timelapse_cli_transport.py` |
| Roast (one per print, #374) + roast generator fail-soft | `tests/hermes/test_print_messages.py`, `tests/pipeline/test_prepared_intake.py` |
| Intake / slicing pipeline / model job | `tests/pipeline/`, `tests/core/`, `tests/slicer/`, `tests/corpus/` |
| Slice failure explanations and worker transport | `tests/core/test_slice_failure.py`; `tests/pipeline/test_prepared_intake.py` exercises one/two copies, direct/serialized errors, safe cleanup, no upload/start, and a failed retry; `tests/pipeline/test_orca_adapter.py` exercises both engine failures and retained logs. These offline tests inject failures; they do not prove that a private model slices successfully. |
| Model sites via API (#462): Thingiverse inventory/download; MyMiniFactory inventory plus the OAuth sign-in download (#464), the Cloudflare-challenge stop and the token never following a redirect to another host | `tests/pipeline/test_thingiverse_sites.py`, `tests/pipeline/test_myminifactory_sites.py` |
| Creality Cloud intake (#604): inventory plus token-gated download, with the named token gap at the point of use | `tests/pipeline/test_crealitycloud_sites.py` |
| Thangs and Nexprint intake (#464): inventory plus token-gated download, MakerOnline inventory-only, and the chat connect-account flow that retries the download once the token file is saved | `tests/pipeline/test_login_token_sites.py` |
| Real Bambu Studio slicing (CI `Slicer test (bambu)` leg) | `tests/slicer/test_bambu_real_a1.py`; job contract in `tests/test_ci_bambu_workflow.py` |
| Spoolman best-effort contract (#387, #235) | `tests/core/test_spoolman_client.py`, `tests/pipeline/test_spoolman_best_effort.py`, `test_spoolman_wrapper_contract.py`, `test_spoolman_warning*.py` |
| Printers: roster, per-printer state, leases (#333–#337) | `tests/test_printers_roster.py`, `tests/test_per_printer_state.py`, `tests/test_multi_printer_walkthrough.py`, `tests/printers/` |
| Printer replay (#616, #689-#693): the real print-now, verify and launch-lease code over HTTP against a fake Moonraker that serves responses recorded from an AD5X on ZMod, including a POST held past a real socket timeout | `tests/replay/`, `tests/fixtures/printers/ad5x_zmod/README.md` |
| Fail-soft state files (corrupt / partial) | `tests/test_state_file_failure_paths.py`, `tests/test_state_files.py`, `tests/test_live_gate.py` |
| Upstream bump automation: step-size policy, harness sweep, body shape | `tests/test_bump_upstream.py` |
| Upstream version parity (test harness pins vs shipped pins) | `tests/test_upstream_version_parity.py`, `tests/test_ci_bambu_workflow.py` |
| Approval / yolo invariants across answer paths; dispatcher single-claim precedence | `tests/bot/test_approval_invariants.py`, `tests/test_dispatch_precedence.py` |
| Fake-G-code factory + foreign-G-code contract | `tests/gcode_factory.py`, `tests/test_gcode_factory.py` |
| Support coverage golden pack (truth-labeled adversarial geometry) | `tests/pipeline/test_support_coverage_golden_pack.py` (extends the per-fix files `test_support_coverage_963.py`, `test_support_coverage_969.py`) |
| Event starvation (fake clock, zero status events) | `tests/test_event_starvation.py` |
| Renamed-key reader completeness (#672 renames) | `tests/test_renamed_key_readers.py` |
| Entrypoint × state-volume layout matrix (#914) | `tests/test_entrypoint_layout_matrix.py` |
| Wrapped aggregates from real record writers; ffmpeg pacing | `tests/test_wrapped_records_from_writers.py` |
| Bambu HMS table content integrity across regenerations | `tests/printers/test_bambu_hms.py`, `tests/printers/test_bambu_hms_generator.py` |
| Docker image, release contracts, supervisor, healthcheck | `tests/test_docker_release*.py`, `tests/docker/`, `tests/test_combined_supervisor.py`, `tests/test_go2rtc_container.py`, `tests/test_native_release_acceptance.py` |
| Web UI (status, config, login, roster) | `tests/test_webui_*.py` |
| Web UI webserver (real HTTP: every-route auth sweep, page/API shapes, camera proxy route, Data/Wrapped route gates, control/spools body plumbing, malformed-input robustness, concurrency, startup/bind rules incl. #1042) | `tests/test_webui_webserver.py` |
| Wrapped / Data tab (#956, #981 v1.1 + v2 + sharing + video/TTS: watchdog events, receipt/JOB joins, per-color grams, timelapse name join, slide presentation, music bed, narration) | `tests/test_print_stats_wrapped.py`, `tests/test_wrapped_v2.py`, `tests/test_wrapped_sharing.py`, `tests/test_wrapped_video_real_ffmpeg.py`, `tests/test_wrapped_video_tts.py` |
| Vision verdict evaluation harness (#455 item 4, Stage 0): session-fixture manifest contract, watchdog-cadence replay, per-print outcome accounting, box-geometry detached experiment | `tests/hermes/test_vision_eval_harness.py`, fixtures in `tests/hermes/fixtures/vision_eval/`, harness in `tests/hermes/vision_eval/` |
| Doctor / bot check surfaces | `tests/test_doctor.py`, `tests/bot/test_health_and_transport.py` |
| Settings / config loading | `tests/test_settings.py`, `tests/test_print_handy_cfg.py`, `tests/test_webui_config*.py` |
| Docs / contribution policy / post-release surfaces / pre-release checklist | `tests/test_docs_index_contract.py`, `tests/test_contribution_policy.py`, `tests/test_post_release_surfaces_workflow.py`, `tests/test_pre_release_checklist.py` |
| CI workflow structure (action pins, per-PR concurrency, required-check names) | `tests/test_action_pins.py`, `tests/test_ci_concurrency.py`, `tests/test_contributing_required_checks.py` |
| Knowledge, macros, live stream | `tests/test_knowledge.py`, `tests/test_macros.py`, `tests/test_live_*.py` |

## #391 regression inventory (stage 1, 2026-10-26)

Fix PRs checked for a test that fails without the fix; gaps filled in
the stage-1 PR unless noted:

| Fix | Verdict |
|---|---|
| #373 Moonraker timelapse silent drop (#383) | Covered by its contract tests (working path, restart, no-render, drifted name, mismatched marker, archive-off). |
| #373 OctoPrint monitor (#385) / stock monitor (#377) | Covered except the MEDIA-post-refusal fallback in both monitors — added in stage 1; mutation-verified by removing the fallback in a scratch copy (both tests fail). |
| #295 Prusa monitor (stage 1: PrusaLink) | Covered: lifecycle, standby/error terminals, dedupe, supersede, the no-camera / camera-read-failure / capture-unavailable / encode-failure / zero-frame / oversize fail-soft paths, the MEDIA-post-refusal fallback, and a full real-ffmpeg lifecycle fed by real PNG stills transcoded through the camera seam; the PrusaLink camera endpoints and seam are covered in `tests/printers/test_prusa_camera.py`. Not live-hardware verified (no Prusa printer available). |
| #295 Elegoo monitor (stage 2: SDCP) | Covered: lifecycle, standby/error terminals, dedupe, supersede, the no-camera / camera-read-failure / capture-unavailable / encode-failure / zero-frame / oversize fail-soft paths, the MEDIA-post-refusal fallback, the production no-source posture driven through the real adapter normalisation from raw SDCP status answers, and a full real-ffmpeg lifecycle through an injected camera source; the camera seam's unverified-source contract is pinned in `tests/printers/test_elegoo_sdcp_camera.py`. No SDCP camera endpoint is verified, so production capture stays off until one is; not live-hardware verified (no Centauri printer available). |
| #295 Bambu camera + timelapse | Covered: X1 stills through the go2rtc fan-out with fake seams (fan-out down and wrong-source name the limitation, no access code in any error), the adapter frame source per family, the monitor capture lifecycle (open once, cadence ticking between status polls, pause holds without frames, complete/cancelled/error stops, supersede, status-failure resilience), the fail-soft paths (source failure, capture refused, zero frames, encode failure, oversize line, MEDIA-post-refusal fallback), and a full real-ffmpeg lifecycle fed by real JPEG stills. Not live-hardware verified (no Bambu printer available). |
| #364 Discord startup retry (#369) | Covered for DNS/refused/timeout/429/503/403/login/intents; classifier branches for certificate errors, gateway discovery, WS handshake statuses, generic connection errors, the full transient-errno table, connector-cause inheritance and jitter bounds were untested — added in stage 1; the whole file fails against pre-#369 code (37 failed, finished log). |
| #384 platform-disable detection (#389) | Reader's main states, doctor and bot-check disabled/enabled rows covered; string variants (`"no"`/`"off"`/case/whitespace), non-mapping sections, empty/comment-only files, the other-platform isolation, and the doctor/bot-check unreadable + unset + missing rows were untested — added in stage 1; the reader file fails against pre-#389 code (15 failed, finished log). |
| #374 one roast per print (#380) | Covered (warming start, already-printing start, replay, completion has none); the roast generator's own fail-soft branches (empty title, no key, no URL, HTTP failure, malformed payload) were untested — added in stage 1. |
| #387 Spoolman best-effort (#396) | Covered by its client + wrapper-contract + best-effort suites; not duplicated here (per #391 coordination rule). |
| #386 channel-access default (#397), #399 docker unblock | Covered by `test_access.py` / `test_docker_release_contract.py` in their own PRs. |
| Fail-soft state readers | Unparseable JSON already degraded correctly everywhere. Parseable-but-malformed timestamps used to crash `recent_links`, `print_alert_buttons` and `print_next` (bug #404, closed); the readers treat an unreadable timestamp as expired, and `tests/test_state_file_failure_paths.py` pins that without xfail markers. |

Known remaining gaps for later #391 stages: the multi-printer
cross-product beyond the sampled Stage 2 matrix (scope 3), upload
timeout mid-flight and roster misconfiguration failure paths (scope 2
remainder), and the Matrix message-ordering flake root cause (scope 4).

## #391 stage 2 (failure paths: uploads)

- Bambu FTPS upload: a timeout mid-transfer and a timeout during the
  size verification both surface as the plain "may be incomplete"
  `BambuLanError` (failure type named, no access code, session closed);
  a failing `quit()` after a verified store does not fail the upload.
  Mutation-verified: removing the upload wrap in a scratch copy fails
  both new timeout tests (8 failed across the two mutated modules).
- OctoPrint upload: the client's `upload()` had no tests at all — now
  pinned for the one multipart POST shape, server-reported target
  verification (origin/path mismatch refuses, nothing started), a
  timeout as a plain not-retried error with no key leak and exactly one
  request, and bad filenames refused before any request.
- PrusaLink upload: a PUT timeout is not retried, leaks no key, and is
  followed by no verification read and no start (the storage lookup GET
  before it fails soft to the default storage, as designed).
- Roster misconfigurations were re-audited for this stage: duplicate
  ids, duplicate chat claims (including the global-channel fallback),
  cross-vendor keys, bad/missing ids, unknown keys and empty rosters
  are all already refused with named errors in
  `tests/test_printers_roster.py` — no gap found, nothing added.

## #391 stage 3 (multi-printer cross-product + flake investigation)

- Cross-family walkthrough added in `tests/test_multi_printer_walkthrough.py`:
  three listener families in one instance (Bambu LAN + Moonraker +
  OctoPrint) route their claimed channels plus the shared fallback,
  each printer's settings view carries only its own family's connection
  (no moonraker URL in the Bambu view, no LAN serial/host in the
  OctoPrint view, no OctoPrint URL in either), Spoolman stays shared,
  state folders stay distinct; and a printer with no channel claim and
  no platform home is unreachable by chat without breaking the others.
  The rest of the Stage 2 matrix was re-audited as already pinned:
  leases/approvals/hardware/maintenance/spool exclusivity per printer
  (`tests/test_per_printer_state.py`), channel routing and cross-channel
  prompt/plugin safety (`tests/bot/test_routing.py`), per-printer
  listener supervision and heartbeats (`tests/test_combined_supervisor.py`,
  `tests/test_listener_health.py`).
- Scope 4 (flake elimination) investigation, 2026-10-26: the Matrix
  message-ordering flake did NOT reproduce. Evidence: six consecutive
  local runs of `tests/bot/test_matrix.py` + `tests/bot/test_routing.py`
  were all green (43 passed each, finished logs), and the failed CI runs
  sampled from the last 60 (40430763841, 40419563602, 40415662779,
  40403501206) failed on other tracks' in-flight branches (design,
  state-files, supervisor timing) — none on a Matrix test. No root cause
  is claimed and no speculative fix is made; scope 4 stays open on #391
  until a failing run can be captured with its log.

## #416 (Matrix message-ordering flake: captured, root-caused, fixed)

- Capture: two CI runs on main failed the same test with the same
  assertion — 40339371151 (main @ 3d37f51, 2026-10-26) and 40442136404
  (main @ 8899a40, 2026-10-27), both `offline-tests (3.14)` only (3.12
  green on the same heads):
  `test_matrix_adapter_drives_a_full_intake_through_the_real_handlers`
  expected the last room message to be `Started.` and got
  `> Answered by message: Print it`. Linked on #416 before any re-run.
- Root cause (code, not the test): `BasePlatformAdapter.answer_typed`
  resolves the prompt registry before posting the close-out. Resolving
  wakes the waiting intake flow, which sends its next message at once.
  Discord/Telegram close-outs are edits, but the Matrix close-out is a
  new room message sent via `asyncio.to_thread`, so the answer note and
  the flow's next message raced, and either could land first.
- Fix: the Matrix adapter posts its answer note before resolving the
  prompt (`StandaloneAdapter.answer_typed` in `print_handy/bot/matrix.py`);
  other platforms are unchanged. Deterministic local reproduction with
  the note's send artificially delayed inverted the order on the old
  code every run and never on the fixed code.
- Pin: `test_a_typed_answer_posts_its_note_before_it_unblocks_the_flow`
  in `tests/bot/test_matrix.py` gates the note's send and asserts the
  waiting flow stays blocked until the note has posted; it fails
  deterministically against the pre-fix adapter.

## Real Bambu Studio leg (`Slicer test (bambu)`, 2026-10-27)

- New CI job runs `tests/slicer/test_bambu_real_a1.py` against the real
  Flathub Bambu Studio (2.8.2.61, ostree commit pinned in ci.yml and
  verified after install), the same invocation the home `[bambu]`
  command uses. A1 templates still come from the pinned OrcaSlicer
  bundle — the production arrangement. The job contract is pinned by
  `tests/test_ci_bambu_workflow.py`.
- Passing today: the configured command answers its CLI, and the
  binary's CLI slicing path slices a cube with the A1 templates
  (header-block identity asserted on the real output).
- Found by the leg's first run and filed as #434, which is closed: real
  Bambu Studio output has no `; generated by BambuStudio` line (the mocks
  invented it). Identity is now read from the header block, and the two
  production-flow tests (A1 primary; fallback with a dead Orca command)
  pass without xfail markers.

## What runs when, and what is not covered (#594)

| Tier | Where | What it pins |
| --- | --- | --- |
| Every PR | `ci.yml` | Everything under `tests/` with the fake slicer, including `tests/corpus/test_journey_matrix.py` (pinned fit option x color mode inventory), `test_spool_fixture.py` (state built by the real `create_spool` and `bind_slot`), and `test_timing_gate.py`. |
| Every PR | `ci.yml` real Orca job | The generated CC0 corpus under `tests/corpus/cases` sliced by real Orca. |
| Every PR | `ci.yml` timing job | Median of 3 repeats against `timing_baseline.json`: `wall_s`, `first_prompt_s`, `fit_check_s`, plus the absolute `fit_check_budget_s` ceiling, which is enforced even for a case with no baseline entry. |
| Every PR, reporting only | `ci.yml` real-slicer timing job | Six corpus cases with the pinned Orca, median of 3 repeats against `timing_baseline_real_slicer.json`. Warnings only, not a required check (see Slicing-speed gate below). |
| Weekly | `model-corpus.yml` | The downloaded public model corpus, sharded. |
| Live only, not in CI | operator | Real printer, real Spoolman and Moonraker, real Discord, the private Duplo 3MF. |

Container-marked tests (#971): tests that need the shipped container image (its entrypoint, state volumes or
runtime layout) carry the `container` marker. Outside the image they skip instead of failing on the
environment, so a local `pytest` run on a plain checkout can show 0 failures. Detection is
`PRINT_HANDY_IN_CONTAINER=1`: the Dockerfile sets it in the image, and the CI `offline-tests` job sets it
explicitly, so the marked tests keep running in CI. `tests/test_container_marker.py` pins the wiring.

Journey matrix: the rows in `test_journey_matrix.py` are the combinations the intake really offers, found by running
it. Combinations that are not offered (for example a split for a single oversized part, or a color-mode prompt for a
one-part model) are listed in `UNSUPPORTED` with the reason instead of being invented.

Fit-check time: `harness.FIT_CHECK_STEPS` names the offloaded bed fit probes. `time_corpus` sums their duration into
`fit_check_s`. This measures the fit probes inside the fake Discord flow on a CI runner, not a user's wall clock.

Phase 2 additions (#594):

- Flow-built state: `harness.run_case` now defaults to a slot map produced by the real `create_spool` and `bind_slot`
  against fake Spoolman (`spool_fixture.build_state`). Every corpus case on both transports slices from it, and
  `_assert_flow_built_state` fails if the state was not built that way or later fake bookkeeping mutates it.
  The shared Spoolman client serves lifecycle GETs from that same fake. Unexpected urllib network attempts in
  default flow-built journeys fail even if production code catches the transport exception; roast generation and
  the multicolor summary's live purge-mode lookup are stubbed (purge mode is explicitly unavailable).
  Deliberate invalid-state cases still pass an explicit handwritten state file. Gateway-env runs keep the handwritten file.
- Journey matrix: added rotate (fits at about 0.95 to 1.0 scale, one plate), group-color split (scale 1.0, two plates)
  and per-journey scale, plate count, exactly one start and embedded tool to slot mapping assertions. Cancelling the
  fit prompt slices nothing, starts nothing and uploads nothing. These run the fake slicer: they prove the Discord and
  pipeline journey, not real slicing. Real Orca per PR covers the generated corpus in the `ci.yml` real Orca job.
- Ack order: `tests/hermes/test_component_ack_order.py` checks the interaction response is sent before the slow call
  for plate confirm, intake Retry, and spool-question same and cancel. The spool-question pair was a strict xfail
  until the handlers were fixed to acknowledge first (P2-164); the markers are gone.
  These are ordering tests with a fake Discord, not a real 3 s network proof. Not covered: insertion, new-spool and
  intake wizards, alert resume/pause/cancel.
- Fit-check time: `timing_baseline.json` has `fit_check_required`, the ten case/transport rows that run the bed fit check.
  A required row with no `fit_check_s` measurement fails the gate, so omission cannot pass silently. The 10 s
  `fit_check_budget_s` ceiling stays PROVISIONAL. Inspected #709 run [41242041079](https://github.com/catduckgnaf/print-handy/actions/runs/41242041079),
  artifact `discord-timing`: three repeats of 54 flows, 30 non-null fit measurements across ten case/transport keys,
  0.00219–0.02180 s (median 0.00776 s), `real_slicer=false`. This is useful mock-runner evidence but cannot calibrate a
  real-Orca or Discord network budget. Missing measurements in any repeat remain missing after median aggregation.

- Generated rearrange: four CC0 parts authored across 330 mm exercise automatic 100% rearrange without private Duplo.
  Both color modes complete one plate. One-color once failed because `slice_model` rejected the rearranged source with
  `color decisions must bind the exact source SHA256`; P1-50 fixed that by validating decisions against the original
  and rebinding them to the arranged derivative (`tests/pipeline/test_data2_fixes.py`).

Known gaps, still open on #594: the private Duplo model is not in CI; the generated rearrange journeys use the
mock slicer and do not establish real-Orca rearrange coverage. Shrink, rotate and split are covered on public
cases but not every fit option crosses every color mode (see `UNSUPPORTED` for model-specific unavailable buttons).
Painted-fit and distinct per-part slot mapping across all fits remain incomplete. The spool-question same/cancel
ack order was a product gap pinned by a strict xfail; the handlers now acknowledge first (P2-164) and the marker
is gone. Other handler families listed above have no new ordering proof.
The original five failures were not replayed on 2.1.9; fit_check_s has mock-runner evidence only; restart 503
coverage is the Spoolman bind refusal, not a gateway restart.

## Test-suite cleanup accounting (2026-11-07)

A line-by-line audit of every file under `tests/` (per-file inventory,
verdicts and evidence in `tests/TEST-SUITE-AUDIT-2026-11-07.md`). Headlines:
the offline legs now install the shipped binaries they used to skip on
(openscad, xvfb, tesseract-ocr) and build the native bounds extension, so
the design, render-preview, label-OCR and native-bounds tests execute in
every offline leg; the host-only sol_recovery coupon tests now run against
a shipped corpus case; the post-it archive tests and the `reviewed-orca`
coupon variant were removed as dead (they skipped on every machine); the
V14 support-clearance test was rewritten as a synthetic component-based
fixture after its hardcoded home-directory path was found; and the two
Hermes dispatcher tests were rewritten against the plugin's registered
hooks because `hermes_cli` is an external package nothing provides.
Legitimate opt-in gates (real slicer, real Spoolman, private corpus) are
unchanged and each names the job that satisfies it in the audit file.

## OpenSCAD real-binary expansion (2026-11-07)

OpenSCAD is a shipped dependency, and "lots of OpenSCAD testing" is a
standing direction, so the real-binary surface was mapped and then widened.
Every product path that shells out to the binary, and how it is tested:

| Product path | Coverage |
|---|---|
| Design render seam (`core/design.py` `render_scad`: `--hardwarnings`, minimal env, rlimits, timeout) | Real binary in `tests/core/test_design.py`: exact dimensions/volume, a bored block's removed volume, `$fn` tessellation vs volume, tiny/large scales (0.5 / 2 / 250 mm), unknown-variable refusal under `--hardwarnings`, syntax-error text, empty-geometry refusal, a 2-second timeout on a 300-sphere union, a Unicode/space source path |
| Design engine turns (`DesignEngine.run_turn`: render, validate, preview, repair loop, session store) | Real renders behind the scripted model in the same file: parameter-driven geometry changes across turns, warning-class repair with the real error fed back, the preview as a real 900x700 four-view sheet (pixel-checked), pruning after real renders, a Unicode/space cache path; edit-session real renders were already covered |
| Preview rendering (`core/render_preview.py` `render` / `render_sheet`) | Real binary in `tests/pipeline/test_render_preview.py`: the sheet is 900x700 with four non-blank tiles and labels, a render from a Unicode/space path (centering/oversize scene checks were already covered); the non-STL refusal test in the same file is deliberately ungated — `render()` validates the suffix before any binary call, so it runs on every machine (issue #1060) |
| CAD split execution (`core/cad_split.py` `_run_openscad`, `prepare_cad_split(execute_openscad=True)`) | Real binary in `tests/core/test_cad_split.py`: `-D` defines choosing parts and scaling geometry, and `prepare_cad_split` end to end — a custom one-bore plan and the shipped rack preset — asserting half dimensions, the cut interface on the cut plane, volume conservation against the expected bore removal, the combined plate, the dowels, and the on-disk validation report |
| Design dispatch (`plugins/print-feedback` `design_dispatch`) | Real renders behind the fake transport in `tests/test_design_dispatch.py`: Slice it / Change it / Cancel were already covered; added a warning-class design repaired through dispatch before posting, and a parametrized design whose rendered dimensions are what the post claims |
| Sandbox pre-scan, extraction, parameters (`scan_scad`, `extract_scad`, `scad_params`) | No binary needed; covered with fakes in `tests/core/test_design.py` and `test_design_841.py` |
| Bot check design row (`bot/__main__.py`) | A PATH probe; covered with `which` mocked in `tests/bot/test_design_check_row.py` |

All real-binary tests use the existing gates (`needs_scad` /
`needs_openscad` skipif on `shutil.which`), so they skip on a machine
without OpenSCAD and execute in every offline CI leg, which installs it.

Finds from this pass: executing the rack preset for real showed its half
boxes clip a Y-offset source, so the end-to-end test's source is centered
(the fake-run test cannot see clipping); and `render_scad`'s error text
drops the `WARNING:` line that `--hardwarnings` made fatal, filed as
issue #1046 (the tests pin the text that is returned today). Deliberately
not covered end to end: the Mac Mini preset's two-shell split path (nine
OpenSCAD runs against a synthetic two-shell source; its union seam is
covered by the `_run_openscad` define tests) and `model_job`'s
`prepare_cad_split` call site, which the intake suites cover with the
execution layer faked.

## Testing-hardening pass (2026-11-07)

A deep dive over version coverage and the merged-PR archaeology produced the
suites below. They exist to make specific historical failure shapes impossible
to reintroduce silently; each suite's docstring names the incident it pins.

### Upstream versions: parity and step size

The "real" test harnesses once ran older upstream versions than the shipped
pins, so a green check proved Print Handy against the wrong Spoolman / Bambu
Studio. Two guards now hold:

- **Parity** — `tests/test_upstream_version_parity.py` requires the Spoolman
  version + zip sha256 in the CI spoolman job and in
  `scripts/run_spoolman_native.sh` to equal the Dockerfile `SPOOLMAN_REF`'s
  recorded version, and `scripts/bump_upstream.py` now sweeps those harness
  pins in the same bot PR that moves the shipped pin (failing closed on
  drift). The Bambu fallback leg (Flathub Studio in the `Slicer test
  (bambu)` job) is deliberately NOT moved with `BAMBU_REF`; its divergence
  is a named, dated exception recorded in `tests/test_ci_bambu_workflow.py`,
  and any future unrecorded divergence fails.
- **Step size** — bump classification compares normalized numeric tuples
  (`02.09.01.70` -> `(2, 9, 1, 70)`): a change in the first or second
  component is a MAJOR step (Spoolman 0.27.0 -> 0.27.1 is minor; Bambu
  2.8.2 -> 02.09.01.70 is major); later-component-only changes and
  prerelease promotions of an equal core are minor. A major step's bot PR
  body carries a required "Major step — deep dive required before merge"
  section: read the upstream changelog for the range and re-examine the
  component's Print Handy surfaces (named per component in the automation)
  before merging. Minor steps keep the current body shape and get targeted
  regression tests around the touched surface instead.

Bundled-Spoolman update check: Spoolman 0.27.1 phones home to api.github.com
by default (`SPOOLMAN_UPDATE_CHECK`, upstream default true). Print Handy is
local-first and updates its bundled copy itself, so `docker/supervise.sh`
exports `SPOOLMAN_UPDATE_CHECK=FALSE` for the bundled instance, pinned by a
contract test in `tests/test_docker_release_contract.py`.

### HMS content integrity

The HMS lookup tests pinned mechanics but not content: a regeneration that
dropped codes would have stayed green. `tests/printers/test_bambu_hms.py`
now floors the table counts at the recorded generation, requires every
provenance prefix to keep non-empty per-model overrides, and decodes one
canary code per prefix. The generator tests cover a new prefix flipping the
common text by majority and by odd-one-out, not only the 093/094 tie-breaks.

### Fixture-vs-live rules

- Offline suites use fakes shaped like the real thing, produced by shared
  factories where one exists: `tests/gcode_factory.py` for slicer G-code
  (per-family headers, footer config blocks, multi-plate `.gcode.3mf`),
  `tests/corpus/fake_discord` for Discord, flow-built spool state for the
  corpus journeys. Hand-written stand-ins for a real writer's output are
  the drift pattern this pass removed for Wrapped: aggregates are now also
  built from records produced by the real record writers
  (`tests/test_wrapped_records_from_writers.py`).
- Live-tier suites keep their gates: the real-Spoolman service tests
  (`tests/services/test_spoolman_real.py` and
  `tests/services/test_spoolman_real_expansion.py`) run only with
  `PRINT_HANDY_SPOOLMAN_TEST_URL` set (the CI spoolman job and
  `scripts/run_spoolman_native.sh` provide the server); the real-ffmpeg
  Wrapped pacing test skips without ffmpeg/ffprobe on PATH. The
  expansion file carries the wider real-server surface: the core
  client's round trips, usage accounting and 404-vs-outage split,
  create/vendor/temperature recording, bind dispositions, unbind and
  swap, cross-printer claims, and reconcile against spools archived or
  deleted behind the cache's back. The older file's tests assume a
  fresh server (their first create must not reuse a filament); the
  expansion tests are written to pass on a reused server too.
- Round-2 homes (2026-11-07 cleanup follow-up): the Cloudflare Worker
  example tests (`tests/test_cloudflare_examples.py`) run in the offline
  legs, which now install Node.js the same way they install OpenSCAD and
  tesseract; the real bind-mount state-volume test
  (`tests/test_state_volume.py::TestEntrypointMigration::test_legacy_data_and_a_real_bind_mounted_state_volume_is_copied`)
  runs in the dedicated `State volume (real bind mount)` CI job on the
  runner VM, because `unshare -rm` is refused inside the offline job's
  container — that job probes namespaces first and fails loudly rather
  than skipping; and the complex-corpus fetched-files contract
  (`tests/corpus/test_complex_corpus.py::test_fetched_files_match_the_manifest`)
  runs in the weekly `model-corpus.yml` workflow (shard 0) against the
  corpus it fetches, under `PRINT_HANDY_PRIVATE_CORPUS`.
- One real-mountpoint cell is gated by name and runs in CI:
  `test_mounted_state_volume_copies_instead_of_symlinking` in
  `tests/test_entrypoint_layout_matrix.py` (`container`-marked) runs only
  with `PRINT_HANDY_LAYOUT_TEST_MOUNT=1`, which the `State volume (real bind
  mount)` job in `ci.yml` sets in its own step. The test makes the real
  mountpoint itself (a bind mount inside `unshare -rm`, the Compose shape)
  next to a populated legacy volume, and fails rather than skips when the
  namespaces are unavailable. Elsewhere it stays skipped. It is also listed
  in the live-proof roster below.
- Synthetic geometry fixtures are labeled synthetic in the file that
  generates them (see the support-coverage golden pack); no fixture
  presents generated data as a real printer archive capture.

### Strict xfails and the register

Known-defect tripwires are `pytest.mark.xfail(strict=True)` entries in
`tests/strict_xfail_register.json` (owner + expiry + reason, checked by
`tests/test_strict_xfail_register.py`): when the fixing PR lands, the test
XPASSes and the suite fails until the marker and the register entry are
removed. This pass registered four: the #979 "print it again" precedence
outcome and the two #981 Wrapped defects (UTC bucketing, mixed-fps video
pacing) are still armed; the fourth, the #978 yolo auto-applies-a-fix
invariant, already fired — PR #987 merged while this pass was in flight,
the marker and entry were removed on rebase, and the invariant is now a
plain passing regression test in `tests/bot/test_approval_invariants.py`. Characterization
tests that record current-but-wrong behavior (the dispatcher claim map,
the two last-print resolvers' divergence, Cura / plate-2 foreign-G-code
blind spots) are labeled as such in their files and are expected to be
rewritten by the fixing change, not deleted quietly.

### Determinism / flake register

Suites added by this pass are deterministic by construction: fake clocks
(event starvation), fixed archives with explicit mtimes (dispatcher
resolvers), generated media probed with ffprobe (Wrapped pacing), and the
real entrypoint run against synthetic `$HOME` layouts. The known
nondeterminism sources in the wider suite stay as documented above
(real-slicer timing is reporting-only; the Matrix ordering flake is
root-caused in its own section). A new flake gets a section here with its
root cause, not a retry.

### Live-proof roster (operator-only, not in CI)

These proofs need catduck's hardware and accounts and stay open until he
runs them; no offline suite substitutes for them:

| Proof | Source |
|---|---|
| Real-printer actuation + chat edit-plan end-to-end | #921 / #923 follow-ups |
| Multi-platform live bot pass (Discord / Telegram / Matrix) | #812 |
| Watchdog camera accuracy on a real camera | #455 |
| GPU-host vision provider run | #455 |
| Entrypoint mounted-volume copy cell (`PRINT_HANDY_LAYOUT_TEST_MOUNT=1`, runs in the `State volume (real bind mount)` CI job) | `tests/test_entrypoint_layout_matrix.py` (`container`-marked) |

## Slicing-speed gate (#734)

The correctness tests prove a slice is right. This gate proves the Discord journey did not get slower than
the last release. It measures whole flows through the fake Discord path (file in, intake questions answered,
sliced, ready to confirm), not single functions.

What runs:

| Leg | CI job | Cases | Status |
| --- | --- | --- | --- |
| Mock slicer | `discord-timing` | every corpus case, `direct` and `standalone` transports | A problem fails the job. The job is a required status check on `main`; CONTRIBUTING lists the required checks. |
| Real Orca | `slicer-timing-real`, "Slicer timing (real Orca, reporting only)" | `single-part`, `stl-single-part`, `plates-multicolor`, `witch-ghost-cults3d`, `rotation-fit`, `stl-large-plate` on `direct` | Reporting only. Problems are warning annotations; the job is `continue-on-error` and is not a required check. |

Both legs run `python -m tests.corpus.time_corpus` three times, then `python -m tests.corpus.timing_gate` on the
three result files. The gate compares the per-case median of the three repeats, never one sample. Each leg writes
a per-case table (median, baseline, limit, change) to the job summary and uploads its run files plus a
`proposed-baseline.json` as an artifact (`discord-timing`, `slicer-timing-real`).

The rule, per case/transport and per metric (`wall_s`, `first_prompt_s`, `fit_check_s`):

    median > baseline x (1 + tolerance) + slack   ->  problem

Each baseline file sets its own `tolerance`. Both use `tolerance` 0.10, the issue's +10%. On the mock leg the
slack is relative with a floor, max(0.35 s, 10% of the baseline) (#1142): a percentage alone cannot absorb
runner noise on a 0.07 s flow, and the old flat 0.5 s could not fire at all on a sub-second case (a 0.01 s
case could reach 0.49 s and pass). The real-Orca leg keeps the flat `slack_s` via `--absolute-slack`, because
its noise is measured in seconds.

Why the flat slack was measured first: the mock leg's flows are tiny (0.02 to 1.1 s), and on a shared runner a
tiny flow's median moves far more than 10% from noise alone. Twelve green `main` runs (2026-11-01,
`discord-timing` artifacts, 36 repeats, 68 case/transport rows each) showed medians of 0.07 s flows reading
0.12 s (+70%) with no code change. To pick the slack, each of the twelve runs was used in turn as the
baseline (the same `--record` the release step uses) and the other eleven were gated against it at +10%:

| `slack_s` | runs failing (of 132 pairings) | worst median over its limit |
| --- | --- | --- |
| 0.10 | 28 | +0.21 s |
| 0.25 | 4 | +0.06 s |
| 0.50 | 0 | 0.19 s under the limit |

So 0.5 s was the smallest tested flat slack that never failed on unchanged code. The floor that replaced it
on the mock leg, 0.35 s, comes from the same kind of measurement: eight green `main` runs gated against the
checked-in baseline needed up to 0.33 s of slack (one run read a 0.38 s case at 0.75 s with no relevant code
change), and 0.30 s already failed two of 312 case/metric pairings. A flow shorter than the floor therefore
cannot be gated tighter than runner noise by this check alone; the reason rule and the cumulative limit
below are what keep a series of small raises honest.

A raise is bookkeeping, not just numbers. `--record` leaves a raised entry's `reason` empty and keeps the
old one as `previous_reason`; the gate refuses a raised entry whose reason is empty, under 20 characters,
or the previous raise's reason copied over, so every raise needs fresh words. Every entry also keeps
`original`, the first values ever recorded for the case (seeded from `raised_from` for entries recorded
before the field existed; the #1142 review stored it explicitly on every entry, and corrected
parts-512-four-color's seed to its first recording, 4.74 s — the 3.77 s previously carried as its
`raised_from` appears in no recorded baseline in the file's history, and the +38% drift printed for that
case came from the seed, not from the code). Past +25% over `original` the gate prints a warning; past +50% it fails, even
when each individual raise was small. Every job summary and every `--record` proposal prints the cumulative
drift of every raised entry, however small, so a re-record shows its own total before anyone merges it.

Two mock-leg cases are not file drops (#1142). `typed-setup-walkthrough` (`[entry] kind = "typed"`) times a
scripted typed-message conversation (start the guided setup, answer its first question, cancel), so the
per-message handling cost is gated, not just the drop flows; it carries a wall baseline only, because the
walkthrough's questions are typed replies rather than timed prompt events. `print-next-second-plate`
(`[entry] kind = "print-next"`) runs a two-plate drop as untimed setup, credits the first plate complete
the way the print listener does, then times only the `/print-next` segment — its prompt and its start —
so the next-plate path is gated on its own numbers instead of hiding inside the drop flow's.

The real-Orca leg keeps `slack_s` 1.0 s and stays reporting-only, because its measured runner noise is too large
for any blocking limit in the tested range (see Real-Orca runner noise below). It runs one untimed warm-up slice first, because the first flatpak start pays
one-off costs. `must-shrink` and `stl-must-shrink` are left out on purpose: they take minutes and write a very large
G-code, so they would measure disk and size search, not regressions, and would make the job slow.

Neither leg measures a large plate (50 MB class), so neither can see the Python pass costs that dominate
one of those (#925/#952). Set expectations accordingly: on a large plate, slice-time verification has **no
fast path by design** — the bounds cache is cold for brand-new bytes and the verified-artifact skip applies
only at print-now — and its dominant term is the semantic motion-bounds parse inside `verify_gcode`
(measured on a synthetic 55 MB / 1.9M-line plate: the bounds parse alone is roughly two-thirds to
three-quarters of the verify interval; the sub-1.5 s figure in #925's original proposal was not achievable
for that parse and should not be quoted as an expectation). The wins #929 shipped for large plates live at
print-now (the verified-artifact skip) and in packaging (head/tail metadata, zip level 1), not in the
slice-time verify interval. Whether to add a large-plate case to the real-Orca leg is an open decision
on #952.

Noise controls in place: median of 3 repeats, per-case baselines (one heavy case cannot hide another's
regression), a floored relative slack for tiny flows, the warm-up slice, and the fixed runner image (`ubuntu-24.04`). Not
built yet: the same-job A/B against the merge base, and a control file to normalize runner speed. The stored
per-release baseline ships first; A/B is the next step if the real-Orca warnings prove too noisy to promote.

### The large-plate case (#952)

`stl-large-plate` joined the real-Orca leg on catduck's decision D4 on #952. The five original cases slice
in seconds, so none of them exercises the Python passes that dominate a large plate — the finalize and
verify passes over the whole file that #925, #929 and #952 are about. This case exists to time those passes
end to end.

The case is a generated solid 113 mm cube (`tests/corpus/cases/stl-large-plate`, written by
`make_models.py` from its `case.toml` spec like every generated case). The model file is a 684-byte STL;
the size is in the slice, not the fixture. On the AD5X template profile (gyroid 10%, 0.24 mm layers) a
local reference slice (OrcaSlicer 2.5.0-alpha; CI pins 2.5.0) produced a 54,614,337-byte /
1,970,123-line G-code — deliberately comparable to the 54,237,491-byte `plate_1.gcode` #952 measured.
Every timing run records `gcode_bytes` per case in its JSON artifact, so the size class is checkable from
the leg's own artifacts rather than from this note. Locally the flow took about 70 s wall per repeat, so
expect the leg to grow by minutes (three repeats), inside its 30-minute timeout.

It is reporting-only, like the rest of the leg; adding it promotes nothing. Its variance is **not yet
measured** — that measurement is the point of adding it now. Judge it from the leg's own artifacts over
time: the spread of its three in-job repeats, and the cross-run spread of its medians once green `main`
runs that include the case accumulate. The stored variance record
(`tests/corpus/timing_variance_real_slicer.json`) predates this case and says nothing about its noise yet.
Before anyone proposes promoting the leg, refresh that record from at least 20 recent green `main` runs
whose artifacts include `stl-large-plate` (the refresh commands under Real-Orca runner noise below) and
apply the existing promotion rule — zero failing pairings at the baseline's `tolerance` and `slack_s`,
this case included. The runner-speed grouping already measured for the small cases (fast and slow groups
about 1.5x apart) will move this case by more seconds than any of them; the refresh has to quantify that
before a blocking limit can be believed.

The checked-in mock baseline was re-recorded for #734 from `main` run 41365651326 (commit 60341e2) with these
limits; its two entries that rose carry a `reason`. The real-Orca baseline was recorded from this job's first CI runs
(its `recorded_from` names the run).

Other refusals the gate makes: a failed or errored flow, a case missing from the run, a new case with no baseline,
the fit check over `fit_check_budget_s`, a `fit_check_required` row with no fit measurement, an entry whose
numbers rose without a `reason` or with the previous raise's reason copied over, and a case whose measured
time is past the cumulative drift limit (+50% over its first recorded values).

### Updating the baselines (every release)

Each release becomes the new normal, so the gate only answers "did this change make slicing slower than the last
release?" The step is on the release checklist the Pre-release checklist workflow opens (section "Slicing-speed
baselines"), and CONTRIBUTING.md requires it before tagging. Nothing updates a baseline automatically.

1. Wait for the `ci.yml` run on the release commit on `main` to finish green.
2. Download its two timing artifacts:

       gh run download <run-id> -n discord-timing -D timing
       gh run download <run-id> -n slicer-timing-real -D slicer-timing

3. Each artifact holds `proposed-baseline.json`, made by `timing_gate --record --recorded-from "CI run <id> (<sha>) ..."`
   from the three repeats. To rebuild it by hand from the run files:

       python -m tests.corpus.timing_gate timing/run-*.json --record \
           --recorded-from "CI run <id> (<sha>), artifact discord-timing, median of 3" > proposed.json
       python -m tests.corpus.timing_gate slicer-timing/run-*.json \
           --baseline tests/corpus/timing_baseline_real_slicer.json --record \
           --recorded-from "CI run <id> (<sha>), artifact slicer-timing-real, median of 3" > proposed-real.json

4. Copy them over `tests/corpus/timing_baseline.json` and `tests/corpus/timing_baseline_real_slicer.json`. Review
   the diff. The `--record` step printed every raised entry's cumulative drift from its first recorded values
   to stderr (the job log when CI made the proposal); read it before copying. An entry that rose carries
   `raised_from`; write its `reason` (what got slower and why that is acceptable, for example a new Orca
   version or a new pipeline stage). The gate refuses a raise with no reason, or with the previous raise's
   reason copied over. Do not change `tolerance` or `slack_s` in this PR; that is a separate, measured change.
5. Open a PR named "Timing baselines for X.Y.Z", let CI gate it against the new numbers, merge it, then tag.
6. Record the decision on the release checklist issue: re-recorded or verified-no-change.

A baseline is only as good as the run it came from. If the release run's summary shows a case far off its usual
value, re-run the job and record from the re-run rather than freezing an outlier.

### Real-Orca runner noise

`tests/corpus/timing_variance_real_slicer.json` is the measured noise of `slicer-timing-real`, written by
`python -m tests.corpus.timing_variance` from the artifacts of 91 green `main` runs (2026-11-02 to 2026-11-05, three
repeats each). It holds, per case and metric, the spread across runs, and the same leave-one-out the mock leg's slack
came from: each run recorded as the release baseline in turn and every other run gated against it (8190 pairings).

What it shows:

- The checked-in baseline fails none of the 91 runs at +10% +1.0 s, but only because it happens to sit on a fast run.
- A baseline recorded on another run fails 1838 of 8190 pairings at +10% +1.0 s, and still 74 at +20% +3.0 s
  (the widest tested). The worst median sat 3.75 s over its limit at +10% +1.0 s.
- The cause is runner speed, not the code: hosted runners come in a fast and a slow group about 1.5x apart
  (`single-part` wall time 4.2 s versus 6.3 s on unchanged code; repeats inside one job agree within 1.3% median).
  Dividing each case by the same run's `single-part` time drops the cross-run variation from 14 to 20% to 0.6 to 12%.

So no blocking limit in the tested grid is safe, and widening the slack until one is would hide real regressions of
several seconds. The real leg stays `--warn-only` and `continue-on-error`; `test_timing_gate.py` refuses to drop
either while the record shows failing pairings at the baseline's own `tolerance` and `slack_s`. The next step toward
promotion is runner-speed normalization (a control case in the same job) or the same-job A/B against the merge base.

To refresh the record, download `slicer-timing-real` from at least 20 recent green `main` runs into one directory
each and rerun the tool:

    gh run download <run-id> -n slicer-timing-real -D artifacts/<run-id>
    python -m tests.corpus.timing_variance artifacts/* --source "<runs, dates, head>" \
        > tests/corpus/timing_variance_real_slicer.json

### Promoting the real-Orca leg

Promote only when a refreshed variance record shows zero failing pairings at the baseline's `tolerance` and
`slack_s`, measured with whatever normalization the gate then uses. Then remove `--warn-only` and
`continue-on-error` from `slicer-timing-real` and add it to the required checks. `test_timing_gate.py` pins the current
split: the mock leg blocks, the real leg reports.

## Deep-dive expansion (2026-11-07)

The "what other tests?" deep dive (P1/P2/P3 proposals) landed as one expansion PR. What each item added, and
the honest limits:

- **Parser differential + property tests** — `tests/core/test_parser_differential_property.py` and
  `tests/pipeline/test_gcode_parser_differential.py`: seeded generators (stdlib `random`, fixed seed ranges;
  no new dependency) drive generated meshes and G-code through paired implementations — mesh fast path vs
  forced-off reference, `mesh_fast` vs a slow bounds/volume reference, layer G-code parsing vs a slow
  reference, native vs Python bounds. The native side keeps the existing gate (it runs where the extension
  is built, including the offline CI legs that require it).
- **Hostile-model corpus** — `tests/pipeline/test_hostile_intake.py`: XXE and entity-expansion 3MF payloads,
  a high-ratio archive bomb, hostile part names (containment proven by walking the export tree), non-image
  thumbnails, and declared-vs-actual triangle counts. Payloads are generated in-test; no binary fixtures.
- **Print-lifecycle failure injection** — `tests/bot/test_lifecycle_failure_injection.py`: scripted failures
  at the deepest seams the fakes support — busy-printer start refusal, Spoolman down at completion (usage
  filed pending, per #387), Discord REST 429/5xx on the terminal post (named outcome, exactly-once
  delivery), listener restart mid-print reconciliation. One gap is documented in the file and filed as
  #1049: print-now with Moonraker offline raises a raw URLError instead of a named REFUSED outcome; the
  fix is a separate lane. The docker fake Moonraker only reports an idle printer, so scenarios are staged
  at the transport seams, not through it.
- **Klipper-generic + Sovol/Marlin parity** — `tests/printers/test_klipper_generic_behavior.py` and
  `tests/printers/test_sovol_marlin_behavior.py`: recorded-shape behavioral suites at Bambu-fixture depth
  for the Moonraker families (all `sovol.*` resolve to the Klipper adapter) and the OctoPrint families
  (`sovol_marlin.*`, `octoprint.marlin`). Not covered, for lack of any in-repo recorded shape: Moonraker
  WebSocket event frames and vendor-specific Moonraker dialects.
- **Real-slicer timing leg** — the hermes leg is unchanged and still reporting-only: the promotion criteria
  above (a refreshed variance record showing zero failing pairings) are not yet met for the real-Orca leg,
  and `stl-large-plate` variance is explicitly unmeasured, so this PR does not promote it. What landed
  instead: the standalone Discord transport now times `single-part` on the real leg (3 repeats, recorded
  as a proposed baseline for review, kept out of the comparison until a baseline is recorded), pinned by
  `tests/test_ci_slicer_timing_workflow.py`.
- **Upgrade golden fixtures** — `tests/fixtures/legacy/` + `tests/test_upgrade_golden_fixtures.py`: frozen
  pre-rename / pre-#1029 shapes run through today's readers (print-again planning, Wrapped aggregates,
  settings load with every retired key at once). Provenance per fixture is in `tests/fixtures/legacy/README.md`;
  one fixture (the pre-#392 Obsidian note body) is marked inferred there.
- **Replay beyond print-start** — `tests/replay/test_completion_replay.py` with recordings in
  `tests/replay/recordings/`: pause / cancel / complete / error sequences through the real listener,
  including the starter mention on terminal posts. The recordings are synthetic in the recorder's format
  (labeled in each file): no real scrubbed recordings of those flows exist in the repo.
- **Cross-transport render parity** — `tests/bot/test_transport_render_parity.py`: the same events rendered
  through Discord, Telegram and Matrix, asserting shared facts, platform mention rules, per-platform
  limits, and the no-LLM unmatched-text contract. There is no distinct CLI renderer in the tree; the
  CLI/standalone surface posts the same shared text through these senders.
- **Concurrency round 2** — `tests/test_concurrency_round2.py`: two printers binding against one
  Spoolman, start-while-printing, two users claiming one approval prompt, and torn/mid-prune record
  reads. Print Handy has no records pruner of its own, so the prune race is staged at the filesystem.
- **Supervisor soak** — `tests/test_supervisor_soak.py`: multi-day fake-clock runs over the outage
  notifier and persisted restart budget (once-per-incident alerts, budget exhaustion and recovery,
  restart persistence). The restart-counting rule itself lives in `docker/supervise.sh` bash, which has
  no fake-clock seam; the soak drives the persisted state through the real `supervisor_state` code.
- **Browser smoke** — `tests/test_webui_browser_smoke.py`: a small Playwright set (login, walkthrough,
  controls, camera, no page errors) gated on `PRINT_HANDY_BROWSER_SMOKE=1` plus an importable `playwright`,
  mirroring the real-slicer gate. It skips in the normal suite and is unexecuted until a CI job installs a
  browser (`playwright install --with-deps chromium`); that job is not added here.
- **Mutation pilot** — `tests/test_mutation_pilot.py` + `tests/MUTATION-PILOT.md`: 17 hand-built mutants
  across the bounds dispatcher and the slot-naming conversion, each paired with its killer test; 17/17
  killed, measured against scratch copies (method in the note).
- **AI/vision provider contracts** — `tests/test_ai_provider_contracts.py` + recorded shapes in
  `tests/fixtures/provider_responses/`: success, malformed, refusal, 401/429/500, timeout and wrong-shape
  responses replayed through the real parsing seams; every failure degrades to the code's named outcome.
- **Coverage floors** — `scripts/coverage_floors.py` + `tests/test_coverage_floors.py`: per-module line
  floors for five guard modules (start guard, launch state, refusal/failure wording, diagnostics plain
  path), measured from a focused guard-test run and set below it. The offline CI legs run the check
  reporting-only (continue-on-error) against each shard's own coverage.xml until the floors are confirmed
  against a merged full-suite report.
- **Non-Docker install surface** — `tests/test_install_surface_acceptance.py`: script syntax, systemd
  template vs installer constant consistency, the root plugin trio, and a real sandboxed standalone
  install (throwaway HOME) that completes and creates exactly the pinned path set. Named gap: no
  standalone uninstaller exists in the repo — install creates two user units plus the data folder and
  nothing removes them; the tests pin the created set as the contract a future uninstaller must match.
  A real VM job (systemd user session, real Moonraker) remains operator territory.
