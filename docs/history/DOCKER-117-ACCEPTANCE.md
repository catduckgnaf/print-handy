# Issue #117 finite acceptance matrix (Phase 2 native scope)

This is not a release checklist marked green by source inspection. Each original
criterion is mapped below. Native checks never certify an image or publication.
Commands and exact-head CI are recorded on the Phase 2 card and PR134.

| Original acceptance criterion | Native evidence | Remaining gate |
| --- | --- | --- |
| Clean host deploys published Compose release without building | Compose requires explicit PRINT_HANDY_IMAGE; DOCKER.md states unreleased; contract tests cover commands, paths, health, no source mount | No published image, release install or clean-host recreation; publication is outside these phases |
| Starts healthy without Hermes | copied-payload subprocess imports with Hermes blocked, network forbidden and no source checkout on sys.path; bot check loads all 13 handlers; existing standalone-no-hermes CI installs runtime-only requirements | Actual OCI COPY/plugin/dependency closure, real startup and signals; healthy Discord login cannot be demonstrated with dummy token |
| Pinned 2.5 alpha and stable transition | OrcaSlicer is prebuilt-only: `docker/orca-prebuilt.sh` pins both channels by version, source revision and SHA256 (stable 2.4.2, alpha 2.5.0-alpha) and the SHA256 is verified before extraction; independent runtime selectors; native contract validates selectors; DOCKER.md explains manual stable transition | Pins do not prove usable binaries; each channel is proven by the hosted headless-slice smoke and runtime acceptance on the exact image |
| External endpoints and both Spoolman modes, bundled persistence | YAML/TOML effective parity, all supported scalar defaults round-trip through environment; external Spoolman blank default; optional pinned override with persistent directory; existing native service mocks/CI | Bundled OCI Spoolman recreation, real external-mode integration and data migration not proven |
| All published architectures, metadata, Compose, persistence | No architecture is advertised as published; source contract inspects Compose and label wiring; config selection regression retained | AMD64 stable image build/runtime, Compose engine validation, metadata/dependency closure, persistence across recreation; ARM64 entirely unverified |
| Immutable version tags, provenance and SBOM | Contributor provenance retained; build source refs pinned; proposed promotion/rollback documented | No image publication workflow, SBOM/provenance attestation, immutable tag policy enforcement or release. These are unimplemented release work, not image-only tests |
| Sanitized YAML/Compose and full supported-setting parity | test_settings + native release scalar inventory check; Docker YAML/TOML parity; empty required secrets and generic example hosts; optional octoprint_url/nozzle_type tests | Build selectors are not runtime YAML settings; list/map overrides use files; no fictitious Fluidd/Klipper direct URL keys; actual mounts/environment need OCI validation |
| Installation, setup, secrets, networking, Spoolman, persistence, rollback, Unraid | DOCKER.md and .env.example describe exact candidate limits, manual two-entry DockerMan parity, external endpoint configuration and recovery | No published supported installation; manual Unraid path not exercised; OCI resources and slicer memory limits unverified |

## Native runtime regressions

- Listener file: missing, malformed, stale, NaN, foreign runtime/host, reused PID;
  live own-process proof independent of printer connectivity; cancellation awaits
  heartbeat exit. tests/test_listener_health.py.
- Bot: fresh connection/no-Hermes verdict, per-container/start-tick check, invalid
  PID/overflow/future/NaN refusal, disconnect state, owned shutdown tasks/server
  closure. tests/bot/test_health_and_transport.py and tests/bot/test_runtime.py.
- No-Orca: fit pipeline invokes a deliberately absent executable and raises
  FileNotFoundError, with no artifact or network fallback. Direct slice_model
  refusal is also covered in tests/pipeline/test_orca_adapter.py. This does not certify
  the user-facing complete intake or a no-Orca OCI image.
- Copied payload contains print_handy, plugins and config only. A child launched
  outside the checkout validates setup with dummy credentials, all handlers and
  forbidden Hermes/socket access. It exercises relocated source, not Docker COPY.
