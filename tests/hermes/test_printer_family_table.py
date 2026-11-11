"""#1241: one table over every printer family for start read-back and alert-button routing.

`/print-stop` already has its per-family table (tests/hermes/test_print_stop.py). This file is the
same idea for the other two acceptance rows of #1147:

- the start verdict each family's print path reaches ignores a leftover state: a reprint's previous
  run, finished with the same file name, is never reported as started, while the printer really
  running that file is;
- a Resume or Cancel press on the second printer's alert reaches only that printer's adapter.

Every family is here: Moonraker (stock Klipper and the AD5X on ZMod, both judged by the Moonraker
read-back classifier), Bambu Lab LAN, OctoPrint, PrusaLink, Elegoo SDCP and stock Flashforge. Each
family's status goes through its real adapter's ``normalize_status`` from a raw answer shaped like
that family's API, so the table pins the state mapping the verdict and the buttons depend on.
Nothing opens a socket.
"""
from unittest import mock

import pytest

from print_handy import printers, settings
from print_handy.core import model_job
from print_handy.hermes import family_control
from print_handy.hermes import print_alert_buttons as pab

FILE = "plate_1_abc.gcode"
BAMBU_PACKAGE = "plate_1_abc.gcode.3mf"


# --------------------------------------------------------------------------- raw answers per family
# (state, filename, nozzle target) -> the raw status that family's client returns.

def _octoprint(state, filename, target=0):
    flags = {"printing": state == "printing", "paused": state == "paused",
             "ready": state not in ("printing", "paused"), "operational": True}
    return {"printer": {"state": {"flags": flags},
                        "temperature": {"tool0": {"actual": 25, "target": target},
                                        "bed": {"actual": 25, "target": 0}}},
            "job": {"job": {"file": {"path": filename}},
                    "progress": {"completion": 100 if state == "complete" else 40}}}


def _prusalink(state, filename, target=0):
    word = {"printing": "PRINTING", "paused": "PAUSED", "complete": "FINISHED", "standby": "IDLE"}[state]
    return {"status": {"printer": {"state": word, "target_nozzle": target, "target_bed": 0}},
            "job": {"file": {"display_name": filename}, "progress": 100 if state == "complete" else 40}}


def _sdcp(state, filename, target=0):
    code = {"printing": 1, "paused": 5, "complete": 8, "standby": 0}[state]
    return {"Status": {"CurrentStatus": [code], "TargetTempOfNozzle": target, "TargetTempOfHotbed": 0},
            "PrintInfo": {"Filename": "/local/" + filename, "CurrentLayer": 10, "TotalLayer": 100}}


def _flashforge_stock(state, filename, target=0):
    word = {"printing": "printing", "paused": "pause", "complete": "completed", "standby": "ready"}[state]
    return {"status": word, "printFileName": filename, "printProgress": 1.0 if state == "complete" else 0.4,
            "rightTargetTemp": target, "platTargetTemp": 0}


def _bambu(state, filename, target=0):
    word = {"printing": "RUNNING", "paused": "PAUSE", "complete": "FINISH", "standby": "IDLE"}[state]
    # The printer shows the bare project name (subtask_name) for the package it was sent.
    return {"print": {"gcode_state": word, "subtask_name": filename.removesuffix(".gcode.3mf"),
                      "nozzle_target_temper": target, "bed_target_temper": 0,
                      "mc_percent": 100 if state == "complete" else 40}}


# connection -> (a model of that family, its raw builder, the client read its status uses, the remote
# name its print path uploads, the start matcher its print path polls with)
FAMILIES = {
    "octoprint": ("octoprint.ender3", _octoprint, "status", FILE, model_job._reports_started),
    "prusalink": ("prusa.mk4s", _prusalink, "snapshot", FILE, model_job._reports_started),
    "elegoo_sdcp": ("elegoo_sdcp.centauricarbon", _sdcp, "status", FILE, model_job._reports_started),
    "flashforge_stock": ("flashforge_stock.ad5x", _flashforge_stock, "detail", FILE, model_job._reports_started),
    "bambulab_lan": ("bambulab.a1", _bambu, "snapshot", BAMBU_PACKAGE, model_job._bambu_reports_file),
}
MOONRAKER_MODELS = ("klipper.generic", "flashforge.ad5x")  # stock Klipper and the AD5X on ZMod


