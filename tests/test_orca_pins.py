"""The real OrcaSlicer CI tests is the one that ships, pinned in one place.

Docker ships the AppImage that docker/orca-prebuilt.sh pins for the one bundled build (the
stable channel; Print Handy ships a single OrcaSlicer and there is no second channel); the
real-slicer CI legs run the same upstream release as a flatpak bundle, on a GNOME runtime that
Flathub rebuilds several times a week. ci.yml pins the bundle and the runtime commit once, at
workflow level, and scripts/install-orca-flatpak.sh installs exactly that. These tests fail when
an Orca bump updates one side and not the other, or when a pin is copied back into a job where
the next bump will miss it.
"""

import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
PREBUILT = ROOT / "docker" / "orca-prebuilt.sh"
INSTALLER = ROOT / "scripts" / "install-orca-flatpak.sh"
REAL_ORCA_JOBS = ("slicer-test-stable", "slicer-test-bambu", "slicer-timing-real")


def workflow():
    return yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)


def prebuilt_channel(channel):
    """Every pin (version, revision, sha256_*, asset_*) of a channel arm in docker/orca-prebuilt.sh."""
    text = PREBUILT.read_text()
    arm = re.search(rf"^    {channel}\)\n(.*?)^        ;;", text, re.S | re.M).group(1)
    return dict(re.findall(r"^\s+(version|revision|sha256_\w+|asset_\w+)=(\S+)$", arm, re.M))


_STAGES = {"alpha": 0, "beta": 1, "rc": 2}


def release_order(version):
    """Sort key for upstream Orca tags: 2.5.0-alpha < 2.5.0-beta < 2.5.0-rc < 2.5.0 < 2.5.1-alpha."""
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc))?", version)
    assert m, f"unrecognised Orca version {version!r}"
    major, minor, patch, stage = m.groups()
    return int(major), int(minor), int(patch), _STAGES.get(stage, 3)


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


class SingleChannelPins(unittest.TestCase):
    """Print Handy ships ONE bundled OrcaSlicer: only the stable arm exists, and it is complete."""

    def test_only_the_stable_channel_is_pinned(self):
        text = PREBUILT.read_text()
        self.assertEqual(re.findall(r"^    (\w+)\)$", text, re.M), ["stable"])
        self.assertNotIn("preview)", text)

    def test_the_single_channel_pins_every_field(self):
        self.assertEqual(set(prebuilt_channel("stable")),
                         {"version", "revision", "sha256_amd64", "sha256_arm64", "asset_amd64", "asset_arm64"})

    def test_ci_bundle_is_the_single_pinned_build(self):
        # Single-channel consistency: the workflow-level bundle is the one pin
        # in orca-prebuilt.sh (there is no second channel to diverge from).
        pins = prebuilt_channel("stable")
        self.assertEqual(workflow()["env"]["ORCA_BUNDLE_VERSION"], pins["version"])

    def test_no_preview_slicer_job_remains(self):
        self.assertNotIn("slicer-test-preview", workflow()["jobs"])

    def test_assets_name_their_version(self):
        pins = prebuilt_channel("stable")
        for arch in ("amd64", "arm64"):
            with self.subTest(arch=arch):
                self.assertTrue(pins[f"asset_{arch}"].endswith(f"_V{pins['version']}.AppImage"))

    def test_release_order(self):
        order = ["2.4.2", "2.5.0-alpha", "2.5.0-beta", "2.5.0-rc", "2.5.0", "2.5.1-alpha"]
        self.assertEqual(sorted(order, key=release_order), order)


if __name__ == "__main__":
    unittest.main()
