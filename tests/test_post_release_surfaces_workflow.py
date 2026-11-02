"""Contract tests for the post-release surfaces check workflow (#394).

Publishing a release must open exactly one checklist issue — "Post-release
surfaces check: <tag>" — listing the merged PRs in the release range with
surface hints, so docs, the Pages site, the wiki, README/About and the
release notes get an explicit updated / verified-no-change decision per
release. These tests pin that contract: the workflow fires only on a
published release (or a manual dispatch), holds least-privilege
GITHUB_TOKEN permissions, and can never edit code, docs or settings.
The one deliberate secret is DOCS_SYNC_TOKEN, used only by the optional
docs auto-sync dispatch step, which degrades gracefully without it.
"""

import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "post-release-surfaces.yml"


def load_workflow():
    return yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)


def run_block():
    return load_workflow()["jobs"]["surfaces"]["steps"][0]["run"]


class PostReleaseSurfacesWorkflowContract(unittest.TestCase):
    def test_triggers_are_release_published_and_dispatch_only(self):
        workflow = load_workflow()
        self.assertEqual(sorted(workflow["on"]), ["release", "workflow_dispatch"])
        self.assertEqual(workflow["on"]["release"], {"types": ["published"]})
        inputs = workflow["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(sorted(inputs), ["tag"])
        self.assertEqual(inputs["tag"]["type"], "string")
        self.assertEqual(inputs["tag"]["required"], "false")

    def test_permissions_are_least_privilege_github_token_only(self):
        workflow = load_workflow()
        self.assertEqual(
            workflow["permissions"],
            {"contents": "read", "issues": "write", "pull-requests": "read"},
        )
        # No job may widen the top-level grant.
        for job in workflow["jobs"].values():
            self.assertNotIn("permissions", job)
        text = WORKFLOW_PATH.read_text()
        self.assertIn("${{ github.token }}", text)
        # The checklist itself uses only the GITHUB_TOKEN above. The single
        # deliberate exception is DOCS_SYNC_TOKEN, read only by the
        # auto-sync dispatch step; no other secret may appear.
        secrets_used = set(re.findall(r"secrets\.([A-Z_]+)", text))
        self.assertEqual(secrets_used, {"DOCS_SYNC_TOKEN"})

    def test_docs_auto_sync_dispatch_is_optional_and_never_fails(self):
        workflow = load_workflow()
        steps = workflow["jobs"]["surfaces"]["steps"]
        self.assertEqual(len(steps), 2)
        dispatch = steps[1]
        # The dispatch runs after the checklist issue step, so its outcome
        # can be recorded on that issue.
        run = dispatch["run"]
        self.assertEqual(dispatch["env"]["DOCS_SYNC_TOKEN"],
                         "${{ secrets.DOCS_SYNC_TOKEN }}")
        self.assertIn("repos/catduckgnaf/print-handy-docs/dispatches", run)
        self.assertIn("print-handy-release", run)
        # Missing secret: the skip and the manual remedy are recorded.
        self.assertIn("DOCS_SYNC_TOKEN", run)
        self.assertIn("docs-auto-sync.yml", run)
        # Graceful degradation: the step always exits 0.
        self.assertTrue(run.rstrip().endswith("exit 0"))

    def test_single_checklist_issue_per_release(self):
        text = WORKFLOW_PATH.read_text()
        self.assertIn('issue_title="Post-release surfaces check: $tag"', text)
        run = run_block()
        # The open-issue lookup must precede creation, so a re-run updates
        # the existing checklist instead of opening a duplicate.
        self.assertLess(run.index("issue list"), run.index("issue create"))
        self.assertIn("issue comment", run)

    def test_checklist_lookups_search_all_states_by_title(self):
        # P2-153 / P2-263: every lookup in this workflow must search by the
        # exact title across ALL issue states. --state open misses a closed
        # checklist (the natural end state), and an unfiltered --limit 100
        # list misses the checklist whenever more than 100 newer issues
        # exist — either way a re-run opened a duplicate.
        text = WORKFLOW_PATH.read_text()
        lookups = [line for line in text.splitlines() if "gh issue list" in line]
        self.assertGreaterEqual(len(lookups), 4)
        for line in lookups:
            with self.subTest(lookup=line.strip()):
                self.assertIn("--state all", line)
                self.assertNotIn("--state open", line)
        for title_var in ('\\"$issue_title\\"', '\\"$pre_title\\"', '\\"Release checklist: $tag\\"'):
            self.assertIn(f'--search "in:title {title_var}"', text)

    def test_pr_list_comes_from_the_tag_compare(self):
        run = run_block()
        self.assertIn('compare/$prev...$tag', run)
        # A removed docs page is the #355 case: it must be called out as a
        # page removal, not just another drift line.
        self.assertIn('select(.status == "removed")', run)
        self.assertIn("PAGE REMOVAL", run)

    def test_issue_numbers_in_subjects_are_skipped_not_fatal(self):
        run = run_block()
        # A trailing "(#NNN)" in a commit subject can be an issue number
        # carried by a direct-to-main commit, not a PR (in the 2.2.1
        # range, #619-#622 were issues). Its pulls lookup 404s, and under
        # "set -euo pipefail" that must not abort the step before the
        # checklist issue is created. The lookup is guarded and the
        # number is skipped with a log line.
        self.assertIn('if ! gh api "repos/$repo/pulls/$n" > pr.json; then', run)
        self.assertIn(
            "#$n is not a pull request (issue reference or direct commit)"
            " — skipped",
            run,
        )
        self.assertIn("continue", run)
        # Title and labels are read from the guarded fetch, so a bogus
        # number cannot kill the step at a second unguarded pulls lookup.
        self.assertIn("jq -r .title pr.json", run)
        self.assertNotIn('gh api "repos/$repo/pulls/$n" --jq', run)
        # The skip happens before the per-PR files fetch, which only
        # runs for numbers that passed the lookup.
        self.assertLess(
            run.index("is not a pull request"), run.index("pulls/$n/files")
        )

    def test_every_surface_needs_a_decision(self):
        run = run_block()
        for surface in (
            "print-handy/docs",
            "Pages site",
            "Wiki",
            "README + repo About",
            "Release notes",
            "Unraid CA templates",
            "Discord-facing surfaces",
        ):
            self.assertIn(surface, run)
        self.assertIn("verified-no-change", run)
        # The relationship to the daily Docs sync check is stated, so the
        # two checks complement rather than duplicate each other.
        self.assertIn("Docs sync check", run)


class PostReleaseLinksPreReleaseChecklist(unittest.TestCase):
    """#657: when the pre-release checklist already opened a
    "Release checklist: <tag>" issue for the version, the post-release
    check records its verification there instead of opening a competing
    "Post-release surfaces check" issue — one checklist per version."""

    def test_checklist_step_looks_up_the_pre_release_issue(self):
        run = run_block()
        self.assertIn('pre_title="Release checklist: $tag"', run)
        # The pre-release lookup precedes issue creation, and the
        # pre-existing branch comments on that issue rather than
        # creating a new one.
        self.assertLess(
            run.index('pre_title="Release checklist: $tag"'),
            run.index("issue create"),
        )
        self.assertIn('gh issue comment "$pre_existing"', run)
        self.assertIn(
            "Recorded the post-release surfaces check on pre-release "
            "checklist issue",
            run,
        )

    def test_own_issue_still_wins_when_it_exists(self):
        run = run_block()
        # The existing surfaces issue is looked up first; the
        # pre-release lookup only runs when there is none.
        self.assertLess(
            run.index('issue_title="Post-release surfaces check: $tag"'),
            run.index('pre_title="Release checklist: $tag"'),
        )
        self.assertIn('if [ -z "$existing" ]; then', run)
        self.assertIn('gh issue comment "$existing"', run)

    def test_docs_sync_note_falls_back_to_the_pre_release_issue(self):
        workflow = load_workflow()
        run = workflow["jobs"]["surfaces"]["steps"][1]["run"]
        self.assertIn('"Release checklist: $tag"', run)
        self.assertLess(
            run.index('issue_title="Post-release surfaces check: $tag"'),
            run.index('"Release checklist: $tag"'),
        )


if __name__ == "__main__":
    unittest.main()
