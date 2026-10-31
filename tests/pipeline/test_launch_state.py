"""Adversarial offline tests for transactional launch state: approval consumption and the printer lease.

No transport: upload, start and printer reads are mocked. Nothing here contacts a printer.
"""
import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from unittest import mock

import pytest

from print_handy.core import launch_state, model_job
from print_handy.printers import flashforge
from print_handy.printers.flashforge.zmod import screen as zmod_screen

PRINTER = "http://m"  # the moonraker_url the mocked hardware record reports


class Crash(BaseException):
    """Stands in for the process dying mid-launch: no ordinary exception handler sees it."""


def upload_ok(cmd, *args, **kwargs):
    if len(cmd) >= 4 and cmd[1] == "upload":
        return mock.Mock(returncode=0, stdout=json.dumps({"result": {"item": {"root": "gcodes", "path": cmd[3]}}}), stderr="")
    return mock.Mock(returncode=0, stdout="", stderr="")


def make_job(root, name):
    folder = Path(root) / name
    (folder / "out").mkdir(parents=True)
    g = folder / "out" / "x.gcode"
    g.write_text(f"; {name}\nSTART_PRINT EXTRUDER_TEMP=220 BED_TEMP=60\n")
    (folder / "JOB.json").write_text(json.dumps({"title": name, "gcode": str(g)}))
    return folder / "JOB.json", g


def dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def leases():
    db = os.environ["PRINT_HANDY_LAUNCH_DB"]
    if not Path(db).exists():
        return []
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM leases")]
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def simulate_restart():
    """The owner of every lease is gone, as after a crash or host restart."""
    con = sqlite3.connect(os.environ["PRINT_HANDY_LAUNCH_DB"])
    with con:
        con.execute("UPDATE leases SET pid=?", (dead_pid(),))
    con.close()


@pytest.fixture
def printer(tmp_path):
    """print_now with every gate passing and the transport mocked. Returns the mocks to steer."""
    phase = mock.Mock(return_value=("standby", "", "Ready"))
    run = mock.Mock(side_effect=upload_ok)
    mr = mock.Mock(return_value={"result": "ok"})
    patches = [mock.patch.object(model_job, "START_CLAIMS", tmp_path / "claims.json"),
               mock.patch.object(model_job.printer_state, "load", return_value={"moonraker_url": PRINTER}),
               mock.patch.object(model_job, "_printer_phase", phase),
               mock.patch.object(model_job, "verify_gcode"),
               mock.patch.object(model_job, "_verify_filament_readiness"),
               mock.patch.object(model_job, "_mr", mr),
               mock.patch.object(model_job, "_wait_printing", return_value="printing"),
               mock.patch.object(model_job, "record_start", return_value="/vault/x.md"),
               mock.patch.object(model_job, "live_snapshot", return_value={"nozzle_c": None, "bed_c": None, "slots": "unknown"}),
               mock.patch.object(model_job, "mesh_readiness", return_value={"profile": "auto", "saved_at": None, "print_leveling": 0,
                                                                              "problem": None, "stale": None, "z_offset_mm": 0.0,
                                                                              "offset_source": "saved gcode offset"}),
               mock.patch.object(model_job, "screen_z_offset", return_value=0.0),
               mock.patch.object(model_job, "_announce_print_name"),
               mock.patch.object(model_job, "_publish_job_card"),
               mock.patch.object(model_job.subprocess, "run", run)]
    for p in patches:
        p.start()
    yield mock.Mock(phase=phase, run=run, mr=mr, root=tmp_path)
    for p in reversed(patches):
        p.stop()


def uploads(run):
    return [c.args[0][3] for c in run.call_args_list if c.args and len(c.args[0]) >= 4 and c.args[0][1] == "upload"]


def start_requests(mr):
    paths = {c["path"] for c in model_job.printers.current().moonraker_start_commands("f")}
    return [c for c in mr.call_args_list if len(c.args) > 1 and c.args[1] in paths]


# 1. One approval, two stale snapshots.

def test_stale_snapshots_cannot_consume_one_approval_twice(tmp_path):
    jp, g = make_job(tmp_path, "a")
    first, second = json.loads(jp.read_text()), json.loads(jp.read_text())
    model_job._consume_approval(jp, first, g)
    with pytest.raises(SystemExit, match="already used"):
        model_job._consume_approval(jp, second, g)
    assert len(json.loads(jp.read_text())["approvals"]) == 1


def test_simultaneous_callers_consume_one_approval_exactly_once(tmp_path):
    jp, g = make_job(tmp_path, "a")
    snapshots = [json.loads(jp.read_text()) for _ in range(8)]
    gate = threading.Barrier(len(snapshots))
    won, refused = [], []

    def caller(job):
        gate.wait()
        try:
            model_job._consume_approval(jp, job, g)
            won.append(job)
        except SystemExit as exc:
            refused.append(str(exc))

    threads = [threading.Thread(target=caller, args=(s,)) for s in snapshots]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(won) == 1 and len(refused) == len(snapshots) - 1
    assert all("already used" in r for r in refused)
    assert len(json.loads(jp.read_text())["approvals"]) == 1


