"""Offline process-identity and stale-state regressions; no network or printer."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from print_handy import healthcheck as health


class ListenerHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "listener_health.json"
        patch = mock.patch.object(health, "heartbeat_path", return_value=self.path)
        patch.start()
        self.addCleanup(patch.stop)

    def test_own_process_healthy_without_orca_or_printer(self):
        health.beat(moonraker_connected=False)
        self.assertTrue(health.check()[0])

    def test_missing_stale_malformed_foreign_and_reused_pid_fail(self):
        self.assertFalse(health.check()[0])
        record = health.beat()
        for changes in ({"time": 0}, {"time": float("nan")}, {"pid": 0},
                        {"pid": "invalid"}, {"process_start": "reused"},
                        {"runtime": "bot"}, {"host": "other-container"}):
            with self.subTest(changes=changes):
                self.path.write_text(json.dumps({**record, **changes}))
                self.assertFalse(health.check()[0])
        for value in ("[]", "null", "broken"):
            self.path.write_text(value)
            self.assertFalse(health.check()[0])

    def test_listener_cancels_heartbeat_on_shutdown(self):
        import asyncio
        from print_handy.hermes import moonraker_event_listener as mel

        async def flow():
            listener = mel.Listener.__new__(mel.Listener)
            listener.state = "offline"
            started = asyncio.Event()
            stopped = asyncio.Event()

            async def heartbeat():
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    stopped.set()

            async def session():
                await asyncio.Future()

            listener.heartbeat = heartbeat
            listener.session = session
            task = asyncio.create_task(listener.forever())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(stopped.is_set())
            self.assertFalse(listener.moonraker_connected)

        asyncio.run(flow())

    def test_current_linux_process_identity(self):
        self.assertTrue(health.process_identity(os.getpid()).isdigit())

    def test_blocked_loop_fails_check_even_with_a_fresh_beat(self):
        # #1020 A3: the writer thread keeps beating while the loop is
        # blocked; the recorded lag, not the beat age, condemns it.
        health.beat(moonraker_connected=True, loop_lag_ms=200_000)
        ok, reason = health.check()
        self.assertFalse(ok)
        self.assertIn("loop lag", reason)
        health.beat(moonraker_connected=True, loop_lag_ms=1_500)
        self.assertTrue(health.check()[0])
        health.beat(moonraker_connected=True)  # no lag field: legacy verdict
        self.assertTrue(health.check()[0])

    def test_beat_once_reports_the_loop_lag(self):
        import time as time_module

        from print_handy.hermes import moonraker_event_listener as mel

        listener = mel.Listener.__new__(mel.Listener)
        listener.state = "printing"
        listener.moonraker_connected = True
        listener._loop_tick = time_module.time() - 120
        captured = {}
        with mock.patch.object(mel.healthcheck, "beat",
                               side_effect=lambda **kw: captured.update(kw)):
            listener._beat_once()
        self.assertGreaterEqual(captured["loop_lag_ms"], 119_000)
        self.assertEqual("printing", captured["state"])

    def test_stack_dump_handler_writes_the_state_file(self):
        # #1020 A4: SIGUSR1 (the supervisor's watchdog signal) dumps every
        # thread's stack to the state file beside the heartbeat.
        import signal
        import time as time_module

        target = Path(self.tmp.name) / "listener_stack_dump.log"
        with mock.patch.object(health, "stack_dump_path", return_value=target):
            installed = health.install_stack_dump_handler()
        self.addCleanup(signal.signal, signal.SIGUSR1, signal.SIG_DFL)
        self.assertEqual(target, installed)
        os.kill(os.getpid(), signal.SIGUSR1)
        deadline = time_module.time() + 3
        while time_module.time() < deadline:
            if target.is_file() and "SIGUSR1 stack dump" in target.read_text():
                break
            time_module.sleep(0.05)
        text = target.read_text()
        self.assertIn("SIGUSR1 stack dump", text)
        self.assertIn("Thread", text)

    def test_stack_dump_is_registered_in_faulthandler_at_c_level(self):
        # The dump must not depend on the main thread returning to Python
        # bytecode (a hang may hold the GIL in C): faulthandler.register
        # writes it from the C signal handler, chained to the Python stamp.
        import faulthandler
        import signal

        target = Path(self.tmp.name) / "listener_stack_dump.log"
        self.addCleanup(signal.signal, signal.SIGUSR1, signal.SIG_DFL)
        self.addCleanup(faulthandler.unregister, signal.SIGUSR1)
        with mock.patch.object(health, "stack_dump_path", return_value=target), \
                mock.patch.object(faulthandler, "register", wraps=faulthandler.register) as register:
            health.install_stack_dump_handler()
        register.assert_called_once()
        args, kwargs = register.call_args
        self.assertEqual(signal.SIGUSR1, args[0])
        self.assertTrue(kwargs["all_threads"])
        self.assertTrue(kwargs["chain"])
        self.assertEqual(str(target), kwargs["file"].name)

    def test_stack_dump_rearm_keeps_the_c_level_dump(self):
        # A second install (the listeners entry point and a listener's own
        # main can both arm it) must not leave the Python handler alone.
        import signal
        import time as time_module

        first = Path(self.tmp.name) / "first.log"
        second = Path(self.tmp.name) / "second.log"
        import faulthandler
        self.addCleanup(signal.signal, signal.SIGUSR1, signal.SIG_DFL)
        self.addCleanup(faulthandler.unregister, signal.SIGUSR1)
        for target in (first, second):
            with mock.patch.object(health, "stack_dump_path", return_value=target):
                health.install_stack_dump_handler()
        os.kill(os.getpid(), signal.SIGUSR1)
        deadline = time_module.time() + 3
        while time_module.time() < deadline:
            if "(above)" in second.read_text():
                break
            time_module.sleep(0.05)
        dump = second.read_text().lower()
        self.assertIn("thread 0x", dump.split("armed at", 1)[1])
        self.assertLess(dump.index("thread 0x"), dump.index("(above)"))


class PerPrinterHeartbeatTests(unittest.TestCase):
    """#336: heartbeats live under printers/<id>/ and are checked per printer."""

    ROSTER = {"printers": [
        {"id": "moon-two", "model": "klipper.generic", "connection": "moonraker"},
        {"id": "bambu-two", "model": "bambulab.a1"},
    ]}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        patch = mock.patch.object(
            health.settings, "printer_state_dir",
            side_effect=lambda printer_id=None, data=None: root / "printers" / printer_id)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(
            health.settings, "default_printer_id", return_value="moon-two")
        patch.start()
        self.addCleanup(patch.stop)

    def roster(self):
        return dict(self.ROSTER)

    def test_heartbeats_are_independent_per_printer(self):
        from print_handy.printers.bambulab.listener import RUNTIME as BAMBU_RUNTIME
        health.beat(data=self.roster(), printer_id="moon-two")
        self.assertTrue(health.check(self.roster(), printer_id="moon-two")[0])
        ok, reason = health.check(self.roster(), printer_id="bambu-two")
        self.assertFalse(ok)
        self.assertIn("missing", reason)
        health.beat(runtime=BAMBU_RUNTIME, data=self.roster(), printer_id="bambu-two")
        self.assertTrue(health.check(self.roster(), printer_id="bambu-two")[0])
        # The other printer's heartbeat is untouched and still fresh.
        self.assertTrue(health.check(self.roster(), printer_id="moon-two")[0])

    def test_record_names_its_printer_and_a_foreign_record_fails(self):
        from print_handy.printers.bambulab.listener import RUNTIME as BAMBU_RUNTIME
        health.beat(data=self.roster(), printer_id="moon-two")
        record = json.loads(health.heartbeat_path(
            self.roster(), "moon-two").read_text())
        self.assertEqual(record["printer_id"], "moon-two")
        # A record beating under bambu-two's own runtime but naming
        # moon-two is foreign to bambu-two and must not satisfy its check.
        foreign_record = health.beat(runtime=BAMBU_RUNTIME, data=self.roster(),
                                      printer_id="moon-two")
        foreign = health.heartbeat_path(self.roster(), "bambu-two")
        foreign.parent.mkdir(parents=True, exist_ok=True)
        foreign.write_text(json.dumps(foreign_record))
        ok, reason = health.check(self.roster(), printer_id="bambu-two")
        self.assertFalse(ok)
        self.assertIn("another printer", reason)

    def test_wrong_runtime_for_the_printer_fails(self):
        # A Moonraker heartbeat does not satisfy the Bambu printer: each
        # printer's expected runtime comes from its own domain.
        health.beat(data=self.roster(), printer_id="bambu-two")
        ok, _ = health.check(self.roster(), printer_id="bambu-two")
        self.assertFalse(ok)

    def test_listener_statuses_aggregates_the_roster(self):
        health.beat(data=self.roster(), printer_id="moon-two")
        statuses = health.listener_statuses(self.roster())
        self.assertEqual(set(statuses), {"moon-two", "bambu-two"})
        self.assertTrue(statuses["moon-two"]["ok"])
        self.assertTrue(statuses["moon-two"]["required"])
        self.assertFalse(statuses["bambu-two"]["ok"])
        self.assertTrue(statuses["bambu-two"]["required"])

    def test_instance_context_routes_an_unaddressed_beat(self):
        from print_handy import listeners
        listeners._set_instance_printer_id("bambu-two")
        self.addCleanup(listeners._set_instance_printer_id, None)
        from print_handy.printers.bambulab.listener import RUNTIME as BAMBU_RUNTIME
        health.beat(runtime=BAMBU_RUNTIME, data=self.roster())
        self.assertTrue(health.check(self.roster(), printer_id="bambu-two")[0])
        self.assertFalse(health.check(self.roster(), printer_id="moon-two")[0])
