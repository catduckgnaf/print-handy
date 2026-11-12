"""#442: a filament runout pause must be announced as a runout, not a generic pause.

Klipper's macro-driven runout pause leaves print_stats.message empty, so the
cause has to come from the printer's filament sensor (discovered from the
Moonraker object list) and/or the IFS slot presence — and a slot that empties
while an already-posted pause is still generic must upgrade the alert.
All Moonraker state here is fake; nothing touches a live printer.
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

from print_handy.runtime import moonraker_event_listener as mel
from print_handy.runtime import print_alert_buttons as pab
from print_handy.runtime import print_control
from print_handy.runtime import print_stop

HEAD_SENSOR = "filament_switch_sensor head_switch_sensor"
ISSUE_SENSOR_STATUS = {HEAD_SENSOR: {"filament_detected": False, "enabled": True}}


def st(state, filename="a.gcode", **kw):
    return {"state": state, "filename": filename, "layer": kw.get("layer"),
            "total_layer": kw.get("total"), "progress": kw.get("progress"),
            "print_duration": None}


class HeadSensorParseTests(unittest.TestCase):
    def test_issue_sensor_payload_is_a_runout(self):
        self.assertIs(mel.head_sensor_from_status(ISSUE_SENSOR_STATUS), False)

    def test_loaded_head_reads_true(self):
        status = {HEAD_SENSOR: {"filament_detected": True, "enabled": True}}
        self.assertIs(mel.head_sensor_from_status(status), True)

    def test_motion_sensor_counts_too(self):
        status = {"filament_motion_sensor encoder": {"filament_detected": False, "enabled": True}}
        self.assertIs(mel.head_sensor_from_status(status), False)

    def test_disabled_sensor_is_unknown_not_runout(self):
        status = {HEAD_SENSOR: {"filament_detected": False, "enabled": False}}
        self.assertIsNone(mel.head_sensor_from_status(status))

    def test_absent_or_malformed_sensor_is_unknown(self):
        self.assertIsNone(mel.head_sensor_from_status({}))
        self.assertIsNone(mel.head_sensor_from_status(None))
        self.assertIsNone(mel.head_sensor_from_status({HEAD_SENSOR: {"enabled": True}}))
        self.assertIsNone(mel.head_sensor_from_status(
            {HEAD_SENSOR: {"filament_detected": "false", "enabled": True}}))
        self.assertIsNone(mel.head_sensor_from_status(
            {HEAD_SENSOR: {"filament_detected": False}}))  # enabled not reported

    def test_an_inactive_tools_empty_sensor_is_not_evidence(self):
        """P2-171: on a dual-extruder printer the idle tool's sensor reads
        empty normally. Only the ACTIVE extruder's path is runout
        evidence — here it is loaded, so the head is loaded."""
        status = {
            "toolhead": {"extruder": "extruder1"},
            "filament_switch_sensor extruder": {"filament_detected": False, "enabled": True},
            "filament_switch_sensor extruder1": {"filament_detected": True, "enabled": True},
        }
        self.assertIs(mel.head_sensor_from_status(status), True)

    def test_the_active_tools_empty_sensor_is_a_runout(self):
        status = {
            "toolhead": {"extruder": "extruder1"},
            "filament_switch_sensor extruder": {"filament_detected": True, "enabled": True},
            "filament_switch_sensor extruder1": {"filament_detected": False, "enabled": True},
        }
        self.assertIs(mel.head_sensor_from_status(status), False)

    def test_conflicting_sensors_with_no_determinable_active_tool_are_unknown(self):
        status = {
            "filament_switch_sensor a": {"filament_detected": False, "enabled": True},
            "filament_switch_sensor b": {"filament_detected": True, "enabled": True},
        }
        self.assertIsNone(mel.head_sensor_from_status(status))

    def test_agreeing_sensors_with_no_determinable_active_tool_still_decide(self):
        status = {
            "filament_switch_sensor a": {"filament_detected": False, "enabled": True},
            "filament_motion_sensor b": {"filament_detected": False, "enabled": True},
        }
        self.assertIs(mel.head_sensor_from_status(status), False)

    def test_sensor_names_come_from_the_object_list(self):
        objects = [HEAD_SENSOR, "filament_motion_sensor e0", "extruder", "gcode_macro PAUSE"]
        self.assertEqual(mel.filament_sensor_names(objects),
                         [HEAD_SENSOR, "filament_motion_sensor e0"])
        self.assertEqual(mel.filament_sensor_names(None), [])
        self.assertEqual(mel.filament_sensor_names("not-a-list"), [])


class ReadHeadSensorTests(unittest.TestCase):
    def fake_call(self, payloads):
        def call(path, method="GET", data=None, timeout=10, base=None):
            if path == "/printer/objects/list":
                return payloads["list"]
            return payloads["query"]
        return call

    def test_discovers_and_queries_the_printers_own_sensor(self):
        payloads = {
            "list": {"result": {"objects": ["extruder", HEAD_SENSOR]}},
            "query": {"result": {"status": ISSUE_SENSOR_STATUS}},
        }
        with mock.patch.object(print_control, "_call", self.fake_call(payloads)), \
                mock.patch.object(mel, "log"):
            self.assertIs(mel.read_head_sensor(), False)

    def test_the_query_also_asks_for_the_toolhead(self):
        """P2-171: which extruder is active decides whose sensor counts,
        so the read asks Moonraker for the toolhead alongside the sensors."""
        seen = []

        def call(path, method="GET", data=None, timeout=10, base=None):
            seen.append(path)
            if path == "/printer/objects/list":
                return {"result": {"objects": ["extruder", HEAD_SENSOR]}}
            return {"result": {"status": ISSUE_SENSOR_STATUS}}

        with mock.patch.object(print_control, "_call", call), \
                mock.patch.object(mel, "log"):
            self.assertIs(mel.read_head_sensor(), False)
        self.assertIn("toolhead", seen[-1])

    def test_single_extruder_entry_sensor_empty_is_a_runout(self):
        """2.2.5 review (P2-171): one extruder, head + entry sensors; an empty
        entry sensor is still runout evidence on the only filament path."""
        status = {
            "toolhead": {"extruder": "extruder"},
            "filament_switch_sensor head": {"filament_detected": True, "enabled": True},
            "filament_switch_sensor entry": {"filament_detected": False, "enabled": True},
        }
        payloads = {
            "list": {"result": {"objects": ["extruder", "filament_switch_sensor head",
                                            "filament_switch_sensor entry"]}},
            "query": {"result": {"status": status}},
        }
        with mock.patch.object(print_control, "_call", self.fake_call(payloads)),                 mock.patch.object(mel, "log"):
            self.assertIs(mel.read_head_sensor(), False)

    def test_multi_extruder_still_ignores_the_idle_tools_sensor(self):
        status = {
            "toolhead": {"extruder": "extruder1"},
            "filament_switch_sensor extruder": {"filament_detected": False, "enabled": True},
            "filament_switch_sensor extruder1": {"filament_detected": True, "enabled": True},
        }
        payloads = {
            "list": {"result": {"objects": ["extruder", "extruder1"] + list(
                k for k in status if k != "toolhead")}},
            "query": {"result": {"status": status}},
        }
        with mock.patch.object(print_control, "_call", self.fake_call(payloads)),                 mock.patch.object(mel, "log"):
            self.assertIs(mel.read_head_sensor(), True)

    def test_no_sensor_objects_means_unknown(self):
        payloads = {"list": {"result": {"objects": ["extruder", "print_stats"]}}}
        with mock.patch.object(print_control, "_call", self.fake_call(payloads)), \
                mock.patch.object(mel, "log"):
            self.assertIsNone(mel.read_head_sensor())

    def test_unreachable_moonraker_is_unknown_never_runout(self):
        def boom(path, **kw):
            raise OSError("connection refused")
        with mock.patch.object(print_control, "_call", boom), mock.patch.object(mel, "log"):
            self.assertIsNone(mel.read_head_sensor())

    def test_pause_is_runout_combination(self):
        self.assertTrue(mel.pause_is_runout("", 1, {1: True}, False))   # head sensor empty
        self.assertTrue(mel.pause_is_runout("", 1, {1: False}, None))   # slot empty
        self.assertFalse(mel.pause_is_runout("", 1, {1: True}, None))   # no evidence
        self.assertFalse(mel.pause_is_runout("", None, {}, True))       # head loaded


class PausedMessageTests(unittest.TestCase):
    def message(self, **kw):
        with mock.patch.object(print_stop, "status", return_value=st("paused", layer=1, total=9)):
            return mel.paused_message("a.gcode", **kw)

    def test_runout_message_names_slot_spool_and_offers_resume(self):
        text = self.message(runout=True, slot=1, spool_id=10)
        self.assertIn("Filament runout", text)
        self.assertIn("slot 1 (IFS channel 1)", text)
        self.assertIn("spool #10", text)
        self.assertIn("spool question", text)
        self.assertIn("Resume print", text)
        self.assertIn("/print-resume", text)
        self.assertIn("only when you press it", text)
        self.assertNotIn("I will not resume", text)

    def test_runout_message_without_slot_still_names_runout(self):
        text = self.message(runout=True)
        self.assertIn("Filament runout", text)
        self.assertIn("active slot", text)

    def test_generic_message_unchanged(self):
        text = self.message()
        self.assertTrue(text.startswith("Print paused:"))
        self.assertNotIn("Filament runout", text)
        self.assertIn("Resume or Cancel", text)


class ListenerRunoutCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hardware = Path(self.tmp.name) / "hardware.json"
        self.hardware.write_text(json.dumps({"active_slot": 1, "filament_slots": {
            "1": {"spoolman_id": 10, "material": "PLA", "color": "white"}}}))
        self.old_hardware = mel.HARDWARE_STATE
        mel.HARDWARE_STATE = self.hardware

    def tearDown(self):
        mel.HARDWARE_STATE = self.old_hardware
        self.tmp.cleanup()

    def listener(self, slots, head, native=True):
        lis = mel.Listener()
        lis.state, lis.filename = "printing", "a.gcode"
        lis.pause_message = ""

        async def read_slots():
            return {}, slots
        if native:
            lis.read_slots = read_slots
        else:
            async def boom():
                raise AssertionError("stock detail read on a non-native printer")
            lis.read_slots = boom
        patches = [
            mock.patch.object(mel.printers, "current",
                              return_value=S(has_native_screen=native)),
            mock.patch.object(mel, "read_head_sensor", lambda: head),
            mock.patch.object(mel, "log"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return lis


class ClassifyRunoutTests(ListenerRunoutCase):
    async def test_head_sensor_runout_with_empty_pause_message(self):
        # The #442 case exactly: message "", slot still loaded, head sensor empty.
        lis = self.listener({1: True}, head=False)
        self.assertEqual(await lis.classify_runout(), (True, 1))

    async def test_slot_emptied_is_a_runout_without_sensor_evidence(self):
        lis = self.listener({1: False}, head=None)
        self.assertEqual(await lis.classify_runout(), (True, 1))

    async def test_no_evidence_stays_generic(self):
        lis = self.listener({1: True}, head=None)
        is_runout, _slot = await lis.classify_runout()
        self.assertFalse(is_runout)

    async def test_loaded_head_and_loaded_slot_stays_generic(self):
        lis = self.listener({1: True}, head=True)
        is_runout, _slot = await lis.classify_runout()
        self.assertFalse(is_runout)

    async def test_generic_moonraker_printer_uses_the_head_sensor(self):
        lis = self.listener({}, head=False, native=False)
        self.assertEqual(await lis.classify_runout(), (True, 1))


class SlotlessQuestionTests(ListenerRunoutCase):
    async def test_a_slotless_runout_never_suspends_usage_or_persists_a_question(self):
        """P2-170: on a slotless printer the runout evidence names no slot,
        and a persisted slot question could never be answered with a spool
        choice (apply_runout_choice refuses a slotless record) — while
        suspending usage stops charging EVERY spool until it is answered.
        The listener posts one plain notice instead and leaves usage
        running."""
        from print_handy.runtime import spool_sync

        self.hardware.write_text(json.dumps({"spool": {"spoolman_id": 10}}))
        lis = self.listener({}, head=False, native=False)
        self.assertEqual(await lis.classify_runout(), (True, None))
        state = Path(self.tmp.name) / "runout_pause.json"
        with mock.patch.object(mel, "RUNOUT_PAUSE_STATE", state), \
                mock.patch.object(spool_sync, "suspend_usage") as suspend, \
                mock.patch.object(mel, "post") as post:
            record = await lis.open_question(None, "runout")
        self.assertIsNone(record)
        suspend.assert_not_called()
        self.assertFalse(state.exists())
        post.assert_called_once()
        self.assertIn("no IFS slot", post.call_args[0][0])


class PauseTransitionTests(ListenerRunoutCase):
    async def run_pause(self, lis):
        with mock.patch.object(mel, "post_alert") as post_alert, \
                mock.patch.object(mel, "post") as post, \
                mock.patch.object(mel, "run_announcer"), \
                mock.patch.object(print_stop, "status",
                                  return_value=st("paused", layer=1, total=9)), \
                mock.patch.object(print_control, "resume") as resume:
            lis.open_question = mock.AsyncMock(return_value={"slot": 1})
            await lis.on_status({"state": "paused", "message": ""}, "event")
        return post_alert, post, resume

    async def test_runout_pause_posts_runout_alert_and_question(self):
        lis = self.listener({1: True}, head=False)
        post_alert, _post, resume = await self.run_pause(lis)
        post_alert.assert_called_once()
        text, kind, _filename = post_alert.call_args[0]
        self.assertEqual(kind, "paused")
        self.assertIn("Filament runout", text)
        self.assertIn("slot 1 (IFS channel 1)", text)
        self.assertIn("spool #10", text)
        self.assertIn("Resume print", text)
        lis.open_question.assert_awaited_once_with(1, "runout")
        self.assertTrue(lis.last_pause_was_runout)
        resume.assert_not_called()  # nothing resumes without the operator's press

    async def test_generic_pause_stays_generic(self):
        lis = self.listener({1: True}, head=None)
        post_alert, _post, _resume = await self.run_pause(lis)
        post_alert.assert_called_once()
        text = post_alert.call_args[0][0]
        self.assertTrue(text.startswith("Print paused:"))
        self.assertNotIn("Filament runout", text)
        lis.open_question.assert_not_awaited()
        self.assertFalse(lis.last_pause_was_runout)


class EmptiedUpgradeTests(ListenerRunoutCase):
    async def poll(self, lis, slots):
        async def read_slots():
            return {"status": "paused"}, slots
        lis.read_slots = read_slots
        await lis.poll_native_presence()

    def paused_listener(self):
        lis = mel.Listener()
        lis.state, lis.filename = "paused", "a.gcode"
        lis.open_question = mock.AsyncMock(return_value={"slot": 1})
        patches = [
            mock.patch.object(mel.printers, "current",
                              return_value=S(has_native_screen=True)),
            mock.patch.object(mel, "log"),
            mock.patch.object(print_stop, "status",
                              return_value=st("paused", layer=1, total=9)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return lis

    async def test_emptied_during_generic_pause_upgrades_the_alert(self):
        lis = self.paused_listener()
        with mock.patch.object(mel, "post_alert") as post_alert:
            await self.poll(lis, {1: True})
            await self.poll(lis, {1: True})
            await self.poll(lis, {1: False})
            post_alert.assert_not_called()  # one empty sample is not a transition yet
            await self.poll(lis, {1: False})
            post_alert.assert_called_once()
            text, kind, _filename = post_alert.call_args[0]
            self.assertEqual(kind, "paused")
            self.assertIn("Filament runout", text)
            self.assertIn("slot 1 (IFS channel 1)", text)
            lis.open_question.assert_awaited_once_with(1, "runout")
            self.assertTrue(lis.last_pause_was_runout)
            await self.poll(lis, {1: False})
            post_alert.assert_called_once()  # no repeat posts for the same event

    async def test_emptied_while_printing_only_logs(self):
        lis = self.paused_listener()
        lis.state = "printing"
        with mock.patch.object(mel, "post_alert") as post_alert:
            await self.poll(lis, {1: True})
            await self.poll(lis, {1: True})
            await self.poll(lis, {1: False})
            await self.poll(lis, {1: False})
        post_alert.assert_not_called()
        lis.open_question.assert_not_awaited()

    async def test_emptied_after_runout_classification_does_not_repost(self):
        lis = self.paused_listener()
        lis.last_pause_was_runout = True
        lis.slot_tracker.confirmed = {1: True}
        lis.slot_tracker.candidate = {1: True}
        lis.slot_tracker.counts = {1: 5}
        with mock.patch.object(mel, "post_alert") as post_alert:
            await self.poll(lis, {1: False})
            await self.poll(lis, {1: False})
        post_alert.assert_not_called()
        lis.open_question.assert_not_awaited()


class ResumeMachineryTests(unittest.TestCase):
    def test_paused_alert_components_offer_resume_and_cancel(self):
        comps = pab.components("abcd1234", "paused")
        buttons = comps[0]["components"]
        labels = {b["label"]: b["custom_id"] for b in buttons}
        self.assertEqual(labels["Resume print"], "pbalert:resume:abcd1234")
        self.assertEqual(labels["Cancel print"], "pbalert:cancel:abcd1234")

    def test_resume_fires_only_on_the_button_click(self):
        alert = {"kind": "paused", "filename": "a.gcode"}
        with mock.patch.object(print_control, "status", return_value=st("paused")), \
                mock.patch.object(print_control, "resume",
                                  return_value=(True, "Resumed.")) as resume:
            pab.components("abcd1234", "paused")  # posting the alert must not resume
            resume.assert_not_called()
            reply = pab.handle_click("resume", alert)
            resume.assert_called_once_with("a.gcode")
        self.assertEqual(reply, "Resumed.")


if __name__ == "__main__":
    unittest.main()


class RunoutAlertPromisesOnlyWhatItDelivers(unittest.TestCase):
    def test_slotless_alert_does_not_promise_a_spool_question(self):
        text = mel.paused_message("a.gcode", runout=True, slot=None)
        self.assertNotIn("spool question", text)
        self.assertIn("1. Load the replacement filament", text)
        self.assertIn("2. Tap Resume print", text)

    def test_alert_with_a_slot_still_asks_the_spool_question(self):
        text = mel.paused_message("a.gcode", runout=True, slot=3)
        self.assertIn("slot 3 (IFS channel 3)", text)
        self.assertIn("2. Answer the spool question", text)
        self.assertIn("3. Tap Resume print", text)

    def test_step_two_names_the_buttons_the_spool_question_shows(self):
        """Nov 2026: step 2 said "Existing spool or New spool" while a runout
        on a known spool shows five buttons. The text names the answers the
        question really offers."""
        def answers(spool_id):
            rows = pab.spool_question_components("a1", prior_spool_id=spool_id, cause="runout")
            return [b["label"].replace(" — ", ", ") for b in rows[0]["components"] if b["label"] != "Cancel print"]

        known = mel.paused_message("a.gcode", runout=True, slot=3, spool_id=12)
        for label in answers(12):
            self.assertIn(label, known)
        unknown = mel.paused_message("a.gcode", runout=True, slot=3, spool_id=None)
        self.assertIn("(Existing spool or New spool)", unknown)
        self.assertEqual(answers(None), ["Existing spool", "New spool"])


class ScreenlessChannelNamesTheSlot(ListenerRunoutCase):
    """2026-11-04: screenless AD5X, no launch record, no saved active slot, every slot still
    read loaded, head sensor empty. Klipper's zmod_ifs.Chan said 3 the whole time."""

    def test_active_channel_reading(self):
        self.assertEqual(mel.zmod_active_channel({"zmod_ifs": {"Chan": 3}}), 3)
        self.assertEqual(mel.zmod_active_channel({"zmod_color": {"channel": 2}}), 2)
        self.assertEqual(mel.zmod_active_channel({"zmod_ifs": {"Chan": 0}, "zmod_color": {"channel": 4}}), 4)
        for status in ({"zmod_ifs": {"Chan": 0}}, {"zmod_ifs": {"Chan": 5}}, {"zmod_ifs": {"Chan": True}},
                       {"zmod_ifs": "x"}, {}, None):
            self.assertIsNone(mel.zmod_active_channel(status), status)

    async def test_channel_names_the_slot_when_nothing_else_does(self):
        self.hardware.write_text(json.dumps({"active_slot": None, "filament_slots": {}}))
        lis = self.listener({1: True, 2: True, 3: True, 4: True}, head=False)

        async def read_slots():
            lis.ifs_channel = 3
            return {}, {1: True, 2: True, 3: True, 4: True}
        lis.read_slots = read_slots
        self.assertEqual(await lis.classify_runout(), (True, 3))

    async def test_presence_evidence_still_beats_the_channel(self):
        lis = self.listener({1: True, 2: False}, head=False)
        lis.slot_tracker.observe({1: True, 2: True})  # slot 2 was loaded when the print started

        async def read_slots():
            lis.ifs_channel = 1
            return {}, {1: True, 2: False}
        lis.read_slots = read_slots
        self.assertEqual(await lis.classify_runout(), (True, 2))


