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
| Hermes profile platform switch (#384) | `tests/test_profile_platform.py`, `tests/hermes/test_profile_platform_intake.py`, doctor + bot-check rows in `tests/test_doctor.py` / `tests/bot/test_health_and_transport.py`, `tests/test_install_hermes.py` |
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
| Docker image, release contracts, supervisor, healthcheck | `tests/test_docker_release*.py`, `tests/docker/`, `tests/test_combined_supervisor.py`, `tests/test_go2rtc_container.py`, `tests/test_native_release_acceptance.py` |
| Web UI (status, config, login, roster) | `tests/test_webui_*.py` |
| Doctor / bot check surfaces | `tests/test_doctor.py`, `tests/bot/test_health_and_transport.py` |
| Settings / config loading | `tests/test_settings.py`, `tests/test_print_handy_cfg.py`, `tests/test_webui_config*.py` |
| Docs / contribution policy / post-release surfaces / pre-release checklist | `tests/test_docs_index_contract.py`, `tests/test_contribution_policy.py`, `tests/test_post_release_surfaces_workflow.py`, `tests/test_pre_release_checklist.py` |
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
| Fail-soft state readers | Unparseable JSON already degraded correctly everywhere; parseable-but-malformed timestamps crash `recent_links`, `print_alert_buttons` and `print_next` — filed as bug #404 with strict-xfail pins in `tests/test_state_file_failure_paths.py` (no fix in this track, per #391's rules). |

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
- Found by the leg's first run, filed as #434: real Bambu Studio
  output has no `; generated by BambuStudio` line (the mocks invented
  it), so `slice_model`'s generator gate rejects every real Bambu
  slice after slicing — Bambu-as-primary and the Orca→Bambu fallback
  can never complete, for any printer. The two production-flow tests
  (A1 primary; fallback with a dead Orca command) are strict xfails
  against #434; the fix removes the markers. The A1 guard's
  Orca-only generator rule is the second layer #434 must reconcile.

## What runs when, and what is not covered (#594)

| Tier | Where | What it pins |
| --- | --- | --- |
| Every PR | `ci.yml` | Everything under `tests/` with the fake slicer, including `tests/corpus/test_journey_matrix.py` (pinned fit option x color mode inventory), `test_spool_fixture.py` (state built by the real `create_spool` and `bind_slot`), and `test_timing_gate.py`. |
| Every PR | `ci.yml` real Orca job | The generated CC0 corpus under `tests/corpus/cases` sliced by real Orca. |
| Every PR | `ci.yml` timing job | Median of 3 repeats against `timing_baseline.json`: `wall_s`, `first_prompt_s`, `fit_check_s`, plus the absolute `fit_check_budget_s` ceiling, which is enforced even for a case with no baseline entry. |
| Weekly | `model-corpus.yml` | The downloaded public model corpus, sharded. |
| Live only, not in CI | operator | Real printer, real Spoolman and Moonraker, real Discord, the private Duplo 3MF. |

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
  for plate confirm and intake Retry (passing), and for spool-question same and cancel (strict xfail, see below).
  These are ordering tests with a fake Discord, not a real 3 s network proof. Not covered: insertion, new-spool and
  intake wizards, alert resume/pause/cancel.
- Fit-check time: `timing_baseline.json` has `fit_check_required`, the ten case/transport rows that run the bed fit check.
  A required row with no `fit_check_s` measurement fails the gate, so omission cannot pass silently. The 10 s
  `fit_check_budget_s` ceiling stays PROVISIONAL. Inspected #709 run [41242041079](https://github.com/catduckgnaf/print-handy/actions/runs/41242041079),
  artifact `discord-timing`: three repeats of 54 flows, 30 non-null fit measurements across ten case/transport keys,
  0.00219–0.02180 s (median 0.00776 s), `real_slicer=false`. This is useful mock-runner evidence but cannot calibrate a
  real-Orca or Discord network budget. Missing measurements in any repeat remain missing after median aggregation.

- Generated rearrange: four CC0 parts authored across 330 mm exercise automatic 100% rearrange without private Duplo.
  Group-color completes one plate. One-color reproduces a production defect: `slice_model` rejects the rearranged
  source with `color decisions must bind the exact source SHA256`. Its success assertion stays strict-xfail; no
  production handler or hash guard was changed. This disproves the old blanket UNSUPPORTED exclusion.

Known gaps, still open on #594: the private Duplo model is not in CI; the new generated rearrange journeys use a
mock slicer and do not establish real-Orca rearrange coverage. Shrink, rotate and split are covered on public
cases but not every fit option crosses every color mode (see `UNSUPPORTED` for model-specific unavailable buttons).
Painted-fit and distinct per-part slot mapping across all fits remain incomplete. Product gaps: one-color
rearrange's source hash binding and spool-question same/cancel doing printer or Spoolman work before responding
(strict xfails, no production changes). Other handler families listed above have no new ordering proof.
The original five failures were not replayed on 2.1.9; fit_check_s has mock-runner evidence only; restart 503
coverage is the Spoolman bind refusal, not a gateway restart.