def _adapter(model):
    adapter = printers.get(model, settings.current())
    assert family_control.connection_of(adapter) in (*FAMILIES, "moonraker"), model
    return adapter


class FakeLease:
    def __init__(self):
        self.calls = []

    def release(self):
        self.calls.append("release")

    def uncertain(self, detail):
        self.calls.append("uncertain")


def _polled_verdict(connection, raw):
    """The outcome a polled family's print path reports for one status read."""
    model, _build, _read, remote, reports = FAMILIES[connection]
    adapter = _adapter(model)
    poll = model_job._poll_start_confirmation(lambda: adapter.normalize_status(raw), remote, window_s=0,
                                              reports=reports)
    lease = FakeLease()
    with mock.patch.object(model_job, "release_start_announcement"):
        out = model_job._settle_polled_start(lease, poll, remote, method=connection)
    return out, lease.calls


# --------------------------------------------------------------------------- start read-back

@pytest.mark.parametrize("connection", sorted(FAMILIES))
def test_a_reprints_leftover_finished_state_is_never_reported_started(connection):
    _model, build, _read, remote, _reports = FAMILIES[connection]
    for leftover in (build("complete", remote), build("standby", remote)):
        out, lease = _polled_verdict(connection, leftover)
        assert out["started"] is False and out["outcome"] == "unconfirmed", (connection, out)
        assert lease == ["uncertain"]  # held for the listener to settle, never resubmitted


@pytest.mark.parametrize("connection", sorted(FAMILIES))
def test_the_printer_running_the_new_upload_is_reported_started(connection):
    _model, build, _read, remote, _reports = FAMILIES[connection]
    out, lease = _polled_verdict(connection, build("printing", remote, target=215))
    assert out["started"] is True and out["outcome"] in ("printing", "preparing"), (connection, out)
    assert lease == ["release"]


@pytest.mark.parametrize("connection", sorted(FAMILIES))
def test_another_file_running_is_not_this_start(connection):
    _model, build, _read, remote, _reports = FAMILIES[connection]
    out, _lease = _polled_verdict(connection, build("printing", "someone-elses.gcode", target=215))
    assert out["started"] is False, (connection, out)


def _moonraker(state, filename, duration, target=0):
    return {"print_stats": {"state": state, "filename": filename, "total_duration": duration,
                            "print_duration": duration},
            "extruder": {"target": target}, "heater_bed": {"target": 0},
            "toolhead": {"homed_axes": "xyz"}, "webhooks": {"state": "ready"}}


@pytest.mark.parametrize("model", MOONRAKER_MODELS)
def test_moonraker_families_judge_the_start_against_the_baseline(model):
    assert family_control.connection_of(_adapter(model)) == "moonraker"
    leftover = _moonraker("complete", FILE, 3600.0)
    # The previous run, unchanged since the baseline: same name, axes still homed, not this start.
    assert model_job.classify_start_readback(leftover, baseline=leftover).outcome == "unconfirmed"
    # A new job loaded since the baseline and running is.
    started = _moonraker("printing", FILE, 4.0, target=215)
    assert model_job.classify_start_readback(started, baseline=leftover).outcome == "printing"


# --------------------------------------------------------------------------- alert-button routing

TWO_PRINTERS = [{"id": "printer-a"}, {"id": "printer-b"}]


def _roster():
    return mock.patch("print_handy.settings.printers", return_value=TWO_PRINTERS), \
        mock.patch("print_handy.settings.default_printer_id", return_value="printer-a")


class FakeClient:
    """One family client: its status read walks `raws`, staying on the last."""

    def __init__(self, read, raws):
        self.raws, self.reads = list(raws), 0
        setattr(self, read, self._next)

    def _next(self):
        self.reads += 1
        return self.raws.pop(0) if len(self.raws) > 1 else self.raws[0]


def _family_printer(connection, raws):
    """A real adapter of the family whose client is fake and whose control() records."""
    model, _build, read, _remote, _reports = FAMILIES[connection]
    adapter = _adapter(model)
    client = FakeClient(read, raws)
    adapter.client = lambda: client
    adapter.control = mock.Mock(return_value=None)
    return adapter, client


