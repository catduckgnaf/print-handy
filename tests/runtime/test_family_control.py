"""family_control (#460): the shared start/pause parity mechanism.

Offline throughout: fake adapters record every control call, scripted
statuses drive the state machine. The load-bearing assertions are the
negative ones — no control call happens without an operator action
(resume/cancel invoked from a button press), and a refused press sends
nothing at all.
"""
import pytest

from print_handy.runtime import family_control as fc

REMOTE = "benchy.gcode"


def st(state, filename=REMOTE, **extra):
    return {"state": state, "filename": filename, "progress": 0.5, **extra}


def scripted(values):
    items = list(values)

    def next_value():
        item = items.pop(0) if len(items) > 1 else items[0]
        if isinstance(item, Exception):
            raise item
        return item

    return next_value


class FakeAdapter:
    """One family's adapter: a connection name and a recording control()."""

    def __init__(self, connection):
        self.connection = connection
        self.transport = connection
        self.calls = []

    def control(self, action):
        self.calls.append(action)
        return True


def control_for(statuses, connection="prusalink"):
    adapter = FakeAdapter(connection)
    control = fc.FamilyControl(adapter, status_fn=scripted(statuses),
                               sleep=lambda _s: None)
    return adapter, control


# -- resume -------------------------------------------------------------------

def test_resume_reaches_the_familys_own_control_once():
    adapter, control = control_for([st("paused"), st("printing")])
    ok, text = control.resume(REMOTE)
    assert ok and "Resumed" in text
    assert adapter.calls == ["resume"]


def test_resume_refuses_when_nothing_is_paused():
    adapter, control = control_for([st("printing")])
    ok, text = control.resume(REMOTE)
    assert not ok and "already printing" in text
    assert adapter.calls == []

    adapter, control = control_for([st("standby")])
    ok, text = control.resume(REMOTE)
    assert not ok and "Nothing sent" in text
    assert adapter.calls == []


def test_resume_refuses_a_different_file_than_the_alert_names():
    adapter, control = control_for([st("paused", "other.gcode")])
    ok, text = control.resume(REMOTE)
    assert not ok and "different print" in text
    assert adapter.calls == []


def test_resume_reports_a_readback_that_never_prints():
    adapter, control = control_for([st("paused"), st("paused")])
    ok, text = control.resume(REMOTE, wait_s=0)
    # #837 bonus: the family mirror of print_control.resume must not call
    # a resume that never took a success either.
    assert not ok and "still reports paused" in text and "Not retrying" in text
    assert adapter.calls == ["resume"]


# -- cancel -------------------------------------------------------------------

def test_cancel_reaches_the_familys_own_control_once():
    adapter, control = control_for([st("paused"), st("standby")])
    ok, text = control.cancel(REMOTE)
    assert ok and "Stopped" in text
    assert adapter.calls == ["stop"]


def test_cancel_refuses_when_nothing_is_active():
    adapter, control = control_for([st("complete")])
    ok, text = control.cancel(REMOTE)
    assert not ok and "Nothing sent" in text
    assert adapter.calls == []


def test_cancel_refuses_a_different_file():
    adapter, control = control_for([st("printing", "other.gcode")])
    ok, text = control.cancel(REMOTE)
    assert not ok and "different print" in text
    assert adapter.calls == []


def test_bambu_cancel_readback_error_is_the_acknowledge_text():
    adapter, control = control_for([st("printing"), st("error")],
                                   connection="bambulab_lan")
    ok, text = control.cancel(REMOTE)
    assert ok and "acknowledge it on its screen" in text
    assert adapter.calls == ["stop"]


# -- control_for resolution -----------------------------------------------------

class _Current:
    def __init__(self, adapter):
        self._adapter = adapter

    def __call__(self, *_a, **_k):
        return self._adapter


def test_control_for_only_serves_family_printers(monkeypatch):
    from print_handy import printers
    family = FakeAdapter("elegoo_sdcp")
    monkeypatch.setattr(printers, "current", _Current(family))
    assert fc.control_for().connection == "elegoo_sdcp"

    moonraker = FakeAdapter("moonraker")
    monkeypatch.setattr(printers, "current", _Current(moonraker))
    assert fc.control_for() is None


