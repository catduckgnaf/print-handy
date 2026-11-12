"""Stock Flashforge LAN client, offline. The transport is a fake; nothing opens a socket."""
import json

import pytest

from print_handy.printers.flashforge_stock.client import (
    BUSY_CODE,
    STOCK_PORT,
    StockClient,
    StockLanError,
)


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("AD5X_SERIAL", "SN-TEST")
    monkeypatch.setenv("AD5X_CHECK_CODE", "code-test")


def scripted(replies):
    seen = []

    def post(url, body, content_type):
        seen.append((url, body, content_type))
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    return post, seen


def test_detail_posts_credentials_and_returns_the_object(creds):
    post, seen = scripted([{"code": 0, "detail": {"status": "ready"}}])
    detail = StockClient("192.0.2.10", post=post).detail()
    assert detail == {"status": "ready"}
    url, body, content_type = seen[0]
    assert url == f"http://192.0.2.10:{STOCK_PORT}/detail"
    assert body == {"serialNumber": "SN-TEST", "checkCode": "code-test"}
    assert content_type == "application/json"


def test_detail_refuses_a_nonzero_code(creds):
    post, _seen = scripted([{"code": BUSY_CODE, "message": "printing"}])
    with pytest.raises(StockLanError, match="refused"):
        StockClient("192.0.2.10", post=post).detail()


def test_upload_sends_print_false_and_returns_the_same_name(creds, tmp_path):
    gcode = tmp_path / "plate.gcode"
    gcode.write_bytes(b"G1 X1\n")
    post, seen = scripted([{"code": 0, "print": False, "fileName": "plate.gcode"}])
    name = StockClient("printer.local", post=post).upload(gcode, "plate.gcode")
    assert name == "plate.gcode"
    url, body, content_type = seen[0]
    assert url.endswith("/uploadGcode")
    assert content_type.startswith("multipart/form-data; boundary=")
    text = body.decode()
    assert 'name="print"' in text and "false" in text
    assert 'filename="plate.gcode"' in text
    assert "code-test" in text  # the firmware requires it in the body
    assert "code-test" not in url  # and never in the URL


def test_upload_rejects_a_reply_that_says_it_started_printing(creds, tmp_path):
    gcode = tmp_path / "plate.gcode"
    gcode.write_bytes(b"G1 X1\n")
    post, _seen = scripted([{"code": 0, "print": True, "fileName": "plate.gcode"}])
    with pytest.raises(StockLanError, match="print enabled"):
        StockClient("printer.local", post=post).upload(gcode, "plate.gcode")


def test_print_gcode_names_the_uploaded_file(creds):
    post, seen = scripted([{"code": 0}])
    name = StockClient("192.0.2.10", post=post).print_gcode("plate.gcode", leveling=True)
    assert name == "plate.gcode"
    body = seen[0][1]
    assert body["fileName"] == "plate.gcode" and body["levelingBeforePrint"] is True
    assert body["serialNumber"] == "SN-TEST"


@pytest.mark.parametrize("filename", ["../plate.gcode", "plate.3mf", "a/b.gcode", 'say "hi".gcode'])
def test_upload_refuses_a_filename_that_is_not_one_gcode_file(creds, tmp_path, filename):
    gcode = tmp_path / "plate.gcode"
    gcode.write_bytes(b"G1 X1\n")
    post, seen = scripted([])
    with pytest.raises(StockLanError, match="filename"):
        StockClient("192.0.2.10", post=post).upload(gcode, filename)
    assert seen == []


def test_missing_credentials_fail_before_any_request(monkeypatch):
    monkeypatch.delenv("AD5X_SERIAL", raising=False)
    monkeypatch.delenv("AD5X_CHECK_CODE", raising=False)
    post, seen = scripted([])
    with pytest.raises(StockLanError, match="credentials"):
        StockClient("192.0.2.10", post=post).detail()
    assert seen == []


def test_a_host_with_a_scheme_is_refused(creds):
    post, seen = scripted([])
    with pytest.raises(StockLanError, match="host"):
        StockClient("http://192.0.2.10", post=post).detail()
    assert seen == []


