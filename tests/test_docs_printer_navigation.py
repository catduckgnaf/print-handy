"""Issue #1526: printer docs are organized brand, model, firmware, connection.

The printer registry is the ground truth; the docs are the shop window.
This offline contract fails when:

* a registered model key is not listed in docs/PRINTER-PROFILES.md;
* a registered vendor's firmware route is not linked to its guide from
  docs/PRINTER-PROFILES.md, or that guide file is missing;
* the README's "Find your printer" section loses a brand heading, drops
  a brand or firmware guide link, resurrects the "Everybody else"
  bucket, or stops naming Reforge as unprofiled;
* the generic Klipper / OctoPrint fallback moves ahead of named brands.

When a new vendor or model lands, extend ROUTES / BRANDS here in the
same change as its docs; that is the point of the contract.
"""

import pathlib
import re
import unittest

from print_handy import printers

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

# Vendor key -> the guide its firmware route is documented in.
ROUTES = {
    "anycubic": "ANYCUBIC.md",
    "bambulab": "BAMBU-LAB.md",
    "creality": "CREALITY.md",
    "elegoo": "ELEGOO.md",
    "elegoo_sdcp": "ELEGOO.md",
    "flashforge": "ZMOD.md",
    "flashforge_forgex": "FORGEX.md",
    "flashforge_opencreator": "OPENCREATOR.md",
    "flashforge_reforge": "REFORGE.md",
    "flashforge_stock": "FLASHFORGE-STOCK.md",
    "klipper": "PRINTER-SUPPORT.md",
    "octoprint": "OCTOPRINT-MARLIN.md",
    "prusa": "PRUSA.md",
    "qidi": "QIDI.md",
    "snapmaker": "SNAPMAKER.md",
    "sovol": "SOVOL.md",
    "sovol_marlin": "SOVOL.md",
}

# The named brands, in the order the README presents them. The generic
# fallback is not a brand and stays after all of them.
BRANDS = [
    "FlashForge",
    "Creality",
    "Qidi",
    "Snapmaker",
    "Bambu Lab",
    "Prusa",
    "Elegoo",
    "Anycubic",
    "Sovol",
]

GENERIC_HEADING = "Any other Klipper or Marlin printer"


def section(text, heading, level="##"):
    """The text under one heading, up to the next heading of its level."""
    start = text.index(f"{level} {heading}")
    rest = text[start + len(level) + len(heading):]
    end = rest.find(f"\n{level} ")
    return rest if end == -1 else rest[:end]


class PrinterNavigationContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.readme = (ROOT / "README.md").read_text(encoding="utf-8")
        cls.profiles = (DOCS / "PRINTER-PROFILES.md").read_text(encoding="utf-8")
        cls.find_section = section(cls.readme, "Find your printer")
        cls.picker = section(cls.profiles, "Find your printer")

    def test_every_registered_model_key_is_listed_in_printer_profiles(self):
        for key in printers.MODELS:
            with self.subTest(model=key):
                self.assertIn(
                    f"`{key}`", self.profiles,
                    f"registered model {key} has no model-key row in "
                    "docs/PRINTER-PROFILES.md; add it to the model key table")

    def test_routes_cover_every_registered_vendor(self):
        self.assertEqual(
            set(ROUTES), set(printers.VENDORS),
            "ROUTES in this test must name a guide for every registered "
            "vendor; a new vendor needs a guide mapping and docs here")

    def test_every_vendor_route_links_its_guide_from_printer_profiles(self):
        for vendor, guide in sorted(ROUTES.items()):
            with self.subTest(vendor=vendor):
                self.assertTrue(
                    (DOCS / guide).exists(),
                    f"the guide for vendor {vendor}, docs/{guide}, does not exist")
                self.assertIn(
                    f"](./{guide})", self.profiles,
                    f"docs/PRINTER-PROFILES.md does not link the {vendor} "
                    f"route to its guide, docs/{guide}")

    def test_readme_has_no_everybody_else_bucket(self):
        self.assertNotIn("Everybody else", self.readme)
        self.assertNotIn("Everybody else", self.profiles)

    def test_readme_brand_sections_come_before_the_generic_fallback(self):
        positions = []
        for brand in BRANDS:
            with self.subTest(brand=brand):
                heading = f"### {brand}"
                self.assertIn(
                    heading, self.find_section,
                    f"README's Find your printer section has no {brand} section")
                positions.append(self.find_section.index(heading))
        self.assertEqual(positions, sorted(positions),
                         "README brand sections are out of their declared order")
        self.assertIn(f"### {GENERIC_HEADING}", self.find_section)
        self.assertGreater(
            self.find_section.index(f"### {GENERIC_HEADING}"), positions[-1],
            "the generic Klipper / Marlin fallback must follow the named brands")

    def test_readme_links_every_brand_and_firmware_guide(self):
        for guide in sorted(set(ROUTES.values())):
            with self.subTest(guide=guide):
                self.assertIn(
                    f"docs/{guide}", self.find_section,
                    f"README's Find your printer section does not link "
                    f"docs/{guide}")

    def test_flashforge_shows_five_routes_and_links_reforge_verification(self):
        flashforge = section(self.find_section, "FlashForge", level="###")
        for guide in ("FLASHFORGE-STOCK.md", "ZMOD.md", "FORGEX.md", "OPENCREATOR.md", "REFORGE.md"):
            with self.subTest(guide=guide):
                self.assertIn(f"docs/{guide}", flashforge)
        self.assertNotRegex(flashforge, re.compile(r"no dedicated Print Handy profile", re.I))
        heading = "Verification status: built from published config, not hardware verified"
        reforge = (ROOT / "docs" / "REFORGE.md").read_text(encoding="utf-8")
        self.assertIn(f"## {heading}", reforge)
        self.assertIn(
            "docs/REFORGE.md#verification-status-built-from-published-config-not-hardware-verified",
            flashforge,
        )

    def test_picker_keeps_named_brands_ahead_of_the_generic_rows(self):
        for brand in BRANDS:
            with self.subTest(brand=brand):
                self.assertRegex(
                    self.picker, re.compile(rf"^\| {re.escape(brand)} \|", re.M | re.I),
                    f"the PRINTER-PROFILES.md picker has no {brand} row")
        self.assertGreater(
            self.picker.index("Any other Klipper printer"),
            self.picker.lower().index("| snapmaker |"),
            "the generic rows must follow the named brands in the picker")


if __name__ == "__main__":
    unittest.main()
