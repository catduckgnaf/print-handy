"""#1147 remainder (and #1153 item 9): printer-control truth, offline.

One section per item. Every printer, Spoolman and chat service here is a fake; nothing opens a socket.
The AD5X/ZMod pins at the end hold the owner's printer path still: the start command sequence, the
pause and cancel sends, and the IFS slot handling stay exactly what they were.
"""
import contextlib
import hashlib
import json
import time
from unittest import mock

import pytest

from print_handy import macros, printers, settings
from print_handy.core import heater_ceiling, launch_state, model_job
from print_handy.hermes import family_event_listener as fel
from print_handy.hermes import print_next, spool_sync

from .test_launch_state import leases, make_job, uploads
from .test_launch_state import mapped, printer  # noqa: F401  (fixtures shared with the lease tests)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- item 2: one heater ceiling

class CeilingAdapter:
    """A printer profile with heater facts; anything else is not a fact."""

    def __init__(self, config=None, **facts):
        self.config = config or {"printer": {"nozzle_type": "hardened_steel"}}
        self._facts = facts

    def fact(self, name):
        return self._facts[name]


def test_item2_the_ceiling_is_the_nozzle_type_lowered_to_the_model_fact():
    assert heater_ceiling.ceiling(CeilingAdapter(max_hotend_c=280), "nozzle") == 280
    # No model fact: the fitted nozzle type's own safe maximum.
    assert heater_ceiling.ceiling(CeilingAdapter(), "nozzle", state={"nozzle": {"type": "brass"}}) == 300
    assert heater_ceiling.ceiling(CeilingAdapter(max_bed_c=100), "bed") == 100
    assert heater_ceiling.ceiling(CeilingAdapter(), "chamber") == heater_ceiling.UNKNOWN_HEATER_CEILING_C


def test_item2_the_ad5x_profile_gives_its_300_c_hotend_ceiling_on_both_firmwares():
    view = settings.current()
    for model in ("flashforge.ad5x", "flashforge_stock.ad5x"):
        adapter = printers.get(model, view)
        assert heater_ceiling.ceiling(adapter, "nozzle", state={"nozzle": {"type": "hardened_steel"}}) == 300
    # Other ZMod models keep no hotend fact (nothing invented for them).
    assert heater_ceiling._fact(printers.get("flashforge.ad5m", view), "max_hotend_c") is None


def test_item2_the_gcode_mechanical_limit_check_uses_the_same_function():
    adapter = CeilingAdapter(max_hotend_c=240, max_bed_c=90)
    state = {"nozzle": {"type": "hardened_steel"}}
    assert model_job._heater_limits(state, adapter) == (
        heater_ceiling.ceiling(adapter, "nozzle", state=state), heater_ceiling.ceiling(adapter, "bed", state=state))


def test_item2_refusal_names_the_heater_value_and_ceiling():
    adapter = CeilingAdapter(max_hotend_c=250)
    assert heater_ceiling.refusal(adapter, "nozzle", 250) is None
    assert heater_ceiling.refusal(adapter, "nozzle", 251) == "the nozzle target 251 C is above this printer's 250 C nozzle ceiling"
    assert heater_ceiling.refusal(adapter, "nozzle", float("nan")) is not None


class MacroTransport:
    """Moonraker for the macro run: one printer-defined macro, an idle printer."""

    def __call__(self, method, path, body=None):
        if path == macros.OBJECTS_LIST_PATH:
            return {"result": {"objects": ["gcode_macro PREHEAT"]}}
        if path == macros.PRINT_STATE_PATH:
            return {"result": {"status": {"print_stats": {"state": "standby"}}}}
        raise AssertionError(f"unexpected {method} {path}")


@pytest.mark.parametrize("arg", ["EXTRUDER_TEMP=251", "BED_TEMP=91", "TEMP=300", "CHAMBER_TEMP=70"])
def test_item2_a_macro_temperature_argument_above_the_ceiling_is_refused(arg):
    adapter = CeilingAdapter(max_hotend_c=250, max_bed_c=90)
    with pytest.raises(macros.MacroRefusal, match="ceiling.*Nothing was sent"):
        macros.prepare_run(MacroTransport(), "PREHEAT", [arg], heater_adapter=adapter)


