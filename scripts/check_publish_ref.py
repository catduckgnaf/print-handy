#!/usr/bin/env python3
"""Publish-ref guard for the docker-publish workflow.

A publication run (``publish=true``) pushes ``:VERSION`` and, for the
highest released version, ``:latest``. Both must only ever come from
released code, so a publishing run fails early unless ALL hold:

1. the dispatched ref is a tag, and the tag name is the version;
2. the tagged commit is reachable from ``origin/main``;
3. the ``CI`` workflow (``ci.yml``) concluded successfully on that exact
   commit;
4. the ``Release checklist: X.Y.Z`` issue exists and is closed
   (CONTRIBUTING: no release is tagged without it).

``:latest`` moves only when this version is the highest released version,
where "released" means a plain ``MAJOR.MINOR.PATCH`` tag reachable from
``origin/main``. The decision is written to ``$GITHUB_OUTPUT`` as
``move_latest=true|false``.

Candidate runs (``publish`` is not ``true``) are never blocked and never
move ``:latest``. Nightly is a separate workflow and is not covered here.

The same reachability check is reused by the one-off latest-retag
workflow (``--retag-latest-to VERSION``): it requires the tag to exist, be
reachable from main, and have a published GitHub release that is neither a
draft nor a prerelease (#1141), and nothing else, because the point of that
workflow is to repair ``:latest`` quickly.

Environment: PUBLISH_VERSION, PUBLISH, GITHUB_REF, GITHUB_SHA,
GITHUB_REPOSITORY, GH_TOKEN (read by ``gh``), GITHUB_OUTPUT and
GITHUB_STEP_SUMMARY when set.
"""

import json
import os
import re
import subprocess
import sys

VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
CI_WORKFLOW = "ci.yml"
MAIN_REF = "origin/main"


def version_key(version):
    return tuple(int(part) for part in version.split("."))


def run(args):
    """Run a command, returning (returncode, stdout)."""
    proc = subprocess.run(args, capture_output=True, text=True)
    return proc.returncode, proc.stdout


def is_ancestor_of_main(commit, runner=run):
    """True when ``commit`` is reachable from origin/main."""
    code, _ = runner(["git", "merge-base", "--is-ancestor", commit, MAIN_REF])
    return code == 0


def tag_commit(tag, runner=run):
    code, out = runner(["git", "rev-parse", f"refs/tags/{tag}^{{commit}}"])
    return out.strip() if code == 0 and out.strip() else None


def released_versions(runner=run):
    """Plain X.Y.Z tags whose commit is reachable from origin/main."""
    code, out = runner(["git", "tag", "--list"])
    if code != 0:
        return []
    found = []
    for tag in out.split():
        if not VERSION_RE.fullmatch(tag):
            continue
        commit = tag_commit(tag, runner)
        if commit and is_ancestor_of_main(commit, runner):
            found.append(tag)
    return sorted(found, key=version_key)


def should_move_latest(version, released):
    """``:latest`` moves only for the highest released version."""
    return all(version_key(version) >= version_key(other) for other in released)


def ci_succeeded_on(repo, sha, runner=run):
    """True when the CI workflow has a successful run on exactly ``sha``."""
    code, out = runner([
        "gh", "api", "--paginate",
        f"repos/{repo}/actions/workflows/{CI_WORKFLOW}/runs?head_sha={sha}&per_page=100",
        "--jq", ".workflow_runs[] | .conclusion",
    ])
    if code != 0:
        return False
    return "success" in out.split()


def checklist_issue_closed(repo, version, runner=run):
    """(exists, closed) for the exact ``Release checklist: X.Y.Z`` issue."""
    title = f"Release checklist: {version}"
    code, out = runner([
        "gh", "issue", "list", "--repo", repo, "--state", "all", "--limit", "100",
        "--search", f'in:title "{title}"', "--json", "number,title,state",
    ])
    if code != 0:
        return False, False
    try:
        issues = json.loads(out or "[]")
    except ValueError:
        return False, False
    matches = [issue for issue in issues if issue.get("title") == title]
    if not matches:
        return False, False
    return True, all(issue.get("state", "").upper() == "CLOSED" for issue in matches)


def release_state(repo, version, runner=run):
    """``"published"``, ``"draft"``, ``"prerelease"`` or ``"missing"`` for the
    GitHub release of tag ``version``."""
    code, out = runner([
        "gh", "release", "view", version, "--repo", repo,
        "--json", "isDraft,isPrerelease",
    ])
    if code != 0:
        return "missing"
    try:
        info = json.loads(out or "{}")
    except ValueError:
        return "missing"
    if info.get("isDraft"):
        return "draft"
    if info.get("isPrerelease"):
        return "prerelease"
    return "published"


