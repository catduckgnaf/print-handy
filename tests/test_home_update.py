"""scripts/home-update.sh against throwaway git repos: a good update sticks, a bad one rolls back."""
import os
import shutil
import site
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from print_handy.settings import LEGACY_ENV

ROOT = Path(__file__).resolve().parents[1]

# What the throwaway checkout's update check runs: the package imports and its settings load.
SMOKE_TEST = (
    "import unittest\n"
    "class Smoke(unittest.TestCase):\n"
    "    def test_package_imports_and_settings_load(self):\n"
    "        from print_handy import settings\n"
    "        self.assertTrue(settings.load)\n"
)


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


@unittest.skipIf(os.environ.get("PRINT_HANDY_UPDATE_CHECK"), "already inside an update check")
@unittest.skipUnless(shutil.which("git"), "git is not installed")
class HomeUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.upstream, self.home = base / "upstream", base / "home"
        files = git(ROOT, "ls-files", "-co", "--exclude-standard").splitlines()
        for name in files:
            # The update script runs `pytest tests` inside the throwaway checkout. Copying this repository's own
            # suite would run all of it twice per update (about 200 s each), so the checkout gets a small test
            # tree instead. The script's pass, rollback and doctor paths are exercised the same way.
            if name.startswith("tests/") or name.startswith("docs/"):
                continue
            if (ROOT / name).is_file():
                (self.upstream / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, self.upstream / name)
        (self.upstream / "tests").mkdir()
        (self.upstream / "tests" / "test_smoke.py").write_text(SMOKE_TEST)
        git(self.upstream, "init", "-q", "-b", "main")
        git(self.upstream, "add", "-A")
        git(self.upstream, "commit", "-qm", "base")
        git(base, "clone", "-q", str(self.upstream), str(self.home))
        # `true --help` exits 0: a stand-in Orca so the doctor passes on machines without flatpak.
        # A profile with a hardware record, so the doctor's checks match a working install.
        profile = base / "profile"
        (profile / "state").mkdir(parents=True)
        (profile / "state" / "printer_hardware.json").write_text(
            '{"spool": {"material": "PLA", "density": 1.24}, "temps_c": {"nozzle": 210, "bed": 60}, '
            '"nozzle": {"diameter_mm": 0.4}}')
        # The install this fixture models is a Hermes install, and a real one
        # states its mode explicitly (the shipped templates always did) — so
        # the fixture does too; since #524 an unset mode means standalone.
        (self.home / "config" / "local.toml").write_text(
            f'[printer]\nname = "Home AD5X"\n\n[orca]\ncommand = ["true"]\n\n[paths]\nhermes_home = "{profile}"\n\n[bot]\nmode = "hermes"\n')

    def tearDown(self):
        self.tmp.cleanup()

    def commit_upstream(self, path, text):
        (self.upstream / path).write_text(text)
        git(self.upstream, "add", "-A")
        git(self.upstream, "commit", "-qm", f"change {path}")
        return git(self.upstream, "rev-parse", "HEAD")

    def update(self, home=None):
        # Environment overrides beat local.toml, so a caller's HERMES_HOME (or any other mapped
        # variable) would point the doctor at the real install instead of the throwaway profile.
        env = {k: v for k, v in os.environ.items() if k not in LEGACY_ENV}
        # Run the update's checks with this interpreter. With HOME moved, the script cannot find
        # the agent venv and would fall back to whatever python3 is first on PATH.
        env.setdefault("PYTHON", sys.executable)
        if home:
            env["PYTHONUSERBASE"] = site.getuserbase()  # keep user-installed packages after HOME moves
            env["HOME"] = str(home)
        return subprocess.run(["sh", "scripts/home-update.sh"], cwd=self.home, capture_output=True, text=True,
                              env=env)

    def test_good_update_sticks_and_keeps_local_settings(self):
        head = self.commit_upstream("NOTES.txt", "harmless\n")
        result = self.update()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(git(self.home, "rev-parse", "HEAD"), head)
        self.assertIn("Home AD5X", (self.home / "config" / "local.toml").read_text())

    def test_good_update_removes_strays_but_a_rollback_does_not(self):
        fake_home = Path(self.tmp.name) / "fakehome"
        fake_home.mkdir()
        legacy = fake_home / ".print_bitch_context.json"
        legacy.write_text('{"ctx": 1}')
        profile = Path(self.tmp.name) / "profile"
        (profile / "state" / "print_handy_context.json").write_text('{"ctx": 1}')
        self.commit_upstream("tests/test_broken.py",
                             "import unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.fail('broken')\n")
        self.assertEqual(self.update(home=fake_home).returncode, 1)
        self.assertTrue(legacy.exists())
        self.commit_upstream("tests/test_broken.py", "")
        result = self.update(home=fake_home)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"removed {legacy}", result.stdout)
        self.assertFalse(legacy.exists())

    def test_breaking_update_rolls_back(self):
        before = git(self.home, "rev-parse", "HEAD")
        self.commit_upstream("tests/test_broken.py",
                             "import unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.fail('broken')\n")
        result = self.update()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Rolled back", result.stdout)
        self.assertEqual(git(self.home, "rev-parse", "HEAD"), before)
        self.assertFalse((self.home / "tests" / "test_broken.py").exists())

    def test_local_edits_to_tracked_files_block_the_update(self):
        self.commit_upstream("NOTES.txt", "harmless\n")
        (self.home / "config" / "home.toml").write_text("# hand edited\n")
        result = self.update()
        self.assertEqual(result.returncode, 1)
        self.assertIn("local edits", result.stdout)


class InstallDocModePinTests(unittest.TestCase):
    """P2-210: the install guide's migration note must tell existing Hermes
    installs whose local file holds only overrides to pin bot.mode in that
    local file BEFORE updating — their mode otherwise comes from the shipped
    home template, whose line the update replaces. The old blanket claim
    that existing configs state the mode explicitly must not come back."""

    def test_migration_note_carries_the_pin_first_step(self):
        doc = (ROOT / "docs" / "INSTALL.md").read_text()
        section = doc.split("## Migration notes", 1)[1]
        self.assertIn("pin the mode before updating", section)
        self.assertIn("config/local.yaml", section)
        self.assertIn("config/local.toml", section)
        self.assertIn("mode: hermes", section)
        self.assertIn('mode = "hermes"', section)
        self.assertNotIn("existing configs state `mode: hermes`", section)
