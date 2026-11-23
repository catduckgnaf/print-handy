"""Per-printer state, records and leases (#334, multi-printer Stage 2b).

The properties pinned here are the stage's acceptance criteria: two printers
hold independent leases in independent databases; an approval consumed for
one printer is invisible to the other; a pre-2.4 state directory (files
still at the state root) is refused with the move that fixes it, never
silently started empty (#1817); a job record with
no printer_id still prints on the configured single printer (the existing
launch-state suites cover that path unchanged); and one spool cannot be
recorded as loaded on two printers.

Offline only: transports are mocked, no printer is contacted.
"""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from print_handy import printers as printer_registry
from print_handy import settings
from print_handy.core import launch_state, model_job, printer_state
from print_handy.runtime import maintenance, spool_sync


def load_local(text, suffix=".yaml"):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td, f"local{suffix}")
        path.write_text(text)
        return settings.load(home=settings.HOME, local=path, environ={})


TWO_PRINTERS = """
discord:
  chat_id: "111"
printers:
  - id: garage-a1
    name: Garage A1
    model: bambulab.a1
    chat_id: "222"
    lan_host: 192.168.1.60
    lan_serial: "00M00A000000000"
  - id: workshop-ad5x
    model: flashforge.ad5x
    moonraker_url: http://192.168.1.61:7125
"""

DEFAULT_ID = settings.default_printer_id()


# ---------------------------------------------------------------- settings

def test_printer_state_dir_is_keyed_by_roster_id():
    root = settings.state_dir()
    assert settings.printer_state_dir("workshop-ad5x") == root / "printers" / "workshop-ad5x"
    assert settings.printer_state_dir() == root / "printers" / DEFAULT_ID


def test_view_for_a_second_entry_projects_its_own_sections():
    data = load_local(TWO_PRINTERS)
    view = settings.view_for("workshop-ad5x", data)
    assert view["printer"]["model"] == "flashforge.ad5x"
    assert view["services"]["moonraker_url"] == "http://192.168.1.61:7125"
    assert view["services"]["moonraker_ws_url"] == "ws://192.168.1.61:7125/websocket"
    # Global sections stay the resolved settings' own, and the roster is intact.
    assert view["paths"] == data["paths"]
    assert [e["id"] for e in view["printers"]] == ["garage-a1", "workshop-ad5x"]
    adapter = printer_registry.get("flashforge.ad5x", view)
    assert adapter.key == "flashforge.ad5x"
    assert adapter.display_name == "Flashforge AD5X"


def test_view_for_the_default_printer_is_the_resolved_settings():
    data = load_local(TWO_PRINTERS)
    assert settings.view_for("garage-a1", data) is data


def test_view_for_rederives_an_underived_timelapse_mode_per_printer():
    # The roster-level mode derives from the FIRST entry's services (the
    # Bambu: no Moonraker, capture off -> "off"); the Klipper entry's own
    # view must re-derive from its own Moonraker URL, or its
    # printer-rendered timelapses stay gated off (P2-261).
    data = load_local(TWO_PRINTERS)
    assert data["timelapse"]["mode"] == "off"
    view = settings.view_for("workshop-ad5x", data)
    assert view["timelapse"]["mode"] == "rendered"
    from print_handy import go2rtc
    assert go2rtc.timelapse_mode(view) == "rendered"
    assert go2rtc.rendered_active(view) is True


def test_view_for_keeps_an_explicit_timelapse_mode_for_every_printer():
    data = load_local("""
timelapse:
  mode: "off"
printers:
  - id: garage-a1
    model: bambulab.a1
    lan_host: 192.168.1.60
    lan_serial: "00M00A000000000"
  - id: workshop-ad5x
    model: flashforge.ad5x
    moonraker_url: http://192.168.1.61:7125
""")
    assert settings.view_for("workshop-ad5x", data)["timelapse"]["mode"] == "off"


