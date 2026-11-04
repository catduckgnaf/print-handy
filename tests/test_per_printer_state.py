"""Per-printer state, records and leases (#334, multi-printer Stage 2b).

The properties pinned here are the stage's acceptance criteria: two printers
hold independent leases in independent databases; an approval consumed for
one printer is invisible to the other; a pre-upgrade state directory is
adopted with a held lease and the hardware record intact; a job record with
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
from print_handy.hermes import maintenance, spool_sync


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


def test_legacy_launch_database_is_adopted_with_a_held_lease(state_root, monkeypatch):
    legacy = state_root / "print_handy_launches.sqlite3"
    monkeypatch.setenv("PRINT_HANDY_LAUNCH_DB", str(legacy))
    launch_state.acquire(DEFAULT_ID, remote="held.gcode")  # held across the "upgrade"
    monkeypatch.delenv("PRINT_HANDY_LAUNCH_DB")
    path = launch_state.db_path(DEFAULT_ID)
    assert path == state_root / "printers" / DEFAULT_ID / "jobs.db"
    assert path.is_file() and legacy.is_file()  # adopted by copy; the legacy file stays
    [row] = launch_state.pending(DEFAULT_ID)
    assert row["remote"] == "held.gcode"
    with pytest.raises(SystemExit, match="in progress"):
        launch_state.acquire(DEFAULT_ID, remote="next.gcode")


def test_legacy_launch_database_is_not_adopted_by_a_second_printer(state_root, monkeypatch):
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
    job = {"title": name, "gcode": str(g)}
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
    legacy = tmp_path / "printer_hardware.json"
    legacy.write_text(json.dumps({"marker": "legacy", "spool": {"spoolman_id": 3}}))
    monkeypatch.setattr(printer_state, "STATE", str(legacy))
    return legacy


def test_hardware_record_adoption_and_isolation(hardware, tmp_path):
    assert printer_state.load(DEFAULT_ID)["marker"] == "legacy"  # adopted read
    with pytest.raises(FileNotFoundError):
        printer_state.load("printer-b")  # a second printer never inherits the singleton record
    printer_state.save({"marker": "b"}, "printer-b")
    assert (tmp_path / "printers" / "printer-b" / "printer_hardware.json").is_file()
    assert printer_state.load("printer-b")["marker"] == "b"
    printer_state.save({"marker": "new"}, DEFAULT_ID)  # new writes go to the per-printer file
    assert printer_state.load(DEFAULT_ID)["marker"] == "new"
    assert json.loads(hardware.read_text())["marker"] == "legacy"  # legacy file left in place
    # Once the default printer owns a record, the unnamed path follows it: a
    # write that landed in the stale legacy copy would never be read back.
    assert printer_state.load()["marker"] == "new"
    printer_state.save({"marker": "unnamed"})
    assert printer_state.load(DEFAULT_ID)["marker"] == "unnamed"
    assert json.loads(hardware.read_text())["marker"] == "legacy"


def test_unnamed_path_is_the_legacy_file_until_the_default_printer_has_a_record(hardware):
    assert printer_state.state_path() == hardware


def test_spool_bind_lands_where_the_slicer_reads(tmp_path, monkeypatch):
    # The 2026-11-04 refusals: a Discord /spool bind (unnamed spool_sync write)
    # updated the legacy record to PLA+ while the slicer read the default
    # printer's own record, still PLA, and refused "slot 3 material".
    legacy = tmp_path / "printer_hardware.json"
    legacy.write_text(json.dumps({"filament_slots": {"3": {"material": "PLA", "spoolman_id": 20}}}))
    monkeypatch.setattr(printer_state, "STATE", str(legacy))
    monkeypatch.setattr(spool_sync, "HARDWARE_STATE", legacy)
    monkeypatch.delenv("PRINTER_HARDWARE_STATE", raising=False)
    monkeypatch.setattr(spool_sync._config, "cfg", lambda: {"printers": [{"id": DEFAULT_ID}]})
    printer_state.save(json.loads(legacy.read_text()), DEFAULT_ID)
    state = spool_sync.load_state()
    state["filament_slots"]["3"]["material"] = "PLA+"
    spool_sync.save_state(state)
    assert printer_state.load(DEFAULT_ID)["filament_slots"]["3"]["material"] == "PLA+"


def test_hardware_record_written_with_the_old_slots_key_is_adopted(hardware):
    # #672: a record written before the rename names the loaded-spool map
    # ifs_slots; loading adopts it under filament_slots, contents intact.
    hardware.write_text(json.dumps({"spool": {}, "ifs_slots": {"1": {"spoolman_id": 7}}}))
    state = printer_state.load()
    assert state["filament_slots"]["1"]["spoolman_id"] == 7
    assert "ifs_slots" not in state


def test_hardware_record_with_both_slots_keys_keeps_the_current_one(hardware):
    hardware.write_text(json.dumps({"spool": {},
                                    "ifs_slots": {"1": {"spoolman_id": 7}},
                                    "filament_slots": {"2": {"spoolman_id": 9}}}))
    state = printer_state.load()
    assert set(state["filament_slots"]) == {"2"}
    assert "ifs_slots" not in state


def test_check_names_the_printer_in_its_facts():
    fake = SimpleNamespace(connection="offline", transport=None,
                           readiness_problems=lambda st, snap: [])
    ok, probs, facts = printer_state.check({"spool": {}}, printer_id="printer-b", adapter=fake)
    assert ok and probs == []
    assert facts["printer_id"] == "printer-b"


# ---------------------------------------------------------------- maintenance

def test_maintenance_record_is_per_printer_with_legacy_adoption(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "PROFILE_STATE", tmp_path / "maintenance.json")
    monkeypatch.setattr(maintenance._config, "state_dir", lambda: tmp_path)
    (tmp_path / "maintenance.json").write_text(json.dumps({"runs": [{"kind": "quick"}]}))
    assert maintenance.load(printer_id=DEFAULT_ID)["runs"] == [{"kind": "quick"}]  # adopted
    assert maintenance.load(printer_id="printer-b") == {"runs": []}
    maintenance.save({"runs": [{"kind": "full"}]}, printer_id="printer-b")
    assert (tmp_path / "printers" / "printer-b" / "maintenance.json").is_file()
    assert maintenance.load()["runs"] == [{"kind": "quick"}]  # the un-keyed path is unchanged


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
    a_record = tmp_path / "printer_hardware.json"
    a_record.write_text(json.dumps({"spool": {}, "filament_slots": {"1": {"spoolman_id": 7}}}))
    b_dir = tmp_path / "printers" / "printer-b"
    b_dir.mkdir(parents=True)
    b_record = b_dir / "printer_hardware.json"
    b_record.write_text(json.dumps({"spool": {}, "filament_slots": {}}))
    with pytest.raises(spool_sync.SpoolError, match="already loaded on the default printer"):
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
