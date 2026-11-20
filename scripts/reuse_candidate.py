#!/usr/bin/env python3
"""Find an accepted candidate image for this exact commit and version, so publication need not rebuild it.

2.3.6 built the same image three times: two no-push candidates on the bump branch and the
publication build on the tag, about 12 minutes each. A candidate run (publish=false) on main or on a
tag now pushes its accepted per-arch images by digest, untagged, and uploads the digest-<arch>
artifacts with a candidate-<arch>.json record. A publish=true run on the same commit calls this
script from the surfaces-gate job, after the publish ref guard and the surfaces check passed. When a
successful docker-publish run on this commit left both records, and the registry confirms each
digest is the image the record names, the build job is skipped and the manifest job tags those
digests: the image that ships is the one acceptance and the lifecycle gate already ran against.

Every check must hold or nothing is reused:
- the run is a successful docker-publish run on this exact head SHA, and not this run;
- both digest-amd64 and digest-arm64 artifacts exist and are not expired;
- each candidate-<arch>.json names this SHA, this version and that arch's platform, and its digest
  equals digest-<arch>.txt and has the sha256 form;
- in the registry, the image at that digest carries this SHA and version in its OCI labels for
  that platform.

Prints ``reused=true`` plus ``amd64=``, ``arm64=`` and ``candidate_run=`` lines for $GITHUB_OUTPUT,
or ``reused=false``. Any error is ``reused=false``: the fallback is the ordinary build, which runs
acceptance and the lifecycle gate again, so a failure here costs time, never safety. stdlib only.

    GITHUB_REPOSITORY=... GITHUB_SHA=... GITHUB_RUN_ID=... PUBLISH_VERSION=X.Y.Z IMAGE=ghcr.io/... \
        python3 scripts/reuse_candidate.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ARCHES = {"amd64": "linux/amd64", "arm64": "linux/arm64"}
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
REVISION = "org.opencontainers.image.revision"
VERSION = "org.opencontainers.image.version"


def sh(*cmd):
    return subprocess.run(list(cmd), capture_output=True, text=True, check=True).stdout


def candidate_runs(repo, sha, this_run, run=sh):
    """Successful docker-publish runs on ``sha``, newest first, excluding this run."""
    data = json.loads(run("gh", "api",
                          f"repos/{repo}/actions/workflows/docker-publish.yml/runs"
                          f"?head_sha={sha}&status=success&per_page=30"))
    runs = [r for r in data.get("workflow_runs", [])
            if str(r.get("id")) != str(this_run) and r.get("head_sha") == sha]
    return sorted(runs, key=lambda r: r.get("created_at", ""), reverse=True)


def has_digest_artifacts(repo, run_id, run=sh):
    data = json.loads(run("gh", "api", f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100"))
    live = {a["name"] for a in data.get("artifacts", []) if not a.get("expired")}
    return all(f"digest-{arch}" in live for arch in ARCHES)


def read_record(folder, arch, sha, version):
    """The digest a candidate artifact records for ``arch``, or a reason it cannot be trusted."""
    record = json.loads((folder / f"candidate-{arch}.json").read_text(encoding="utf-8"))
    digest = (folder / f"digest-{arch}.txt").read_text(encoding="utf-8").strip()
    if record.get("sha") != sha:
        return None, f"{arch}: record is for commit {record.get('sha')}, not {sha}"
    if record.get("version") != version:
        return None, f"{arch}: record is for version {record.get('version')}, not {version}"
    if record.get("platform") != ARCHES[arch]:
        return None, f"{arch}: record is for platform {record.get('platform')}"
    if not DIGEST_RE.fullmatch(digest) or record.get("digest") != digest:
        return None, f"{arch}: digest file and record disagree or are malformed"
    return digest, None


def registry_labels(image, digest, platform, run=sh):
    """OCI labels of the image at ``image@digest`` for ``platform``."""
    doc = json.loads(run("docker", "buildx", "imagetools", "inspect", f"{image}@{digest}",
                         "--format", "{{json .Image}}"))
    if "config" not in doc:  # an index (the image plus its attestation): one entry per platform
        doc = doc.get(platform) or {}
    return (doc.get("config") or {}).get("Labels") or {}


def find(repo, sha, this_run, version, image, run=sh, download=None):
    """(digests by arch, run id) of a reusable candidate, or (None, reason)."""
    reasons = []
    for candidate in candidate_runs(repo, sha, this_run, run):
        run_id = candidate["id"]
        if not has_digest_artifacts(repo, run_id, run):
            reasons.append(f"run {run_id}: no digest artifacts (a candidate that did not push)")
            continue
        with tempfile.TemporaryDirectory() as tmp:
            digests, problem = {}, None
            for arch, platform in ARCHES.items():
                folder = Path(tmp) / arch
                (download or _download)(repo, run_id, f"digest-{arch}", folder, run)
                digest, problem = read_record(folder, arch, sha, version)
                if problem:
                    break
                labels = registry_labels(image, digest, platform, run)
                if labels.get(REVISION) != sha or labels.get(VERSION) != version:
                    problem = (f"{arch}: registry labels at {digest} are revision {labels.get(REVISION)}, "
                               f"version {labels.get(VERSION)}")
                    break
                digests[arch] = digest
            if problem is None:
                return digests, run_id
            reasons.append(f"run {run_id}: {problem}")
    return None, "; ".join(reasons) or "no successful docker-publish run on this commit"


def _download(repo, run_id, name, folder, run=sh):
    run("gh", "run", "download", str(run_id), "-R", repo, "-n", name, "-D", str(folder))


def main():
    env = os.environ
    try:
        digests, detail = find(env["GITHUB_REPOSITORY"], env["GITHUB_SHA"], env.get("GITHUB_RUN_ID", ""),
                               env["PUBLISH_VERSION"], env["IMAGE"])
    except Exception as exc:  # noqa: BLE001 - any failure falls back to the ordinary build
        digests, detail = None, f"lookup failed ({type(exc).__name__}: {exc})"
    if digests:
        print("reused=true")
        for arch, digest in digests.items():
            print(f"{arch}={digest}")
        print(f"candidate_run={detail}")
        message = f"Reusing the accepted candidate images from run {detail}: no rebuild."
    else:
        print("reused=false")
        message = f"No reusable candidate ({detail}); building and accepting the image in this run."
    print(message, file=sys.stderr)
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"### Candidate reuse\n\n{message}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