def test_item2_a_macro_within_the_ceiling_and_its_other_arguments_still_run():
    adapter = CeilingAdapter(max_hotend_c=250, max_bed_c=90)
    prepared = macros.prepare_run(MacroTransport(), "PREHEAT", ["EXTRUDER_TEMP=250", "BED_TEMP=90", "SPEED=900"],
                                  heater_adapter=adapter)
    assert prepared.script == "PREHEAT EXTRUDER_TEMP=250 BED_TEMP=90 SPEED=900"


def test_item2_octoprint_preheat_above_the_ceiling_refuses_before_the_lease(tmp_path, monkeypatch):
    from .test_start_path import FakeAdapter, ScriptedClient

    gcode = tmp_path / "plate_1.gcode"
    gcode.write_text("G1 X1\n")
    client = ScriptedClient(post=[{"state": "standby", "filename": ""}])
    adapter = FakeAdapter("octoprint", client)
    adapter.config = {"printer": {"nozzle_type": "hardened_steel"}}
    adapter.fact = lambda name: {"max_hotend_c": 250.0, "max_bed_c": 100.0}[name]
    monkeypatch.setattr(model_job, "verify_gcode", lambda path: {"offline": True})
    monkeypatch.setattr(model_job, "_verify_routed", lambda *a, **k: None)
    monkeypatch.setattr(model_job, "_refuse_multicolor", lambda text: None)
    monkeypatch.setattr(model_job, "_with_gcode_temperatures",
                        lambda state, text, recorded=None: {**state, "temps_c": {"bed": 60, "nozzle": 260}})
    acquired = []
    monkeypatch.setattr(launch_state, "acquire", lambda *a, **k: acquired.append(a) or mock.Mock())
    with pytest.raises(SystemExit, match="REFUSED: OctoPrint preheat: the nozzle target 260 C is above"):
        model_job._print_octoprint(adapter, {"nozzle": {"type": "hardened_steel"}}, gcode,
                                   {"filament_selection": {"slot": 1}}, None, 25, False)
    assert acquired == [] and client.uploads == [] and client.starts == []


# --------------------------------------------------------------------------- item 4: bed-clear in the primitive

@pytest.mark.bed_clear_unconfirmed
def test_item4_print_now_without_a_bed_clear_approval_refuses_before_upload(printer):
    jp, _g = make_job(printer.root, "a")
    with pytest.raises(SystemExit, match="no bed-clear approval"):
        model_job.print_now(jp)
    assert uploads(printer.run) == [] and leases() == []
    assert "approvals" not in json.loads(jp.read_text())


@pytest.mark.bed_clear_unconfirmed
def test_item4_yolo_does_not_waive_the_bed_clear_approval(printer, monkeypatch):
    monkeypatch.setattr(model_job, "require_mechanical_limits", lambda *a, **k: None)  # not this test's subject
    jp, _g = make_job(printer.root, "a")
    with pytest.raises(SystemExit, match="no bed-clear approval"):
        model_job.print_now(jp, yolo=True)
    assert uploads(printer.run) == []


@pytest.mark.bed_clear_unconfirmed
def test_item4_an_approval_for_another_gcode_does_not_start_this_one(printer):
    jp, _g = make_job(printer.root, "a")
    with pytest.raises(SystemExit, match="no bed-clear approval"):
        model_job.print_now(jp, bed_clear="0" * 64)
    assert uploads(printer.run) == []


@pytest.mark.bed_clear_unconfirmed
def test_item4_the_approval_for_this_exact_gcode_starts_it_once(printer):
    jp, g = make_job(printer.root, "a")
    assert model_job.print_now(jp, bed_clear=sha(g))["started"] is True
    assert len(uploads(printer.run)) == 1
    with pytest.raises(SystemExit, match="already used"):  # still single-use
        model_job.print_now(jp, bed_clear=sha(g))


