#!/usr/bin/env python3
"""Pre-release checklist generator (#657, Stage 2).

Builds the single "Release checklist: X.Y.Z" issue body for a version
that has NOT been tagged yet, from the merged PRs in the range between
the previous release tag and HEAD. The companion workflow
(.github/workflows/pre-release-checklist.yml) collects that range with
the GitHub API and feeds it to this script as JSON; the script itself
does no network or repository access, so the generation is fully
testable offline (tests/test_pre_release_checklist.py).

The post-release surfaces check (#394) files its checklist AFTER a
release ships and covers document surfaces only. This checklist is the
before-tag half: the same seven document/GitHub surfaces with the same
per-PR hints, plus the surfaces that produced post-release surprises
when nobody compared them beforehand — settings variables across their
surfaces (the 2.2.1 Web UI audit: fields offered in the UI and consumed
by nothing, settings with no UI field at all), adapter/connection
coverage, the registry and docs matrices, Docker/GHCR, both Unraid
template repos, and the live checks the range's PRs say are still owed.

Every item is a checkbox. Fix-first items must be resolved — or
explicitly deferred by catduck on the issue — before the version is
tagged; CONTRIBUTING.md states that rule. Nightly builds are exempt:
nightly.yml never runs this generator and the workflow is
manual-dispatch only.

Input JSON (see the workflow for how it is assembled):

  {"version": "2.2.2", "from_ref": "2.2.1", "to_ref": "<sha>",
   "repo": "catduckgnaf/print-handy",
   "prs": [{"number": 655, "title": "...", "labels": ["enhancement"],
            "body": "...",
            "files": [{"filename": "print_handy/settings.py",
                       "status": "modified"}]}]}

Usage:
  build_release_checklist.py --input range.json --output body.md \
      --title-output title.txt

Exit code: 2 on malformed input (bad version, missing keys).
"""

import argparse
import json
import re
import sys

VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")

# ---------------------------------------------------------------------------
# Per-PR classification. A PR can belong to several classes; each class
# feeds one checklist section below.
# ---------------------------------------------------------------------------


def surface_hints(files):
    """Document/GitHub surface hints, mirroring post-release-surfaces.yml."""
    hints = []
    for entry in files:
        name = entry.get("filename", "")
        if name.startswith("docs/") and name.endswith(".md"):
            hint = "docs guide"
        elif name == "README.md":
            hint = "README"
        elif name.startswith("docker/unraid/") or name == "docker-compose.yml":
            hint = "Unraid CA template (release-time sync: verify)"
        elif name.startswith("scripts/install"):
            hint = "install surfaces"
        elif name.startswith("print_handy/bot/commands"):
            hint = "bot commands / Discord-facing docs"
        elif name.startswith(".github/"):
            hint = "GitHub surfaces"
        else:
            continue
        if hint not in hints:
            hints.append(hint)
    return hints


def removed_docs_pages(files):
    return [
        entry.get("filename", "")
        for entry in files
        if entry.get("status") == "removed"
        and entry.get("filename", "").startswith("docs/")
    ]


def _names(files):
    return [entry.get("filename", "") for entry in files]


def settings_paths(files):
    """Paths that add or change a configuration variable on some surface:
    the defaults tree and loader, the Web UI catalog, the env example."""
    matched = []
    for name in _names(files):
        if (
            name == "print_handy/settings.py"
            or name == "print_handy/webui/catalog.py"
            or name == ".env.example"
            or "catalog" in name
            or "/defaults" in name
            or name.startswith("print_handy/defaults")
        ):
            matched.append(name)
    return matched


def adapter_paths(files):
    """Paths that change printer adapter or connection behavior."""
    matched = []
    for name in _names(files):
        base = name.rsplit("/", 1)[-1]
        if (
            name.startswith("print_handy/printers/")
            or name.startswith("print_handy/core/connection")
            or name.startswith("print_handy/hermes/moonraker")
            or "adapter" in base
            or "moonraker" in name
            or "octoprint" in name
        ):
            matched.append(name)
    return matched


