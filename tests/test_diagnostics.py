"""#390 Stage 1: taxonomy, markers and the operator-message bar."""
import errno
import os
import unittest
from unittest import mock

from print_handy import diagnostics, settings
from print_handy.diagnostics import Category


class MarkerTests(unittest.TestCase):
    def test_marker_shape_is_stable_and_greppable(self):
        self.assertEqual(diagnostics.marker("spoolman", Category.TRANSIENT),
                         "PH_SPOOLMAN_TRANSIENT")
        self.assertEqual(diagnostics.marker("Discord Startup", Category.CONFIGURATION),
                         "PH_DISCORD_STARTUP_CONFIGURATION")

    def test_legacy_marker_is_registered_and_never_renamed(self):
        self.assertIn("DISCORD_STARTUP_RETRY", diagnostics.LEGACY_MARKERS)


class ClassifyTests(unittest.TestCase):
    def test_explicit_category_always_wins(self):
        self.assertEqual(diagnostics.classify(ValueError("x"), category=Category.HARDWARE),
                         Category.HARDWARE)

    def test_settings_error_is_configuration(self):
        self.assertEqual(diagnostics.classify(settings.SettingsError("[printer] bed_mm bad")),
                         Category.CONFIGURATION)

    def test_transient_transport_failures(self):
        self.assertEqual(diagnostics.classify(TimeoutError("slow")), Category.TRANSIENT)
        self.assertEqual(diagnostics.classify(ConnectionResetError("reset")), Category.TRANSIENT)
        self.assertEqual(diagnostics.classify(OSError(errno.ETIMEDOUT, "timed out")),
                         Category.TRANSIENT)

    def test_unrecognised_failure_is_unknown_not_a_guess(self):
        self.assertEqual(diagnostics.classify(RuntimeError("mystery")), Category.UNKNOWN)
        self.assertEqual(diagnostics.classify(OSError(errno.EACCES, "denied")), Category.UNKNOWN)


class OperatorMessageTests(unittest.TestCase):
    def test_message_meets_the_bar(self):
        text = diagnostics.operator_message(
            "Spool tracking was skipped for this job", subsystem="spoolman",
            exc=ConnectionRefusedError("refused"))
        self.assertIn("Spool tracking was skipped", text)
        self.assertIn("Likely cause:", text)
        self.assertIn("What to do:", text)
        self.assertIn("Reference: [PH_SPOOLMAN_TRANSIENT](https://catduckgnaf.github.io/print-handy-docs/diagnostics#ph-spoolman-transient)", text)

    def test_no_bare_exception_text_and_secrets_are_redacted(self):
        secret = "hunter2-secret-value"
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": secret}):
            text = diagnostics.operator_message(
                "Upload failed", subsystem="delivery",
                cause=f"the stored credential {secret} was rejected",
                category=Category.CONFIGURATION)
        self.assertNotIn(secret, text)
        self.assertIn("***", text)

    def test_every_category_has_a_remedy(self):
        for category in Category:
            self.assertTrue(diagnostics.DEFAULT_REMEDIES[category])


if __name__ == "__main__":
    unittest.main()
