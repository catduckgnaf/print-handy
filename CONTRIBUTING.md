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
- An issue never authorizes operating a live printer. If a report involves unexpected motion, heating, or an ignored confirmation, say so in the first line so it can be prioritised.
- Labels describe the kind of issue (`bug`, `enhancement`, `documentation`, `question`). Priority (`P0`–`P2`) and planning labels (`backlog`, `MVP`) are applied by maintainers during triage, not by the reporter.
- Duplicates are closed with a link to the canonical issue. Issues closed as not planned get a short reason and, where one exists, a link to the issue that supersedes them. A closed issue can be reopened when new evidence or a changed scope justifies it.

## Prepare a change

- Use a branch for your change and keep commits focused and clearly named.
- Add or update tests for changed behavior. For a bug fix, include a regression test where practical.
- Follow the existing Python and configuration patterns. Do not weaken printer readiness, artifact validation, bed bounds, or confirmation gates to make a test pass.
- For public model fixtures, use only redistributable models and record exact provenance and license in the case manifest. Keep private or non redistributable files outside the repository.
- Update documentation when user visible behavior, setup, configuration, or supported capabilities change. Do not claim support beyond what code and tests establish.

## Run checks

Use Python 3.11 or newer. From the repository root, create an isolated environment, install test dependencies and run the offline suite:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-test.txt
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

## Open a pull request

- Open a pull request against `main` and use the repository pull request template.
- Explain the user impact, verified cause when fixing a bug, the proposed behavior, and relevant risks or limitations.
- Link related issues. Use `Fixes #N` only when the pull request is intended to close that issue after merge.
- Include test results and distinguish automated tests from manual or live checks. Do not include secrets or personal information in examples or logs.
- Keep the PR in draft while implementation or required checks are incomplete. Respond to review feedback with focused updates.

## Documentation and release notes

Use [the documentation home](docs/README.md) for user guidance. Keep the root README focused on purpose, compatibility and the first setup step; put detailed reference material in `docs/`. Check relative links from the file that contains them. Follow [the release-note guide](docs/RELEASE-NOTES.md): user capabilities first, fixes next, implementation details and PR references last. Keep pending changes separate from shipped releases, and preserve the detailed internal history.

Before a non-nightly version is tagged, its pre-release checklist must exist. Dispatch the pre-release checklist workflow (`.github/workflows/pre-release-checklist.yml`, #657) with the target version; it opens (or updates) the single `Release checklist: X.Y.Z` issue from the previous release tag's merged-PR range, combining the document surfaces below with variables verified on every surface, adapter and connection coverage, the registry and docs matrices, Docker/GHCR, both Unraid template repos (this repo's `docker/unraid/` template and `catduck-unraid-templates`) and the live checks the range's PRs still owe. No non-nightly release is tagged until every fix-first item on that issue is resolved or explicitly deferred by catduck on the issue. Nightly builds are exempt: the nightly workflow never opens a checklist and is never gated by one.

Every non-nightly release also re-records the slicing-speed baselines (#734). After the release commit's `ci.yml` run on `main` is green, take the `proposed-baseline.json` from its `discord-timing` and `slicer-timing-real` artifacts, copy them over `tests/corpus/timing_baseline.json` and `tests/corpus/timing_baseline_real_slicer.json`, give every raised entry a `reason`, and merge that as its own reviewed PR before tagging. The checklist issue's "Slicing-speed baselines" section tracks it, and [tests/README.md](tests/README.md) (Slicing-speed gate) has the commands, the thresholds and why they were chosen. The mock-slicer leg is a blocking check; the real-Orca leg reports warnings only until a few releases of data justify promoting it.

When a release is published, the post-release surfaces workflow (`.github/workflows/post-release-surfaces.yml`, #394) opens one `Post-release surfaces check: <tag>` issue listing the release's merged PRs — or, when a `Release checklist: <tag>` issue already exists for the version, records its verification on that issue instead of opening a competing checklist. Every surface in it — these docs, the Pages site (including pages to add or remove), the wiki, the README and repo About text, the release notes and the Unraid CA template sync — needs an explicit updated / verified-no-change decision before the issue closes. It complements the daily Docs sync check in `print-handy-docs`, which only watches file drift between `docs/` and the site copies.

Publication is gated on the two machine-checkable surfaces: the `docker-publish` workflow's surfaces gate (see `docs/DOCKER.md`, "Validation and publication") refuses a `publish=true` run while the README/package version disagree with the release or the Pages site has drifted from `docs/`; candidate runs only warn, and nightly is never gated. The gate's remedy is the Docs auto-sync workflow in `print-handy-docs`, which publishing a release also triggers automatically when the `DOCS_SYNC_TOKEN` secret is configured.

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

Fill the `Documentation impact` fields in the PR template literally. For example:

```text
Documentation: updated
Reason: Added the new setting and its default to docs/CONFIGURATION-YAML.md.
Wiki: not applicable
Wiki reason: Versioned docs are authoritative; no separate wiki page describes this setting.
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