class InfinityFailoverMovesUsage(ListenerRunoutCase):
    """Zmod Infinity loads a similar spool from another slot with no pause; usage must follow it."""

    def test_watch_debounces_and_reports_a_stable_switch(self):
        w = mel.FailoverChannelWatch()
        self.assertIsNone(w.observe(3))
        self.assertIsNone(w.observe(None))
        self.assertIsNone(w.observe(2))   # one poll is a blip
        self.assertIsNone(w.observe(3))
        self.assertIsNone(w.observe(2))
        self.assertEqual(w.observe(2), (3, 2))
        self.assertIsNone(w.observe(2))

    def test_target_only_for_the_charged_single_spool(self):
        mapped = {1: 16, 2: 19, 3: 20, 4: 12}.get
        self.assertEqual(mel.failover_usage_target(3, 2, 20, mapped), 19)
        self.assertIsNone(mel.failover_usage_target(3, 2, None, mapped))   # multi-slot job: no spool charged
        self.assertIsNone(mel.failover_usage_target(3, 2, 16, mapped))     # charging some other spool
        self.assertIsNone(mel.failover_usage_target(3, 5, 20, mapped))     # replacement slot unmapped
        self.assertIsNone(mel.failover_usage_target(3, 2, 20, {3: 20, 2: 20}.get))

    async def test_follow_failover_repoints_usage_and_says_so(self):
        from print_handy.runtime import spool_sync
        self.hardware.write_text(json.dumps({"filament_slots": {
            "2": {"spoolman_id": 19}, "3": {"spoolman_id": 20}}}))
        lis = self.listener({}, head=None)
        posts = []
        with mock.patch.object(spool_sync, "active_usage_spool", return_value=20), \
             mock.patch.object(spool_sync, "resume_usage") as resume, \
             mock.patch.object(mel, "post", posts.append):
            await lis.follow_failover((3, 2))
        resume.assert_called_once_with(19)
        self.assertIn("spool #19", posts[0])
        # #1332: the failover post names each slot with its base.
        self.assertIn("slot 3 (IFS channel 3) ran out", posts[0])
        self.assertIn("to slot 2 (IFS channel 2) on its own", posts[0])

    def test_watch_retry_reports_the_switch_again(self):
        w = mel.FailoverChannelWatch()
        w.observe(3)
        w.observe(2)
        change = w.observe(2)
        self.assertEqual(change, (3, 2))
        w.retry(change)
        self.assertIsNone(w.observe(2))
        self.assertEqual(w.observe(2), (3, 2))

    async def test_failed_usage_read_is_retried_not_dropped(self):
        # Review of #878: a transient error must not consume the switch.
        from print_handy.runtime import spool_sync
        lis = self.listener({}, head=None)
        lis.failover_watch = mel.FailoverChannelWatch()
        for ch in (3, 2, 2):
            change = lis.failover_watch.observe(ch)
        with mock.patch.object(spool_sync, "active_usage_spool", side_effect=OSError("down")), \
             mock.patch.object(mel, "post"):
            await lis.follow_failover(change)
        self.assertEqual(lis.failover_watch.channel, 3)

    async def test_follow_failover_leaves_a_multi_slot_job_alone(self):
        from print_handy.runtime import spool_sync
        lis = self.listener({}, head=None)
        with mock.patch.object(spool_sync, "active_usage_spool", return_value=None), \
             mock.patch.object(spool_sync, "resume_usage") as resume, \
             mock.patch.object(mel, "post") as post:
            await lis.follow_failover((1, 2))
        resume.assert_not_called()
        post.assert_not_called()
