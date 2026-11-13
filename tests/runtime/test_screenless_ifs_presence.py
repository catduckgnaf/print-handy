"""ZMod screenless IFS presence and the runout-question fixes.

In screenless mode (save_variables display_off=1) the stock firmware is not
running, so its /detail API refuses every connection; slot presence comes from
Klipper's zmod_color instead. All printer state here is fake.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

from print_handy.runtime import moonraker_event_listener as mel
from print_handy.runtime import print_alert_buttons as pab
from print_handy.runtime import print_control


def zmod_status(display_off=1, present=(True, True, True, True)):
    """The live AD5X objects/query reply shape (ZMod 1.7.5-2, Helix, screenless)."""
    return {"result": {"status": {
        "zmod_color": {"ifs": True, "slots": [
            {"ID": str(i), "Material": "PLA", "Color": "white", "HEX": "FFFFFF", "hasFilament": p}
            for i, p in enumerate(present, start=1)]},
        "save_variables": {"variables": {"display_off": display_off}},
    }}}


class ListenerCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for name, value in (("NATIVE_ERROR_STATE", root / "native_error.json"),
                            ("HARDWARE_STATE", root / "hardware.json"),
                            ("RUNOUT_PAUSE_STATE", root / "runout.json")):
            patch = mock.patch.object(mel, name, value, create=True)
            patch.start()
            self.addCleanup(patch.stop)
        for patch in (mock.patch.object(mel.printers, "current", return_value=S(has_native_screen=True)),
                      mock.patch.object(pab, "open_insertion_slots", return_value=set(), create=True)):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def listener(self, state="printing"):
        lis = mel.Listener()
        lis.ifs_monitor = mel.FilamentInsertionMonitor(Path(self.tmp.name) / "edges.json",
                                                       Path(self.tmp.name) / "pending.json")
        lis.state, lis.filename = state, "a.gcode"
        return lis


class ScreenlessReadTests(ListenerCase):
    async def test_screenless_reads_zmod_color_and_never_the_stock_api(self):
        lis = self.listener()
        with mock.patch.object(print_control, "_call", return_value=zmod_status(1, (True, False, True, True))), \
                mock.patch("print_handy.core.model_job.read_stock_detail",
                           side_effect=AssertionError("stock /detail read in screenless mode")):
            detail, slots = await lis.read_slots()
        self.assertIsNone(detail)
        self.assertEqual(slots, {1: True, 2: False, 3: True, 4: True})
        self.assertEqual(lis.display_off, 1)

    async def test_stock_screen_mode_still_reads_the_stock_api(self):
        lis = self.listener()
        detail = {"status": "ready", "hasMatlStation": True}
        with mock.patch.object(print_control, "_call", return_value=zmod_status(0)), \
                mock.patch("print_handy.core.model_job.read_stock_detail", return_value=detail) as read, \
                mock.patch("print_handy.core.model_job.read_stock_filament_slots_from_detail", return_value={1: True}):
            self.assertEqual(await lis.read_slots(), (detail, {1: True}))
        read.assert_called_once()

    def test_zmod_slot_presence_rejects_malformed_lists(self):
        with self.assertRaises(RuntimeError):
            mel.zmod_slot_presence({"zmod_color": {"slots": [{"ID": "x", "hasFilament": True}]}})
        with self.assertRaises(RuntimeError):
            mel.zmod_slot_presence({})
        self.assertEqual(mel.zmod_slot_presence(zmod_status()["result"]["status"])[4], True)

    async def test_screenless_poll_tracks_mid_print_slot_events(self):
        lis = self.listener()
        lis.open_question = mock.AsyncMock()
        replies = [zmod_status(1, (True,) * 4)] * 2 + [zmod_status(1, (True, False, True, True))] * 2 \
            + [zmod_status(1, (True,) * 4)] * 2
        with mock.patch.object(print_control, "_call", side_effect=replies), \
                mock.patch.object(mel, "post") as post, mock.patch.object(mel, "log") as log:
            for _ in replies:
                await lis.poll_native_presence()
        lines = [c.args[0] for c in log.call_args_list]
        self.assertIn("mid-print slot 2 emptied", lines)
        self.assertIn("mid-print slot 2 refilled", lines)
        lis.open_question.assert_awaited_once_with(2, "swap")
        post.assert_not_called()  # no stock-screen error alert without the stock API
        self.assertFalse(any("failed" in line for line in lines))

    async def test_idle_screenless_insertion_prompts(self):
        lis = self.listener("standby")
        replies = [zmod_status(1, (True, False, True, True))] * 2 + [zmod_status(1, (True,) * 4)] * 2
        with mock.patch.object(print_control, "_call", side_effect=replies), \
                mock.patch.object(mel, "post_insertion_confirmation") as prompt, mock.patch.object(mel, "log"):
            for _ in replies:
                await lis.poll_native_presence()
        prompt.assert_called_once_with(2)

    async def test_repeated_failure_logs_one_line_without_traceback(self):
        lis = self.listener()
        seen = []

        def fake_log(line):
            seen.append((line, sys.exc_info()[0]))
        with mock.patch.object(print_control, "_call", side_effect=OSError("refused")), \
                mock.patch.object(mel, "log", fake_log):
            for _ in range(5):
                await lis.poll_native_presence()
        self.assertEqual(len(seen), 1)
        self.assertIn("presence poll failed", seen[0][0])
        self.assertIsNone(seen[0][1])  # logged outside the handler: no traceback attached
        with mock.patch.object(print_control, "_call", return_value=zmod_status()), \
                mock.patch.object(mel, "log", fake_log):
            await lis.poll_native_presence()
        self.assertIn("recovered", seen[-1][0])

    async def test_screenless_cancel_does_not_force_restart(self):
        lis = self.listener()
        lis.display_off = 1
        with mock.patch("print_handy.runtime.klipper_report.cancelled_by_bot", return_value=False), \
                mock.patch.object(mel, "screen_cancel_reboot") as reboot, mock.patch.object(mel, "log"):
            self.assertFalse(lis.start_screen_cancel_reboot("moonraker cancelled"))
        reboot.assert_not_called()


class RunoutQuestionTests(ListenerCase):
    async def poll(self, lis, slots):
        async def read_slots():
            return {"status": "printing"}, slots
        lis.read_slots = read_slots
        await lis.poll_native_presence()

    async def test_answered_runout_refill_does_not_ask_swap(self):
        lis = self.listener("paused")
        with mock.patch.object(mel, "log"), mock.patch.object(mel, "post_spool_question"), \
                mock.patch.object(mel, "post_alert"), \
                mock.patch("print_handy.runtime.spool_sync.suspend_usage", return_value=10) as suspend:
            for slots in ({1: True}, {1: True}, {1: False}, {1: False}):
                await self.poll(lis, slots)  # slot 1 empties while paused: the runout question opens
            self.assertEqual(mel.load_runout_pause()["slot"], 1)
            mel.RUNOUT_PAUSE_STATE.unlink()  # the operator answered; the plugin closed the question
            lis.state = "printing"
            for slots in ({1: True}, {1: True}):
                await self.poll(lis, slots)
        self.assertEqual(suspend.call_count, 1)  # usage is not suspended a second time
        self.assertFalse(mel.RUNOUT_PAUSE_STATE.exists())

    async def test_unasked_mid_print_swap_still_asks(self):
        lis = self.listener()
        lis.open_question = mock.AsyncMock()
        with mock.patch.object(mel, "log"):
            for slots in ({1: True}, {1: True}, {1: False}, {1: False}, {1: True}, {1: True}):
                await self.poll(lis, slots)
        lis.open_question.assert_awaited_once_with(1, "swap")


class NeverLoadedSlotTests(ListenerCase):
    async def test_never_loaded_slot_early_in_a_print_is_not_a_runout(self):
        lis = self.listener()
        lis.pause_message = ""

        async def read_slots():
            return None, {1: True, 2: True, 3: True, 4: False}
        lis.read_slots = read_slots
        with mock.patch.object(mel, "read_head_sensor", return_value=True), mock.patch.object(mel, "log"), \
                mock.patch.object(mel, "recovery_slot", return_value=(1, None)):
            is_runout, _slot = await lis.classify_runout()
        self.assertFalse(is_runout)

    def test_only_slots_seen_loaded_count_as_live_empty(self):
        tracker = mel.MidPrintSlotTracker()
        tracker.observe({1: True, 4: False})
        self.assertEqual(tracker.empty_slots({1: False, 4: False}), [1])


class IdleInsertionBaselineTests(unittest.TestCase):
    def monitor(self, root):
        return mel.FilamentInsertionMonitor(root / "edges.json", root / "pending.json")

    def test_startup_presence_warming_does_not_prompt_for_unchanged_spools(self):
        # 2026-10-30 reboot: four loaded spools persisted, ZMod first reported
        # them absent, then present. That is a reboot, not four insertions.
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'edges.json').write_text(json.dumps({str(i): True for i in range(1, 5)}))
            monitor = self.monitor(Path(tmp))
            self.assertEqual(monitor.observe({i: False for i in range(1, 5)}, idle=True), [])
            # ZMod may first report absent before its real IFS readings arrive.
            self.assertEqual(monitor.observe({i: True for i in range(1, 5)}, idle=True), [])
            self.assertEqual(monitor.observe({i: True for i in range(1, 5)}, idle=True), [])
            self.assertEqual(json.loads((Path(tmp) / "edges.json").read_text()),
                             {str(i): True for i in range(1, 5)})

    def test_delayed_initialization_multiple_absent_samples_does_not_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'edges.json').write_text(json.dumps({str(i): True for i in range(1, 5)}))
            monitor = self.monitor(root)
            for _ in range(4):
                self.assertEqual(monitor.observe({i: False for i in range(1, 5)}, idle=True), [])
            for _ in range(4):
                self.assertEqual(monitor.observe({i: True for i in range(1, 5)}, idle=True), [])

    def test_empty_slots_eventually_become_a_valid_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'edges.json').write_text(json.dumps({str(i): True for i in range(1, 5)}))
            with mock.patch.object(mel.time, 'monotonic', return_value=100.0) as clock:
                monitor = self.monitor(root)
                empty = {i: False for i in range(1, 5)}
                monitor.observe(empty, idle=True)
                clock.return_value = 221.0
                monitor.observe(empty, idle=True)
                self.assertEqual(json.loads((root / 'edges.json').read_text()),
                                 {str(i): False for i in range(1, 5)})

    def test_a_reading_change_mid_settle_restarts_the_settle_window(self):
        """P2-271: the settle window measures how long the CURRENT reading
        has held. A disagreement that changes shape mid-window must not be
        adopted on the first reading's clock — adopting {T,F} at t=121 on
        a two-second-old reading later produced a false insertion prompt
        for slot 2 when the readings settled back to {T,T}."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'edges.json').write_text(json.dumps({"1": True, "2": True}))
            with mock.patch.object(mel.time, 'monotonic') as clock:
                monitor = self.monitor(root)
                clock.return_value = 0.0
                self.assertEqual(monitor.observe({1: False, 2: False}, idle=True), [])
                clock.return_value = 119.0
                self.assertEqual(monitor.observe({1: True, 2: False}, idle=True), [])
                clock.return_value = 121.0
                self.assertEqual(monitor.observe({1: True, 2: False}, idle=True), [])
                self.assertEqual(json.loads((root / 'edges.json').read_text()),
                                 {"1": True, "2": True})
                clock.return_value = 122.0
                self.assertEqual(monitor.observe({1: True, 2: True}, idle=True), [])
                clock.return_value = 123.0
                self.assertEqual(monitor.observe({1: True, 2: True}, idle=True), [])
                self.assertEqual(json.loads((root / 'edges.json').read_text()),
                                 {"1": True, "2": True})

    def test_a_changed_reading_that_then_holds_is_adopted_after_its_own_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'edges.json').write_text(json.dumps({"1": True, "2": True}))
            with mock.patch.object(mel.time, 'monotonic') as clock:
                monitor = self.monitor(root)
                clock.return_value = 0.0
                monitor.observe({1: False, 2: False}, idle=True)
                clock.return_value = 119.0
                monitor.observe({1: True, 2: False}, idle=True)
                clock.return_value = 238.0
                monitor.observe({1: True, 2: False}, idle=True)
                self.assertEqual(json.loads((root / 'edges.json').read_text()),
                                 {"1": True, "2": True})
                clock.return_value = 239.0
                monitor.observe({1: True, 2: False}, idle=True)
                self.assertEqual(json.loads((root / 'edges.json').read_text()),
                                 {"1": True, "2": False})

    def test_unknown_or_partial_readings_do_not_arm_or_emit_prompts(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor = self.monitor(Path(tmp))
            self.assertEqual(monitor.observe({1: False, 2: False, 3: None}, idle=True), [])
            self.assertEqual(monitor.observe({i: True for i in range(1, 5)}, idle=True), [])
            self.assertEqual(monitor.observe({1: True, 2: True, 3: None, 4: True}, idle=True), [])
            self.assertEqual(monitor.observe({i: True for i in range(1, 5)}, idle=True), [])

    def test_reconnect_rebaselines_but_later_real_slot_reinsert_prompts_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            monitor = self.monitor(root)
            full = {i: True for i in range(1, 5)}
            monitor.observe(full, idle=True)
            monitor.observe(full, idle=True)  # stable initial baseline
            # A Klippy reconnect must invalidate in-memory arming without deleting edges.
            monitor.observe({}, idle=False)
            self.assertEqual(monitor.observe({i: False for i in range(1, 5)}, idle=True), [])
            self.assertEqual(monitor.observe(full, idle=True), [])
            self.assertEqual(monitor.observe(full, idle=True), [])
            # Once re-armed, an actual settled removal and reinsertion is still detected.
            removed = {1: True, 2: False, 3: True, 4: True}
            self.assertEqual(monitor.observe(removed, idle=True), [])
            self.assertEqual(monitor.observe(removed, idle=True), [])
            self.assertEqual(monitor.observe(full, idle=True), [])
            self.assertEqual(monitor.observe(full, idle=True), [2])
            self.assertEqual(monitor.observe(full, idle=True), [])

    def test_reconnect_placeholder_dip_after_arming_does_not_prompt_every_slot(self):
        """2026-11-02 restart: readings matched the edges, armed, then ZMod reported one
        all-empty sample and full again; that posted four insertion prompts."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'edges.json').write_text(json.dumps({str(i): True for i in range(1, 5)}))
            full = {i: True for i in range(1, 5)}
            with mock.patch.object(mel.time, 'monotonic') as clock:
                clock.return_value = 0.0
                monitor = self.monitor(root)
                monitor.reset_baseline(warmup=True)
                self.assertEqual(monitor.observe(full, idle=True), [])
                clock.return_value = 2.0
                self.assertEqual(monitor.observe({i: False for i in range(1, 5)}, idle=True), [])
                clock.return_value = 4.0
                self.assertEqual(monitor.observe(full, idle=True), [])
                clock.return_value = 6.0
                self.assertEqual(monitor.observe(full, idle=True), [])
                self.assertFalse((root / 'pending.json').exists())
                # After the warm-up window a real quick pull and reinsert still prompts.
                clock.return_value = 200.0
                self.assertEqual(monitor.observe(full, idle=True), [])
                self.assertEqual(monitor.observe({1: True, 2: False, 3: True, 4: True}, idle=True), [])
                self.assertEqual(monitor.observe(full, idle=True), [2])

    def test_idle_busy_rebaseline_has_no_warmup_so_post_print_swaps_still_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor = self.monitor(Path(tmp))
            monitor.observe({1: True}, idle=True)
            monitor.observe({1: True}, idle=False)  # a print ran
            self.assertEqual(monitor.observe({1: True}, idle=True), [])
            self.assertEqual(monitor.observe({1: False}, idle=True), [])
            self.assertEqual(monitor.observe({1: True}, idle=True), [1])


class PendingPromptTests(unittest.TestCase):
    def test_answered_or_emptied_prompts_are_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending = Path(tmp) / "pending.json"
            pending.write_text(json.dumps({s: {"event": f"slot-{s}-present", "status": "awaiting_user_dispatch"}
                                           for s in ("1", "2", "3")}))
            monitor = mel.FilamentInsertionMonitor(Path(tmp) / "edges.json", pending)
            # Slot 1 answered (no open prompt), slot 2 emptied again, slot 3 still open and loaded.
            monitor.prune_pending({1: True, 2: False, 3: True}, {2, 3})
            self.assertEqual(sorted(json.loads(pending.read_text())), ["3"])

    def test_open_insertion_slots_reads_unanswered_prompts(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(pab, "STATE", Path(tmp) / "alerts.json"):
            aid = pab.record("insertion", "", slot=2)
            pab.record("paused", "a.gcode")
            self.assertEqual(pab.open_insertion_slots(), {2})
            pab.take(aid)
            self.assertEqual(pab.open_insertion_slots(), set())


class NativeErrorPersistenceTests(ListenerCase):
    async def test_restart_does_not_repost_the_same_screen_error(self):
        error = {"status": "error", "errorCode": "E0120", "matlStationInfo": {"currentLoadSlot": 2}}

        async def read_slots():
            return error, {}
        with mock.patch.object(mel, "post") as post, mock.patch.object(mel, "log"):
            for _ in range(2):  # two listener lifetimes
                lis = self.listener()
                lis.read_slots = read_slots
                await lis.poll_native_presence()
        post.assert_called_once()
        self.assertEqual(mel.load_native_error_alerted(), ("E0120", 2))


if __name__ == "__main__":
    unittest.main()


class SharedPresenceReaderTests(unittest.TestCase):
    """The one zmod_color presence reader, shared by the listener and mapped launch."""

    def setUp(self):
        from print_handy.printers.flashforge.zmod import stock
        self.read = stock.read_zmod_color_slots
        patch = mock.patch.object(stock, "_slots", return_value=4)
        patch.start()
        self.addCleanup(patch.stop)

    def test_presence_per_slot_and_unknown_when_not_boolean(self):
        status = zmod_status(1, (True, False, True, True))["result"]["status"]
        status["zmod_color"]["slots"][3]["hasFilament"] = None
        self.assertEqual(self.read(status), {1: True, 2: False, 3: True, 4: None})
        self.assertEqual(mel.zmod_slot_presence(status), self.read(status))

    def test_missing_or_malformed_slot_lists_raise(self):
        for status in ({}, {"zmod_color": {"slots": []}},
                       {"zmod_color": {"slots": [{"ID": "9", "hasFilament": True}]}},
                       {"zmod_color": {"slots": [{"ID": "1"}, {"ID": "1"}]}}):
            with self.assertRaises(RuntimeError):
                self.read(status)