@pytest.mark.bed_clear_unconfirmed
def test_item4_every_transport_refuses_at_its_approval_step(mapped):
    """The mapped native (AD5X) route checks too, inside the lease and before the upload."""
    jp, g = make_job(mapped.root, "a")
    with pytest.raises(SystemExit, match="no bed-clear approval"):
        model_job.print_mapped_native(g, mapped.st, {"display_off": 0}, {"0": 1},
                                      json.loads(jp.read_text()), jp, 0, False)
    assert uploads(mapped.run) == [] and mapped.scripts == [] and leases() == []
    assert "approvals" not in json.loads(jp.read_text())


def test_item4_print_next_passes_the_confirmed_gcode_sha_to_print_now(tmp_path):
    g = tmp_path / "plate_1.gcode"
    g.write_text("G1 X1\n")
    seen = {}

    async def spawn(*argv, **kwargs):
        seen["argv"] = argv
        raise OSError("not spawned in tests")
    with mock.patch.object(print_next.asyncio, "create_subprocess_exec", spawn):
        import asyncio
        ok, _tail = asyncio.run(print_next.start({"gcode": str(g), "gcode_sha256": sha(g), "plan_id": "p",
                                                  "part_id": "x", "copy": 1},
                                                 idle_check=lambda: None, claims_dir=tmp_path))
    assert ok is False
    argv = list(seen["argv"])
    assert argv[argv.index("print-now") + 1] == str(g)
    assert argv[argv.index("--bed-clear-sha256") + 1] == sha(g)


@pytest.mark.parametrize("argv, want", [(["print-now", "x.gcode"], None),
                                        (["print-now", "x.gcode", "--bed-clear-confirmed"], True),
                                        (["print-now", "x.gcode", "--bed-clear-sha256", "ab" * 32], "ab" * 32)])
def test_item4_the_cli_passes_the_operators_confirmation_through(monkeypatch, argv, want):
    seen = {}
    monkeypatch.setattr(model_job, "_print_now_cli", lambda target, **kw: seen.update(kw))
    monkeypatch.setattr(model_job.sys, "argv", ["model_job", *argv])
    model_job.main()
    assert seen.get("bed_clear") == want


# --------------------------------------------------------------------------- item 5: clock sync is ZMod-only

def test_item5_a_stock_klipper_printer_gets_no_date_set(monkeypatch):
    sent = []
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: sent.append(a) or (_ for _ in ()).throw(
        AssertionError("no request")))
    klipper = printers.get("klipper.generic", settings.current())
    assert launch_state.clock_sync_supported(klipper) is False
    assert launch_state.sync_printer_clock("http://p:7125", "America/New_York", adapter=klipper) == launch_state.NOT_ZMOD
    assert sent == []


def test_item5_the_ad5x_on_zmod_still_syncs():
    assert launch_state.clock_sync_supported(printers.get("flashforge.ad5x", settings.current())) is True
    # A stock-firmware AD5X has no Moonraker and no DATE_SET either.
    assert launch_state.clock_sync_supported(printers.get("flashforge_stock.ad5x", settings.current())) is False


def test_item5_the_listener_passes_its_own_printer_to_the_clock_sync(monkeypatch):
    from print_handy.hermes import moonraker_event_listener as mel
    seen = {}
    monkeypatch.setattr(mel.launch_state, "sync_printer_clock",
                        lambda url, tz, log=None, adapter=None: seen.update(adapter=adapter) or launch_state.NOT_ZMOD)
    klipper = printers.get("klipper.generic", settings.current())
    monkeypatch.setattr(mel.Listener, "_clock_adapter", staticmethod(lambda: klipper))
    logged = []
    monkeypatch.setattr(mel, "log", logged.append)
    holder = mock.Mock(clock_sync_retry_at=0.0, _clock_adapter=lambda: klipper)
    mel.Listener._sync_printer_clock(holder, "poll")
    assert seen["adapter"] is klipper and logged == [] and holder.clock_sync_retry_at == 0.0


