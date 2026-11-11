# Contributing to Print Handy

Thanks for helping improve Print Handy. This guide describes the repository workflow and project specific checks. The [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md) provides the expected pull request description structure.

## Before starting

- Search open issues and pull requests for related work. For larger changes, discuss the approach in an issue first so scope and safety expectations are clear.
- Read the relevant project documentation and code. The README describes supported printers and workflows; configuration details are in `docs/CONFIGURATION-YAML.md`.
- Keep changes focused. Separate unrelated fixes and avoid including machine specific settings, credentials, private printer data, or personal model files.
- Printer control is safety sensitive. Keep offline analysis and tests separate from live printer operations. A code change or successful test does not authorize contacting or operating a printer.

## Filing an issue

- Use the [issue forms](.github/ISSUE_TEMPLATE). Bug reports require the observed behaviour, the steps that reproduce it, the Print Handy version and install method, and evidence. Feature requests require the problem, the proposed behaviour, scope, and acceptance criteria. Research issues require the question and the decision the answer informs. The forms mark which fields are required.
- State measured facts, and label estimates and suspicions as such. Do not present a cause as verified when it is a hypothesis; say what evidence would settle it.
- Never include credentials, tokens, private model files, or personal runtime records in an issue. Redact logs before posting.
- Everything you write here is public, and the edit history keeps old versions. Do not include local file paths, machine or host names, private IP addresses, MAC addresses, email addresses, SSH targets, chat or account IDs, or anything else that says where you keep things. Say "the host", "the container" or "the printer" instead, and use paths inside the container (for example `/data/...`). The Public text policy workflow checks issues, pull requests, comments and commit messages, labels an offending issue `needs-sanitizing`, and fails a pull request. It never repeats the matched value.
- Attach the sanitized diagnostics bundle to every bug report: run `/print-handy bundle` in Discord (or `python3 -m print_handy.diagnostics_bundle`), review the file, and upload it. The Issue policy workflow labels a bug report `needs-info` until the bundle (or a sanitization report) and the evidence are present.
- An issue never authorizes operating a live printer. If a report involves unexpected motion, heating, or an ignored confirmation, say so in the first line so it can be prioritised.
- Labels describe the kind of issue (`bug`, `enhancement`, `documentation`, `question`). Priority (`P0`–`P2`) and planning labels (`backlog`, `MVP`) are applied by maintainers during triage, not by the reporter.
- Duplicates are closed with a link to the canonical issue. Issues closed as not planned get a short reason and, where one exists, a link to the issue that supersedes them. A closed issue can be reopened when new evidence or a changed scope justifies it.

## Prepare a change

- Use a branch for your change and keep commits focused and clearly named.
- Add or update tests for changed behavior. For a bug fix, include a regression test where practical. Every pull request answers for this in its `Tests added and regression scoping` section: which tests were added, why none were added when that is the case, and how the change was scoped against the full suite and the existing regression tests.
- Follow the existing Python and configuration patterns. Do not weaken printer readiness, artifact validation, bed bounds, or confirmation gates to make a test pass.
- For public model fixtures, use only redistributable models and record exact provenance and license in the case manifest. Keep private or non redistributable files outside the repository.
- Update documentation when user visible behavior, setup, configuration, or supported capabilities change. Do not claim support beyond what code and tests establish.

## Run checks