def test_status_of_maps_the_documented_states():
    assert StockClient.status_of({"status": "ready"})["state"] == "standby"
    assert StockClient.status_of({"status": "completed", "printFileName": "a.gcode"})["state"] == "complete"
    assert StockClient.status_of({"status": "busy"})["state"] == "busy"
    assert StockClient.status_of({"status": "printing", "printProgress": 40})["progress"] == 40
    assert StockClient.status_of({"status": "something-new"})["state"] == "unknown"


def test_a_refused_request_does_not_leak_the_check_code(creds):
    post, _seen = scripted([json.JSONDecodeError("bad", "code-test", 0)])
    with pytest.raises(StockLanError) as caught:
        StockClient("192.0.2.10", post=post).detail()
    assert "code-test" not in str(caught.value)
    assert caught.value.__cause__ is None


def _bays(present, *, named=True):
    """/detail with the IFS bays in `present` loaded; a loaded bay reports its material and color
    the way the AD5X endpoint schema documents (MatlSlotInfo), unless named=False."""
    def bay(n):
        info = {"slotId": n, "hasFilament": n in present}
        if named and n in present:
            info.update(materialName="PLA", materialColor="#1A2B3C")
        return info
    return {
        "status": "ready",
        "hasMatlStation": True,
        "matlStationInfo": {"slotInfos": [bay(n) for n in (1, 2, 3, 4)]},
    }


def test_slots_of_reads_four_ifs_bays():
    assert StockClient.slots_of(_bays({1, 3})) == {1: True, 2: False, 3: True, 4: False}


def test_slots_of_refuses_a_missing_station():
    with pytest.raises(StockLanError, match="attached IFS"):
        StockClient.slots_of({"hasMatlStation": False, "matlStationInfo": {}})


def test_heating_and_pausing_are_not_idle():
    assert StockClient.status_of({"status": "heating"})["state"] == "printing"
    assert StockClient.status_of({"status": "pausing"})["state"] == "paused"


class _Lease:
    def __init__(self, events):
        self.events = events

    def submitting(self):
        self.events.append("submitting")

    def uncertain(self, detail):
        self.events.append("uncertain")

    def release(self):
        self.events.append("release")


class _FakeStock:
    """Scripted /detail replies. upload and print_gcode record names and never open a socket."""

    def __init__(self, details):
        self.details = list(details)
        self.uploads = []
        self.started = []
        self.bays = []

    def detail(self):
        return self.details.pop(0)

    def slots_of(self, detail):
        return StockClient.slots_of(detail)

    def upload(self, gcode, remote):
        self.uploads.append(remote)
        return remote

    def print_gcode(self, remote, leveling=True, bay=None):
        self.started.append(remote)
        self.bays.append(bay)
        self.details.append({
            "status": "printing",
            "printFileName": remote,
            "hasMatlStation": True,
            "matlStationInfo": {"slotInfos": [
                {"slotId": n, "hasFilament": True} for n in (1, 2, 3, 4)
            ]},
        })
        return remote


def _stock_print(monkeypatch, tmp_path, details, *, slot=1, after=None, gcode_text="G1 X1\n"):
    from print_handy.core import launch_state, model_job
    from print_handy.printers.flashforge_stock.adapter import StockFlashforgeAdapter

    events = []
    gcode = tmp_path / "plate.gcode"
    gcode.write_text(gcode_text)
    fake = _FakeStock(details)
    adapter = StockFlashforgeAdapter({
        "printer": {"model": "flashforge_stock.ad5x", "filament_slots": 4,
                    "bed_mm": [220.0, 220.0], "max_print_height_mm": 220.0},
        "layout": {"single_copy_edge_mm": 5, "multi_copy_edge_mm": 5},
        "flashforge_stock": {"host": "192.0.2.10"},
    })
    monkeypatch.setattr(adapter, "client", lambda: fake)
    monkeypatch.setattr(model_job, "verify_gcode", lambda path: {"offline": True})
    monkeypatch.setattr(launch_state, "acquire", lambda *a, **kw: events.append("acquire") or _Lease(events))
    monkeypatch.setattr(model_job, "_consume_approval", lambda *a, **kw: events.append("consume"))
    monkeypatch.setattr(model_job, "record_start", lambda g, remote, **kw: "/records/1")
    if after is not None:
        started = list(fake.started)

        def print_gcode(remote, leveling=True, bay=None):
            fake.started.append(remote)
            fake.bays.append(bay)
            fake.details.append(after)
            return remote

        fake.print_gcode = print_gcode
        del started
    job = {"filament_selection": {"slot": slot}}
    out = model_job._print_flashforge_stock(
        adapter, {}, gcode, job, tmp_path / "JOB.json", 25, False)
    return out, fake, events


