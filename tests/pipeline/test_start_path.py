"""The launch/start path under failure: every transport, every step, nothing resubmitted.

Each transport (mapped native, generic Moonraker, OctoPrint, PrusaLink, SDCP, stock Flashforge, Bambu) is
driven through one injected failure at a time: preflight, upload, the start request (timeout, 4xx, 5xx)
and the readback (raises, still heating, printing). After each, the table asserts the lease, the uniform
`START READBACK` outcome line, how the intake classifies the run (prepared_intake.classify_start), the
message the person gets, and that no upload or start request was ever repeated. Nothing here contacts a
printer.
"""
import contextlib
import io
import json
import os
import sqlite3
import sys
import time
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from print_handy.core import launch_state, model_job, prepared_intake
from print_handy.hermes import print_next

from .test_launch_state import dead_pid, leases, make_job, uploads
from .test_launch_state import mapped, printer  # noqa: F401  (fixtures shared with the lease tests)

REAL_WAIT_PRINTING = model_job._wait_printing


# --------------------------------------------------------------------------- helpers

@pytest.fixture
def patch(request):
    """mock.patch.object for tests that use the `mapped` or `printer` fixture.

    Those fixtures patch model_job names with mocks. A monkeypatch (set up earlier, as an autouse
    fixture's dependency) would restore the mock *after* they restored the real function, leaking it into
    later tests; this one is set up after them, so it is undone first.
    """
    for name in ("mapped", "printer"):
        if name in request.fixturenames:
            request.getfixturevalue(name)
    with contextlib.ExitStack() as stack:
        yield lambda obj, attr, value: stack.enter_context(mock.patch.object(obj, attr, value))



class Run(tuple):
    """(exit ok, the last-6-lines tail the intake sees, the whole output)."""

    def __new__(cls, ok, tail, full):
        return super().__new__(cls, (ok, tail, full))


def run_cli(monkeypatch, capsys, fn):
    """Run one launch the way the print-now subprocess does."""
    monkeypatch.setattr(model_job, "print_now", lambda target, **kw: fn())
    ok = True
    try:
        model_job._print_now_cli("job.gcode")
    except SystemExit as exc:
        ok = exc.code in (0, None)
        if not ok:
            print(exc.code, file=sys.stderr)
    captured = capsys.readouterr()
    text = (captured.out + captured.err).strip()
    tail = "\n".join(text.splitlines()[-6:])[-1500:]
    return Run(ok, tail, text)


def intake_view(ok, tail):
    """(status, message) as prepared_intake would record and post it for this finished run."""
    verdict = prepared_intake.classify_start(ok, tail)
    if verdict.unknown:
        return verdict.status, prepared_intake.START_PENDING_MESSAGE
    if verdict.status == "STARTED":
        return verdict.status, None
    return verdict.status, prepared_intake.plain_start_failure(tail)


def phases():
    return [row["phase"] for row in leases()]


def http_error(code, message="rejected"):
    body = io.BytesIO(json.dumps({"error": {"code": code, "message": message}}).encode())
    return urllib.error.HTTPError("http://m/printer/print/start", code, message, {}, body)


# --------------------------------------------------------------------------- client-based transports

IDLE = {"state": "standby", "filename": ""}
BUSY = {"state": "printing", "filename": "someone-elses.gcode"}


class ScriptedClient:
    """One client for the five client-based transports. Counts every request; never opens a socket."""

    def __init__(self, post, pre=IDLE, fail=None, started_anyway=False):
        self.post, self.pre, self.fail = list(post), pre, fail or {}
        self.started_anyway = started_anyway
        self.uploads, self.starts, self.reads = [], [], 0
        self.remote, self.started = None, False

    def _read(self):
        self.reads += 1
        if not self.started:
            return self.pre
        reply = self.post.pop(0) if len(self.post) > 1 else self.post[0]
        reply = reply(self.remote) if callable(reply) else reply
        if isinstance(reply, Exception):
            raise reply
        return reply

    def status(self):
        return self._read()

    snapshot = detail = status

    def require_ready(self, status):
        if status["state"] != "standby":
            raise RuntimeError(f"printer is {status['state']}")

    def slots_of(self, detail):
        return {1: True}

    def upload(self, gcode, remote):
        self.uploads.append(remote)
        if "upload" in self.fail:
            raise self.fail["upload"]
        self.remote = remote
        return remote

    def _start(self, *args, **kwargs):
        self.starts.append(args)
        if "start" in self.fail:
            self.started = self.started_anyway
            raise self.fail["start"]
        self.started = True

    start = start_print = print_gcode = _start


class FakeAdapter:
    multi_slot = False
    label = "Fake"
    slot_count = 4

    def __init__(self, transport, client):
        self.transport, self.key, self._client = transport, f"{transport}.fake", client

    def client(self, **kwargs):
        return self._client

    def readiness_problems(self, state, raw, **kwargs):
        return []

    def normalize_status(self, raw):
        return raw

    def normalize_tool_mapping(self, mapping, state):
        return mapping

    def gcode_problems(self, text, state=None):
        return []

    def extruder_count(self):
        return 1


KINDS = {"octoprint": "_print_octoprint", "prusa": "_print_prusa", "sdcp": "_print_elegoo_sdcp",
         "stock": "_print_flashforge_stock", "bambu": "_print_bambu"}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    from print_handy.hermes import bambu_watch, elegoo_watch, octo_watch, prusa_watch, stock_watch
    from print_handy.printers.bambulab import ams_mapping

    gcode = tmp_path / "plate_1.gcode"
    gcode.write_text("G1 X1\n")
    package = tmp_path / "plate_1.gcode.3mf"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", "G1 X1\n")
    monkeypatch.setattr(model_job, "START_CLAIMS", tmp_path / "claims.json")
    monkeypatch.setattr(model_job, "verify_gcode", lambda path: {"offline": True})
    monkeypatch.setattr(model_job, "record_start", lambda g, remote, **kw: "/records/1")
    monkeypatch.setattr(model_job.printers, "current", lambda *a, **k: FakeAdapter("octoprint", None))
    def with_gcode_temperatures(state, text, recorded=None):  # model_job's real signature
        return {**state, "temps_c": {"bed": 60, "nozzle": 210}}
    monkeypatch.setattr(model_job, "_with_gcode_temperatures", with_gcode_temperatures)
    monkeypatch.setattr(model_job, "_bambu_start_confirm_window", lambda: 30.0)
    from print_handy.core import delivery
    monkeypatch.setattr(delivery, "bambu_handoff", lambda job, gcodes, config=None: [package])
    monkeypatch.setattr(ams_mapping, "resolve_for_print", lambda *a, **k: ([0], "job"))
    # Launched-print monitors run in the detached monitor worker
    # (P1-85/P1-98); the start path only has to ask for one.
    from print_handy.hermes import monitor_worker
    spawned = []

    def spawn(watch_module_name, filename, printer_id=None, *, popen=None):  # monitor_worker's real signature
        spawned.append((watch_module_name, filename, printer_id))
    monkeypatch.setattr(monitor_worker, "spawn", spawn)
    return mock.Mock(gcode=gcode, package=package, root=tmp_path, spawned=spawned)