# --------------------------------------------------------------------------- item 6: OctoPrint / Prusa leases settle

def uncertain_lease(printer_id, remote):
    lease = launch_state.acquire(printer_id, job_json=None, gcode_sha256="0" * 64, remote=remote)
    lease.submitting()
    lease.uncertain("submitted, awaiting confirmation")
    return lease


def listener(connection, reads, printer_id="shelf"):
    it = iter(reads)
    return fel.FamilyListener(connection, lambda: next(it), printer_id=printer_id, claim_fn=lambda f: True,
                              start_poster=lambda *a: None, pause_poster=lambda *a: None,
                              terminal_poster=lambda *a: None, retirer=lambda *a: None,
                              beat=lambda **k: None, state_path="unused-family-state.json")


@pytest.mark.parametrize("connection", ["octoprint", "prusalink"])
def test_item6_an_uncertain_start_settles_when_the_printer_runs_its_file(connection, tmp_path):
    uncertain_lease("shelf", "plate_1_abc.gcode")
    lst = listener(connection, [{"state": "printing", "filename": "plate_1_abc.gcode"}])
    lst._state_path = tmp_path / "state.json"
    lst.poll_once()
    assert launch_state.held("shelf") is None


@pytest.mark.parametrize("connection", ["octoprint", "prusalink"])
def test_item6_an_uncertain_start_is_released_after_the_printer_stays_idle_and_cold(connection, tmp_path):
    uncertain_lease("shelf", "plate_1_abc.gcode")
    lst = listener(connection, [])
    lst._state_path = tmp_path / "state.json"
    cold = {"state": "standby", "filename": "", "hotend_target_c": 0, "bed_target_c": 0}
    assert lst._settle_lease("standby", "", cold, now=1000.0) is None
    assert lst._settle_lease("standby", "", cold, now=1030.0) is None  # inside the cold window
    assert launch_state.held("shelf") is not None
    assert lst._settle_lease("standby", "", cold, now=1061.0) == "not_started"
    assert launch_state.held("shelf") is None


@pytest.mark.parametrize("connection", ["octoprint", "prusalink"])
def test_item6_heat_or_another_file_never_settles_the_lease(connection, tmp_path):
    uncertain_lease("shelf", "plate_1_abc.gcode")
    lst = listener(connection, [])
    lst._state_path = tmp_path / "state.json"
    warm = {"hotend_target_c": 210, "bed_target_c": 60}
    for now in (1000.0, 1100.0, 1200.0):  # heating for this start: preparing, not abandoned
        assert lst._settle_lease("standby", "", warm, now=now) is None
    assert lst._settle_lease("printing", "someone-elses.gcode", warm, now=1300.0) is None
    cold = {"hotend_target_c": 0, "bed_target_c": 0}
    for now in (1400.0, 1500.0):  # an earlier file shown idle is not this start's story
        assert lst._settle_lease("complete", "earlier.gcode", cold, now=now) is None
    assert launch_state.held("shelf")["phase"] == "uncertain"


@pytest.mark.parametrize("connection", ["octoprint", "prusalink", "elegoo_sdcp", "flashforge_stock"])
@pytest.mark.parametrize("ended", ["complete", "cancelled", "error", "standby"])
def test_item6_a_lease_whose_own_file_is_seen_ended_is_left_uncertain(connection, ended, tmp_path):
    # First read after the print finished: the lease's own file ended, heaters
    # cold. It may have printed unseen, so it is not released as not started,
    # and the same file finished is not "started" (a reprint's previous run).
    uncertain_lease("shelf", "plate_1_abc.gcode")
    lst = listener(connection, [])
    lst._state_path = tmp_path / "state.json"
    cold = {"hotend_target_c": 0, "bed_target_c": 0}
    for now in (1000.0, 1030.0, 1061.0, 1200.0):
        assert lst._settle_lease(ended, "plate_1_abc.gcode", cold, now=now) is None
    for now in (1300.0, 1400.0, 1500.0):  # later idle reads with no file: still not evidence
        assert lst._settle_lease("standby", "", cold, now=now) is None
    assert launch_state.held("shelf")["phase"] == "uncertain"


