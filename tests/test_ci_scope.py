"""CI scope (scripts/ci_scope.py, .github/workflows/ci.yml): only a pure release bump skips the heavy jobs.

The 2.3.6 bump PR (#1733) changed five version and notes files and ran the full suite on every push.
These tests pin the allowlist, the version-line-only rule for print_handy/__init__.py, the fail-safe
default, and the workflow wiring that keeps the required check names honest.
"""
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ci_scope  # noqa: E402

BUMP_FILES = ["README.md", "docs/history/INTERNAL_RELEASE_NOTES.md", ".github/release-notes-draft.md",
              "docs/RELEASE-2.3.6.md", "print_handy/__init__.py"]
VERSION_DIFF = ('--- a/print_handy/__init__.py\n+++ b/print_handy/__init__.py\n@@ -1 +1 @@\n'
                '-__version__ = "2.3.5"\n+__version__ = "2.3.6"\n')


def fake_git(files, init_diff=VERSION_DIFF):
    def run(*args):
        if "--name-only" in args:
            return "\n".join(files) + "\n"
        return init_diff
    return run


def test_a_pure_release_bump_is_lightweight():
    full, reason = ci_scope.scope("pull_request", "base", "head", run=fake_git(BUMP_FILES))
    assert full is False and "release bump only" in reason


def test_any_other_file_runs_the_full_suite():
    full, reason = ci_scope.scope("pull_request", "base", "head",
                                  run=fake_git(BUMP_FILES + ["print_handy/core/intake.py"]))
    assert full is True and "print_handy/core/intake.py" in reason
    for path in ("docs/COST.md", "tests/test_release_check.py", "scripts/check_release.py",
                 ".github/workflows/ci.yml", "docs/RELEASE-NOTES-GUIDE.md"):
        assert ci_scope.scope("pull_request", "b", "h", run=fake_git(["README.md", path]))[0] is True, path


def test_init_change_beyond_the_version_line_runs_the_full_suite():
    diff = VERSION_DIFF + "+import os\n"
    assert ci_scope.scope("pull_request", "b", "h", run=fake_git(BUMP_FILES, diff))[0] is True
    assert ci_scope.scope("pull_request", "b", "h", run=fake_git(BUMP_FILES, ""))[0] is True


def test_pushes_dispatches_missing_shas_and_empty_diffs_run_the_full_suite():
    for event in ("push", "workflow_dispatch", ""):
        assert ci_scope.scope(event, "b", "h", run=fake_git(BUMP_FILES))[0] is True
    assert ci_scope.scope("pull_request", "", "h", run=fake_git(BUMP_FILES))[0] is True
    assert ci_scope.scope("pull_request", "b", "h", run=fake_git([]))[0] is True


def test_a_failing_git_call_prints_full_true(monkeypatch, capsys):
    def boom(*args):
        raise RuntimeError("no history")
    monkeypatch.setattr(ci_scope, "git", boom)
    monkeypatch.setenv("EVENT", "pull_request")
    monkeypatch.setenv("BASE_SHA", "b")
    monkeypatch.setenv("HEAD_SHA", "h")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert ci_scope.main() == 0
    out = capsys.readouterr()
    assert out.out.strip() == "full=true" and "scope check failed" in out.err


def ci():
    return yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


HEAVY = ["edit-corpus-offline", "edit-corpus-real", "offline-tests", "bounds-image-stage",
         "runtime-image-build", "native-bounds", "discord-timing", "slicer-timing-real",
         "slicer-test-stable", "slicer-test-bambu", "webui-browser-smoke"]


def test_heavy_jobs_skip_only_on_an_explicit_release_bump():
    jobs = ci()["jobs"]
    for name in HEAVY:
        assert jobs[name]["needs"] == "scope", name
        # !cancelled(): a failed scope job leaves full empty, and the job still runs.
        assert jobs[name]["if"] == "${{ !cancelled() && needs.scope.outputs.full != 'false' }}", name


def test_required_aggregators_accept_skipped_only_for_a_release_bump():
    jobs = ci()["jobs"]
    offline = jobs["offline-tests-result"]
    assert offline["if"] == "always()"
    assert set(offline["needs"]) == {"scope", "offline-tests", "release-bump-tests"}
    run = offline["steps"][0]["run"]
    assert 'needs.offline-tests.result }}" = success' in run
    assert 'needs.scope.outputs.full }}" = false' in run
    assert 'needs.release-bump-tests.result }}" = success' in run
    for name, shard in (("edit-corpus-offline-result", "edit-corpus-offline"),
                        ("slicer-test-stable-result", "slicer-test-stable")):
        run = jobs[name]["steps"][-1]["run"]
        assert set(jobs[name]["needs"]) == {"scope", shard}
        assert f'needs.{shard}.result }}}}" = skipped && test "${{{{ needs.scope.outputs.full }}}}" = false' in run


def test_release_bump_tests_run_only_on_a_bump_and_select_the_docs_tests():
    job = ci()["jobs"]["release-bump-tests"]
    assert job["needs"] == "scope" and job["if"] == "needs.scope.outputs.full == 'false'"
    assert '-k "docs or readme or release or version"' in job["steps"][-1]["run"]
    scope = ci()["jobs"]["scope"]
    assert scope["steps"][0]["with"]["fetch-depth"] == 0
    assert "scripts/ci_scope.py" in scope["steps"][-1]["run"]