- Source-built binary help, actual slicing/guards, loader dependencies, tini/xvfb
  signals and cross-container health collision behavior remain Phase 3 gates.

## Reconciliation and contributor credit

Read-only tree comparison of #132 merge 362c1642ed34f090f6f81c786c0b91c4f01cb514
against #135 head fba5727184dd8f0070e59c8f1fc088751142a003 differs only in
ISSUE-126-TIMELAPSE.md and test_timelapse_cli_transport.py (53 lines present in
merge). No duplicated standalone implementation is needed. #135 remains open.
#139 remains open at 3bb0984250c20eb336d11ba1da06633950b45182 by zaheria985;
its MIT attribution and selective reuse remain in DOCKER.md and progress notes.
No contributor PR was closed, merged or represented as wholly integrated.

## Minimal Phase 3 approval proposal (PENDING, not authorization)

Host: Host B, existing issue-117-standalone worktree at the final Phase 2 SHA.
Purpose: one stable-Orca source-build attempt, then one offline runtime fixture
container from that exact image. No alpha, ARM64, no-Orca image, bundled Spoolman
container, live endpoint, publish or replacement/retry is included in this packet.
Each additional container or replacement needs a fresh exact approval.

Build: one disposable named build container, 4 CPUs and 6 GiB memory, no restart,
maximum 5 hours, no privileged mode, device access or host listeners. Use exact
Dockerfile stable ref and APP_CHANNEL=development, WITH_ORCA=true,
ORCA_CHANNEL=stable. Phase 3 must identify the concrete builder/container name,
command and bounded storage before requesting approval; the failed prior build
is not reusable approval. Dependency fetch needs controlled outbound access;
networking must review any changed network/service configuration first. Stop if
recipe needs third-party source patches or an unapproved replacement.

Runtime: one disposable named container using the produced immutable image ID,
4 CPUs/6 GiB, no restart, at most 30 minutes, network none, no ports/devices,
no checkout mount, writable isolated disposable data paths only. Dummy secrets,
offline fixtures copied as data, no Discord/printer/HA access. Check packaged
plugin imports, executable help/dependency closure, actual local cube slicing and
guards, identity health, owned signals and data surviving process restart. Actual
container recreation requires another explicitly approved container; a process
restart must not be reported as recreation. Capture digest, logs and exit codes.

Native tooling on Host B cannot prove OCI packaging, Linux loader closure or
tini/xvfb signal behavior. Native tests are already preferred where sufficient.
Unraid is the eventual durable DockerMan host, not a disposable build default;
host A is latency-sensitive and unsuitable for this build. Neither host has
been approved as an alternate. Cleanup is limited to the two named disposable
containers and their own fixture data after evidence capture, never broad prune,
shared caches, volumes or unrelated resources. Retain image/logs for review unless
Catduck approves their removal. Lifetime and all exact names/commands must be
confirmed in Phase 3's live approval request before creation.

## Phase 3/4 contributor continuation (PR #139)

Reconciled onto main 570efd89401048f8d012d54fc135c2ef0e2a3256 by merge, not rebase. The
contributor's AppImage path and listener-only image are removed; #134's bot/listener design,
payload, health identity and source-only Orca are kept unchanged. The exact head SHA, commands and
CI links are in the PR #139 evidence reply, not repeated here so this page does not go stale.

Evidence below comes from a contributor cloud sandbox (Docker 29.9.1, BuildKit, amd64, 4 CPU,
15 GiB). It is development evidence, NOT maintainer-approved acceptance and NOT a published image.

