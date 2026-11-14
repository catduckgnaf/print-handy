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


if __name__ == "__main__":
    unittest.main()