Use Python 3.12 or newer. From the repository root, create an isolated environment, install test dependencies and run the offline suite:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[test]"
python -m pytest -q tests
```

Relevant focused checks can be run individually. For example:

```bash
python3 -m pytest -q tests/corpus
python3 -m pytest -q tests/slicer
```

Real slicer checks require a compatible OrcaSlicer installation and are enabled with `PRINT_HANDY_REAL_SLICER=1`. They invoke the slicer locally; they are not printer tests. The Discord flow corpus uses fake interactions and does not contact a printer. Check the CI workflow for the current Python versions and additional integration jobs.

Report which checks you actually ran and their result in the pull request. Clearly identify checks not run and why. Do not describe a pending CI job as passing.

### US spelling in prose

Prose in this repository uses American spellings for the color/honor family: color, honor, behavior, not the British forms. The `spelling` CI job runs `scripts/check_us_spelling.py`, which scans only the lines a pull request adds to Markdown files, plus the PR description, and fails with the American replacement for each British form it finds. Text a change does not touch is never scanned, and code identifiers are not prose. The word list lives in one place, `BRITISH_TO_US` in the script. Run it locally before pushing:

```bash
python3 scripts/check_us_spelling.py --base origin/main
```

A line that must keep a British spelling (quoted upstream text, an exact identifier) carries an HTML comment naming the allowed word: `<!-- us-spelling: allow colour -->` allows that word on that line, and a bare `<!-- us-spelling: allow -->` allows every listed word on the line. <!-- us-spelling: allow -->

## Adding roast lines

The one-line roasts come from a local corpus, never from a model (#1002). There are two ways to add lines, and both are validated by the same gates.

### Contribute lines to the shipped corpus

Roast lines live in `print_handy/data/roasts/`, one JSON file per bank:

- `generic.json` and `one_liners.json` — lines for any completed print (one-liners must need no facts about the print at all).
- `overnight.json`, `long_print.json`, `heavy_filament.json`, `repeat_print.json`, `short_print.json` — context banks, drawn only when the fact they talk about is known for that print.
- `failed.json`, `cancelled.json`, `spaghetti.json` — status banks, drawn only for prints that ended that way. Keep these gentle: a costly failure is not the moment for the sharpest material, and success language never belongs in them.
- `title_reactions.json` — a map from a title keyword (for example `benchy`) to its lines; every keyword keeps at least three.
- `color_roasts.json`, `reprint_failed.json`, `history_callbacks.json` — keyed banks for color families, prints that follow earlier failures, and callbacks to earlier prints.
- `fragments.json` — openers, comparisons, verdicts and compliments the engine can frame a drawn line with. `dictionary.json` defines 3D printing terms (and the title keyword some of them route to). `blocklist.json` lists words no roast may contain.
- `dark_humor.json` — an optional bank of edgier generic lines.

A new entry is an object with three fields:

```json
{"id": "generic-a-unique-stable-name", "family": "generic-a-unique-stable-name", "text": "A line about {title}."}
```

- `id` is stable and unique across the whole corpus. It is stored, not derived from the text, so editing a line keeps its repeat history — pick a descriptive id and never reuse one.
- `family` names the cooldown group: a line and its variants share a family so they do not cluster in consecutive draws. A line with no variants is its own family.
- `text` is the template. It may use only these slots, filled from the print's own facts: `{title}`, `{duration}`, `{material}`, `{color}`, `{grams}`, `{printer}`, `{copies}`, `{print_number}`, `{times}`, `{hour}`, `{last_title}`, `{prev_fails}`, `{fail_streak}`, `{color_streak}`, `{top_color}`. A template that needs a fact a print does not have is simply never drawn for that print.

Every line, once filled against a fully populated print, must meet the roast contract: one sentence, at most 20 words, no quotes, no hashtags, nothing from the blocklist. The target is always the print, the choice, or the printer — never the operator. Write original lines; do not copy joke lists or kits.

CI runs the corpus gates in `tests/test_roast_corpus.py` on every pull request, and they cover a contributed entry automatically — the gates iterate the bank files and the loaded corpus, they are not a fixed list of lines. The gates check: valid JSON with provenance fields (`_about`, `_phase`, `_license`) in each bank file, bank minimum sizes, no exact duplicates and no near-duplicates (difflib ratio over 0.88) anywhere across the banks, known slots only, every entry filling cleanly and meeting the contract, the blocklist, and stable ids and families. Run them locally before pushing (the near-duplicate gate compares every pair of lines and takes a while):

```bash
python -m pytest -q tests/test_roast_corpus.py tests/core/test_roast_engine.py
```

Adding entries to an existing bank needs no code. A brand-new bank file is gated for format and ids by the same tests, but drawing from it is an engine change in `print_handy/core/roast.py` — discuss that in an issue first.

### Add house roasts on your own install, no pull request needed

An operator can keep private house roasts without forking: point the `vision.roast_custom_bank` setting (environment form `PRINT_HANDY__VISION__ROAST_CUSTOM_BANK`) at a local JSON file and its valid entries are drawn alongside the shipped corpus on completed prints:

```json
{"entries": [
  {"id": "custom-workshop-motto", "family": "custom-house", "text": "The workshop gremlin union has certified {title} as officially finished."},
  "House rule number nine: {title} gets a bow before it gets a shelf."
]}
```

The file is a JSON object with an `entries` list (a bare list works too); each entry is an object as above or a bare string. At load time the engine runs the same gates as the shipped corpus — schema, known slots, the filled contract, the blocklist, and no exact or near duplicates against the shipped lines or each other. A refused entry is skipped with the problem named in the log (the entry id and the reason), the valid entries still load, and a missing or unreadable file simply means no custom roasts: roasts continue from the shipped corpus and no print post is ever blocked. `bot check` names the first custom-bank problem as well.

## Open a pull request

- Open a pull request against `main` and use the repository pull request template.
- Explain the user impact, verified cause when fixing a bug, the proposed behavior, and relevant risks or limitations.
- Link related issues. Use `Fixes #N` only when the pull request is intended to close that issue after merge.
- Include test results and distinguish automated tests from manual or live checks. Do not include secrets or personal information in examples or logs.
- Keep the PR in draft while implementation or required checks are incomplete. Respond to review feedback with focused updates.
- Merging runs on the required checks, not on branch freshness (policy changed 2026-11-07, #1017 batch 2). `main`'s required checks (offline tests on both Python versions, spoolman, the stable slicer test and `discord-timing`) do **not** require the branch to be up to date, and the repository has auto-merge on: enable auto-merge on your PR and GitHub merges it once those checks pass on the PR's own head. The up-to-date rule was tried and removed the same day because every merge to `main` forced a fresh full CI run on every open PR. The risk it guarded against is real — #999 was green but had been tested before #988 renamed a function it used — so when your change touches code that has moved on `main` since you branched, update the branch and let CI run on the new head before merging anyway.

## Documentation and release notes

Use [the documentation home](docs/README.md) for user guidance. Keep the root README focused on purpose, compatibility and the first setup step; put detailed reference material in `docs/`. Check relative links from the file that contains them. Follow [the release-note guide](docs/RELEASE-NOTES-GUIDE.md): user capabilities first, fixes next, implementation details and PR references last. Keep pending changes separate from shipped releases, and preserve the detailed internal history.

The full release procedure lives in [the release checklist](docs/history/RELEASE-CHECKLIST.md), including the automated Release check workflow and the audit of past release catches and misses. Before a non-nightly version is tagged, its pre-release checklist must exist. Dispatch the pre-release checklist workflow (`.github/workflows/pre-release-checklist.yml`, #657) with the target version; it opens (or updates) the single `Release checklist: X.Y.Z` issue from the previous release tag's merged-PR range, combining the document surfaces below with variables verified on every surface, adapter and connection coverage, the registry and docs matrices, Docker/GHCR, both Unraid template repos (this repo's `docker/unraid/` template and `catduck-unraid-templates`) and the live checks the range's PRs still owe. No non-nightly release is tagged until every fix-first item on that issue is resolved or explicitly deferred by catduck on the issue. That rule is a maintainer step: nothing stops `git tag`. What automation does enforce is publication: the `docker-publish` workflow refuses a `publish=true` run unless the `Release checklist: X.Y.Z` issue exists and is closed (see "What automation enforces" below). Nightly builds are exempt: the nightly workflow never opens a checklist and is never gated by one.

Every non-nightly release also re-records the slicing-speed baselines (#734). After the release commit's `ci.yml` run on `main` is green, take the `proposed-baseline.json` from its `discord-timing` and `slicer-timing-real` artifacts, copy them over `tests/corpus/timing_baseline.json` and `tests/corpus/timing_baseline_real_slicer.json`, give every raised entry a fresh `reason` (the proposal leaves it empty on purpose, and the gate rejects an empty reason or the previous raise's reason copied over), and merge that as its own reviewed PR before tagging. The checklist issue's "Slicing-speed baselines" section tracks it, and [tests/README.md](tests/README.md) (Slicing-speed gate) has the commands, the thresholds and why they were chosen. The mock-slicer leg (`discord-timing`) fails its own job when a case is slower than its limit, and it is a required status check, so a slowdown blocks the merge. The gate allows `max(0.35 s, 10%)` of slack on top of +10%, limits the total drift from each case's first recorded time (a warning past +25%, a failure past +50%), and refuses a raise without a new reason; the real-Orca leg reports warnings only until a few releases of data justify promoting it.

When a release is published, the post-release surfaces workflow (`.github/workflows/post-release-surfaces.yml`, #394) opens one `Post-release surfaces check: <tag>` issue listing the release's merged PRs — or, when a `Release checklist: <tag>` issue already exists for the version, records its verification on that issue instead of opening a competing checklist. Every surface in it — these docs, the Pages site (including pages to add or remove), the wiki, the README and repo About text, the release notes and the Unraid CA template sync — needs an explicit updated / verified-no-change decision before the issue closes. It complements the daily Docs sync check in `print-handy-docs`, which only watches file drift between `docs/` and the site copies.

Publication is gated on the two machine-checkable surfaces: the `docker-publish` workflow's surfaces gate (see `docs/DOCKER.md`, "Validation and publication") refuses a `publish=true` run while the README/package version disagree with the release or the Pages site has drifted from `docs/`; candidate runs only warn, and nightly is never gated. The same job also runs the publish ref guard (see below). The surfaces gate's remedy is the Docs auto-sync workflow in `print-handy-docs`, which publishing a release also triggers automatically when the `DOCS_SYNC_TOKEN` secret is configured.

### What automation enforces, and what is a maintainer step

Enforced by automation:

- **Merge:** only the status checks listed in branch protection. Today those are `offline-tests (3.12)`, `offline-tests (3.14)`, `spoolman` and `Slicer test (stable)`. Every other CI job below runs and reports on a PR, but a red result does not stop a merge.
- **Publishing an image:** a `docker-publish` run with `publish=true` fails before anything is built unless the dispatched ref is the release tag, the tagged commit is reachable from `main`, the `CI` workflow concluded successfully on that exact commit, and the `Release checklist: X.Y.Z` issue exists and is closed. It also fails while the README, package version or Pages site is stale. `:latest` moves only for the highest released version. Candidate runs (`publish=false`) work from any ref. `scripts/check_publish_ref.py` holds the logic and `tests/test_publish_ref_guard.py` tests it.
- **Repairing `:latest`:** the manual `retag-latest.yml` workflow (see `docs/DOCKER.md`) copies an existing version tag to `:latest` by digest, and refuses a version whose tag is not on `main` or that has no published (non-draft, non-prerelease) GitHub release.
- **Timing:** the `discord-timing` job fails on a slowdown, an empty or copied raise reason, or cumulative drift (as described above), and it is required.

Maintainer steps (nothing in automation stops them): creating the `Release checklist: X.Y.Z` issue and working through it before tagging; creating the tag; re-recording and reviewing the timing baselines; reading the `Slicer timing (real Orca, reporting only)` warnings; and the install, upgrade and rollback checks the checklist lists.

Checks branch protection requires on `main` (the owner sets these; see the paragraph on merging above): `offline-tests (3.12)`, `offline-tests (3.14)`, `spoolman`, `Slicer test (stable)` and `discord-timing`, the mock-slicer timing gate.

Checks the maintainers should mark required next (this repository's settings are the owner's to change; this list is the recommendation):

- `static` (Docker static checks: shell lint and Compose rendering) and `State volume (real bind mount)`, the two Docker jobs that run without a Docker build.
- `Web UI browser smoke (Chromium)`.
- `contribution-policy` (pull request description and tests policy).
- `public-text` (no private paths, hosts or names in public text).

Until they are required, a red result on them does not stop auto-merge (branch protection requires only the five checks above and no review), so look at them before enabling auto-merge.

## Automated PR review support

The **Contribution checks / contribution-policy** check runs when a PR opens,
changes, is reopened or has its description edited, including fork PRs. It checks:

1. A written summary and actual verification evidence (or explicit unrun checks).
2. `Documentation: updated` with a changed documentation file, or `not needed`
   with a specific reason. User-facing features, changed defaults, settings,
   installation steps and supported integrations should update their relevant
   guide in the same PR. Maintainers review exemptions; a checkbox cannot prove
   documentation quality.
3. `Wiki: updated`, `not applicable` or `deferred`, with a reason. An update
   needs a wiki page URL; a deferral needs a GitHub issue URL. The versioned
   [documentation home](docs/README.md) is authoritative. Do not duplicate
   entire guides into a separate wiki. If a wiki entry exists, check its links
   and summary against the feature; otherwise say why it is not applicable.
4. Local Markdown file destinations in changed Markdown files. External URLs,
   heading anchors and semantic accuracy remain human review responsibilities.
5. `Tests added: added` with the new or updated tests named in the detail and
   at least one test file in the PR, or `none` with a specific reason no new
   test is appropriate; plus `Regression scoping: checked` (or
   `not applicable`) with a reason stating how the change was scoped against
   the full suite and the existing regression tests, what was left out, and
   why. Running a focused test is verification, recorded above; this section
   is about what the PR leaves in the suite and whether broader regression
   coverage was considered, not only the tests that were touched.

Fill the `Documentation impact` fields in the PR template literally. For example:

```text
Documentation: updated
Reason: Added the new setting and its default to docs/CONFIGURATION-YAML.md.
Wiki: not applicable
Wiki reason: Versioned docs are authoritative; no separate wiki page describes this setting.
```

Fill the `Tests added and regression scoping` fields literally too. For example:

```text
Tests added: added
Tests added detail: Added test_option_default in tests/test_settings.py, pinning the new option's default value.
Regression scoping: checked
Regression scoping reason: The settings and defaults regression tests were considered; the focused run covers them and CI runs the full suite on this head.
```

Existing PRs can add this section when revising their descriptions. The check
is read-only: no auto-merge, auto-close, code execution with a write token,
contributor comments, labels, wiki writes or publication. Existing owner
assignment automation remains separate. Branch protection currently requires
other CI checks; this new check does not silently change repository protection.
Maintainers must review its result before merge until it is made required.

For local reproduction, save JSON containing `body` (PR description) and `files`
(changed repository-relative paths), then run:

```bash
python scripts/check_contribution.py --metadata /path/to/pr.json
python -B -m unittest discover -s tests -p test_contribution_policy.py
```

### Review checklist

- Confirm scope and related work before implementation; open a draft PR early.
- Check user-facing guides, configuration examples and wiki impact for each feature.
- Verify regression coverage and exact-head CI, without treating offline tests as hardware evidence.
- Check compatibility, rollback, safety guards and third-party attribution.
- Keep pending work and release publication separate from merged source.
