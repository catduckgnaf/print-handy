import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
RESOLVER = ROOT / "scripts" / "resolve_release_range.sh"


class ReleaseRangeResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Release Range Test"], cwd=self.repo, check=True)
        self.commit("initial")
        self.tag("2.2.6")
        self.commit("release changes")
        self.tag("2.2.7")
        self.commit("unreleased changes")
        main = self.git("rev-parse", "HEAD")
        subprocess.run(["git", "checkout", "-qb", "stray"], cwd=self.repo, check=True)
        self.commit("stray tag target")
        self.tag("2.2.8")
        subprocess.run(["git", "checkout", "-q", "main"], cwd=self.repo, check=True)
        self.git("reset", "--hard", main)
        self.commit("post release change")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, text=True).strip()

    def commit(self, text):
        (self.repo / "history.txt").write_text(text + "\n", encoding="utf-8")
        subprocess.run(["git", "add", "history.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", text], cwd=self.repo, check=True)

    def tag(self, name):
        subprocess.run(["git", "tag", name], cwd=self.repo, check=True)

    def resolve(self, version, from_ref="", end="HEAD"):
        output = subprocess.check_output([str(RESOLVER), version, from_ref, end], cwd=self.repo, text=True)
        return dict(line.split("=", 1) for line in output.splitlines())

    def test_unrelated_newer_tag_is_ignored_and_normal_range_uses_ancestor(self):
        result = self.resolve("2.2.7", "2.2.7")
        self.assertEqual(result, {"from_ref": "2.2.6", "to_ref": "2.2.7^"})

    def test_post_release_range_ends_before_current_release(self):
        result = self.resolve("2.2.7", "2.2.7")
        self.assertEqual(result, {"from_ref": "2.2.6", "to_ref": "2.2.7^"})

    def test_bump_range_uses_prior_ancestor(self):
        result = self.resolve("2.2.8", "", "2.2.7^")
        self.assertEqual(result, {"from_ref": "2.2.6", "to_ref": "2.2.7^"})

    def test_explicit_range_is_preserved(self):
        result = self.resolve("2.2.8", "2.2.6")
        self.assertEqual(result, {"from_ref": "2.2.6", "to_ref": "HEAD"})


if __name__ == "__main__":
    unittest.main()
