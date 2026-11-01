"""The real OrcaSlicer CI tests is the one that ships, pinned in one place.

Docker ships the AppImage that docker/orca-prebuilt.sh pins per channel; the real-slicer CI legs
run the same upstream release as a flatpak bundle, on a GNOME runtime that Flathub rebuilds
several times a week. ci.yml pins the bundle and the runtime commit once, at workflow level, and
scripts/install-orca-flatpak.sh installs exactly that. These tests fail when an Orca bump updates
one side and not the other, or when a pin is copied back into a job where the next bump will miss it.
"""

import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
PREBUILT = ROOT / "docker" / "orca-prebuilt.sh"
INSTALLER = ROOT / "scripts" / "install-orca-flatpak.sh"
REAL_ORCA_JOBS = ("slicer-test-stable", "slicer-test-preview", "slicer-test-bambu")


def workflow():
    return yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)


def prebuilt_channel(channel):
    """The version=/revision= pins of one channel's case arm in docker/orca-prebuilt.sh."""
    text = PREBUILT.read_text()
    arm = re.search(rf"^    {channel}\)\n(.*?)^        ;;", text, re.S | re.M).group(1)
    return dict(re.findall(r"^\s+(version|revision)=(\S+)$", arm, re.M))


class OrcaPins(unittest.TestCase):
    def test_pins_are_set_once_at_workflow_level(self):
        env = workflow()["env"]
        self.assertRegex(env["ORCA_BUNDLE_SHA256"], r"^[0-9a-f]{64}$")
        self.assertRegex(env["ORCA_RUNTIME_COMMIT"], r"^[0-9a-f]{64}$")
        self.assertEqual(env["ORCA_RUNTIME_REF"], "runtime/org.gnome.Platform/x86_64/50")

    def test_ci_bundle_is_the_release_docker_ships_on_stable(self):
        self.assertEqual(workflow()["env"]["ORCA_BUNDLE_VERSION"], prebuilt_channel("stable")["version"])

    def test_no_pin_is_copied_into_the_workflow_body(self):
        env = workflow()["env"]
        text = WORKFLOW_PATH.read_text()
        for name in ("ORCA_BUNDLE_VERSION", "ORCA_BUNDLE_SHA256", "ORCA_RUNTIME_COMMIT"):
            with self.subTest(pin=name):
                self.assertEqual(text.count(env[name]), 1, f"{name} value appears outside the env block")
        self.assertNotIn("org.gnome.Platform//", text)

    def test_every_real_orca_leg_installs_through_the_script(self):
        jobs = workflow()["jobs"]
        for job_id in REAL_ORCA_JOBS:
            with self.subTest(job=job_id):
                job = jobs[job_id]
                self.assertEqual(job["env"]["ORCA_FLATPAK_APP_ID"], "com.orcaslicer.OrcaSlicer")
                runs = [step.get("run", "") for step in job["steps"]]
                self.assertIn("sh scripts/install-orca-flatpak.sh", runs)
                cache = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/cache@"))
                # A cache restored from another runtime commit would be downgraded by the script
                # every run; keying on the pins keeps one cache per pinned pair.
                self.assertIn("${{ env.ORCA_BUNDLE_VERSION }}", cache["with"]["key"])
                self.assertIn("${{ env.ORCA_RUNTIME_COMMIT }}", cache["with"]["key"])

    def test_installer_deploys_and_proves_the_pinned_runtime(self):
        script = INSTALLER.read_text()
        self.assertIn('--commit="$ORCA_RUNTIME_COMMIT" "$ORCA_RUNTIME_REF"', script)
        self.assertIn('test "$deployed" = "$ORCA_RUNTIME_COMMIT"', script)
        self.assertIn('sha256sum --check', script)
        self.assertNotRegex(script, r"[0-9a-f]{40}")


if __name__ == "__main__":
    unittest.main()