def publish_problems(version, ref, sha, repo, runner=run):
    """Every reason a publishing run for ``version`` at ``ref`` must fail."""
    problems = []
    if not ref.startswith("refs/tags/"):
        problems.append(
            f"the dispatched ref is {ref or '(empty)'}, not a tag. Publish only from "
            f"the release tag: dispatch the workflow with the tag {version} selected."
        )
        return problems
    tag = ref[len("refs/tags/"):]
    if tag != version:
        problems.append(f"the tag is {tag} but the version being published is {version}.")
    if not is_ancestor_of_main(sha, runner):
        problems.append(
            f"the tagged commit {sha[:12]} is not reachable from main. Release only "
            "code that has been merged."
        )
    if not ci_succeeded_on(repo, sha, runner):
        problems.append(
            f"the CI workflow has no successful run on commit {sha[:12]}. Wait for "
            "CI to pass on that exact commit (or fix it) before publishing."
        )
    exists, closed = checklist_issue_closed(repo, version, runner)
    if not exists:
        problems.append(
            f'no "Release checklist: {version}" issue exists. Dispatch the '
            "pre-release checklist workflow for this version and work it."
        )
    elif not closed:
        problems.append(
            f'the "Release checklist: {version}" issue is still open. Resolve or '
            "explicitly defer every item on it, then close it."
        )
    return problems


def emit(lines, move_latest):
    report = "\n".join(lines)
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as handle:
            handle.write(report + "\n")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a") as handle:
            handle.write(f"move_latest={'true' if move_latest else 'false'}\n")


def check_publish(env, runner=run):
    """Gate for a docker-publish run. Returns the process exit code."""
    version = env.get("PUBLISH_VERSION", "")
    if not VERSION_RE.fullmatch(version):
        print(f"PUBLISH_VERSION must use MAJOR.MINOR.PATCH form, got {version!r}")
        return 2
    if env.get("PUBLISH", "false") != "true":
        emit([
            f"## Publish ref guard: {version} [candidate]",
            "",
            "Candidate run: nothing is pushed, so any ref is allowed.",
        ], False)
        return 0
    ref = env.get("GITHUB_REF", "")
    sha = env.get("GITHUB_SHA", "")
    repo = env.get("GITHUB_REPOSITORY", "")
    problems = publish_problems(version, ref, sha, repo, runner)
    if problems:
        emit(
            [f"## Publish ref guard: {version} [FAILED]", ""]
            + [f"- {problem}" for problem in problems],
            False,
        )
        print("Publish ref guard: FAILING this publish run before anything is built.",
              file=sys.stderr)
        return 1
    released = [v for v in released_versions(runner) if v != version]
    move_latest = should_move_latest(version, released)
    note = (
        f"`:latest` will move to {version}."
        if move_latest
        else f"{version} is not the highest released version "
        f"({released[-1]} is), so `:latest` stays where it is."
    )
    emit([f"## Publish ref guard: {version} [passed]", "", note], move_latest)
    return 0


def check_retag(env, runner=run):
    """Gate for the one-off latest-retag workflow."""
    version = env.get("RETAG_VERSION", "")
    if not VERSION_RE.fullmatch(version):
        print(f"RETAG_VERSION must use MAJOR.MINOR.PATCH form, got {version!r}")
        return 2
    commit = tag_commit(version, runner)
    if commit is None:
        print(f"No tag {version} exists in this repository.")
        return 1
    if not is_ancestor_of_main(commit, runner):
        print(f"Tag {version} ({commit[:12]}) is not reachable from main; refusing to "
              "point :latest at it.")
        return 1
    repo = env.get("GITHUB_REPOSITORY", "")
    state = release_state(repo, version, runner)
    if state != "published":
        reason = {
            "missing": "has no GitHub release",
            "draft": "only has a draft GitHub release",
            "prerelease": "is marked as a prerelease on GitHub",
        }[state]
        print(f"Tag {version} {reason}; :latest only points at a published release. "
              "Publish the release first, or pick a version that was released.")
        return 1
    print(f"Tag {version} ({commit[:12]}) is reachable from main and has a published release.")
    return 0


def main(argv):
    if len(argv) > 1 and argv[1] == "--retag-latest-to":
        return check_retag({
            "RETAG_VERSION": argv[2] if len(argv) > 2 else "",
            "GITHUB_REPOSITORY": os.environ.get("GITHUB_REPOSITORY", ""),
        })
    return check_publish(os.environ)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
