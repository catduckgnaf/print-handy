"""One launch pipeline (#1603 item 1): the shared launch sequence and its hooks.

Each test pins the shared sequence through the production entry point its
callers use: _run_transport_launch for the five polled transports (the
TransportLaunch adapters), and the shared leased-upload steps both
Moonraker routes now call (_check_bed_clear / _consume_bed_clear,
_upload_via_moonrakerctl, _abandon_leased_upload).
"""
import inspect
from types import SimpleNamespace as S

import pytest

from print_handy.launch import print_now


class _Lease:
    """A launch_state lease that records what the sequence did with it."""

    def __init__(self, events):
        self.events = events
        self.token = "tok"

    def submitting(self):
        self.events.append("submitting")

    def release(self):
        self.events.append("lease.release")

    def uncertain(self, detail):
        self.events.append(f"lease.uncertain:{detail}")


def _rig(monkeypatch, events, *, confirmed=True):
    """Wire the runner's collaborators to recorders; returns the fake lease."""
    lease = _Lease(events)
    monkeypatch.setattr(print_now, "_printer_key", lambda state, job: "printer-key")
    monkeypatch.setattr(print_now.launch_state, "acquire",
                        lambda *a, **k: events.append("acquire") or lease)
    monkeypatch.setattr(print_now, "_claim_bed_clear",
                        lambda *a, **k: events.append("bed_clear"))
    monkeypatch.setattr(print_now, "_claim_start_quietly",
                        lambda remote, job: events.append(f"claim:{remote}"))
    monkeypatch.setattr(print_now, "release_start_announcement",
                        lambda remote: events.append(f"release_claim:{remote}"))
    monkeypatch.setattr(print_now, "_poll_start_confirmation",
                        lambda read, remote, **k: events.append("poll")
                        or print_now.StartPoll(confirmed, False, {"state": "printing"}, 1, 45.0))
    monkeypatch.setattr(print_now, "_assign_print_number", lambda *a, **k: 7)
    monkeypatch.setattr(print_now, "record_start", lambda *a, **k: "/records/7")
    return lease


def _spec(events, **overrides):
    hooks = dict(
        method="fake_method", label="Fake",
        preflight=lambda: events.append("preflight") or {"sha": "ab" * 32, "remote": "job.gcode",
                                                         "observed_file": "old.gcode"},
        recheck=lambda ctx: events.append("recheck") or {"state": "standby"},
        upload=lambda ctx: events.append("upload"),
        start=lambda ctx, remote: events.append(f"start:{remote}"),
        monitor=lambda ctx, remote: events.append(f"monitor:{remote}"),
        read_status=lambda ctx: lambda: {"state": "printing"},
        stage="fake_start_confirm",
        truth=lambda ctx: lambda live, rem: None)
    hooks.update(overrides)
    return print_now.TransportLaunch(**hooks)


def test_transport_launch_runs_the_shared_sequence_in_order(monkeypatch):
    events = []
    _rig(monkeypatch, events)
    out = print_now._run_transport_launch(
        _spec(events), state={}, gcode="job.gcode", job={}, job_path="JOB.json", reprint=False)
    # verify -> lease -> recheck idle inside the lease -> bed-clear ->
    # submitting -> upload -> claim -> start -> poll -> record -> monitor.
    # The bed-clear claim sits before submitting and before the upload
    # (#1586): a failed upload can never burn it downstream either, because
    # the polled transports' single claim is this one, inside the lease.
    assert events == ["preflight", "acquire", "recheck", "bed_clear", "submitting",
                      "upload", "claim:job.gcode", "start:job.gcode", "poll",
                      "lease.release", "monitor:job.gcode"]
    assert out["started"] is True and out["method"] == "fake_method"
    assert out["print_number"] == 7 and out["record_path"] == "/records/7"


def test_transport_launch_recheck_refusal_releases_the_lease_before_any_claim(monkeypatch):
    events = []
    _rig(monkeypatch, events)

    def refuse_recheck(ctx):
        events.append("recheck")
        raise SystemExit("REFUSED: busy. Nothing uploaded or started.")

    with pytest.raises(SystemExit, match="REFUSED: busy"):
        print_now._run_transport_launch(
            _spec(events, recheck=refuse_recheck),
            state={}, gcode="job.gcode", job={}, job_path="JOB.json", reprint=False)
    assert events == ["preflight", "acquire", "recheck", "lease.release"]


