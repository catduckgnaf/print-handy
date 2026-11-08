"""Supervisor/listener fake-clock soak: multi-day compressed time.

Three real seams, driven entirely by a fake clock (no real sleeping):

* ``outage_alert.tick`` — the bot's outage notifier, state persisted to
  a directory, so each new tick call is also a bot-restart simulation:
  a restart mid-outage must neither repeat nor lose a notice.
* ``supervisor_state`` — the persisted restart-budget record (#1025).
  The counting rule itself lives in ``docker/supervise.sh`` (bash
  ``$SECONDS`` arithmetic: a window that has fully elapsed resets the
  count; exceeding the budget in the window marks exhaustion and stops
  restarting). There is no Python seam for that arithmetic, so the
  soak's ``BudgetDriver`` mirrors the shell rule line for line and
  persists every step through the real ``supervisor_state.write``;
  all assertions read the real persisted record back. Named gap:
  supervise.sh keeps its counters in shell variables and does not read
  the persisted record back at startup — the record is the visibility
  surface for the bot and the bundle, so budget enforcement across a
  *container* restart is not asserted here, only persistence.
* ``healthcheck.check`` with a fake ``now`` — staleness and the
  loop-lag tripwire over compressed time.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from print_handy import healthcheck, settings, supervisor_state
from print_handy.bot import outage_alert

PID = "p1"
T0 = 1_700_000_000.0
DAY = 86_400.0
DOWN = {PID: {"required": True, "ok": False, "reason": "heartbeat stale"}}
UP = {PID: {"required": True, "ok": True, "reason": ""}}
BUDGET, WINDOW = 5, 600


def _data(tmp):
    path = Path(tmp) / "local.toml"
    path.write_text(f'[paths]\nhermes_home = "{tmp}/hermes"\n')
    return settings.load(local=path, environ={})


def _kind(text):
    if text.startswith("**CRITICAL"):
        return "critical"
    if "are still down" in text:
        return "still_down"
    if "are back" in text:
        return "up"
    return "down"


class BudgetDriver:
    """docker/supervise.sh's restart counting, mirrored on a fake clock:

        if SECONDS - window_start >= window: window_start=SECONDS; restarts=0
        restarts += 1
        restarts > budget  ->  record exhausted, stop restarting
    """

    def __init__(self, data):
        self.data = data
        self.window_start = None
        self.restarts = 0
        self.exhausted = False

    def exit(self, now, status=1):
        if self.window_start is None or now - self.window_start >= WINDOW:
            self.window_start = now
            self.restarts = 0
        self.restarts += 1
        self.exhausted = self.restarts > BUDGET
        supervisor_state.write(PID, restarts=self.restarts, budget=BUDGET,
                               window_s=WINDOW, exhausted=self.exhausted,
                               last_exit_status=status, now=now, data=self.data)
        return self.exhausted

    def record(self):
        return supervisor_state.read(PID, data=self.data)


class Notifier:
    """outage_alert.tick with a recording send; every call re-reads the
    persisted state from disk, exactly like a bot process restart."""

    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.sent = []  # (now, kind, text)

    def poll(self, statuses, now, exhausted=False):
        def send(printer_id, text):
            self.sent.append((now, _kind(text), text))
            return True

        outage_alert.tick(statuses, self.state_dir, send, now=now,
                          exhausted={PID} if exhausted else None)
        return self

    def kinds(self):
        return [kind for _now, kind, _text in self.sent]


# ------------------------------------------------------------- the big soak

def test_multi_day_outage_alerts_once_per_incident_and_recovers(tmp_path):
    notifier = Notifier(tmp_path / "alert-state")
    driver = BudgetDriver(_data(tmp_path))

    # Day 1: the listener drops at T0 and stays down for three hours.
    # Poll every simulated minute: silence inside the grace period, one
    # down notice at +180s, then hourly reminders — never one per poll.
    t = T0
    while t <= T0 + 3 * 3600:
        notifier.poll(DOWN, t)
        t += 60
    assert notifier.kinds() == ["down", "still_down", "still_down"]
    assert notifier.sent[0][0] == T0 + outage_alert.DOWN_AFTER_S

    # The supervisor burns its whole budget inside one window: the
    # persisted record says exhausted, and the next poll escalates to
    # CRITICAL — once. A full further day down adds reminders only.
    for i in range(BUDGET + 1):
        driver.exit(T0 + 3 * 3600 + i * 10)
    assert driver.record()["exhausted"] is True
    t = T0 + 3 * 3600 + 60
    while t <= T0 + DAY + 3 * 3600:
        notifier.poll(DOWN, t, exhausted=True)
        t += 60
    assert notifier.kinds().count("critical") == 1

    # Recovery: exactly one "back" notice, and the incident is cleared.
    notifier.poll(UP, t)
    assert notifier.kinds()[-1] == "up"
    assert PID not in outage_alert.load(tmp_path / "alert-state")

    # Day 3: a fresh outage is a fresh incident — it alerts again from
    # the top (down, not still_down), and can go critical again.
    t2 = T0 + 2 * DAY
    notifier.poll(DOWN, t2)
    notifier.poll(DOWN, t2 + outage_alert.DOWN_AFTER_S)
    assert notifier.kinds()[-1] == "down"
    assert notifier.kinds().count("down") == 2
    notifier.poll(DOWN, t2 + outage_alert.DOWN_AFTER_S + 60, exhausted=True)
    assert notifier.kinds().count("critical") == 2


def test_a_bot_restart_mid_outage_neither_repeats_nor_loses_the_notice(tmp_path):
    state_dir = tmp_path / "alert-state"
    first = Notifier(state_dir)
    first.poll(DOWN, T0).poll(DOWN, T0 + outage_alert.DOWN_AFTER_S)
    assert first.kinds() == ["down"]
    # A brand-new Notifier over the same state directory is the restarted
    # bot: the persisted incident survives, so no second down notice —
    # and the recovery notice still fires when the listener returns.
    restarted = Notifier(state_dir)
    restarted.poll(DOWN, T0 + outage_alert.DOWN_AFTER_S + 60)
    assert restarted.kinds() == []
    restarted.poll(UP, T0 + 4000)
    assert restarted.kinds() == ["up"]


# ------------------------------------------------------------- the budget

def test_budget_exhaustion_then_recovery_after_the_window(tmp_path):
    driver = BudgetDriver(_data(tmp_path))
    # Six exits inside one 600s window: the sixth crosses budget 5.
    results = [driver.exit(T0 + i * 10) for i in range(BUDGET + 1)]
    assert results == [False] * BUDGET + [True]
    record = driver.record()
    assert (record["restarts"], record["budget"], record["window_s"],
            record["exhausted"]) == (6, BUDGET, WINDOW, True)
    # More exits in the same window keep spending the same window's
    # count — no reset, no second exhaustion event to double-report.
    driver.exit(T0 + 120)
    assert driver.record()["restarts"] == 7 and driver.record()["exhausted"] is True
    # Once the window has fully elapsed, the next exit opens a fresh
    # window: count back to 1, exhaustion cleared — recovery.
    driver.exit(T0 + WINDOW + 1)
    record = driver.record()
    assert (record["restarts"], record["exhausted"]) == (1, False)


def test_persisted_budget_survives_a_process_restart(tmp_path):
    """The record supervise.sh writes via the CLI is read back verbatim
    by a different process: no amnesia, and the spend is not doubled."""
    env = {**os.environ, "PRINT_HANDY__PATHS__HERMES_HOME": f"{tmp_path}/hermes"}
    proc = subprocess.run(
        [sys.executable, "-m", "print_handy.supervisor_state",
         "--printer", PID, "--restarts", "6", "--budget", "5",
         "--window", "600", "--status", "1", "--exhausted"],
        capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    record = supervisor_state.read(PID, data=_data(tmp_path))
    assert record is not None
    assert (record["restarts"], record["exhausted"],
            record["last_exit_status"]) == (6, True, 1)
    # The record sits exactly where any other reader looks for it.
    assert supervisor_state.path_for(PID, data=_data(tmp_path)).is_file()


# ------------------------------------------------------------- the heartbeat

def test_heartbeat_staleness_and_loop_lag_on_a_fake_clock(tmp_path):
    data = _data(tmp_path)
    healthcheck.beat(data=data, printer_id=PID, loop_lag_ms=5)
    ok, _reason = healthcheck.check(data, now=time.time() + 5, printer_id=PID)
    assert ok is True
    # Ninety-plus seconds of silence on the fake clock: stale.
    ok, reason = healthcheck.check(data, now=time.time() + 10_000, printer_id=PID)
    assert ok is False and "stale" in reason
    # A fresh beat whose event loop is blocked trips the lag wire at
    # once — the degradation signal behind the outage alerts above.
    healthcheck.beat(data=data, printer_id=PID, loop_lag_ms=200_000)
    ok, reason = healthcheck.check(data, now=time.time() + 5, printer_id=PID)
    assert ok is False and "blocked or stalled" in reason
