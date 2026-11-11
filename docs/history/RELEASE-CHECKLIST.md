# Release checklist

The one place that lists everything a non-nightly Print Handy release
must do, in order, and which check proves each step. It exists because
each item below was once caught late, missed, or learned the hard way —
the audit table at the end maps every item to the release that taught
it. Nightly builds are exempt from all of this (CONTRIBUTING.md).

Automation entry point: the **Release check** workflow
(`.github/workflows/release-check.yml`) runs `scripts/check_release.py`
plus the parity and Docker contract suites. Run it on the bump PR
before merging; it fails closed.

## 1. Pre-flight sweep (before the bump PR)

- [ ] Full offline suite green on current `main` (or on the bump PR's
  exact head in CI — CI on the exact head is authoritative).
- [ ] Skim every merged PR since the last tag (`git log <tag>..HEAD`)
  for: bugs, missing tests, `docs/SETTINGS.md` gaps, dead settings
  options (a dead option gets **wired**, never removed), version-surface
  inconsistencies, and template/parity drift.
- [ ] Every variable added or changed in the range has a verdict on
  every surface (the PR template's Surfaces and variables section);
  `tests/test_variables_contract.py` and
  `tests/test_docker_release.py` must pass.
- [ ] Slicing-speed baselines re-recorded from the release commit's own
  green `ci.yml` run on `main`: copy `proposed-baseline.json` from the
  `discord-timing` and `slicer-timing-real` artifacts over
  `tests/corpus/timing_baseline.json` and
  `tests/corpus/timing_baseline_real_slicer.json`, give every raised
  entry a `reason`, and merge as its own PR **before** tagging.
  Never mix proposed savings with shipped ones (audit A11).
- [ ] Pre-release checklist issue exists: dispatch
  `.github/workflows/pre-release-checklist.yml` with the target
  version. No non-nightly release is tagged until every fix-first item
  on it is resolved or explicitly deferred by catduck on the issue.
- [ ] Owed live checks from the range's PRs are listed on that issue.
  Offline results are never presented as live proof.

## 2. Release notes (voice guide below)

- [ ] Draft GitHub release notes cover **every** merged PR in the
  range, grouped user-first. The PR list comes from the tag range's
  ancestry (`git log`), never from dates or memory (audit A2/A12).
- [ ] `docs/history/INTERNAL_RELEASE_NOTES.md` gains a `# Version X.Y.Z`
  section covering the same PRs; the `Unreleased` bullets move into it.
- [ ] Any PR that changes behavior for existing installs gets an
  **Upgrade** line naming what changes for someone who already had the
  old value/behavior (audit A12, the #655 lesson).
- [ ] American spelling throughout; `check_release.py` scans the whole
  draft (the CI spelling job only sees PR diffs — audit A10).
- [ ] Voice: follow the guide in §5. Fun, plain-spoken, user-first; no
  corporate stiffness, no invented claims.

## 3. Version surfaces (the bump PR)

- [ ] `print_handy/__init__.py`: `__version__ = "X.Y.Z"`.
- [ ] `README.md`: the `Current release: **X.Y.Z**` line.
- [ ] `docs/history/INTERNAL_RELEASE_NOTES.md`: the `# Version X.Y.Z` section,
  dated.
- [ ] Release check workflow green on the bump PR's exact head; all
  other required checks green; review threads resolved.
- [ ] Docs sync: the docs in `docs/` are the source of truth. The Pages
  site (print-handy-docs) is synced by its Docs auto-sync workflow —
  if its Actions cannot open the sync PR, dispatch it by hand and hand
  the docs PR to merge, as in 2.1.8/2.2.1/2.2.5 (audit A1).

## 4. Publish sequence

1. Merge order: automation/hardening PRs first, baselines PR, then the
   bump PR. Verify CI fully green on each exact head before merging.
2. Tag `X.Y.Z` on the bump merge commit and push the tag **once**.
   Never move a published tag; a re-cut needs catduck's explicit call
   (audit A6).
3. Dispatch `docker-publish.yml` with `version=X.Y.Z`,
   `publish=false` (candidate) and read the result — especially the
   lifecycle gate, which is a warning in candidate mode and the only
   mount-realistic exercise of the entrypoint (audit A6).
4. If the surfaces gate stops the publish, run its printed remedy
   (docs sync), merge the sync, and re-dispatch (audit A1).
5. Dispatch `docker-publish.yml` again with `publish=true`,
   `--ref X.Y.Z`.
6. Create the GitHub Release with the §2 notes plus a provenance block
   (tag commit, publish run IDs, image digests).

## 5. Release-notes voice guide

Print Handy release notes sound like the person who built it telling
you what your printer can do now — because that is who writes them.
Playful, plain-spoken, user-focused, a little humor, zero corporate
stiffness. Study the shipped notes (GitHub Releases 2.2.x and the
README's own voice) before drafting.

Quoted examples of the voice (from shipped surfaces):

> "It is also a little mean. Every print start comes with a one-line
> roast of your model (another articulated dragon? bold choice)"
> — README

> "Paste a model link from your phone into Discord, Telegram or
> Matrix, tap a few buttons, and go live your life."
> — README

> "One Docker image for amd64 + arm64: ghcr.io/catduckgnaf/print-handy:2.2.5, also tagged latest."
> — 2.2.5 release notes (the standing opening fact, every release)

> "Opening Spoolman from the Home Assistant dashboard no longer
> dead-ends on the bundled Spoolman's one-line API stub: the page now
> explains itself and links onward to the Web UI Spools page."
> — 2.2.5 release notes, Fixes (names the pain, then the fix)

> "Every Web UI setting works: the 2.2.1 audit found six options that
> were offered and saved but changed nothing. All six are now wired,
> and none was removed."
> — 2.2.2 release notes (honest about the miss, plain about the fix)

Do:

- Lead with what the user can do, in their words ("See what's loaded",
  not "Add loaded-filament query surface").
- Name the pain before the fix in Fixes entries.
- Keep the jokes in the framing, never in safety or upgrade facts.
- Credit PRs at the end of each entry, e.g. ``.
- Say what was **not** verified (no live printer pass) as plainly as
  what was.

Don't:

- No "we are excited to announce", no "leverage", no "seamless".
- Never claim a cause that was not verified; say what changed.
- Never list an unshipped or open-PR item as shipped.
- Never bury an upgrade action; existing installs read Upgrade first.

Template: `docs/history/RELEASE-NOTES-TEMPLATE.md`.

## 6. Post-release verification

- [ ] GHCR verified **anonymously** (the repo token lacks the packages
  scope; `gh` reads 403/404 and look like a missing image — audit A7):
  fetch a pull token from `https://ghcr.io/token`, read the `:X.Y.Z`
  and `:latest` index manifests, confirm they are the same digest,
  both `linux/amd64` and `linux/arm64` manifests are present, and the
  per-arch config's `org.opencontainers.image.revision` label equals
  the tag commit.
- [ ] GitHub Release exists with the final notes.
- [ ] Post-release surfaces: the `post-release-surfaces` workflow
  records its check on the release checklist issue (or opens
  `Post-release surfaces check: X.Y.Z`). Verify the issue/comment
  actually exists — do not assume the workflow ran (audit A4) — and
  record a decision (updated / verified-no-change) per surface.
- [ ] Digest PR: record the published index digest in
  `docs/DOCKER.md` ("Pinning the image") as its own PR (audit A15).
  Never pin it in an Unraid template: both templates follow `:latest`.
- [ ] CA template repo (`catduck-unraid-templates`): verify parity
  only. Per catduck's standing restraint, routine variables are
  documented, not synced; touch the CA repo only for big changes or
  when something is actually wrong, and record the parity verdict on
  the checklist issue either way (audit A5).
- [ ] CA template tag: the CA template's `Repository` and first
  `Branch` stay `:latest` (the parity test in
  `tests/test_docker_release.py` enforces it). A release never bumps
  the CA template (#1143).
- [ ] Codecov: uploads are report-only. If tokenless uploads are
  rejected, the remaining step is catduck activating the repo on
  codecov.io — record it, never gate the release on it (audit A8).

## 7. Audit table — past catches and misses

Researched 2026-11-07 from git history, GitHub Releases 2.1.7–2.2.5,
release checklist issues and the release workflows.

| # | Release | What happened | Now covered by |
|---|---------|---------------|----------------|
| A1 | 2.1.8 | Publish stopped by the surfaces gate on Pages drift (13 pages); docs-repo Actions could not open its own sync PR, so the remedy was hand-run. | Surfaces gate kept; §3 makes docs sync a pre-tag step. |
| A2 | 2.1.7/2.1.8 | attributed to the wrong release; notes assembled by date instead of tag ancestry. | `check_release.py` ancestry coverage (§2). |
| A3 | 2.2.0 | Docs-sync defects shipped and were chased after publish. | Surfaces gate + pre-release checklist + §1 sweep. |
| A4 | 2.2.1 | Post-release workflow fetched issue numbers as PRs and 404'd before its checklist issue existed; fixed in #642 by skipping non-PRs. | Guard in workflow; §6 requires verifying the issue exists. |
| A5 | 2.2.5 | CA template parity gaps (e.g. SPOOLMAN_CORS_ORIGIN, VISUAL_WATCH__*) found only by a manual release-night sweep. | Variables/Docker parity tests in CI; §6 CA verify-only step. |
| A6 | 2.2.5 | First 2.2.5 tag failed the lifecycle gate: entrypoint `rm -rf` on the /data/state mount point (EBUSY) broke upgraded installs; no image was published from that tag; the tag was re-created on the fixed commit at catduck's direction and the release later re-cut. Entrypoint unit tests missed it (plain directories, not mount points). | Candidate-before-publish (§4); lifecycle gate is the mount-realistic check; tags never moved without catduck's call. |
| A7 | 2.2.5 | `gh` token lacks packages scope; GHCR checks 403/404 and mimic a missing image. | §6 anonymous GHCR verification. |
| A8 | 2.2.x | Codecov tokenless uploads rejected server-side until the repo is activated on codecov.io. | Report-only Codecov; §6 records activation as catduck's step. |
| A9 | multiple | PR bodies failing the contribution template blocked green PRs; a body edit after the final push leaves branch protection reading a skipped edited-event suite. | Draft bodies to the template first; finish body edits before the final push, or follow with a synchronize push and re-read checks. |
| A10 | 2.2.2 | Spelling check scans only added PR-diff lines, so release notes were never scanned. | `check_release.py` full-notes spelling scan. |
| A11 | #952 | Timing expectations mixed #925's proposed savings with #929's shipped ones; an Orca beta-to-rc change made the comparison uninterpretable. | §1 baselines re-recorded from the release commit's own run, reasons on raised entries. |
| A12 | 2.2.2 | Internal notes missed merged PRs and #655's changes for existing installs had no Upgrade line. | `check_release.py` coverage; §2 Upgrade rule. |
| A13 | 2.2.6 | At the 2.2.6 freeze the internal `Unreleased` section covered only 8 of 25 merged PRs — A12 live again. Found by the 2.2.6 pre-flight sweep; fixed in the bump PR. | `check_release.py` fails closed on exactly this. |
| A14 | 2.2.6 | Timing baselines were last re-recorded for 2.2.5. | §1 baselines step, done for 2.2.6 from main CI run 42346828094. |
| A15 | 2.2.5 | The digest pin lands only after publish and needed its own follow-up PR; forgetting it strands the template on the old digest. | Superseded: templates follow `:latest` and never pin (#1266); §6 keeps only the DOCKER.md digest PR. |
