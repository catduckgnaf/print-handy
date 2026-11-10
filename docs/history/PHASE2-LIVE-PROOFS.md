# Evidence-bound phase 2 work: #1067–#1072

This is an operator checklist and a **read-only evidence harness**, not proof that any
hardware, camera, cloud account or release has been checked. The linked issues remain
the source of truth. Do not mark a case complete based only on a passing CI test.

## Use and privacy

Before running anything against live printers, back up state and review any command
that changes the printer or data. Especially: the **apply** half of duplicate-number
renumbering is destructive to live state; this PR does not run it or authorize it.
No live printer, firmware, GPU account or Docker registry is contacted by these scripts.

List the cases:

```sh
python scripts/phase2_evidence.py cases
```

Capture a **sanitized** result you have actually obtained on the relevant host:

```sh
python scripts/phase2_evidence.py record \
  --issue 1070 --case runout-diagnostics --status observed \
  --context "AD5X HelixScreen paused runout test" \
  --note "Operator collected the attached redacted diagnostics bundle" \
  --artifact sanitized-diagnostics.txt --out evidence-1070.json
python scripts/phase2_evidence.py verify \
  --record evidence-1070.json --artifact-dir .
```

Attach both the JSON record and the sanitized artifact to the matching GitHub issue.
The helper records a SHA-256 and size, checks file integrity and rejects unsupported
case names. It does **not** inspect logs for secrets, validate sensor truth, run a test,
or upload anything. Review and redact hostnames, tokens, network addresses, filenames,
slicer models and personal data manually before posting. A record with `observed`
means only that an operator supplied an attachment, not that the finding is correct.

If a check cannot be run, use `--status blocked` and a concrete `--note` instead.
Use `--status deferred` only after the authorized owner explicitly decides to defer
it. A blocked/deferred record is never a successful hardware proof.

## #1067: camera and GPU proof

Use real hardware, real timelapse frames and a labeled camera evaluation set.
Record separate evidence for NVENC, Intel VAAPI/QSV, AMD encode, actual ONNX GPU
provider inference, camera classification and the Stage 3 classifier decision.
Name the model digest, GPU/driver/firmware versions, host image digest, successful
provider, CPU fallback, latency distribution and false-positive/false-negative
counts. Never use synthetic camera frames as proof of live classifier accuracy.
OpenVINO is currently amd64-only in the build; the published image remains
CPU ONNX inference. The Cloudflare route is optional and requires a real measured
account usage record. Missing proof must remain *unverified*, not green.

## #1068: live renumber, rollback and assembly checks

For the owner-only duplicate renumber, first review the plan printed by
`renumber-duplicates`, save a sanitized record, back up the live state, and
use `--apply` only after explicit owner approval. The helper does not call it.
Record before/after print numbers and any rollback evidence without exposing user data.

For rollback, use the existing **Docker image validation (manual)** workflow on
both native amd64 and arm64. The previous-release phases must use the previous
tag's own example environment, not the candidate's new keys. PR #1075 already
corrected that harness and its validation runs passed; record those exact run
URLs in #1068 rather than rerunning a fixed issue as new work. The assembly
production caller remains dependent on the arrival of real multi-part sessions;
test-only calls of `assembly_check.review()` do not satisfy it.

## #1069: a reproducible OpenSCAD comparison

Run on the same host with the pinned 2021.01 binary and a candidate binary
containing both CGAL and Manifold backends. Supply actual project .scad fixtures,
including a pathological high-$fn or Minkowski model and a cad_split case.

```sh
python scripts/benchmark_openscad_backends.py \
  --baseline /path/to/openscad-2021 \
  --candidate /path/to/openscad-manifold \
  --scad sanitized-design.scad --scad sanitized-stress.scad \
  --repeats 3 --out openscad-phase2-results.json
```

Requires Linux and GNU `/usr/bin/time`. Each repeat performs an STL export
from the same .scad source and reports elapsed wall time, peak child RSS,
output size and SHA-256. It runs the pinned default backend, candidate CGAL,
and candidate Manifold. A run is a **failure** unless the slicer process exits
successfully and emits a nonempty STL. No silent benchmark success.

**Those numbers alone do not prove correctness:** independently compare mesh
volume, bounds, topology, intended openings, warnings, `--hardwarnings`,
and non-manifold diagnostics; document deltas. Preview PNGs with and without
`/dev/dri`, and headless Orca 2.5.0-rc GPU usage, remain separate operator
measurements. Record CPU/GPU utilization with host tools and cite the raw logs.
Do not claim that the benchmark proves rendering or slicing uses GPU compute.
No packaging switch to an unpinned candidate is made by this PR.

## #1070: paused runout on AD5X + HelixScreen

Collect the sanitized `/print-handy bundle` from an actual paused runout,
along with firmware version, IFS state, head sensor, command attempts and
HelixScreen UI observations. Establish whether the prohibition comes from
HelixScreen UI, firmware command support, active filament operation or Print
Handy state. **Do not bypass any temperature, sensor or motion interlock**.
Only add in-bot paused load/unload support if live evidence identifies a safe,
supported command and a guarded, explicitly initiated recovery procedure.

## #1071 and #1072: merged change and release checklist

#1071 was already completed and merged; it is intentionally absent from the
evidence-case inventory. Avoid duplicating its AI mesh repair caution.

#1072 is the 2.2.7 release checklist, not an authorization to publish. Follow
its maintained tagging, exact digest, docs and rollback gate evidence. PR #1075
corrected the previous-release environment mismatch. At the time this harness
was created, a 2.2.7 tag and candidate runs existed but no 2.2.7 GitHub
Release was verified. Do not tag, force-recut, publish, or describe the
release as shipped from this harness. Record candidate, image publication,
docs site, digest and live-check disposition separately. Operator-only
deferrals must name the actual owner decision, not merely an agent statement.

## Acceptance

CI proves the harness rejects missing, tampered and path-traversal evidence,
and fails cases whose tools produce no output. It cannot prove any of the
hardware findings by itself. The corresponding issues close only when the
required real evidence and authorized decisions are posted there.