def launch(kind, rig, client):
    adapter = FakeAdapter({"sdcp": "elegoo_sdcp", "prusa": "prusalink", "stock": "flashforge_stock",
                           "bambu": "bambulab_lan"}.get(kind, kind), client)
    state = {"nozzle": {"type": "hardened_steel"}, "bed_mm": [256, 256]}
    job = {"filament_selection": {"slot": 1}, "printer_model": "bambulab.a1"}
    return getattr(model_job, KINDS[kind])(adapter, state, rig.gcode, job, None, 25, False)


def printing(remote):
    return {"state": "printing", "filename": remote, "gcode_state": "RUNNING"}


def late_printing(after):
    """Idle for `after` readbacks, then printing: what a printer that takes a moment looks like."""
    count = {"n": 0}

    def reply(remote):
        count["n"] += 1
        return printing(remote) if count["n"] > after else {"state": "standby", "filename": ""}
    return reply


# case id -> (scripted client kwargs, exit ok, lease phases, outcome phase, intake status)
CLIENT_CASES = {
    "preflight_busy": (dict(post=[IDLE], pre=BUSY), False, [], None, "START_REFUSED"),
    "upload_fails": (dict(post=[IDLE], fail={"upload": RuntimeError("HTTP 503 uploading")}),
                     False, ["uncertain"], "awaiting_confirmation", "START_PENDING"),
    "start_timeout": (dict(post=[IDLE], fail={"start": TimeoutError("timed out")}),
                      False, ["uncertain"], "awaiting_confirmation", "START_PENDING"),
    # These clients fold every HTTP answer into one RuntimeError, so a 4xx cannot be told from a 5xx
    # there: both stay uncertain (only the Moonraker paths see a typed HTTP status).
    "start_4xx": (dict(post=[IDLE], fail={"start": RuntimeError("HTTP 400 bad request")}),
                  False, ["uncertain"], "awaiting_confirmation", "START_PENDING"),
    "start_5xx": (dict(post=[IDLE], fail={"start": RuntimeError("HTTP 503 unavailable")}),
                  False, ["uncertain"], "awaiting_confirmation", "START_PENDING"),
    "readback_raises": (dict(post=[RuntimeError("HTTP 503 status")]),
                        True, ["uncertain"], "awaiting_confirmation", "START_PENDING"),
    "readback_idle_forever": (dict(post=[IDLE]), True, ["uncertain"], "awaiting_confirmation", "START_PENDING"),
    "readback_heating_no_file": (dict(post=[{"state": "heating", "filename": ""}]),
                                 True, ["uncertain"], "awaiting_confirmation", "START_PENDING"),
    "readback_heating_with_file": (dict(post=[lambda r: {"state": "heating", "filename": r}]),
                                   True, [], "preparing", "STARTED"),
    "readback_printing_late": (dict(post=[late_printing(2)]), True, [], "executing", "STARTED"),
    "readback_printing": (dict(post=[printing]), True, [], "executing", "STARTED"),
}


@pytest.mark.parametrize("kind", sorted(KINDS))
@pytest.mark.parametrize("case", sorted(CLIENT_CASES))
def test_client_transport_failure_table(kind, case, rig, monkeypatch, capsys):
    kwargs, want_ok, want_leases, want_phase, want_status = CLIENT_CASES[case]
    kwargs = dict(kwargs)
    if kind == "bambu" and case == "readback_heating_with_file":
        # Bambu has no heating state: PREPARE is "printing" and the printer names the bare project.
        kwargs["post"] = [lambda r: {"state": "printing", "gcode_state": "PREPARE", "filename": r.split(".")[0]}]
        want_phase = "executing"
    if kind == "stock" and case == "readback_heating_with_file":
        kwargs["post"] = [lambda r: {"state": "printing", "filename": r}]  # the stock client folds heating in
        want_phase = "executing"
    client = ScriptedClient(**kwargs)
    ok, tail, full = run_cli(monkeypatch, capsys, lambda: launch(kind, rig, client))

    assert ok is want_ok, tail
    assert phases() == want_leases, tail
    outcome = print_next.start_outcome(full)
    assert outcome.get("phase") == want_phase, full
    status, message = intake_view(ok, tail)
    assert status == want_status, tail
    if status == "START_PENDING":
        assert message == prepared_intake.START_PENDING_MESSAGE and "Do not send it again" in message
    if status == "START_REFUSED":
        assert message.startswith("Didn't start")
    if status == "STARTED":
        assert outcome["started"] is True
    # Nothing is ever resubmitted: at most one upload and one start request, whatever the readback said.
    assert len(client.uploads) <= 1 and len(client.starts) <= 1
    if case == "preflight_busy":
        assert client.uploads == [] and client.starts == []
    if case == "readback_idle_forever":
        assert client.reads > 4  # the window was polled, not one immediate readback
    if want_leases == ["uncertain"] and client.uploads:
        assert model_job._read_claims() == {}  # unannounced: the listener can still post the start


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_item1_a_start_confirmed_after_a_few_polls_is_not_called_unconfirmed(kind, rig):
    """The four single-readback transports used to read once, right after the start request."""
    client = ScriptedClient(post=[late_printing(2)])
    out = launch(kind, rig, client)
    assert out["started"] is True and out["phase"] == "executing" and phases() == []
    assert client.reads >= 5 and len(client.starts) == 1  # two preflight reads, then the polls
    # The start asked for exactly one monitor, from this family's watch module (2.2.5 review).
    watch = {"octoprint": "octo_watch", "prusa": "prusa_watch", "sdcp": "elegoo_watch",
             "stock": "stock_watch", "bambu": "bambu_watch"}[kind]
    assert [(name, printer) for name, _remote, printer in rig.spawned] == [(f"print_handy.hermes.{watch}", None)]


def test_start_family_monitor_passes_the_jobs_printer_id_to_the_worker(rig):
    from print_handy.hermes import octo_watch
    model_job._start_family_monitor(octo_watch, None, "part.gcode", {"printer_id": "voron"})
    assert rig.spawned == [("print_handy.hermes.octo_watch", "part.gcode", "voron")]


@pytest.mark.parametrize("kind", ["octoprint", "prusa", "sdcp", "stock"])
def test_item1_transitional_states_keep_the_window_open_and_name_the_state(kind, rig):
    client = ScriptedClient(post=[{"state": "calibrate_doing", "filename": ""}])
    out = launch(kind, rig, client)
    assert out["started"] is False and phases() == ["uncertain"]
    [row] = leases()
    assert "calibrate_doing" in row["detail"] and "snapshot(s) over 45s" in row["detail"]
    assert client.reads > 10


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_item2_every_transport_prints_the_uniform_outcome_line(kind, rig, capsys):
    client = ScriptedClient(post=[IDLE])
    out = launch(kind, rig, client)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith(model_job.START_OUTCOME_MARK)]
    assert len(lines) == 1
    parsed = json.loads(lines[0][len(model_job.START_OUTCOME_MARK):])
    assert parsed["phase"] == "awaiting_confirmation" == out["phase"] and parsed["file"] == out["file"]


