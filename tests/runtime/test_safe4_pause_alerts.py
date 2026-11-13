"""SAFE-4 / P1-101: a launched print's pause alerts carry its printer_id.

All five family monitor factories used to build their pause alerts
from the listener process's instance identity — which is None in the
print_now process — so a click on a secondary printer's pause alert
resolved control_for(None): the default printer. Each factory now
takes the launched job's printer_id and hands it to pause_alerts_for.
"""
from types import SimpleNamespace as S

import pytest

from print_handy.runtime import (bambu_watch, elegoo_watch, octo_watch,
                                prusa_watch, stock_watch)

REMOTE = "benchy.gcode"


class CapturedMonitor:
    """Stand-in for a family's monitor class: records its kwargs."""

    instances = []

    def __init__(self, filename, **kwargs):
        self.filename = filename
        self.kwargs = kwargs
        CapturedMonitor.instances.append(self)

    def run(self):
        return None


class _Thread:
    def __init__(self, target):
        self._target = target

    def start(self):
        pass  # never run the monitor loop in these wiring tests


def _adapter(connection, client, config=None):
    return S(connection=connection, config=config or {}, client=lambda: client,
             normalize_status=lambda raw: raw, camera_snapshot=lambda: None)


CASES = [
    (bambu_watch, "BambuPrintMonitor",
     lambda: _adapter("bambulab_lan", S(snapshot=lambda: {}))),
    (elegoo_watch, "ElegooPrintMonitor",
     lambda: _adapter("elegoo_sdcp", S(status=lambda: {}),
                      {"elegoo_sdcp": {"host": "192.0.2.30"}})),
    (octo_watch, "OctoPrintMonitor",
     lambda: _adapter("octoprint", S(status=lambda: {}, timelapse_movies=lambda: []))),
    (prusa_watch, "PrusaPrintMonitor",
     lambda: _adapter("prusalink", S(snapshot=lambda: {}))),
    (stock_watch, "StockPrintMonitor",
     lambda: _adapter("flashforge_stock", S(detail=lambda: {}),
                      {"flashforge_stock": {"host": "192.0.2.40"}})),
]


@pytest.mark.parametrize("module,monitor_name,make_adapter",
                         CASES, ids=[c[0].__name__ for c in CASES])
def test_start_monitor_carries_the_launched_jobs_printer_id(
        monkeypatch, module, monitor_name, make_adapter):
    CapturedMonitor.instances = []
    monkeypatch.setattr(module, monitor_name, CapturedMonitor)
    thread = module.start_monitor(make_adapter(), REMOTE, post=lambda _m: True,
                                  thread_factory=_Thread, printer_id="workshop-ad5x")
    assert thread is not None
    assert len(CapturedMonitor.instances) == 1
    alerts = CapturedMonitor.instances[0].kwargs["pause_alerts"]
    assert alerts is not None
    assert alerts.printer_id == "workshop-ad5x"
    assert alerts.filename == REMOTE


@pytest.mark.parametrize("module,monitor_name,make_adapter",
                         CASES, ids=[c[0].__name__ for c in CASES])
def test_start_monitor_without_a_job_id_keeps_the_instance_fallback(
        monkeypatch, module, monitor_name, make_adapter):
    # No launched job id (a listener-side start): the old behaviour —
    # pause_alerts_for falls back to the instance identity (None here).
    CapturedMonitor.instances = []
    monkeypatch.setattr(module, monitor_name, CapturedMonitor)
    thread = module.start_monitor(make_adapter(), REMOTE, post=lambda _m: True,
                                  thread_factory=_Thread)
    assert thread is not None
    alerts = CapturedMonitor.instances[0].kwargs["pause_alerts"]
    assert alerts is not None and alerts.printer_id is None
