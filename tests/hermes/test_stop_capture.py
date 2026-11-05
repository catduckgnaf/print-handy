"""Stop-reason capture with a fake Moonraker (2026-11-05). Offline only: nothing reaches a printer."""
import json

import pytest

from print_handy.hermes import print_records as records
from print_handy.hermes import stop_capture

NOW = 1793893797.0  # 2026-11-05T15:49:57Z, the 10:49:57 ET V12 shutdown

PRE_STOP = """partial
Start printer at Thu Nov  5 08:43:10 2026 (1792060990.1 3.3)
MCU 'mcu' config: ADC_MAX=4095 BUS_PINS_i2c1=PB6,PB7
Stats 9000.1: gcodein=0 mcu: mcu_awake=0.004
!! Weight 512.3g > max 400g, Z=0.42: nozzle hit or part detachment
Transition to shutdown state: Weight 512.3g > max 400g, Z=0.42: nozzle hit or part detachment
"""
POST_RESTART = """Start printer at Thu Nov  5 13:27:02 2026 (1792073222.1 12.3)
MCU 'mcu' config: ADC_MAX=4095 BUS_PINS_i2c1=PB6,PB7
Starting heater checks for extruder
webhooks: registering remote method 'shutdown_machine' for connection id: 1
"""


def fake_moonraker(*, klippy="shutdown", message="", log=None, ps_message=""):
    def fetch(path, range_bytes=None):
        if path == "/printer/info":
            return {"result": {"state": klippy, "state_message": message}}
        if path.startswith("/printer/objects/query"):
            return {"result": {"status": {"webhooks": {"state": klippy, "state_message": message},
                                          "print_stats": {"state": "error", "message": ps_message,
                                                          "filename": "v12.gcode"}}}}
        if path == "/server/files/klippy.log":
            if log is None:
                raise OSError("404")
            assert range_bytes == stop_capture.LOG_TAIL_BYTES
            return log
        raise OSError("404")
    return fetch


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(stop_capture, "stops_dir", lambda: tmp_path / "stops")
    monkeypatch.setattr(stop_capture, "_archive_folder", lambda name: tmp_path / "archive" / "job1")
    monkeypatch.setattr(records, "PRINTS_DIR", tmp_path / "Prints")
    return tmp_path


def test_shutdown_reason_comes_from_moonraker_and_lands_in_every_store(dirs):
    record = dirs / "Prints" / "v12.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"gcode_file": "v12.gcode", "started_at": "2026-11-05T14:49:47Z",
                                  "completed_at": "2026-11-05T15:50:30Z"}))
    lines = []
    reason = "MCU 'mcu' shutdown: Timer too close"
    out = stop_capture.capture("v12.gcode", "shutdown", fetch=fake_moonraker(message=reason, log=PRE_STOP),
                               now=NOW, log=lines.append)
    assert out["stop_reason"] == reason
    side = json.loads((dirs / "stops" / "v12.gcode.json").read_text())
    assert side["stop_reason"] == reason and side["first_captured_at"] == "2026-11-05T15:49:57Z"
    assert side["events"][0]["log_tail"][-1].startswith("Transition to shutdown state")
    assert json.loads((dirs / "archive" / "job1" / "STOP.json").read_text())["stop_reason"] == reason
    stored = json.loads(record.read_text())
    assert stored["stop_reason"] == reason and stored["stop_capture"].endswith("v12.gcode.json")
    assert any(reason in line and "shutdown" in line for line in lines)


def test_printer_that_already_restarted_still_yields_the_pre_stop_reason(dirs):
    # Klipper is ready again and its state message is the fresh boot's; only the log
    # session from before the restart knows why the print stopped.
    fetch = fake_moonraker(klippy="ready", message="Printer is ready", log=PRE_STOP + POST_RESTART)
    out = stop_capture.capture("v12.gcode", "cancel", fetch=fetch, now=NOW, log=lambda _: None)
    assert out["stop_reason"] == "Weight 512.3g > max 400g, Z=0.42: nozzle hit or part detachment"
    event = out["events"][-1]
    assert event["log_restarted_after_stop"] is True
    assert not any("registering remote method" in line for line in event["log_tail"])


def test_log_rolled_over_and_nothing_stated_records_no_reason(dirs):
    fetch = fake_moonraker(klippy="ready", message="Printer is ready", log="x\n" + POST_RESTART)
    out = stop_capture.capture("v12.gcode", "cancel", fetch=fetch, now=NOW, log=lambda _: None)
    assert out["stop_reason"] == ""
    assert out["events"][-1]["log_tail"] == []


def test_first_reason_wins_over_a_later_cancel(dirs):
    stop_capture.capture("v12.gcode", "shutdown", fetch=fake_moonraker(message="Timer too close"),
                         now=NOW, log=lambda _: None)
    out = stop_capture.capture("v12.gcode", "cancel", fetch=fake_moonraker(klippy="ready", message="Printer is ready"),
                               now=NOW + 60, log=lambda _: None)
    assert out["stop_reason"] == "Timer too close"
    assert [e["trigger"] for e in out["events"]] == ["shutdown", "cancel"]


def test_moonraker_unreachable_never_raises(dirs):
    def broken(path, range_bytes=None):
        raise OSError("connection refused")
    out = stop_capture.capture("v12.gcode", "error", fetch=broken, now=NOW, log=lambda _: None)
    assert out["stop_reason"] == ""
    assert out["events"][0]["sources"]["printer_info"].startswith("failed")


def test_print_stats_message_is_the_fallback_reason(dirs):
    fetch = fake_moonraker(klippy="ready", message="Printer is ready", ps_message="Must home axis first: 10.000 10.000 0.200")
    out = stop_capture.capture("v12.gcode", "error", fetch=fetch, now=NOW, log=lambda _: None)
    assert out["stop_reason"].startswith("Must home axis first")


def test_completed_record_carries_the_captured_reason(dirs):
    stop_capture.capture("v12.gcode", "shutdown", fetch=fake_moonraker(message="Timer too close"),
                         now=NOW, log=lambda _: None)
    built = records.build_record_data("v12.gcode", {"state": "error"}, {}, {"started_at": "2026-11-05T14:49:47Z"})
    assert built["stop_reason"] == "Timer too close"
    assert built["stop_capture"].endswith("v12.gcode.json")
    assert "stop_reason" not in records.build_record_data("other.gcode", {"state": "complete"}, {}, {})


def test_an_earlier_print_of_the_same_file_keeps_its_record_and_reason(dirs):
    old = dirs / "Prints" / "v12-old.json"
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps({"gcode_file": "v12.gcode", "started_at": "2026-11-04T10:00:00Z",
                               "completed_at": "2026-11-04T11:00:00Z"}))
    stop_capture.capture("v12.gcode", "shutdown", fetch=fake_moonraker(message="Timer too close"),
                         now=NOW, log=lambda _: None)
    assert "stop_reason" not in json.loads(old.read_text())
    # A reprint that starts after the stop gets neither the old reason nor the old sidecar.
    assert records._stop_fields("v12.gcode", "2026-11-06T09:00:00Z") == {}

    def reprint(path, range_bytes=None):
        if path.startswith("/server/history/list"):
            return {"result": {"jobs": [{"filename": "v12.gcode", "start_time": NOW + 86400}]}}
        return fake_moonraker(klippy="ready", message="Printer is ready")(path, range_bytes)
    out = stop_capture.capture("v12.gcode", "cancel", fetch=reprint, now=NOW + 90000, log=lambda _: None)
    assert out["stop_reason"] == "" and [e["trigger"] for e in out["events"]] == ["cancel"]
