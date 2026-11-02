"""scripts/migrate-names.sh against temporary directories. Never touches a real install."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "migrate-names.sh"
UNIT = "hermes-3dprinting-moonraker-listener.service"


class MigrateNamesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "profile"
        self.archive = self.tmp / "archive"
        self.units = self.tmp / "units"
        self.config = self.tmp / "local.toml"
        (self.home / "state").mkdir(parents=True)
        (self.archive / "job1").mkdir(parents=True)
        (self.units / f"{UNIT}.d").mkdir(parents=True)
        (self.home / "state" / "print_buddy_context.json").write_text("ctx\n")
        (self.home / "state" / "print_bitch_start_messages.json").write_text("msgs\n")
        (self.home / "state" / "print_buddy_start_claims.json").write_text("old\n")
        (self.home / "state" / "print_handy_start_claims.json").write_text("new\n")
        (self.archive / "job1" / "print_buddy_note.txt").write_text("note\n")
        (self.home / ".env").write_text("PRINT_BUDDY_ARCHIVE=/x\nOTHER=1\n")
        (self.home / "config.yaml").write_text("a: 1\n")
        self.config.write_text("# PRINT_BUDDY_CONTEXT_PATH mentioned\n")
        (self.units / UNIT).write_text("[Service]\nEnvironment=PRINT_BUDDY_CLEANUP_DRY_RUN=1\n")
        (self.units / f"{UNIT}.d" / "x.conf").write_text("[Service]\nEnvironment=PRINT_BUDDY_BOUNDS_CACHE=/y\n")

    def run_script(self, *args):
        result = subprocess.run(
            ["bash", str(SCRIPT), "--hermes-home", str(self.home), "--archive", str(self.archive),
             "--systemd-dir", str(self.units), "--config", str(self.config), *args],
            capture_output=True, text=True, cwd=self.tmp)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result.stdout

    def test_gateway_hermes_home_is_ignored(self):
        # The gateway exports HERMES_HOME=~/.hermes; the migration must still use the profile.
        result = subprocess.run(
            ["bash", str(SCRIPT), "--archive", str(self.archive), "--systemd-dir", str(self.units),
             "--config", str(self.config)],
            capture_output=True, text=True, cwd=self.tmp,
            env=dict(os.environ, HERMES_HOME=str(self.tmp / "gateway-root"),
                     PRINT_HANDY__PATHS__HERMES_HOME=str(self.home)))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn(f"profile:  {self.home}", result.stdout)
        self.assertTrue((self.home / "state" / "print_handy_context.json").exists())

    def snapshot(self):
        return {str(p.relative_to(self.tmp)): (p.read_text() if p.is_file() else None)
                for p in sorted(self.tmp.rglob("*"))}

    def test_dry_run_changes_nothing(self):
        before = self.snapshot()
        out = self.run_script("--dry-run")
        self.assertEqual(before, self.snapshot())
        self.assertIn("would: rename", out)
        self.assertIn("would: edit", out)

    def test_migrates_names_and_backs_up(self):
        out = self.run_script()
        state = self.home / "state"
        self.assertEqual("ctx\n", (state / "print_handy_context.json").read_text())
        self.assertFalse((state / "print_buddy_context.json").exists())
        # The older print_bitch name chains straight to print_handy.
        self.assertEqual("msgs\n", (state / "print_handy_start_messages.json").read_text())
        self.assertFalse((state / "print_bitch_start_messages.json").exists())
        self.assertEqual("new\n", (state / "print_handy_start_claims.json").read_text())
        self.assertEqual("old\n", (state / "print_buddy_start_claims.json").read_text())
        self.assertIn("exists", out)
        self.assertTrue((self.archive / "job1" / "print_handy_note.txt").exists())
        self.assertEqual("PRINT_HANDY_ARCHIVE=/x\nOTHER=1\n", (self.home / ".env").read_text())
        self.assertEqual("PRINT_BUDDY_ARCHIVE=/x\nOTHER=1\n", (self.home / ".env.pre-rename").read_text())
        self.assertIn("PRINT_HANDY_CONTEXT_PATH", self.config.read_text())
        self.assertIn("PRINT_HANDY_CLEANUP_DRY_RUN", (self.units / UNIT).read_text())
        self.assertIn("PRINT_HANDY_BOUNDS_CACHE", (self.units / f"{UNIT}.d" / "x.conf").read_text())
        self.assertFalse((self.home / "config.yaml.pre-rename").exists())
        self.assertIn("FIRMWARE_RESTART", out)

    def test_pb_orca_cmd_is_renamed_too(self):
        # #504: the leftover short-prefix variable rides along with the PRINT_BUDDY_ pass,
        # including in a file that carries no PRINT_BUDDY_ name at all.
        (self.home / ".env").write_text("PB_ORCA_CMD=/opt/orca\nOTHER=1\n")
        self.run_script()
        self.assertEqual("PRINT_HANDY_ORCA_CMD=/opt/orca\nOTHER=1\n", (self.home / ".env").read_text())
        self.assertEqual("PB_ORCA_CMD=/opt/orca\nOTHER=1\n", (self.home / ".env.pre-rename").read_text())

    def test_pb_orca_cmd_is_migrated_after_an_earlier_rename_pass(self):
        # P2-194: a file the earlier pass already migrated carries an
        # existing .pre-rename backup; that used to skip the file before
        # the PB_ORCA_CMD substitution ran, leaving a variable the current
        # code no longer reads.
        (self.home / ".env").write_text("PRINT_HANDY_ARCHIVE=/x\nPB_ORCA_CMD=/opt/orca\n")
        (self.home / ".env.pre-rename").write_text("PRINT_BUDDY_ARCHIVE=/x\nPB_ORCA_CMD=/opt/orca\n")
        self.run_script()
        self.assertEqual("PRINT_HANDY_ARCHIVE=/x\nPRINT_HANDY_ORCA_CMD=/opt/orca\n",
                         (self.home / ".env").read_text())
        # The existing backup still holds the true original, untouched.
        self.assertEqual("PRINT_BUDDY_ARCHIVE=/x\nPB_ORCA_CMD=/opt/orca\n",
                         (self.home / ".env.pre-rename").read_text())

    def test_second_run_is_a_no_op(self):
        self.run_script()
        after_first = self.snapshot()
        self.run_script()
        self.assertEqual(after_first, self.snapshot())

    def test_archive_root_itself_is_never_renamed(self):
        root = self.tmp / "print_buddy_archive"
        (root / "job").mkdir(parents=True)
        (root / "job" / "print_buddy_x.txt").write_text("x\n")
        self.archive = root
        self.run_script()
        self.assertTrue(root.is_dir())
        self.assertTrue((root / "job" / "print_handy_x.txt").exists())

    def test_printer_steps_configure_the_plugin_before_enabling_it(self):
        out = self.run_script("--dry-run")
        self.assertLess(out.index("user.moonraker.conf"), out.index("ENABLE_PLUGIN name=print-handy"))

    def test_printer_family_becomes_model(self):
        self.config.write_text('[printer]\nfamily = "sovol_klipper"\nfilament_slots = 1\n')
        self.run_script()
        self.assertEqual('[printer]\nmodel = "sovol.sv06"\nfilament_slots = 1\n', self.config.read_text())
        self.assertEqual('[printer]\nfamily = "sovol_klipper"\nfilament_slots = 1\n',
                         Path(f"{self.config}.pre-model").read_text())


if __name__ == "__main__":
    unittest.main()