def test_item2_the_marker_is_shared_between_the_writer_and_the_parsers():
    assert model_job.START_OUTCOME_MARK == print_next.OUTCOME_MARK


def test_item2_intake_never_posts_started_for_an_unresolved_exit_zero():
    for phase in ("awaiting_confirmation", "preparing_or_unresolved"):
        for mark in ("START READBACK ", "MAPPED START READBACK "):
            tail = f'PRINT x\n{mark}{json.dumps({"phase": phase, "started": False})}'
            verdict = prepared_intake.classify_start(True, tail)
            assert verdict.status == "START_PENDING" and verdict.started is None and verdict.outcome == "unknown"
    for phase in ("executing", "preparing"):
        verdict = prepared_intake.classify_start(True, f'START READBACK {json.dumps({"phase": phase})}')
        assert verdict.status == "STARTED" and verdict.outcome == phase
    # An exit 0 with no outcome line at all (an older print-now) keeps reading as started.
    assert prepared_intake.classify_start(True, "PRINT STARTED {}").status == "STARTED"
    assert print_next.start_outcome("noise\nSTART READBACK not json") == {}


def test_item2_a_pending_start_keeps_its_file_for_the_listener_edit():
    out = {"phase": "awaiting_confirmation", "started": False, "file": "a/b_1.gcode"}
    tail = "START SUBMITTED; AWAITING CONFIRMATION {}\nSTART READBACK " + json.dumps(out)
    assert print_next.still_preparing(tail) and print_next.started_file(tail) == "b_1.gcode"


# --------------------------------------------------------------------------- generic Moonraker

QUIET = {"result": {"status": {"print_stats": {"state": "standby", "filename": ""},
                               "extruder": {"target": 0}, "heater_bed": {"target": 0},
                               "virtual_sdcard": {"is_active": False}, "webhooks": {"state": "ready"}}}}


def moonraker_status(*, filename="", state="standby", target=0):
    return {"result": {"status": {"print_stats": {"state": state, "filename": filename, "filament_used": 3},
                                  "extruder": {"target": target}, "heater_bed": {"target": target},
                                  "virtual_sdcard": {"is_active": state == "printing"},
                                  "webhooks": {"state": "ready"}}}}


class MoonrakerScript:
    """Steers the generic path: what the start POST does, and what the printer says afterwards."""

    def __init__(self, printer, start=None, after=None, phase_raises=None):
        self.printer, self.start, self.after = printer, start, after or (lambda remote: QUIET)
        self.started, self.posts = False, 0
        printer.mr.side_effect = self.mr
        self.wait_raises = phase_raises  # what reading the printer back raises after the start

    def remote(self):
        found = uploads(self.printer.run)
        return found[-1] if found else None

    def mr(self, st, path, data=None, timeout=30):
        if path == "/printer/print/start":
            self.posts += 1
            if self.start is not None:
                self.start()
            return {"result": "ok"}
        if path.startswith("/printer/objects/query"):
            if self.posts and self.after is not None:
                reply = self.after(self.remote())
                if isinstance(reply, Exception):
                    raise reply
                return reply
            return QUIET
        if path.startswith("/server/history"):
            return {"result": {"jobs": []}}
        return {"result": "ok"}


def raises(exc):
    def start():
        raise exc
    return start


# case -> (script kwargs builder, exit ok, lease phases, outcome phase, status)
GENERIC_CASES = {
    "start_timeout_nothing_visible": (lambda: dict(start=raises(TimeoutError("timed out"))),
                                      True, ["uncertain"], "preparing_or_unresolved", "START_PENDING"),
    "start_timeout_file_printing": (lambda: dict(start=raises(TimeoutError("timed out")),
                                                 after=lambda r: moonraker_status(filename=r, state="printing")),
                                    True, ["uncertain"], "executing", "STARTED"),
    "start_5xx": (lambda: dict(start=raises(http_error(503, "Service Unavailable"))),
                  True, ["uncertain"], "preparing_or_unresolved", "START_PENDING"),
    "start_4xx_must_home": (lambda: dict(start=raises(http_error(400, "Must home axis first"))),
                            False, [], None, "START_REFUSED"),
    "start_4xx_not_found": (lambda: dict(start=raises(http_error(404, "File not found"))),
                            False, [], None, "START_REFUSED"),
    "start_4xx_but_printer_busy": (lambda: dict(start=raises(http_error(400, "odd")),
                                                after=lambda r: moonraker_status(filename=r, state="printing")),
                                   True, ["uncertain"], "executing", "STARTED"),
    "readback_phase_raises": (lambda: dict(phase_raises=http_error(503, "Service Unavailable")),
                              True, ["uncertain"], "awaiting_confirmation", "START_PENDING"),
    # Heaters on and nothing naming this file: unrelated work looks the same, so the accepted start stays uncertain.
    "readback_heating_without_this_file": (lambda: dict(after=lambda r: moonraker_status(target=215)),
                                           True, ["uncertain"], "awaiting_confirmation", "START_PENDING"),
    "readback_stale_previous_file": (lambda: dict(after=lambda r: moonraker_status(filename="previous-job.gcode")),
                                     True, ["uncertain"], "awaiting_confirmation", "START_PENDING"),
    "readback_this_file_preparing": (lambda: dict(after=lambda r: moonraker_status(filename=r, target=215)),
                                     True, [], "preparing", "STARTED"),
    "start_4xx_with_heaters_on_and_no_file": (lambda: dict(start=raises(http_error(400, "macro failed")),
                                                           after=lambda r: moonraker_status(target=215)),
                                              True, ["uncertain"], "preparing_or_unresolved", "START_PENDING"),
    "readback_printing": (lambda: dict(after=lambda r: moonraker_status(filename=r, state="printing")),
                          True, [], "executing", "STARTED"),
}


@pytest.mark.parametrize("case", sorted(GENERIC_CASES))
def test_generic_moonraker_failure_table(case, printer, monkeypatch, capsys, patch):
    build, want_ok, want_leases, want_phase, want_status = GENERIC_CASES[case]
    script = MoonrakerScript(printer, **build())
    patch(model_job, "_wait_printing",
          mock.Mock(side_effect=script.wait_raises) if script.wait_raises else (lambda *a, **k: "standby"))
    jp, _ = make_job(printer.root, "a")
    ok, tail, full = run_cli(monkeypatch, capsys, lambda: REAL_PRINT_NOW(jp))
    assert ok is want_ok, tail
    assert phases() == want_leases, tail
    assert print_next.start_outcome(full).get("phase") == want_phase, full
    status, message = intake_view(ok, tail)
    assert status == want_status, tail
    assert len(uploads(printer.run)) == 1 and script.posts == 1  # nothing resubmitted
    if status == "START_REFUSED":
        assert "rejected the start request" in message and "Must home" in message or "File not found" in message
    if want_leases == ["uncertain"] and want_phase in ("awaiting_confirmation", "preparing_or_unresolved"):
        assert model_job._read_claims() == {}


REAL_PRINT_NOW = model_job.print_now


