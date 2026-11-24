"""IFS jams are classified from the printer's own alert line (#1787).

ZMod leaves print_stats.message empty on a macro-driven pause (#442) and
reports an IFS jam only as a gcode response ("!! Filament jam detected
(IFS)."). On 2026-11-21 three jams on an AD5X were posted as a runout and as
generic pauses, with a spool question, because the jam post (#1020 E1) only
read the empty pause message. The line can arrive just before or just after
the paused state; both orders are pinned here, plus the "still jammed" case
after a resume.
"""

import os
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

from print_handy import settings
from print_handy.core import printer_state
from print_handy.runtime import moonraker_event_listener as mel
from print_handy.runtime import print_alert_buttons as pab
from print_handy.runtime import print_control, print_stop, spool_sync

JAM_LINE = "!! Filament jam detected (IFS)."
PHOTO_LINE = "tgalarm_photo Filament jam detected (IFS)."
FACTS = {"duration_s": 9488, "layer": 132, "slot": 3, "next_cmd_delay": None,
         "detection_length": None, "head_sensor": True}


def st(state):
    return {"state": state, "filename": "tongs.gcode", "layer": 132, "total_layer": 540,
            "progress": 0.25, "print_duration": 9488}


class FirmwareCauseTests(unittest.TestCase):
    def test_alert_lines_name_the_cause(self):
        self.assertEqual(mel.firmware_pause_cause(JAM_LINE), "jam")
        self.assertEqual(mel.firmware_pause_cause(PHOTO_LINE), "jam")
        self.assertEqual(mel.firmware_pause_cause("!! Filament runout detected"), "runout")

    def test_other_lines_are_not_a_cause(self):
        # Not alert-shaped, or an alert that names no filament cause.
        self.assertIsNone(mel.firmware_pause_cause("// Filament jam detected (IFS)"))
        self.assertIsNone(mel.firmware_pause_cause("echo: printing jam_jar_lid.gcode"))
        self.assertIsNone(mel.firmware_pause_cause("!! Move out of range: 0.000 300.000"))
        self.assertIsNone(mel.firmware_pause_cause("// action:prompt_text Extruder: None (3)"))
        self.assertIsNone(mel.firmware_pause_cause(""))
        self.assertIsNone(mel.firmware_pause_cause(None))


class JamListenerCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        state_root = Path(self.tmp.name) / "state"
        hardware = state_root / "printers" / settings.default_printer_id() / "printer_hardware.json"
        hardware.parent.mkdir(parents=True, exist_ok=True)
        hardware.write_text(json.dumps({"active_slot": 3, "filament_slots": {
            "3": {"spoolman_id": 21, "material": "PLA+", "color": "black"}}}))
        patches = [
            mock.patch.object(printer_state, "STATE_ROOT", str(state_root)),
            mock.patch.dict(os.environ, {"PRINTER_HARDWARE_STATE": ""}),
            mock.patch.object(mel.printers, "current", return_value=S(has_native_screen=True)),
            mock.patch.object(mel, "log"),
            mock.patch.object(mel, "run_announcer"),
            mock.patch.object(mel, "post"),
            mock.patch.object(mel, "_bot_token", return_value="token"),
            mock.patch.object(print_stop, "status", return_value=st("paused")),
            mock.patch.object(pab, "latest_alert_message", return_value=("chan", "m1"), create=True),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.post_alert = mock.patch.object(mel, "post_alert", return_value=True).start()
        self.addCleanup(mock.patch.stopall)
        self.rearm = mock.patch.object(pab, "rearm_alert", return_value=True, create=True).start()
        self.resume = mock.patch.object(print_control, "resume").start()

    def tearDown(self):
        self.tmp.cleanup()

    def listener(self, *, head=True, slots=None):
        lis = mel.Listener()
        lis.state, lis.filename = "printing", "tongs.gcode"
        lis.pause_message = ""

        async def read_slots():
            return {}, slots or {3: True}
        lis.read_slots = read_slots
        lis._jam_facts = lambda: dict(FACTS)
        lis._settle_launch_lease = lambda: None
        lis.open_question = mock.AsyncMock(return_value={
            "slot": 3, "cause": "runout", "suspended_spool_id": 21})
        mock.patch.object(mel, "read_head_sensor", lambda: head).start()
        return lis

    async def pause(self, lis):
        await lis.on_status({"state": "paused", "message": ""}, "event")

    async def resume_print(self, lis):
        await lis.on_status({"state": "printing"}, "event")

    def posted(self):
        return [call.args[0] for call in self.post_alert.call_args_list]


class JamBeforePauseTests(JamListenerCase):
    async def test_a_jam_line_before_the_pause_gets_the_jam_post_and_no_spool_question(self):
        lis = self.listener()
        await lis.note_firmware_alert(JAM_LINE)
        await self.pause(lis)
        self.assertEqual(len(self.posted()), 1)
        self.assertIn("Paused: filament jam", self.posted()[0])
        self.assertIn("slot 3 (IFS channel 3)", self.posted()[0])
        lis.open_question.assert_not_awaited()
        self.assertEqual(lis.last_pause_episode, "paused:jam")
        self.resume.assert_not_called()  # a jam is never resumed by Print Handy

    async def test_the_line_reaches_the_listener_through_the_gcode_response(self):
        lis = self.listener()
        lis.opencreator_prompt = mock.AsyncMock()
        raw = json.dumps({"method": "notify_gcode_response", "params": [JAM_LINE]})
        await lis._handle_message(None, raw)
        self.assertEqual(lis.firmware_cause, "jam")
        await self.pause(lis)
        self.assertIn("Paused: filament jam", self.posted()[0])

    async def test_an_old_jam_line_does_not_explain_a_later_pause(self):
        lis = self.listener()
        await lis.note_firmware_alert(JAM_LINE)
        lis.firmware_cause_at -= mel.FIRMWARE_CAUSE_WINDOW_S + 1
        await self.pause(lis)
        self.assertTrue(self.posted()[0].startswith("Print paused:"))
        self.assertNotEqual(lis.last_pause_episode, "paused:jam")


class JamAfterPauseTests(JamListenerCase):
    async def test_a_jam_line_after_a_generic_pause_upgrades_it_in_place(self):
        lis = self.listener(head=True)
        await self.pause(lis)  # no cause yet: posted generic
        self.assertTrue(self.posted()[0].startswith("Print paused:"))
        await lis.note_firmware_alert(JAM_LINE)
        await lis.note_firmware_alert(PHOTO_LINE)  # the second alert line changes nothing more
        self.assertEqual(lis.last_pause_episode, "paused:jam")
        self.assertEqual(self.rearm.call_count, 1)
        _token, channel, message, text, kind, _file = self.rearm.call_args.args
        self.assertEqual((channel, message, kind), ("chan", "m1", "paused"))
        self.assertIn("Paused: filament jam", text)
        self.assertEqual(len(self.posted()), 1)  # rewritten, not a second post

    async def test_a_jam_line_after_a_runout_pause_withdraws_the_spool_question(self):
        lis = self.listener(head=False)  # the empty head sensor reads as a runout
        with mock.patch.object(spool_sync, "resume_usage") as resume_usage, \
                mock.patch.object(mel, "clear_runout_pause") as clear:
            await self.pause(lis)
            self.assertIn("Filament runout", self.posted()[0])
            lis.open_question.assert_awaited_once()
            await lis.note_firmware_alert(JAM_LINE)
        resume_usage.assert_called_once_with(21)
        clear.assert_called_once()
        self.assertIsNone(lis.pause_context)
        self.assertFalse(lis.last_pause_was_runout)
        text = self.rearm.call_args.args[3]
        self.assertIn("Paused: filament jam", text)
        self.assertIn("spool question above no longer applies", text)

    async def test_the_withdrawal_names_the_spool_usage_resumed_on(self):
        lis = self.listener(head=False)
        with mock.patch.object(spool_sync, "resume_usage", return_value=True), \
                mock.patch.object(mel, "clear_runout_pause"):
            await self.pause(lis)
            await lis.note_firmware_alert(JAM_LINE)
        self.assertIn("filament usage is back on spool #21", self.rearm.call_args.args[3])

    async def test_a_multi_slot_print_is_not_reported_as_resumed(self):
        """PR #1791 review: resume_usage returns False on a multi-slot print
        (#1362), where the usage pointer stays cleared on purpose. The text
        says so instead of claiming usage is back on the spool."""
        lis = self.listener(head=False)
        with mock.patch.object(spool_sync, "resume_usage", return_value=False), \
                mock.patch.object(mel, "clear_runout_pause") as clear:
            await self.pause(lis)
            await lis.note_firmware_alert(JAM_LINE)
        clear.assert_called_once()  # the same outcome the same-spool answer gives
        text = self.rearm.call_args.args[3]
        self.assertIn("multi-slot print", text)
        self.assertNotIn("back on spool", text)

    async def test_a_failed_resume_keeps_the_question_open_and_says_so(self):
        lis = self.listener(head=False)
        with mock.patch.object(spool_sync, "resume_usage",
                               side_effect=spool_sync.SpoolError("readback mismatch")), \
                mock.patch.object(mel, "clear_runout_pause") as clear:
            await self.pause(lis)
            await lis.note_firmware_alert(JAM_LINE)
        clear.assert_not_called()
        self.assertIsNotNone(lis.pause_context)  # the question and its reminders stay
        text = self.rearm.call_args.args[3]
        self.assertIn("still open", text)
        self.assertNotIn("no longer applies", text)
        self.assertEqual(lis.last_pause_episode, "paused:jam")

    async def test_a_jam_line_long_after_the_pause_is_not_an_upgrade(self):
        lis = self.listener(head=True)
        await self.pause(lis)
        lis.paused_at -= mel.FIRMWARE_CAUSE_WINDOW_S + 1
        await lis.note_firmware_alert(JAM_LINE)
        self.assertNotEqual(lis.last_pause_episode, "paused:jam")
        self.rearm.assert_not_called()


class StillJammedTests(JamListenerCase):
    async def test_jamming_again_right_after_a_resume_says_still_jammed(self):
        """The 2026-11-21 sequence: resumed three times, jammed again each time."""
        lis = self.listener()
        await lis.note_firmware_alert(JAM_LINE)
        await self.pause(lis)
        for round_ in range(1, 4):
            await self.resume_print(lis)
            self.assertIsNone(lis.firmware_cause)  # a resume spends the cause
            await lis.note_firmware_alert(JAM_LINE)
            await self.pause(lis)
            text = self.rearm.call_args.args[3]
            self.assertIn("Still jammed", text)
            self.assertIn("Nothing was changed", text)
            self.assertEqual(lis.jam_episode["repauses"], round_)
        self.assertEqual(len(self.posted()), 1)  # the first jam post only; the rest rewrite it
        self.assertIn("(3 times now)", self.rearm.call_args.args[3])
        lis.open_question.assert_not_awaited()

    async def test_a_jam_long_after_the_resume_is_a_new_jam_post(self):
        lis = self.listener()
        await lis.note_firmware_alert(JAM_LINE)
        await self.pause(lis)
        await self.resume_print(lis)
        lis.jam_episode["resumed_at"] -= mel.JAM_REPAUSE_WINDOW_S + 1
        await lis.note_firmware_alert(JAM_LINE)
        await self.pause(lis)
        self.assertEqual(len(self.posted()), 2)
        self.assertIn("Paused: filament jam", self.posted()[1])
        self.assertNotIn("Still jammed", self.posted()[1])

    async def test_without_an_editable_alert_one_short_post_is_added(self):
        lis = self.listener()
        self.rearm.return_value = False
        await lis.note_firmware_alert(JAM_LINE)
        await self.pause(lis)
        await self.resume_print(lis)
        await lis.note_firmware_alert(JAM_LINE)
        await self.pause(lis)
        self.assertEqual(len(self.posted()), 2)
        self.assertIn("Still jammed", self.posted()[1])


if __name__ == "__main__":
    unittest.main()