| Gate | Contributor sandbox evidence | Remaining |
| --- | --- | --- |
| Runtime stage builds | main's runtime `useradd --uid 1000` fails on ubuntu:24.04 (its `ubuntu` user owns UID 1000: `useradd: UID 1000 is not unique`, exit 4) for every variant, so no image could ever complete; fixed by removing that user first; `WITH_ORCA=false` image built | Rebuild under approval |
| Bounded parallelism | `docker/orca-build-jobs.sh` (auto/1-64, cgroup-aware, validated); measured `auto` = 3 on 4 CPU/15 GiB; `CMAKE_BUILD_PARALLEL_LEVEL` bounds each dependency (previously each used all CPUs) | Peak memory of a full build unmeasured |
| Source-keyed cache | Cache-mount content survived a failed `RUN` and a cancelled build (measured); pinned checkout reused on re-run; toolchain fingerprint change reset build trees; ccache wired via `CC`/`CXX` (Ubuntu's `/usr/lib/ccache` has no `cc`/`c++`, so a PATH-only setup is bypassed) | Hit rate on a full build unmeasured |
| Stable Orca image | Not completed: the sandbox's network policy refuses GitHub source-archive downloads (HTTP 403) that Orca's dependency recipe needs; the build stopped at the first dependency download | One approved hosted or maintainer build |
| No-Orca runtime acceptance | `tests/docker/image_acceptance.sh` passed every check, external and bundled modes (bundled with a local stand-in for the pinned Spoolman image, built from the official 0.27.0 release, because the sandbox cannot pull from GHCR) | Same run under approval, with the real Spoolman image |
| Orca runtime acceptance | Harness checks written (library closure, `--help`, real cube slice plus G-code guards) | Needs the stable Orca image |
| ARM64, alpha, Unraid | None | Separate evidence each |

### Phase 3 hosted run: approval packet (PROPOSAL, not authorization)

- Workflow: `.github/workflows/docker-image-validation.yml`, `workflow_dispatch` only, actions pinned
  by commit SHA, read-only `contents` permission, no registry login, no push.
- Runner: one GitHub-hosted `ubuntu-24.04` (amd64, 4 vCPU, 16 GiB, ephemeral VM), one job,
  `timeout-minutes: 330`. Preinstalled SDKs are deleted from the VM first to free disk for the build.
- First dispatch: `app_channel=development`, `with_orca=true`, `orca_channel=stable`,
  `orca_build_jobs=auto`, `spoolman_mode=external`. A bundled-Spoolman run is a separate dispatch.
- Containers: one BuildKit build into the VM's image store; then about a dozen one-off `--rm` probe
  containers with `--network none`; then one Compose project (`ph-accept-<mode>-<run>`: bot,
  listener, fake Moonraker, plus Spoolman when bundled) on an internal network with no route out,
  `restart: "no"`, no published ports, dummy secrets, recreated twice by the harness.
- Lifetime and cleanup: bounded by the job timeout; the harness removes its own project and tag; the
  VM, image and build cache are destroyed with the runner. Only `evidence/` (pins, identities, build
  log, acceptance results and logs) is kept, as a 30-day artifact. The image is never uploaded.
- Not included: alpha, ARM64, publication, Discord login, printers, Home Assistant, Spoolman servers,
  retries without a new approval.

### Phase 4: installable package proposal (PROPOSAL, not publication)

- Registry: GHCR `ghcr.io/catduckgnaf/print-handy`, published only from a maintainer-approved
  workflow run gated by a protected environment. Nothing in this PR publishes.
- Build once, promote by digest: a validated development image is promoted to stable by re-tagging
  its exact digest (for example `docker buildx imagetools create`), never by rebuilding.
- Tags: the immutable digest is authoritative. Human tags carry both selectors, for example
  `2.1.0-orca-stable`, plus moving `stable-orca-stable` and `development-orca-stable` channel tags.
  An existing version tag is never overwritten. A `no-orca` variant is listed only after its own
  acceptance.
- Attestations: BuildKit SLSA provenance (`mode=max`) and an SPDX SBOM on every published image,
  verified after push.
- Rollback: redeploy the previous recorded digest with unchanged data paths (see the upgrade and
  rollback procedure in DOCKER.md). A genuine cross-version test needs two identified digests; the
  harness's same-image tag switch is recreation coverage only.
- Install: `PRINT_HANDY_IMAGE` set to the published digest in `.env`, or the Unraid templates' Repository.
