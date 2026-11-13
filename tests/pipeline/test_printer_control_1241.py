"""#1241: the printer-control items left after #1147, offline.

- One heater ceiling for the Web UI too: ``webui.controls.temperature_limit`` asks
  ``core.heater_ceiling.ceiling``, so the Web UI temperature routes, its raw G-code scan and
  print-now's G-code check refuse the same values for the same printer and hardware record.
- Bambu uncertain launch leases settle on their own: the Bambu listener's sink feeds every LAN
  status to the same ``family_control.LaunchLeaseSettler`` the polled families use.

The per-family start and alert-button table is tests/runtime/test_printer_family_table.py.
Every printer here is a fake or a profile; nothing opens a socket.
"""
import json
from pathlib import Path
from unittest import mock

import pytest

from print_handy import printers, settings
from print_handy.core import heater_ceiling, launch_state, model_job, printer_state
from print_handy.runtime import family_control
from print_handy.printers.bambulab.listener import BambuListener
from print_handy.webui import controls

PRINTER = "flashforge-ad5x"


# --------------------------------------------------------------------------- one heater ceiling

def _data(tmp_path):
    local = tmp_path / "local.yaml"
    local.write_text(f"paths:\n  state_home: {tmp_path}/hermes\nwebui:\n  controls_enabled: true\n")
    return settings.load(local=local, environ={})


def _printer(tmp_path, model, nozzle_type):
    """(data, view, adapter) for PRINTER as `model` with `nozzle_type` configured."""
    data = _data(tmp_path)
    view = settings.view_for(PRINTER, data)
    view["printer"]["nozzle_type"] = nozzle_type
    return data, view, printers.get(model, view)


def _record(tmp_path, record):
    """Point printer_state at a tmp root holding `record` (None: no record) for PRINTER."""
    state = tmp_path / "state" / "printer_hardware.json"
    if record is not None:
        target = state.parent / "printers" / PRINTER / "printer_hardware.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(record))
    return mock.patch.object(printer_state, "STATE", str(state))


@pytest.mark.parametrize("model", ["flashforge.ad5x", "flashforge_stock.ad5x"])
@pytest.mark.parametrize("fitted", ["hardened_steel", "stock"])
def test_an_ad5x_gets_the_same_ceiling_from_the_web_ui_and_the_gcode_check(tmp_path, model, fitted):
    _data_, view, adapter = _printer(tmp_path, model, "hardened_steel")
    record = {"printer": "AD5X", "nozzle": {"diameter_mm": 0.4, "type": fitted}}
    with _record(tmp_path, record):
        webui = (controls.temperature_limit(adapter, view, "nozzle", PRINTER),
                 controls.temperature_limit(adapter, view, "bed", PRINTER))
        assert webui == model_job._heater_limits(printer_state.load(PRINTER), adapter)
    assert webui[0] == 300  # the AD5X's published hotend maximum, whatever is fitted


@pytest.mark.parametrize("model", ["flashforge.ad5x", "klipper.generic", "octoprint.ender3", "bambulab.a1",
                                   "prusa.mk4s", "elegoo_sdcp.centauricarbon", "flashforge_stock.ad5x"])
@pytest.mark.parametrize("fitted", [None, "hardened_steel", "brass", "stock", "not-a-nozzle"])
@pytest.mark.parametrize("configured", ["hardened_steel", "stock"])
def test_every_web_ui_heater_limit_is_the_core_ceiling(tmp_path, model, fitted, configured):
    _data_, view, adapter = _printer(tmp_path, model, configured)
    record = None if fitted is None else {"printer": "P", "nozzle": {"diameter_mm": 0.4, "type": fitted}}
    with _record(tmp_path, record):
        for heater in ("nozzle", "bed", "chamber", "other"):
            assert controls.temperature_limit(adapter, view, heater, PRINTER) == \
                heater_ceiling.ceiling(adapter, heater, state=record, view=view), (heater, model, fitted)