def test_item5_generic_readback_error_after_the_start_is_a_clean_uncertain_line(printer, monkeypatch, capsys, patch):
    MoonrakerScript(printer)
    patch(model_job, "_wait_printing", mock.Mock(side_effect=http_error(503, "Service Unavailable")))
    jp, _ = make_job(printer.root, "a")
    out = model_job.print_now(jp, wait_s=0)
    text = capsys.readouterr().out
    assert out["phase"] == "awaiting_confirmation" and "UNCERTAIN: the start was sent" in text
    assert phases() == ["uncertain"] and model_job._read_claims() == {}


def test_item5_a_failing_print_number_or_record_does_not_hide_a_confirmed_start(printer, monkeypatch, patch):
    MoonrakerScript(printer, after=lambda r: moonraker_status(filename=r, state="printing"))
    patch(model_job, "_assign_print_number", mock.Mock(side_effect=OSError("disk full")))
    patch(model_job, "record_start", mock.Mock(side_effect=OSError("disk full")))
    jp, _ = make_job(printer.root, "a")
    patch(model_job, "_wait_printing", lambda *a, **k: "printing")
    out = model_job.print_now(jp)
    assert out["started"] is True and out["print_number"] is None and phases() == []


def test_item6_start_rejection_is_only_a_typed_4xx():
    assert model_job._start_rejection(http_error(400, "Must home axis first")) == "HTTP 400: Must home axis first"
    assert model_job._start_rejection(http_error(404, "File not found")).startswith("HTTP 404")
    for exc in (http_error(500), http_error(503), http_error(408), http_error(429), TimeoutError("t"),
                ConnectionResetError("reset"), urllib.error.URLError("refused"), RuntimeError("HTTP 400")):
        assert model_job._start_rejection(exc) is None


def test_item6_a_rejected_start_leaves_the_printer_free_for_the_next_job(printer, monkeypatch, capsys, patch):
    MoonrakerScript(printer, start=raises(http_error(400, "Must home axis first")))
    patch(model_job, "_wait_printing", lambda *a, **k: "standby")
    jp, _ = make_job(printer.root, "a")
    with pytest.raises(SystemExit, match=r"REFUSED: the printer rejected the start request \(HTTP 400: Must home"):
        model_job.print_now(jp)
    assert leases() == [] and model_job._read_claims() == {}
    # The printer is not wedged: a second job takes the lease and starts.
    printer.mr.side_effect = MoonrakerScript(printer, after=lambda r: moonraker_status(filename=r, state="printing")).mr
    b, _ = make_job(printer.root, "b")
    assert model_job.print_now(b)["started"] is True


def test_item8_generic_lost_post_is_reconciled_like_the_mapped_path(printer, monkeypatch, patch):
    """A 30 s POST can time out after Moonraker accepted it. It used to raise with no reconcile."""
    script = MoonrakerScript(printer, start=raises(TimeoutError("timed out")),
                             after=lambda r: moonraker_status(filename="3mf_print/" + r, state="printing"))
    jp, _ = make_job(printer.root, "a")
    patch(model_job, "_wait_printing", lambda *a, **k: "standby")
    out = model_job.print_now(jp)
    assert out["phase"] == "executing" and out["accepted"] is True and script.posts == 1
    assert phases() == ["uncertain"]  # the duplicate guard holds until the printer reports the file
    # Heaters alone count only from a proven-off baseline.
    for proven_off, want in ((True, "preparing"), (False, "preparing_or_unresolved")):
        with mock.patch.object(model_job, "_preparation_evidence", return_value=True):  # heaters on now
            MoonrakerScript(printer)
            verdict, _ = model_job._settle_lost_start(mock.Mock(), {}, "x.gcode", TimeoutError(),
                                                      base={"file": "x.gcode"}, heaters_were_off=proven_off)
            assert verdict["phase"] == want


def leftover_complete(remote):
    """What a printer shows after the previous run of the same artifact: remote names come from the
    file hash, so a reprint's name is the one already there, state complete, axes still homed."""
    return {"result": {"status": {"print_stats": {"state": "complete", "filename": remote, "total_duration": 612.0},
                                  "toolhead": {"homed_axes": "xyz"}, "extruder": {"target": 0},
                                  "heater_bed": {"target": 0}, "virtual_sdcard": {"is_active": False},
                                  "idle_timeout": {"state": "Idle"}, "webhooks": {"state": "ready"}}}}


class ReprintScript(MoonrakerScript):
    """The printer shows the previous run's leftover from the upload on, until the start changes it."""

    def mr(self, st, path, data=None, timeout=30):
        if path.startswith("/printer/objects/query") and not self.posts and self.remote():
            return leftover_complete(self.remote())
        return super().mr(st, path, data=data, timeout=timeout)


@pytest.mark.parametrize("start, want_phase", [(raises(TimeoutError("timed out")), "preparing_or_unresolved"),
                                               (None, "awaiting_confirmation")])
def test_a_reprints_leftover_complete_state_is_never_reported_started(start, want_phase, printer, monkeypatch,
                                                                       capsys, patch):
    """A lost (or answered) start on a reprint used to read the previous run's state=complete with the
    same file name (and homed axes) as "preparing": STARTED, lease released, though nothing ran."""
    script = ReprintScript(printer, start=start, after=leftover_complete)
    patch(model_job, "_wait_printing", lambda *a, **k: "complete")
    jp, _ = make_job(printer.root, "a")
    ok, tail, full = run_cli(monkeypatch, capsys, lambda: REAL_PRINT_NOW(jp))
    assert ok, tail
    outcome = print_next.start_outcome(full)
    assert outcome.get("phase") == want_phase and outcome.get("started") is False, full
    assert intake_view(ok, tail)[0] == "START_PENDING", tail
    assert phases() == ["uncertain"] and script.posts == 1  # the duplicate guard holds; nothing resubmitted


def test_a_reprint_that_heats_from_a_cold_baseline_is_still_preparing(printer, monkeypatch, capsys, patch):
    """The fix compares with the baseline; it does not ignore this start's real heating."""
    def heating(remote):
        status = leftover_complete(remote)
        status["result"]["status"]["extruder"] = {"target": 215}
        return status
    ReprintScript(printer, start=raises(TimeoutError("timed out")), after=heating)
    patch(model_job, "_wait_printing", lambda *a, **k: "complete")
    jp, _ = make_job(printer.root, "a")
    ok, tail, full = run_cli(monkeypatch, capsys, lambda: REAL_PRINT_NOW(jp))
    assert print_next.start_outcome(full).get("phase") == "preparing", full


def test_wait_printing_does_not_end_on_the_previous_jobs_cancelled_state(monkeypatch):
    reads = iter([("cancelled", "x.gcode", "Idle")] * 3 + [("printing", "x.gcode", "Printing")])
    monkeypatch.setattr(model_job, "_printer_phase", lambda st: next(reads))
    monkeypatch.setattr(model_job.time, "sleep", lambda s: None)
    baseline = {"print_stats": {"state": "cancelled", "filename": "x.gcode"}}
    assert REAL_WAIT_PRINTING({}, "x.gcode", 30, baseline=baseline) == "printing"
    reads = iter([("cancelled", "x.gcode", "Idle")])
    assert REAL_WAIT_PRINTING({}, "x.gcode", 30) == "cancelled"  # no baseline: unchanged behavior


