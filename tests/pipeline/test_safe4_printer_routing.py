"""SAFE-4: commands must reach the routed printer, not the default one.

Two printers on the roster (garage-ad5x = default, workshop-ad5x =
secondary). Each test drives one path bound to the secondary printer
and proves the printer_id reaches the layer that used to fall back to
the default printer: intake's recovery reboot (P1-79), the slice
worker's hardware record (P1-78), and G-code verification (P1-75).
"""
import asyncio
import json
from types import SimpleNamespace as S
from unittest.mock import AsyncMock

import pytest

from print_handy import settings
from print_handy.core import model_job, prepared_intake

TWO_PRINTERS = """
discord:
  chat_id: "111"
printers:
  - id: garage-ad5x
    name: Garage AD5X
    model: flashforge.ad5x
    moonraker_url: http://192.168.1.60:7125
  - id: workshop-ad5x
    name: Workshop AD5X
    model: flashforge.ad5x
    chat_id: "222"
    moonraker_url: http://192.168.1.61:7125
"""


@pytest.fixture
def two_printers(tmp_path, monkeypatch):
    local = tmp_path / "local.yaml"
    local.write_text(TWO_PRINTERS)
    data = settings.load(home=settings.HOME, local=local, environ={})
    assert [entry["id"] for entry in data["printers"]] == ["garage-ad5x", "workshop-ad5x"]
    monkeypatch.setattr(settings, "current", lambda: data)
    return data


# -- P1-79: intake's recovery reboot ------------------------------------------


def _run_offer(monkeypatch, printer_id):
    calls = []

    def force_reboot(*args, **kwargs):
        calls.append((args, kwargs))
        return True, "Printer restarted."

    fake_pc = S(REBOOT_LABEL="Force restart", CANCEL_REBOOT_LABEL="Cancel",
                force_reboot=force_reboot)
    monkeypatch.setattr(prepared_intake._external, "load", lambda name: fake_pc)

    async def ask(adapter, source, key, question, choices, timeout=None, **kw):
        return 0  # the operator pressed "Force restart"

    monkeypatch.setattr(prepared_intake, "_ask", ask)
    adapter = S(send=AsyncMock())
    source = S(chat_id="222", metadata=None)
    kwargs = {} if printer_id is None else {"printer_id": printer_id}
    out = asyncio.run(prepared_intake._offer_force_restart(
        adapter, source, "key", "The start was refused.", **kwargs))
    assert "Printer restarted." in out
    return calls


def test_offer_force_restart_reboots_the_routed_printer(monkeypatch):
    calls = _run_offer(monkeypatch, "workshop-ad5x")
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert kwargs.get("printer_id", args[0] if args else None) == "workshop-ad5x"


def test_offer_force_restart_without_a_route_reboots_the_default(monkeypatch):
    # Legacy single-printer behaviour: no routed id, force_reboot keeps
    # its own default (printer_id=None resolves the default printer).
    calls = _run_offer(monkeypatch, None)
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert kwargs.get("printer_id", args[0] if args else None) is None


# -- P1-78: the slice worker's hardware record ---------------------------------


def test_slice_job_loads_the_jobs_printer_record(monkeypatch, tmp_path, two_printers):
    loads = []

    def fake_load(printer_id=None):
        loads.append(printer_id)
        return {"spool": {"material": "PLA"}}

    monkeypatch.setattr(model_job.printer_state, "load", fake_load)
    for pid in ("garage-ad5x", "workshop-ad5x"):
        job = {"printer_id": pid, "downloaded_file": str(tmp_path / "x.txt"), "audit": {}}
        jp = tmp_path / f"JOB-{pid}.json"
        jp.write_text(json.dumps(job))
        # The .txt source refuses immediately after the record load, which
        # is the seam under test: which printer's record was loaded.
        with pytest.raises(RuntimeError, match="only STL/3MF"):
            model_job.slice_job(jp)
    assert loads == ["garage-ad5x", "workshop-ad5x"]


# -- P1-75: G-code verification -------------------------------------------------


def test_verify_gcode_uses_the_jobs_printer_not_the_default(monkeypatch, tmp_path):
    checks = []

    def fake_check(st=None, **kwargs):
        checks.append((st, kwargs))
        return True, [], {}

    monkeypatch.setattr(model_job.printer_state, "check", fake_check)
    # The default printer is a live Moonraker printer: if verification
    # resolved the singleton it would take the live path, never the
    # job printer's offline checks.
    default_adapter = S(connection="moonraker")
    monkeypatch.setattr(model_job.printers, "current", lambda *a, **k: default_adapter)

    job_state = {"spool": {"material": "PLA"}, "temps_c": {"nozzle": 210, "bed": 60}}
    job_adapter = S(connection="prusalink",
                    gcode_problems=lambda text, st: ["job printer problem"])
    gcode = tmp_path / "plate_1.gcode"
    gcode.write_text("G1 X1 Y1\n")
    with pytest.raises(RuntimeError, match="job printer problem"):
        model_job.verify_gcode(gcode, adapter=job_adapter, state=job_state,
                               printer_id="workshop-ad5x")
    assert checks and checks[0][0] is job_state
    assert checks[0][1].get("printer_id") == "workshop-ad5x"
    assert checks[0][1].get("adapter") is job_adapter

