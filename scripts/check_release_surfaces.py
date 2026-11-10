#!/usr/bin/env python3
"""Release surfaces gate for the docker-publish workflow.

A version release may only be published when the surfaces that describe
Print Handy match what is being released:

- the Pages site (catduckgnaf/print-handy-docs) has no drift against
  ``docs/`` at the release ref — drift is defined exactly the way the
  docs repo's own Docs sync check defines it (its ``scripts/check-sync.sh``
  and ``scripts/sync-manifest.tsv``, including REMOVED pages), and
- the README and package version agree with the version being published,
  and the README's relative documentation links (and their heading anchors) resolve inside the repo
  (Pages-site routes it links to are checked against the docs checkout).

The gate runs on every ``docker-publish`` dispatch. In candidate mode
(``publish=false``) stale surfaces are reported as warnings only. With
``publish=true`` the gate exits non-zero when anything is stale, and the
``build`` job — the first job that can push an image — needs this job, so
a stale release cannot be published until the remedy in the report has
been run. Nightly builds use ``.github/workflows/nightly.yml``, a
separate workflow this gate is never part of, so nightly is never gated.

Environment:
  PUBLISH_VERSION  release version, MAJOR.MINOR.PATCH (required)
  PUBLISH          "true" for a publication dispatch, anything else is a
                   candidate (default "false")
  DOCS_DIR         path to a catduckgnaf/print-handy-docs checkout
                   (required for the drift check)
  GITHUB_OUTPUT / GITHUB_STEP_SUMMARY are honoured when set.

Exit code: 1 only when something is stale AND PUBLISH is "true".
"""

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE_PREFIX = "https://catduckgnaf.github.io/print-handy-docs/"
DRIFT_PREFIXES = ("CHANGED:", "ADDED:", "REMOVED:", "MISSING-SITE-COPY:")

REMEDY = """\
Remedy:
- Pages drift: run the Docs auto-sync for this release, then merge the
  sync PR it opens in catduckgnaf/print-handy-docs:
    gh workflow run docs-auto-sync.yml --repo catduckgnaf/print-handy-docs -f ref={version}
  (Publishing the GitHub release also sends this sync a
  repository_dispatch automatically when the DOCS_SYNC_TOKEN secret is
  configured on this repo; the manual dispatch above always works.)
  Once the sync PR is merged and the site matches, re-dispatch this
  publish.
- README / version problems: fix them in the release PR (the version in
  print_handy/__init__.py, the README's current-version references and
  its links), merge, re-tag and re-dispatch. Never move a published tag.
"""


def heading_anchors(path):
    """GitHub-style anchor slugs for the headings of one Markdown file."""
    text = re.sub(r"```.*?```", "", path.read_text(encoding="utf-8"), flags=re.S)
    seen, anchors = {}, set()
    for match in re.finditer(r"^#{1,6}\s+(.*?)\s*#*$", text, flags=re.M):
        title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", match.group(1))
        slug = re.sub(r"[^\w\- ]", "", title.replace("`", "").strip().lower())
        slug = slug.replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


def readme_problems(root, docs_dir, version):
    """In-repo consistency checks for the README and the package version."""
    problems = []
    init_text = (root / "print_handy" / "__init__.py").read_text()
    match = re.search(r'__version__\s*=\s*"([^"]+)"', init_text)
    if match is None:
        problems.append("print_handy/__init__.py: no __version__ found")
    elif match.group(1) != version:
        problems.append(
            f"print_handy/__init__.py: __version__ is {match.group(1)}, "
            f"not the version being published ({version})"
        )
    readme_path = root / "README.md"
    readme = readme_path.read_text()
    if version not in readme:
        problems.append(
            f"README.md: does not mention the version being published ({version})"
        )
    for target in re.findall(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", readme):
        if target.startswith("#"):
            continue
        if target.startswith(SITE_PREFIX):
            route = target[len(SITE_PREFIX):].split("#")[0].strip("/")
            if route and docs_dir is not None:
                candidates = [
                    docs_dir / "docs" / f"{route}.md",
                    docs_dir / "docs" / route / "index.md",
                ]
                if not any(candidate.exists() for candidate in candidates):
                    problems.append(
                        f"README.md: Pages site link {target} has no page "
                        f"in print-handy-docs (expected docs/{route}.md)"
                    )
            continue
        if re.match(r"^[a-z][a-z0-9+.-]*:", target):
            continue  # external URL or mailto: not checked offline
        path, _, anchor = target.partition("#")
        if path and not (root / path).exists():
            problems.append(f"README.md: link target does not exist: {target}")
        elif path.endswith(".md") and anchor and anchor.lower() not in heading_anchors(root / path):
            problems.append(f"README.md: link anchor does not exist: {target}")
    return problems


def drift_problems(root, docs_dir):
    """Pages drift per the docs repo's own Docs sync check definition."""
    if docs_dir is None:
        return ["Pages drift: DOCS_DIR is not set, so drift was not checked"]
    check_sync = docs_dir / "scripts" / "check-sync.sh"
    if not check_sync.is_file():
        return [f"Pages drift: {check_sync} not found in the docs checkout"]
    env = dict(os.environ, PRINT_HANDY_DIR=str(root))
    proc = subprocess.run(
        ["bash", str(check_sync)],
        env=env,
        capture_output=True,
        text=True,
    )
    output = proc.stdout + proc.stderr
    drift_lines = [
        line for line in output.splitlines() if line.startswith(DRIFT_PREFIXES)
    ]
    if proc.returncode == 0:
        return []
    if proc.returncode == 1:
        return [f"Pages drift: {line}" for line in drift_lines] or [
            "Pages drift: the Docs sync check reported drift"
        ]
    return [
        "Pages drift: the Docs sync check itself failed "
        f"(exit {proc.returncode}); its output was:\n{output.strip()}"
    ]


def main():
    version = os.environ.get("PUBLISH_VERSION", "")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        print(f"PUBLISH_VERSION must use MAJOR.MINOR.PATCH form, got {version!r}")
        return 2
    publish = os.environ.get("PUBLISH", "false") == "true"
    docs_dir_env = os.environ.get("DOCS_DIR", "")
    docs_dir = Path(docs_dir_env) if docs_dir_env else None

    problems = readme_problems(ROOT, docs_dir, version)
    problems += drift_problems(ROOT, docs_dir)
    stale = bool(problems)

    mode = "PUBLISH" if publish else "candidate (warnings only)"
    lines = [f"## Release surfaces gate — {version} [{mode}]", ""]
    if stale:
        lines.append("Stale release surfaces:")
        lines += [f"- {problem}" for problem in problems]
        lines.append("")
        lines.append(REMEDY.format(version=version))
    else:
        lines.append(
            "All release surfaces are current: the README and package "
            "version match the release, its links resolve, and the Pages "
            "site has no drift against docs/ at this ref."
        )
    report = "\n".join(lines)
    print(report)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as handle:
            handle.write(report + "\n")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a") as handle:
            handle.write(f"stale={'true' if stale else 'false'}\n")

    if stale and publish:
        print(
            "Surfaces gate: FAILING this publish run — stale surfaces "
            "listed above. (Candidate runs only warn.)",
            file=sys.stderr,
        )
        return 1
    if stale:
        print("Surfaces gate: stale surfaces reported as warnings (candidate).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
