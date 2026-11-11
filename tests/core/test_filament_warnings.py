"""Filament warn-and-accept (owner ruling, catduck, 2026-11-10): filament
conditions never hard-block a print; they become warnings the operator can
accept. These tests pin which readiness problems are filament warnings and
the new warning-returning contract of validate_plate_usage_if_present.

"You should never block a print for that" / "I actually really don't want
anything filament to block a print, allow the user to accept the warning."
"""
import tempfile
import unittest
import zipfile
from pathlib import Path

from print_handy.core import source_colors as sc
from .test_plate_usage import write_source, two_plate_source, selection


def _stats_manifest(plates):
    return {'filament_stats': {'plates': plates}}


class FilamentWarningClassificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _unknown_usage(self):
        # Knob has no source extruder: plate 2's objects usage is unknown.
        path = write_source(self.dir / "noext.3mf",
                            [("Base", 1, None), ("Knob", None, None)], [[0], [1]])
        manifest = sc.inventory(path)
        base = manifest["parts"][0]
        decisions = {"source_sha256": manifest["source_sha256"], "schema": "part_slots_v1",
                     "mode": "per_part", "choices": {"part:" + base["key"]: selection(1)}}
        return manifest, decisions

    def test_unknown_usage_class_is_a_filament_warning(self):
        manifest, decisions = self._unknown_usage()
        warnings = sc.filament_readiness_warnings(manifest, decisions, [2])
        self.assertTrue(any("plate 2" in w and "objects" in w for w in warnings))
        # The full problem list is unchanged: warnings are a subset of it.
        problems = sc.plate_readiness_problems(manifest, decisions, [2])
        self.assertTrue(set(warnings) <= set(problems))
        self.assertEqual(sc.filament_readiness_warnings(manifest, decisions, [1]), [])

    def test_out_of_palette_support_designation_is_a_filament_warning(self):
        path = write_source(self.dir / "palette.3mf",
                            [("Base", 1, None), ("Lid", 2, None), ("Knob", 3, None)],
                            [[0, 1], [2]], project_extra={"support_filament": "9"})
        manifest = sc.inventory(path)
        parts = {p["name"]: p for p in manifest["parts"]}
        decisions = {"source_sha256": manifest["source_sha256"], "schema": "part_slots_v1",
                     "mode": "per_part",
                     "choices": {"part:" + parts[n]["key"]: selection(i + 1)
                                 for i, n in enumerate(("Base", "Lid", "Knob"))}}
        warnings = sc.filament_readiness_warnings(manifest, decisions, [1])
        self.assertTrue(any("outside project palette" in w for w in warnings), warnings)

    def test_disagreement_is_a_filament_warning(self):
        slice_info = """<config>
          <plate><metadata key="index" value="1"/><filament id="1" used_g="10" used_m="3"/><filament id="3" used_g="4" used_m="1"/></plate>
          <plate><metadata key="index" value="2"/><filament id="3" used_g="7" used_m="2"/></plate>
        </config>"""
        path = write_source(self.dir / "two.3mf",
                            [("Base", 1, None), ("Lid", 2, None), ("Knob", 3, None)],
                            [[0, 1], [2]], slice_info=slice_info)
        manifest = sc.inventory(path)
        parts = {p["name"]: p for p in manifest["parts"]}
        decisions = {"source_sha256": manifest["source_sha256"], "schema": "part_slots_v1",
                     "mode": "per_part",
                     "choices": {"part:" + parts[n]["key"]: selection(i + 1)
                                 for i, n in enumerate(("Base", "Lid", "Knob"))}}
        warnings = sc.filament_readiness_warnings(manifest, decisions, [1])
        self.assertTrue(any("disagree" in w for w in warnings), warnings)

    def test_unmapped_used_filament_is_not_a_filament_warning(self):
        # A used filament with no slot mapping in the decisions is not filament
        # usage data, so it is not in this subset. Behavior change (#1189,
        # owner decision 2026-11-10): the row no longer refuses a print; the
        # gates fill it with model_job.filament_fallbacks first and present
        # it as a warning naming the fallback slot.
        manifest = sc.inventory(two_plate_source(self.dir / "two.3mf"))
        parts = {p["name"]: p for p in manifest["parts"]}
        decisions = {"source_sha256": manifest["source_sha256"], "schema": "part_slots_v1",
                     "mode": "per_part",
                     "choices": {"part:" + parts["Base"]["key"]: selection(1),
                                 "part:" + parts["Lid"]["key"]: selection(2)}}
        problems = sc.plate_readiness_problems(manifest, decisions, [2])
        self.assertTrue(any("no validated slot mapping" in p for p in problems))
        self.assertEqual(sc.filament_readiness_warnings(manifest, decisions, [2]), [])

    def test_slot_past_capacity_is_not_a_filament_warning(self):
        # Not in the usage-data subset; since #1189 the gates replace the slot
        # with the fallback slot and warn instead of refusing (owner decision).
        manifest = sc.inventory(two_plate_source(self.dir / "two.3mf"))
        parts = {p["name"]: p for p in manifest["parts"]}
        decisions = {"source_sha256": manifest["source_sha256"], "schema": "part_slots_v1",
                     "mode": "per_part",
                     "choices": {"part:" + parts["Base"]["key"]: selection(1),
                                 "part:" + parts["Lid"]["key"]: selection(2),
                                 "part:" + parts["Knob"]["key"]: selection(5)}}
        problems = sc.plate_readiness_problems(manifest, decisions, [2], slot_capacity=4)
        self.assertTrue(any("slot 5" in p for p in problems))
        self.assertEqual(
            sc.filament_readiness_warnings(manifest, decisions, [2], slot_capacity=4), [])

    def test_unbound_decisions_are_not_a_filament_warning(self):
        manifest, _ = self._unknown_usage()
        problems = sc.plate_readiness_problems(manifest, None, [2])
        self.assertTrue(any("not bound" in p for p in problems))
        warnings = sc.filament_readiness_warnings(manifest, None, [2])
        self.assertFalse(any("not bound" in w for w in warnings))