# -- status_fn_for: the one read per family -------------------------------------

class _RawAdapter:
    def __init__(self, connection, client):
        self.connection = connection
        self._client = client

    def client(self):
        return self._client

    def normalize_status(self, raw):
        return {"echo": raw}


class _Client:
    def __init__(self, **answers):
        self._answers = answers

    def snapshot(self):
        return self._answers["snapshot"]

    def status(self):
        return self._answers["status"]

    def detail(self):
        return self._answers["detail"]


def test_status_fn_for_reads_each_family_the_way_its_monitor_does():
    cases = {
        "bambulab_lan": _Client(snapshot={"via": "snapshot"}),
        "prusalink": _Client(snapshot={"via": "snapshot"}),
        "elegoo_sdcp": _Client(status={"via": "status"}),
        "octoprint": _Client(status={"via": "status"}),
        "flashforge_stock": _Client(detail={"via": "detail"}),
    }
    for connection, client in cases.items():
        with pytest.MonkeyPatch.context():
            fn = fc.status_fn_for(_RawAdapter(connection, client))
            assert fn() == {"echo": client._answers.popitem()[1]}


def test_status_fn_for_rejects_a_non_family_connection():
    with pytest.raises(ValueError):
        fc.status_fn_for(_RawAdapter("moonraker", _Client()))


# -- PauseAlerts: the monitor-side tracker --------------------------------------

class Recorder:
    def __init__(self):
        self.posts = []
        self.retires = []

    def post(self, filename, connection, printer_id, status):
        self.posts.append((filename, connection, status))
        return True

    def retire(self, filename, headline):
        self.retires.append((filename, headline))
        return 1


def tracker(rec):
    return fc.PauseAlerts(REMOTE, "prusalink", poster=rec.post, retirer=rec.retire)


def test_pause_alerts_post_once_per_episode_and_retire_at_the_end():
    rec = Recorder()
    alerts = tracker(rec)
    alerts.note("printing", st("printing"))       # baseline: no post
    alerts.note("paused", st("paused"))           # episode 1
    alerts.note("paused", st("paused"))           # same episode: no duplicate
    alerts.note("printing", st("printing"))       # resumed at the printer
    alerts.note("paused", st("paused"))           # episode 2: a second alert
    alerts.note("complete", st("complete"))       # end: retire
    assert [p[0] for p in rec.posts] == [REMOTE, REMOTE]
    assert rec.retires == [(REMOTE, "Print complete.")]


def test_pause_alerts_resume_clears_the_pause_ping_episodes(monkeypatch, tmp_path):
    """A resume ends the pause for the starter-pings classifier too:
    the tracker's printing-after-paused transition clears the job's
    pause episodes, so a later pause is a new problem that can ping.
    Other kinds' episodes (the watchdog's) are not the pause's to clear."""
    import json

    from print_handy.runtime import starter_pings
    state = tmp_path / "starter_pings.json"
    monkeypatch.setattr(starter_pings, "STATE", state)
    state.write_text(json.dumps({REMOTE: {
        "episodes": {"paused:runout": 100.0, "watchdog": 90.0},
        "last_ping": 100.0}}))
    rec = Recorder()
    alerts = tracker(rec)
    alerts.note("printing", st("printing"))
    alerts.note("paused", st("paused"))
    alerts.note("printing", st("printing"))       # the resume
    data = json.loads(state.read_text())
    assert "paused:runout" not in data[REMOTE]["episodes"]
    assert data[REMOTE]["episodes"]["watchdog"] == 90.0


def test_pause_alerts_first_observation_is_a_baseline():
    rec = Recorder()
    alerts = tracker(rec)
    alerts.note("paused", st("paused"))           # monitor started mid-pause
    assert rec.posts == []
    alerts.note("cancelled", st("cancelled"))
    assert rec.retires == [(REMOTE, "Print cancelled.")]


def test_pause_alerts_never_actuates_the_printer():
    # The tracker holds a poster and a retirer only; driving it through a
    # full lifecycle makes no adapter control call of any kind (the fake
    # adapter is not even reachable from it).
    rec = Recorder()
    alerts = tracker(rec)
    for state in ("printing", "paused", "printing", "paused", "error"):
        alerts.note(state, st(state))
    assert len(rec.posts) == 2
    assert rec.retires == [(REMOTE, "Print stopped with an error.")]


