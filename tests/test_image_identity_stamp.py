"""The publishing workflows stamp the image tag and commit into the image (#1332).

The diagnostics bundle reads ``PRINT_HANDY_IMAGE_TAG`` and
``PRINT_HANDY_IMAGE_SHA`` (#1020 C1). Before #1332 nothing set them in a
published image, so every bundle said "not recorded". Each workflow that
pushes an image now passes both as build args, from the same variables
that feed its OCI version and revision labels, and the Dockerfile turns
them into the runtime environment as its last layer.
"""
import os
import re
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
PUBLISHERS = ("docker-publish.yml", "develop.yml", "nightly.yml")


def _buildx_invocations(workflow):
    """Every ``docker buildx build`` command in a workflow, continuation lines joined."""
    data = yaml.safe_load((ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8"))
    found = []
    for job in (data.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            run = step.get("run") or ""
            joined = re.sub(r"\\\s*\n\s*", " ", run)
            found += [line for line in joined.splitlines() if "docker buildx build" in line]
    return found


class ImageIdentityStamp(unittest.TestCase):
    def test_every_pushing_build_stamps_the_labelled_tag_and_commit(self):
        for workflow in PUBLISHERS:
            invocations = _buildx_invocations(workflow)
            # One acceptance build and one push build per workflow; both must
            # carry the args, or the pushed image would not be the accepted one.
            self.assertEqual(len(invocations), 2, workflow)
            for cmd in invocations:
                version = re.search(r"org\.opencontainers\.image\.version=\$(\w+)", cmd)
                revision = re.search(r"org\.opencontainers\.image\.revision=\$(\w+)", cmd)
                self.assertTrue(version and revision, (workflow, cmd))
                self.assertIn(f'--build-arg "IMAGE_TAG=${version.group(1)}"', cmd, workflow)
                self.assertIn(f'--build-arg "IMAGE_SHA=${revision.group(1)}"', cmd, workflow)

    def test_dockerfile_turns_the_args_into_the_environment_the_bundle_reads(self):
        lines = [ln.strip() for ln in (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()]
        self.assertIn("ARG IMAGE_TAG=", lines)
        self.assertIn("ARG IMAGE_SHA=", lines)
        env = "ENV PRINT_HANDY_IMAGE_TAG=$IMAGE_TAG PRINT_HANDY_IMAGE_SHA=$IMAGE_SHA"
        self.assertIn(env, lines)
        # A per-commit value must not invalidate the cached build layers.
        after = lines[lines.index("ARG IMAGE_TAG="):]
        self.assertFalse([ln for ln in after if ln.startswith(("RUN ", "COPY ", "FROM "))], after)

    def test_the_bundle_reports_the_stamped_values(self):
        from print_handy import diagnostics_bundle

        with mock.patch.dict(os.environ, {"PRINT_HANDY_IMAGE_TAG": "2.3.0",
                                          "PRINT_HANDY_IMAGE_SHA": "c4fdf55f"}):
            self.assertEqual(diagnostics_bundle._image_identity(), "tag 2.3.0, commit c4fdf55f")
        with mock.patch.dict(os.environ, {"PRINT_HANDY_IMAGE_TAG": "", "PRINT_HANDY_IMAGE_SHA": ""}):
            self.assertIn("not recorded", diagnostics_bundle._image_identity())


if __name__ == "__main__":
    unittest.main()