def test_a_stale_job_write_cannot_resurrect_a_consumed_approval(tmp_path):
    """Another JOB.json writer (slice, remap) saving an older snapshot must not erase the consumption."""
    jp, g = make_job(tmp_path, "a")
    stale = json.loads(jp.read_text())
    model_job._consume_approval(jp, json.loads(jp.read_text()), g)
    model_job.write_job(jp.parent, stale)  # lost update: approvals list gone from JOB.json
    with pytest.raises(SystemExit, match="already used"):
        model_job._consume_approval(jp, json.loads(jp.read_text()), g)


def test_reprint_is_still_a_new_explicit_approval(tmp_path):
    jp, g = make_job(tmp_path, "a")
    model_job._consume_approval(jp, json.loads(jp.read_text()), g)
    model_job._consume_approval(jp, json.loads(jp.read_text()), g, reprint=True)
    assert [a["kind"] for a in json.loads(jp.read_text())["approvals"]] == ["bed_clear", "reprint"]


def test_consumption_still_runs_the_artifact_guard_on_current_state(tmp_path):
    jp, g = make_job(tmp_path, "a")
    stale = json.loads(jp.read_text())
    current = dict(stale, artifact_binding={"gcode_sha256": "0" * 64}, gcode_verified=True,
                   downloaded_file=str(g))
    model_job.write_job(jp.parent, current)  # resliced/rebound since the caller read it
    with pytest.raises(RuntimeError, match="stale color artifact"):
        model_job._consume_approval(jp, stale, g)
    assert "approvals" not in json.loads(jp.read_text())


# 2. Two different jobs, one printer.

def test_second_job_is_refused_while_the_first_launch_is_in_flight(printer):
    a, _ = make_job(printer.root, "a")
    b, _ = make_job(printer.root, "b")
    seen = {}

    def upload_a_then_race_b(cmd, *args, **kwargs):
        if cmd[1] == "upload" and "b" not in seen:
            seen["b"] = "raced"
            with pytest.raises(SystemExit, match="REFUSED: another launch .*in progress"):
                model_job.print_now(b)  # B also saw an idle printer before A submitted
        return upload_ok(cmd, *args, **kwargs)

    printer.run.side_effect = upload_a_then_race_b
    assert model_job.print_now(a)["started"] is True
    assert len(uploads(printer.run)) == 1 and len(start_requests(printer.mr)) == 1
    assert "approvals" not in json.loads(b.read_text())  # a refused lease consumes nothing


def test_stale_idle_check_cannot_start_a_second_job_after_the_first_started(printer):
    a, _ = make_job(printer.root, "a")
    b, _ = make_job(printer.root, "b")
    model_job.print_now(a)
    remote = uploads(printer.run)[0]
    calls = []

    def idle_once_then_busy(st):
        calls.append(1)
        return ("standby", "", "Ready") if len(calls) == 1 else ("printing", remote, "Printing")

    printer.phase.side_effect = idle_once_then_busy  # B's early check predates A's start
    with pytest.raises(SystemExit, match="REFUSED: printer is busy"):
        model_job.print_now(b)
    assert len(uploads(printer.run)) == 1 and len(start_requests(printer.mr)) == 1
    assert "approvals" not in json.loads(b.read_text())


# 3. A crash after submission is uncertain until reconciled, never replayed.

def crash_on_start(mr):
    paths = {c["path"] for c in model_job.printers.current().moonraker_start_commands("f")}

    def fake(st, path, data=None, timeout=30):
        if path in paths:
            raise Crash("process died after sending the start request")
        return {"result": "ok"}
    mr.side_effect = fake


def test_crash_after_submission_leaves_the_printer_uncertain(printer):
    a, _ = make_job(printer.root, "a")
    b, _ = make_job(printer.root, "b")
    crash_on_start(printer.mr)
    with pytest.raises(Crash):
        model_job.print_now(a)
    simulate_restart()
    rows = leases()
    assert len(rows) == 1 and rows[0]["phase"] in ("submitting", "uncertain")
    printer.mr.side_effect = None
    for target, kw in ((a, {"reprint": True}), (b, {})):
        with pytest.raises(SystemExit, match="REFUSED: .*uncertain"):
            model_job.print_now(target, **kw)
    assert len(uploads(printer.run)) == 1  # nothing replayed
    assert "approvals" not in json.loads(b.read_text())


def test_uncertain_submission_reconciles_from_printer_evidence_not_replay(printer):
    a, _ = make_job(printer.root, "a")
    b, _ = make_job(printer.root, "b")
    crash_on_start(printer.mr)
    with pytest.raises(Crash):
        model_job.print_now(a)
    simulate_restart()
    remote = uploads(printer.run)[0]
    printer.mr.side_effect = None
    printer.phase.return_value = ("complete", remote, "Ready")  # the printer did run A's exact file
    with pytest.raises(SystemExit, match="already used"):
        model_job.print_now(a)  # reconciled as submitted; the approval stays consumed
    assert model_job.print_now(b)["started"] is True
    assert leases() == []
    assert len(uploads(printer.run)) == 2  # A once, then B; A was never resubmitted