class UsageClaimWarningTests(unittest.TestCase):
    """validate_plate_usage_if_present returns warnings; it no longer refuses
    for damaged filament usage claims. require_plate_usage_known keeps its
    strict contract for callers that ask for it directly."""

    def test_invalid_claim_warns_instead_of_refusing(self):
        manifest = _stats_manifest({
            '7': {'usage_known': False, 'usage_unknown_reason': sc.USAGE_INVALID}})
        warnings = sc.validate_plate_usage_if_present(manifest)
        self.assertEqual(len(warnings), 1)
        self.assertIn('plate 7', warnings[0])
        self.assertNotIn('no slicing attempted', warnings[0])

    def test_unreadable_claim_warns_instead_of_refusing(self):
        path = Path(tempfile.mkdtemp()) / "usage.3mf"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("Metadata/slice_info.config", "<config><plate>")
        manifest = {"filaments": []}
        sc._append_filament_stats(path, manifest)
        warnings = sc.validate_plate_usage_if_present(manifest)
        self.assertEqual(len(warnings), 1)
        self.assertIn('could not be read', warnings[0])

    def test_absent_and_unmapped_claims_stay_silent(self):
        for plates in (
                {'1': {'usage_known': False, 'usage_unknown_reason': sc.USAGE_ABSENT}},
                {'1': {'usage_known': False, 'usage_unknown_reason': sc.USAGE_NO_RECORDS}},
                {'1': {'usage_known': False, 'usage_unknown_reason': sc.USAGE_UNMAPPED}},
                {'1': {'usage_known': True, 'extruders': {1: {'g': 2.0, 'm': 1.0}}}}):
            with self.subTest(plates=plates):
                self.assertEqual(sc.validate_plate_usage_if_present(_stats_manifest(plates)), [])

    def test_warnings_scope_to_the_selected_plates(self):
        manifest = _stats_manifest({
            '1': {'usage_known': True, 'extruders': {1: {'g': 2.0, 'm': 1.0}}},
            '2': {'usage_known': False, 'usage_unknown_reason': sc.USAGE_INVALID}})
        self.assertEqual(sc.validate_plate_usage_if_present(manifest, selected_plate_ids=[1]), [])
        warnings = sc.validate_plate_usage_if_present(manifest, selected_plate_ids=[2])
        self.assertEqual(len(warnings), 1)
        self.assertIn('plate 2', warnings[0])

    def test_strict_validator_still_refuses(self):
        manifest = _stats_manifest({
            '7': {'usage_known': False, 'usage_unknown_reason': sc.USAGE_INVALID}})
        with self.assertRaisesRegex(RuntimeError, 'plate 7'):
            sc.require_plate_usage_known(manifest, selected_plate_ids=['7'])


if __name__ == "__main__":
    unittest.main()
