"""OpenCreator is in the weekly firmware watch (docs/firmware-baselines.toml).

The firmware feed already announced OpenCreator releases to owners, but the
weekly watch, which opens a maintainer issue when a release is newer than the
audited baseline, had no OpenCreator entry, so a new release never asked for
the Creator 5 contracts to be re-checked.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_firmware_releases.py"

spec = importlib.util.spec_from_file_location("check_firmware_releases", SCRIPT)
watch = importlib.util.module_from_spec(spec)
sys.modules.setdefault(spec.name, watch)
spec.loader.exec_module(watch)


class OpenCreatorIsWatched(unittest.TestCase):
    def setUp(self):
        self.watched = {w.name: w for w in watch.read_baselines(ROOT)}

    def test_opencreator_entry_reads_releases_from_its_repository(self):
        entry = self.watched.get("OpenCreator")
        self.assertIsNotNone(entry, "OpenCreator is missing from docs/firmware-baselines.toml")
        self.assertEqual(entry.repo, "FlashForge-C5-Modding-Group/OpenCreator")
        self.assertEqual(entry.feed, "releases")

    def test_baseline_is_the_audited_release_and_names_its_evidence(self):
        entry = self.watched["OpenCreator"]
        self.assertEqual(entry.baseline, "0.0.7.4")
        self.assertRegex(entry.baseline_source, r"#\d+")

    def test_watch_and_feed_follow_the_same_repository(self):
        feed = (ROOT / "docs" / "firmware-feed.toml").read_text(encoding="utf-8")
        self.assertIn('repo = "FlashForge-C5-Modding-Group/OpenCreator"', feed)
        self.assertEqual(self.watched["OpenCreator"].repo,
                         "FlashForge-C5-Modding-Group/OpenCreator")


    def test_a_newer_release_opens_one_maintainer_notice(self):
        """The watch run with only the shipped OpenCreator entry: a release
        newer than 0.0.7.4 opens exactly one issue naming it."""
        import tempfile

        entry = self.watched["OpenCreator"]
        shipped = (ROOT / "docs" / "firmware-baselines.toml").read_text(encoding="utf-8")
        block = shipped[shipped.index("[[firmware]]\nname = \"OpenCreator\""):]
        calls = []

        class Client(watch.GitHubClient):
            def __init__(self):
                super().__init__(token=None)

            def request(self, method, path, payload=None):
                calls.append((method, path, payload))
                key = (method, path.split("?")[0])
                return {
                    ("GET", "/repos/ghzserg/zmod/releases"): [],
                    ("GET", "/repos/me/repo/issues"): [],
                    ("GET", "/repos/me/repo/labels/firmware-watch"): {"name": "x"},
                    ("POST", "/repos/me/repo/issues"): {"number": 12},
                    ("GET", f"/repos/{entry.repo}/releases"): [
                        {"tag_name": "0.0.7.5", "name": "", "prerelease": False,
                         "draft": False, "published_at": "2026-11-17T00:00:00Z",
                         "html_url": "https://example.invalid/0.0.7.5"},
                        {"tag_name": "0.0.7.4", "name": "", "prerelease": False,
                         "draft": False, "published_at": "2026-11-14T00:00:00Z",
                         "html_url": "https://example.invalid/0.0.7.4"},
                    ],
                }.get(key, [])

        zmod_doc = (ROOT / "docs" / "ZMOD.md").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp) / "docs"
            docs.mkdir()
            (docs / "ZMOD.md").write_text(zmod_doc, encoding="utf-8")
            (docs / "firmware-baselines.toml").write_text(block, encoding="utf-8")
            watch.run(Path(tmp), "me/repo", False, Client())
        writes = [c for c in calls if c[0] != "GET"]
        self.assertEqual(len(writes), 1, writes)
        self.assertEqual(writes[0][2]["title"], "Firmware release: OpenCreator 0.0.7.5")


if __name__ == "__main__":
    unittest.main()
