"""Contract for the GPU image flavor build (#1196): build only, pinned actions, both flavors."""

import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
PATH = ROOT / ".github" / "workflows" / "gpu-flavor-build.yml"


class GpuFlavorWorkflowContract(unittest.TestCase):
    def setUp(self):
        self.text = PATH.read_text()
        self.workflow = yaml.load(self.text, Loader=yaml.BaseLoader)
        self.job = self.workflow["jobs"]["build"]

    def test_builds_each_flavor_the_dockerfile_accepts(self):
        flavors = self.job["strategy"]["matrix"]["flavor"]
        self.assertEqual(flavors, ["openvino", "cuda"])
        dockerfile = (ROOT / "Dockerfile").read_text()
        for flavor in flavors:
            self.assertIn(flavor, dockerfile)

    def test_never_pushes_or_logs_in(self):
        for word in ("--push", "docker login", "docker push", "ghcr.io", "packages: write", "id-token"):
            self.assertNotIn(word, self.text)
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})

    def test_actions_are_pinned_to_a_commit(self):
        uses = re.findall(r"uses: (\S+)", self.text)
        self.assertTrue(uses)
        for ref in uses:
            self.assertRegex(ref, r"@[0-9a-f]{40}$")

    def test_build_passes_the_flavor_and_skips_the_heavy_optional_stages(self):
        run = "\n".join(step.get("run", "") for step in self.job["steps"])
        self.assertIn('--build-arg ORT_FLAVOR="$ORT_FLAVOR"', run)
        self.assertIn("WITH_ORCA=false", run)
        self.assertEqual(self.job["env"]["ORT_FLAVOR"], "${{ matrix.flavor }}")

    def test_path_filter_covers_what_changes_the_flavor(self):
        paths = self.workflow["on"]["pull_request"]["paths"]
        self.assertIn("Dockerfile", paths)
        self.assertIn("pyproject.toml", paths)
        self.assertIn(".github/workflows/gpu-flavor-build.yml", paths)
