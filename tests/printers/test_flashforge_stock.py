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


def _bays(present):
    return {
        "status": "ready",
        "hasMatlStation": True,
        "matlStationInfo": {
            "slotInfos": [{"slotId": n, "hasFilament": n in present} for n in (1, 2, 3, 4)]
        },
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

    def detail(self):
        return self.details.pop(0)

    def slots_of(self, detail):
        return StockClient.slots_of(detail)

    def upload(self, gcode, remote):
        self.uploads.append(remote)
        return remote

    def print_gcode(self, remote, leveling=True):
        self.started.append(remote)
        self.details.append({
            "status": "printing",
            "printFileName": remote,
            "hasMatlStation": True,
            "matlStationInfo": {"slotInfos": [
                {"slotId": n, "hasFilament": True} for n in (1, 2, 3, 4)
            ]},
        })
        return remote


def _stock_print(monkeypatch, tmp_path, details, *, slot=1, after=None):
    from print_handy.core import launch_state, model_job
    from print_handy.printers.flashforge_stock.adapter import StockFlashforgeAdapter

    events = []
    gcode = tmp_path / "plate.gcode"
    gcode.write_text("G1 X1\n")
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
    monkeypatch.setattr(model_job, "record_start", lambda g, remote: "/records/1")
    if after is not None:
        started = list(fake.started)

        def print_gcode(remote, leveling=True):
            fake.started.append(remote)
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
    """#373: the launched-print monitor is the stock family's completion/timelapse path."""
    from print_handy.hermes import stock_watch

    started = []
    monkeypatch.setattr(stock_watch, "start_monitor",
                        lambda adapter, remote: started.append(remote))
    idle = _bays({1})
    out, fake, _events = _stock_print(monkeypatch, tmp_path, [idle, idle], slot=1)
    assert out["started"] is True
    assert started == [out["file"]]


def test_stock_print_starts_no_monitor_when_the_start_is_unconfirmed(monkeypatch, tmp_path):
    from print_handy.hermes import stock_watch

    started = []
    monkeypatch.setattr(stock_watch, "start_monitor",
                        lambda adapter, remote: started.append(remote))
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
