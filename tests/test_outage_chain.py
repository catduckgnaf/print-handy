"""#1020 H: the healthcheck -> supervisor record -> outage alert chain,
offline and container-shaped.

A stale heartbeat on disk (what a hung listener leaves behind) must
fail the healthcheck, read as down in the shared listener statuses,
and — with the supervisor's restart budget spent — escalate through
the bot's outage tick to the critical notice with the bundle attached.
No container, no Discord: the same files and functions the container
path uses, over a temporary state folder.
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from print_handy import healthcheck, settings, supervisor_state  # noqa: E402
from print_handy.bot import outage_alert  # noqa: E402


def _data(tmp):
    path = Path(tmp) / "local.toml"
    path.write_text(f'[paths]\nstate_home = "{tmp}/hermes"\n')
    return settings.load(local=path, environ={})


class OutageChainTests(unittest.TestCase):
    def test_hung_listener_chain_reaches_the_critical_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = _data(tmp)
            printer_id = settings.default_printer_id(data)
            state_dir = settings.state_dir(data)

            # A hung listener: its heartbeat froze long ago, and the
            # supervisor has already spent the restart budget on it.
            beat = healthcheck.heartbeat_path(data, printer_id)
            beat.parent.mkdir(parents=True, exist_ok=True)
            beat.write_text(json.dumps(
                {"time": time.time() - 3600, "loop_lag_ms": 0, "runtime": "moonraker"}))
            supervisor_state.write(printer_id, restarts=6, budget=5, window_s=600,
                                   exhausted=True, data=data)

            # 1. The watchdog's probe fails on the stale heartbeat.
            ok, reason = healthcheck.check(data, printer_id=printer_id)
            self.assertFalse(ok)
            self.assertIn("stale", reason)

            # 2. The bot's shared status view reads the printer as down.
            statuses = healthcheck.listener_statuses(data)
            self.assertTrue(statuses[printer_id]["required"])
            self.assertIs(statuses[printer_id]["ok"], False)

            # 3. The outage tick escalates: budget spent -> critical,
            #    bundle attached, pin hook fired.
            records = supervisor_state.read_all(data)
            exhausted = {pid for pid, rec in records.items() if rec.get("exhausted")}
            outage_alert.save(state_dir, {printer_id: {"since": time.time() - 3600,
                                                       "notified_at": None}})
            sent, pinned = [], []
            messages = outage_alert.tick(
                statuses, state_dir,
                lambda pid, text: sent.append((pid, text)) or True,
                exhausted=exhausted,
                attach=lambda kind: "/state/diagnostics-bundle.md",
                pin=pinned.append)
            self.assertEqual([kind for _p, kind, _t in messages], ["critical"])
            self.assertEqual(pinned, [printer_id])
            self.assertIn("MEDIA:/state/diagnostics-bundle.md", sent[0][1])
            self.assertIn("python3 -m print_handy.diagnostics_bundle", sent[0][1])

    def test_fresh_heartbeat_chain_stays_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = _data(tmp)
            printer_id = settings.default_printer_id(data)
            healthcheck.beat(data=data, printer_id=printer_id,
                             loop_lag_ms=5, moonraker_connected=True)
            ok, _reason = healthcheck.check(data, printer_id=printer_id)
            self.assertTrue(ok)
            statuses = healthcheck.listener_statuses(data)
            self.assertIs(statuses[printer_id]["ok"], True)
            sent = []
            messages = outage_alert.tick(statuses, settings.state_dir(data),
                                         lambda pid, text: sent.append(text) or True)
            self.assertEqual(messages, [])
            self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
