"""Non-Docker install surface acceptance (deep-dive item 15), offline.

Two install surfaces ship in this repo:

* the ZMod plugin trio at the repo root -- ``install.sh`` /
  ``update.sh`` / ``uninstall.sh`` plus ``print-handy.cfg``;
* the standalone (no-Hermes) installer ``scripts/install-standalone.sh``,
  which renders the systemd user units from ``systemd/*.service.template``
  into ``SYSTEMD_USER_DIR`` and provisions the data folder that
  ``[paths] hermes_home`` names.

``tests/test_home_update.py`` (the git update/rollback script) already
covers its surface by executing it; nothing here duplicates those
cases. This file pins the cross-surface contracts those suites cannot
see: shell syntax, script constants vs systemd unit directives (drift
between them is exactly what breaks a bare-metal install silently),
install -> update -> uninstall path/name consistency read from the
scripts' own text, and the bot-check / doctor entries the docs promise.

The standalone installer IS honestly sandboxable -- it takes
``SYSTEMD_USER_DIR``, ``PYTHON`` and ``NO_SYSTEMCTL=1`` overrides and
derives its data folder through the settings env override -- so one
test executes it for real inside a throwaway folder and checks the
rendered layout. The root plugin trio is not executable off a printer
(ZMod/Moonraker run them), so those stay at contract level.
"""
import os
import re
import stat
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ROOT_SCRIPTS = ("install.sh", "update.sh", "uninstall.sh")
STANDALONE = ROOT / "scripts" / "install-standalone.sh"
UNIT_TEMPLATES = {
    "print-handy-bot": ROOT / "systemd" / "print-handy-bot.service.template",
    "print-handy-listener": ROOT / "systemd" / "print-handy-listener.service.template",
}
EXPECTED_EXECSTART = {
    "print-handy-bot": "-m print_handy.bot run",
    "print-handy-listener": "-m print_handy.hermes.moonraker_event_listener",
}


def read(path):
    return Path(path).read_text()


def unit_directives(text):
    """(section, key) -> value for a systemd unit/template, comments dropped."""
    directives = {}
    section = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif "=" in line:
            key, _, value = line.partition("=")
            directives.setdefault((section, key.strip()), []).append(value.strip())
    return directives


