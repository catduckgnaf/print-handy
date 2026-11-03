"""An uncertain start on a printer whose clock reset settles without the operator (2026-11-03).

The AD5X (ZMod) loses its clock on power up and stamps its job history with its build date.
A start whose response timed out stayed uncertain because its own run, stamped months before
the lease, failed the time rule, and the next print was refused. Offline: nothing contacts a printer.
"""
import time
from unittest import mock

import pytest

from print_handy.core import launch_state

REMOTE = "Panel-A-Lower-V21_plate_1_ad4a.gcode.3mf"
ZMOD_NAME = "3mf_print/Panel-A-Lower-V21_plate_1_ad4a.gcode"
LEASED_AT = "2026-11-03T09:15:42-0500"
RESET_CLOCK_STAMP = 1767218054.77  # what the printer recorded for the 09:19 run: 2025-12-31
BASELINE = ["id:000060", "id:000061", "id:000062"]


def row():
    return {"remote": REMOTE, "leased_at": LEASED_AT}


def test_a_newer_job_id_settles_the_run_whatever_the_reset_clock_stamped():
    own = ("000063", ZMOD_NAME, RESET_CLOCK_STAMP)
    assert launch_state.reached_printer(row(), "", [own], baseline=BASELINE)


def test_an_older_job_id_of_the_same_file_never_settles_it():
    # A reprint: the same file ran before this submission and dropped out of the baseline window.
    older = ("00005f", ZMOD_NAME, RESET_CLOCK_STAMP)
    assert not launch_state.reached_printer(row(), "", [older], baseline=BASELINE)


def test_a_baseline_run_or_another_file_never_settles_it():
    assert not launch_state.reached_printer(row(), "", [("000062", ZMOD_NAME, RESET_CLOCK_STAMP)],
                                            baseline=BASELINE)
    assert not launch_state.reached_printer(row(), "", [("000063", "3mf_print/Other_plate_1.gcode",
                                                         RESET_CLOCK_STAMP)], baseline=BASELINE)


def test_without_a_baseline_the_time_rule_alone_still_decides():
    # No pre-submission history read: the id rule has nothing to compare against.
    assert not launch_state.reached_printer(row(), "", [("000063", ZMOD_NAME, RESET_CLOCK_STAMP)])
    assert not launch_state.reached_printer(row(), "", [("job-new", ZMOD_NAME, RESET_CLOCK_STAMP)],
                                            baseline=["id:job-old"])


def test_the_next_launch_proceeds_once_the_reset_clock_run_is_in_history():
    key = "flashforge-ad5x"
    history = [("000062", "3mf_print/Panel-A-Upper_plate_1.gcode", time.time() - 9000)]
    lease = launch_state.acquire(key, job_json="/archive/a/JOB.json", gcode_sha256="a" * 64,
                                 remote=REMOTE, started_files=lambda: list(history))
    lease.submitting()
    lease.uncertain("TimeoutError")
    with mock.patch.object(launch_state, "_owner_alive", return_value=False):
        with pytest.raises(SystemExit, match="earlier start on this printer is uncertain"):
            launch_state.acquire(key, job_json="/archive/b/JOB.json", gcode_sha256="b" * 64,
                                 remote="Next.gcode.3mf", started_files=lambda: list(history))
        history.insert(0, ("000063", ZMOD_NAME, RESET_CLOCK_STAMP))
        launch_state.acquire(key, job_json="/archive/b/JOB.json", gcode_sha256="b" * 64,
                             remote="Next.gcode.3mf", started_files=lambda: list(history)).release()
    assert launch_state.pending(key) == []


def test_the_listener_settles_an_uncertain_lease_when_the_printer_runs_its_file():
    key = "flashforge-ad5x"
    lease = launch_state.acquire(key, job_json="/archive/a/JOB.json", gcode_sha256="a" * 64, remote=REMOTE)
    assert launch_state.settle_observed(key, ZMOD_NAME) is None  # leased: nothing was sent yet
    lease.submitting()
    lease.uncertain("TimeoutError")
    assert launch_state.settle_observed(key, "3mf_print/Other_plate_1.gcode") is None
    assert launch_state.settle_observed(key, "") is None
    assert launch_state.pending(key)[0]["phase"] == "uncertain"
    settled = launch_state.settle_observed(key, ZMOD_NAME)
    assert settled["phase"] == "uncertain" and settled["remote"] == REMOTE
    assert launch_state.pending(key) == []
    lease.release()  # the owner's own token-scoped release is a harmless no-op afterwards
    assert launch_state.settle_observed(key, ZMOD_NAME) is None


YEAR = 365 * 86400
HOST_START = 1793715549.0  # the 09:19:09 host time the run really began


@pytest.mark.parametrize("offset", [-YEAR, -600, 0, 600])
@pytest.mark.parametrize("synced_before_read", [False, True])
def test_settling_never_depends_on_the_printer_clock(monkeypatch, offset, synced_before_read):
    """Every combination of printer clock error and a clock sync between the run and the
    history read: the submission's own run settles its lease, an earlier same-file run never does."""
    from print_handy.core import model_job
    own = {"job_id": "000063", "filename": ZMOD_NAME, "start_time": HOST_START + offset}
    earlier = {"job_id": "00005f", "filename": ZMOD_NAME, "start_time": HOST_START - 3 * 86400 + offset}
    monkeypatch.setattr(model_job, "_mr", lambda st, path, timeout=10: {"result": {"jobs": [own, earlier]}})
    skew = 0 if synced_before_read else -offset  # host minus printer
    monkeypatch.setattr(launch_state, "printer_clock_skew", lambda url, timeout=6: skew)
    monkeypatch.setattr(model_job.time, "time", lambda: HOST_START + 7200)
    history = model_job._moonraker_started_files({"moonraker_url": "http://m"})
    assert launch_state.reached_printer(row(), "", history, baseline=BASELINE)
    assert not launch_state.reached_printer(row(), "", [h for h in history if h[0] == "00005f"],
                                            baseline=BASELINE)