def test_operator_reconciles_an_uncertain_submission_without_replaying_it(printer):
    from print_handy.core import launch_state
    a, _ = make_job(printer.root, "a")
    crash_on_start(printer.mr)
    with pytest.raises(Crash):
        model_job.print_now(a)
    simulate_restart()
    printer.mr.side_effect = None
    # Since #334 the lease row is keyed by the roster printer id, the same key
    # the reconcile-launch CLI resolves with, not by the Moonraker URL.
    assert launch_state.resolve(model_job._printer_key(), "not_started")["phase"] in ("submitting", "uncertain")
    assert leases() == []
    with pytest.raises(SystemExit, match="already used"):
        model_job.print_now(a)  # a new start needs a new explicit approval
    assert model_job.print_now(a, reprint=True)["started"] is True


def test_crash_before_submission_is_released_after_restart_and_approval_stays_used(printer):
    a, _ = make_job(printer.root, "a")

    def crash_during_upload(cmd, *args, **kwargs):
        raise Crash("process died while uploading")
    printer.run.side_effect = crash_during_upload
    with pytest.raises(Crash):
        model_job.print_now(a)
    printer.run.side_effect = upload_ok
    simulate_restart()
    with pytest.raises(SystemExit, match="already used"):
        model_job.print_now(a)  # the approval from before the restart is not reusable
    assert model_job.print_now(a, reprint=True)["started"] is True
    assert leases() == []


def test_upload_failure_releases_the_lease(printer):
    a, _ = make_job(printer.root, "a")
    printer.run.side_effect = lambda cmd, *a_, **k: mock.Mock(returncode=1, stdout="", stderr="boom")
    with pytest.raises(SystemExit, match="could not be uploaded to the printer"):
        model_job.print_now(a)
    assert leases() == []


def test_hard_crash_before_submission_is_released_after_restart():
    from print_handy.core import launch_state
    launch_state.acquire(PRINTER, job_json="/jobs/a/JOB.json", gcode_sha256="a" * 64, remote="a.gcode")
    with pytest.raises(SystemExit, match="in progress"):
        launch_state.acquire(PRINTER, remote="b.gcode")
    simulate_restart()  # the owner died during checks or upload: nothing was sent to start
    launch_state.acquire(PRINTER, remote="b.gcode").release()
    assert leases() == []


# OctoPrint: the same lease, with upload and start as one uncertain step.

class FakeOctoPrint:
    transport, key, multi_slot = "octoprint", "octoprint.marlin", False

    def __init__(self):
        self.statuses, self.uploads = [], []
        outer = self

        class Client:
            def status(self):
                status = outer.statuses.pop(0) if len(outer.statuses) > 1 else outer.statuses[0]
                return status() if callable(status) else status
            def require_ready(self, status):
                if status["state"] != "standby": raise RuntimeError(f"printer is {status['state']}")
            def upload(self, gcode, remote): outer.uploads.append(remote); return remote
            def start(self, remote, bed, hotend): pass
        self._client = Client()

    def client(self): return self._client
    def normalize_status(self, raw): return raw
    def normalize_tool_mapping(self, mapping, state): return mapping


@pytest.fixture
def octoprint(tmp_path):
    fake = FakeOctoPrint()
    st = {"temps_c": {"bed": 60, "nozzle": 210}, "nozzle": {"type": "hardened_steel"}}
    with mock.patch.object(model_job.printers, "current", return_value=fake), \
         mock.patch.object(model_job.printer_state, "load", return_value=st), \
         mock.patch.object(model_job, "_with_gcode_temperatures", side_effect=lambda state, text: state), \
         mock.patch.object(model_job, "verify_gcode"), \
         mock.patch.object(model_job, "record_start", return_value="/vault/x.md"):
        fake.root = tmp_path
        yield fake


def octo_job(root, name):
    jp, g = make_job(root, name)
    jp.write_text(json.dumps(dict(json.loads(jp.read_text()), printer_model="octoprint.marlin")))
    return jp


def test_octoprint_unconfirmed_start_is_uncertain_until_the_printer_reports_it(octoprint):
    a, b = octo_job(octoprint.root, "a"), octo_job(octoprint.root, "b")
    idle = {"state": "standby", "filename": ""}
    octoprint.statuses = [idle, idle, idle]  # pre-check, in-lease check, readback: never confirmed
    assert model_job.print_now(a)["outcome"] == "unconfirmed"
    assert [r["phase"] for r in leases()] == ["uncertain"]
    octoprint.statuses = [idle]
    with pytest.raises(SystemExit, match="uncertain"):
        model_job.print_now(b)
    done = {"state": "standby", "filename": octoprint.uploads[0]}  # OctoPrint shows A's file: it ran
    octoprint.statuses = [done, done, lambda: {"state": "printing", "filename": octoprint.uploads[-1]}]
    assert model_job.print_now(b)["outcome"] == "printing"
    assert len(octoprint.uploads) == 2 and leases() == []


