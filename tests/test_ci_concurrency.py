"""Contract test for the CI workflow's concurrency groups (.github/workflows/ci.yml).

A pull request keeps one run, and a newer push to it cancels the run in progress.
Pushes to main share one group without cancel-in-progress, so the running main run
always finishes and a burst of merges keeps only the newest queued run. A dispatch
gets a group of its own, so a CI run dispatched on a release tag for the publish
ref guard is never cancelled or replaced.
"""

import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
CI_PATH = ROOT / ".github" / "workflows" / "ci.yml"


def load_ci():
    return yaml.load(CI_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


class CiConcurrencyContract(unittest.TestCase):
    def test_groups_per_event(self):
        concurrency = load_ci()["concurrency"]
        group = concurrency["group"]
        self.assertIn("format('pr-{0}', github.event.pull_request.number)", group)
        self.assertIn("github.event_name == 'push' && 'main-push'", group)
        self.assertIn("format('run-{0}', github.run_id)", group)
        # Only pull requests cancel a run in progress; a main run always finishes.
        self.assertEqual(concurrency["cancel-in-progress"],
                         "${{ github.event_name == 'pull_request' }}")

    def test_push_runs_only_on_main(self):
        # The shared 'main-push' group is safe only while push runs are main-only.
        self.assertEqual(load_ci()["on"]["push"]["branches"], ["main"])


if __name__ == "__main__":
    unittest.main()