def test_an_unrecognized_fitted_nozzle_is_held_to_brass_on_the_web_ui_too(tmp_path):
    """On main the Web UI fell back to the configured hardened steel (450 C) for a fitted type it
    could not read, while print-now held the same printer to brass (300 C)."""
    data, view, adapter = _printer(tmp_path, "klipper.generic", "hardened_steel")
    with _record(tmp_path, {"printer": "P", "nozzle": {"diameter_mm": 0.4, "type": "mystery alloy"}}):
        assert controls.temperature_limit(adapter, view, "nozzle", PRINTER) == 300
        sent = []
        code, body = controls.set_temperature(data, PRINTER, {"heater": "nozzle", "target": 320}, adapter=adapter,
                                              read=lambda a, v: {"state": "standby"},
                                              send=lambda *a: sent.append(a))
        assert code == 400 and "outside 0..300" in body["reason"]
        code, body = controls.send_gcode(data, PRINTER, {"script": "M104 S320"}, adapter=adapter,
                                         read=lambda a, v: {"state": "standby"},
                                         send=lambda *a: sent.append(a))
        assert code == 400 and "outside 0..300" in body["reason"]
    assert sent == []


def test_the_web_ui_keeps_no_ceiling_copy_of_its_own():
    for name in ("_fitted_nozzle_type", "_fact"):
        assert not hasattr(controls, name), name
    assert controls.UNKNOWN_HEATER_CEILING_C == heater_ceiling.UNKNOWN_HEATER_CEILING_C


# --------------------------------------------------------------------------- Bambu leases settle

PACKAGE = "plate_1_abc.gcode.3mf"


def uncertain_lease(printer_id, remote):
    lease = launch_state.acquire(printer_id, job_json=None, gcode_sha256="0" * 64, remote=remote)
    lease.submitting()
    lease.uncertain("submitted, awaiting confirmation")
    return lease


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def sink(clock, printer_id="garage-a1"):
    return family_control.BambuEventSink(claim_fn=lambda f: True, start_poster=lambda *a: None,
                                         pause_poster=lambda *a: None, terminal_poster=lambda *a: None,
                                         retirer=lambda *a: None, printer_id=printer_id, clock=clock)


def status(state, filename="", nozzle=0, bed=0):
    return {"kind": "status", "status": {"state": state, "filename": filename, "hotend_target_c": nozzle,
                                         "bed_target_c": bed}, "transition": None}


def test_a_bambu_status_on_the_leases_file_settles_it_as_started():
    uncertain_lease("garage-a1", PACKAGE)
    clock = Clock()
    target = sink(clock)
    # The printer shows the bare project name for the package it was sent; PREPARE reads as printing.
    target("status", status("printing", "plate_1_abc", nozzle=220, bed=65))
    assert launch_state.held("garage-a1") is None


def test_a_bambu_lease_is_released_after_the_printer_stays_idle_and_cold():
    uncertain_lease("garage-a1", PACKAGE)
    clock = Clock()
    target = sink(clock)
    for at in (1000.0, 1030.0):
        clock.now = at
        target("status", status("standby"))
        assert launch_state.held("garage-a1") is not None  # inside the cold window
    clock.now = 1061.0
    target("status", status("standby"))
    assert launch_state.held("garage-a1") is None


def test_heat_or_another_file_never_settles_a_bambu_lease():
    uncertain_lease("garage-a1", PACKAGE)
    clock = Clock()
    target = sink(clock)
    settler = target._settler
    for at in (1000.0, 1100.0, 1200.0):  # heating for this start: preparing, not abandoned
        clock.now = at
        assert settler.observe("standby", "", status("standby", nozzle=220)["status"], now=at) is None
    assert settler.observe("printing", "someone-elses", status("printing", "someone-elses")["status"],
                           now=1300.0) is None
    for at in (1400.0, 1500.0):  # an earlier file shown finished is not this start's story
        assert settler.observe("complete", "earlier", status("complete", "earlier")["status"], now=at) is None
    assert launch_state.held("garage-a1")["phase"] == "uncertain"


@pytest.mark.parametrize("ended", ["complete", "cancelled", "error", "standby"])
def test_a_bambu_lease_whose_own_file_is_seen_ended_is_left_uncertain(ended):
    # The listener first sees the printer after the print finished (or missed
    # every active update): the lease's own file in a terminal state, heaters
    # cold. That print may have run, so it is never released as not started,
    # and a finished state on the same file is never "started" either (a
    # reprint's previous run, #1147). It stays uncertain for the operator.
    uncertain_lease("garage-a1", PACKAGE)
    clock = Clock()
    target = sink(clock)
    for at in (1000.0, 1030.0, 1061.0, 1200.0):
        clock.now = at
        target("status", status(ended, "plate_1_abc"))
        assert launch_state.held("garage-a1")["phase"] == "uncertain"
    # The operator clears the screen: idle, no file, cold well past the window.
    for at in (1300.0, 1400.0, 1500.0):
        clock.now = at
        target("status", status("standby"))
    assert launch_state.held("garage-a1")["phase"] == "uncertain"