# Native mapped route (SET_ZCOLOR): the idle guard runs again inside the lease.

from print_handy.printers.flashforge.zmod import native_idle  # noqa: E402

mapped_presence = [True]  # zmod_color hasFilament for slot 1 in the mapped fixture
mapped_label = ["PLA"]  # zmod_color Material for slot 1 in the mapped fixture
mapped_hex = ["000000"]  # zmod_color HEX for slot 1 in the mapped fixture
mapped_history = []  # Moonraker /server/history/list jobs in the mapped fixture

@pytest.fixture
def mapped(tmp_path):
    # spec the real ZMod native module: a bare Mock() accepted any attribute, so a call to a function that
    # does not exist (or was renamed) passed silently.
    native = mock.Mock(spec=flashforge.zmod)
    native.normalize_mapping.side_effect = lambda m, st: {"0": 1}
    native.require_idle_settled.return_value = True
    native.require_idle_screenless.side_effect = native_idle.require_idle_screenless
    native.read_zmod_color_slots.side_effect = lambda status: {
        int(item["ID"]): item.get("hasFilament") for item in status["zmod_color"]["slots"]}
    native.screen_label_matches.side_effect = zmod_screen.label_matches
    adapter = mock.Mock(native=native)
    scripts = []
    mapped_presence[0] = True
    mapped_label[0] = "PLA"
    mapped_hex[0] = "000000"
    mapped_history.clear()

    def fake_mr(st, path, data=None, timeout=30):
        if path == "/printer/gcode/script":
            scripts.append(data["script"])
            return {"result": "ok"}
        if path.startswith("/server/history/list"):
            return {"result": {"count": len(mapped_history), "jobs": list(mapped_history)}}
        return {"result": {"status": {
            "webhooks": {"state": "ready"}, "print_stats": {"state": "standby", "filename": ""},
            "extruder": {"target": 0}, "heater_bed": {"target": 0},
            "zmod_color": {"slots": [{"ID": 1, "Material": mapped_label[0], "HEX": mapped_hex[0],
                                      "hasFilament": mapped_presence[0]}]},
            "filament_switch_sensor head_switch_sensor": {"enabled": True}}}}

    st = {"moonraker_url": PRINTER, "filament_slots": {"1": {"material": "PLA", "hex": "#000000"}}}
    with mock.patch.object(model_job.printers, "current", return_value=adapter), \
         mock.patch.object(model_job, "START_CLAIMS", tmp_path / "claims.json"), \
         mock.patch.object(model_job, "validate_saved_mesh_start"), \
         mock.patch.object(model_job, "_mr", side_effect=fake_mr), \
         mock.patch.object(model_job, "read_stock_detail", return_value={}), \
         mock.patch.object(model_job, "read_stock_filament_slots_from_detail", return_value={1: True}), \
         mock.patch.object(model_job, "_sync_active_spool"), \
         mock.patch.object(model_job, "_announce_print_name"), \
         mock.patch.object(model_job, "_publish_job_card"), \
         mock.patch.object(model_job, "_assign_print_number", return_value=None), \
         mock.patch.object(model_job, "record_start", return_value="/vault/x.md"), \
         mock.patch.object(model_job, "_wait_printing", return_value="standby"), \
         mock.patch.object(model_job, "_preparation_evidence", return_value=False), \
         mock.patch.object(model_job.subprocess, "run", side_effect=upload_ok) as run:
        yield mock.Mock(native=native, scripts=scripts, run=run, st=st, root=tmp_path)


def launch_mapped(m, name):
    jp, g = make_job(m.root, name)
    job = json.loads(jp.read_text())
    return jp, lambda: model_job.print_mapped_native(g, m.st, {"display_off": 0}, {"0": 1}, job, jp, 0, False)


@pytest.mark.parametrize("foreign", [False, True])
def test_mapped_launch_reconciles_lost_post_response_by_exact_file(mapped, monkeypatch, foreign):
    jp, g = make_job(mapped.root, "timeout-start")
    job = json.loads(jp.read_text())
    original = model_job._mr.side_effect  # the mapped fixture owns the patch
    post_count = 0

    def lose_post(st, path, data=None, timeout=30):
        nonlocal post_count
        if path.startswith("/printer/gcode/script"):
            post_count += 1
            mapped.scripts.append(data["script"])
            submitted = data["script"].split("FILENAME=", 1)[1].split()[0]
            mapped.reported_file = "gcodes/" + submitted
            raise TimeoutError("response lost after Moonraker accepted command")
        if "zmod_color" in path:
            status = {
                "print_stats": {"state": "standby", "filename": ""},
                "virtual_sdcard": {"is_active": False}, "webhooks": {"state": "ready"},
                "extruder": {"target": 0}, "heater_bed": {"target": 0},
                "zmod_color": {"slots": [{"ID": 1, "Material": "PLA", "HEX": "000000", "hasFilament": True}]},
                "filament_switch_sensor head_switch_sensor": {"enabled": True}}
            return {"result": {"status": status}}
        if "print_stats&extruder" in path:
            return {"result": {"status": {
                "print_stats": {"state": "printing", "filename": "3mf_print/" + ("x.gcode" if foreign else mapped.reported_file.removeprefix("gcodes/")), "filament_used": 4},
                "virtual_sdcard": {"is_active": True}, "webhooks": {"state": "ready"}}}}
        return original(st, path, data=data, timeout=timeout)

    model_job._mr.side_effect = lose_post
    out = model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1}, job, jp, 0, False)
    assert out["phase"] == ("preparing_or_unresolved" if foreign else "executing")
    assert post_count == 1 and len(mapped.scripts) == 1
    assert len(uploads(mapped.run)) == 1
    if not foreign:
        assert any(row['phase'] == 'uncertain' for row in leases())
    if foreign:
        with pytest.raises((RuntimeError, SystemExit)):
            model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1}, job, jp, 0, False)
        assert post_count == 1


