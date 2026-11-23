"""Jam slot attribution replay (Print #138 facts, no private text).

Facts replayed: the job was launched with T0=3 and the firmware echoed
T0 -> 3. zmod_ifs Chan already read 2 before the print (the previous
print's channel). The start self-check warned 3 vs Chan 2, and the later
explicit IFS jam was posted on slot 2 because the jam facts preferred
Chan. Neither value proves which feeder physically fed or jammed, so the
post names no slot when they conflict. Firmware jam alerts, the buttons
and the never-auto-resume rule are unchanged.
"""

import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from print_handy import settings
from print_handy.core import printer_state
from print_handy.runtime import lane_data_sync, moonraker_event_listener as mel


class ReplayCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state_root = Path(tmp.name) / "state"
        hardware = state_root / "printers" / settings.default_printer_id() / "printer_hardware.json"
        hardware.parent.mkdir(parents=True, exist_ok=True)
        hardware.write_text(json.dumps({"active_slot": 3, "filament_slots": {
            "3": {"spoolman_id": 23, "material": "PLA", "color": "6F4E37"}}}))
        self.calls = []

        def call(path, *args, **kwargs):
            self.calls.append((path, args, kwargs))
            if "FFMInfo" in path:
                return {"result": {"status": {"FFMInfo": {"channel": None},
                                              "zmod_ifs": {"Chan": 2}}}}
            return {"result": {"status": {"zmod_ifs": {"Chan": 2}}, "objects": []}}

        for patch in (
            mock.patch.object(printer_state, "STATE_ROOT", str(state_root)),
            mock.patch.dict(os.environ, {"PRINTER_HARDWARE_STATE": ""}),
            mock.patch.object(mel, "launch_mapped_slots", return_value={3}),
            mock.patch.object(mel.print_control, "_call", side_effect=call),
            mock.patch.object(mel, "log"), mock.patch.object(mel, "warn"),
            mock.patch.object(mel.lane_data_sync, "parse_lane_data", return_value={}),
            mock.patch("print_handy.runtime.spool_sync.read_lane_data", return_value=None),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def listener(self, channel):
        lis = mel.Listener()
        lis.filename = "job.gcode"
        lis.last_stats = {"print_duration": 120}
        lis.ifs_channel = channel
        return lis


class StartSelfCheckReplay(ReplayCase):
    def test_start_check_reads_only_and_never_remaps_the_slot(self):
        job, ffm, chan, helix = self.listener(2)._slot_sources()
        self.assertEqual((job, ffm, chan, helix), (3, None, 2, None))
        for path, _args, _kwargs in self.calls:
            self.assertIn("/printer/objects/query", path)  # reads only

    def test_warning_keeps_the_conflict_and_says_chan_is_not_proof(self):
        text = self.listener(2)._slot_agreement()
        self.assertIn("the job requested slot 3 (IFS channel 3)", text)
        self.assertIn("zmod_ifs Chan reports slot 2 (IFS channel 2)", text)
        self.assertIn("can still describe the previous print", text)
        self.assertIn("not proof", text)

    def test_agreeing_sources_stay_silent(self):
        self.assertIsNone(lane_data_sync.slot_agreement_warning(job_slot=3, ifs_chan=3))


class JamAttributionReplay(ReplayCase):
    def test_conflicting_chan_and_job_name_no_slot(self):
        facts = self.listener(2)._jam_facts()
        self.assertIsNone(facts["slot"])
        self.assertEqual(facts["slot_conflict"], {"job": 3, "channel": 2})
        text = mel.jam_message("job.gcode", facts)
        first = text.splitlines()[0]
        self.assertIn("Paused: filament jam", first)
        self.assertIn("an unverified slot", first)
        self.assertIn("slot 3 (IFS channel 3)", first)
        self.assertIn("slot 2 (IFS channel 2)", first)
        self.assertIn("neither is proof", first)
        self.assertNotIn("jam** on slot 2", first)
        self.assertIn("slot_conflict=job:3/channel:2", mel.jam_facts_line(facts))
        # The firmware alert content and the safe actions are untouched.
        self.assertIn("Nothing resumes on its own", text)
        self.assertIn("Cancel ends the print", text)

    def test_agreement_still_names_the_slot(self):
        facts = self.listener(3)._jam_facts()
        self.assertEqual(facts["slot"], 3)
        self.assertIsNone(facts["slot_conflict"])
        self.assertIn("jam** on slot 3 (IFS channel 3)", mel.jam_message("job.gcode", facts))

    def test_single_known_source_is_used(self):
        self.assertEqual(mel.jam_slot_attribution(None, 2), (2, None))
        self.assertEqual(mel.jam_slot_attribution(3, None), (3, None))
        self.assertEqual(mel.jam_slot_attribution(None, None), (None, None))
        self.assertEqual(mel.jam_slot_attribution(3, 0), (3, None))  # Chan 0 = nothing loaded

    def test_repause_message_uses_the_same_wording(self):
        facts = self.listener(2)._jam_facts()
        self.assertIn("an unverified slot", mel.jam_repause_message("job.gcode", facts, 1, 30))


class AmbiguousLaunchReplay(ReplayCase):
    """Several launch slots: the hardware active slot is not the job's request."""

    def test_ambiguous_launch_ignores_hardware_active_slot(self):
        with mock.patch.object(mel, "launch_mapped_slots", return_value={1, 4}):
            # Hardware active_slot is 3 and Chan is 3: agreement with a value the
            # job never requested must not name a slot.
            facts = self.listener(3)._jam_facts()
            self.assertIsNone(facts["slot"])
            self.assertIsNone(facts["slot_conflict"])

    def test_ambiguous_launch_never_names_chan_alone(self):
        # Chan may be the previous print's channel even when it is a launched slot.
        with mock.patch.object(mel, "launch_mapped_slots", return_value={1, 4}):
            for chan in (4, 1, 2):
                facts = self.listener(chan)._jam_facts()
                self.assertIsNone(facts["slot"])
                self.assertIsNone(facts["slot_conflict"])

    def test_ambiguous_launch_message_names_no_slot(self):
        # The rendered posts must not imply a known jam location.
        with mock.patch.object(mel, "launch_mapped_slots", return_value={1, 4}):
            facts = self.listener(4)._jam_facts()
        posts = (mel.jam_message("job.gcode", facts),
                 mel.jam_repause_message("job.gcode", facts, 2, 15))
        for text in posts:
            first = text.splitlines()[0]
            self.assertNotIn("the active slot", first)
            self.assertIn("an unverified slot", first)
            self.assertIn("slot 1 (IFS channel 1) and slot 4 (IFS channel 4)", first)
            self.assertIn("nothing shows which one jammed", first)
            self.assertNotIn("jam** on slot", first)
        self.assertIn("Nothing resumes on its own", posts[0])


if __name__ == "__main__":
    unittest.main()
