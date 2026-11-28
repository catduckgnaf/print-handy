# Local Vision Failure Detector Evaluation (#1899)

This document records the research, reproducible benchmark results, licensing analysis, and migration decision for replacing the Obico YOLOv2 failure-detection model with **PrintGuard Standard** (`v3.0.1`) in Print Handy's built-in local camera watchdog (`print_handy/runtime/local_vision.py`).

## 1. Historical Evidence Recovery (#455, #1067, #1844)

We reviewed repository history and issues `#455`, `#1067`, `#1158`, and `#1844`:

- **Prior alternative identity:** The previously discussed open alternative was **PrintGuard** (`https://github.com/oliverbravery/PrintGuard`), specifically its pre-3.0 **Legacy** ShuffleNetV2 prototypical-network encoder from the maintainer's university dissertation (`oliverbravery/Edge-FDM-Fault-Detection`).
- **Prior in-tree evidence:** Neither `#455` nor `#1067` committed a numeric benchmark of PrintGuard inside this repository. `#455` Stage 0 evaluated Obico (`model-weights-5a6b1be1fa.onnx`) and Frigate (concluding Frigate is an NVR inference pipeline requiring a custom print-defect model and risking duplicate camera pulls unless fed from a single stream). `#1067` built the Stage 0 replay harness (`tests/runtime/vision_eval/harness.py`) with 3 synthetic sessions and 1 real healthy 12-frame print (`tests/runtime/fixtures/timelapse_real`) and deferred live multi-session real-camera accuracy proof to `#1082`.
- **Why PrintGuard Legacy underperformed historically:** In the maintainer's write-up (*Rebuilding PrintGuard's Brain*, `https://oliverbravery.uk/blog/rebuilding-printguards-brain`), Oliver Bravery documented that the Legacy dissertation model failed to generalize to community cameras due to static-camera shortcut learning, duplicate/leaky public datasets, limited lighting/angle diversity, and grayscale-only preprocessing.
- **Upstream bandwidth incident (#1844):** In November 2026, Print Handy CI test runs triggered ~1,100 daily downloads of Obico's 202 MB ONNX model from `obico.io`. `#1848` fixed this by bundling weights inside the Docker image (`ADD --checksum`), enforcing a suite-wide public-internet guard in `tests/conftest.py`, and blocking model downloads during tests. Obico later moved its weights to GitHub Releases (`TheSpaghettiDetective/obico-server`, tag `ml-models-2026-11-23`).

## 2. Candidate Inventory

| Candidate | Version / Pin | Architecture | Weight Size | Code License | Weights License | Deployment Model | Source |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **PrintGuard Standard** (selected) | `v3.0.1` (`b45ff609…`) | ConvNeXt-Atto, distilled binary classifier (`[1, 3, 224, 224]` -> scalar prob) | 13.56 MB | GPL-2.0-only | GPL-2.0-only (no separate model license) | Embedded ONNX in Print Handy (clean-room loader) or standalone PrintGuard Hub (`POST /api/v1/classify`) | <https://github.com/oliverbravery/PrintGuard> |
| PrintGuard Light | `v3.0.1` (`b45ff609…`) | MobileNetV4-Conv-Small, distilled (`[1, 3, 256, 256]` -> scalar prob) | 9.95 MB | GPL-2.0-only | GPL-2.0-only | Embedded ONNX or PrintGuard Hub | <https://github.com/oliverbravery/PrintGuard> |
| PrintGuard Legacy | `v3.0.1` (`b45ff609…`) | ShuffleNetV2-x1.0 prototypical network (`[1, 3, 224, 224]` grayscale -> 1024-d embedding + prototypes) | 5.04 MB + 56 KB prototypes | GPL-2.0-only | GPL-2.0-only | Embedded ONNX or PrintGuard Hub | <https://github.com/oliverbravery/Edge-FDM-Fault-Detection> |
| Obico (baseline, replaced) | `5a6b1be1fa` (`ea3f98cc…`) | YOLOv2-style region detector (`[1, 3, 416, 416]` -> `boxes` + `confs`) | 202.22 MB | AGPL-3.0 | AGPL-3.0 | Embedded ONNX or self-hosted `obico-server` | <https://github.com/TheSpaghettiDetective/obico-server> |
| Frigate (pipeline only) | N/A | NVR inference pipeline (COCO by default; no built-in print-defect weights) | Pipeline only | MIT | Depends on supplied model | Separate NVR container; requires custom print-defect model and careful RTSP fan-out so printers are not double-polled | <https://github.com/blakeblackshear/frigate> |
| Hosted / LAN AI endpoint | Operator choice | Multimodal LLM via `[llm]` + `[ai.small]` | Remote | Operator choice | Operator choice | Optional (`vision.ai_enabled`); frames leave the machine when hosted | `docs/SETTINGS.md` |

## 3. Reproducible Benchmark Results

### 3.1 Local Hardware Measurements (Actual)

Measured via `python scripts/benchmark_vision_detectors.py --models-dir <dir>` on an `x86_64` Linux host (6 logical CPUs, `onnxruntime 1.31.1`, `CPUExecutionProvider`, isolated subprocess per model/thread configuration) over the 12 real `1280x720` healthy camera frames in `tests/runtime/fixtures/timelapse_real` (16:9 frames produce 2 overlapping square tiles for PrintGuard models and 1 full-frame resize for Obico):

| Candidate | Size (MB) | Load (1T, ms) | Mean Latency (1T, ms) | Mean Latency (4T, ms) | Peak RSS Delta (MiB) | `timelapse_real` Mean Score | `timelapse_real` Max Score | Posted False Alerts (Watch Schedule) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **Obico** (baseline) | 202.22 | 1,641.7 | 335.7 | 112.5 | +552.1 | 0.0273 | 0.1374 | 0 / 1 |
| **PrintGuard Standard** | **13.56** | **116.2** | **66.3** | **43.8** | **+63.6** | 0.1123 | 0.6464 (frame 9, stray strand on bed) | **0 / 1** |
| PrintGuard Light | 9.95 | 83.6 | 44.5 | 34.7 | +44.7 | 0.0907 | 0.1451 | 0 / 1 |
| PrintGuard Legacy | 5.04 | 95.9 | 42.1 | 38.4 | +37.2 | 0.0756 | 0.1208 | 0 / 1 |

Key observations from the measured run:

- **14.9x smaller weights** (13.56 MB vs 202.22 MB), cutting ~189 MB from every Print Handy Docker image and source install.
- **8.7x lower RAM overhead** (+63.6 MiB vs +552.1 MiB RSS), substantially improving viability on constrained single-board computers, Unraid servers, and Home Assistant hosts running concurrent `go2rtc` / timelapse workloads.
- **5.1x faster single-thread CPU inference** (66.3 ms across 2 tiles vs 335.7 ms) and **14.1x faster session cold load** (116 ms vs 1,642 ms).
- **Threshold calibration (`POSITIVE_FLOOR = 0.50`, `FAILURE_AT = 0.75`):** PrintGuard Standard uses `0.50` as its binary decision boundary and `0.75` as PrintGuard's own default alert threshold. On `timelapse_real` (a healthy print with a stray filament purge piece resting on the bed), frame 9 scored `0.6464` (noted once as weak `uncertain`, not posted) and frame 10 dropped back to `0.0722` (`ok`), producing **zero posted false alerts** across the session. All 3 synthetic `vision_eval` sessions (`synthetic-healthy-01`, `synthetic-stalled-01`, `synthetic-detached-clean-01`) scored `0.0135`–`0.0149` under PrintGuard Standard.

### 3.2 Upstream Maintainer Held-Out Evaluation (Reported, Not Independently Replicated)

PrintGuard's `v3.0` README and blog report an evaluation on 1,534 curated frames from 30 unseen community hubs (20 failed prints, 70 good prints; hub-separated holdout):

| Model | Failed Prints Caught | Good Prints Alarmed |
| --- | ---: | ---: |
| PrintGuard Standard | 10 / 20 (50%) | 1 / 70 (1.4%) |
| PrintGuard Light | 5 / 20 (25%) | 1 / 70 (1.4%) |
| PrintGuard Legacy | 1 / 20 (5%) | 1 / 70 (1.4%) |
| Obico (Spaghetti Detective) | 3 / 20 (15%) | 0 / 70 (0%) |

Note the small failure sample size (20 failed prints) and the fact that these numbers come from the upstream maintainer's own holdout rather than an independent Print Handy multi-session failure corpus.

### 3.3 Real-Data Limitations and Hardware Not Tested

- **Real failure corpus gap:** This repository currently ships 1 real healthy camera session (`timelapse_real`, 12 frames) and 3 synthetic sessions (`vision_eval`). It does **not** yet contain real multi-session labeled failure sequences (spaghetti, detached parts, air printing, first-layer failures across diverse angles/lighting/filament colors). Operators can evaluate PrintGuard Standard on their own captured failure and healthy print folders offline using `python -m tests.runtime.vision_eval --model /path/to/printguard-standard.onnx --frames healthy/ --frames failed/=spaghetti`.
- **Unsupported failure classes:** PrintGuard Standard is a binary failure classifier (trained on spaghetti, detached parts, and layer splits) returning one calibrated failure probability. It does not output bounding boxes (so the Stage 0 box-geometry detached heuristic in `vision_eval` receives whole-tile windows only) and cannot certify `empty_idle` (auto light-off continues to rely on `local_empty` or the AI endpoint).
- **Untested hardware accelerators:** Only `x86_64` Linux CPU (`CPUExecutionProvider`) was measured in this run. **Not measured here:** Intel OpenVINO / Arc iGPU (`ORT_FLAVOR=openvino`), NVIDIA CUDA / TensorRT (`ORT_FLAVOR=cuda`), AMD ROCm, and ARM64 CPU (e.g., Raspberry Pi 4/5). Provider auto-selection remains covered by offline unit tests; live GPU acceleration remains unverified.

## 4. Licensing, Architecture, and Deployment Analysis

- **License boundary (#1158):** Print Handy is licensed **AGPL-3.0-only**. PrintGuard (`oliverbravery/PrintGuard`) is licensed **GPL-2.0-only**, with no separate permissive license declared for its ONNX weight files. Because GPL-2.0-only and AGPL-3.0-only are not mutually relicensable for combined code:
  1. **Zero PrintGuard source code is copied into Print Handy.** `print_handy/runtime/local_vision.py` is an original clean-room AGPL-3.0-only implementation written against PrintGuard Standard's documented `metadata.json` input/output contract (bilinear shortest-edge resize to 256, centered 224 crop, ImageNet mean/std normalization, overlapping square tiling for wide frames, scalar probability read).
  2. **Standalone unmodified weights:** The unmodified `models/standard/model.onnx` file is fetched at Docker build time from the pinned `oliverbravery/PrintGuard` GitHub commit (`ADD --checksum`), placed read-only at `/opt/print-handy/models/printguard-standard.onnx` (loaded as data by MIT-licensed `onnxruntime`), and explicitly documented as **GPL-2.0-only** in `docs/COMPONENTS.md` and `NOTICE`.
  3. **Alternative considered (PrintGuard Hub sidecar):** Running `ghcr.io/oliverbravery/printguard` as an external HTTP service (`POST /api/v1/classify`) avoids shipping GPL-2.0-only weights in the Print Handy image altogether, at the cost of requiring a second container and losing out-of-the-box single-container local detection on Unraid and source installs. The embedded standalone-weights architecture was chosen by the project owner to preserve zero-setup single-container operation while maintaining strict code separation and explicit GPL-2.0-only attribution.
- **go2rtc and watchdog safety preserved:** Print Handy continues to pull stills exclusively from its `go2rtc` fan-out (zero extra printer webcam connections), preserves the existing `VisualPrintWatch` verdict contract (`ok` / `uncertain` / `spaghetti`), sensitivity profiles, `FailureStreak` temporal escalation, explicit fail-soft `uncertain` degradation, and operator-confirmed pause/cancel behavior.

## 5. Distribution, Caching, and Automated Updates (#1844)

- **Docker image:** `Dockerfile` (`FROM scratch AS vision-model`) fetches the pinned `oliverbravery/PrintGuard` commit via `ADD --checksum=sha256:...` and copies it read-only (`0444`) into `/opt/print-handy/models/printguard-standard.onnx`. BuildKit caches the `vision-model` layer across builds, and running containers never contact GitHub.
- **Source installs:** Download the pinned 13.56 MB file once in a background daemon thread under an interprocess `flock` (`PrinterControlLock`), write to a process-unique `.part` file, verify exact SHA-256 and byte-size cap before atomic `os.replace`, and back off exponentially on failure.
- **Zero downloads in CI tests:** `tests/conftest.py` enforces both the suite-wide public-internet socket guard (`_no_public_internet`) and `_no_detector_model_download`. Offline tests use a 273-byte synthetic ONNX stand-in (`tests/runtime/fixtures/tiny_failure_detector.onnx`) with PrintGuard Standard's I/O contract.
- **Automated upstream tracking:** Both `scripts/check_upstream_releases.py` (`upstream-watch.yml`) and `scripts/bump_upstream.py` (`upstream-bump.yml`) now track `oliverbravery/PrintGuard`. Automated bump PRs verify `models/standard/metadata.json` matches the expected input/output contract before updating `local_vision.py`, `Dockerfile`, `docs/COMPONENTS.md`, and `docs/SETTINGS.md`, stopping for human review if upstream ever changes the model contract.

## 6. Upstream Contribution Opportunities & Opt-In Training-Frame Feedback

- **Upstream contribution opportunities:**
  - Share independent multi-printer replay results and edge-case false-positive observations (such as stray purge strands on 16:9 CoreXY beds like `timelapse_real/frame-000009.jpg`) with `oliverbravery/PrintGuard` (related to upstream discussion `#204`, *Ways to deal with consistent false positives*).
  - Clarify model weight licensing upstream (e.g., whether future model exports could carry an explicit permissive or dual license alongside the GPL-2.0-only hub application).
- **Opt-in training-frame submission (`docs/feedback.md`) feasibility:**
  - PrintGuard already operates an opt-in, human-reviewed submission flow (`docs/feedback.md`) backed by a Cloudflare Worker + R2 bucket funded out of pocket by its maintainer, with strict rate caps: **60 frames/day per hub**, **120 frames/day per network**, **1,000 frames/day globally**, **5 GB total bucket cap**, and **150 KB/frame**.
  - **Decision:** Print Handy **does not** integrate with or send frames to PrintGuard's feedback endpoint. Sending traffic from another application to a maintainer-funded, globally rate-limited inbox without explicit permission would risk exhausting PrintGuard's daily quota for PrintGuard users. Any future Print Handy opt-in contribution flow must be off by default, require maintainer confirmation and endpoint policy agreement first, enforce an explicit human review/label/send step, minimize metadata, clearly disclose recipient/retention/deletion limits, and support one-click revocation.

## 7. Migration and Rollback

- **Existing Docker installs:** Upgrading to the new Print Handy image automatically ships `/opt/print-handy/models/printguard-standard.onnx` with no configuration changes required (`vision.local_detector_enabled` and `vision.watch_source` are unchanged).
- **Existing source installs:** On the first camera watch check after upgrade, Print Handy downloads `printguard-standard.onnx` (~13.6 MB) into `<state_home>/models/` once; any leftover `obico-failure-detector.onnx` in `<state_home>/models/` is unused and may be deleted by the operator to reclaim 202 MB.
- **Custom `vision.local_model_path`:** Operators who explicitly pinned `vision.local_model_path` to an older Obico YOLOv2 `.onnx` file must clear `vision.local_model_path` (to use the bundled PrintGuard Standard model) or point it at a compatible PrintGuard probability-output ONNX model; incompatible output shapes fail soft to `uncertain` with a clear log error.
- **Rollback:** Reverting the PR restores the previous image tag and detector module with no database or config-schema migration required.
