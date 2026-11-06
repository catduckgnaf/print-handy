# Fixture excerpt of tests/test_go2rtc_container.py: the pin-table guard.
AMD64_SHA = "20b46c6910734cb05ecefb234066f36868ff19b42ba1418a7a32e13a3e035712"
ARM64_SHA = "af416f22b21a24ab83b8d78642190b4932ae695b4e386853f11d1b0319781763"


class Go2rtcPrebuiltTests(unittest.TestCase):
    """go2rtc is a pinned official prebuilt, verified before installation."""

    def test_pin_table_parses_to_the_official_assets_and_digests(self):
        helper = PREBUILT.read_text()
        self.assertIn("version=1.9.15", helper)
        self.assertIn(f"sha256_amd64={AMD64_SHA}", helper)
        self.assertIn(f"sha256_arm64={ARM64_SHA}", helper)
        self.assertIn("asset_amd64=go2rtc_linux_amd64", helper)
        self.assertIn("asset_arm64=go2rtc_linux_arm64", helper)
        self.assertIn("https://github.com/AlexxIT/go2rtc/releases/download/v${version}/${asset}", helper)
