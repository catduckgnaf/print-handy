"""#1404: crash capture — one redacted JSON record per process role.

A crash record is written by the process that crashed (installed as the
exception hooks by ``logs.configure(role=...)``), never uploaded anywhere,
and read back only by the diagnostics bundle's Crashes section. These
tests pin the record shape, its redaction, its fail-soft posture, and
the hook behaviour (record, chain, never record an operator interrupt).
"""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from print_handy import crash_report

# Built from parts so no token-shaped string sits in the source (GitHub
# push protection flags those).
DISCORD_TOKEN = ".".join(["MTIzNDU2Nzg5MDEyMzQ1Njc4OQ", "GabcDE",
                          "abcdefghijklmnopqrstuvwxyz0123456789"])


def _data(tmp):
    return {"paths": {"state_home": Path(tmp) / "profile"}}


def _raise(exc):
    try:
        raise exc
    except Exception:
        return sys.exc_info()
    raise AssertionError("unreachable")


class RecordTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = _data(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_record_writes_json_with_the_expected_fields(self):
        exc_type, exc, tb = _raise(ValueError("bad plate"))
        target = crash_report.record(exc_type, exc, tb, role="bot", data=self.data, now=1000.0)
        self.assertEqual(target, crash_report.settings.state_dir(self.data) / "crashes" / "bot.json")
        payload = json.loads(target.read_text())
        self.assertEqual(payload["time"], 1000.0)
        self.assertIn("T", payload["recorded_at"])  # epoch 1000, rendered as ISO text
        self.assertEqual(payload["role"], "bot")
        self.assertEqual(payload["version"], crash_report.__version__)
        self.assertEqual(payload["pid"], __import__("os").getpid())
        self.assertEqual(payload["exception"], "ValueError")
        self.assertEqual(payload["message"], "bad plate")
        self.assertIn("Traceback", payload["traceback"])
        self.assertIn("ValueError: bad plate", payload["traceback"])
        self.assertIn("context", payload)

    def test_record_redacts_secrets_from_message_and_traceback(self):
        exc_type, exc, tb = _raise(RuntimeError(f"login rejected {DISCORD_TOKEN}"))
        target = crash_report.record(exc_type, exc, tb, role="listener", data=self.data, now=1.0)
        text = target.read_text()
        self.assertNotIn(DISCORD_TOKEN, text)
        payload = json.loads(text)
        self.assertNotIn(DISCORD_TOKEN, payload["message"])
        self.assertNotIn(DISCORD_TOKEN, payload["traceback"])

    def test_record_redacts_secrets_from_the_bound_context(self):
        # Review on #1409: bound log context reached the crash file unredacted.
        from print_handy import logs
        token = logs.bind(api_key="plain-value-under-a-secret-key",
                          detail=f"auth {DISCORD_TOKEN}", intake="iid-7")
        try:
            exc_type, exc, tb = _raise(RuntimeError("boom"))
            target = crash_report.record(exc_type, exc, tb, role="bot", data=self.data, now=1.0)
        finally:
            logs.unbind(token)
        text = target.read_text()
        self.assertNotIn("plain-value-under-a-secret-key", text)
        self.assertNotIn(DISCORD_TOKEN, text)
        context = json.loads(text)["context"]
        self.assertEqual(context["api_key"], logs.REDACTED)
        self.assertEqual(context["intake"], "iid-7")

    def test_concurrent_records_for_one_role_never_lose_the_record(self):
        # Review on #1409: a shared temp name let one writer's os.replace
        # move the other's temp file away, losing a crash record.
        exc_type, exc, tb = _raise(ValueError("race"))
        results, start = [], threading.Barrier(8)

        def crash():
            start.wait()
            results.append(crash_report.record(exc_type, exc, tb, role="listener",
                                               data=self.data, now=2.0))

        threads = [threading.Thread(target=crash) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 8)
        self.assertTrue(all(path is not None for path in results), results)
        folder = crash_report.crash_dir(self.data)
        self.assertEqual(json.loads((folder / "listener.json").read_text())["message"], "race")
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["listener.json"])

    def test_record_sanitizes_the_role_into_a_filename(self):
        exc_type, exc, tb = _raise(ValueError("x"))
        target = crash_report.record(exc_type, exc, tb, role="Bot Main!", data=self.data, now=1.0)
        self.assertEqual(target.name, "bot-main.json")

    def test_record_never_raises_on_an_unwritable_state_folder(self):
        blocker = Path(self._tmp.name) / "blocker"
        blocker.write_text("a file, not a folder")
        data = {"paths": {"state_home": blocker / "profile"}}
        exc_type, exc, tb = _raise(ValueError("x"))
        self.assertIsNone(crash_report.record(exc_type, exc, tb, role="bot", data=data, now=1.0))


class InstallTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = _data(self._tmp.name)
        self._sys_hook = sys.excepthook
        self._thread_hook = threading.excepthook
        self._installed = set(crash_report._installed_roles)

    def tearDown(self):
        sys.excepthook = self._sys_hook
        threading.excepthook = self._thread_hook
        crash_report._installed_roles.clear()
        crash_report._installed_roles.update(self._installed)
        self._tmp.cleanup()

    def test_install_is_idempotent_per_role(self):
        self.assertTrue(crash_report.install("hooktest", data=self.data))
        first = sys.excepthook
        self.assertTrue(crash_report.install("hooktest", data=self.data))
        self.assertIs(sys.excepthook, first)

    def test_installed_hook_records_and_chains_to_the_previous_hook(self):
        chained = []
        sys.excepthook = lambda *args: chained.append(args)
        crash_report.install("hookchain", data=self.data)
        exc_type, exc, tb = _raise(ValueError("chained crash"))
        sys.excepthook(exc_type, exc, tb)
        self.assertEqual(len(chained), 1)
        records = crash_report.read_records(self.data)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["role"], "hookchain")
        self.assertEqual(records[0]["exception"], "ValueError")

    def test_keyboard_interrupt_is_not_recorded_but_still_chains(self):
        chained = []
        sys.excepthook = lambda *args: chained.append(args)
        crash_report.install("hookinterrupt", data=self.data)
        try:
            raise KeyboardInterrupt
        except KeyboardInterrupt:
            sys.excepthook(*sys.exc_info())
        self.assertEqual(len(chained), 1)
        self.assertEqual(crash_report.read_records(self.data), [])

    def test_thread_hook_records_and_chains(self):
        chained = []
        threading.excepthook = lambda args: chained.append(args)
        crash_report.install("hookthread", data=self.data)
        exc_type, exc, tb = _raise(RuntimeError("worker died"))

        class _Args:
            pass

        args = _Args()
        args.exc_type, args.exc_value, args.exc_traceback, args.thread = exc_type, exc, tb, None
        threading.excepthook(args)
        self.assertEqual(len(chained), 1)
        records = crash_report.read_records(self.data)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["exception"], "RuntimeError")


class ReadAndSectionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = _data(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_read_records_sorts_by_time_and_skips_corrupt_files(self):
        crash_report.record(*_raise(ValueError("later")), role="bot", data=self.data, now=200.0)
        crash_report.record(*_raise(RuntimeError("earlier")), role="listener", data=self.data, now=100.0)
        crashes = crash_report.settings.state_dir(self.data) / "crashes"
        (crashes / "broken.json").write_text("{not json")
        records = crash_report.read_records(self.data)
        self.assertEqual([r["exception"] for r in records], ["RuntimeError", "ValueError"])

    def test_section_is_the_no_crashes_line_when_empty(self):
        self.assertEqual(crash_report.section(self.data), "(no crashes recorded)")

    def test_section_lists_role_time_exception_and_traceback(self):
        crash_report.record(*_raise(ValueError("bad plate")), role="webui",
                            data=self.data, now=1000.0)
        text = crash_report.section(self.data)
        self.assertIn("webui", text)
        self.assertIn("ValueError", text)
        self.assertIn("bad plate", text)
        self.assertIn("```text", text)
        self.assertIn("Traceback", text)


if __name__ == "__main__":
    unittest.main()