@pytest.mark.parametrize("action", ["resume", "cancel"])
@pytest.mark.parametrize("connection", sorted(FAMILIES))
def test_a_press_on_the_second_printers_alert_reaches_only_that_printers_adapter(monkeypatch, connection,
                                                                                 action):
    _model, build, _read, remote, _reports = FAMILIES[connection]
    shown = build("printing", remote).get("print", {}).get("subtask_name") if connection == "bambulab_lan" \
        else remote
    if action == "resume":
        b_raws = [build("paused", remote), build("printing", remote)]
    else:
        b_raws = [build("printing", remote), build("complete", remote)]
    a_raws = [build("paused" if action == "resume" else "printing", remote)]  # the first printer could act too
    printer_a, client_a = _family_printer(connection, a_raws)
    printer_b, client_b = _family_printer(connection, b_raws)
    monkeypatch.setattr(family_control, "adapter_for",
                        lambda printer_id=None: printer_b if printer_id == "printer-b" else printer_a)

    class QuickControl(family_control.FamilyControl):
        def __init__(self, adapter, **kwargs):
            super().__init__(adapter, sleep=lambda s: None)

    monkeypatch.setattr(family_control, "FamilyControl", QuickControl)
    alert = {"kind": "paused", "filename": shown, "printer_id": "printer-b", "connection": connection}
    roster, default = _roster()
    with roster, default, mock.patch("print_handy.hermes.print_control.status") as mr_status, \
            mock.patch("print_handy.hermes.print_control.resume") as mr_resume, \
            mock.patch("print_handy.hermes.print_stop.cancel") as mr_cancel, \
            mock.patch.object(pab, "_repost_alert"):
        text = pab.handle_click(action, alert)
    printer_b.control.assert_called_once_with("resume" if action == "resume" else "stop")
    assert ("Resumed" if action == "resume" else "Stopped") in text, (connection, text)
    printer_a.control.assert_not_called()
    assert client_a.reads == 0  # the first printer is never even read
    for moonraker in (mr_status, mr_resume, mr_cancel):
        moonraker.assert_not_called()


@pytest.mark.parametrize("connection", sorted(FAMILIES))
def test_an_alert_whose_printer_is_another_family_sends_nothing(monkeypatch, connection):
    """An alert naming one family, pressed for a roster printer of another, never reaches either."""
    other = next(c for c in sorted(FAMILIES) if c != connection)
    _model, build, _read, remote, _reports = FAMILIES[other]
    printer_b, client_b = _family_printer(other, [build("paused", remote)])
    monkeypatch.setattr(family_control, "adapter_for", lambda printer_id=None: printer_b)
    roster, default = _roster()
    with roster, default:
        text = pab.handle_click("resume", {"kind": "paused", "filename": FILE, "printer_id": "printer-b",
                                           "connection": connection})
    assert "Nothing sent" in text
    printer_b.control.assert_not_called()


@pytest.mark.parametrize("action", ["resume", "cancel"])
@pytest.mark.parametrize("model", MOONRAKER_MODELS)
def test_moonraker_families_route_the_press_to_the_alerts_printer(monkeypatch, model, action):
    assert family_control.connection_of(_adapter(model)) == "moonraker"
    paused = {"klippy": "ready", "state": "paused", "filename": FILE}
    control_for = mock.Mock()
    monkeypatch.setattr(family_control, "control_for", control_for)
    roster, default = _roster()
    with roster, default, mock.patch("print_handy.hermes.print_control.status", return_value=paused) as status, \
            mock.patch("print_handy.hermes.print_control.resume", return_value=(True, "Resumed.")) as resume, \
            mock.patch("print_handy.hermes.print_stop.cancel", return_value=(True, "Stopped.")) as cancel:
        pab.handle_click(action, {"kind": "paused", "filename": FILE, "printer_id": "printer-b"})
    status.assert_called_once_with(printer_id="printer-b")
    if action == "resume":
        resume.assert_called_once_with(FILE, printer_id="printer-b")
        cancel.assert_not_called()
    else:
        cancel.assert_called_once_with(FILE, printer_id="printer-b")
        resume.assert_not_called()
    control_for.assert_not_called()  # a Moonraker alert never takes the family path