def test_a_bambu_cold_window_already_running_stops_once_the_own_file_shows_ended():
    uncertain_lease("garage-a1", PACKAGE)
    settler = sink(Clock())._settler
    assert settler.observe("standby", "", status("standby")["status"], now=1000.0) is None
    assert settler.observe("complete", "plate_1_abc", status("complete", "plate_1_abc")["status"],
                           now=1030.0) is None
    assert settler.observe("standby", "", status("standby")["status"], now=1061.0) is None
    assert settler.observe("standby", "", status("standby")["status"], now=1200.0) is None
    assert launch_state.held("garage-a1")["phase"] == "uncertain"


def test_the_bambu_sink_reads_the_lease_database_only_on_change_or_every_few_seconds():
    clock = Clock()
    observed = []
    target = family_control.BambuEventSink(claim_fn=lambda f: True, start_poster=lambda *a: None,
                                           retirer=lambda *a: None, printer_id="garage-a1", clock=clock,
                                           settler=mock.Mock(observe=lambda *a, **k: observed.append(a[:2])))
    for at in (1000.0, 1001.0, 1002.0):  # temperature deltas while printing: one read
        clock.now = at
        target("status", status("printing", "plate_1_abc", nozzle=200 + at - 1000))
    clock.now = 1003.0
    target("status", status("paused", "plate_1_abc"))  # a state change reads at once
    clock.now = 1003.0 + family_control.BambuEventSink.LEASE_CHECK_S
    target("status", status("paused", "plate_1_abc"))  # and the window re-reads
    target("completion", status("complete", "plate_1_abc"))  # only status events feed it
    assert observed == [("printing", "plate_1_abc"), ("paused", "plate_1_abc"), ("paused", "plate_1_abc")]


def test_a_failing_lease_read_never_breaks_the_bambu_posts():
    posted = []
    target = family_control.BambuEventSink(claim_fn=lambda f: False, start_poster=lambda *a: posted.append(a),
                                           retirer=lambda *a: None, printer_id="garage-a1", clock=Clock(),
                                           settler=mock.Mock(observe=mock.Mock(side_effect=OSError("locked"))))
    target("status", status("standby"))
    target("status", {**status("printing", "plate_1_abc"), "transition": "started"})
    assert posted == [("started", "plate_1_abc")]


class FakeA1:
    """The Bambu listener's adapter seam: real normalisation, LAN reported unconfigured."""

    def __init__(self):
        self._real = printers.get("bambulab.a1", settings.current())

    def normalize_status(self, raw):
        return self._real.normalize_status(raw)

    def lan_configured(self):
        return False

    def camera_kind(self):
        return None


def test_the_bambu_listener_telemetry_settles_the_lease_through_its_sink():
    """End to end from the LAN frame: push_status on the package's project name settles the lease."""
    uncertain_lease("garage-a1", PACKAGE)
    listener = BambuListener(FakeA1(), lan_client=None, emit=sink(Clock()), printer_id="garage-a1",
                             clock=Clock(), beat=lambda **k: None)
    listener._on_message({"print": {"command": "push_status", "msg": 0, "gcode_state": "PREPARE",
                                    "subtask_name": "plate_1_abc", "nozzle_target_temper": 220,
                                    "bed_target_temper": 65}})
    assert launch_state.held("garage-a1") is None


def test_the_bambu_listener_main_wires_the_settling_sink(monkeypatch):
    built = {}
    from print_handy import listeners

    class Probe:
        transport = "bambulab_lan"
        connection = "bambulab_lan"

    monkeypatch.setattr(printers, "current", lambda: Probe())
    monkeypatch.setattr(listeners, "instance_printer_id", lambda: "garage-a1")
    monkeypatch.setattr(family_control, "auto_light_for", lambda adapter: None)
    monkeypatch.setattr(family_control, "adapter_for", lambda printer_id=None: Probe())

    class Stub:
        def __init__(self, adapter, *, emit, printer_id, **kw):
            built.update(emit=emit, printer_id=printer_id)

        def run_forever(self):
            return 0

    from print_handy.printers.bambulab import listener as bambu_listener
    monkeypatch.setattr(bambu_listener, "BambuListener", Stub)
    monkeypatch.setattr("print_handy.logs.configure", lambda *a, **k: None)
    assert bambu_listener.main() == 0
    assert isinstance(built["emit"], family_control.BambuEventSink)
    assert isinstance(built["emit"]._settler, family_control.LaunchLeaseSettler)
    assert built["emit"]._settler._printer_id == "garage-a1"
