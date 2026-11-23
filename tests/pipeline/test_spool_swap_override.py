"""Mid-job spool swap: an operator can print a plate sliced for the old spool with the one now loaded
(same material family only), and pre-upload refusals leave as one REFUSED line."""
import contextlib
import copy
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from print_handy.core import model_job
from print_handy.gcode import verify as gcode_verify
from print_handy.jobs import cli as jobs_cli
from print_handy.launch import gates as launch_gates
from print_handy.launch import print_now as launch_print_now
from print_handy.printers.flashforge.zmod import multicolor_contract as contract
from tests.bound_names import patch_bound, setattr_bound


def _state():
    return {"filament_slots": {"2": {"spoolman_id": 7, "material": "PLA+", "physical_color_verified": True}}}


def test_swapped_spool_is_refused_without_confirmation(monkeypatch):
    monkeypatch.delenv("PRINT_ACCEPT_SPOOL_CHANGE", raising=False)
    with pytest.raises(RuntimeError, match=r"spool changed since slicing \(was 3, now 7\)"):
        contract.normalize_mapping({"0": {"physical_slot": 2, "spoolman_id": 3}}, _state())


def test_confirmed_swap_is_accepted(monkeypatch):
    monkeypatch.setenv("PRINT_ACCEPT_SPOOL_CHANGE", "1")
    assert contract.normalize_mapping({"0": {"physical_slot": 2, "spoolman_id": 3}}, _state()) == {"0": 2}


def test_confirmation_does_not_waive_unverified_slot(monkeypatch):
    monkeypatch.setenv("PRINT_ACCEPT_SPOOL_CHANGE", "1")
    state = copy.deepcopy(_state())
    state["filament_slots"]["2"]["physical_color_verified"] = False
    with pytest.raises(RuntimeError, match="unknown or unverified"):
        contract.normalize_mapping({"0": {"physical_slot": 2, "spoolman_id": 3}}, state)


def _gcode(d):
    g = Path(d) / "x.gcode"
    g.write_text("; estimated printing time (normal mode) = 1h 2m 0s\nSTART_PRINT EXTRUDER_TEMP=220 BED_TEMP=60\n")
    return g


def _ready(stack):
    stack.enter_context(mock.patch.object(model_job.printer_state, "load", return_value={"moonraker_url": "http://m"}))
    stack.enter_context(patch_bound(launch_gates, "_printer_phase", return_value=("standby", "x.gcode", "Ready")))
    stack.enter_context(mock.patch.object(launch_print_now, "live_snapshot", return_value={"slots": "unknown"}))
    stack.enter_context(patch_bound(launch_gates, "mesh_readiness", return_value={"problem": None}))


def test_print_now_reports_contract_refusal_as_one_line():
    with tempfile.TemporaryDirectory() as d, contextlib.ExitStack() as stack:
        _ready(stack)
        stack.enter_context(patch_bound(gcode_verify, "verify_gcode"))
        stack.enter_context(mock.patch("print_handy.printers.flashforge.zmod.multicolor_contract.embedded_mapping", return_value={"0": {}}))
        stack.enter_context(mock.patch("print_handy.printers.flashforge.zmod.multicolor_contract.normalize_mapping",
                                       side_effect=RuntimeError("slot 2 spool changed since slicing (was 3, now 7);\n reslice or confirm")))
        with pytest.raises(SystemExit) as exc:
            launch_print_now.print_now(_gcode(d))
    assert str(exc.value) == ("REFUSED: slot 2 spool changed since slicing (was 3, now 7); reslice or confirm. "
                              "Nothing uploaded or started.")


def test_print_now_reports_verify_failure_as_refused():
    with tempfile.TemporaryDirectory() as d, contextlib.ExitStack() as stack:
        _ready(stack)
        stack.enter_context(patch_bound(gcode_verify, "verify_gcode", side_effect=RuntimeError("bad gcode")))
        with pytest.raises(SystemExit, match="REFUSED: bad gcode. Nothing uploaded or started."):
            launch_print_now.print_now(_gcode(d))


def test_cli_flag_sets_the_confirmation_for_the_process(monkeypatch):
    monkeypatch.delenv("PRINT_ACCEPT_SPOOL_CHANGE", raising=False)
    seen = {}
    setattr_bound(monkeypatch, launch_print_now, "print_now",
                        lambda target, **kw: seen.update(env=os.environ.get("PRINT_ACCEPT_SPOOL_CHANGE")))
    monkeypatch.setattr("sys.argv", ["model_job", "print-now", "x.gcode", "--accept-spool-change"])
    jobs_cli.main()
    assert seen["env"] == "1"
    monkeypatch.delenv("PRINT_ACCEPT_SPOOL_CHANGE")
    seen.clear()
    monkeypatch.setattr("sys.argv", ["model_job", "print-now", "x.gcode"])
    jobs_cli.main()
    assert seen["env"] is None
