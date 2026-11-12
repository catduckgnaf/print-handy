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


class EscalationTests(unittest.TestCase):
    """#1020 B3/C4: budget-exhausted critical, bundle attach, pin hook."""

    def _down(self, **kw):
        return {"p1": {"required": True, "ok": False,
                       "reason": "listener heartbeat stale or invalid", **kw}}

    def test_critical_replaces_the_down_notice_when_the_budget_is_spent(self):
        state = {"p1": {"since": 0.0, "notified_at": None}}
        messages, new_state = outage_alert.decide(self._down(), state, 1000.0,
                                                  exhausted={"p1"})
        self.assertEqual([m[1] for m in messages], ["critical"])
        text = messages[0][2]
        self.assertIn("exhausted its restart budget", text)
        self.assertIn("/print-handy bundle", text)
        self.assertIn("python3 -m print_handy.diagnostics_bundle", text)
        self.assertIn("critical_at", new_state["p1"])
        # Once per outage: a later tick with the returned state stays quiet.
        again, _ = outage_alert.decide(self._down(), new_state, 1100.0, exhausted={"p1"})
        self.assertEqual(again, [])

    def test_no_critical_without_exhaustion(self):
        state = {"p1": {"since": 0.0, "notified_at": None}}
        messages, _ = outage_alert.decide(self._down(), state, 1000.0)
        self.assertEqual([m[1] for m in messages], ["down"])

    def test_still_down_names_exhaustion_after_the_critical(self):
        state = {"p1": {"since": 0.0, "notified_at": 100.0, "critical_at": 100.0}}
        messages, _ = outage_alert.decide(self._down(), state, 100.0 + 3601,
                                          exhausted={"p1"})
        self.assertEqual([m[1] for m in messages], ["still_down"])
        self.assertIn("Automatic restarts are exhausted", messages[0][2])

    def test_failed_critical_is_withheld_and_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            outage_alert.save(tmp, {"p1": {"since": 0.0, "notified_at": None}})
            sent = []
            outage_alert.tick(self._down(), tmp, lambda pid, text: sent.append(text) or False,
                              now=1000.0, exhausted={"p1"})
            state = outage_alert.load(tmp)
            self.assertNotIn("critical_at", state["p1"])
            outage_alert.tick(self._down(), tmp, lambda pid, text: True,
                              now=1010.0, exhausted={"p1"})
            state = outage_alert.load(tmp)
            self.assertIn("critical_at", state["p1"])

    def test_attach_adds_the_bundle_to_failure_notices_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            outage_alert.save(tmp, {"p1": {"since": 0.0, "notified_at": None}})
            sent = []
            outage_alert.tick(self._down(), tmp, lambda pid, text: sent.append(text) or True,
                              now=1000.0, attach=lambda kind: "/state/diagnostics-bundle.md")
            self.assertIn("MEDIA:/state/diagnostics-bundle.md", sent[0])
            sent.clear()
            outage_alert.tick({"p1": {"required": True, "ok": True, "reason": "alive"}}, tmp,
                              lambda pid, text: sent.append(text) or True, now=2000.0,
                              attach=lambda kind: "/state/diagnostics-bundle.md")
            self.assertEqual(len(sent), 1)
            self.assertNotIn("MEDIA:", sent[0])

    def test_pin_runs_after_a_delivered_critical(self):
        with tempfile.TemporaryDirectory() as tmp:
            outage_alert.save(tmp, {"p1": {"since": 0.0, "notified_at": None}})
            pinned = []
            outage_alert.tick(self._down(), tmp, lambda pid, text: True, now=1000.0,
                              exhausted={"p1"}, pin=pinned.append)
            self.assertEqual(pinned, ["p1"])


class DegradedNoticeTests(unittest.TestCase):
    """#1020 B4: one degraded notice per printer-reported state."""

    def test_pause_error_stop_each_notice_once(self):
        state = {}
        text = outage_alert.degraded_notice(state, "p1", "paused",
                                            filename="benchy.gcode", now=10.0)
        self.assertIn("**paused**", text)
        self.assertIn("on benchy.", text)
        self.assertIn("direct check of the printer", text)
        self.assertIsNone(outage_alert.degraded_notice(
            state, "p1", "paused", filename="benchy.gcode", now=20.0))
        text = outage_alert.degraded_notice(state, "p1", "error", now=30.0)
        self.assertIn("**error**", text)

    def test_printing_and_unknown_states_never_notice(self):
        state = {}
        for reported in ("printing", "standby", "complete", "", None):
            self.assertIsNone(outage_alert.degraded_notice(state, "p1", reported, now=1.0))
        self.assertEqual(state, {})

    def test_state_round_trips_through_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = {}
            outage_alert.degraded_notice(state, "p1", "cancelled", now=5.0)
            outage_alert.save_degraded(tmp, state)
            loaded = {k: v for k, v in outage_alert.load_degraded(tmp).items()
                      if isinstance(v, dict)}
            self.assertEqual(loaded["p1"]["state"], "cancelled")