# -- the two adapter control translations ----------------------------------------

def test_octoprint_adapter_translates_stop_to_cancel():
    from unittest import mock
    from print_handy.printers.octoprint.adapter import OctoPrintAdapter
    from print_handy.printers.octoprint.client import OctoPrintError

    calls = []

    class Client:
        def control(self, action):
            calls.append(action)
            return True

    adapter = object.__new__(OctoPrintAdapter)
    with mock.patch.object(OctoPrintAdapter, "client", lambda self: Client()):
        assert adapter.control("resume") is True
        assert adapter.control("stop") is True
        with pytest.raises(OctoPrintError):
            adapter.control("explode")
    assert calls == ["resume", "cancel"]


def test_stock_adapter_translates_resume_to_continue_via_jobctl():
    from unittest import mock
    from print_handy.printers.flashforge_stock.adapter import StockFlashforgeAdapter
    from print_handy.printers.flashforge_stock.client import StockLanError

    calls = []

    class Client:
        def control(self, cmd, args=None):
            calls.append((cmd, args))
            return True

    adapter = object.__new__(StockFlashforgeAdapter)
    with mock.patch.object(StockFlashforgeAdapter, "client", lambda self: Client()):
        assert adapter.control("resume") is True
        assert adapter.control("stop") is True
        with pytest.raises(StockLanError):
            adapter.control("explode")
    assert calls == [("jobCtl_cmd", {"jobID": "", "action": "continue"}),
                     ("jobCtl_cmd", {"jobID": "", "action": "cancel"})]


def test_pause_alerts_default_retirer_is_scoped_to_the_owning_printer(monkeypatch):
    """P2-176: with no retirer injected, the tracker retires through
    retire_pause_alerts carrying its own printer_id."""
    seen = []
    monkeypatch.setattr(
        fc, "retire_pause_alerts",
        lambda fn, headline, printer_id=None: seen.append((fn, headline, printer_id)) or 1)
    alerts = fc.PauseAlerts(REMOTE, "prusalink", "printer-a",
                            poster=lambda *a: True)
    alerts.note("printing", st("printing"))
    alerts.note("complete", st("complete"))
    assert seen == [(REMOTE, "Print complete.", "printer-a")]


def test_bambu_sink_default_retirer_is_scoped_to_the_owning_printer(monkeypatch):
    """P2-176: the Bambu sink's end-of-print retirement is scoped the
    same way when no retirer is injected."""
    seen = []
    monkeypatch.setattr(
        fc, "retire_pause_alerts",
        lambda fn, headline, printer_id=None: seen.append((fn, headline, printer_id)) or 1)
    sink = fc.BambuEventSink(claim_fn=lambda _f: True, printer_id="printer-b")
    sink("completion", {"status": {"state": "complete", "filename": REMOTE},
                        "transition": None})
    assert seen == [(REMOTE, "Print complete.", "printer-b")]


def test_default_printer_pause_alerts_post_and_retire_under_its_real_id(monkeypatch):
    """2.2.5 review (P2-176): printer_id None (the default printer) must not
    reach the retirer as "every printer" or be stored without an id."""
    from print_handy import settings
    from print_handy.runtime import print_alert_buttons as pab
    monkeypatch.setattr(settings, "default_printer_id", lambda data=None: "printer-a")
    mel = fc._mel()
    monkeypatch.setattr(mel, "_buttonless_standalone", lambda: False)
    monkeypatch.setattr(mel, "_bot_token", lambda: "tok")
    posted, retired = [], []
    monkeypatch.setattr(pab, "post_with_buttons",
                        lambda *a, **kw: posted.append(kw.get("printer_id")) or True)
    monkeypatch.setattr(pab, "retire_alerts",
                        lambda *a, **kw: retired.append(kw.get("printer_id")) or 1)
    assert fc.post_pause_alert(REMOTE, "prusalink", None, st("paused"))
    fc.retire_pause_alerts(REMOTE, "Print ended.", None)
    assert posted == ["printer-a"]
    assert retired == ["printer-a"]