def test_item6_a_printer_that_never_launched_gets_no_database(tmp_path, monkeypatch):
    monkeypatch.delenv("PRINT_HANDY_LAUNCH_DB")
    monkeypatch.setattr(settings, "printer_state_dir", lambda printer_id=None, data=None: tmp_path / "p")
    assert launch_state.held("shelf") is None
    assert not (tmp_path / "p" / "jobs.db").exists()


# --------------------------------------------------------------------------- item 7: no record lock over Spoolman

class SlowSpoolman:
    """A Spoolman that answers slowly and checks the hardware-record lock is free on every call."""

    def __init__(self, held, rows):
        self.held, self.rows, self.calls = held, rows, []

    def __call__(self, method, path, body=None):
        assert not self.held, f"hardware lock held across Spoolman {method} {path}"
        self.calls.append((method, path))
        time.sleep(0.01)
        if path.startswith("/api/v1/field/spool") and method == "GET":
            return [{"key": spool_sync.PH_SLOT_FIELD}]
        if path.startswith("/api/v1/spool?"):
            return self.rows
        return {}


def _tracked_lock(held):
    @contextlib.contextmanager
    def hardware_lock(printer_id=None, path=None, timeout=30.0):
        held.append(path)
        try:
            yield
        finally:
            held.pop()
    return hardware_lock


@pytest.mark.parametrize("migrated", [False, True])
def test_item7_the_hardware_lock_is_never_held_across_a_spoolman_call(tmp_path, monkeypatch, migrated):
    from print_handy.core import printer_state
    held = []
    monkeypatch.setattr(printer_state, "hardware_lock", _tracked_lock(held))
    monkeypatch.setattr(spool_sync, "_reconcile_lock", lambda pid: None)
    monkeypatch.setattr(spool_sync, "push_screen", lambda *a, **k: "unsupported")
    monkeypatch.setattr(spool_sync, "repair_usage_spool", lambda **k: "Filament charging: checked")
    monkeypatch.setattr(spool_sync, "_printer_adapter", lambda pid: None)
    state_path = tmp_path / "hw.json"
    record = {"filament_slots": {"1": {"spoolman_id": 7, "material": "PLA"}}}
    if migrated:
        record["ph_slot_migrated"] = True
    state_path.write_text(json.dumps(record))
    rows = [{"id": 7, "filament": {"material": "PLA", "name": "PLA"},
             "extra": {spool_sync.PH_SLOT_FIELD: json.dumps(spool_sync.format_ph_slot("p1", 1))} if migrated else {}},
            {"id": 9, "archived": True, "filament": {"material": "PLA"},
             "extra": {spool_sync.PH_SLOT_FIELD: json.dumps(spool_sync.format_ph_slot("p1", 2))}}]
    client = SlowSpoolman(held, rows)
    out = spool_sync.reconcile("p1", client=client, mr=lambda *a, **k: {}, state_path=state_path,
                               pending_path=tmp_path / "pending.json")
    assert out["ok"] is True, out
    assert any(m == "PATCH" for m, _ in client.calls)  # the writes did go out, unlocked
    assert json.loads(state_path.read_text())["ph_slot_migrated"] is True


