"""Offline contract for the README quickstart check (tests/docker/readme_quickstart.py, #1156).

The hosted job runs the README's "Get started" blocks verbatim on a clean runner and asks the bot
for `/print-help` through the Web UI chat. These tests keep the pieces it depends on honest without
Docker: the README still has the block shape the job extracts, the single download substitution
covers every URL, the `.env` keys the README names exist in `.env.example`, the help markers match
the bot's real help text, and the workflow stays hosted, read-only and wired to the script.
"""
import importlib.util
import re
import unittest
from pathlib import Path

import yaml

from print_handy.bot.commands import help_text

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("readme_quickstart", ROOT / "tests/docker/readme_quickstart.py")
quickstart = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(quickstart)


class ReadmeQuickstartExtraction(unittest.TestCase):
    def setUp(self):
        self.readme = (ROOT / "README.md").read_text(encoding="utf-8")

    def test_readme_has_a_setup_block_and_a_start_block(self):
        setup, start, section = quickstart.quickstart_blocks(self.readme)
        self.assertIn("curl", setup)
        self.assertIn(".env", setup)
        self.assertRegex(start, r"(?m)^docker compose up -d\b")
        self.assertIn("/print-help", section)

    def test_the_one_substitution_covers_every_download(self):
        setup, start, _ = quickstart.quickstart_blocks(self.readme)
        localized, count = quickstart.localize_downloads(setup, ROOT)
        self.assertGreaterEqual(count, 1)
        self.assertNotIn("raw.githubusercontent.com", localized + start)
        for target in re.findall(r"file://\S+", localized):
            path = Path(target[len("file://"):])
            self.assertTrue(path.is_file(), f"{target} does not exist in the checkout")

    def test_substitution_changes_nothing_but_the_download_base(self):
        setup, _, _ = quickstart.quickstart_blocks(self.readme)
        localized, _ = quickstart.localize_downloads(setup, ROOT)
        base = "file://" + ROOT.resolve().as_posix().rstrip("/") + "/"
        undo = localized.replace(base, "https://raw.githubusercontent.com/catduckgnaf/print-handy/TAG/")
        self.assertEqual(quickstart.RAW_BASE.sub("X/", undo), quickstart.RAW_BASE.sub("X/", setup))

    def test_env_example_has_every_key_the_readme_says_to_fill_in(self):
        env = (ROOT / ".env.example").read_text(encoding="utf-8")
        for key in quickstart.ENV_KEYS:
            self.assertRegex(env, rf"(?m)^#?[ \t]*{key}=", key)
        edited = quickstart.set_env_value(env, "PRINT_HANDY_IMAGE", "print-handy:ci")
        # Compose reads the last live assignment, so that one must carry the value.
        live = re.findall(r"(?m)^PRINT_HANDY_IMAGE=(.*)$", edited)
        self.assertEqual(live[-1:], ["print-handy:ci"], live)

    def test_the_live_line_wins_over_a_commented_example_above_it(self):
        text = "# KEY=example\nKEY=\n"
        self.assertEqual(quickstart.set_env_value(text, "KEY", "v"), "# KEY=example\nKEY=v\n")
        self.assertEqual(quickstart.set_env_value("# KEY=example\n", "KEY", "v"), "KEY=v\n")

    def test_webui_wait_does_not_hinge_on_the_bot_heartbeat_or_spend_the_redirect(self):
        # CI has no platform token, so the bot never starts and /healthz (the
        # bot heartbeat verdict) answers 503 for good; the wait must still end
        # once the Web UI answers HTTP at all. It must never request /: the
        # first browser navigation there is the one-time walkthrough redirect
        # that ask_for_help asserts.
        asked = []

        def fake_request(method, path, body=None, headers=None):
            asked.append(path)
            return (503, "", "") if path == "/healthz" else (303, "/walkthrough", "")

        original = quickstart._request
        quickstart._request = fake_request
        try:
            self.assertEqual(quickstart._wait_webui(timeout=5), 503)
        finally:
            quickstart._request = original
        self.assertEqual(asked, ["/healthz"])

    def test_missing_env_key_fails_by_name(self):
        with self.assertRaisesRegex(AssertionError, "NOT_THERE"):
            quickstart.set_env_value("A=1\n", "NOT_THERE", "x")

    def test_help_markers_are_the_bots_real_help_text(self):
        text = help_text()
        for marker in quickstart.HELP_MARKERS:
            self.assertIn(marker, text)

    def test_a_readme_without_the_start_block_is_refused(self):
        broken = "## Get started\n\n```bash\ncurl -fLO x\n```\n\nsend /print-help\n\n## Next\n"
        with self.assertRaises(AssertionError):
            quickstart.quickstart_blocks(broken)


class ReadmeQuickstartWorkflow(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.safe_load((ROOT / ".github/workflows/compose-clean-start.yml").read_text())

    def test_job_is_hosted_read_only_and_runs_the_script(self):
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        job = self.workflow["jobs"]["readme-quickstart"]
        self.assertTrue(job["runs-on"].startswith("ubuntu-"))
        run = "\n".join(step.get("run", "") for step in job["steps"])
        self.assertIn("docker buildx build --load", run)
        self.assertIn("tests/docker/readme_quickstart.py", run)
        self.assertNotIn("push", run)
        self.assertNotIn("secrets.", (ROOT / ".github/workflows/compose-clean-start.yml").read_text())

    def test_readme_changes_trigger_the_job(self):
        paths = self.workflow[True]["pull_request"]["paths"] if True in self.workflow \
            else self.workflow["on"]["pull_request"]["paths"]
        self.assertIn("README.md", paths)
        self.assertIn("tests/docker/**", paths)


if __name__ == "__main__":
    unittest.main()
