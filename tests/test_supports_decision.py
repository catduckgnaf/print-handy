"""#1020 F: the supports question is evidence-gated, reasoned, recorded.

Regression cases: a flat tray (no finding, clean clearance) never shows
the menu; a real overhang (slicer floating-region finding) shows it with
the region count; a low-clearance underside shows it with the measured
gap. The decision record lands on JOB.json with its evidence.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from print_handy.core import model_job as mj  # noqa: E402
from print_handy.core import support_clearance  # noqa: E402
from print_handy.core.prepared_intake import supports_decision  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "support_clearance"


class SupportsDecisionTests(unittest.TestCase):
    def test_flat_tray_never_shows_the_menu(self):
        # F1/F4: no finding, not flagged, clearance clean -> no menu.
        show, reason, inputs = supports_decision(
            False, None, {"risky": False, "findings": []})
        self.assertFalse(show)
        self.assertEqual(reason, "")
        self.assertEqual(inputs, {"flagged": False, "finding": False,
                                  "clearance_risky": False, "clearance_codes": []})
        show, _reason, _inputs = supports_decision(False, None, None)
        self.assertFalse(show)

    def test_flat_fixture_model_never_shows_the_menu(self):
        # F4 with the real analyzer: the 5.00 mm gap fixture is a plain
        # supported-by-the-plate shape — no findings, not risky.
        report = support_clearance.analyze(str(FIXTURES / "04_bed_gap_500_large.3mf"))
        self.assertFalse(support_clearance.risky(report))
        show, _reason, _inputs = supports_decision(False, None, report)
        self.assertFalse(show)

    def test_real_overhang_shows_the_menu_with_the_region_count(self):
        # F2/F4: the slicer's floating-region finding, with its numbers.
        finding = {"objects": ["arm_left", "arm_right"],
                   "texts": ["floating regions need support"]}
        show, reason, inputs = supports_decision(False, finding, None)
        self.assertTrue(show)
        self.assertIn("2 floating regions", reason)
        self.assertTrue(inputs["finding"])

    def test_low_clearance_fixture_shows_the_menu_with_the_gap(self):
        # F2/F4: the 0.40 mm gap fixture is risky, and the reason names
        # the measured clearance.
        report = support_clearance.analyze(str(FIXTURES / "01_bed_gap_040_large.3mf"))
        self.assertTrue(support_clearance.risky(report))
        show, reason, inputs = supports_decision(False, None, report)
        self.assertTrue(show)
        self.assertIn("0.4 mm", reason)
        self.assertTrue(inputs["clearance_risky"])
        self.assertIn(support_clearance.CODE_CLEARANCE, inputs["clearance_codes"])

    def test_flagged_only_falls_back_to_a_plain_reason(self):
        show, reason, _inputs = supports_decision(True, None, None)
        self.assertTrue(show)
        self.assertIn("flagged", reason)


class SupportsQuestionRecordTests(unittest.TestCase):
    def test_the_record_keeps_reason_inputs_report_and_finding(self):
        # F3: JOB.json carries the whole audit after the decision.
        audit = {"shown": True, "reason": "the slicer found 1 floating region",
                 "inputs": {"flagged": True, "finding": True,
                            "clearance_risky": False, "clearance_codes": []},
                 "clearance": {"risky": False, "findings": []},
                 "finding": {"objects": ["arm"], "texts": ["floating"]}}
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "JOB.json").write_text(json.dumps({"id": "job-1"}))
            mj.record_supports_question(folder, audit)
            stored = json.loads((folder / "JOB.json").read_text())
        record = stored["supports_question"]
        self.assertTrue(record["shown"])
        self.assertEqual(record["reason"], audit["reason"])
        self.assertEqual(record["inputs"], audit["inputs"])
        self.assertEqual(record["clearance"], audit["clearance"])
        self.assertEqual(record["finding"], audit["finding"])
        self.assertIn("at", record)


if __name__ == "__main__":
    unittest.main()
