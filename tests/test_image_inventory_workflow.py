"""The image inventory is report only, pinned, and cloudflared never tracks latest."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github/workflows/image-inventory.yml").read_text()


class ImageInventoryWorkflowTest(unittest.TestCase):
    def test_actions_are_pinned_to_full_commit_shas(self):
        uses = re.findall(r"uses:\s*(\S+)", WORKFLOW)
        self.assertGreaterEqual(len(uses), 3)
        for ref in uses:
            self.assertRegex(ref, r"@[0-9a-f]{40}$", ref)

    def test_trivy_scans_the_sbom_file_not_the_workspace(self):
        # aquasecurity/trivy-action builds `trivy <scan-type> <scan-ref>` and
        # `scan-ref` defaults to `.`. `input` only sets TRIVY_INPUT, which the
        # sbom subcommand does not read, so a scheduled run would scan the
        # workspace and fail before any report exists.
        block = WORKFLOW.split("scan-type: sbom", 1)[1].split("format:", 1)[0]
        self.assertIn("scan-ref: sbom.spdx.json", block)
        self.assertNotIn("input:", block)

    def test_report_only_and_read_only(self):
        self.assertIn('exit-code: "0"', WORKFLOW)
        self.assertIn("contents: read", WORKFLOW)
        self.assertNotRegex(WORKFLOW, r"(?m)^\s*[^#\n]*(docker push|push: true|push-by-digest)")
        self.assertNotIn("packages: write", WORKFLOW)

    def test_cloudflared_default_is_tag_plus_digest(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        match = re.search(r"CLOUDFLARED_IMAGE:-([^}]+)\}", compose)
        self.assertIsNotNone(match)
        self.assertRegex(match.group(1), r"^cloudflare/cloudflared:\d{4}\.\d+\.\d+@sha256:[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