class ShellSyntaxTests(unittest.TestCase):
    def test_every_install_surface_script_is_syntactically_valid_shell(self):
        scripts = [ROOT / name for name in ROOT_SCRIPTS] + [
            ROOT / "scripts" / name for name in
            ("install-standalone.sh", "home-update.sh",
             "install-orca-flatpak.sh", "migrate-names.sh")]
        for script in scripts:
            with self.subTest(script=script.name):
                self.assertTrue(script.is_file(), script)
                shell = "bash" if "bash" in script.read_text().splitlines()[0] else "sh"
                result = subprocess.run([shell, "-n", str(script)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_root_scripts_declare_the_shell_they_are_written_for(self):
        for name in ROOT_SCRIPTS:
            with self.subTest(script=name):
                self.assertTrue(read(ROOT / name).startswith("#!/bin/sh"))


class PluginTrioConsistencyTests(unittest.TestCase):
    """install -> update -> uninstall, read from the trio's own text."""

    def test_the_cfg_name_is_the_same_in_all_three_scripts_and_on_disk(self):
        self.assertTrue((ROOT / "print-handy.cfg").is_file())
        self.assertIn("print-handy.cfg", read(ROOT / "install.sh"))
        self.assertIn("print-handy.cfg", read(ROOT / "update.sh"))
        # The old underscore name may appear only as the rename source.
        self.assertIn("print_handy.cfg", read(ROOT / "update.sh"))

    def test_the_macro_block_uninstall_names_is_the_one_the_cfg_defines(self):
        cfg = read(ROOT / "print-handy.cfg")
        self.assertIn("[gcode_macro _PRINT_HANDY_VARS]", cfg)
        uninstall = read(ROOT / "uninstall.sh")
        self.assertIn("_PRINT_HANDY_VARS", uninstall)
        self.assertIn("mod_data/user.cfg", uninstall)
        # install.sh sends the operator to the same file update.sh
        # promises not to change.
        self.assertIn("mod_data/user.cfg", read(ROOT / "install.sh"))
        self.assertIn("mod_data/user.cfg", read(ROOT / "update.sh"))

    def test_update_promises_not_to_touch_what_install_says_to_copy(self):
        self.assertIn("were not changed", read(ROOT / "update.sh"))
        self.assertIn("does not switch filament", read(ROOT / "install.sh"))
        self.assertIn("does not write printer config", read(ROOT / "install.sh"))


class StandaloneUnitContractTests(unittest.TestCase):
    def test_templates_exist_for_exactly_the_units_the_installer_writes(self):
        installer = read(STANDALONE)
        for name, template in UNIT_TEMPLATES.items():
            with self.subTest(unit=name):
                self.assertTrue(template.is_file(), template)
                self.assertIn(name, installer)
        self.assertIn("systemd/$name.service.template", installer)
        self.assertEqual({p.name for p in (ROOT / "systemd").glob("*.service.template")},
                         {f"{name}.service.template" for name in UNIT_TEMPLATES})

    def test_unit_directives_match_the_installers_own_constants(self):
        installer = read(STANDALONE)
        for name, template in UNIT_TEMPLATES.items():
            with self.subTest(unit=name):
                directives = unit_directives(read(template))
                # ExecStart runs the module the template names, through
                # the {{PYTHON}} the installer substitutes.
                self.assertEqual(directives[("Service", "ExecStart")],
                                 ["{{PYTHON}} " + EXPECTED_EXECSTART[name]])
                self.assertEqual(directives[("Service", "WorkingDirectory")], ["{{REPO}}"])
                # These are systemd USER units: they run as the invoking
                # user, so a User= directive would be a defect, not a pin.
                self.assertNotIn(("Service", "User"), directives)
                self.assertEqual(directives[("Install", "WantedBy")], ["default.target"])
                self.assertEqual(directives[("Service", "Restart")], ["always"])
                # Secrets live in the data folder's .env, substituted
                # from the same {{DATA_HOME}} the installer provisions.
                self.assertEqual(directives[("Service", "EnvironmentFile")],
                                 ["-{{DATA_HOME}}/.env"])
                self.assertIn("PRINT_HANDY__BOT__MODE=standalone",
                              directives[("Service", "Environment")])
        # The installer substitutes exactly the placeholders the
        # templates carry -- no more, no fewer (drift either way breaks).
        placeholders = set()
        for template in UNIT_TEMPLATES.values():
            placeholders |= set(re.findall(r"\{\{[A-Z_]+\}\}", read(template)))
        self.assertEqual(placeholders, {"{{REPO}}", "{{PYTHON}}", "{{DATA_HOME}}"})
        for placeholder in placeholders:
            self.assertIn(f"s|{placeholder}|", installer)

    def test_every_path_the_installer_creates_is_named_from_its_constants(self):
        installer = read(STANDALONE)
        # Directories: the data home, its state and logs, the unit dir.
        for fragment in ('"$DATA_HOME"', '"$DATA_HOME/state"', '"$DATA_HOME/logs"',
                         '"$SYSTEMD_USER_DIR"'):
            self.assertIn(fragment, installer)
        # Files: the .env in the data home and one unit per template.
        self.assertIn('"$DATA_HOME/.env"', installer)
        self.assertIn('"$SYSTEMD_USER_DIR/$name.service"', installer)
        # The data home comes from the settings layer, not a hard-coded
        # path that could drift from what the bot actually reads.
        self.assertIn("print_handy.settings get paths.hermes_home", installer)


class StandaloneSandboxRunTests(unittest.TestCase):
    """The installer executed for real in a throwaway folder."""

    def run_installer(self, tmp, *args):
        data = Path(tmp) / "data"
        units = Path(tmp) / "units"
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("PRINT_HANDY__") and k != "PRINT_HANDY_CONFIG"}
        env.update({"PRINT_HANDY__PATHS__HERMES_HOME": str(data),
                    "SYSTEMD_USER_DIR": str(units), "NO_SYSTEMCTL": "1",
                    "PYTHON": sys.executable, "HOME": str(tmp)})
        result = subprocess.run(["bash", str(STANDALONE), *args],
                                capture_output=True, text=True, env=env, cwd=tmp)
        return result, data, units

    def test_sandboxed_install_renders_the_promised_layout(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            result, data, units = self.run_installer(tmp)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for name, execstart in EXPECTED_EXECSTART.items():
                unit = (units / f"{name}.service").read_text()
                self.assertNotIn("{{", unit, "every placeholder was substituted")
                self.assertIn(f"WorkingDirectory={ROOT}", unit)
                self.assertIn(f"ExecStart={sys.executable} {execstart}", unit)
                self.assertIn(f"EnvironmentFile=-{data}/.env", unit)
            env_file = data / ".env"
            self.assertIn("DISCORD_BOT_TOKEN=", env_file.read_text())
            self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)
            self.assertTrue((data / "state" / "printer_hardware.json").is_file())
            self.assertTrue((data / "logs").is_dir())
            # The last lines name the check entry the docs promise.
            self.assertIn("-m print_handy.bot check", result.stdout)

    def test_sandboxed_dry_run_changes_nothing(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            result, data, units = self.run_installer(tmp, "--dry-run")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("would:", result.stdout)
            self.assertFalse(data.exists())
            self.assertFalse(units.exists())


class PromisedEntryPointsTests(unittest.TestCase):
    def test_the_bot_check_entry_the_docs_and_installer_promise_exists(self):
        docs = read(ROOT / "docs" / "STANDALONE.md")
        self.assertIn("python3 -m print_handy.bot check", docs)
        self.assertIn("-m print_handy.bot check", read(STANDALONE))
        main = read(ROOT / "print_handy" / "bot" / "__main__.py")
        self.assertIn('command == "check"', main)

    def test_the_doctor_entry_the_docs_promise_exists(self):
        self.assertIn("python3 -m print_handy.doctor", read(ROOT / "docs" / "INSTALL.md"))
        doctor = ROOT / "print_handy" / "doctor.py"
        self.assertTrue(doctor.is_file())
        # The doctor runs as a module: its entry is the __main__ guard.
        self.assertIn('if __name__ == "__main__":', read(doctor))


if __name__ == "__main__":
    unittest.main()
