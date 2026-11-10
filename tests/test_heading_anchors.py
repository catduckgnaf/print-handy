"""Regression coverage for Markdown link text in release heading anchors."""

import pathlib
import runpy
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
heading_anchors = runpy.run_path(
    str(ROOT / "scripts" / "check_release_surfaces.py")
)["heading_anchors"]


class HeadingAnchorTests(unittest.TestCase):
    def test_link_text_is_preserved_and_duplicate_headings_are_numbered(self):
        with tempfile.TemporaryDirectory() as directory:
            page = pathlib.Path(directory) / "guide.md"
            page.write_text(
                "## Install [OrcaSlicer](https://example.com)\n"
                "## Install [OrcaSlicer](https://example.com)\n",
                encoding="utf-8",
            )
            self.assertEqual(
                heading_anchors(page),
                {"install-orcaslicer", "install-orcaslicer-1"},
            )