def test_item8_both_paths_share_one_reconcile():
    import inspect
    # print_now is a thin wrapper since the SAFE-6 control lock (P1-33); its body
    # is _print_now_locked. Both start paths must call the one reconcile helper.
    source = (inspect.getsource(model_job.print_now) + inspect.getsource(model_job._print_now_locked)
              + inspect.getsource(model_job.print_mapped_native))
    assert source.count("_settle_lost_start(") == 2


# --------------------------------------------------------------------------- mapped native

MAPPED_CASES = {
    "start_timeout_nothing_visible": (lambda m: dict(post=raises(TimeoutError("timed out"))),
                                      True, ["uncertain"], "preparing_or_unresolved", "START_PENDING"),
    "start_5xx": (lambda m: dict(post=raises(http_error(502, "Bad Gateway"))),
                  True, ["uncertain"], "preparing_or_unresolved", "START_PENDING"),
    "start_4xx_must_home": (lambda m: dict(post=raises(http_error(400, "Must home axis first"))),
                            False, [], None, "START_REFUSED"),
    "start_4xx_with_heaters_on_and_no_file": (lambda m: dict(post=raises(http_error(400, "macro failed")), heating=True),
                                              True, ["uncertain"], "preparing_or_unresolved", "START_PENDING"),
    "start_404": (lambda m: dict(post=raises(http_error(404, "File not found"))),
                  False, [], None, "START_REFUSED"),
    "readback_wait_raises": (lambda m: dict(wait=http_error(503, "Service Unavailable")),
                             True, ["uncertain"], "preparing_or_unresolved", "START_PENDING"),
    # #836: preparation evidence is provisional — the lease stays uncertain until the printer
    # reports this file printing; it is no longer released on heater targets alone.
    "readback_still_heating": (lambda m: dict(heating=True), True, ["uncertain"], "preparing", "STARTED"),
    "readback_idle": (lambda m: dict(), True, ["uncertain"], "preparing_or_unresolved", "START_PENDING"),
    "readback_printing": (lambda m: dict(printing=True), True, [], "executing", "STARTED"),
}


@pytest.mark.parametrize("case", sorted(MAPPED_CASES))
def test_mapped_native_failure_table(case, mapped, monkeypatch, capsys, patch):
    options = MAPPED_CASES[case][0](mapped)
    original = model_job._mr.side_effect
    posts = []

    def mr(st, path, data=None, timeout=30):
        if path == "/printer/gcode/script":
            posts.append(data["script"])
            if options.get("post"):
                options["post"]()
            return {"result": "ok"}
        if "print_stats&extruder" in path and posts and options.get("printing"):
            remote = uploads(mapped.run)[-1]
            return {"result": {"status": {"print_stats": {"state": "printing", "filename": remote},
                                          "virtual_sdcard": {"is_active": True}, "webhooks": {"state": "ready"}}}}
        return original(st, path, data=data, timeout=timeout)

    model_job._mr.side_effect = mr
    if options.get("wait"):
        patch(model_job, "_wait_printing", mock.Mock(side_effect=options["wait"]))
    if options.get("printing"):
        patch(model_job, "_wait_printing", lambda *a, **k: "printing")
    # #836: heater evidence is read immediately before the POST too, and a timeless mock would
    # answer that read with the post-POST state. Evidence counts only once the POST has gone out.
    model_job._preparation_evidence.side_effect = lambda st: bool(posts) and bool(options.get("heating"))
    jp, g = make_job(mapped.root, "a")
    job = json.loads(jp.read_text())
    ok, tail, full = run_cli(monkeypatch, capsys, lambda: model_job.print_mapped_native(
        g, mapped.st, {"display_off": 1}, {"0": 1}, job, jp, 0, False))
    assert ok is want_ok(case), tail
    _, _, want_leases, want_phase, want_status = MAPPED_CASES[case]
    assert phases() == want_leases, tail
    assert print_next.start_outcome(full).get("phase") == want_phase, full
    status, message = intake_view(ok, tail)
    assert status == want_status, tail
    assert len(uploads(mapped.run)) == 1 and len(posts) == 1  # one upload, one SET_ZCOLOR, ever
    if "MAPPED START READBACK " in full:  # print_next keeps the last 1500 characters: the line must stay short
        line = [ln for ln in full.splitlines() if "MAPPED START READBACK " in ln][-1]
        assert len(line) < 700, len(line)
    checkpoint = json.loads(g.with_suffix(".launch.json").read_text())
    assert checkpoint["lease_token"]
    if status == "START_REFUSED":
        assert "rejected the start request" in message and checkpoint["phase"] == "rejected"
    if want_leases == ["uncertain"] and want_phase == "preparing_or_unresolved":
        assert model_job._read_claims() == {}
    if want_phase == "preparing":
        # #836: a provisional preparing start was announced by this path, so its claim stays
        # held for the listener's warmup/abandoned edits; only the lease stays uncertain.
        assert model_job._read_claims() != {}


def want_ok(case):
    return MAPPED_CASES[case][1]


def test_item5_mapped_record_and_number_failures_do_not_mask_the_start(mapped, monkeypatch, patch):
    patch(model_job, "_assign_print_number", mock.Mock(side_effect=OSError("disk full")))
    patch(model_job, "record_start", mock.Mock(side_effect=OSError("disk full")))
    patch(model_job, "_wait_printing", lambda *a, **k: "printing")
    jp, g = make_job(mapped.root, "a")
    out = model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1}, json.loads(jp.read_text()), jp, 0, False)
    assert out["started"] is True and out["print_number"] is None and phases() == []


def test_item5_mapped_readback_error_is_uncertain_not_a_traceback(mapped, monkeypatch, capsys, patch):
    patch(model_job, "_wait_printing", mock.Mock(side_effect=http_error(503, "Service Unavailable")))
    jp, g = make_job(mapped.root, "a")
    out = model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1}, json.loads(jp.read_text()), jp, 0, False)
    text = capsys.readouterr().out
    assert out["phase"] == "preparing_or_unresolved" and "UNCERTAIN: the start was sent" in text
    assert "MAPPED START READBACK" in text and phases() == ["uncertain"] and model_job._read_claims() == {}


# --------------------------------------------------------------------------- #836: start truthfulness