def test_item7_a_slow_spoolman_does_not_block_a_launch_waiting_for_the_record(tmp_path, monkeypatch):
    """Real record lock (fcntl), real threads: a writer gets the record while Spoolman is still slow."""
    pytest.importorskip("fcntl")
    import threading

    from print_handy.core import printer_state
    if not hasattr(printer_state, "hardware_lock"):
        pytest.skip("no hardware lock")
    monkeypatch.setattr(spool_sync, "_reconcile_lock", lambda pid: None)
    monkeypatch.setattr(spool_sync, "push_screen", lambda *a, **k: "unsupported")
    monkeypatch.setattr(spool_sync, "repair_usage_spool", lambda **k: "ok")
    monkeypatch.setattr(spool_sync, "_printer_adapter", lambda pid: None)
    state_path = tmp_path / "hw.json"
    state_path.write_text(json.dumps({"filament_slots": {}, "ph_slot_migrated": True}))
    in_spoolman, release = threading.Event(), threading.Event()

    def client(method, path, body=None):
        if path.startswith("/api/v1/spool?"):
            in_spoolman.set()
            release.wait(5)
            return []
        return [{"key": spool_sync.PH_SLOT_FIELD}]
    worker = threading.Thread(target=spool_sync.reconcile, args=("p1",),
                              kwargs=dict(client=client, mr=lambda *a, **k: {}, state_path=state_path,
                                          pending_path=tmp_path / "pending.json"))
    worker.start()
    assert in_spoolman.wait(5)
    took = time.monotonic()
    with printer_state.hardware_lock(path=state_path, timeout=2):
        took = time.monotonic() - took
    release.set()
    worker.join(5)
    assert took < 1.0


# --------------------------------------------------------------------------- item 9 (#1153): no duplicate start reads

def _count_reads(mapped, display_off):
    original = model_job._mr.side_effect
    reads = {"status": 0, "detail": 0}
    at_upload = {}

    def mr(st, path, data=None, timeout=30):
        if "zmod_color" in path and "print_stats" in path:
            reads["status"] += 1
        return original(st, path, data=data, timeout=timeout)
    model_job._mr.side_effect = mr
    model_job.read_stock_detail.side_effect = lambda st: reads.__setitem__("detail", reads["detail"] + 1) or {}
    # The real idle guard reads both sources each time it is asked (the fixture's mock does not).
    mapped.native.require_idle_settled.side_effect = lambda read_moonraker, read_native: bool(
        read_moonraker() and read_native() is not None)
    real_run = mapped.run.side_effect

    def run(cmd, *args, **kwargs):
        if len(cmd) >= 4 and cmd[1] == "upload":
            at_upload.update(reads)
        return real_run(cmd, *args, **kwargs)
    mapped.run.side_effect = run
    jp, g = make_job(mapped.root, f"reads-{display_off}")
    model_job.print_mapped_native(g, mapped.st, {"display_off": display_off}, {"0": 1},
                                  json.loads(jp.read_text()), jp, 0, False)
    return at_upload


def test_1153_item9_the_mapped_start_reads_the_printer_once_per_check(mapped):
    # One status read for the preflight and its idle check, one fresh idle read inside the lease.
    assert _count_reads(mapped, 0) == {"status": 2, "detail": 2}


def test_1153_item9_screenless_start_reads_status_twice_and_never_the_stock_detail(mapped):
    assert _count_reads(mapped, 1) == {"status": 2, "detail": 0}


def test_1153_item9_the_idle_check_inside_the_lease_still_reads_the_printer_fresh(mapped):
    """The saved read only serves the first check: a printer that turned busy is still caught."""
    original = model_job._mr.side_effect
    count = {"n": 0}

    def mr(st, path, data=None, timeout=30):
        reply = original(st, path, data=data, timeout=timeout)
        if "zmod_color" in path and "print_stats" in path:
            count["n"] += 1
            if count["n"] >= 2:
                reply["result"]["status"]["print_stats"] = {"state": "printing", "filename": "other.gcode"}
        return reply
    model_job._mr.side_effect = mr
    jp, g = make_job(mapped.root, "busy-later")
    with pytest.raises(SystemExit, match="idle guard failed"):
        model_job.print_mapped_native(g, mapped.st, {"display_off": 1}, {"0": 1},
                                      json.loads(jp.read_text()), jp, 0, False)
    assert uploads(mapped.run) == []


# --------------------------------------------------------------------------- AD5X / ZMod regression pins