def test_native_file_identity_accepts_only_exact_3mf_conversion():
    assert model_job._native_file_matches('3mf_print/part_abc.gcode', 'part_abc.gcode.3mf')
    assert not model_job._native_file_matches('3mf_print/part_def.gcode', 'part_abc.gcode.3mf')
    assert not model_job._native_file_matches('other/part_abc.gcode', 'part_abc.gcode.3mf')


def test_mapped_launch_rechecks_idle_inside_the_lease(mapped):
    jp, launch = launch_mapped(mapped, "a")
    mapped.native.require_idle_settled.side_effect = [True, RuntimeError("REFUSED: idle guard failed: Moonraker reports active execution")]
    with pytest.raises(SystemExit, match="idle guard failed"):
        launch()
    assert uploads(mapped.run) == [] and mapped.scripts == [] and leases() == []
    assert "approvals" not in json.loads(jp.read_text())


def test_mapped_launch_screenless_starts_from_klipper_state_alone(mapped):
    """Screenless ZMod (display_off=1, HelixScreen or no screen): the stock /detail API is
    down, so idle and slot presence come from Moonraker and zmod_color, and the print
    starts with exactly one SET_ZCOLOR, as ZMod's own print-file macro does."""
    jp, g = make_job(mapped.root, "a")
    job = json.loads(jp.read_text())
    with mock.patch.object(model_job, "read_stock_detail",
                           side_effect=AssertionError("stock /detail read in screenless mode")):
        out = model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1}, job, jp, 0, False)
    assert len(uploads(mapped.run)) == 1
    assert len(mapped.scripts) == 1 and mapped.scripts[0].startswith("SET_ZCOLOR FILENAME=")
    assert out["submission_count"] == 1
    mapped.native.require_idle_settled.assert_not_called()
    assert mapped.native.require_idle_screenless.call_count == 2  # before upload and inside the lease


def test_mapped_launch_screenless_refuses_an_empty_slot_before_upload(mapped):
    mapped_presence[0] = False
    jp, g = make_job(mapped.root, "a")
    job = json.loads(jp.read_text())
    with mock.patch.object(model_job, "read_stock_detail", side_effect=AssertionError("stock read")), \
            pytest.raises(RuntimeError, match="occupancy is unknown or unconfirmed"):
        model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1}, job, jp, 0, False)
    assert uploads(mapped.run) == [] and mapped.scripts == [] and leases() == []


def test_mapped_launch_refuses_an_unknown_screen_mode(mapped):
    jp, g = make_job(mapped.root, "a")
    job = json.loads(jp.read_text())
    with pytest.raises(SystemExit, match="REFUSED: ZMod reports an unknown screen mode.*Nothing uploaded"):
        model_job.print_mapped_native(g, mapped.st, {"display_off": None}, {"0": 1}, job, jp, 0, False)
    assert uploads(mapped.run) == [] and mapped.scripts == [] and leases() == []


@pytest.mark.parametrize("moonraker, reason", [
    ({"state": "ready", "print": {"state": "standby"}, "extruder": {"target": 0}, "bed": {"target": 0}}, None),
    ({"state": "ready", "print": {"state": "printing"}, "extruder": {"target": 0}, "bed": {"target": 0}},
     "print.state"),
    ({"state": "ready", "print": {"state": "standby"}, "extruder": {"target": 220}, "bed": {"target": 0}},
     "extruder.target"),
    ({"state": "ready", "print": {"state": "standby"}, "extruder": {"target": 0}, "bed": {}},
     "bed.target"),
    ({"state": "startup", "print": {"state": "standby"}, "extruder": {"target": 0}, "bed": {"target": 0}},
     "webhooks.state"),
])
def test_screenless_idle_guard_needs_every_moonraker_predicate(moonraker, reason):
    if reason is None:
        assert native_idle.require_idle_screenless(lambda: moonraker) is True
    else:
        with pytest.raises(RuntimeError, match=f"REFUSED: idle guard failed: .*{reason}"):
            native_idle.require_idle_screenless(lambda: moonraker)