def test_836_a_preheat_during_the_upload_refuses_before_sending(mapped, monkeypatch, patch):
    """The idle guard proved the heaters off before the upload; a preheat landing during the
    upload (HelixScreen) is caught by the re-read immediately before the POST, and the start
    is refused unsent instead of being credited with someone else's heater targets."""
    heated = {"on": False}
    real_run = mapped.run.side_effect

    def upload_then_preheat(cmd, *args, **kwargs):
        result = real_run(cmd, *args, **kwargs)
        if len(cmd) >= 4 and cmd[1] == "upload":
            heated["on"] = True
        return result
    mapped.run.side_effect = upload_then_preheat
    model_job._preparation_evidence.side_effect = lambda st: heated["on"]
    jp, g = make_job(mapped.root, "a")
    with pytest.raises(SystemExit, match="heaters are already on"):
        model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1},
                                      json.loads(jp.read_text()), jp, 0, False)
    assert mapped.scripts == [] and len(uploads(mapped.run)) == 1  # uploaded, but never sent
    assert phases() == [] and model_job._read_claims() == {}
    assert json.loads(g.with_suffix(".launch.json").read_text())["phase"] == "not_submitted"
    # The refusal leaves no side effect of a start behind: the active spool, the display
    # message and the Home Assistant job card all come after the heater re-read.
    model_job._sync_active_spool.assert_not_called()
    model_job._announce_print_name.assert_not_called()
    model_job._publish_job_card.assert_not_called()


def test_a_sent_mapped_start_still_sets_the_spool_display_and_job_card_before_the_post(mapped, patch):
    order = []
    original = model_job._mr.side_effect

    def mr(st, path, data=None, timeout=30):
        if path == "/printer/gcode/script":
            order.append("post")
        return original(st, path, data=data, timeout=timeout)
    model_job._mr.side_effect = mr
    model_job._preparation_evidence.side_effect = lambda st: order.append("reread") or False
    model_job._sync_active_spool.side_effect = lambda *a, **k: order.append("spool") or {}
    model_job._announce_print_name.side_effect = lambda *a, **k: order.append("display")
    model_job._publish_job_card.side_effect = lambda *a, **k: order.append("card")
    jp, g = make_job(mapped.root, "a")
    model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1}, json.loads(jp.read_text()), jp, 0, False)
    assert order[:5] == ["reread", "spool", "display", "card", "post"], order


def test_836_a_lost_post_is_not_credited_when_the_pre_post_baseline_was_unproven(mapped, patch):
    """The re-read immediately before the POST could not prove the heaters were off (None), so
    heater targets after a lost POST response are not this job's evidence: unresolved, not
    preparing. (Before #836 the mapped path passed heaters_were_off=True unconditionally.)"""
    original = model_job._mr.side_effect
    posts = []

    def mr(st, path, data=None, timeout=30):
        if path == "/printer/gcode/script":
            posts.append(data["script"])
            raise TimeoutError("timed out")
        return original(st, path, data=data, timeout=timeout)
    model_job._mr.side_effect = mr
    # Unreadable before the POST (no baseline proven), on after it. The old code never read the
    # pre-POST baseline and credited the "on" read; the fix passes the real baseline through.
    model_job._preparation_evidence.side_effect = lambda st: True if posts else None
    jp, g = make_job(mapped.root, "a")
    out = model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1},
                                        json.loads(jp.read_text()), jp, 0, False)
    assert out["phase"] == "preparing_or_unresolved" and out["accepted"] is False
    assert phases() == ["uncertain"] and len(posts) == 1


def test_836_a_bang_bang_gcode_store_reply_is_a_refusal(mapped, patch):
    """Klipper answers a failed SET_ZCOLOR with HTTP 200 and a `!!` line in the gcode store.
    The receipt is inspected: START_REFUSED naming the `!!` text, checkpoint rejected, lease
    released, exactly one POST."""
    original = model_job._mr.side_effect
    posts = []

    def mr(st, path, data=None, timeout=30):
        if path == "/printer/gcode/script":
            posts.append(data["script"])
            return {"result": "ok"}
        if path.startswith("/server/gcode_store"):
            # The store only gains the `!!` line once the POST has gone out: the watermark is
            # captured from the store immediately before the POST, so a stateless store that
            # always showed the line would (correctly) read as a pre-submission error.
            entries = [{"message": '!! Unknown command:"SET_ZCOLOR"', "time": time.time()}] if posts else []
            return {"result": {"gcode_store": entries}}
        return original(st, path, data=data, timeout=timeout)
    model_job._mr.side_effect = mr
    jp, g = make_job(mapped.root, "a")
    with pytest.raises(SystemExit, match=r"REFUSED: the printer rejected the start request \(!! Unknown command"):
        model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1},
                                      json.loads(jp.read_text()), jp, 0, False)
    assert len(posts) == 1 and phases() == [] and model_job._read_claims() == {}
    assert json.loads(g.with_suffix(".launch.json").read_text())["phase"] == "rejected"


def test_836_a_bang_bang_line_in_the_receipt_itself_is_a_refusal(mapped, patch):
    """The `!!` line can also ride in the POST receipt itself; no gcode-store read is needed."""
    original = model_job._mr.side_effect
    posts = []

    def mr(st, path, data=None, timeout=30):
        if path == "/printer/gcode/script":
            posts.append(data["script"])
            return {"result": '!! Unknown command:"SET_ZCOLOR"'}
        return original(st, path, data=data, timeout=timeout)
    model_job._mr.side_effect = mr
    jp, g = make_job(mapped.root, "a")
    with pytest.raises(SystemExit, match=r"REFUSED: the printer rejected the start request \(!! Unknown command"):
        model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1},
                                      json.loads(jp.read_text()), jp, 0, False)
    assert len(posts) == 1 and phases() == []
    assert json.loads(g.with_suffix(".launch.json").read_text())["phase"] == "rejected"


def _store_reply(entries):
    return {"result": {"gcode_store": list(entries)}}


def test_848_start_receipt_error_attributes_by_watermark_not_by_clock():
    """Codex P1 on #848: a `!!` line logged just before the POST (inside the old 5 s window)
    belongs to an earlier problem and must not refuse this start, and a `!!` line logged after
    the POST must refuse it even when its timestamp is old (printer/host clock skew). Only the
    gcode-store watermark captured before the POST separates the two — never a timestamp."""
    now = time.time()
    before = {"message": "!! Must home axis first", "time": now - 1}  # 1 s before the send
    after_skewed = {"message": '!! Unknown command:"SET_ZCOLOR"', "time": now - 3600}  # skewed clock
    with mock.patch.object(model_job, "_mr", side_effect=[
            _store_reply([before]), _store_reply([before])]):
        watermark = model_job._gcode_store_watermark({})
        assert model_job._start_receipt_error({}, {"result": "ok"}, watermark=watermark) is None
    with mock.patch.object(model_job, "_mr", side_effect=[
            _store_reply([before]), _store_reply([before, {"message": "echo: all good", "time": now},
                                                  after_skewed])]):
        watermark = model_job._gcode_store_watermark({})
        assert model_job._start_receipt_error({}, {"result": "ok"}, watermark=watermark) \
            == '!! Unknown command:"SET_ZCOLOR"'
    # No watermark (the store was unreadable before the POST): store lines are not attributed
    # to this submission at all — no evidence, never a refusal.
    with mock.patch.object(model_job, "_mr", side_effect=OSError("offline")):
        assert model_job._gcode_store_watermark({}) is None
    with mock.patch.object(model_job, "_mr", return_value=_store_reply([after_skewed])):
        assert model_job._start_receipt_error({}, {"result": "ok"}, watermark=None) is None
    # An unreadable readback store is likewise no evidence.
    with mock.patch.object(model_job, "_mr", side_effect=[_store_reply([before]), OSError("offline")]):
        watermark = model_job._gcode_store_watermark({})
        assert model_job._start_receipt_error({}, {"result": "ok"}, watermark=watermark) is None
    # A `!!` line in the receipt itself still refuses, watermark or not.
    with mock.patch.object(model_job, "_mr", side_effect=OSError("offline")):
        assert model_job._start_receipt_error({}, {"result": '!! Unknown command:"SET_ZCOLOR"'},
                                              watermark=None) == '!! Unknown command:"SET_ZCOLOR"'