def registry_paths(files):
    """Paths behind the registry and docs matrices."""
    matched = []
    for name in _names(files):
        if (
            name.startswith("printers/registry")
            or name.startswith("firmware/commands")
            or name.startswith("print_handy/knowledge/")
            or name
            in ("docs/MACROS.md", "docs/PRINTER-SUPPORT.md", "docs/PRINTER-PROFILES.md")
        ):
            matched.append(name)
    return matched


def docker_paths(files):
    matched = []
    for name in _names(files):
        if (
            name.startswith("docker/")
            or name == "docker-compose.yml"
            or name.rsplit("/", 1)[-1].startswith("Dockerfile")
            or name.startswith(".github/workflows/docker-")
        ):
            matched.append(name)
    return matched


def unraid_paths(files):
    return [
        name
        for name in _names(files)
        if name.startswith("docker/unraid/") or name == "docker-compose.yml"
    ]


# ---------------------------------------------------------------------------
# Owed live checks, collected from PR bodies.
# ---------------------------------------------------------------------------

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_OWED_RES = (
    re.compile(
        r"\b(?:live|hardware)\b[^.\n]{0,100}?\b(?:owed|unverified|"
        r"not (?:yet )?(?:verified|run|tested|checked)|not run|"
        r"pending|deferred|outstanding)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:owed|outstanding|deferred)\b[^.\n]{0,100}?\b(?:live|hardware)\b",
        re.IGNORECASE,
    ),
)


def owed_live_checks(pr):
    """Lines in a PR body that name a live/hardware check still owed.

    HTML comments (the template's guidance text) are stripped first so
    boilerplate cannot match. These are candidates: the checklist asks
    for each one to be confirmed, done, or deferred by catduck.
    """
    body = _COMMENT_RE.sub("", pr.get("body") or "")
    found = []
    for line in body.splitlines():
        line = line.strip(" -\t")
        if not line or len(line) < 8:
            continue
        if any(pattern.search(line) for pattern in _OWED_RES):
            found.append(line[:240])
    return found


# ---------------------------------------------------------------------------
# Body assembly.
# ---------------------------------------------------------------------------


def _pr_bullets(prs, paths_fn, empty_text):
    lines = []
    for pr in prs:
        paths = paths_fn(pr.get("files", []))
        if paths:
            shown = ", ".join(f"`{path}`" for path in paths[:6])
            if len(paths) > 6:
                shown += f", and {len(paths) - 6} more"
            lines.append(f"- #{pr['number']} {pr.get('title', '')} — {shown}")
    if not lines:
        lines.append(empty_text)
    return lines