@pytest.mark.parametrize("heating", [True, False])
def test_screenless_set_zcolor_timeout_during_warmup_is_not_a_refusal(mapped, monkeypatch, heating):
    """2026-10-30 19:22 ET regression (#616 routed screenless starts through SET_ZCOLOR):
    the POST stayed open through warmup, timed out after 30 s, and the chat said the
    start was refused while the AD5X began printing 2.5 minutes later."""
    jp, g = make_job(mapped.root, "warmup-timeout")
    job = json.loads(jp.read_text())
    original = model_job._mr.side_effect  # the mapped fixture owns the patch

    def held_open(st, path, data=None, timeout=30):
        if path.startswith("/printer/gcode/script"):
            mapped.scripts.append(data["script"])
            raise TimeoutError("timed out")
        if mapped.scripts and "print_stats&extruder" in path:
            # Still preparing: Klipper has not opened the file yet.
            return {"result": {"status": {
                "print_stats": {"state": "standby", "filename": ""},
                "virtual_sdcard": {"is_active": False}, "webhooks": {"state": "ready"},
                "extruder": {"target": 0}, "heater_bed": {"target": 0}}}}
        return original(st, path, data=data, timeout=timeout)

    model_job._mr.side_effect = held_open
    model_job._preparation_evidence.return_value = heating
    out = model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1}, job, jp, 0, False)
    assert len(mapped.scripts) == 1  # never resubmitted
    claims = model_job._read_claims()
    if heating:
        assert out["phase"] == "preparing" and out["accepted"] is True and out["started"] is True
        assert claims  # the intake announces this start; the listener must not post a second card
    else:
        assert out["phase"] == "preparing_or_unresolved" and out["accepted"] is False
        assert claims == {}  # unannounced: the listener posts "Print started" when it begins
    assert any(row["phase"] == "uncertain" for row in leases())  # duplicate guard holds either way


def test_mapped_unresolved_start_is_uncertain_and_blocks_the_next_job(mapped):
    _, launch_a = launch_mapped(mapped, "a")
    out = launch_a()
    assert out["phase"] == "preparing_or_unresolved" and len(mapped.scripts) == 1
    assert [r["phase"] for r in leases()] == ["uncertain"]
    # 2026-10-30: the intake did not announce this start, so the listener must
    # still be free to post "Print started" once the printer reaches it.
    assert model_job._read_claims() == {}
    _, launch_b = launch_mapped(mapped, "b")
    with pytest.raises(SystemExit, match="uncertain"):
        launch_b()
    assert len(mapped.scripts) == 1 and len(uploads(mapped.run)) == 1


def test_mapped_launch_uploads_3mf_package_when_zmod_is_current(mapped, monkeypatch):
    """#564: the native mapped route uploads the .gcode.3mf package and points
    SET_ZCOLOR at the package name when ZMod >= 1.7.5 and the format is auto."""
    adapter = model_job.printers.current()
    adapter.config = {"printer": {"zmod_delivery_format": "auto"}}
    adapter.model.vendor_key = "flashforge"
    monkeypatch.setattr(model_job, "_zmod_version", lambda st: (1, 7, 5))
    jp, g = make_job(mapped.root, "a")
    job = json.loads(jp.read_text())
    out = model_job.print_mapped_native(g, mapped.st, {"display_off": 0}, {"0": 1}, job, jp, 0, False)
    cmds = [c.args[0] for c in mapped.run.call_args_list if len(c.args[0]) >= 4 and c.args[0][1] == "upload"]
    assert len(cmds) == 1
    cmd = cmds[0]
    assert cmd[2].endswith(".gcode.3mf") and cmd[3].endswith(".gcode.3mf")
    assert cmd[4] == model_job.file_sha256(Path(cmd[2]))
    assert mapped.scripts and mapped.scripts[0].startswith("SET_ZCOLOR FILENAME=" + cmd[3])
    assert out["delivery_format"]["format"] == "3mf"
    # The note lands on the job dict, which write_job persists (best effort in this
    # fixture: the mocked native layer leaves non-serializable state in the job, so
    # the on-disk write warns and skips — the same behaviour as the pre-existing
    # pending-usage write in this harness).
    assert job["delivery_format"]["format"] == "3mf"


def test_mapped_launch_stays_on_plain_gcode_when_version_is_unknown(mapped, monkeypatch):
    """#564 fallback pin: auto + an unreadable ZMod version is today's plain upload."""
    adapter = model_job.printers.current()
    adapter.config = {"printer": {"zmod_delivery_format": "auto"}}
    adapter.model.vendor_key = "flashforge"
    monkeypatch.setattr(model_job, "_zmod_version", lambda st: None)
    _, launch = launch_mapped(mapped, "a")
    out = launch()
    cmds = [c.args[0] for c in mapped.run.call_args_list if len(c.args[0]) >= 4 and c.args[0][1] == "upload"]
    assert len(cmds) == 1
    assert cmds[0][2].endswith("x.gcode") and cmds[0][3].endswith(".gcode") and not cmds[0][3].endswith(".3mf")
    assert "delivery_format" not in out


