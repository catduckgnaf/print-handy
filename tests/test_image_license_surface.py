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


if __name__ == "__main__":
    unittest.main()