def test_848_a_bang_line_logged_just_before_the_post_is_not_this_starts_refusal(mapped, patch):
    """End to end on the mapped path: an unrelated console error 1 s before the SET_ZCOLOR POST
    (the old code's 5 s window blamed it) does not refuse a start the printer goes on to print."""
    store = [{"message": "!! Must home axis first", "time": time.time()}]  # already in the store pre-POST
    original = model_job._mr.side_effect

    def mr(st, path, data=None, timeout=30):
        if path.startswith("/server/gcode_store"):
            return _store_reply(store)
        return original(st, path, data=data, timeout=timeout)
    model_job._mr.side_effect = mr
    patch(model_job, "_wait_printing", lambda *a, **k: "printing")
    jp, g = make_job(mapped.root, "a")
    out = model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1},
                                        json.loads(jp.read_text()), jp, 0, False)
    assert out["started"] is True and out["phase"] == "executing"
    assert len(mapped.scripts) == 1


def test_848_a_bang_line_after_the_post_refuses_even_with_a_skewed_old_timestamp(mapped, patch):
    """The `!!` answer to the POST carries a timestamp an hour before the send (clock skew):
    the old time filter skipped it and waited out a pending start; the watermark counts it,
    because it entered the store after the watermark was captured."""
    store, posts = [], []
    original = model_job._mr.side_effect

    def mr(st, path, data=None, timeout=30):
        if path == "/printer/gcode/script":
            posts.append(data["script"])
            store.append({"message": '!! Unknown command:"SET_ZCOLOR"', "time": time.time() - 3600})
            return {"result": "ok"}
        if path.startswith("/server/gcode_store"):
            return _store_reply(store)
        return original(st, path, data=data, timeout=timeout)
    model_job._mr.side_effect = mr
    jp, g = make_job(mapped.root, "a")
    with pytest.raises(SystemExit, match=r"REFUSED: the printer rejected the start request \(!! Unknown command"):
        model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1},
                                      json.loads(jp.read_text()), jp, 0, False)
    assert len(posts) == 1 and phases() == [] and model_job._read_claims() == {}
    assert json.loads(g.with_suffix(".launch.json").read_text())["phase"] == "rejected"


# --------------------------------------------------------------------------- item 3: the checkpoint

def launch_twice(mapped, patch, second_reprint):
    patch(model_job, "_wait_printing", lambda *a, **k: "printing")
    jp, g = make_job(mapped.root, "a")
    job = json.loads(jp.read_text())
    first = model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1}, job, None, 0, False)
    return g, first, lambda: model_job.print_mapped_native(
        g, mapped.st, {"display_off": 1}, {"0": 1}, job, None, 0, second_reprint)


def test_item3_a_deliberate_reprint_is_a_new_attempt_with_its_own_checkpoint(mapped, patch):
    g, first, again = launch_twice(mapped, patch, second_reprint=True)
    assert phases() == []  # the first attempt settled
    second = again()
    assert len(mapped.scripts) == 2 and len(uploads(mapped.run)) == 2
    assert second["lease_token"] != first["lease_token"]
    assert json.loads(g.with_suffix(".launch.json").read_text())["lease_token"] == second["lease_token"]


def test_item3_an_unapproved_replay_is_still_refused_before_anything_is_uploaded(mapped, patch):
    g, first, again = launch_twice(mapped, patch, second_reprint=False)
    with pytest.raises(RuntimeError, match="checkpoint exists"):
        again()
    assert len(mapped.scripts) == 1 and len(uploads(mapped.run)) == 1 and phases() == []


def test_item3_an_attempt_that_never_reached_the_printer_does_not_block_a_retry(mapped, monkeypatch, patch):
    patch(model_job, "_wait_printing", lambda *a, **k: "printing")
    jp, g = make_job(mapped.root, "a")
    job = json.loads(jp.read_text())
    original = model_job._mr.side_effect
    model_job._mr.side_effect = lambda st, path, data=None, timeout=30: (
        (_ for _ in ()).throw(http_error(400, "Must home axis first")) if path == "/printer/gcode/script"
        else original(st, path, data=data, timeout=timeout))
    with pytest.raises(SystemExit, match="rejected the start request"):
        model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1}, job, None, 0, False)
    model_job._mr.side_effect = original
    out = model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1}, job, None, 0, False)
    assert out["started"] is True  # no --reprint needed: the first attempt provably sent nothing


def test_item3_the_checkpoint_file_is_kept_for_print_again(mapped, patch):
    g, _first, _again = launch_twice(mapped, patch, second_reprint=False)
    assert g.with_suffix(".launch.json").is_file()


# --------------------------------------------------------------------------- item 4: Bambu naming

@pytest.mark.parametrize("reported", ["plate_1_abc", "plate_1_abc.gcode.3mf", "plate_1_abc.gcode"])
def test_item4_same_remote_covers_bambu_naming(reported):
    assert launch_state.same_remote(reported, "plate_1_abc.gcode.3mf")
    assert not launch_state.same_remote("plate_1_abd", "plate_1_abc.gcode.3mf")
    assert not launch_state.same_remote("", "plate_1_abc.gcode.3mf")


def test_item4_a_bambu_lease_settles_from_the_bare_subtask_name(tmp_path):
    lease = launch_state.acquire("p1", job_json="j", gcode_sha256="s", remote="Benchy_abc.gcode.3mf")
    lease.submitting()
    lease.uncertain("submitted, awaiting confirmation")
    # The next launch sees the printer's subtask_name: the package name without its extensions.
    again = launch_state.acquire("p1", job_json="k", gcode_sha256="t", remote="Other.gcode.3mf",
                                 observed_file="Benchy_abc")
    assert again.token != lease.token and phases() == ["leased"]
    other = launch_state.acquire("p2", job_json="j", gcode_sha256="s", remote="Benchy_abc.gcode.3mf")
    other.submitting()
    other.uncertain("submitted, awaiting confirmation")
    with pytest.raises(SystemExit, match="uncertain"):  # a different bare name settles nothing
        launch_state.acquire("p2", job_json="k", gcode_sha256="t", remote="Z.gcode.3mf", observed_file="Benchy_abd")


# --------------------------------------------------------------------------- item 7: stale leases

def age(minutes, **columns):
    con = sqlite3.connect(os.environ["PRINT_HANDY_LAUNCH_DB"])
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - minutes * 60))
    with con:
        con.execute("UPDATE leases SET updated_at=?, leased_at=?", (stamp, stamp))
        for column, value in columns.items():
            con.execute(f"UPDATE leases SET {column}=?", (value,))
    con.close()