def build_checklist(data):
    """Return (title, body) for the range described by *data*."""
    version = data["version"]
    from_ref = data.get("from_ref", "")
    to_ref = data.get("to_ref", "HEAD")
    prs = sorted(data.get("prs", []), key=lambda pr: pr["number"])
    title = f"Release checklist: {version}"

    lines = []
    lines.append(f"## Release checklist: {version}")
    lines.append("")
    lines.append(
        f"Target version: **{version}** — not yet tagged. "
        f"Range: `{from_ref}...{to_ref}` ({len(prs)} merged PRs). "
        "Opened by the Pre-release checklist workflow "
        "(`.github/workflows/pre-release-checklist.yml`, manual dispatch) "
        "before tagging; see #657 for why this check exists and "
        "CONTRIBUTING.md for the release rule. It is the before-tag half "
        "of the post-release surfaces check (#394): when this version is "
        "published, that workflow records its verification on this issue "
        "instead of opening a competing one."
    )
    lines.append("")
    lines.append(
        "**The rule:** no non-nightly release is tagged until this issue "
        "exists and every **fix-first** item below is resolved or "
        "explicitly deferred by catduck in a comment on this issue. An "
        "item with no decision recorded is not done. Nightly builds are "
        "exempt by name: the nightly workflow never opens this checklist "
        "and this workflow is manual-dispatch only."
    )
    lines.append("")
    lines.append("## Fix-first vs defer")
    lines.append("")
    lines.append(
        "- **Fix-first** (resolve before tagging, or defer explicitly "
        "here): variables without a verdict on every surface, registry "
        "or docs-matrix drift, Docker/GHCR mismatches, template "
        "mismatches, slicing-speed baselines not re-recorded, and "
        "document-surface gaps."
    )
    lines.append(
        "- **Defer** (only by catduck's explicit call on this issue): "
        "the owed live checks below, and any fix-first item catduck "
        "chooses to defer with the reason recorded here."
    )
    lines.append("")

    # -- Merged PRs with the post-release hint table. ----------------------
    lines.append("## Merged PRs in this range")
    lines.append("")
    lines.append("| PR | Title | Labels | Surface hints |")
    lines.append("|---|---|---|---|")
    removed_pages = []
    if prs:
        for pr in prs:
            hints = surface_hints(pr.get("files", []))
            gone = removed_docs_pages(pr.get("files", []))
            if gone:
                hints.append("PAGE REMOVAL: " + ", ".join(gone))
                removed_pages.extend(gone)
            labels = ", ".join(pr.get("labels") or []) or "—"
            pr_title = (pr.get("title") or "").replace("|", " ")
            lines.append(
                f"| #{pr['number']} | {pr_title} | {labels} | "
                f"{'; '.join(hints) if hints else '—'} |"
            )
    else:
        lines.append(
            "| — | No squash-merged PR numbers found in the range's "
            "commit subjects; check the release notes' PR list by hand. "
            "| — | — |"
        )
    if removed_pages:
        lines.append("")
        lines.append(
            "**Page removals in this range:** "
            + ", ".join(sorted(set(removed_pages)))
            + " — the corresponding Pages site page, its nav/sidebar "
            "entry and every inbound link must be removed (the #355 "
            "`-light` case: the site kept serving a guide after it was "
            "deleted upstream)."
        )
    lines.append("")

    # -- (a) Document and GitHub surfaces. ---------------------------------
    lines.append("## Document and GitHub surfaces")
    lines.append("")
    lines.append(
        "Every surface below needs exactly one decision recorded — "
        "**updated** or **verified-no-change** — before tagging."
    )
    lines.append("")
    for item in (
        "**print-handy/docs** (source of truth): guides for anything "
        "this release changes are updated in the release PRs or a "
        "follow-up — decision: updated / verified-no-change",
        "**Pages site** (print-handy-docs): drifted pages re-synced; "
        "pages **added or removed** as the PR list requires (removals "
        "include nav/sidebar and inbound links); live URLs "
        "spot-checked — decision: updated / verified-no-change",
        "**Wiki** (Home, _Sidebar, setup pages): links and summaries "
        "still point at the right guides; removed guides unlinked — "
        "decision: updated / verified-no-change",
        "**README + repo About** (description, topics): still accurate "
        "for what ships — decision: updated / verified-no-change",
        f"**Release notes** for {version} (drafted, not yet published): "
        "match the PRs above (user capabilities first, no shipped item "
        "missing, no unshipped item claimed) — decision: updated / "
        "verified-no-change",
        "**Discord-facing surfaces** where relevant (bot help/command "
        "docs, setup instructions): match shipped behavior — decision: "
        "updated / verified-no-change / not relevant",
    ):
        lines.append(f"- [ ] {item}")
    lines.append("")

    # -- (b) Variables. -----------------------------------------------------
    lines.append("## Variables in this range (fix-first)")
    lines.append("")
    lines.append(
        "PRs in this range whose diff touches a settings, catalog or "
        "defaults path:"
    )
    lines.append("")
    lines.extend(
        _pr_bullets(
            prs,
            settings_paths,
            "No PR in this range touches a settings, catalog or "
            "defaults path; variable verdicts are not owed for this "
            "release.",
        )
    )
    lines.append("")
    lines.append(
        "- [ ] Every variable those PRs add or change has its "
        "per-surface verdicts recorded (in the PR's Surfaces and "
        "variables section, #657 Stage 1): defaults tree; environment "
        "loader (`OPTIONAL_KEYS`) and `.env.example`; Web UI catalog — "
        "a field, or a recorded no-UI verdict with reason; the "
        "consuming code (module/adapter named); docs "
        "(`docs/SETTINGS.md`); Unraid templates. A variable missing a "
        "verdict on any surface is fixed before tagging or deferred "
        "by catduck here — a field offered in the Web UI and consumed "
        "by nothing is the 2.2.1 failure this section exists to catch."
    )
    lines.append(
        "- [ ] The variables cross-surface contract test passes on the "
        "release candidate (no new uncovered variable, no stale "
        "exemption)."
    )
    lines.append("")

    # -- (c) Adapters and connections. --------------------------------------
    lines.append("## Adapters and connections in this range (fix-first)")
    lines.append("")
    lines.append("PRs in this range touching printer adapter or connection code:")
    lines.append("")
    lines.extend(
        _pr_bullets(
            prs,
            adapter_paths,
            "No PR in this range touches printer adapter or connection "
            "code.",
        )
    )
    lines.append("")
    lines.append(
        "- [ ] Every behavior change in those PRs was checked against "
        "every adapter/connection in its scope (the printer-scope "
        "rule: a change for one printer family is checked for all of "
        "them, OctoPrint-connected printers included), and any "
        "adapter it does not apply to is named with the reason."
    )
    lines.append("")

    # -- (d) Registry and docs matrices. ------------------------------------
    lines.append("## Registry and docs matrices (fix-first)")
    lines.append("")
    lines.append("PRs in this range touching the registries or their matrices:")
    lines.append("")
    lines.extend(
        _pr_bullets(
            prs,
            registry_paths,
            "No PR in this range touches the printer/firmware "
            "registries, the knowledge base or their docs matrices.",
        )
    )
    lines.append("")
    lines.append(
        "- [ ] `docs/MACROS.md` matches the firmware command registry "
        "(every Moonraker family has its coverage or its recorded "
        "no-add verdict)."
    )
    lines.append(
        "- [ ] `docs/PRINTER-SUPPORT.md`, `docs/PRINTER-PROFILES.md` "
        "and the model lists (README, Web UI printer picker, Unraid "
        "template) match `printers/registry/`; the registry drift and "
        "parity tests pass on the release candidate."
    )
    lines.append("")

    # -- (e) Docker and GHCR. -----------------------------------------------
    lines.append("## Docker and GHCR (fix-first)")
    lines.append("")
    lines.append("PRs in this range touching the image or its workflows:")
    lines.append("")
    lines.extend(
        _pr_bullets(
            prs,
            docker_paths,
            "No PR in this range touches `docker/`, a Dockerfile, "
            "`docker-compose.yml` or the Docker workflows.",
        )
    )
    lines.append("")
    lines.append(
        f"- [ ] A candidate `docker-publish` run (publish=false) for "
        f"{version} is green; the surfaces gate reports the README, "
        f"package version and Pages site current."
    )
    lines.append(
        f"- [ ] GHCR plan verified: `:{version}` and `:latest` will "
        "point at the release build (multi-arch), and no other tag "
        "moves."
    )
    lines.append("")

    # -- (f) Unraid templates, both repos. ----------------------------------
    lines.append("## Unraid templates (fix-first)")
    lines.append("")
    lines.append("PRs in this range touching the in-repo template or compose file:")
    lines.append("")
    lines.extend(
        _pr_bullets(
            prs,
            unraid_paths,
            "No PR in this range touches `docker/unraid/` or "
            "`docker-compose.yml`.",
        )
    )
    lines.append("")
    lines.append(
        "- [ ] In-repo template (`docker/unraid/print-handy.xml`) "
        "matches the shipped variables and defaults for this release "
        "— decision: updated / verified-no-change."
    )
    lines.append(
        "- [ ] **catduck-unraid-templates** repo: its Print Handy "
        "template matches the in-repo one — decision: updated / "
        "verified-no-change. (Standing permission applies there; a "
        "mismatch found here is still fixed before tagging, not after.)"
    )
    lines.append("")

    # -- (g) Owed live checks. ----------------------------------------------
    lines.append("## Owed live checks")
    lines.append("")
    owed = []
    for pr in prs:
        for line in owed_live_checks(pr):
            owed.append(f"- #{pr['number']}: {line}")
    if owed:
        lines.append(
            "Lines collected from this range's PR bodies that name a "
            "live or hardware check still owed. Confirm each one: it "
            "is either done and recorded, or explicitly deferred by "
            "catduck on this issue before tagging."
        )
        lines.append("")
        lines.extend(owed)
    else:
        lines.append(
            "No PR body in this range names an owed live check. "
            "Record any operator-owed live checks here before tagging "
            "(first live print on a new path, on-hardware verification, "
            "and similar), each as its own line."
        )
    lines.append("")
    lines.append(
        "- [ ] Every owed live check above is done and recorded, or "
        "explicitly deferred by catduck on this issue."
    )
    lines.append("")

    # -- (h) Slicing-speed baselines (#734). --------------------------------
    lines.append("## Slicing-speed baselines (fix-first)")
    lines.append("")
    lines.append(
        "Each release becomes the new normal for the slicing-speed gate "
        "(#734, `tests/README.md`, Slicing-speed gate): the gate only asks whether a change "
        "made slicing slower than the last release. Record the baselines "
        "from the CI run of the commit being released, before tagging."
    )
    lines.append("")
    lines.append(
        f"- [ ] The `ci.yml` run on the {version} release commit is green. "
        "Its `discord-timing` artifact (mock slicer, required gate) and "
        "`slicer-timing-real` artifact (real Orca, reporting only) each "
        "hold `proposed-baseline.json`, recorded with `--recorded-from` "
        "naming that run."
    )
    lines.append(
        "- [ ] `tests/corpus/timing_baseline.json` and "
        "`tests/corpus/timing_baseline_real_slicer.json` are replaced by "
        "those proposals in a reviewed PR. Every entry that rose keeps "
        "`raised_from` and gets a `reason` (what got slower and why that "
        "is acceptable); the gate refuses a raise without one. Tolerance "
        "and slack are not changed in that PR. Decision: re-recorded / "
        "verified-no-change."
    )
    lines.append(
        "- [ ] The real-slicer leg's warnings since the last release were "
        "reviewed, and its promotion to a required gate is decided "
        "(promote / keep reporting-only, with the measured spread). "
        "First release after #734: this whole section is the step's first "
        "real run; record the outcome on #734."
    )
    lines.append("")

    # -- (i) The gate. --------------------------------------------------------
    lines.append("## Fix-first gate")
    lines.append("")
    lines.append(
        "- [ ] Every fix-first item above is resolved or explicitly "
        "deferred by catduck on this issue."
    )
    lines.append(
        f"- [ ] The version bump agrees everywhere it appears "
        f"(`print_handy/__init__.py`, README, docs) with {version}, "
        f"and the tag `{version}` is created only after this gate "
        "passes."
    )
    lines.append(
        "- [ ] After tagging and publishing, the post-release "
        "surfaces check records its verification on this issue "
        "(it links here instead of opening a competing checklist)."
    )
    lines.append("")
    return title, "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", required=True, help="range JSON file")
    parser.add_argument("--output", required=True, help="issue body output file")
    parser.add_argument(
        "--title-output", help="optional file to write the issue title to"
    )
    args = parser.parse_args(argv)

    try:
        data = json.loads(open(args.input, encoding="utf-8").read())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"build_release_checklist: cannot read input: {exc}", file=sys.stderr)
        return 2
    version = data.get("version", "")
    if not VERSION_RE.fullmatch(version or ""):
        print(
            "build_release_checklist: version must use MAJOR.MINOR.PATCH "
            f"form, got {version!r}",
            file=sys.stderr,
        )
        return 2
    if not data.get("from_ref"):
        print(
            "build_release_checklist: from_ref is required (the previous "
            "release tag or an explicit start ref)",
            file=sys.stderr,
        )
        return 2

    title, body = build_checklist(data)
    with open(args.output, "w", encoding="utf-8") as handle:
        handle.write(body + "\n")
    if args.title_output:
        with open(args.title_output, "w", encoding="utf-8") as handle:
            handle.write(title + "\n")
    print(title)
    return 0


if __name__ == "__main__":
    sys.exit(main())