def test_transport_launch_upload_failure_is_one_uncertain_step(monkeypatch):
    events = []
    _rig(monkeypatch, events)

    def broken_upload(ctx):
        events.append("upload")
        raise RuntimeError("connection lost")

    with pytest.raises(SystemExit, match="Fake submission failed or uncertain"):
        print_now._run_transport_launch(
            _spec(events, upload=broken_upload),
            state={}, gcode="job.gcode", job={}, job_path="JOB.json", reprint=False)
    # submitting happened, the lease went uncertain (never released, never
    # resubmitted), the start claim was freed for the listener, and the
    # start hook never ran.
    assert events[:5] == ["preflight", "acquire", "recheck", "bed_clear", "submitting"]
    assert "upload" in events and not any(e.startswith("start:") for e in events)
    assert any(e.startswith("lease.uncertain:Fake submission") for e in events)
    assert "release_claim:job.gcode" in events and "lease.release" not in events


def test_transport_launch_unconfirmed_start_records_and_monitors_nothing(monkeypatch):
    events = []
    _rig(monkeypatch, events, confirmed=False)
    out = print_now._run_transport_launch(
        _spec(events), state={}, gcode="job.gcode", job={}, job_path="JOB.json", reprint=False)
    assert out["started"] is False
    assert any(e.startswith("lease.uncertain:") for e in events)
    assert not any(e.startswith("monitor:") for e in events)


def test_transport_launch_adopts_the_upload_remote_only_when_the_spec_says_so(monkeypatch):
    events = []
    _rig(monkeypatch, events)
    print_now._run_transport_launch(
        _spec(events, upload=lambda ctx: events.append("upload") or "stored.gcode",
              adopt_upload_remote=True),
        state={}, gcode="job.gcode", job={}, job_path="JOB.json", reprint=False)
    assert "claim:stored.gcode" in events and "start:stored.gcode" in events

    events = []
    _rig(monkeypatch, events)
    print_now._run_transport_launch(
        _spec(events, upload=lambda ctx: events.append("upload") or "stored.gcode"),
        state={}, gcode="job.gcode", job={}, job_path="JOB.json", reprint=False)
    assert "claim:job.gcode" in events and "start:job.gcode" in events


def test_every_polled_adapter_delegates_to_the_shared_runner():
    for name in ("_print_octoprint", "_print_bambu", "_print_elegoo_sdcp",
                 "_print_prusa", "_print_flashforge_stock"):
        source = inspect.getsource(getattr(print_now, name))
        assert "TransportLaunch(" in source and "_run_transport_launch(" in source, name
        assert "lease.submitting" not in source, name  # only the runner submits now


# -- the Moonraker routes' shared leased-upload steps -------------------------

def test_bed_clear_phases_check_then_consume(monkeypatch):
    calls = []
    monkeypatch.setattr(print_now, "_claim_bed_clear",
                        lambda *a, **k: calls.append((a, k)))
    print_now._check_bed_clear("JOB.json", {}, "job.gcode", False)
    assert calls == [(("JOB.json", {}, "job.gcode", False), {"consume": False})]
    monkeypatch.setattr(print_now, "yolo_active", lambda: False)
    print_now._consume_bed_clear("JOB.json", {}, "job.gcode", False)
    assert calls[-1] == (("JOB.json", {}, "job.gcode", False), {"consume": True})
    monkeypatch.setattr(print_now, "yolo_active", lambda: True)
    print_now._consume_bed_clear("JOB.json", {}, "job.gcode", False)
    assert calls[-1] == (("JOB.json", {}, "job.gcode", False), {"consume": False})


def test_moonraker_upload_failure_uses_the_routes_own_exception(monkeypatch):
    monkeypatch.setattr(print_now, "_timing", lambda *a, **k: None)
    monkeypatch.setattr(print_now.subprocess, "run",
                        lambda *a, **k: S(returncode=1, stdout="", stderr="no route"))
    with pytest.raises(RuntimeError, match="The file could not be uploaded"):
        print_now._upload_via_moonrakerctl({}, "up.gcode", "job.gcode", "sha", "JOB.json",
                                           failure=RuntimeError)
    with pytest.raises(SystemExit, match="The file could not be uploaded"):
        print_now._upload_via_moonrakerctl({}, "up.gcode", "job.gcode", "sha", "JOB.json",
                                           failure=SystemExit)
    monkeypatch.setattr(print_now.subprocess, "run",
                        lambda *a, **k: S(returncode=0, stdout='{"result": {}}', stderr=""))
    assert print_now._upload_via_moonrakerctl({}, "up.gcode", "job.gcode", "sha", "JOB.json",
                                              failure=RuntimeError) == '{"result": {}}'


def test_abandon_leased_upload_releases_lease_claim_and_cleanup_in_order(monkeypatch):
    events = []
    monkeypatch.setattr(print_now, "release_start_announcement",
                        lambda remote: events.append(f"release_claim:{remote}"))
    print_now._abandon_leased_upload(_Lease(events), "job.gcode",
                                     on_abandon=lambda: events.append("cleanup"))
    assert events == ["lease.release", "release_claim:job.gcode", "cleanup"]
