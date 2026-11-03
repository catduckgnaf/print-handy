"""#838 start bookkeeping, listener/alert side: runout attribution by newly
emptied slots (item 7) and the alert Cancel refusal during a Klipper
shutdown (item 9). Offline only.
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

from print_handy.hermes import moonraker_event_listener as mel
from print_handy.hermes import print_alert_buttons as pab


# ---------------------------------------------------------------- item 7: attribution

class AttributeSlotTests(unittest.TestCase):
    def test_newly_emptied_mapped_slot_wins_over_other_empty_slots(self):
        # Two slots read empty; only slot 2 became empty during this print
        # and is one of the job's mapped slots.
        self.assertEqual(mel.attribute_runout_slot({2}, [1, 2], {1, 2}, fallback=1), 2)

    def test_presence_fallback_only_when_exactly_one_candidate_remains(self):
        # Nothing newly emptied: presence evidence narrowed by the mapped
        # slots leaves exactly one candidate.
        self.assertEqual(mel.attribute_runout_slot(set(), [1, 3], {3}, fallback=1), 3)
        # Two presence candidates stay ambiguous: the recorded fallback wins.
        self.assertEqual(mel.attribute_runout_slot(set(), [1, 3], {1, 3}, fallback=2), 2)

    def test_newly_emptied_outside_the_mapping_does_not_attribute(self):
        self.assertEqual(mel.attribute_runout_slot({4}, [4], {1, 2}, fallback=1), 1)

    def test_no_mapping_recorded_uses_newly_emptied_alone(self):
        self.assertEqual(mel.attribute_runout_slot({2}, [1, 2], set(), fallback=1), 2)


class ClassifyRunoutAttributionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hardware = Path(self.tmp.name) / "hardware.json"
        self.hardware.write_text(json.dumps({"active_slot": 1, "filament_slots": {
            "1": {"spoolman_id": 10, "material": "PLA", "color": "white"},
            "2": {"spoolman_id": 11, "material": "PLA", "color": "black"}}}))
        self.old_hardware = mel.HARDWARE_STATE
        mel.HARDWARE_STATE = self.hardware

    def tearDown(self):
        mel.HARDWARE_STATE = self.old_hardware
        self.tmp.cleanup()

    async def test_two_empty_slots_attribute_to_the_newly_emptied_mapped_one(self):
        """Slot 1 (the recorded active slot) only just started reading empty —
        one unconfirmed sample — while slot 2 confirmed emptied during the
        print. Presence alone is ambiguous; the newly-emptied mapped slot is
        the runout, not the recorded active slot a CHANGE_ZCOLOR just unloaded."""
        lis = mel.Listener()
        lis.state, lis.filename = "printing", "a.gcode"
        lis.pause_message = ""
        tracker = mel.MidPrintSlotTracker()
        for _ in range(2):
            tracker.observe({1: True, 2: True})
        for _ in range(2):
            tracker.observe({1: True, 2: False})  # slot 2 confirmed emptied mid-print
        lis.slot_tracker = tracker

        async def read_slots():
            return {}, {1: False, 2: False}
        lis.read_slots = read_slots
        with mock.patch.object(mel.printers, "current", return_value=S(has_native_screen=True)), \
                mock.patch.object(mel, "read_head_sensor", lambda: None), \
                mock.patch.object(mel, "launch_mapped_slots", return_value={1, 2}), \
                mock.patch.object(mel, "log"):
            self.assertEqual(await lis.classify_runout(), (True, 2))


# ---------------------------------------------------------------- item 9: cancel in shutdown

def test_cancel_during_shutdown_refuses_and_never_restarts():
    """Alert Cancel while Klipper is in shutdown must not run FIRMWARE_RESTART
    as a side effect; it names /print-reboot instead (#838 item 9)."""
    st = {"klippy": "shutdown", "state": "paused", "filename": "f.gcode"}
    with mock.patch("print_handy.hermes.print_control.status", return_value=st), \
            mock.patch("print_handy.hermes.print_control._call") as call, \
            mock.patch.object(pab, "clear_shutdown") as clear, \
            mock.patch("print_handy.hermes.print_stop.cancel") as cancel:
        out = pab.handle_click("cancel", {"filename": "f.gcode"})
    clear.assert_not_called()
    call.assert_not_called()  # no /printer/firmware_restart, no print cancel
    cancel.assert_not_called()
    assert "/print-reboot" in out
    assert "shutdown" in out


def test_cancel_when_ready_still_cancels_normally():
    st = {"klippy": "ready", "state": "paused", "filename": "f.gcode"}
    with mock.patch("print_handy.hermes.print_control.status", return_value=st), \
            mock.patch.object(pab, "clear_shutdown") as clear, \
            mock.patch("print_handy.hermes.print_stop.cancel", return_value=(True, "Stopped.")) as cancel:
        out = pab.handle_click("cancel", {"filename": "f.gcode"})
    clear.assert_not_called()
    cancel.assert_called_once_with("f.gcode")
    assert out == "Stopped."