def test_ad5x_pin_the_mapped_start_sends_one_upload_then_exactly_one_set_zcolor(mapped):
    jp, g = make_job(mapped.root, "pin")
    model_job.print_mapped_native(g, mapped.st, {"display_off": 0}, {"0": 1},
                                  json.loads(jp.read_text()), jp, 0, False)
    [remote] = uploads(mapped.run)
    assert mapped.scripts == [f"SET_ZCOLOR FILENAME={remote} SILENT=1 AUTO_ASSIGN=0 LEVELING=0 T0=1"]


def test_ad5x_pin_the_ifs_slot_mapping_reaches_set_zcolor_and_an_empty_slot_refuses(mapped):
    model_job.read_stock_filament_slots_from_detail.return_value = {1: False}
    jp, g = make_job(mapped.root, "empty-slot")
    with pytest.raises(RuntimeError, match="T0 slot 1 occupancy is unknown or unconfirmed"):
        model_job.print_mapped_native(g, mapped.st, {"display_off": 0}, {"0": 1},
                                      json.loads(jp.read_text()), jp, 0, False)
    assert uploads(mapped.run) == [] and mapped.scripts == []


def test_ad5x_pin_the_zmod_moonraker_start_command_is_unchanged():
    adapter = printers.get("flashforge.ad5x", settings.current())
    assert adapter.moonraker_start_commands("plate_1.gcode") == [
        {"method": "POST", "path": "/printer/print/start", "body": {"filename": "plate_1.gcode"}}]
    assert adapter.native_or_none is not None and adapter.multi_slot is True


def test_ad5x_pin_pause_sends_one_moonraker_pause_and_reads_back():
    from print_handy.hermes import print_alert_buttons as pab
    seq = iter([{"klippy": "ready", "state": "printing", "filename": "f.gcode"},
                {"klippy": "ready", "state": "paused", "filename": "f.gcode"}])
    with mock.patch("print_handy.hermes.print_control.status", side_effect=lambda: next(seq)), \
            mock.patch("print_handy.hermes.print_control._call") as call:
        out = pab.handle_click("pause", {"kind": "visual", "filename": "f.gcode"}, pause_wait_s=2, pause_poll_s=0.01)
    assert out == "Paused. Printer now reports paused."
    call.assert_called_once_with("/printer/print/pause", method="POST", timeout=15)


def test_ad5x_pin_force_restart_ladder_on_the_default_printer_is_unchanged():
    from print_handy.hermes import panic
    calls = []
    with mock.patch("time.sleep", return_value=None):
        res = panic.force_restart(caller=lambda path, **kw: calls.append((path, kw.get("method"))) or {},
                                  status_reader=lambda: {"klippy": "ready", "state": "standby"},
                                  wait_s=1, poll_s=0.01)
    assert res["steps"] == ["emergency_stop", "firmware_restart", "klippy_ready"]
    assert calls == [("/printer/emergency_stop", "POST"), ("/printer/firmware_restart", "POST")]


def test_ad5x_pin_clock_sync_still_sends_date_set_to_an_idle_zmod_printer(monkeypatch):
    sent = []
    skew = iter([3 * 86400.0, 0.0])
    monkeypatch.setattr(launch_state, "printer_clock_skew", lambda base, timeout=6: next(skew))
    monkeypatch.setattr(launch_state.time, "sleep", lambda s: None)

    class Reply:
        def __init__(self, body=b"{}"):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        if "objects/query" in url:
            return Reply(json.dumps({"result": {"status": {"print_stats": {"state": "standby", "filename": ""},
                                                           "virtual_sdcard": {"is_active": False}}}}).encode())
        sent.append(url)
        return Reply()
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    ad5x = printers.get("flashforge.ad5x", settings.current())
    assert launch_state.sync_printer_clock("http://p:7125", "America/New_York", adapter=ad5x, now=1793667044) == "ok"
    assert len(sent) == 1 and "DATE_SET" in sent[0]
