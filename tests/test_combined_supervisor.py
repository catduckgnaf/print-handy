"""Native lifecycle regressions: fake children only, no service or printer contact."""
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'docker/supervise.sh'


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def pid_stopped(pid):
    """Gone, or a zombie awaiting init's reap: either way it is stopped.

    A listener child whose supervisor loop has exited is reaped by init,
    not by supervise.sh, so existence alone does not mean it still runs.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
        return state == 'Z'
    except OSError:
        return True


def assert_pid_stopped(testcase, pid, timeout=5.0):
    testcase.assertTrue(wait_for(lambda: pid_stopped(pid), timeout),
                        f'pid {pid} still running')


class CombinedSupervisorTests(unittest.TestCase):
    def environment(self, directory, child, listener_child=None):
        fake = Path(directory, 'python3')
        # The supervisor probes the listener gate (#256, per printer since
        # #336) and the Web UI gate (#286) before starting children; the
        # fake answers them from PRINT_HANDY_TEST_LISTENER_GATE (default 0
        # = listener required) and PRINT_HANDY_TEST_WEBUI_GATE (default 3
        # = Web UI disabled). The bot gate (print_handy.bot_gate) starts
        # the bot when this test's DISCORD_BOT_TOKEN is set (the default
        # platform). The gate's --roster form prints
        # PRINT_HANDY_TEST_ROSTER (default: one printer) and fails only
        # when the gate code is 2 (unresolved configuration). Listener
        # invocations (print_handy.listeners --printer <id>) run the
        # listener body; every other python3 invocation runs the child body.
        if listener_child is None:
            listener_child = child
        gate = ('case "$*" in '
                '*print_handy.listener_gate*--roster*) '
                'if [ "${PRINT_HANDY_TEST_LISTENER_GATE:-0}" = 2 ]; then exit 2; fi; '
                'printf "%s\\n" "${PRINT_HANDY_TEST_ROSTER:-printer1}"; exit 0;; '
                '*print_handy.listener_gate*) '
                'exit "${PRINT_HANDY_TEST_LISTENER_GATE:-0}";; '
                '*print_handy.bot_gate*) '
                'if [ -n "${DISCORD_BOT_TOKEN:-}" ]; then exit 0; fi; exit 3;; '
                '*print_handy.webui_gate*) '
                'exit "${PRINT_HANDY_TEST_WEBUI_GATE:-3}";; '
                '*print_handy.live_gate*) '
                'exit "${PRINT_HANDY_TEST_LIVE_GATE:-3}";; '
                # supervise.sh persists each listener restart through
                # this module (#1020); the fake just acknowledges it.
                '*print_handy.supervisor_state*) '
                'exit 0;; '
                '*print_handy.listeners*) ' + listener_child + ';; '
                'esac\n')
        fake.write_text('#!/bin/bash\n' + gate + child)
        fake.chmod(0o755)
        return {**os.environ, 'PATH': directory + ':' + os.environ['PATH'],
                'PRINT_HANDY_BUNDLED_SPOOLMAN': 'false',
                'PRINT_HANDY__SERVICES__SPOOLMAN_URL': 'http://external.invalid:8000',
                'DISCORD_BOT_TOKEN': ''}

    def test_explicit_command_is_executed_without_services(self):
        result = subprocess.run(['bash', str(SCRIPT), 'printf', 'offline-probe'],
                                capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'offline-probe')

    def test_even_clean_required_child_exit_is_failure(self):
        # #336: listener instances are no longer required children, so the
        # required child whose clean exit must fail the container is the bot.
        with tempfile.TemporaryDirectory() as directory:
            env = self.environment(directory, 'exit 0\n')
            env['PRINT_HANDY_TEST_LISTENER_GATE'] = '3'
            env['DISCORD_BOT_TOKEN'] = 'dummy'
            result = subprocess.run(['bash', str(SCRIPT)], env=env,
                                    capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('Required child exited', result.stderr)

    def test_child_failure_status_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            env = self.environment(directory, 'exit 7\n')
            env['PRINT_HANDY_TEST_LISTENER_GATE'] = '3'
            env['DISCORD_BOT_TOKEN'] = 'dummy'
            result = subprocess.run(['bash', str(SCRIPT)], env=env,
                                    capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 7, result.stderr)

    def test_sigterm_exits_and_stops_child(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory, 'child-pid')
            env = self.environment(directory, f'echo $$ > {marker}\nexec sleep 60\n')
            process = subprocess.Popen(['bash', str(SCRIPT)], env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                for _ in range(100):
                    if marker.exists():
                        break
                    time.sleep(0.02)
                self.assertTrue(marker.exists())
                pid = int(marker.read_text())
                process.send_signal(signal.SIGTERM)
                process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0)
                assert_pid_stopped(self, pid)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_listener_skipped_for_non_moonraker_connection(self):
        """Gate says no listener (#256): only the bot child runs, and its exit rules."""
        with tempfile.TemporaryDirectory() as directory:
            env = self.environment(directory, 'exit 5\n')
            env['PRINT_HANDY_TEST_LISTENER_GATE'] = '3'
            env['DISCORD_BOT_TOKEN'] = 'dummy'
            result = subprocess.run(['bash', str(SCRIPT)], env=env,
                                    capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertIn('listener not started', result.stdout)

    def test_listener_gate_failure_stops_container(self):
        """An unresolved configuration stops the supervisor before any child starts."""
        with tempfile.TemporaryDirectory() as directory:
            env = self.environment(directory, 'exit 9\n')
            env['PRINT_HANDY_TEST_LISTENER_GATE'] = '2'
            result = subprocess.run(['bash', str(SCRIPT)], env=env,
                                    capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('Listener gate failed', result.stderr)

    def test_webui_child_runs_only_when_the_gate_allows_it(self):
        """Web UI gate 0 (#286): with no listener and no bot, the Web UI child runs and its exit rules."""
        with tempfile.TemporaryDirectory() as directory:
            env = self.environment(directory, 'exit 6\n')
            env['PRINT_HANDY_TEST_LISTENER_GATE'] = '3'
            env['PRINT_HANDY_TEST_WEBUI_GATE'] = '0'
            result = subprocess.run(['bash', str(SCRIPT)], env=env,
                                    capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 6, result.stderr)
        self.assertNotIn('idling', result.stdout)

    def test_webui_gate_failure_stops_container(self):
        """An unresolved configuration at the Web UI gate stops the supervisor before the child starts."""
        with tempfile.TemporaryDirectory() as directory:
            env = self.environment(directory, 'exit 9\n')
            env['PRINT_HANDY_TEST_LISTENER_GATE'] = '3'
            env['PRINT_HANDY_TEST_WEBUI_GATE'] = '2'
            result = subprocess.run(['bash', str(SCRIPT)], env=env,
                                    capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('Web UI gate failed', result.stderr)

    def test_live_child_runs_only_when_the_gate_allows_it(self):
        """Live gate 0 (#311): with no listener and no bot, the Activity server child runs and its exit rules."""
        with tempfile.TemporaryDirectory() as directory:
            env = self.environment(directory, 'exit 8\n')
            env['PRINT_HANDY_TEST_LISTENER_GATE'] = '3'
            env['PRINT_HANDY_TEST_LIVE_GATE'] = '0'
            result = subprocess.run(['bash', str(SCRIPT)], env=env,
                                    capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 8, result.stderr)
        self.assertNotIn('idling', result.stdout)

    def test_live_gate_failure_stops_container(self):
        """An unresolved configuration at the live gate stops the supervisor before the child starts."""
        with tempfile.TemporaryDirectory() as directory:
            env = self.environment(directory, 'exit 9\n')
            env['PRINT_HANDY_TEST_LISTENER_GATE'] = '3'
            env['PRINT_HANDY_TEST_LIVE_GATE'] = '2'
            result = subprocess.run(['bash', str(SCRIPT)], env=env,
                                    capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('Live gate failed', result.stderr)

    def test_no_children_configuration_idles_until_terminated(self):
        """No listener, no bot token, external Spoolman: stay up, exit 0 on SIGTERM."""
        with tempfile.TemporaryDirectory() as directory:
            env = self.environment(directory, 'exit 9\n')
            env['PRINT_HANDY_TEST_LISTENER_GATE'] = '3'
            process = subprocess.Popen(['bash', str(SCRIPT)], env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                time.sleep(1.0)
                self.assertIsNone(process.poll())
                process.send_signal(signal.SIGTERM)
                stdout, _ = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0)
                self.assertIn('idling', stdout)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_disabled_backend_requires_external_url(self):
        with tempfile.TemporaryDirectory() as directory:
            env = self.environment(directory, 'exit 0\n')
            env['PRINT_HANDY__SERVICES__SPOOLMAN_URL'] = 'http://127.0.0.1:8000'
            result = subprocess.run(['bash', str(SCRIPT)], env=env,
                                    capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 2)
        self.assertIn('External Spoolman URL required', result.stderr)


class MultiPrinterSupervisorTests(unittest.TestCase):
    """#336: one listener instance per printer, restarted alone, never fatal."""

    def environment(self, directory, child, listener_child):
        env = CombinedSupervisorTests().environment(directory, child, listener_child)
        env['DISCORD_BOT_TOKEN'] = 'dummy'
        return env

    def start(self, env):
        process = subprocess.Popen(['bash', str(SCRIPT)], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self._stop, process)
        return process

    @staticmethod
    def _stop(process):
        if process.poll() is None:
            process.kill()
            process.communicate()

    def stop_cleanly(self, process):
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, stderr)

    def test_two_printers_each_get_their_own_listener(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = Path(directory, 'calls')
            listener = (f'echo "listener $$ $*" >> {calls}\n'
                        'exec sleep 60\n')
            env = self.environment(directory, 'exec sleep 60\n', listener)
            env['PRINT_HANDY_TEST_ROSTER'] = 'printer-a\nprinter-b'
            process = self.start(env)
            pids = []
            try:
                self.assertTrue(wait_for(lambda: calls.exists()
                                         and 'printer-a' in calls.read_text()
                                         and 'printer-b' in calls.read_text()),
                                'both listener instances started')
                lines = calls.read_text().splitlines()
                self.assertEqual(len(lines), 2, lines)
                self.assertTrue(any('--printer printer-a' in line for line in lines))
                self.assertTrue(any('--printer printer-b' in line for line in lines))
                pids = [int(line.split()[1]) for line in lines]
                self.assertIsNone(process.poll())
            finally:
                self.stop_cleanly(process)
            for pid in pids:
                assert_pid_stopped(self, pid)

    def test_killing_one_listener_restarts_only_that_child(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = Path(directory, 'calls')
            flag = Path(directory, 'crashed-a')
            listener = (f'case "$*" in *printer-a*) '
                        f'if [ ! -f {flag} ]; then touch {flag}; '
                        f'echo "crash-a $$" >> {calls}; exit 1; fi;; esac\n'
                        f'echo "run $$ $*" >> {calls}\n'
                        'exec sleep 60\n')
            env = self.environment(directory, 'exec sleep 60\n', listener)
            env['PRINT_HANDY_TEST_ROSTER'] = 'printer-a\nprinter-b'
            env['PRINT_HANDY_LISTENER_RESTART_BACKOFF_S'] = '1'
            process = self.start(env)
            try:
                def settled():
                    if not calls.exists():
                        return False
                    text = calls.read_text()
                    return (text.count('crash-a') == 1
                            and text.count('--printer printer-a') == 1
                            and text.count('--printer printer-b') == 1)
                self.assertTrue(wait_for(settled),
                                'printer-a restarted once, printer-b untouched: '
                                + (calls.read_text() if calls.exists() else 'no calls'))
                self.assertIsNone(process.poll())
            finally:
                self.stop_cleanly(process)

    def test_hung_listener_is_killed_and_restarted_by_the_watchdog(self):
        """#1020: a listener that never exits but whose heartbeat is stale is restarted."""
        with tempfile.TemporaryDirectory() as directory:
            calls = Path(directory, 'calls')
            # First run spins (hung, never exits); the healthcheck probe
            # (print_handy.healthcheck) fails until the second run exists.
            listener = (f'echo "run $$" >> {calls}\n'
                        f'if [ "$(wc -l < {calls})" -le 1 ]; then trap "echo usr1 >> {calls}" USR1; '
                        'while true; do sleep 0.2; done; fi\n'
                        'exec sleep 60\n')
            env = self.environment(directory, 'exec sleep 60\n', listener)
            # The fake python3 answers every unknown module with the listener
            # body, so route the probe explicitly: stale until two runs exist.
            fake = Path(directory, 'python3')
            text = fake.read_text().replace(
                "case \"$*\" in ",
                "case \"$*\" in *print_handy.healthcheck*) "
                f"if [ \"$(wc -l < {calls})\" -ge 2 ]; then echo 'ok  alive'; exit 0; fi; "
                "echo 'FAIL  listener heartbeat stale or invalid'; exit 1;; ", 1)
            fake.write_text(text)
            env['PRINT_HANDY_LISTENER_RESTART_BACKOFF_S'] = '1'
            env['PRINT_HANDY_LISTENER_WATCHDOG_INTERVAL_S'] = '1'
            env['PRINT_HANDY_LISTENER_WATCHDOG_GRACE_S'] = '1'
            process = self.start(env)
            try:
                self.assertTrue(wait_for(lambda: calls.exists() and calls.read_text().count('run ') >= 2, 30),
                                'the hung listener was restarted: ' + (calls.read_text() if calls.exists() else 'no calls'))
                self.assertIn('usr1', calls.read_text(), 'the stack dump signal was sent before the kill')
                self.assertIsNone(process.poll())
            finally:
                self.stop_cleanly(process)

    def test_crashing_health_probe_never_kills_the_listener(self):
        """A probe with no verdict (an import crash, a timeout) is not a stale heartbeat."""
        with tempfile.TemporaryDirectory() as directory:
            calls = Path(directory, 'calls')
            probes = Path(directory, 'probes')
            listener = f'echo "run $$" >> {calls}\nexec sleep 60\n'
            env = self.environment(directory, 'exec sleep 60\n', listener)
            fake = Path(directory, 'python3')
            fake.write_text(fake.read_text().replace(
                "case \"$*\" in ",
                f"case \"$*\" in *print_handy.healthcheck*) echo probe >> {probes}; "
                "echo 'Traceback (most recent call last):' >&2; exit 1;; ", 1))
            env['PRINT_HANDY_LISTENER_RESTART_BACKOFF_S'] = '1'
            env['PRINT_HANDY_LISTENER_WATCHDOG_INTERVAL_S'] = '1'
            env['PRINT_HANDY_LISTENER_WATCHDOG_GRACE_S'] = '1'
            process = self.start(env)
            try:
                self.assertTrue(wait_for(lambda: probes.exists() and len(probes.read_text().split()) >= 3, 30),
                                'the watchdog kept probing')
                self.assertEqual(calls.read_text().count('run '), 1, calls.read_text())
                self.assertIsNone(process.poll())
            finally:
                self.stop_cleanly(process)

    def test_listener_restart_loop_exhausts_its_budget_and_stays_down(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = Path(directory, 'calls')
            listener = (f'echo "try $$" >> {calls}\n'
                        'exit 1\n')
            env = self.environment(directory, 'exec sleep 60\n', listener)
            env['PRINT_HANDY_LISTENER_RESTART_BACKOFF_S'] = '1'
            env['PRINT_HANDY_LISTENER_RESTART_BUDGET'] = '2'
            process = self.start(env)
            try:
                # Initial start + 2 budgeted restarts = 3 invocations, then
                # the loop gives up: the printer degrades to unhealthy while
                # the container (kept up by the bot child) stays up.
                self.assertTrue(wait_for(lambda: calls.exists()
                                         and len(calls.read_text().splitlines()) == 3),
                                'listener restarted up to its budget')
                time.sleep(3.0)
                self.assertEqual(len(calls.read_text().splitlines()), 3)
                self.assertIsNone(process.poll())
            finally:
                self.stop_cleanly(process)