def test_operator_can_clear_a_lease_left_by_another_host_but_not_a_live_local_one():
    from print_handy.core import launch_state
    lease = launch_state.acquire(PRINTER, remote="a.gcode")
    with pytest.raises(SystemExit, match="still running"):
        launch_state.resolve(PRINTER, "not_started")
    con = sqlite3.connect(os.environ["PRINT_HANDY_LAUNCH_DB"])
    with con:
        con.execute("UPDATE leases SET host='another-container'")
    con.close()
    with pytest.raises(SystemExit, match="in progress"):
        launch_state.acquire(PRINTER, remote="b.gcode")  # unknown liveness fails closed
    assert launch_state.resolve(PRINTER, "not_started")["remote"] == "a.gcode"
    lease.release()  # a stale token changes nothing
    assert leases() == []


@pytest.mark.parametrize("label,material", [("SILK", "Silk PLA"), ("PLA", "Silk PLA"), ("PLA", "PLA+"),
                                            ("silk", "PLA Silk")])
def test_mapped_launch_accepts_the_screen_label_print_handy_writes(mapped, label, material):
    """CHANGE_ZCOLOR writes SILK for Silk PLA; the printer reports SILK back (2026-10-31
    can holder refused as a live material mismatch on its own label)."""
    mapped_label[0] = label
    mapped.st["filament_slots"]["1"]["material"] = material
    jp, g = make_job(mapped.root, "a")
    job = json.loads(jp.read_text())
    out = model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1}, job, jp, 0, False)
    assert out["submission_count"] == 1 and len(uploads(mapped.run)) == 1


@pytest.mark.parametrize("label,material", [("SILK", "PETG"), ("PETG", "Silk PLA"), ("PLA", "PLA-CF"),
                                            ("?", "PLA"), ("", "PLA")])
def test_mapped_launch_refuses_a_slot_label_that_disagrees_before_upload(mapped, label, material):
    mapped_label[0] = label
    mapped.st["filament_slots"]["1"]["material"] = material
    jp, g = make_job(mapped.root, "a")
    job = json.loads(jp.read_text())
    with pytest.raises(SystemExit, match=r"REFUSED: printer slot 1 is labeled .* but Spoolman has .*Nothing uploaded"):
        model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1}, job, jp, 0, False)
    assert uploads(mapped.run) == [] and mapped.scripts == [] and leases() == []
# An uncertain start settles from the printer's own record of that exact file (2026-10-30
# Bag Clip: uploaded as X.gcode.3mf, reported as 3mf_print/X.gcode, finished before the next
# launch, and blocked every later job until a manual reconcile-launch).

HELD = "Bag-Clip_plate_1_bf4e.gcode.3mf"


def held_row(leased_at="2026-10-30T19:21:32-0400"):
    return {"remote": HELD, "leased_at": leased_at}


def epoch(text):
    from datetime import datetime
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S%z").timestamp()


@pytest.mark.parametrize("reported", ["3mf_print/Bag-Clip_plate_1_bf4e.gcode", "Bag-Clip_plate_1_bf4e.gcode",
                                      "/gcodes/Bag-Clip_plate_1_bf4e.gcode", HELD])
def test_the_current_file_settles_its_3mf_upload_by_any_documented_path(reported):
    assert launch_state.reached_printer(held_row(), reported)


def test_history_settles_a_start_only_after_the_lease_and_only_for_that_file():
    started = epoch("2026-10-30T19:24:41-0400")
    assert launch_state.reached_printer(held_row(), "", [("3mf_print/Bag-Clip_plate_1_bf4e.gcode", started)])
    # A reprint: the same file ran an hour before this lease. It says nothing about this start.
    assert not launch_state.reached_printer(held_row(), "", [("3mf_print/Bag-Clip_plate_1_bf4e.gcode",
                                                              started - 3600)])
    assert not launch_state.reached_printer(held_row(), "", [("3mf_print/Other_plate_1.gcode", started)])
    assert not launch_state.reached_printer(held_row(), "", [])
    assert not launch_state.reached_printer({"remote": None, "leased_at": "x"}, "", [("x", started)])


def test_history_is_read_only_when_the_current_file_does_not_settle_it():
    read = mock.Mock(side_effect=AssertionError("history read"))
    assert launch_state.reached_printer(held_row(), "3mf_print/Bag-Clip_plate_1_bf4e.gcode", read)
    broken = mock.Mock(side_effect=OSError("moonraker down"))
    assert not launch_state.reached_printer(held_row(), "", broken)


def hold_uncertain(key, remote):
    lease = launch_state.acquire(key, job_json="/archive/earlier/JOB.json", gcode_sha256="e" * 64, remote=remote)
    lease.uncertain("start request did not complete: TimeoutError('timed out')")
    simulate_restart()