def test_item7_a_leased_row_of_an_unreachable_owner_expires_after_the_ttl():
    launch_state.acquire("p1", remote="a.gcode")
    age(1, host="the-old-container")
    with pytest.raises(SystemExit, match="in progress"):
        launch_state.acquire("p1", remote="b.gcode")  # another container, young: cannot be checked, still alive
    age(30, host="the-old-container")
    lease = launch_state.acquire("p1", remote="b.gcode")
    assert phases() == ["leased"] and lease.token == leases()[0]["token"]


def test_item7_a_live_owner_on_this_host_is_never_stolen_however_old_the_row():
    held = launch_state.acquire("p1", job_json="old", gcode_sha256="s", remote="a.gcode")  # this PID is alive
    age(24 * 60)
    with pytest.raises(SystemExit, match="another launch on this printer is in progress"):
        launch_state.acquire("p1", remote="b.gcode")
    held.submitting()  # and its owner can still go on
    assert phases() == ["submitting"]


def test_item7_a_stolen_lease_refuses_to_submit_before_any_request():
    first = launch_state.acquire("p1", remote="a.gcode")
    age(30, host="the-old-container")
    second = launch_state.acquire("p1", remote="b.gcode")  # the TTL released the first launch's row
    launch_state.reset_submitted()
    with pytest.raises(SystemExit, match="lost its printer lease before sending; nothing started"):
        first.submitting()
    assert launch_state.submitted() is False
    assert [r["token"] for r in leases()] == [second.token]
    first.uncertain("late")  # a stale owner cannot change the thief's row
    first.release()
    assert [r["phase"] for r in leases()] == ["leased"]


def test_item1_a_launch_function_stops_when_its_lease_was_stolen(rig, monkeypatch):
    client = ScriptedClient(post=[printing])
    real = launch_state.Lease.submitting

    def stolen(self):
        age(30, host="the-old-container")
        launch_state.acquire("p1", remote="thief.gcode")
        launch_state.Lease._set(self, "leased")  # no-op for the stolen row
        return real(self)
    monkeypatch.setattr(launch_state.Lease, "submitting", stolen)
    monkeypatch.setattr(model_job, "_printer_key", lambda *a, **k: "p1")
    with pytest.raises(SystemExit, match="lost its printer lease"):
        launch("prusa", rig, client)
    assert client.uploads == [] and client.starts == []


@pytest.mark.parametrize("phase", ["submitting", "uncertain"])
def test_item7_submitting_and_uncertain_rows_never_expire(phase):
    lease = launch_state.acquire("p1", remote="a.gcode")
    lease.submitting()
    if phase == "uncertain":
        lease.uncertain("lost")
    age(24 * 60, pid=dead_pid(), host="another-container")
    with pytest.raises(SystemExit, match="REFUSED"):
        launch_state.acquire("p1", remote="b.gcode")
    assert phases() == [phase]


def test_item7_the_operator_can_clear_an_expired_leased_row():
    launch_state.acquire("p1", remote="a.gcode")
    with pytest.raises(SystemExit, match="still running"):
        launch_state.resolve("p1", "not_started")
    age(20, host="the-old-container")
    assert launch_state.resolve("p1", "not_started")["reconciled"] == "not_started"


# --------------------------------------------------------------------------- the Lease class itself

def test_lease_methods_survive_the_submitted_flag_block():
    """#693 inserted the process-wide submitted() block inside the class, so release/uncertain became
    nested functions of reset_submitted and every launch ended in an AttributeError."""
    for name in ("submitting", "uncertain", "release"):
        assert callable(getattr(launch_state.Lease, name, None)), name


# --------------------------------------------------------------------------- item 9: the spec'd native mock

def test_item9_the_mapped_fixture_native_mock_rejects_attributes_the_module_lacks(mapped):
    assert callable(mapped.native.require_idle_screenless)
    with pytest.raises(AttributeError):
        mapped.native.require_idle_screenles  # a typo the bare Mock() used to accept


# --------------------------------------------------------------------------- review follow-ups

def test_item3_another_files_printing_is_not_this_start():
    with mock.patch.object(model_job, "_printer_phase",
                           return_value=("printing", "someone-elses.gcode", "Moonraker reports active execution")), \
            mock.patch.object(model_job.time, "sleep"):
        assert REAL_WAIT_PRINTING({}, "mine.gcode", 0.05) == "busy"
    with mock.patch.object(model_job, "_printer_phase", return_value=("printing", "mine.gcode", "x")):
        assert REAL_WAIT_PRINTING({}, "mine.gcode", 0.05) == "printing"


def test_item4_a_cut_off_outcome_line_is_pending_not_started():
    whole = 'START READBACK ' + json.dumps({"phase": "executing", "started": True, "file": "a.gcode"})
    assert prepared_intake.classify_start(True, whole).status == "STARTED"
    for cut in (whole[:40], whole[:-1], 'START READBACK {"phase": "exec', "READBACK"):
        verdict = prepared_intake.classify_start(True, "PRINT STARTED {}\n" + cut)
        assert verdict.status == "START_PENDING" and verdict.started is None, cut
    assert print_next.outcome_unreadable('MAPPED START READBACK {"phase": "x"') is True
    assert print_next.outcome_unreadable("no outcome line at all") is False


def test_item4_the_mapped_detail_goes_on_its_own_earlier_line(mapped, patch, capsys):
    patch(model_job, "_wait_printing", lambda *a, **k: "printing")
    jp, g = make_job(mapped.root, "a")
    model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1}, json.loads(jp.read_text()), jp, 0, False)
    lines = capsys.readouterr().out.splitlines()
    detail = [i for i, ln in enumerate(lines) if ln.startswith("MAPPED START DETAIL ")]
    final = [i for i, ln in enumerate(lines) if ln.startswith("MAPPED START READBACK ")]
    assert detail and final and detail[-1] < final[-1] == len(lines) - 1
    assert len(lines[final[-1]]) < 700 and "command" not in lines[final[-1]]


def test_item5_a_failure_before_submitting_releases_the_start_claim_on_the_mapped_path(mapped):
    seen = []

    def card(*a, **k):
        seen.append(bool(model_job._read_claims()))
        raise RuntimeError("job card failed")
    model_job._publish_job_card.side_effect = card
    jp, g = make_job(mapped.root, "a")
    with pytest.raises(RuntimeError, match="job card failed"):
        model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1}, json.loads(jp.read_text()), jp, 0, False)
    assert seen == [True]  # the claim was held when it failed
    assert model_job._read_claims() == {} and phases() == []
    assert json.loads(g.with_suffix(".launch.json").read_text())["phase"] == "not_submitted"


def test_item5_a_failure_before_submitting_releases_the_start_claim_on_the_generic_path(printer):
    seen = []

    def card(*a, **k):
        seen.append(bool(model_job._read_claims()))
        raise RuntimeError("job card failed")
    model_job._publish_job_card.side_effect = card
    jp, _ = make_job(printer.root, "a")
    with pytest.raises(RuntimeError, match="job card failed"):
        model_job.print_now(jp)
    assert seen == [True] and model_job._read_claims() == {} and phases() == []
