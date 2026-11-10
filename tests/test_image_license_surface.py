"""The image names its license and source, and ships the third-party components table."""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class ImageLicenseSurface(unittest.TestCase):
    def test_dockerfile_labels_license_source_and_description(self):
        text = (ROOT / "Dockerfile").read_text()
        for label in ("org.opencontainers.image.licenses", "org.opencontainers.image.source",
                      "org.opencontainers.image.description"):
            self.assertIn(label, text)
        self.assertIn('org.opencontainers.image.licenses="AGPL-3.0-only"', text)
        self.assertIn("https://github.com/catduckgnaf/print-handy", text)

    def test_the_components_table_ships_in_the_image(self):
        text = (ROOT / "Dockerfile").read_text()
        self.assertRegex(text, r"COPY LICENSE NOTICE docs/COMPONENTS\.md /usr/share/doc/print-handy/")
        lines = (ROOT / ".dockerignore").read_text().splitlines()
        self.assertIn("docs", lines)
        self.assertGreater(lines.index("!docs/COMPONENTS.md"), lines.index("docs"))

    def test_every_pinned_component_row_names_a_license_and_a_source(self):
        text = (ROOT / "docs" / "COMPONENTS.md").read_text(encoding="utf-8")
        table = text.split("## Pinned components", 1)[1].split("\n## ", 1)[0]
        rows = [r for r in table.splitlines() if r.startswith("| ") and not r.startswith("| ---")]
        header, body = rows[0], rows[1:]
        self.assertTrue(header.rstrip().endswith("| License | Source |"))
        self.assertGreaterEqual(len(body), 10)
        for row in body:
            cells = [c.strip() for c in row.strip().strip("|").split(" | ")]
            self.assertTrue(cells[-2], row)
            self.assertRegex(cells[-1], r"<https://", row)

    def test_the_spoolman_row_matches_the_dockerfile_pin(self):
        ref = re.search(r"ARG SPOOLMAN_REF=([0-9a-f]{40})", (ROOT / "Dockerfile").read_text()).group(1)
        self.assertIn(f"commit `{ref[:8]}…` (`SPOOLMAN_REF`)", (ROOT / "docs" / "COMPONENTS.md").read_text(encoding="utf-8"))


class LicensingAttribution1158(unittest.TestCase):
    """#1158: the table stays in step with the pins, the README links it,
    the validation workflow asserts the labels on the built image, and
    every release carries an SBOM."""

    def test_readme_links_the_components_table(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/COMPONENTS.md", readme)

    def test_every_build_pin_appears_in_the_table(self):
        table = (ROOT / "docs" / "COMPONENTS.md").read_text(encoding="utf-8")
        orca = (ROOT / "docker" / "orca-prebuilt.sh").read_text()
        orca_version = re.search(r"version=([0-9][0-9A-Za-z.]*)", orca).group(1)
        orca_revision = re.search(r"revision=([0-9a-f]{40})", orca).group(1)
        self.assertIn(orca_version, table)
        self.assertIn(f"revision `{orca_revision[:8]}…`", table)
        go2rtc = (ROOT / "docker" / "go2rtc-prebuilt.sh").read_text()
        go2rtc_version = re.search(r"version=([0-9][0-9A-Za-z.]*)", go2rtc).group(1)
        self.assertIn(f"`{go2rtc_version}`", table)
        dockerfile = (ROOT / "Dockerfile").read_text()
        bambu_ref = re.search(r"ARG BAMBU_REF=([0-9a-f]{40})", dockerfile).group(1)
        self.assertIn(f"commit `{bambu_ref[:8]}…`", table)
        uv_version = re.search(r"uv==([0-9][0-9A-Za-z.]*)", dockerfile).group(1)
        self.assertIn(f"`{uv_version}`", table)
        compose = (ROOT / "docker-compose.yml").read_text()
        cloudflared = re.search(r"cloudflare/cloudflared:([0-9][0-9A-Za-z.]*)@", compose).group(1)
        self.assertIn(f"`{cloudflared}`", table)

    def test_piper_is_listed_as_operator_supplied_not_shipped(self):
        # Wrapped narration uses a Piper binary and voice model from
        # operator-configured paths; neither is bundled, and the table
        # must say so instead of implying a shipped component.
        table = (ROOT / "docs" / "COMPONENTS.md").read_text(encoding="utf-8")
        self.assertIn("Piper", table)
        piper_lines = [ln for ln in table.splitlines() if "Piper" in ln]
        self.assertTrue(
            any("not shipped" in ln or "operator-supplied" in ln for ln in piper_lines),
            piper_lines,
        )

    def test_ffmpeg_row_is_verified_against_the_debian_copyright_file(self):
        table = (ROOT / "docs" / "COMPONENTS.md").read_text(encoding="utf-8")
        ffmpeg_lines = [ln for ln in table.splitlines() if "ffmpeg" in ln]
        self.assertTrue(ffmpeg_lines)
        self.assertTrue(
            any("7.1.5" in ln and "copyright" in ln for ln in ffmpeg_lines),
            ffmpeg_lines,
        )

    def test_publish_workflows_stamp_the_revision_label(self):
        for name in ("docker-publish.yml", "develop.yml", "nightly.yml"):
            text = (ROOT / ".github" / "workflows" / name).read_text()
            self.assertIn("org.opencontainers.image.revision", text, name)

    def test_image_validation_workflow_asserts_the_labels(self):
        text = (ROOT / ".github" / "workflows" / "docker-image-validation.yml").read_text()
        self.assertIn("org.opencontainers.image.licenses", text)
        self.assertIn("org.opencontainers.image.source", text)

    def test_publish_run_generates_an_sbom_for_the_published_image(self):
        text = (ROOT / ".github" / "workflows" / "docker-publish.yml").read_text()
        self.assertIn("anchore/sbom-action", text)
        self.assertIn("spdx", text.lower())

    def test_release_workflow_attaches_the_sbom_to_the_release(self):
        path = ROOT / ".github" / "workflows" / "release-sbom.yml"
        self.assertTrue(path.is_file(), "release-sbom.yml is missing")
        text = path.read_text()
        self.assertIn("release:", text)
        self.assertIn("published", text)
        self.assertIn("anchore/sbom-action", text)
        self.assertIn("gh release upload", text)


if __name__ == "__main__":
    unittest.main()