def test_mapped_launch_settles_a_finished_uncertain_start_from_history(mapped):
    jp, g = make_job(mapped.root, "a")
    job = json.loads(jp.read_text())
    hold_uncertain(model_job._printer_key(mapped.st, job), HELD)
    import time
    mapped_history.append({"filename": "3mf_print/Bag-Clip_plate_1_bf4e.gcode", "start_time": time.time(),
                           "status": "completed"})
    out = model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1}, job, jp, 0, False)
    assert out["submission_count"] == 1 and len(uploads(mapped.run)) == 1
    assert all(row["job_json"] != "/archive/earlier/JOB.json" for row in leases())  # the held start is settled


def test_mapped_launch_stays_refused_when_history_does_not_show_the_held_file(mapped):
    jp, g = make_job(mapped.root, "a")
    job = json.loads(jp.read_text())
    hold_uncertain(model_job._printer_key(mapped.st, job), HELD)
    import time
    mapped_history.append({"filename": "3mf_print/Bag-Clip_plate_1_bf4e.gcode", "start_time": time.time() - 3600,
                           "status": "completed"})
    with pytest.raises(SystemExit, match="REFUSED: an earlier start on this printer is uncertain"):
        model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1}, job, jp, 0, False)
    assert uploads(mapped.run) == [] and mapped.scripts == []
    assert [row["phase"] for row in leases()] == ["uncertain"]


# Mapped launch color check (bug 1): compared as colors, refused only on a clear difference.

def launch_mapped_screenless(m, record, printer_hex):
    """Run a screenless mapped launch with slot 1's record and the printer's HEX as given."""
    m.st["filament_slots"]["1"] = dict(record)
    mapped_hex[0] = printer_hex
    jp, g = make_job(m.root, "color")
    job = json.loads(jp.read_text())
    return lambda: model_job.print_mapped_native(g, m.st, {"display_off": 1}, {"0": 1}, job, jp, 0, False)


@pytest.mark.parametrize("record_hex, printer_hex", [
    ("FFFFFF", "FAFAFA"), ("FAFAFA", "FFFFFF"), ("0C6283FF", "0C6283"), ("0C6283", "0C6283FF"),
    ("#FFF", "FFFFFF"), ("#fff", "ffffff"), ("0x0C6283", "0C6283"), ("FF0000,00FF00", "FF0000"),
])
def test_mapped_launch_proceeds_silently_on_the_same_color_in_another_spelling(mapped, capsys, record_hex, printer_hex):
    launch = launch_mapped_screenless(mapped, {"material": "PLA", "hex": record_hex, "hex_source": "spoolman"}, printer_hex)
    launch()
    assert len(uploads(mapped.run)) == 1 and len(mapped.scripts) == 1
    assert "WARNING" not in capsys.readouterr().out


@pytest.mark.parametrize("record, printer_hex, shown", [
    ({"material": "PLA"}, "FFFFFF", "no color code"),  # the record has no hex at all (was a KeyError)
    ({"material": "PLA", "hex": None}, "FFFFFF", "no color code"),  # None (was an AttributeError)
    ({"material": "PLA", "hex": "teal"}, "FFFFFF", "no color code"),
    ({"material": "PLA", "hex": "FFFFFF"}, "", "blank"),  # the printer reports no color
])
def test_mapped_launch_warns_and_proceeds_when_a_color_cannot_be_compared(mapped, capsys, record, printer_hex, shown):
    launch_mapped_screenless(mapped, record, printer_hex)()
    out = capsys.readouterr().out
    assert len(uploads(mapped.run)) == 1 and len(mapped.scripts) == 1
    assert "WARNING: printer slot 1" in out and shown in out and "could not be compared" in out


def test_mapped_launch_warns_naming_both_values_when_the_colors_are_close(mapped, capsys):
    launch_mapped_screenless(mapped, {"material": "PLA", "hex": "161616", "hex_source": "spoolman"}, "000000")()
    out = capsys.readouterr().out
    assert len(uploads(mapped.run)) == 1
    assert "WARNING" in out and "#000000" in out and "#161616" in out and "close but not identical" in out


@pytest.mark.parametrize("record_hex, printer_hex", [("0C6283", "067749"), ("FFFFFF", "000000"), ("FF0000FF", "00FF00")])
def test_mapped_launch_refuses_clearly_different_colors_before_upload(mapped, record_hex, printer_hex):
    launch = launch_mapped_screenless(mapped, {"material": "PLA", "hex": record_hex, "hex_source": "spoolman"}, printer_hex)
    with pytest.raises(SystemExit, match=rf"REFUSED: printer slot 1 color is #{printer_hex} but Spoolman has "
                                         rf"#{record_hex[:6]} there \(T0\). Nothing uploaded or started."):
        launch()
    assert uploads(mapped.run) == [] and mapped.scripts == [] and leases() == []


@pytest.mark.parametrize("source", ["name", "default"])
def test_mapped_launch_warns_instead_of_refusing_over_a_hex_the_system_guessed(mapped, capsys, source):
    launch_mapped_screenless(mapped, {"material": "PLA", "hex": "0C6283", "hex_source": source}, "067749")()
    out = capsys.readouterr().out
    assert len(uploads(mapped.run)) == 1 and len(mapped.scripts) == 1
    assert "WARNING" in out and "#067749" in out and "#0C6283" in out and "worked out from the spool name" in out