def test_stock_print_refuses_an_empty_slot_before_upload(monkeypatch, tmp_path):
    from print_handy.core import model_job
    from print_handy.printers.flashforge_stock.adapter import StockFlashforgeAdapter

    gcode = tmp_path / "plate.gcode"
    gcode.write_text("G1 X1\n")
    fake = _FakeStock([_bays({1})])
    adapter = StockFlashforgeAdapter({
        "printer": {"model": "flashforge_stock.ad5x", "filament_slots": 4,
                    "bed_mm": [220.0, 220.0], "max_print_height_mm": 220.0},
        "layout": {"single_copy_edge_mm": 5, "multi_copy_edge_mm": 5},
        "flashforge_stock": {"host": "192.0.2.10"},
    })
    monkeypatch.setattr(adapter, "client", lambda: fake)
    with pytest.raises(SystemExit, match="REFUSED:.*slot 2"):
        model_job._print_flashforge_stock(
            adapter, {}, gcode, {"filament_selection": {"slot": 2}},
            tmp_path / "JOB.json", 25, False)
    assert fake.uploads == [] and fake.started == []


def test_stock_print_refuses_start_print_before_upload(monkeypatch, tmp_path):
    from print_handy.core import model_job
    from print_handy.printers.flashforge_stock.adapter import StockFlashforgeAdapter

    gcode = tmp_path / "plate.gcode"
    gcode.write_text("START_PRINT BED_TEMP=60 EXTRUDER_TEMP=215 MESH=auto\\n")
    fake = _FakeStock([_bays({1})])
    adapter = StockFlashforgeAdapter({
        "printer": {"model": "flashforge_stock.ad5x", "filament_slots": 4,
                    "bed_mm": [220.0, 220.0], "max_print_height_mm": 220.0},
        "layout": {"single_copy_edge_mm": 5, "multi_copy_edge_mm": 5},
        "flashforge_stock": {"host": "192.0.2.10"},
    })
    monkeypatch.setattr(adapter, "client", lambda: fake)
    with pytest.raises(SystemExit, match="REFUSED:.*START_PRINT"):
        model_job._print_flashforge_stock(
            adapter, {}, gcode, {"filament_selection": {"slot": 1}},
            tmp_path / "JOB.json", 25, False)
    assert fake.uploads == [] and fake.started == []


def test_stock_print_starts_only_the_occupied_slot(monkeypatch, tmp_path):
    idle = _bays({2})
    out, fake, events = _stock_print(monkeypatch, tmp_path, [idle, idle], slot=2)
    assert len(fake.uploads) == 1 and fake.started == fake.uploads
    assert out["method"] == "flashforge_stock"
    assert out["started"] is True
    assert out["file"] == fake.started[0]
    assert events[:3] == ["acquire", "consume", "submitting"]


def test_stock_print_starts_the_timelapse_monitor_on_a_confirmed_start(monkeypatch, tmp_path):
    """#373: the launched-print monitor is the stock family's completion/timelapse path.

    Since P1-85 it starts in the detached monitor worker, not on a thread
    in the print-now process; the print path's contract is that it asks
    the worker for a monitor naming the started file."""
    from print_handy.runtime import monitor_worker

    started = []
    monkeypatch.setattr(monitor_worker, "spawn",
                        lambda watch, remote, printer_id=None: started.append(remote))
    idle = _bays({1})
    out, fake, _events = _stock_print(monkeypatch, tmp_path, [idle, idle], slot=1)
    assert out["started"] is True
    assert started == [out["file"]]


