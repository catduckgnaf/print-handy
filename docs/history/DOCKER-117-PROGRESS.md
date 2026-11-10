# Issue #117 candidate progress

## Provenance and reconciliation

Standalone bot foundations came from zaheria985/print-handy commit
fba5727184dd8f0070e59c8f1fc088751142a003, integrated through #132. Actual tree
comparison with #132 merge 362c1642ed34f090f6f81c786c0b91c4f01cb514 differs
only in two timelapse files (53 added lines): #135 is not missing standalone
functionality to reimplement here.

Contributor Docker #139 commit 3bb0984250c20eb336d11ba1da06633950b45182 (MIT)
informs listener heartbeat, .dockerignore and optional Spoolman packaging.
This candidate keeps that credit, replaces binary downloads with pinned source
builds and adds container/process identity checks. It does not claim every #139
change is integrated or validated. Contributor PRs are not closed or merged.

## Implemented, native validation only

- Standalone default, separate bot/listener Compose services, external Spoolman
  by default and optional bundled override. No source mount masking /app.
- Packaged plugins retained; local secrets/state excluded from build context.
- Listener heartbeat reports connectivity independently from event-loop liveness,
  checks Linux process start identity and cancels on loop shutdown. Bot command
  health checks container/process identity in addition to its connection state.
- Optional source-built Orca stable/alpha selector; independent app-channel label.
  Setting the label does not switch the source checkout. YAML/TOML settings agree.
- Conditional display wrapper; setup, persistence, proposed promotion/rollback
  policy, manual DockerMan parity and unsupported limits documented in DOCKER.md.

Latest focused Host B run before documentation: 29 tests plus 11 subtests pass
in test_docker_release_contract.py, test_listener_health.py and
bot/test_health_and_transport.py. These are source/config checks, not image proof.
Entrypoint bash syntax and git diff whitespace checks passed in the prior run.

## Recovered image results and exact remaining gates

First approved stable-source attempt exited 100 during apt/dpkg because storage
was full, before Orca compilation. Host B storage was subsequently expanded.
The resumed attempt proc_2a21857e23b5 was killed with exit -15 on agent_close
while configuring dependency OpenEXR. It did not finish an image or runtime test.
This is not evidence that the Orca recipe succeeds or fails compilation.

No new build/container has been started during this continuation. The one bounded
4 CPU/6 GiB approval does not authorize unlimited replacements. After native
work, a fresh exact approval is required for a retry and its offline runtime
container: source dependency closure, executable/help, missing libraries, packaged
plugin loading, bot setup with dummy credentials, fail-closed no-Orca slicing,
heartbeat isolation, restart/persistence and signal handling remain image gates.
Native tooling cannot prove an OCI image or source-built slicer behaves correctly.
Do not infer those outcomes from old green GitHub source CI.

Alpha, ARM64/multiarch and Unraid installation remain unverified. Application
stable/development branch creation and automated image publication/promotion are
not performed here. No tags, release, deployment, Discord login/send, live printer
or Home Assistant verification is included. Candidate is not review-ready for
supported Docker delivery until image gates and final CI are satisfied.

## Finite follow-up review queue (not implementation under #117)

1. #136 KiCad: review separately for dependency and artifact boundaries.
2. #138 A1 with #137 vendor-delivery: reconcile shared profiles/settings/slicer work.
3. Documentation #140, #119 and #120: reconcile actual shipped behavior first.
4. #118 fixtures: review coverage and provenance after behavior settles.

## Phase 2 native continuation

See DOCKER-117-ACCEPTANCE.md for the finite original-criterion matrix and the
pending minimal Phase 3 approval packet. This supersedes the earlier 29-test
snapshot as an inventory of coverage, not as image certification. Bot shutdown
now awaits owned tasks/server closure and malformed PID health fails closed.
The environment starter defaults to external Spoolman and includes required
standalone inputs. New native tests exercise a relocated copied payload with
Hermes and socket access blocked, absent-Orca refusal, all default scalar keys,
independent build selectors and health/shutdown edge cases. Exact run counts and
head-specific CI are recorded in the phase handoff, not invented here.
