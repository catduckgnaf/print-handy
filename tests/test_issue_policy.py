"""Offline coverage for the bug report completeness check."""
import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_issue_policy.py"
_spec = importlib.util.spec_from_file_location("issue_policy", SCRIPT)
assert _spec is not None and _spec.loader is not None
policy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(policy)

GOOD = """### What happened
The listener stopped posting after a reprint and the heartbeat went stale for hours.
### Evidence
heartbeat age 50000 s, healthcheck failing streak 1700, last log line at 22:28 ET.
### Print Handy version
2.2.4
### Printer and firmware
Flashforge AD5X with ZMod
### Diagnostics bundle
https://github.com/user-attachments/files/1/diagnostics-bundle.md
### Sanitization report
Sanitization report: removed 3 IP addresses. Verification pass found nothing further.
"""


class IssuePolicyTests(unittest.TestCase):
    def test_complete_bug_passes(self):
        self.assertEqual([], policy.check(GOOD, ["bug"]))

    def test_missing_bundle_and_report_fails(self):
        body = GOOD.split("### Diagnostics bundle")[0]
        errors = policy.check(body, ["bug"])
        self.assertEqual(1, len(errors))
        self.assertIn("sanitized diagnostics bundle", errors[0])

    def test_empty_evidence_fails(self):
        body = GOOD.replace("heartbeat age 50000 s, healthcheck failing streak 1700, last log line at 22:28 ET.", "_No response_")
        self.assertTrue(any("evidence" in e for e in policy.check(body, ["bug"])))

    def test_report_alone_is_enough(self):
        body = GOOD.replace("https://github.com/user-attachments/files/1/diagnostics-bundle.md", "_No response_")
        self.assertEqual([], policy.check(body, ["bug"]))

    def test_pasted_doctor_output_in_the_bundle_section_passes(self):
        # #1404 / the #442 shape: no attachment and no word "doctor", just
        # the Doctor rows pasted into the bundle section (>= 2 rows).
        body = GOOD.replace(
            "https://github.com/user-attachments/files/1/diagnostics-bundle.md",
            "ok    settings: Flashforge AD5X, bed 220x220 mm, 4 slot(s), 0.4 mm nozzle\n"
            "ok    printer model: flashforge.ad5x (Flashforge AD5X (ZMod)), 4 slots\n"
            "ok    core: 39 modules import").replace(
            "Sanitization report: removed 3 IP addresses. Verification pass found nothing further.",
            "_No response_")
        self.assertNotIn("doctor", body.lower().split("### diagnostics bundle")[1])
        self.assertEqual([], policy.check(body, ["bug"]))

    def test_a_single_doctor_row_is_not_a_bundle(self):
        body = GOOD.replace(
            "https://github.com/user-attachments/files/1/diagnostics-bundle.md",
            "ok    settings: Flashforge AD5X, bed 220x220 mm, 4 slot(s), 0.4 mm nozzle, all fine here"
            ).replace(
            "Sanitization report: removed 3 IP addresses. Verification pass found nothing further.",
            "_No response_")
        errors = policy.check(body, ["bug"])
        self.assertTrue(any("sanitized diagnostics bundle" in e for e in errors))

    def test_non_bug_is_ignored(self):
        self.assertEqual([], policy.check("### Idea\nA feature.", ["enhancement"]))

    # -- #1470: development-checkout "Not applicable" bundle path -----------

    def na_body(self, bundle, install="Development checkout",
                version="main at 9c90114d21229d2059aebec9cf0d33270f9e7632"):
        return f"""### What happened
The listener stopped posting after a reprint and the heartbeat went stale for hours.
### Evidence
heartbeat age 50000 s, healthcheck failing streak 1700, last log line at 22:28 ET.
### Print Handy version
{version}
### How is Print Handy installed?
{install}
### Printer and firmware
Not applicable
### Diagnostics bundle
{bundle}
### Sanitization report
_No response_
"""

    def test_dev_checkout_na_bundle_with_reason_and_commit_passes(self):
        body = self.na_body(
            "Not applicable: this is a code review finding found by offline "
            "analysis, not a runtime incident, so there is no bundle to collect.")
        self.assertEqual([], policy.check(body, ["bug"]))

    def test_dev_checkout_na_bundle_with_dash_separator_passes(self):
        # The form help text shows the em-dash shape; it must be accepted too.
        body = self.na_body(
            "Not applicable — found by offline code review, not a runtime incident")
        self.assertEqual([], policy.check(body, ["bug"]))

    def test_dev_checkout_na_reason_at_minimum_passes(self):
        # Boundary: exactly BUNDLE_NA_MIN_REASON characters of reason pass.
        reason = "r" * policy.BUNDLE_NA_MIN_REASON
        self.assertEqual([], policy.check(self.na_body(f"Not applicable: {reason}"), ["bug"]))

    def test_dev_checkout_na_reason_one_below_minimum_fails(self):
        # Boundary: one character below the minimum still fails.
        reason = "r" * (policy.BUNDLE_NA_MIN_REASON - 1)
        errors = policy.check(self.na_body(f"Not applicable: {reason}"), ["bug"])
        self.assertTrue(any("sanitized diagnostics bundle" in e for e in errors), errors)

    def test_dev_checkout_na_without_a_reason_fails(self):
        errors = policy.check(self.na_body("Not applicable"), ["bug"])
        self.assertTrue(any("sanitized diagnostics bundle" in e for e in errors), errors)

    def test_dev_checkout_na_without_commit_in_version_fails(self):
        body = self.na_body(
            "Not applicable: this is a code review finding found by offline "
            "analysis, not a runtime incident, so there is no bundle to collect.",
            version="2.2.4")
        errors = policy.check(body, ["bug"])
        self.assertEqual(1, len(errors), errors)
        self.assertIn("commit", errors[0])
        self.assertIn("print handy version", errors[0])

    def test_dev_checkout_na_with_short_commit_in_version_passes(self):
        body = self.na_body(
            "Not applicable: this is a code review finding found by offline "
            "analysis, not a runtime incident, so there is no bundle to collect.",
            version="main at 9c90114")
        self.assertEqual([], policy.check(body, ["bug"]))

    def test_runtime_bug_with_na_bundle_still_fails(self):
        for install in ("Docker", "Unraid", "Native (systemd)"):
            with self.subTest(install=install):
                body = self.na_body(
                    "Not applicable: this is a code review finding found by offline "
                    "analysis, not a runtime incident, so there is no bundle to collect.",
                    install=install)
                errors = policy.check(body, ["bug"])
                self.assertTrue(any("sanitized diagnostics bundle" in e for e in errors), errors)

    def test_bug_without_install_section_cannot_use_na_bundle(self):
        body = self.na_body(
            "Not applicable: this is a code review finding found by offline "
            "analysis, not a runtime incident, so there is no bundle to collect.")
        body = body.replace("### How is Print Handy installed?\nDevelopment checkout\n", "")
        errors = policy.check(body, ["bug"])
        self.assertTrue(any("sanitized diagnostics bundle" in e for e in errors), errors)

    # -- #1470: form pins ----------------------------------------------------

    def test_feature_request_form_label_uses_us_spelling(self):
        form = (Path(__file__).resolve().parents[1]
                / ".github/ISSUE_TEMPLATE/feature_request.yml").read_text(encoding="utf-8")
        self.assertIn("label: Proposed behavior", form)
        self.assertNotIn("label: Proposed behaviour", form)

    def test_bug_report_form_help_describes_the_na_path(self):
        form = (Path(__file__).resolve().parents[1]
                / ".github/ISSUE_TEMPLATE/bug_report.yml").read_text(encoding="utf-8")
        bundle_field = form.split("id: diagnostics_bundle", 1)[1].split("- type:", 1)[0]
        self.assertIn("Not applicable", bundle_field)
        self.assertIn("Development checkout", bundle_field)
        self.assertIn("commit", bundle_field)


if __name__ == "__main__":
    unittest.main()
