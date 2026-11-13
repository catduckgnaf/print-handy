"""#677: raw readers of the hardware record adopt the pre-#672 ifs_slots key."""
import json
import tempfile
import unittest
from pathlib import Path

from print_handy.runtime import moonraker_event_listener as mel

OLD_RECORD = {"active_slot": 2, "ifs_slots": {"2": {"spoolman_id": 12}}}


class LegacySlotsKeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hardware = Path(self.tmp.name) / "hardware.json"
        self.old_hardware = mel.HARDWARE_STATE
        mel.HARDWARE_STATE = self.hardware

    def tearDown(self):
        mel.HARDWARE_STATE = self.old_hardware
        self.tmp.cleanup()

    def test_recovery_slot_reads_an_old_key_record(self):
        self.hardware.write_text(json.dumps(OLD_RECORD))
        self.assertEqual(mel.recovery_slot("a.gcode"), (2, 12))

    def test_mapped_spool_reads_an_old_key_record(self):
        self.hardware.write_text(json.dumps(OLD_RECORD))
        self.assertEqual(mel._mapped_spool(2), 12)

    def test_current_key_record_still_reads(self):
        self.hardware.write_text(json.dumps({"active_slot": 2, "filament_slots": {"2": {"spoolman_id": 7}}}))
        self.assertEqual(mel.recovery_slot("a.gcode"), (2, 7))
        self.assertEqual(mel._mapped_spool(2), 7)

    def test_unreadable_record_still_answers_nothing(self):
        self.assertEqual(mel.recovery_slot("a.gcode"), (None, None))
        self.assertIsNone(mel._mapped_spool(2))
        self.hardware.write_text("{not json")
        self.assertEqual(mel.recovery_slot("a.gcode"), (None, None))
        self.assertIsNone(mel._mapped_spool(2))


if __name__ == "__main__":
    unittest.main()
