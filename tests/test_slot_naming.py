"""#1020 D + E1/E2: slot bases named, one lane conversion, the print-start
self-check, and the IFS jam classification/post.

Everything here is offline: the conversion and agreement logic is pure,
and the jam post is a text builder over measured facts.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from print_handy.core import filament_color  # noqa: E402
from print_handy.hermes import lane_data_sync, spool_sync  # noqa: E402
from print_handy.hermes import moonraker_event_listener as listener  # noqa: E402


class ConversionTableTests(unittest.TestCase):
    """D2: the inner lane value converts exactly once, in one function."""

    def test_slots_1_to_4_round_trip(self):
        for slot in (1, 2, 3, 4):
            with self.subTest(slot=slot):
                self.assertEqual(spool_sync.lane_index0(slot), slot - 1)
                self.assertEqual(spool_sync.slot_from_lane_index0(slot - 1), slot)
                # HelixScreen writes the inner value as a string.
                self.assertEqual(spool_sync.slot_from_lane_index0(str(slot - 1)), slot)

    def test_unknown_values_convert_to_none(self):
        for bad in (None, True, "x", -1, 4, 1.5, {}):
            with self.subTest(bad=bad):
                self.assertIsNone(spool_sync.slot_from_lane_index0(bad))

    def test_written_records_use_the_one_conversion(self):
        spool = {"id": 7, "filament": {"name": "PLA", "material": "PLA",
                                       "color_hex": "FF0000"}}
        for slot in (1, 2, 3, 4):
            with self.subTest(slot=slot):
                record = spool_sync.build_lane_record(slot, spool, now=1.0)
                self.assertEqual(record["lane"], spool_sync.lane_index0(slot))

    def test_read_normalization_converts_once(self):
        raw = {f"lane{slot}": {"spool_id": 7, "lane": str(slot - 1),
                               "scan_time": "1", "color": "FF0000",
                               "material": "PLA", "name": "x"}
               for slot in (1, 2, 3, 4)}
        lanes = lane_data_sync.parse_lane_data(raw)
        for slot in (1, 2, 3, 4):
            with self.subTest(slot=slot):
                self.assertEqual(lanes[slot]["slot"], slot)
                self.assertEqual(lanes[slot]["lane_slot"], slot)


class SlotDisplayTests(unittest.TestCase):
    """D1: a user-visible slot names its base."""

    def test_display_form(self):
        self.assertEqual(filament_color.slot_display(3), "slot 3 (IFS channel 3)")

    def test_runout_post_names_the_base(self):
        text = listener.paused_message("benchy.gcode", runout=True, slot=3, spool_id=7)
        self.assertIn("slot 3 (IFS channel 3)", text)


class AgreementTests(unittest.TestCase):
    """D3: warn only when the sources disagree after conversion."""

    def test_agreement_is_quiet(self):
        self.assertIsNone(lane_data_sync.slot_agreement_warning(
            job_slot=3, ffm_channel=3, ifs_chan=3, helix_slot=3))

    def test_one_known_source_is_quiet(self):
        self.assertIsNone(lane_data_sync.slot_agreement_warning(job_slot=3))
        self.assertIsNone(lane_data_sync.slot_agreement_warning())

    def test_disagreement_names_every_source_with_its_base(self):
        text = lane_data_sync.slot_agreement_warning(
            job_slot=3, ffm_channel=3, ifs_chan=3, helix_slot=2, filename="benchy.gcode")
        self.assertIn("Slot check warning", text)
        self.assertIn("slot 3 (IFS channel 3)", text)
        self.assertIn("slot 2 (IFS channel 2)", text)
        self.assertIn("HelixScreen seated", text)

    def test_helix_inner_lane_confusion_is_caught(self):
        # The #1020 evidence shape: the job's spool record sits at outer
        # lane3 but its inner lane value is "1" (0-based for slot 2) —
        # Helix's converted seat disagrees with the job's slot 3.
        raw = {"lane3": {"spool_id": 7, "lane": "1", "scan_time": "1",
                         "color": "000000", "material": "PLA", "name": "x"}}
        lanes = lane_data_sync.parse_lane_data(raw)
        seated = lane_data_sync.helix_seated_slot(lanes, 7)
        self.assertEqual(seated, 2)
        self.assertIsNotNone(lane_data_sync.slot_agreement_warning(
            job_slot=3, helix_slot=seated))


class JamTests(unittest.TestCase):
    """E1/E2: jam classification from the printer's own state, facts in
    the post, safe actions only."""

    def test_jam_classifies_from_the_pause_text(self):
        self.assertTrue(listener.pause_is_jam("Filament jam detected (IFS)"))
        self.assertEqual(listener.classify_pause("Filament jam detected (IFS)"), "jam")
        self.assertFalse(listener.pause_is_jam("Filament runout detected"))
        self.assertEqual(listener.classify_pause("Filament runout detected"), "runout")
        self.assertEqual(listener.classify_pause(""), "other")

    def test_jam_post_carries_the_measured_facts(self):
        text = listener.jam_message("benchy.gcode", {
            "duration_s": 3661, "layer": 42, "slot": 3,
            "next_cmd_delay": 0.02, "detection_length": 10, "head_sensor": True})
        self.assertIn("Filament jam detected (IFS)", text)
        self.assertIn("slot 3 (IFS channel 3)", text)
        self.assertIn("1h 01m", text)
        self.assertIn("Layer: 42", text)
        self.assertIn("next_cmd_delay: 0.02", text)
        self.assertIn("detection_length: 10", text)
        self.assertIn("Head switch sensor: present", text)

    def test_jam_post_states_unknowns_and_never_auto_resumes(self):
        text = listener.jam_message("benchy.gcode", {
            "duration_s": None, "layer": None, "slot": None,
            "next_cmd_delay": None, "detection_length": None, "head_sensor": None})
        self.assertIn("not reported", text)
        self.assertIn("nothing has resumed it", text)
        # The only actions offered are the operator's own: inspect, then
        # resume or cancel by hand.
        self.assertIn("Inspect the filament path", text)
        self.assertIn("tap Resume print below", text)
        self.assertIn("tap Cancel", text)


if __name__ == "__main__":
    unittest.main()
