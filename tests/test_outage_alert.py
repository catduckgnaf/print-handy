"""Offline coverage for the listener outage notices (#1020)."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from print_handy.bot import outage_alert  # noqa: E402

DOWN = {"p1": {"required": True, "ok": False, "reason": "listener heartbeat stale or invalid"}}
UP = {"p1": {"required": True, "ok": True, "reason": "listener event loop alive"}}
UNKNOWN = {"p1": {"required": True, "ok": None, "reason": "unknown"}}
NOT_REQUIRED = {"p1": {"required": False, "ok": None, "reason": "no listener"}}


class DecideTests(unittest.TestCase):
    def test_no_notice_inside_the_grace_period(self):
        messages, state = outage_alert.decide(DOWN, {}, 1000)
        self.assertEqual([], messages)
        self.assertEqual({"p1": {"since": 1000, "notified_at": None}}, state)
        messages, state = outage_alert.decide(DOWN, state, 1000 + outage_alert.DOWN_AFTER_S - 1)
        self.assertEqual([], messages)

    def test_one_down_notice_then_silence_until_the_reminder(self):
        _, state = outage_alert.decide(DOWN, {}, 1000)
        messages, state = outage_alert.decide(DOWN, state, 1000 + outage_alert.DOWN_AFTER_S)
        self.assertEqual(["down"], [kind for _p, kind, _t in messages])
        self.assertIn("will NOT be posted", messages[0][2])
        again, state = outage_alert.decide(DOWN, state, 1000 + outage_alert.DOWN_AFTER_S + 600)
        self.assertEqual([], again)
        remind, state = outage_alert.decide(DOWN, state, 1000 + outage_alert.DOWN_AFTER_S + outage_alert.REMIND_EVERY_S)
        self.assertEqual(["still_down"], [kind for _p, kind, _t in remind])

    def test_recovery_notice_only_after_a_down_notice(self):
        _, state = outage_alert.decide(DOWN, {}, 1000)
        quiet, state_after = outage_alert.decide(UP, state, 1060)
        self.assertEqual([], quiet)
        self.assertEqual({}, state_after)
        sent, notified = outage_alert.decide(DOWN, state, 1000 + outage_alert.DOWN_AFTER_S)
        self.assertEqual(1, len(sent))
        back, cleared = outage_alert.decide(UP, notified, 5000)
        self.assertEqual(["up"], [kind for _p, kind, _t in back])
        self.assertEqual({}, cleared)

    def test_unknown_keeps_the_outage_and_not_required_is_ignored(self):
        _, state = outage_alert.decide(DOWN, {}, 1000)
        messages, state = outage_alert.decide(UNKNOWN, state, 1100)
        self.assertEqual([], messages)
        self.assertIn("p1", state)
        self.assertEqual(([], {}), outage_alert.decide(NOT_REQUIRED, {}, 1000))


class TickTests(unittest.TestCase):
    def test_failed_send_is_retried_next_tick_and_restart_does_not_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            sent = []
            outage_alert.tick(DOWN, directory, lambda p, t: sent.append(t) or True, now=1000)
            self.assertEqual([], sent)
            self.assertEqual(1, len(outage_alert.tick(
                DOWN, directory, lambda p, t: False, now=1000 + outage_alert.DOWN_AFTER_S)))
            self.assertEqual([], sent)
            outage_alert.tick(DOWN, directory, lambda p, t: sent.append(t) or True, now=1000 + outage_alert.DOWN_AFTER_S + 15)
            self.assertEqual(1, len(sent), "the failed notice is retried")
            # a "restarted bot" reloads state from disk and does not repeat it
            outage_alert.tick(DOWN, directory, lambda p, t: sent.append(t) or True, now=1000 + outage_alert.DOWN_AFTER_S + 30)
            self.assertEqual(1, len(sent))
            outage_alert.tick(UP, directory, lambda p, t: sent.append(t) or True, now=9000)
            self.assertEqual(2, len(sent))
            self.assertIn("are back", sent[-1])


if __name__ == "__main__":
    unittest.main()
