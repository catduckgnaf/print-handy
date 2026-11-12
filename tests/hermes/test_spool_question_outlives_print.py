"""A runout spool question that outlives its print (2026-11-12 live run).

Built from a live record: a runout on slot 3 opened the spool question
(prior spool #21) and it was never answered. For four days the reminder
was re-posted every SPOOL_NAG_SECONDS (619 reminders) and filament usage
stayed suspended, while the spool reconcile kept reporting slot 3 bound to
spool #21 and new prints started normally. The question's own refeed rule
(#1063) already says the spool that ran out showing back in its slot is
the refeed answer.

Pinned here: once the question's print is over, a print start or a spool
reconcile that finds the slot still bound to #21 and loaded closes the
question as a refeed; a different or unknown spool leaves it open, the
reminders back off, and a new print gets one warn-only notice. During the
paused print itself nothing changes. Offline: every printer, Spoolman and
chat call is a stand-in.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

from print_handy.runtime import moonraker_event_listener as mel
from print_handy.runtime import spool_sync

DAY = 24 * 60 * 60


def live_record(now):
    """The live runout_pause.json, opened four days ago, last reminded a minute ago."""
    return {"filename": "earlier plate.gcode", "slot": 3, "prior_spool_id": 21,
            "cause": "runout", "usage_suspended": True, "suspended_spool_id": 21,
            "recorded_at": now - 4 * DAY, "last_nag_at": now - 11 * 60, "nags": 619}


class QuestionCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "runout_pause.json"
        self.now = time.time()
        self.state.write_text(json.dumps(live_record(self.now)))
        self.bound = {3: 21}
        self.presence = {1: True, 2: True, 3: True, 4: True}
        self.resume = mock.Mock()
        self.post = mock.Mock(return_value=True)
        self.nag_post = mock.Mock(return_value=True)
        patches = [
            mock.patch.object(mel, "RUNOUT_PAUSE_STATE", self.state),
            mock.patch.object(mel, "_mapped_spool", lambda slot: self.bound.get(slot)),
            mock.patch.object(mel, "post", self.post),
            mock.patch.object(mel, "post_spool_question", self.nag_post),
            mock.patch.object(mel.printers, "current", return_value=S(has_native_screen=True)),
            mock.patch.object(spool_sync, "resume_usage", self.resume),
            mock.patch.object(mel, "log"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def listener(self):
        lis = mel.Listener()

        async def read_slots():
            return {}, dict(self.presence)
        lis.read_slots = read_slots
        return lis

    def record(self):
        return json.loads(self.state.read_text()) if self.state.exists() else None

    def posted(self):
        return [call.args[0] for call in self.post.call_args_list]


class ReconcileTests(QuestionCase):
    async def reconcile(self, lis, stats):
        def fake_call(path):
            assert "print_stats" in path
            return {"result": {"status": {"print_stats": stats}}}

        import print_handy.listeners as listeners_mod
        from print_handy.core import model_job
        with mock.patch.object(spool_sync, "reconcile",
                               return_value={"ok": True, "assignments": {3: 21}}), \
                mock.patch.object(spool_sync, "reconcile_text", return_value="slot 3 -> spool #21"), \
                mock.patch.object(mel.print_control, "_call", side_effect=fake_call), \
                mock.patch.object(model_job, "retry_pending_spool_sync", return_value=False), \
                mock.patch.object(listeners_mod, "instance_printer_id", return_value=None):
            await lis.reconcile_spools()

    async def test_reconcile_closes_the_question_as_a_refeed_when_the_same_spool_is_back(self):
        await self.reconcile(self.listener(), {"state": "standby", "filename": ""})
        self.assertIsNone(self.record(), "the question must close")
        self.resume.assert_called_once()
        self.assertEqual(self.resume.call_args.args[0], 21)
        posts = self.posted()
        self.assertEqual(len(posts), 1)
        self.assertIn("refeed", posts[0])
        self.assertIn("#21", posts[0])

    async def test_reconcile_leaves_a_different_spool_open_and_backs_off(self):
        self.bound = {3: 16}
        await self.reconcile(self.listener(), {"state": "standby", "filename": ""})
        record = self.record()
        self.assertIsNotNone(record)
        self.assertTrue(record["print_over"])
        self.assertEqual(record["nags"], 619)
        self.resume.assert_not_called()
        self.assertEqual(self.posted(), [])

    async def test_reconcile_leaves_an_unknown_binding_or_empty_slot_open(self):
        self.bound = {}
        await self.reconcile(self.listener(), {"state": "standby", "filename": ""})
        self.assertIsNotNone(self.record())
        self.bound = {3: 21}
        self.presence[3] = False
        await self.reconcile(self.listener(), {"state": "standby", "filename": ""})
        self.assertIsNotNone(self.record())
        self.resume.assert_not_called()
        self.assertEqual(self.posted(), [])

    async def test_reconcile_never_touches_the_question_during_its_own_paused_print(self):
        await self.reconcile(self.listener(), {"state": "paused", "filename": "earlier plate.gcode",
                                               "total_duration": 4 * DAY + 600})
        record = self.record()
        self.assertEqual(record, live_record(self.now) | {"recorded_at": record["recorded_at"],
                                                          "last_nag_at": record["last_nag_at"]})
        self.assertNotIn("print_over", record)
        self.resume.assert_not_called()


class PrintStartTests(QuestionCase):
    async def start(self, lis):
        lis.state, lis.filename = "standby", "earlier plate.gcode"
        lis._begin_capture = mock.AsyncMock()
        lis.light_print_started = mock.AsyncMock()
        lis._slot_self_check = mock.AsyncMock()
        lis._settle_launch_lease = mock.Mock()
        lis._note_watch_not_running = mock.Mock()
        with mock.patch.object(mel, "run_announcer"), \
                mock.patch.object(mel, "start_claimed", return_value=False), \
                mock.patch.object(mel, "_claim_notification", return_value=True), \
                mock.patch.object(mel, "_record_transition_markers"), \
                mock.patch.object(mel, "post_exclude_objects"), \
                mock.patch.object(mel, "_live_auto_on_print_start"), \
                mock.patch.object(mel, "start_message", return_value="Print started: plate 2"):
            await lis.on_status({"state": "printing", "filename": "plate two.gcode"}, "event")

    async def test_a_new_print_with_the_same_spool_back_closes_the_question(self):
        await self.start(self.listener())
        self.assertIsNone(self.record(), "the question must close")
        # The new print's launch owns the usage pointer: it is not re-pointed.
        self.resume.assert_not_called()
        posts = self.posted()
        self.assertEqual(posts[0], "Print started: plate 2")
        self.assertEqual(len([p for p in posts if "refeed" in p]), 1)
        self.assertFalse(any("Spool question still open" in p for p in posts))

    async def test_a_new_print_with_a_different_spool_gets_one_warn_only_notice(self):
        self.bound = {3: 16}
        lis = self.listener()
        await self.start(lis)
        self.assertEqual(lis.state, "printing")  # never held
        record = self.record()
        self.assertTrue(record["print_over"])
        notices = [p for p in self.posted() if "Spool question still open" in p]
        self.assertEqual(len(notices), 1)
        self.assertIn("filament usage is paused until it is answered", notices[0])
        self.assertIn("IFS slot 3", notices[0])
        self.resume.assert_not_called()


class NagTests(QuestionCase):
    async def test_reminders_back_off_once_the_question_print_is_over(self):
        lis = self.listener()
        lis.state, lis.filename = "standby", ""
        await lis.nag_if_due()
        self.nag_post.assert_not_called()
        self.assertEqual(self.record()["nags"], 619)
        record = self.record()
        record["last_nag_at"] = self.now - mel.SPOOL_NAG_ENDED_SECONDS - 1
        self.state.write_text(json.dumps(record))
        await lis.nag_if_due()
        self.nag_post.assert_called_once()
        self.assertEqual(self.record()["nags"], 620)

    async def test_a_different_print_running_also_backs_off(self):
        lis = self.listener()
        lis.state, lis.filename = "printing", "plate two.gcode"
        await lis.nag_if_due()
        self.nag_post.assert_not_called()

    async def test_the_paused_print_itself_keeps_the_short_reminder(self):
        lis = self.listener()
        lis.state, lis.filename = "paused", "earlier plate.gcode"
        await lis.nag_if_due()
        self.nag_post.assert_called_once()
        self.assertEqual(self.record()["nags"], 620)

    async def test_the_ended_print_mark_backs_off_even_for_a_reprint_of_the_same_file(self):
        record = self.record()
        record["print_over"] = True
        self.state.write_text(json.dumps(record))
        lis = self.listener()
        lis.state, lis.filename = "printing", "earlier plate.gcode"
        await lis.nag_if_due()
        self.nag_post.assert_not_called()


class RuleTests(unittest.TestCase):
    def test_refeed_rule_needs_the_same_spool_bound_and_the_slot_loaded(self):
        record = live_record(time.time())
        self.assertTrue(mel.refeed_settles_question(record, 21, True))
        self.assertFalse(mel.refeed_settles_question(record, 16, True))
        self.assertFalse(mel.refeed_settles_question(record, None, True))
        self.assertFalse(mel.refeed_settles_question(record, 21, False))
        self.assertFalse(mel.refeed_settles_question(record, 21, None))
        self.assertFalse(mel.refeed_settles_question(record | {"cause": "swap"}, 21, True))

    def test_a_reprint_of_the_same_file_is_not_the_question_print(self):
        now = time.time()
        record = live_record(now)
        self.assertTrue(mel.spool_question_print_is_current(
            record, "paused", "earlier plate.gcode", 4 * DAY + 60, now=now))
        self.assertFalse(mel.spool_question_print_is_current(
            record, "printing", "earlier plate.gcode", 300, now=now))
        self.assertFalse(mel.spool_question_print_is_current(
            record, "standby", "earlier plate.gcode", None, now=now))


if __name__ == "__main__":
    unittest.main()