def test_stamp_job_printer_records_the_default_id():
    job = {}
    model_job.stamp_job_printer(job)
    assert job["printer_id"] == DEFAULT_ID
    stamped = {"printer_id": "workshop-ad5x"}
    model_job.stamp_job_printer(stamped)
    assert stamped["printer_id"] == "workshop-ad5x"  # an existing identity is never rewritten


# ---------------------------------------------------------------- launch state

@pytest.fixture
def state_root(tmp_path, monkeypatch):
    """Launch databases under a temp state root, with no env override."""
    monkeypatch.delenv("PRINT_HANDY_LAUNCH_DB", raising=False)
    monkeypatch.setattr(settings, "state_dir", lambda data=None: tmp_path)
    monkeypatch.setattr(printer_state, "STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("PRINTER_HARDWARE_STATE", raising=False)
    return tmp_path


def test_two_printers_hold_leases_concurrently_in_separate_databases(state_root):
    a = launch_state.acquire("printer-a", remote="a.gcode")
    b = launch_state.acquire("printer-b", remote="b.gcode")
    assert (state_root / "printers" / "printer-a" / "jobs.db").is_file()
    assert (state_root / "printers" / "printer-b" / "jobs.db").is_file()
    with pytest.raises(SystemExit, match="another launch on this printer is in progress"):
        launch_state.acquire("printer-a", remote="a2.gcode")
    a.release()
    launch_state.acquire("printer-a", remote="a2.gcode").release()
    b.release()
    assert launch_state.pending("printer-a") == []
    assert {row["printer"] for row in launch_state.pending()} == set()


def test_approval_consumption_is_invisible_across_printers(state_root):
    sha = "a" * 64
    with launch_state.transaction("printer-a") as con:
        launch_state.record_consumed(con, "/jobs/a/JOB.json", sha, "bed_clear", "2026-01-01T00:00:00+0000")
    with launch_state.transaction("printer-a") as con:
        assert len(launch_state.consumed(con, "/jobs/a/JOB.json", sha)) == 1
    with launch_state.transaction("printer-b") as con:
        assert launch_state.consumed(con, "/jobs/a/JOB.json", sha) == []


def test_a_root_launch_database_is_refused_with_the_move(state_root, monkeypatch):
    legacy = state_root / "print_handy_launches.sqlite3"
    monkeypatch.setenv("PRINT_HANDY_LAUNCH_DB", str(legacy))
    launch_state.acquire(DEFAULT_ID, remote="held.gcode")  # held across the "upgrade"
    monkeypatch.delenv("PRINT_HANDY_LAUNCH_DB")
    target = state_root / "printers" / DEFAULT_ID / "jobs.db"
    with pytest.raises(RuntimeError, match=f"mv {legacy} {target}"):
        launch_state.db_path(DEFAULT_ID)
    assert not target.exists()  # nothing was created beside it
    target.parent.mkdir(parents=True)
    legacy.rename(target)
    [row] = launch_state.pending(DEFAULT_ID)
    assert row["remote"] == "held.gcode"
    with pytest.raises(SystemExit, match="in progress"):
        launch_state.acquire(DEFAULT_ID, remote="next.gcode")


def test_a_root_launch_database_is_never_a_second_printers(state_root, monkeypatch):
    legacy = state_root / "print_handy_launches.sqlite3"
    monkeypatch.setenv("PRINT_HANDY_LAUNCH_DB", str(legacy))
    launch_state.acquire(DEFAULT_ID, remote="held.gcode").release()
    monkeypatch.delenv("PRINT_HANDY_LAUNCH_DB")
    assert launch_state.db_path("printer-b") == state_root / "printers" / "printer-b" / "jobs.db"
    assert not (state_root / "printers" / "printer-b" / "jobs.db").exists()


# ---------------------------------------------------------------- approvals through model_job

def _make_job(root, name, printer_id=None):
    folder = Path(root) / name
    (folder / "out").mkdir(parents=True)
    g = folder / "out" / "x.gcode"
    g.write_text("; same content on both printers\nSTART_PRINT EXTRUDER_TEMP=220 BED_TEMP=60\n")
    job = {"title": name, "gcode": str(g),
           "printer_model": settings.current()["printer"]["model"]}
    if printer_id:
        job["printer_id"] = printer_id
    jp = folder / "JOB.json"
    jp.write_text(json.dumps(job))
    return jp, g, job


def test_approvals_are_consumed_per_printer_and_never_across(tmp_path, state_root):
    jp_a, g_a, job_a = _make_job(tmp_path, "a", "printer-a")
    jp_b, g_b, job_b = _make_job(tmp_path, "b", "printer-b")
    model_job._consume_approval(jp_a, job_a, g_a)
    model_job._consume_approval(jp_b, job_b, g_b)  # the other printer's approval is its own
    with pytest.raises(SystemExit, match="already used"):
        model_job._consume_approval(jp_a, json.loads(jp_a.read_text()), g_a)
    # Printer A's consumed approval cannot be spent under printer B's identity either:
    # the record is bound to the job, the printer and the artifact, in A's own database.
    with pytest.raises(SystemExit, match="already used"):
        model_job._consume_approval(jp_a, {**job_a, "printer_id": "printer-b"}, g_a)
    with launch_state.transaction("printer-b") as con:
        assert launch_state.consumed(con, str(jp_a.resolve()), model_job.file_sha256(g_a)) == []


# ---------------------------------------------------------------- hardware record

@pytest.fixture
def hardware(tmp_path, monkeypatch):
    """A temp state root; returns the default printer's (not yet written) record path."""
    monkeypatch.setattr(printer_state, "STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("PRINTER_HARDWARE_STATE", raising=False)
    return tmp_path / "printers" / DEFAULT_ID / "printer_hardware.json"


def test_hardware_records_are_per_printer_for_every_printer(hardware, tmp_path):
    assert printer_state.state_path() == hardware  # no printer named: the default's own file
    assert printer_state.state_path(DEFAULT_ID) == hardware
    with pytest.raises(FileNotFoundError):
        printer_state.load("printer-b")
    printer_state.save({"marker": "b"}, "printer-b")
    assert (tmp_path / "printers" / "printer-b" / "printer_hardware.json").is_file()
    assert printer_state.load("printer-b")["marker"] == "b"
    printer_state.save({"marker": "unnamed"})
    assert json.loads(hardware.read_text())["marker"] == "unnamed"
    assert printer_state.load(DEFAULT_ID)["marker"] == "unnamed"
    assert not (tmp_path / "printer_hardware.json").exists()  # nothing at the state root


def test_a_root_hardware_record_is_refused_with_the_move(hardware, tmp_path):
    # #1817: the pre-2.4 root record is no longer adopted. While it is the
    # default printer's only record, a load refuses and names the move.
    root = tmp_path / "printer_hardware.json"
    root.write_text(json.dumps({"marker": "root", "spool": {"spoolman_id": 3}}))
    with pytest.raises(RuntimeError, match=f"mv {root} {hardware}"):
        printer_state.load()
    with pytest.raises(RuntimeError, match="no longer read"):
        printer_state.provision_default_record()  # never shadowed by an empty seed
    with pytest.raises(FileNotFoundError):
        printer_state.load("printer-b")  # a second printer never inherited the root record
    hardware.parent.mkdir(parents=True)
    root.rename(hardware)
    assert printer_state.load()["marker"] == "root"


def test_a_stale_root_record_beside_the_per_printer_one_is_ignored(hardware, tmp_path):
    (tmp_path / "printer_hardware.json").write_text(json.dumps({"marker": "stale"}))
    printer_state.save({"marker": "live"}, DEFAULT_ID)
    assert printer_state.load()["marker"] == "live"
    from print_handy import state_layout
    [entry] = state_layout.root_files(tmp_path, DEFAULT_ID)
    assert entry.stale and entry.required


def test_spool_bind_lands_where_the_slicer_reads(hardware):
    # The 2026-11-04 refusals: a Discord /spool bind (unnamed spool_sync write)
    # updated one record while the slicer read another. One resolver now.
    printer_state.save({"filament_slots": {"3": {"material": "PLA", "spoolman_id": 20}}}, DEFAULT_ID)
    state = spool_sync.load_state()
    state["filament_slots"]["3"]["material"] = "PLA+"
    spool_sync.save_state(state)
    assert printer_state.load(DEFAULT_ID)["filament_slots"]["3"]["material"] == "PLA+"


def test_check_names_the_printer_in_its_facts():
    fake = SimpleNamespace(connection="offline", transport=None,
                           readiness_problems=lambda st, snap: [])
    ok, probs, facts = printer_state.check({"spool": {}}, printer_id="printer-b", adapter=fake)
    assert ok and probs == []
    assert facts["printer_id"] == "printer-b"


# ---------------------------------------------------------------- maintenance

def test_maintenance_record_is_per_printer_for_every_printer(tmp_path, monkeypatch):
    monkeypatch.delenv("PRINTER_MAINTENANCE_STATE", raising=False)
    monkeypatch.setattr(settings, "state_dir", lambda data=None: tmp_path)
    maintenance.save({"runs": [{"kind": "full"}]}, printer_id="printer-b")
    assert (tmp_path / "printers" / "printer-b" / "maintenance.json").is_file()
    assert maintenance.load(printer_id=DEFAULT_ID) == {"runs": []}
    maintenance.save({"runs": [{"kind": "quick"}]})  # unnamed: the default printer's own file
    assert (tmp_path / "printers" / DEFAULT_ID / "maintenance.json").is_file()
    assert maintenance.load(printer_id=DEFAULT_ID)["runs"] == [{"kind": "quick"}]


def test_a_root_maintenance_record_is_refused_with_the_move(tmp_path, monkeypatch):
    # #1817: reading the default printer's missing file as empty would reset
    # its maintenance hours while the real record sits unread at the root.
    monkeypatch.delenv("PRINTER_MAINTENANCE_STATE", raising=False)
    monkeypatch.setattr(settings, "state_dir", lambda data=None: tmp_path)
    root = tmp_path / "maintenance.json"
    root.write_text(json.dumps({"runs": [{"kind": "quick"}]}))
    target = tmp_path / "printers" / DEFAULT_ID / "maintenance.json"
    with pytest.raises(RuntimeError, match=f"mv {root} {target}"):
        maintenance.load()
    assert maintenance.load(printer_id="printer-b") == {"runs": []}
    target.parent.mkdir(parents=True)
    root.rename(target)
    assert maintenance.load()["runs"] == [{"kind": "quick"}]


# ---------------------------------------------------------------- spool uniqueness

def _spool_client(method, path, body=None):
    if path.startswith("/api/v1/spool/"):
        sid = int(path.rsplit("/", 1)[1])
        return {"id": sid, "filament": {"name": "Red PLA", "material": "PLA", "density": 1.24,
                                        "diameter": 1.75, "color_hex": "FF0000"},
                "remaining_weight": 500}
    return {"result": "ok"}


_MR = lambda method, path, body=None: {"result": "ok"}


def test_the_same_spool_cannot_be_loaded_on_two_printers(tmp_path):
    a_record = tmp_path / "printers" / "printer-a" / "printer_hardware.json"
    a_record.parent.mkdir(parents=True)
    a_record.write_text(json.dumps({"spool": {}, "filament_slots": {"1": {"spoolman_id": 7}}}))
    b_dir = tmp_path / "printers" / "printer-b"
    b_dir.mkdir(parents=True)
    b_record = b_dir / "printer_hardware.json"
    b_record.write_text(json.dumps({"spool": {}, "filament_slots": {}}))
    with pytest.raises(spool_sync.SpoolError, match="already loaded on printer-a"):
        spool_sync.bind_slot(1, 7, client=_spool_client, mr=_MR, state_path=b_record,
                             push_screen_update=False)
    out = spool_sync.bind_slot(1, 9, client=_spool_client, mr=_MR, state_path=b_record,
                               push_screen_update=False)
    assert out["spool_id"] == 9
    assert json.loads(b_record.read_text())["filament_slots"]["1"]["spoolman_id"] == 9
    # Rebinding a spool on the printer that already loads it is not a cross-printer conflict.
    out = spool_sync.bind_slot(1, 7, client=_spool_client, mr=_MR, state_path=a_record,
                               push_screen_update=False)
    assert out["spool_id"] == 7


# ---------------------------------------------------------------- print path keying

def _upload_ok(cmd, *args, **kwargs):
    if len(cmd) >= 4 and cmd[1] == "upload":
        return mock.Mock(returncode=0, stdout=json.dumps({"result": {"item": {"root": "gcodes", "path": cmd[3]}}}), stderr="")
    return mock.Mock(returncode=0, stdout="", stderr="")


@pytest.fixture
def moonraker(tmp_path):
    patches = [mock.patch.object(model_job, "START_CLAIMS", tmp_path / "claims.json"),
               mock.patch.object(model_job.printer_state, "load", return_value={"moonraker_url": "http://m"}),
               mock.patch.object(model_job, "_printer_phase", return_value=("standby", "", "Ready")),
               mock.patch.object(model_job, "verify_gcode"),
               mock.patch.object(model_job, "_verify_filament_readiness"),
               mock.patch.object(model_job, "_mr", return_value={"result": "ok"}),
               mock.patch.object(model_job, "_wait_printing", return_value="printing"),
               mock.patch.object(model_job, "record_start", return_value="/vault/x.md"),
               mock.patch.object(model_job, "live_snapshot", return_value={"nozzle_c": None, "bed_c": None, "slots": "unknown"}),
               mock.patch.object(model_job, "mesh_readiness", return_value={"profile": "auto", "saved_at": None,
                                                                           "print_leveling": 0, "problem": None,
                                                                           "stale": None, "z_offset_mm": 0.0,
                                                                           "offset_source": "saved gcode offset"}),
               mock.patch.object(model_job, "screen_z_offset", return_value=0.0),
               mock.patch.object(model_job, "_announce_print_name"),
               mock.patch.object(model_job, "_publish_job_card"),
               mock.patch.object(model_job.subprocess, "run", side_effect=_upload_ok)]
    for p in patches:
        p.start()
    yield tmp_path
    for p in reversed(patches):
        p.stop()


def test_a_lease_on_another_printer_does_not_block_this_print(moonraker):
    jp, _g, _job = _make_job(moonraker, "a", DEFAULT_ID)
    other = launch_state.acquire("printer-b", remote="other.gcode")
    try:
        assert model_job.print_now(jp)["started"] is True
    finally:
        other.release()


def test_a_lease_on_the_jobs_own_printer_refuses_the_print(moonraker):
    jp, _g, _job = _make_job(moonraker, "a", DEFAULT_ID)
    held = launch_state.acquire(DEFAULT_ID, remote="held.gcode")
    try:
        with pytest.raises(SystemExit, match="another launch on this printer is in progress"):
            model_job.print_now(jp)
        assert "approvals" not in json.loads(jp.read_text())  # a refused lease consumes nothing
    finally:
        held.release()


def test_upload_goes_to_the_jobs_printer_not_the_global_url(moonraker, monkeypatch):
    # bin/moonrakerctl reads only MOONRAKER_URL / services.moonraker_url; the
    # start goes to the job's printer (st), so the upload must be aimed there too.
    monkeypatch.setenv("MOONRAKER_URL", "http://global-default:7125")
    jp, _g, _job = _make_job(moonraker, "a", DEFAULT_ID)
    with mock.patch.object(model_job.printer_state, "load",
                           return_value={"moonraker_url": "http://printer-b:7125/"}):
        assert model_job.print_now(jp)["started"] is True
    uploads = [c for c in model_job.subprocess.run.call_args_list
               if len(c.args[0]) >= 2 and c.args[0][1] == "upload"]
    assert uploads and all(c.kwargs["env"]["MOONRAKER_URL"] == "http://printer-b:7125" for c in uploads)


def test_moonrakerctl_env_keeps_the_environment_without_a_printer_url(monkeypatch):
    monkeypatch.setenv("MOONRAKER_URL", "http://global-default:7125")
    assert model_job._moonrakerctl_env({})["MOONRAKER_URL"] == "http://global-default:7125"
    assert model_job._moonrakerctl_env({"moonraker_url": "http://b"})["MOONRAKER_URL"] == "http://b"


# ------------------------------------------- crash/concurrency integrity (audit P0)

def test_hardware_save_uses_unique_temp_files_and_leaves_none_behind(state_root):
    import threading
    names = []
    real_replace = printer_state.os.replace

    def spy(src, dst):
        names.append(str(src))
        return real_replace(src, dst)

    with mock.patch.object(printer_state.os, "replace", spy):
        threads = [threading.Thread(target=printer_state.save, args=({"n": i}, DEFAULT_ID))
                   for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
    assert len(set(names)) == len(names) == 8  # no shared "<file>.tmp"
    folder = printer_state.state_path(DEFAULT_ID).parent
    assert not list(folder.glob("*.tmp"))
    assert json.loads(printer_state.state_path(DEFAULT_ID).read_text())["n"] in range(8)


def test_failed_hardware_save_keeps_the_old_record_and_removes_its_temp(state_root):
    printer_state.save({"n": "old"}, DEFAULT_ID)
    with mock.patch.object(printer_state.json, "dump", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            printer_state.save({"n": "new"}, DEFAULT_ID)
    folder = printer_state.state_path(DEFAULT_ID).parent
    assert json.loads(printer_state.state_path(DEFAULT_ID).read_text()) == {"n": "old"}
    assert not list(folder.glob("*.tmp"))


# --- hardware record read-modify-write lock (single-source plan) -------------

def _lock_worker(root, n):
    import json, sys
    from pathlib import Path
    from print_handy.core import printer_state as ps
    ps.STATE_ROOT = root
    for _ in range(n):
        ps.update(lambda st: st.__setitem__("count", st.get("count", 0) + 1), "p1")


def test_concurrent_processes_do_not_lose_updates(tmp_path, monkeypatch):
    import multiprocessing as mp, json
    from print_handy.core import printer_state as ps
    monkeypatch.setattr(ps, "STATE_ROOT", tmp_path)
    monkeypatch.delenv("PRINTER_HARDWARE_STATE", raising=False)
    ps.save({"count": 0}, "p1")
    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=_lock_worker, args=(str(tmp_path), 25)) for _ in range(4)]
    [p.start() for p in procs]
    [p.join(60) for p in procs]
    assert all(p.exitcode == 0 for p in procs)
    assert ps.load("p1")["count"] == 100


def test_hardware_lock_is_reentrant_and_times_out(tmp_path, monkeypatch):
    import fcntl, os, pytest
    from print_handy.core import printer_state as ps
    monkeypatch.setattr(ps, "STATE_ROOT", tmp_path)
    monkeypatch.delenv("PRINTER_HARDWARE_STATE", raising=False)
    with ps.hardware_lock("p1"):
        with ps.hardware_lock("p1"):
            pass
    target = ps.state_path("p1")
    fd = os.open(str(target) + ".lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(TimeoutError):
            with ps.hardware_lock("p1", timeout=0.2):
                pass
    finally:
        os.close(fd)


