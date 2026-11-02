"""#390 Stage 3: the diagnostics bundle — complete, bounded, and secret-free."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from print_handy import diagnostics_bundle, settings


def _data(tmp, extra_toml=""):
    path = Path(tmp) / "local.toml"
    path.write_text(extra_toml)
    return settings.load(local=path, environ={})


class BundleTests(unittest.TestCase):
    def test_bundle_has_every_section_and_no_secret_value(self):
        secret = "bundle-secret-value-123"
        with tempfile.TemporaryDirectory() as tmp:
            data = _data(tmp)
            with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": secret,
                                              "PRINT_HANDY_IMAGE_DIGEST": "sha256:deadbeef"}):
                text = diagnostics_bundle.build_bundle(data, include_doctor=False)
        for heading in ("# Print Handy diagnostics bundle", "## Configuration summary",
                        "## Printers", "## Doctor", "## Recent log tail"):
            self.assertIn(heading, text)
        self.assertIn("sha256:deadbeef", text)
        self.assertNotIn(secret, text)

    def test_secret_shaped_values_render_as_env_indirection_only(self):
        summary = diagnostics_bundle._summarise(
            {"token_env": "DISCORD_BOT_TOKEN", "api_key": "never-rendered"}, "discord")
        self.assertIn("DISCORD_BOT_TOKEN", str(summary))
        self.assertNotIn("never-rendered", str(summary))

    def test_log_tail_is_bounded_and_redacted(self):
        secret = "tail-secret-value-456"
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "pb.log"
            log.write_text("\n".join(f"line {i} token={secret}" for i in range(50)))
            data = _data(tmp)
            data["logging"] = {"file": str(log)}
            with mock.patch.dict(os.environ, {"SOME_TOKEN": secret}):
                tail = diagnostics_bundle.log_tail(data, lines=5)
        self.assertEqual(len(tail.splitlines()), 5)
        self.assertNotIn(secret, tail)

    def test_long_url_credentials_redact_before_truncation(self):
        # The userinfo @ sits past the 77-char truncation point: truncating
        # first would strand the password with no @ left for the credential
        # pattern in logs.redact to match (P1-95).
        value = "http://" + "a" * 65 + ":s3cretpw@cam.local:7125/" + "x" * 30
        summary = diagnostics_bundle._summarise({"moonraker_url": value}, "services")
        self.assertNotIn("s3cretpw", str(summary))
        self.assertNotIn("s3cr", str(summary))

    def test_zero_log_lines_returns_no_lines_not_the_whole_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "pb.log"
            log.write_text("first line\nsecond line\n")
            data = _data(tmp)
            data["logging"] = {"file": str(log)}
            for count in (0, -3):
                tail = diagnostics_bundle.log_tail(data, lines=count)
                self.assertNotIn("first line", tail)
                self.assertIn("no log lines", tail)

    def test_missing_log_file_setting_is_explained_not_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            tail = diagnostics_bundle.log_tail(_data(tmp))
        self.assertIn("no [logging] file is configured", tail)

    def test_write_bundle_uses_state_dir_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = _data(tmp)
            target = diagnostics_bundle.write_bundle(data=data)
            self.assertTrue(target.is_file())
            self.assertEqual(target.name, diagnostics_bundle.BUNDLE_NAME)
            self.assertIn("diagnostics bundle", target.read_text())


if __name__ == "__main__":
    unittest.main()