def test_stock_print_starts_no_monitor_when_the_start_is_unconfirmed(monkeypatch, tmp_path):
    from print_handy.runtime import monitor_worker

    started = []
    monkeypatch.setattr(monitor_worker, "spawn",
                        lambda watch, remote, printer_id=None: started.append(remote))
    idle = _bays({1})
    after = {"status": "ready", "printFileName": "plate.gcode",
             "hasMatlStation": True,
             "matlStationInfo": {"slotInfos": [
                 {"slotId": n, "hasFilament": True} for n in (1, 2, 3, 4)]}}
    out, _fake, _events = _stock_print(monkeypatch, tmp_path, [idle, idle], slot=1, after=after)
    assert out["started"] is False
    assert started == []


def _stock_adapter():
    from print_handy import printers, settings
    model = "flashforge_stock.ad5x"
    return printers.get(model, settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": model}))


def test_normalize_status_reports_detail_targets():
    detail = {"status": "printing", "printProgress": 0.4, "rightTargetTemp": 220.0, "platTargetTemp": 60}
    for raw in (detail, {"detail": detail}):
        out = _stock_adapter().normalize_status(raw)
        assert (out["hotend_target_c"], out["bed_target_c"]) == (220.0, 60)
        assert (out["state"], out["hotend_c"], out["bed_c"]) == ("printing", None, None)


@pytest.mark.parametrize("raw", [{}, {"status": "ready"}, {"rightTargetTemp": True, "platTargetTemp": "60"}])
def test_normalize_status_targets_are_none_when_absent(raw):
    out = _stock_adapter().normalize_status(raw)
    assert out["hotend_target_c"] is None and out["bed_target_c"] is None


# #211: stock artifacts are verified offline against the record, never through Moonraker.

def _orca_like(toolpath_move):
    return ("G28\nG90\nM83\nG1 X-10 Y-10 Z5 F3000\nG1 E5 F300\n"  # the slicer's start code purges off the bed
            "; MACHINE_START_GCODE_END\n;LAYER_CHANGE\nG1 Z0.2\nG1 X10 Y10\n"
            f"{toolpath_move}\n"
            "; MACHINE_END_GCODE_START\nG1 X0 Y225 Z50\nM104 S0\nM140 S0\n")


def test_stock_verification_holds_the_toolpath_to_the_bed_inside_the_axis_envelope(monkeypatch):
    from print_handy.printers.flashforge_stock import adapter as stock_adapter
    monkeypatch.setattr(stock_adapter, "recorded_job_problems", lambda text, adapter, state: [])
    adapter = _stock_adapter()
    assert adapter.gcode_problems(_orca_like("G1 X100 Y100 E1"), {"temps_c": {}}) == []
    problems = adapter.gcode_problems(_orca_like("G1 X500 Y100 E1"), {"temps_c": {}})
    assert problems and "X500" in problems[0]


def test_stock_verification_runs_the_recorded_job_checks_with_state(monkeypatch):
    from print_handy.printers.flashforge_stock import adapter as stock_adapter
    seen = []
    monkeypatch.setattr(stock_adapter, "recorded_job_problems",
                        lambda text, adapter, state: seen.append(state) or ["nozzle 0.6 != recorded 0.4"])
    problems = _stock_adapter().gcode_problems(_orca_like("G1 X100 Y100 E1"), {"temps_c": {}})
    assert problems == ["nozzle 0.6 != recorded 0.4"] and seen == [{"temps_c": {}}]
    # Without state (the cheap pre-check before /detail) only foreign start code is looked at.
    assert _stock_adapter().gcode_problems("G1 X500 Y500\n") == []


def test_stock_g_code_verification_never_reaches_moonraker(monkeypatch, tmp_path):
    from print_handy import printers
    from print_handy.core import model_job, printer_state
    gcode = tmp_path / "plate.gcode"
    gcode.write_text("G1 X1\n")
    monkeypatch.setattr(printers, "current", _stock_adapter)
    monkeypatch.setattr(printer_state, "load", lambda *a, **kw: {"temps_c": {}})
    monkeypatch.setattr(printer_state, "check", lambda st, *a, **kw: (True, [], {}))
    monkeypatch.setattr(model_job, "_mr", lambda *a, **kw: pytest.fail("Moonraker was queried"))
    monkeypatch.setattr(model_job, "_verify_offline",
                        lambda path, state, adapter=None: {"offline": True, "connection": "flashforge_stock"})
    assert model_job.verify_gcode(gcode)["connection"] == "flashforge_stock"
    assert model_job.verify_gcode_offline(gcode, {"temps_c": {}})["connection"] == "flashforge_stock"


def test_the_printer_check_reads_stock_detail_not_moonraker(monkeypatch):
    from print_handy.core import printer_state
    monkeypatch.setattr(printer_state, "_check_spoolman", lambda *a, **kw: None)
    adapter = _stock_adapter()
    for detail, ok in (({"status": "ready"}, True), ({"status": "printing"}, False)):
        monkeypatch.setattr(adapter, "client", lambda d=detail: _FakeStock([d]))
        result, problems, facts = printer_state.check({"nozzle": {}}, adapter=adapter)  # no moonraker_url
        assert result is ok and facts["connection"] == "flashforge_stock"
        assert problems == ([] if ok else ["printer state is 'printing', not idle"])



# --- #1147: the start request names the selected IFS bay (AD5X /printGcode schema) ---------------

def test_1147_print_gcode_without_a_bay_sends_only_the_file_and_leveling(creds):
    post, seen = scripted([{"code": 0}])
    StockClient("192.0.2.10", post=post).print_gcode("plate.gcode")
    assert "useMatlStation" not in seen[0][1] and "materialMappings" not in seen[0][1]


def test_1147_print_gcode_names_the_bay_for_t0(creds):
    post, seen = scripted([{"code": 0}])
    bay = StockClient.material_mapping(3, material="PETG", tool_color="#ff8800", slot_color="#FF8800FF")
    StockClient("192.0.2.10", post=post).print_gcode("plate.gcode", bay=bay)
    body = seen[0][1]
    assert body["useMatlStation"] is True and body["gcodeToolCnt"] == 1
    assert body["materialMappings"] == [{"toolId": 0, "slotId": 3, "materialName": "PETG",
                                         "toolMaterialColor": "#FF8800", "slotMaterialColor": "#FF8800"}]
    assert body["fileName"] == "plate.gcode" and body["levelingBeforePrint"] is True


@pytest.mark.parametrize("slot, material, color", [(0, "PLA", "#FFFFFF"), (5, "PLA", "#FFFFFF"),
                                                   (1, "", "#FFFFFF"), (1, "PLA", "white"), (1, "PLA", None)])
def test_1147_a_bay_the_schema_cannot_carry_is_refused_before_any_request(slot, material, color):
    with pytest.raises(StockLanError):
        StockClient.material_mapping(slot, material=material, tool_color=color, slot_color=color)


def test_1147_stock_print_sends_the_selected_bay_with_its_detail_material_and_the_gcode_color(monkeypatch, tmp_path):
    idle = _bays({2})
    out, fake, _events = _stock_print(monkeypatch, tmp_path, [idle, idle], slot=2,
                                      gcode_text="; filament_colour = #00FF00\nG1 X1\n")
    assert out["started"] is True
    assert fake.bays == [{"toolId": 0, "slotId": 2, "materialName": "PLA",
                          "toolMaterialColor": "#00FF00", "slotMaterialColor": "#1A2B3C"}]


def test_1147_stock_print_refuses_a_bay_it_cannot_name_before_upload(monkeypatch, tmp_path):
    unnamed = _bays({1}, named=False)
    with pytest.raises(SystemExit, match="REFUSED:.*IFS slot 1 cannot be named"):
        _stock_print(monkeypatch, tmp_path, [unnamed, unnamed], slot=1)
