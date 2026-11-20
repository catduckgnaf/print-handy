"""Build once (scripts/reuse_candidate.py, docker-publish.yml): a publication reuses an accepted candidate.

2.3.6 built the same image three times. A publication now tags a candidate's pushed digests only
when the candidate ran on this exact commit and version and the registry agrees; anything else
falls back to the ordinary build. These tests pin each refusal and the workflow wiring.
"""
import json
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import reuse_candidate as rc  # noqa: E402

REPO, SHA, VERSION, IMAGE = "o/r", "a" * 40, "2.3.7", "ghcr.io/o/r"
DIGESTS = {"amd64": "sha256:" + "1" * 64, "arm64": "sha256:" + "2" * 64}


class Fake:
    """Answers the gh and docker calls the script makes, from in-memory state."""

    def __init__(self, runs=None, artifacts=None, records=None, labels=None):
        self.runs = runs if runs is not None else [{"id": 11, "head_sha": SHA, "created_at": "2026-11-21T10:00:00Z"}]
        self.artifacts = artifacts if artifacts is not None else ["digest-amd64", "digest-arm64"]
        self.records = records or {arch: {"sha": SHA, "version": VERSION, "platform": rc.ARCHES[arch],
                                          "digest": DIGESTS[arch]} for arch in rc.ARCHES}
        self.labels = labels or {arch: {rc.REVISION: SHA, rc.VERSION: VERSION} for arch in rc.ARCHES}

    def run(self, *cmd):
        if cmd[:2] == ("gh", "api") and "/workflows/" in cmd[2]:
            return json.dumps({"workflow_runs": self.runs})
        if cmd[:2] == ("gh", "api") and cmd[2].endswith("artifacts?per_page=100"):
            return json.dumps({"artifacts": [{"name": n, "expired": False} for n in self.artifacts]})
        if cmd[:4] == ("docker", "buildx", "imagetools", "inspect"):
            arch = next(a for a, d in DIGESTS.items() if cmd[4].endswith(d))
            # An index: the platform image plus its attestation.
            return json.dumps({rc.ARCHES[arch]: {"config": {"Labels": self.labels[arch]}},
                               "unknown/unknown": {}})
        raise AssertionError(cmd)

    def download(self, repo, run_id, name, folder, run):
        arch = name.split("-", 1)[1]
        folder.mkdir(parents=True)
        (folder / f"candidate-{arch}.json").write_text(json.dumps(self.records[arch]))
        (folder / f"digest-{arch}.txt").write_text(self.records[arch]["digest"] + "\n")


def find(fake, this_run="99"):
    return rc.find(REPO, SHA, this_run, VERSION, IMAGE, run=fake.run, download=fake.download)


def test_matching_candidate_is_reused():
    assert find(Fake()) == (DIGESTS, 11)


def test_no_run_this_run_and_runs_without_digests_are_not_reused():
    assert find(Fake(runs=[]))[0] is None
    assert find(Fake(), this_run="11")[0] is None
    digests, reason = find(Fake(artifacts=["digest-amd64"]))
    assert digests is None and "did not push" in reason


def test_a_record_for_another_commit_version_or_platform_is_refused():
    for field, value, words in (("sha", "b" * 40, "commit"), ("version", "2.3.6", "version"),
                                ("platform", "linux/arm64", "platform")):
        fake = Fake()
        fake.records["amd64"] = dict(fake.records["amd64"], **{field: value})
        digests, reason = find(fake)
        assert digests is None and words in reason, field


def test_a_digest_file_that_disagrees_with_its_record_is_refused():
    fake = Fake()
    fake.records["arm64"] = dict(fake.records["arm64"], digest="sha256:short")
    assert find(fake)[0] is None


def test_registry_labels_must_name_this_commit_and_version():
    for labels in ({rc.REVISION: "c" * 40, rc.VERSION: VERSION}, {rc.REVISION: SHA, rc.VERSION: "2.3.6"}, {}):
        fake = Fake()
        fake.labels["arm64"] = labels
        digests, reason = find(fake)
        assert digests is None and "registry labels" in reason


def test_the_newest_good_run_wins_and_a_bad_newer_run_falls_through():
    fake = Fake(runs=[{"id": 11, "head_sha": SHA, "created_at": "2026-11-21T10:00:00Z"},
                      {"id": 12, "head_sha": SHA, "created_at": "2026-11-21T11:00:00Z"}])
    assert find(fake) == (DIGESTS, 12)


def test_any_error_prints_reused_false(monkeypatch, capsys):
    monkeypatch.setattr(rc, "sh", lambda *cmd: (_ for _ in ()).throw(RuntimeError("gh missing")))
    for name, value in (("GITHUB_REPOSITORY", REPO), ("GITHUB_SHA", SHA), ("PUBLISH_VERSION", VERSION),
                        ("IMAGE", IMAGE)):
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert rc.main() == 0
    out = capsys.readouterr()
    assert out.out.strip() == "reused=false" and "lookup failed" in out.err


def publish():
    return yaml.safe_load((ROOT / ".github" / "workflows" / "docker-publish.yml").read_text(encoding="utf-8"))


def test_workflow_reuses_only_after_the_guard_and_surfaces_check_on_a_publication():
    gate = publish()["jobs"]["surfaces-gate"]
    names = [step.get("id") or step.get("name") for step in gate["steps"]]
    reuse = names.index("reuse")
    assert names.index("guard") < reuse
    assert reuse > next(i for i, s in enumerate(gate["steps"]) if "check_release_surfaces" in s.get("run", ""))
    step = gate["steps"][reuse]
    assert step["if"] == "inputs.publish" and "reuse_candidate.py" in step["run"]
    for output in ("reused", "reused_amd64", "reused_arm64", "candidate_run"):
        assert output in gate["outputs"]


def test_build_is_skipped_only_on_reuse_and_the_manifest_tags_the_reused_digests():
    jobs = publish()["jobs"]
    assert jobs["build"]["if"] == "needs.surfaces-gate.outputs.reused != 'true'"
    steps = jobs["manifest"]["steps"]
    assert steps[0]["if"] == "needs.surfaces-gate.outputs.reused != 'true'"
    use = next(s for s in steps if s.get("name") == "Use the reused candidate's digests")
    assert use["if"] == "needs.surfaces-gate.outputs.reused == 'true'"
    assert "digests/digest-amd64.txt" in use["run"] and "digests/digest-arm64.txt" in use["run"]
    push = next(s for s in jobs["build"]["steps"] if s.get("id") == "publish")
    assert "digest-out/candidate-$ARCH.json" in push["run"]
    # The record is written only after the pushed image was proven to be the accepted one.
    assert push["run"].index("evidence/identity-check.txt") < push["run"].index("candidate-$ARCH.json")
    upload = next(s for s in jobs["build"]["steps"] if s.get("name") == "Upload pushed digest for the manifest job")
    assert upload["if"] == "steps.publish.outcome == 'success'"
